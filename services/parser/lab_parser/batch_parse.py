"""Parse a document in bounded page batches inside one job (PM-DECISION B, Refs #7).

Uses the pure ``batch_coverage`` plan and the ``batch_retry`` retry rules:

* ``plan_batches`` splits the document into batches (default 10 core pages); each batch is parsed
  from its **core pages only** (overlap context is for later LLM stages, so a page is never parsed
  twice and no evidence is duplicated);
* at most ``limits.parallel_batches`` (default 2) batch children run at once;
* failed pages are retried with ``derive_retry_plan``/``execute_and_merge_retry``, one page at a
  time, for at most ``retry_rounds`` rounds (the mandatory retry budget);
* the per-batch results are merged into ONE ``NormalizedBatch`` with original physical page indices
  and per-page statuses, which the existing persistence stores unchanged.

A page that is still failed when the budget is spent raises ``PartialParseFailure``: a partial result
is never published as a success (the good pages are not stored either). ``needs_review`` pages are
the normalizer's honest "no attested geometry / blank" answer and do not fail the job.

Crash semantics: batch progress is not durable; a crashed job re-runs every batch (documented in
docs/PARSE_WORKER.md). Cancellation stops every running child (each is its own process group) and
raises ``InspectionError("cancelled")``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timezone
import threading
import time
from typing import Callable, Sequence

from lab_contracts.parse import PageMapEntry, PageStatus, ParseResult

from .batch_coverage import BatchLimits, BatchPageReport, BatchResultInput, evaluate_coverage, plan_batches
from .batch_retry import RetryTargetPage, derive_retry_plan, execute_and_merge_retry
from .isolation import InspectionError, inspect_geometry
from .normalize import Artifact, NormalizedBatch
from .unstructured_runner import LOST_TEXT_DIAGNOSTIC, UNSAFE_CODES, run_unstructured_fast

__all__ = ["DEFAULT_LIMITS", "MAX_RETRY_ROUNDS", "PartialParseFailure", "parse_in_batches"]

DEFAULT_LIMITS = BatchLimits(max_pages=2000, batch_size=10, overlap_pages=0, parallel_batches=2)
MAX_RETRY_ROUNDS = 5
# A child failure with one of these codes can succeed on a later attempt of the whole job.
TRANSIENT_CODES = frozenset({"timeout", "process_unavailable", "inspection_io_failed"})
# These are about this process or this input, not about one batch: retrying them cannot help.
SYSTEMIC_CODES = frozenset({"unsupported_platform", "invalid_input", "invalid_limits", "invalid_pdf", "encrypted_pdf",
                            "page_limit_exceeded", "input_size"})
MAX_FAILED_FRACTION = 0.5     # more failed pages than this after the first pass = systemic: no page-by-page retry
JOB_DEADLINE_SECONDS = 1800.0


class PartialParseFailure(Exception):
    """Some core pages are still failed after the retry budget; nothing is published."""

    code = "pages_failed"

    def __init__(self, failed_pages: Sequence[int]):
        self.failed_pages = tuple(failed_pages)
        super().__init__(self.code)


@dataclass
class _Page:
    status: str
    diagnostics: list
    evidence: list
    observations: list
    source: NormalizedBatch | None  # the run this page's content came from (None: failed, no content)


def _failed(code: str) -> _Page:
    return _Page("failed", [code], [], [], None)


def _pages_of(run: NormalizedBatch) -> dict[int, _Page]:
    if run.quarantine_reason is not None:
        return {p.page_index: _failed(run.quarantine_reason) for p in run.result.pages}
    pages = {}
    for status in run.result.pages:
        pages[status.page_index] = _Page(
            status.status, list(status.diagnostics),
            [e for e in run.evidence if e.regions[0].page_index == status.page_index],
            [o for o in run.observations if o.page_index == status.page_index], run)
    return pages


def parse_in_batches(
    data: bytes,
    *,
    document_id: str,
    budget_ref: str,
    operation_id: str,
    run_batch: Callable[..., NormalizedBatch] = run_unstructured_fast,
    limits: BatchLimits = DEFAULT_LIMITS,
    retry_rounds: int = 2,
    cancelled: Callable[[], bool] | None = None,
    python: str | None = None,
    job_deadline_seconds: float = JOB_DEADLINE_SECONDS,
    max_failed_fraction: float = MAX_FAILED_FRACTION,
) -> NormalizedBatch:
    """Return one merged ``NormalizedBatch`` for all pages.

    Raises ``InspectionError`` for a systemic failure (invalid input or platform, cancel, the job
    deadline, or every batch failing with the same transient code, so the caller can requeue the
    job exactly as for an unbatched run) and ``PartialParseFailure`` for pages that stay failed.
    Bounded cost: ``job_deadline_seconds`` ends the job (``timeout``, transient) before any further
    child starts, and when more than ``max_failed_fraction`` of the pages failed the first pass the
    cause is systemic, so no page-by-page retry is attempted.
    """
    if type(retry_rounds) is not int or not 0 <= retry_rounds <= MAX_RETRY_ROUNDS:
        raise InspectionError("invalid_input")
    cancelled = cancelled or (lambda: False)
    deadline = time.monotonic() + job_deadline_seconds
    geometry = inspect_geometry(data, cancelled=cancelled)
    plan = plan_batches(document_id=document_id, document_sha256=geometry.sha256,
                        page_count=len(geometry.pages), limits=limits)
    outcome: dict[int, _Page] = {}
    lock = threading.Lock()

    codes: dict[str, str] = {}
    stop = threading.Event()   # set by the first unexpected failure: queued batches then never start a child

    def run(pages: Sequence[int], op: str) -> dict[int, _Page]:
        if cancelled() or stop.is_set():
            raise InspectionError("cancelled")
        if time.monotonic() >= deadline:
            raise InspectionError("timeout")
        try:
            batch = run_batch(data, geometry=geometry, page_indices=list(pages), cancelled=cancelled, python=python,
                              document_id=document_id, budget_ref=budget_ref, operation_id=op)
        except InspectionError as exc:
            if exc.code == "cancelled" or cancelled() or exc.code in SYSTEMIC_CODES:
                raise
            codes[op] = exc.code
            return {p: _failed(exc.code) for p in pages}
        except BaseException:
            stop.set()
            raise
        return _pages_of(batch)

    def report(page: int, state: _Page, op: str, attempt: int | None) -> BatchPageReport:
        return BatchPageReport(page_index=page, status=state.status, diagnostics=tuple(state.diagnostics),
                               role="core", operation_id=op, attempt=attempt)

    with ThreadPoolExecutor(limits.parallel_batches, thread_name_prefix="batch") as pool:
        futures = []
        for spec in plan.batches:
            op = f"{operation_id}-b{spec.batch_index}"
            futures.append((spec, op, pool.submit(run, spec.core_page_indices, op)))
        results = []
        error: BaseException | None = None
        for spec, op, future in futures:
            try:
                got = future.result()
            except BaseException as exc:  # noqa: BLE001 - cancel or an unexpected failure: stop everything
                stop.set()
                error = error or exc
                continue
            with lock:
                outcome.update(got)
            results.append(BatchResultInput(
                batch_id=spec.batch_id, batch_index=spec.batch_index, operation_id=op,
                pages=tuple(report(p, got[p], op, None) for p in spec.core_page_indices)))
        if error is not None:
            raise error

    first_codes = [codes.get(r.operation_id) for r in results]
    if first_codes and all(c in TRANSIENT_CODES for c in first_codes):
        # Nothing worked and every failure was transient (even with different codes): a whole-job
        # transient failure the consumer can requeue, not page failures.
        raise InspectionError(first_codes[0])
    coverage = evaluate_coverage(plan, results)
    if len(coverage.failed_pages) + len(coverage.missing_pages) > max_failed_fraction * len(geometry.pages):
        raise PartialParseFailure(sorted(set(coverage.failed_pages) | set(coverage.missing_pages)))
    history: tuple = ()
    prior = results
    for round_number in range(1, retry_rounds + 1):
        if not coverage.retry_page_indices:
            break
        retry_op = f"{operation_id}-r{round_number}"
        retry_plan = derive_retry_plan(plan, prior, retry_operation_id=retry_op, attempt=round_number,
                                       prior_history=history)

        def callback(target: RetryTargetPage) -> BatchPageReport:
            got = run([target.page_index], f"{retry_op}-p{target.page_index}")
            state = got[target.page_index]
            if state.status != "failed" or outcome[target.page_index].status == "failed":
                outcome[target.page_index] = state  # a retry never replaces content with a worse state
            return report(target.page_index, state, retry_op, round_number)

        prior_sequence = prior.merged_results if hasattr(prior, "merged_results") else prior
        merged = execute_and_merge_retry(plan, prior_sequence, retry_plan, callback, prior_history=history)
        prior, history, coverage = merged, merged.operation_history, merged.coverage_report

    failed = sorted(set(coverage.failed_pages) | set(coverage.missing_pages))
    if failed:
        raise PartialParseFailure(failed)
    merged = _merge(outcome, operation_id, len(geometry.pages))
    quarantined = any(code in p.diagnostics for p in merged.result.pages for code in UNSAFE_CODES)
    # Observations of hidden-text pages are dropped by the runner, so such a page proves elements existed.
    if (not merged.observations and not quarantined
            and any(LOST_TEXT_DIAGNOSTIC in p.diagnostics for p in merged.result.pages)):
        # The WHOLE document produced no element while pypdf finds text on some page: the parser silently
        # lost the text (for example its spaCy model could not be installed). Transient: never a clean
        # empty success. With any element anywhere, a suspect page stays needs_review with a diagnostic.
        raise InspectionError("text_extraction_empty")
    return merged


def _merge(outcome: dict[int, _Page], operation_id: str, page_count: int) -> NormalizedBatch:
    """Merge the per-page outcomes into one batch.

    The merged manifest numbers parser pages as physical page + 1. The raw artifacts it references
    are the per-run upstream outputs, which keep their own sub-document ``page_number``; each run's
    own manifest artifact (with its true ``page_index_map``) is kept in ``artifacts`` for that
    reason, so re-normalizing a stored raw output must use its run manifest, not the merged one.
    Evidence ids depend on the batch plan and the retry history (a different batch size or a retried
    page gives different raw artifacts and so different ids); they are stable only for the same
    plan and outcome.
    """
    ordered = [outcome[p] for p in range(page_count)]
    runs = list(dict.fromkeys(id(s.source) for s in ordered if s.source is not None))
    sources = {id(s.source): s.source for s in ordered if s.source is not None}
    first = sources[runs[0]]
    manifests = [sources[r].manifest for r in runs]
    manifest = first.manifest.model_copy(update={
        "page_selection": list(range(page_count)),
        "page_index_map": [PageMapEntry(parser_page=i + 1, page_index=i) for i in range(page_count)],
        "started_at": min(m.started_at for m in manifests).astimezone(timezone.utc),
        "finished_at": max(m.finished_at for m in manifests).astimezone(timezone.utc),
        "warnings": [], "fallback_reason": None,
    })
    artifacts, seen = [], set()
    raw_refs: list[str] = []
    for r in runs:
        for artifact in sources[r].artifacts:
            if artifact.ref not in seen:
                seen.add(artifact.ref)
                artifacts.append(artifact)
        raw_refs.extend(ref for ref in sources[r].result.raw_result_refs if ref not in raw_refs)
    manifest_artifact = Artifact(manifest.model_dump_json().encode())
    if manifest_artifact.ref not in seen:
        artifacts.append(manifest_artifact)
    evidence = [e for page in ordered for e in page.evidence]
    result = ParseResult(
        operation_id=operation_id, manifest_ref=manifest_artifact.ref, raw_result_refs=raw_refs,
        normalized_evidence_refs=[e.evidence_id for e in evidence],
        pages=[PageStatus(page_index=i, status=p.status, diagnostics=p.diagnostics) for i, p in enumerate(ordered)])
    return NormalizedBatch(manifest, first.capabilities, result, tuple(artifacts), tuple(evidence),
                           tuple(o for page in ordered for o in page.observations), None)
