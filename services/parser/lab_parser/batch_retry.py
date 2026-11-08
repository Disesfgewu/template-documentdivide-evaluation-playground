"""Bounded deterministic retry derivation, callback execution, and coverage merge.

Implements the pure-Python retry foundation for SPEC 2, 4.2, 16.6, and 19.4:
- Derives executable failed/missing-only original physical page selections from
  validated prior results without re-scheduling successful core pages.
- Retains per-page successful results, prior operation provenance, and immutable
  originals without reprocessing them.
- Explicit parent plan/result identity, retry operation identity, and attempt
  provenance.
- Fails closed for cross-document/plan, unexpected page, duplicate conflicting
  results, altering successful/partial/needs_review core outcomes, or overlap
  upgrading core coverage.
- Bounded callback execution accepting ONLY retry core pages (adjacent overlap
  may be referenced from existing results, never silently reprocessed).
- Merges retry outcomes preserving prior operation history, immutable originals,
  and coverage calculation using the existing evaluate_coverage contract.
- Finite, caller-explicit execution with zero calls when no retries are required.
- Failed/missing pages remain visible; full_document flags remain honest.
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
    "MergedRetryResult",
    "OperationAttemptRecord",
    "RetryPlan",
    "RetryTargetPage",
    "derive_retry_plan",
    "execute_and_merge_retry",
    "execute_retry",
    "merge_retry_results",
]


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
    """Specification of an executable failed or missing core page target.

    Contains strictly failed or missing physical core page indices derived from
    validated prior coverage results. Includes read-only references to existing
    adjacent overlap reports from prior results so callbacks may reference
    boundary context without reprocessing successful pages.
    """

    page_index: int
    owner_batch_id: str
    owner_batch_index: int
    batch_page_offset: int
    prior_status: PageStatusCode
    prior_diagnostics: tuple[str, ...]
    prior_operation_id: str | None
    referenced_overlap_indices: tuple[int, ...]
    referenced_overlap_reports: tuple[BatchPageReport, ...]

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


@dataclass(frozen=True)
class RetryPlan:
    """Explicit, immutable plan for retrying failed or missing core pages."""

    rule_version: str
    document_id: str
    document_sha256: str
    plan_id: str
    plan_sha256: str
    prior_report_sha256: str
    retry_operation_id: str
    attempt: int
    target_pages: tuple[RetryTargetPage, ...]
    target_page_indices: tuple[int, ...]
    target_batch_ids: tuple[str, ...]
    retry_plan_sha256: str


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


def derive_retry_plan(
    plan: BatchPlan,
    prior_results: Sequence[BatchResultInput],
    *,
    retry_operation_id: str,
    attempt: int = 1,
    prior_report: CoverageReport | None = None,
) -> RetryPlan:
    """Derive executable failed/missing-only physical page targets from validated prior results.

    Successful (``ok``), ``partial``, and ``needs_review`` core pages are never
    scheduled for retry (SPEC 4.2, T07).
    """
    batches_by_id = _validate_plan_integrity(plan)
    op_id = _require_non_empty_id(retry_operation_id, "retry_operation_id")
    att = _require_strict_int(attempt, "attempt", minimum=1)

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

        target_pages.append(
            RetryTargetPage(
                page_index=p_idx,
                owner_batch_id=spec.batch_id,
                owner_batch_index=spec.batch_index,
                batch_page_offset=entry.batch_page_offset,
                prior_status=item.status,
                prior_diagnostics=item.diagnostics,
                prior_operation_id=prior_res.operation_id if prior_res else None,
                referenced_overlap_indices=tuple(referenced_indices),
                referenced_overlap_reports=tuple(referenced_reports),
            )
        )
        target_page_indices.append(p_idx)

    target_page_set = set(target_page_indices)
    target_batch_ids = tuple(
        b.batch_id
        for b in plan.batches
        if any(p in target_page_set for p in b.core_page_indices)
    )

    plan_payload = {
        "rule_version": BATCH_PLAN_RULE_VERSION,
        "document_id": plan.document_id,
        "document_sha256": plan.document_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "prior_report_sha256": base_report.report_sha256,
        "retry_operation_id": op_id,
        "attempt": att,
        "target_page_indices": target_page_indices,
        "target_batch_ids": list(target_batch_ids),
    }
    retry_sha256 = _canonical_hash(plan_payload)

    return RetryPlan(
        rule_version=BATCH_PLAN_RULE_VERSION,
        document_id=plan.document_id,
        document_sha256=plan.document_sha256,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        prior_report_sha256=base_report.report_sha256,
        retry_operation_id=op_id,
        attempt=att,
        target_pages=tuple(target_pages),
        target_page_indices=tuple(target_page_indices),
        target_batch_ids=target_batch_ids,
        retry_plan_sha256=retry_sha256,
    )


def execute_retry(
    retry_plan: RetryPlan,
    callback: Callable[[RetryTargetPage], BatchPageReport],
) -> tuple[BatchPageReport, ...]:
    """Execute a finite, bounded retry callback accepting ONLY retry core pages.

    If retry_plan has zero targets, executes zero calls to callback.
    """
    if not isinstance(retry_plan, RetryPlan):
        raise ValueError("invalid_retry_plan: must be a RetryPlan instance")
    if not callable(callback):
        raise ValueError("invalid_callback: must be callable")

    if not retry_plan.target_pages:
        return ()

    outcomes: list[BatchPageReport] = []
    for target in retry_plan.target_pages:
        rep = callback(target)
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
        outcomes.append(rep)

    return tuple(outcomes)


def merge_retry_results(
    plan: BatchPlan,
    prior_results: Sequence[BatchResultInput],
    retry_plan: RetryPlan,
    retry_outcomes: Sequence[BatchPageReport],
) -> MergedRetryResult:
    """Merge retry page outcomes with prior results, preserving prior history and immutable originals.

    Fails closed for cross-document/plan, unexpected pages, duplicate conflicting
    results, changing successful/partial/needs_review core outcomes, or overlap
    upgrading core coverage.
    """
    batches_by_id = _validate_plan_integrity(plan)
    if not isinstance(retry_plan, RetryPlan):
        raise ValueError("invalid_retry_plan: must be a RetryPlan instance")

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

    expected_plan_payload = {
        "rule_version": retry_plan.rule_version,
        "document_id": retry_plan.document_id,
        "document_sha256": retry_plan.document_sha256,
        "plan_id": retry_plan.plan_id,
        "plan_sha256": retry_plan.plan_sha256,
        "prior_report_sha256": retry_plan.prior_report_sha256,
        "retry_operation_id": retry_plan.retry_operation_id,
        "attempt": retry_plan.attempt,
        "target_page_indices": list(retry_plan.target_page_indices),
        "target_batch_ids": list(retry_plan.target_batch_ids),
    }
    if retry_plan.retry_plan_sha256 != _canonical_hash(expected_plan_payload):
        raise ValueError("tampered_retry_plan: retry_plan_sha256 mismatch")

    prior_report = evaluate_coverage(plan, prior_results)
    if prior_report.report_sha256 != retry_plan.prior_report_sha256:
        raise ValueError(
            "prior_report_mismatch: prior_results do not match retry_plan base report"
        )

    if not isinstance(retry_outcomes, (list, tuple)):
        raise ValueError("invalid_retry_outcomes: must be a sequence of BatchPageReport")

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

        if p_idx in seen_retry:
            prev = seen_retry[p_idx]
            if (
                prev.status != outcome.status
                or prev.diagnostics != outcome.diagnostics
                or prev.role != outcome.role
                or prev.operation_id != outcome.operation_id
                or prev.attempt != outcome.attempt
            ):
                raise ValueError(
                    f"conflicting_duplicate_retry_page_result: page {p_idx} reported multiple times with differing outcomes"
                )
            continue
        seen_retry[p_idx] = outcome

    prior_by_batch_id: dict[str, BatchResultInput] = {
        res.batch_id: res for res in prior_results
    }

    merged_results: list[BatchResultInput] = []
    for spec in plan.batches:
        prior_b_res = prior_by_batch_id.get(spec.batch_id)
        if prior_b_res is not None:
            new_pages: list[BatchPageReport] = []
            any_retried_in_batch = False
            for p_rep in prior_b_res.pages:
                role = next(
                    e.role for e in spec.page_entries if e.page_index == p_rep.page_index
                )
                if role == "core" and p_rep.page_index in seen_retry:
                    retry_rep = seen_retry[p_rep.page_index]
                    new_pages.append(
                        BatchPageReport(
                            page_index=p_rep.page_index,
                            status=retry_rep.status,
                            diagnostics=retry_rep.diagnostics,
                            role="core",
                            operation_id=retry_rep.operation_id
                            or retry_plan.retry_operation_id,
                            attempt=retry_rep.attempt
                            if retry_rep.attempt is not None
                            else retry_plan.attempt,
                        )
                    )
                    any_retried_in_batch = True
                else:
                    new_pages.append(p_rep)

            existing_indices = {p.page_index for p in prior_b_res.pages}
            for c_idx in spec.core_page_indices:
                if c_idx not in existing_indices and c_idx in seen_retry:
                    retry_rep = seen_retry[c_idx]
                    new_pages.append(
                        BatchPageReport(
                            page_index=c_idx,
                            status=retry_rep.status,
                            diagnostics=retry_rep.diagnostics,
                            role="core",
                            operation_id=retry_rep.operation_id
                            or retry_plan.retry_operation_id,
                            attempt=retry_rep.attempt
                            if retry_rep.attempt is not None
                            else retry_plan.attempt,
                        )
                    )
                    any_retried_in_batch = True

            batch_op_id = (
                retry_plan.retry_operation_id
                if any_retried_in_batch
                else prior_b_res.operation_id
            )
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
                    retry_rep = seen_retry[c_idx]
                    new_pages.append(
                        BatchPageReport(
                            page_index=c_idx,
                            status=retry_rep.status,
                            diagnostics=retry_rep.diagnostics,
                            role="core",
                            operation_id=retry_rep.operation_id
                            or retry_plan.retry_operation_id,
                            attempt=retry_rep.attempt
                            if retry_rep.attempt is not None
                            else retry_plan.attempt,
                        )
                    )
            if new_pages:
                merged_results.append(
                    BatchResultInput(
                        batch_id=spec.batch_id,
                        batch_index=spec.batch_index,
                        pages=tuple(sorted(new_pages, key=lambda x: x.page_index)),
                        operation_id=retry_plan.retry_operation_id,
                    )
                )

    new_coverage_report = evaluate_coverage(plan, merged_results)

    history: list[OperationAttemptRecord] = []
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
) -> MergedRetryResult:
    """Convenience helper to execute bounded retry callback and merge outcomes."""
    outcomes = execute_retry(retry_plan, callback)
    return merge_retry_results(plan, prior_results, retry_plan, outcomes)
