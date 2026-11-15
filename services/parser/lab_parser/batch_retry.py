"""Bounded deterministic retry derivation, callback execution, and coverage merge.

Implements the pure-Python retry foundation for SPEC 2, 4.2, 16.6, and 19.4:
- Derives executable failed/missing-only original physical page selections from
  validated prior results without re-scheduling successful core pages.
- Validates source plan, prior results snapshot, and prior history snapshot
  BEFORE executing callbacks. The canonical history and results digests are
  for structural integrity and staleness detection (not cryptographic authentication).
- Retains per-page successful results, prior operation provenance, and immutable
  originals across multiple retry rounds without reprocessing them.
- Materializes inherited known parent batch operation_id onto retained page projections,
  while truly unknown legacy operations remain unknown (never fabricated).
- Binds canonical source BatchResultInput signatures and operation attempt records.
- Enforces strict attempt progression and fresh operation identity; rejects
  spoofed outcome provenance.
- Fails closed for cross-document/plan, unexpected pages, duplicate conflicting
  results, altering successful/partial/needs_review core outcomes, or overlap
  upgrading core coverage.
- Bounded callback execution accepting ONLY retry core pages (adjacent overlap
  may be referenced from existing results, never silently reprocessed).
- Truthful execution selection metadata: RetryTargetPage.is_full_document is True
  only if the source document itself has one page.
- Wraps all callback execution and validation failures in RetryExecutionError to
  preserve recoverable partial progress without losing completed pages.
  Unexecuted targets remain visible as failed/missing without fabricating success.
- Finite, caller-explicit execution with zero calls when no retries are required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from lab_parser.batch_coverage import (
    BATCH_PLAN_RULE_VERSION,
    BatchPageReport,
    BatchPlan,
    BatchResultInput,
    BatchSpec,
    CoverageReport,
    PageCoverageItem,
    PageRole,
    PageStatusCode,
    ReportedPageStatusCode,
    _canonical_hash,
    _require_diagnostics,
    _require_non_empty_id,
    _require_sha256,
    _require_strict_int,
    _validate_plan_integrity,
    _validate_report_integrity,
    evaluate_coverage,
)

__all__ = [
    "MAX_BATCH_RESULTS_INPUT",
    "MAX_HISTORY_RECORDS",
    "MAX_RETRY_OUTCOMES_INPUT",
    "MergedRetryResult",
    "OperationAttemptRecord",
    "RetryExecutionError",
    "RetryPlan",
    "RetryTargetPage",
    "derive_retry_plan",
    "execute_and_merge_retry",
    "execute_retry",
    "merge_retry_results",
]

MAX_BATCH_RESULTS_INPUT = 4000
MAX_HISTORY_RECORDS = 4000
MAX_RETRY_OUTCOMES_INPUT = 4000


class RetryExecutionError(RuntimeError):
    """Raised when a retry callback fails after one or more pages were processed.

    Preserves completed outcomes and optional partial merged result so caller
    does not lose recoverable progress.
    """

    def __init__(
        self,
        message: str,
        *,
        completed_outcomes: Sequence[BatchPageReport],
        failed_target: RetryTargetPage,
        cause: BaseException,
        partial_merged_result: MergedRetryResult | None = None,
    ) -> None:
        super().__init__(message)
        self.completed_outcomes = tuple(completed_outcomes)
        self.failed_target = failed_target
        self.cause = cause
        self.partial_merged_result = partial_merged_result


@dataclass(frozen=True)
class OperationAttemptRecord:
    """Provenance record for a batch or retry execution attempt."""

    operation_id: str
    attempt: int
    page_indices: tuple[int, ...]
    batch_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty_id(self.operation_id, "operation_id")
        _require_strict_int(self.attempt, "attempt", minimum=0)
        if not isinstance(self.page_indices, (list, tuple)):
            raise ValueError("invalid_page_indices: must be a tuple or list")
        for p in self.page_indices:
            _require_strict_int(p, "page_indices", minimum=0)
        if not isinstance(self.batch_ids, (list, tuple)):
            raise ValueError("invalid_batch_ids: must be a tuple or list")
        for b in self.batch_ids:
            _require_non_empty_id(b, "batch_ids")
        object.__setattr__(self, "page_indices", tuple(self.page_indices))
        object.__setattr__(self, "batch_ids", tuple(self.batch_ids))


@dataclass(frozen=True)
class RetryTargetPage:
    """Specification of an executable failed or missing core page target."""

    page_index: int
    owner_batch_id: str
    owner_batch_index: int
    batch_page_offset: int
    prior_status: PageStatusCode
    prior_diagnostics: tuple[str, ...]
    prior_operation_id: str | None
    referenced_overlap_indices: tuple[int, ...]
    referenced_overlap_reports: tuple[BatchPageReport, ...]
    is_full_document: bool

    def __post_init__(self) -> None:
        _require_strict_int(self.page_index, "page_index", minimum=0)
        _require_non_empty_id(self.owner_batch_id, "owner_batch_id")
        _require_strict_int(self.owner_batch_index, "owner_batch_index", minimum=0)
        _require_strict_int(self.batch_page_offset, "batch_page_offset", minimum=0)
        if self.prior_status not in ("failed", "missing"):
            raise ValueError(
                f"invalid_prior_status_for_retry: expected 'failed' or 'missing', got {self.prior_status!r}"
            )
        diag = _require_diagnostics(self.prior_diagnostics, "prior_diagnostics")
        object.__setattr__(self, "prior_diagnostics", diag)
        if self.prior_operation_id is not None:
            _require_non_empty_id(self.prior_operation_id, "prior_operation_id")
        if not isinstance(self.referenced_overlap_indices, (list, tuple)):
            raise ValueError("invalid_referenced_overlap_indices: must be a sequence")
        for p in self.referenced_overlap_indices:
            _require_strict_int(p, "referenced_overlap_indices", minimum=0)
        object.__setattr__(
            self, "referenced_overlap_indices", tuple(self.referenced_overlap_indices)
        )
        if not isinstance(self.referenced_overlap_reports, (list, tuple)):
            raise ValueError("invalid_referenced_overlap_reports: must be a sequence")
        for rep in self.referenced_overlap_reports:
            if not isinstance(rep, BatchPageReport):
                raise ValueError("invalid_referenced_overlap_report: expected BatchPageReport")
        object.__setattr__(
            self, "referenced_overlap_reports", tuple(self.referenced_overlap_reports)
        )
        if not isinstance(self.is_full_document, bool):
            raise ValueError("invalid_is_full_document: must be a bool")


@dataclass(frozen=True)
class RetryPlan:
    """Explicit, immutable plan binding source plan and prior results to retry targets."""

    rule_version: str
    document_id: str
    document_sha256: str
    plan_id: str
    plan_sha256: str
    prior_results_sha256: str
    prior_report_sha256: str
    prior_history_sha256: str
    retry_operation_id: str
    attempt: int
    target_pages: tuple[RetryTargetPage, ...]
    target_page_indices: tuple[int, ...]
    target_batch_ids: tuple[str, ...]
    retry_plan_sha256: str
    plan: BatchPlan
    prior_results: tuple[BatchResultInput, ...]
    prior_history: tuple[OperationAttemptRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan, BatchPlan):
            raise ValueError("invalid_plan: must be a BatchPlan instance")
        if not isinstance(self.prior_results, (list, tuple)):
            raise ValueError("invalid_prior_results: must be a sequence of BatchResultInput")
        if len(self.prior_results) > MAX_BATCH_RESULTS_INPUT:
            raise ValueError("prior_results exceeds maximum allowed size")
        for r in self.prior_results:
            if not isinstance(r, BatchResultInput):
                raise ValueError("invalid_prior_results: elements must be BatchResultInput")
        object.__setattr__(self, "prior_results", tuple(self.prior_results))

        if not isinstance(self.prior_history, (list, tuple)):
            raise ValueError("invalid_prior_history: must be a sequence of OperationAttemptRecord")
        if len(self.prior_history) > MAX_HISTORY_RECORDS:
            raise ValueError("prior_history exceeds maximum allowed size")
        for h in self.prior_history:
            if not isinstance(h, OperationAttemptRecord):
                raise ValueError("invalid_prior_history: elements must be OperationAttemptRecord")
        object.__setattr__(self, "prior_history", tuple(self.prior_history))

        if not isinstance(self.target_pages, (list, tuple)):
            raise ValueError("invalid_target_pages: must be a sequence of RetryTargetPage")
        if len(self.target_pages) > self.plan.page_count:
            raise ValueError("target_pages exceeds plan page_count")
        object.__setattr__(self, "target_pages", tuple(self.target_pages))

        if not isinstance(self.target_page_indices, (list, tuple)):
            raise ValueError("invalid_target_page_indices: must be a sequence of int")
        object.__setattr__(self, "target_page_indices", tuple(self.target_page_indices))

        if not isinstance(self.target_batch_ids, (list, tuple)):
            raise ValueError("invalid_target_batch_ids: must be a sequence of str")
        object.__setattr__(self, "target_batch_ids", tuple(self.target_batch_ids))


@dataclass(frozen=True)
class MergedRetryResult:
    """Result of merging retry page outcomes into prior batch results."""

    plan_id: str
    plan_sha256: str
    retry_operation_id: str
    attempt: int
    merged_results: tuple[BatchResultInput, ...]
    prior_results: tuple[BatchResultInput, ...]
    retry_outcomes: tuple[BatchPageReport, ...]
    coverage_report: CoverageReport
    operation_history: tuple[OperationAttemptRecord, ...]


def _prior_results_hash(results: Sequence[BatchResultInput]) -> str:
    canonical_list = [
        {
            "batch_id": r.batch_id,
            "batch_index": r.batch_index,
            "operation_id": r.operation_id,
            "pages": [
                {
                    "page_index": p.page_index,
                    "status": p.status,
                    "diagnostics": list(p.diagnostics),
                    "role": p.role,
                    "operation_id": p.operation_id,
                    "attempt": p.attempt,
                }
                for p in sorted(r.pages, key=lambda x: x.page_index)
            ],
        }
        for r in sorted(results, key=lambda x: (x.batch_index, x.batch_id))
    ]
    return _canonical_hash({"results": canonical_list})


def _history_hash(history: Sequence[OperationAttemptRecord]) -> str:
    canonical_list = [
        {
            "operation_id": h.operation_id,
            "attempt": h.attempt,
            "page_indices": list(h.page_indices),
            "batch_ids": list(h.batch_ids),
        }
        for h in history
    ]
    return _canonical_hash({"history": canonical_list})


def _validate_history_records(
    plan: BatchPlan,
    history: Sequence[OperationAttemptRecord],
) -> None:
    if len(history) > MAX_HISTORY_RECORDS:
        raise ValueError("prior_history exceeds maximum allowed size")
    plan_batch_map = {b.batch_id: b for b in plan.batches}
    seen_ops: set[str] = set()
    for h in history:
        if not isinstance(h, OperationAttemptRecord):
            raise ValueError("invalid_history_record: must be OperationAttemptRecord")
        if h.operation_id in seen_ops:
            raise ValueError(f"duplicate_history_operation_id: {h.operation_id!r}")
        seen_ops.add(h.operation_id)

        for b_id in h.batch_ids:
            if b_id not in plan_batch_map:
                raise ValueError(f"unknown_history_batch_id: {b_id!r} not in plan")

        window_pages: set[int] = set()
        for b_id in h.batch_ids:
            window_pages.update(plan_batch_map[b_id].window_page_indices)

        for p_idx in h.page_indices:
            if p_idx < 0 or p_idx >= plan.page_count:
                raise ValueError(f"unknown_history_page_index: {p_idx} out of plan bounds")
            if h.batch_ids and p_idx not in window_pages:
                raise ValueError(
                    f"history_ownership_mismatch: page {p_idx} not in window of batch_ids {h.batch_ids}"
                )


def derive_retry_plan(
    plan: BatchPlan,
    prior_results: Sequence[BatchResultInput] | MergedRetryResult,
    *,
    retry_operation_id: str,
    attempt: int = 1,
    prior_report: CoverageReport | None = None,
    prior_history: Sequence[OperationAttemptRecord] = (),
) -> RetryPlan:
    """Derive executable failed/missing-only physical page targets from validated prior results."""
    batches_by_id = _validate_plan_integrity(plan)
    op_id = _require_non_empty_id(retry_operation_id, "retry_operation_id")
    att = _require_strict_int(attempt, "attempt", minimum=1)

    if isinstance(prior_results, MergedRetryResult):
        if (prior_results.plan_id != plan.plan_id
                or prior_results.plan_sha256 != plan.plan_sha256):
            raise ValueError("cross_document_or_plan_mismatch: merged source belongs to another plan")
        if prior_history and tuple(prior_history) != prior_results.operation_history:
            raise ValueError("prior_history_mismatch: cannot replace captured merged history")
        prior_history = prior_results.operation_history
        prior_results = prior_results.merged_results

    if not isinstance(prior_results, (list, tuple)):
        raise ValueError("invalid_prior_results: must be a sequence of BatchResultInput")
    if len(prior_results) > MAX_BATCH_RESULTS_INPUT:
        raise ValueError("prior_results exceeds maximum allowed size")
    for r in prior_results:
        if not isinstance(r, BatchResultInput):
            raise ValueError("invalid_prior_results: elements must be BatchResultInput")

    if not isinstance(prior_history, (list, tuple)):
        raise ValueError("invalid_prior_history: must be a sequence of OperationAttemptRecord")
    _validate_history_records(plan, prior_history)

    # Detect if prior_results contain retry attempts > 0 but caller omitted prior_history
    has_prior_retries = any(
        p.attempt is not None and p.attempt > 0 for r in prior_results for p in r.pages
    )
    if has_prior_retries and not prior_history:
        raise ValueError(
            "missing_required_prior_history: prior_results contain retry attempts > 0; caller must pass prior_history to preserve lineage"
        )

    # Check operation freshness and attempt progression
    prior_ops: set[str] = set()
    max_prior_attempt = 0
    for h in prior_history:
        prior_ops.add(h.operation_id)
        max_prior_attempt = max(max_prior_attempt, h.attempt)
    for r in prior_results:
        if r.operation_id is not None:
            prior_ops.add(r.operation_id)
        for p in r.pages:
            if p.operation_id is not None:
                prior_ops.add(p.operation_id)
            if p.attempt is not None:
                max_prior_attempt = max(max_prior_attempt, p.attempt)

    if op_id in prior_ops:
        raise ValueError(
            f"duplicate_operation_id: operation_id {op_id!r} was already used in prior operations"
        )
    if att <= max_prior_attempt:
        raise ValueError(
            f"invalid_attempt_progression: attempt {att} must be > max prior attempt {max_prior_attempt}"
        )

    # A nonempty history must account for every known current producer. Bounds
    # alone cannot prevent an unrelated but well-formed history dropping lineage.
    if prior_history:
        history_by_operation = {h.operation_id: h for h in prior_history}
        for result in prior_results:
            for page in result.pages:
                producer = page.operation_id or result.operation_id
                if producer is None:
                    continue
                record = history_by_operation.get(producer)
                if (record is None or page.page_index not in record.page_indices
                        or result.batch_id not in record.batch_ids
                        or (page.attempt is not None and page.attempt != record.attempt)):
                    raise ValueError("prior_history_mismatch: known source provenance is missing or inconsistent")

    eval_report = evaluate_coverage(plan, prior_results)
    if prior_report is not None:
        _validate_report_integrity(plan, prior_report)
        if prior_report.report_sha256 != eval_report.report_sha256:
            raise ValueError(
                "prior_report_mismatch: provided prior_report does not match prior_results evaluation"
            )
        base_report = prior_report
    else:
        base_report = eval_report

    prior_by_batch_id: dict[str, BatchResultInput] = {
        res.batch_id: res for res in prior_results
    }

    target_pages: list[RetryTargetPage] = []
    target_page_indices: list[int] = []

    for p_idx in base_report.retry_page_indices:
        item = base_report.pages[p_idx]
        spec = batches_by_id[item.owner_batch_id]
        entry = next(e for e in spec.page_entries if e.page_index == p_idx)
        prior_res = prior_by_batch_id.get(spec.batch_id)

        prior_page_rep: BatchPageReport | None = None
        if prior_res is not None:
            prior_page_rep = next(
                (p for p in prior_res.pages if p.page_index == p_idx), None
            )

        # Inherit known parent batch op if page op is None; truly unknown remains None
        if prior_page_rep and prior_page_rep.operation_id is not None:
            prior_p_op = prior_page_rep.operation_id
        elif prior_res and prior_res.operation_id is not None:
            prior_p_op = prior_res.operation_id
        else:
            prior_p_op = None

        referenced_indices: list[int] = []
        referenced_reports: list[BatchPageReport] = []
        for w_p_idx in spec.window_page_indices:
            if w_p_idx != p_idx:
                referenced_indices.append(w_p_idx)
                if prior_res is not None:
                    for page_rep in prior_res.pages:
                        if page_rep.page_index == w_p_idx:
                            referenced_reports.append(page_rep)
                            break

        is_full_doc = (plan.page_count == 1)

        target_pages.append(
            RetryTargetPage(
                page_index=p_idx,
                owner_batch_id=spec.batch_id,
                owner_batch_index=spec.batch_index,
                batch_page_offset=entry.batch_page_offset,
                prior_status=item.status,
                prior_diagnostics=item.diagnostics,
                prior_operation_id=prior_p_op,
                referenced_overlap_indices=tuple(referenced_indices),
                referenced_overlap_reports=tuple(referenced_reports),
                is_full_document=is_full_doc,
            )
        )
        target_page_indices.append(p_idx)

    target_page_set = set(target_page_indices)
    target_batch_ids = tuple(
        b.batch_id
        for b in plan.batches
        if any(p in target_page_set for p in b.core_page_indices)
    )

    prior_res_sha = _prior_results_hash(prior_results)
    prior_hist_sha = _history_hash(prior_history)

    plan_payload = {
        "rule_version": BATCH_PLAN_RULE_VERSION,
        "document_id": plan.document_id,
        "document_sha256": plan.document_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "prior_results_sha256": prior_res_sha,
        "prior_report_sha256": base_report.report_sha256,
        "prior_history_sha256": prior_hist_sha,
        "retry_operation_id": op_id,
        "attempt": att,
        "target_page_indices": target_page_indices,
        "target_batch_ids": list(target_batch_ids),
        "target_page_hashes": [
            _canonical_hash(
                {
                    "page_index": t.page_index,
                    "owner_batch_id": t.owner_batch_id,
                    "owner_batch_index": t.owner_batch_index,
                    "batch_page_offset": t.batch_page_offset,
                    "prior_status": t.prior_status,
                    "prior_diagnostics": list(t.prior_diagnostics),
                    "prior_operation_id": t.prior_operation_id,
                    "is_full_document": t.is_full_document,
                }
            )
            for t in target_pages
        ],
    }
    retry_sha256 = _canonical_hash(plan_payload)

    return RetryPlan(
        rule_version=BATCH_PLAN_RULE_VERSION,
        document_id=plan.document_id,
        document_sha256=plan.document_sha256,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        prior_results_sha256=prior_res_sha,
        prior_report_sha256=base_report.report_sha256,
        prior_history_sha256=prior_hist_sha,
        retry_operation_id=op_id,
        attempt=att,
        target_pages=tuple(target_pages),
        target_page_indices=tuple(target_page_indices),
        target_batch_ids=target_batch_ids,
        retry_plan_sha256=retry_sha256,
        plan=plan,
        prior_results=tuple(prior_results),
        prior_history=tuple(prior_history),
    )


def _validate_retry_plan_and_sources(
    plan: BatchPlan,
    prior_results: Sequence[BatchResultInput],
    retry_plan: RetryPlan,
    *,
    prior_history: Sequence[OperationAttemptRecord] = (),
) -> None:
    _validate_plan_integrity(plan)
    if not isinstance(retry_plan, RetryPlan):
        raise ValueError("invalid_retry_plan: must be a RetryPlan instance")
    if not isinstance(prior_results, (list, tuple)):
        raise ValueError("invalid_prior_results: must be a sequence of BatchResultInput")
    if len(prior_results) > MAX_BATCH_RESULTS_INPUT:
        raise ValueError("prior_results exceeds maximum allowed size")

    history_to_use = prior_history if prior_history else retry_plan.prior_history
    _validate_history_records(plan, history_to_use)

    if (
        retry_plan.document_id != plan.document_id
        or retry_plan.document_sha256 != plan.document_sha256
        or retry_plan.plan_id != plan.plan_id
        or retry_plan.plan_sha256 != plan.plan_sha256
        or retry_plan.rule_version != plan.rule_version
    ):
        raise ValueError(
            "cross_document_or_plan_mismatch: retry_plan does not match plan identity"
        )

    canonical_plan = derive_retry_plan(
        plan,
        prior_results,
        retry_operation_id=retry_plan.retry_operation_id,
        attempt=retry_plan.attempt,
        prior_history=history_to_use,
    )

    if (
        retry_plan.target_pages != canonical_plan.target_pages
        or retry_plan.target_page_indices != canonical_plan.target_page_indices
        or retry_plan.target_batch_ids != canonical_plan.target_batch_ids
        or retry_plan.prior_results_sha256 != canonical_plan.prior_results_sha256
        or retry_plan.prior_report_sha256 != canonical_plan.prior_report_sha256
        or retry_plan.prior_history_sha256 != canonical_plan.prior_history_sha256
        or retry_plan.prior_history != canonical_plan.prior_history
        or retry_plan.retry_plan_sha256 != canonical_plan.retry_plan_sha256
    ):
        raise ValueError(
            "tampered_or_stale_retry_plan: retry_plan does not match canonical derivation from source plan and results"
        )


def execute_retry(
    *args: Any,
    plan: BatchPlan | None = None,
    prior_results: Sequence[BatchResultInput] | None = None,
    retry_plan: RetryPlan | None = None,
    callback: Callable[[RetryTargetPage], BatchPageReport] | None = None,
    prior_history: Sequence[OperationAttemptRecord] | None = None,
) -> tuple[BatchPageReport, ...]:
    """Execute a finite, bounded retry callback accepting ONLY retry core pages."""
    if len(args) == 4:
        p_in, res_in, rp_in, cb_in = args
    elif len(args) == 2:
        rp_in, cb_in = args
        p_in = plan if plan is not None else rp_in.plan
        res_in = prior_results if prior_results is not None else rp_in.prior_results
    elif len(args) == 0:
        if retry_plan is None or callback is None:
            raise ValueError("execute_retry requires retry_plan and callback")
        rp_in = retry_plan
        cb_in = callback
        p_in = plan if plan is not None else rp_in.plan
        res_in = prior_results if prior_results is not None else rp_in.prior_results
    else:
        raise ValueError(f"invalid_arguments: expected 2 or 4 arguments, got {len(args)}")

    if not isinstance(rp_in, RetryPlan):
        raise ValueError("invalid_retry_plan: must be a RetryPlan instance")
    if not callable(cb_in):
        raise ValueError("invalid_callback: must be callable")

    history_in = (
        prior_history if prior_history is not None else rp_in.prior_history
    )

    _validate_retry_plan_and_sources(p_in, res_in, rp_in, prior_history=history_in)

    if not rp_in.target_pages:
        return ()

    outcomes: list[BatchPageReport] = []
    for target in rp_in.target_pages:
        try:
            rep = cb_in(target)
            if not isinstance(rep, BatchPageReport):
                raise ValueError(
                    f"invalid_callback_return: expected BatchPageReport for page {target.page_index}, got {type(rep).__name__}"
                )
            if rep.page_index != target.page_index:
                raise ValueError(
                    f"unexpected_retry_page: expected page_index {target.page_index}, got {rep.page_index}"
                )
            if rep.role is not None and rep.role != "core":
                raise ValueError(
                    f"overlap_cannot_upgrade_core_coverage: page {rep.page_index} must have role 'core', got {rep.role!r}"
                )
            if rep.operation_id is not None and rep.operation_id != rp_in.retry_operation_id:
                raise ValueError(
                    f"mismatched_retry_operation_id: expected {rp_in.retry_operation_id!r}, got {rep.operation_id!r}"
                )
            if rep.attempt is not None and rep.attempt != rp_in.attempt:
                raise ValueError(
                    f"mismatched_retry_attempt: expected {rp_in.attempt}, got {rep.attempt}"
                )
        except Exception as exc:
            # Wrap all callback exceptions and validation failures in RetryExecutionError
            raise RetryExecutionError(
                f"retry_callback_failed on page {target.page_index}: {exc}",
                completed_outcomes=tuple(outcomes),
                failed_target=target,
                cause=exc,
            ) from exc

        clean_rep = BatchPageReport(
            page_index=rep.page_index,
            status=rep.status,
            diagnostics=rep.diagnostics,
            role="core",
            operation_id=rp_in.retry_operation_id,
            attempt=rp_in.attempt,
        )
        outcomes.append(clean_rep)

    return tuple(outcomes)


def merge_retry_results(
    plan: BatchPlan,
    prior_results: Sequence[BatchResultInput],
    retry_plan: RetryPlan,
    retry_outcomes: Sequence[BatchPageReport],
    *,
    prior_history: Sequence[OperationAttemptRecord] = (),
) -> MergedRetryResult:
    """Merge retry page outcomes with prior results, preserving prior history and immutable originals."""
    history_in = prior_history if prior_history else retry_plan.prior_history
    _validate_retry_plan_and_sources(plan, prior_results, retry_plan, prior_history=history_in)

    if not isinstance(retry_outcomes, (list, tuple)):
        raise ValueError("invalid_retry_outcomes: must be a sequence of BatchPageReport")
    if len(retry_outcomes) > MAX_RETRY_OUTCOMES_INPUT:
        raise ValueError("retry_outcomes exceeds maximum allowed size")

    prior_report = evaluate_coverage(plan, prior_results)
    target_set = set(retry_plan.target_page_indices)
    seen_retry: dict[int, BatchPageReport] = {}

    for idx, outcome in enumerate(retry_outcomes):
        if not isinstance(outcome, BatchPageReport):
            raise ValueError(
                f"invalid_retry_outcomes[{idx}]: expected BatchPageReport, got {type(outcome).__name__}"
            )
        p_idx = outcome.page_index
        if p_idx < 0 or p_idx >= plan.page_count:
            raise ValueError(
                f"unexpected_page: page_index {p_idx} out of plan bounds"
            )

        prior_page_item = prior_report.pages[p_idx]
        if prior_page_item.status in ("ok", "partial", "needs_review"):
            raise ValueError(
                f"cannot_reprocess_or_alter_successful_core_page: page {p_idx} already has terminal or review outcome {prior_page_item.status!r}"
            )

        if p_idx not in target_set:
            raise ValueError(
                f"unexpected_retry_page: page {p_idx} was not scheduled in retry plan"
            )

        if outcome.role is not None and outcome.role != "core":
            raise ValueError(
                f"overlap_cannot_upgrade_core_coverage: retry page {p_idx} has role {outcome.role!r}, must be 'core'"
            )

        if outcome.operation_id is not None and outcome.operation_id != retry_plan.retry_operation_id:
            raise ValueError(
                f"mismatched_retry_operation_id: expected {retry_plan.retry_operation_id!r}, got {outcome.operation_id!r}"
            )
        if outcome.attempt is not None and outcome.attempt != retry_plan.attempt:
            raise ValueError(
                f"mismatched_retry_attempt: expected {retry_plan.attempt}, got {outcome.attempt}"
            )

        clean_outcome = BatchPageReport(
            page_index=p_idx,
            status=outcome.status,
            diagnostics=outcome.diagnostics,
            role="core",
            operation_id=retry_plan.retry_operation_id,
            attempt=retry_plan.attempt,
        )

        if p_idx in seen_retry:
            prev = seen_retry[p_idx]
            if (
                prev.status != clean_outcome.status
                or prev.diagnostics != clean_outcome.diagnostics
                or prev.role != clean_outcome.role
            ):
                raise ValueError(
                    f"conflicting_duplicate_retry_page_result: page {p_idx} reported multiple times with differing outcomes"
                )
            continue
        seen_retry[p_idx] = clean_outcome

    prior_by_batch_id: dict[str, BatchResultInput] = {
        res.batch_id: res for res in prior_results
    }

    merged_results: list[BatchResultInput] = []
    for spec in plan.batches:
        prior_b_res = prior_by_batch_id.get(spec.batch_id)
        if prior_b_res is not None:
            new_pages: list[BatchPageReport] = []
            for p_rep in prior_b_res.pages:
                role = next(
                    e.role for e in spec.page_entries if e.page_index == p_rep.page_index
                )
                if role == "core" and p_rep.page_index in seen_retry:
                    new_pages.append(seen_retry[p_rep.page_index])
                else:
                    # Materialize inherited known batchop onto retained page projection
                    if p_rep.operation_id is not None:
                        op = p_rep.operation_id
                        att = p_rep.attempt if p_rep.attempt is not None else 0
                    elif prior_b_res.operation_id is not None:
                        op = prior_b_res.operation_id
                        att = 0
                    else:
                        op = None
                        att = None

                    new_pages.append(
                        BatchPageReport(
                            page_index=p_rep.page_index,
                            status=p_rep.status,
                            diagnostics=p_rep.diagnostics,
                            role=p_rep.role,
                            operation_id=op,
                            attempt=att,
                        )
                    )

            existing_indices = {p.page_index for p in prior_b_res.pages}
            for c_idx in spec.core_page_indices:
                if c_idx not in existing_indices and c_idx in seen_retry:
                    new_pages.append(seen_retry[c_idx])

            producer_ops = {
                p.operation_id for p in new_pages if p.operation_id is not None
            }
            if len(producer_ops) == 1 and len(new_pages) == len(
                [p for p in new_pages if p.operation_id is not None]
            ):
                batch_op_id: str | None = next(iter(producer_ops))
            else:
                batch_op_id = None

            merged_results.append(
                BatchResultInput(
                    batch_id=spec.batch_id,
                    batch_index=spec.batch_index,
                    pages=tuple(sorted(new_pages, key=lambda x: x.page_index)),
                    operation_id=batch_op_id,
                )
            )
        else:
            new_pages = []
            for c_idx in spec.core_page_indices:
                if c_idx in seen_retry:
                    new_pages.append(seen_retry[c_idx])
            if new_pages:
                producer_ops = {
                    p.operation_id for p in new_pages if p.operation_id is not None
                }
                batch_op_id = (
                    next(iter(producer_ops)) if len(producer_ops) == 1 else None
                )
                merged_results.append(
                    BatchResultInput(
                        batch_id=spec.batch_id,
                        batch_index=spec.batch_index,
                        pages=tuple(sorted(new_pages, key=lambda x: x.page_index)),
                        operation_id=batch_op_id,
                    )
                )

    new_coverage_report = evaluate_coverage(plan, merged_results)

    history: list[OperationAttemptRecord] = []
    if history_in:
        history.extend(history_in)
    else:
        for res in prior_results:
            if res.operation_id is not None:
                history.append(
                    OperationAttemptRecord(
                        operation_id=res.operation_id,
                        attempt=0,
                        page_indices=tuple(sorted(p.page_index for p in res.pages)),
                        batch_ids=(res.batch_id,),
                    )
                )

    if seen_retry:
        retried_batches = tuple(
            sorted(
                {
                    spec.batch_id
                    for spec in plan.batches
                    if any(p in seen_retry for p in spec.core_page_indices)
                }
            )
        )
        history.append(
            OperationAttemptRecord(
                operation_id=retry_plan.retry_operation_id,
                attempt=retry_plan.attempt,
                page_indices=tuple(sorted(seen_retry.keys())),
                batch_ids=retried_batches,
            )
        )

    return MergedRetryResult(
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        retry_operation_id=retry_plan.retry_operation_id,
        attempt=retry_plan.attempt,
        merged_results=tuple(merged_results),
        prior_results=tuple(prior_results),
        retry_outcomes=tuple(seen_retry.values()),
        coverage_report=new_coverage_report,
        operation_history=tuple(history),
    )


def execute_and_merge_retry(
    plan: BatchPlan,
    prior_results: Sequence[BatchResultInput],
    retry_plan: RetryPlan,
    callback: Callable[[RetryTargetPage], BatchPageReport],
    *,
    prior_history: Sequence[OperationAttemptRecord] = (),
) -> MergedRetryResult:
    """Execute bounded retry callback and merge outcomes into prior results."""
    history_in = prior_history if prior_history else retry_plan.prior_history
    _validate_retry_plan_and_sources(plan, prior_results, retry_plan, prior_history=history_in)

    try:
        outcomes = execute_retry(
            plan=plan,
            prior_results=prior_results,
            retry_plan=retry_plan,
            callback=callback,
            prior_history=history_in,
        )
    except RetryExecutionError as exc:
        partial_merged: MergedRetryResult | None = None
        if exc.completed_outcomes:
            try:
                partial_merged = merge_retry_results(
                    plan,
                    prior_results,
                    retry_plan,
                    exc.completed_outcomes,
                    prior_history=history_in,
                )
            except Exception:
                partial_merged = None
        raise RetryExecutionError(
            str(exc),
            completed_outcomes=exc.completed_outcomes,
            failed_target=exc.failed_target,
            cause=exc.cause,
            partial_merged_result=partial_merged,
        ) from exc.cause

    return merge_retry_results(
        plan,
        prior_results,
        retry_plan,
        outcomes,
        prior_history=history_in,
    )
