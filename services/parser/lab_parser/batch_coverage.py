"""Bounded deterministic PDF page-batch planning and coverage accounting.

Implements the pure-Python foundation for SPEC 2 (500+ page batched processing),
4.2 (explicit tracking of failed/needs_review/uncovered pages; reuse of succeeded
pages on retry), 16.6 (page_batch_size=10, parallel_batches=2, max_pages=2000),
and 19.4 (adjacent overlap context with original 0-based page indices; context
overlap never satisfies core coverage).

This module does not execute external parsers, providers, databases, or queues,
and never fabricates extracted text or evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "BATCH_PLAN_RULE_VERSION",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_OVERLAP_PAGES",
    "DEFAULT_PARALLEL_BATCHES",
    "BatchLimits",
    "BatchPageEntry",
    "BatchPageReport",
    "BatchPlan",
    "BatchResultInput",
    "BatchSpec",
    "CoverageReport",
    "CoverageStatus",
    "MergedRetryResult",
    "OperationAttemptRecord",
    "PageCoverageItem",
    "PageRole",
    "PageStatusCode",
    "ReportedPageStatusCode",
    "RetryPlan",
    "RetryTargetPage",
    "derive_retry_plan",
    "evaluate_coverage",
    "execute_and_merge_retry",
    "execute_retry",
    "merge_retry_results",
    "plan_batches",
    "select_retry_batches",
]

BATCH_PLAN_RULE_VERSION = "pdf-batch-plan-1"
DEFAULT_MAX_PAGES = 2000
DEFAULT_BATCH_SIZE = 10
DEFAULT_OVERLAP_PAGES = 1
DEFAULT_PARALLEL_BATCHES = 2

PageRole = Literal["core", "overlap_before", "overlap_after"]
ReportedPageStatusCode = Literal["ok", "partial", "needs_review", "failed"]
PageStatusCode = Literal["ok", "partial", "needs_review", "failed", "missing"]
CoverageStatus = Literal["complete_raw", "needs_review", "incomplete"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_REPORTED_STATUSES: frozenset[str] = frozenset(
    {"ok", "partial", "needs_review", "failed"}
)
_VALID_ROLES: frozenset[str] = frozenset(
    {"core", "overlap_before", "overlap_after"}
)


def _require_strict_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid_{name}: must be an integer, got {value!r}")
    if value < minimum:
        raise ValueError(f"invalid_{name}: must be >= {minimum}, got {value}")
    return int(value)


def _require_non_empty_id(value: Any, name: str, *, max_len: int = 128) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_{name}: must be a string")
    if not value or len(value) > max_len or value.strip() != value:
        raise ValueError(
            f"invalid_{name}: must be 1..{max_len} non-leading/trailing-whitespace characters"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"invalid_{name}: control characters are not allowed")
    return value


def _require_sha256(value: Any, name: str = "document_sha256") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(
            f"invalid_{name}: must be 64 lowercase hexadecimal characters"
        )
    return value


def _require_diagnostics(value: Any, name: str = "diagnostics") -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"invalid_{name}: must be a list or tuple of strings")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item) > 256:
            raise ValueError(
                f"invalid_{name}[{idx}]: must be a non-empty string up to 256 characters"
            )
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
            raise ValueError(
                f"invalid_{name}[{idx}]: control characters are not allowed"
            )
        out.append(item)
    return tuple(out)


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BatchLimits:
    """Explicit positive finite limits for PDF page-batch planning (SPEC 16.6, 19.4)."""

    max_pages: int = DEFAULT_MAX_PAGES
    batch_size: int = DEFAULT_BATCH_SIZE
    overlap_pages: int = DEFAULT_OVERLAP_PAGES
    parallel_batches: int = DEFAULT_PARALLEL_BATCHES

    def __post_init__(self) -> None:
        max_pages = _require_strict_int(self.max_pages, "max_pages", minimum=1)
        if max_pages > DEFAULT_MAX_PAGES:
            raise ValueError(
                f"invalid_max_pages: {max_pages} exceeds ceiling {DEFAULT_MAX_PAGES}"
            )
        batch_size = _require_strict_int(self.batch_size, "batch_size", minimum=1)
        if batch_size > max_pages:
            raise ValueError(
                f"invalid_batch_size: {batch_size} exceeds max_pages {max_pages}"
            )
        overlap_pages = _require_strict_int(
            self.overlap_pages, "overlap_pages", minimum=0
        )
        if overlap_pages >= batch_size:
            raise ValueError(
                f"invalid_overlap_pages: {overlap_pages} must be < batch_size {batch_size}"
            )
        parallel_batches = _require_strict_int(
            self.parallel_batches, "parallel_batches", minimum=1
        )
        if parallel_batches > max_pages:
            raise ValueError(
                f"invalid_parallel_batches: {parallel_batches} exceeds max_pages {max_pages}"
            )


@dataclass(frozen=True)
class BatchPageEntry:
    """Mapping between batch-local position and original 0-based PDF page index."""

    batch_page_offset: int
    page_index: int
    role: PageRole
    is_core: bool

    def __post_init__(self) -> None:
        _require_strict_int(self.batch_page_offset, "batch_page_offset", minimum=0)
        _require_strict_int(self.page_index, "page_index", minimum=0)
        if not isinstance(self.role, str) or self.role not in _VALID_ROLES:
            raise ValueError(f"invalid_role: {self.role!r}")
        if not isinstance(self.is_core, bool):
            raise ValueError("invalid_is_core: must be a bool")
        if self.is_core != (self.role == "core"):
            raise ValueError("invalid_is_core: must match role == 'core'")


@dataclass(frozen=True)
class BatchSpec:
    """Single deterministic page batch with explicit core and overlap pages."""

    batch_id: str
    batch_index: int
    core_start: int
    core_end: int
    core_page_indices: tuple[int, ...]
    overlap_before_indices: tuple[int, ...]
    overlap_after_indices: tuple[int, ...]
    window_page_indices: tuple[int, ...]
    page_entries: tuple[BatchPageEntry, ...]
    is_full_document: bool
    batch_sha256: str


@dataclass(frozen=True)
class BatchPlan:
    """Deterministic partition of a document's physical pages into bounded batches."""

    rule_version: str
    document_id: str
    document_sha256: str
    page_count: int
    limits: BatchLimits
    batch_count: int
    batches: tuple[BatchSpec, ...]
    plan_sha256: str
    plan_id: str


@dataclass(frozen=True)
class BatchPageReport:
    """Reported outcome for a single physical page inside a batch execution."""

    page_index: int
    status: ReportedPageStatusCode
    diagnostics: tuple[str, ...] = ()
    role: PageRole | None = None
    operation_id: str | None = None
    attempt: int | None = None

    def __post_init__(self) -> None:
        _require_strict_int(self.page_index, "page_index", minimum=0)
        if not isinstance(self.status, str) or self.status not in _VALID_REPORTED_STATUSES:
            raise ValueError(
                f"invalid_page_status: must be one of {sorted(_VALID_REPORTED_STATUSES)}, got {self.status!r}"
            )
        diag = _require_diagnostics(self.diagnostics, "diagnostics")
        object.__setattr__(self, "diagnostics", diag)
        if self.status != "ok" and not diag:
            raise ValueError("non_ok_page_requires_diagnostics")
        if self.role is not None:
            if not isinstance(self.role, str) or self.role not in _VALID_ROLES:
                raise ValueError(f"invalid_role: {self.role!r}")
        if self.operation_id is not None:
            _require_non_empty_id(self.operation_id, "operation_id")
        if self.attempt is not None:
            _require_strict_int(self.attempt, "attempt", minimum=0)


@dataclass(frozen=True)
class BatchResultInput:
    """Result reported for one planned batch execution."""

    batch_id: str
    batch_index: int
    pages: tuple[BatchPageReport, ...]
    operation_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_id(self.batch_id, "batch_id")
        _require_strict_int(self.batch_index, "batch_index", minimum=0)
        if self.operation_id is not None:
            _require_non_empty_id(self.operation_id, "operation_id")
        if not isinstance(self.pages, (list, tuple)):
            raise ValueError("invalid_pages: must be a sequence of BatchPageReport")
        converted: list[BatchPageReport] = []
        seen_pages: set[int] = set()
        for idx, item in enumerate(self.pages):
            if not isinstance(item, BatchPageReport):
                raise ValueError(
                    f"invalid_pages[{idx}]: expected BatchPageReport, got {type(item).__name__}"
                )
            if item.page_index in seen_pages:
                raise ValueError(
                    f"duplicate_page_in_batch_result: page_index {item.page_index} in {self.batch_id}"
                )
            seen_pages.add(item.page_index)
            converted.append(item)
        object.__setattr__(self, "pages", tuple(converted))


@dataclass(frozen=True)
class PageCoverageItem:
    """Per-page core coverage accounting record (SPEC 4.2)."""

    page_index: int
    owner_batch_id: str
    owner_batch_index: int
    status: PageStatusCode
    diagnostics: tuple[str, ...]
    overlap_observations: tuple[tuple[str, ReportedPageStatusCode], ...]
    counts_as_covered: bool
    requires_retry: bool
    requires_review: bool


@dataclass(frozen=True)
class CoverageReport:
    """Full-document core coverage accounting over a BatchPlan.

    ``status`` is never complete when any core page is missing, failed, partial,
    or needs_review, or when any batch report is missing. Even when all core
    pages succeed, the status is ``complete_raw`` (raw page-batch coverage only,
    never a verified benchmark or gold completion claim).
    """

    rule_version: str
    plan_id: str
    plan_sha256: str
    document_id: str
    document_sha256: str
    page_count: int
    status: CoverageStatus
    can_claim_complete: bool
    can_freeze_benchmark: bool
    total_core_pages: int
    covered_core_pages: int
    ok_pages: tuple[int, ...]
    partial_pages: tuple[int, ...]
    needs_review_pages: tuple[int, ...]
    failed_pages: tuple[int, ...]
    missing_pages: tuple[int, ...]
    retry_page_indices: tuple[int, ...]
    retry_batch_ids: tuple[str, ...]
    completed_batch_ids: tuple[str, ...]
    missing_batch_ids: tuple[str, ...]
    coverage_ratio: float
    pages: tuple[PageCoverageItem, ...]
    report_sha256: str


def plan_batches(
    *,
    document_id: str,
    document_sha256: str,
    page_count: int,
    limits: BatchLimits = BatchLimits(),
) -> BatchPlan:
    """Create a deterministic, bounded page-batch plan for a PDF document.

    Every physical page in ``0 .. page_count - 1`` is assigned as a core page
    to exactly one batch in strictly increasing order. Each batch also includes
    up to ``limits.overlap_pages`` adjacent preceding and succeeding pages with
    their original 0-based physical ``page_index`` preserved.
    """
    doc_id = _require_non_empty_id(document_id, "document_id")
    doc_sha = _require_sha256(document_sha256, "document_sha256")
    if not isinstance(limits, BatchLimits):
        raise ValueError("invalid_limits: must be a BatchLimits instance")
    total_pages = _require_strict_int(page_count, "page_count", minimum=1)
    if total_pages > limits.max_pages:
        raise ValueError(
            f"page_limit_exceeded: page_count {total_pages} exceeds max_pages {limits.max_pages}"
        )

    batch_size = limits.batch_size
    overlap = limits.overlap_pages
    num_batches = (total_pages + batch_size - 1) // batch_size

    plan_header = {
        "rule_version": BATCH_PLAN_RULE_VERSION,
        "document_id": doc_id,
        "document_sha256": doc_sha,
        "page_count": total_pages,
        "limits": {
            "max_pages": limits.max_pages,
            "batch_size": limits.batch_size,
            "overlap_pages": limits.overlap_pages,
            "parallel_batches": limits.parallel_batches,
        },
    }
    plan_seed = _canonical_hash(plan_header)[:16]

    batches: list[BatchSpec] = []
    for batch_idx in range(num_batches):
        core_start = batch_idx * batch_size
        core_end = min(total_pages - 1, core_start + batch_size - 1)
        before_start = max(0, core_start - overlap)
        after_end = min(total_pages - 1, core_end + overlap)

        overlap_before = tuple(range(before_start, core_start))
        core_pages = tuple(range(core_start, core_end + 1))
        overlap_after = tuple(range(core_end + 1, after_end + 1))
        window_pages = overlap_before + core_pages + overlap_after

        entries: list[BatchPageEntry] = []
        for offset, p_idx in enumerate(window_pages):
            if p_idx < core_start:
                role: PageRole = "overlap_before"
            elif p_idx > core_end:
                role = "overlap_after"
            else:
                role = "core"
            entries.append(
                BatchPageEntry(
                    batch_page_offset=offset,
                    page_index=p_idx,
                    role=role,
                    is_core=(role == "core"),
                )
            )

        is_full_doc = len(core_pages) == total_pages
        batch_payload = {
            "plan_seed": plan_seed,
            "batch_index": batch_idx,
            "core_start": core_start,
            "core_end": core_end,
            "core_page_indices": list(core_pages),
            "overlap_before_indices": list(overlap_before),
            "overlap_after_indices": list(overlap_after),
            "window_page_indices": list(window_pages),
            "is_full_document": is_full_doc,
        }
        batch_sha = _canonical_hash(batch_payload)
        batch_id = f"batch_{plan_seed}_{batch_idx:04d}"

        batches.append(
            BatchSpec(
                batch_id=batch_id,
                batch_index=batch_idx,
                core_start=core_start,
                core_end=core_end,
                core_page_indices=core_pages,
                overlap_before_indices=overlap_before,
                overlap_after_indices=overlap_after,
                window_page_indices=window_pages,
                page_entries=tuple(entries),
                is_full_document=is_full_doc,
                batch_sha256=batch_sha,
            )
        )

    plan_payload = {
        **plan_header,
        "batch_count": num_batches,
        "batch_hashes": [b.batch_sha256 for b in batches],
    }
    plan_sha256 = _canonical_hash(plan_payload)
    plan_id = f"plan_{plan_sha256[:16]}"

    return BatchPlan(
        rule_version=BATCH_PLAN_RULE_VERSION,
        document_id=doc_id,
        document_sha256=doc_sha,
        page_count=total_pages,
        limits=limits,
        batch_count=num_batches,
        batches=tuple(batches),
        plan_sha256=plan_sha256,
        plan_id=plan_id,
    )


def _batch_result_signature(result: BatchResultInput) -> dict[str, Any]:
    return {
        "batch_id": result.batch_id,
        "batch_index": result.batch_index,
        "operation_id": result.operation_id,
        "pages": [
            {
                "page_index": p.page_index,
                "status": p.status,
                "diagnostics": list(p.diagnostics),
                "role": p.role,
                "operation_id": p.operation_id,
                "attempt": p.attempt,
            }
            for p in sorted(result.pages, key=lambda x: x.page_index)
        ],
    }


def _validate_plan_integrity(plan: BatchPlan) -> dict[str, BatchSpec]:
    if not isinstance(plan, BatchPlan):
        raise ValueError("invalid_plan: must be a BatchPlan instance")
    if plan.rule_version != BATCH_PLAN_RULE_VERSION:
        raise ValueError(f"unsupported_rule_version: {plan.rule_version!r}")
    if not isinstance(plan.limits, BatchLimits):
        raise ValueError("invalid_plan: limits must be a BatchLimits instance")
    expected = plan_batches(
        document_id=plan.document_id,
        document_sha256=plan.document_sha256,
        page_count=plan.page_count,
        limits=plan.limits,
    )
    if plan.plan_sha256 != expected.plan_sha256 or plan.plan_id != expected.plan_id:
        raise ValueError("tampered_or_inconsistent_plan: plan hash mismatch")
    if (
        isinstance(plan.batch_count, bool)
        or not isinstance(plan.batch_count, int)
        or plan.batch_count != expected.batch_count
    ):
        raise ValueError("tampered_or_inconsistent_plan: batch_count mismatch")
    if (
        not isinstance(plan.batches, tuple)
        or len(plan.batches) != expected.batch_count
        or plan.batches != expected.batches
        or plan != expected
    ):
        raise ValueError("tampered_or_inconsistent_plan: batches do not match canonical plan")
    return {b.batch_id: b for b in plan.batches}


def _validate_report_integrity(plan: BatchPlan, report: CoverageReport) -> None:
    if not isinstance(report, CoverageReport):
        raise ValueError("invalid_report: must be a CoverageReport instance")
    if (
        report.plan_sha256 != plan.plan_sha256
        or report.plan_id != plan.plan_id
        or report.document_id != plan.document_id
        or report.document_sha256 != plan.document_sha256
        or isinstance(report.page_count, bool)
        or report.page_count != plan.page_count
        or isinstance(report.total_core_pages, bool)
        or report.total_core_pages != plan.page_count
    ):
        raise ValueError("plan_report_mismatch: report does not belong to plan")
    if report.rule_version != BATCH_PLAN_RULE_VERSION:
        raise ValueError(
            f"tampered_or_inconsistent_report: unsupported rule_version {report.rule_version!r}"
        )
    if not isinstance(report.can_freeze_benchmark, bool) or report.can_freeze_benchmark:
        raise ValueError(
            "tampered_or_inconsistent_report: can_freeze_benchmark must be False"
        )
    if not isinstance(report.can_claim_complete, bool):
        raise ValueError(
            "tampered_or_inconsistent_report: can_claim_complete must be a bool"
        )
    if not isinstance(report.pages, tuple) or len(report.pages) != plan.page_count:
        raise ValueError(
            "tampered_or_inconsistent_report: pages must be a tuple of length page_count"
        )
    if not isinstance(report.missing_batch_ids, tuple):
        raise ValueError(
            "tampered_or_inconsistent_report: missing_batch_ids must be a tuple"
        )

    plan_batch_ids = [b.batch_id for b in plan.batches]
    plan_batch_set = set(plan_batch_ids)
    if any(
        not isinstance(b_id, str) or b_id not in plan_batch_set
        for b_id in report.missing_batch_ids
    ):
        raise ValueError(
            "tampered_or_inconsistent_report: unknown batch in missing_batch_ids"
        )
    if len(set(report.missing_batch_ids)) != len(report.missing_batch_ids):
        raise ValueError(
            "tampered_or_inconsistent_report: duplicate missing_batch_ids"
        )
    missing_batch_set = set(report.missing_batch_ids)
    expected_missing_batch_ids = tuple(
        b.batch_id for b in plan.batches if b.batch_id in missing_batch_set
    )
    if report.missing_batch_ids != expected_missing_batch_ids:
        raise ValueError(
            "tampered_or_inconsistent_report: missing_batch_ids out of canonical order"
        )

    owner_by_page: dict[int, BatchSpec] = {}
    neighbor_overlap_batches: dict[int, list[str]] = {
        p: [] for p in range(plan.page_count)
    }
    for spec in plan.batches:
        for p_idx in spec.core_page_indices:
            owner_by_page[p_idx] = spec
        for entry in spec.page_entries:
            if not entry.is_core:
                neighbor_overlap_batches[entry.page_index].append(spec.batch_id)

    ok_pages: list[int] = []
    partial_pages: list[int] = []
    needs_review_pages: list[int] = []
    failed_pages: list[int] = []
    missing_pages: list[int] = []
    retry_pages: list[int] = []
    covered_count = 0

    for expected_idx, item in enumerate(report.pages):
        if not isinstance(item, PageCoverageItem):
            raise ValueError(
                f"tampered_or_inconsistent_report: pages[{expected_idx}] is not PageCoverageItem"
            )
        if isinstance(item.page_index, bool) or item.page_index != expected_idx:
            raise ValueError(
                f"tampered_or_inconsistent_report: page_index mismatch at {expected_idx}"
            )
        owner = owner_by_page[expected_idx]
        if (
            item.owner_batch_id != owner.batch_id
            or isinstance(item.owner_batch_index, bool)
            or item.owner_batch_index != owner.batch_index
        ):
            raise ValueError(
                f"tampered_or_inconsistent_report: owner batch mismatch for page {expected_idx}"
            )
        if not isinstance(item.diagnostics, tuple):
            raise ValueError(
                f"tampered_or_inconsistent_report: diagnostics for page {expected_idx} must be a tuple"
            )
        _require_diagnostics(item.diagnostics, f"pages[{expected_idx}].diagnostics")

        if not isinstance(item.overlap_observations, tuple):
            raise ValueError(
                f"tampered_or_inconsistent_report: overlap_observations for page {expected_idx} must be a tuple"
            )
        allowed_neighbors = neighbor_overlap_batches[expected_idx]
        seen_obs_batches: list[str] = []
        for obs_entry in item.overlap_observations:
            if not isinstance(obs_entry, tuple) or len(obs_entry) != 2:
                raise ValueError(
                    f"tampered_or_inconsistent_report: invalid overlap observation on page {expected_idx}"
                )
            obs_batch_id, obs_status = obs_entry
            if (
                not isinstance(obs_batch_id, str)
                or obs_batch_id not in allowed_neighbors
                or obs_batch_id in missing_batch_set
            ):
                raise ValueError(
                    f"tampered_or_inconsistent_report: invalid overlap batch {obs_batch_id!r} on page {expected_idx}"
                )
            if (
                not isinstance(obs_status, str)
                or obs_status not in _VALID_REPORTED_STATUSES
            ):
                raise ValueError(
                    f"tampered_or_inconsistent_report: invalid overlap status {obs_status!r} on page {expected_idx}"
                )
            seen_obs_batches.append(obs_batch_id)
        if seen_obs_batches != [
            b_id for b_id in allowed_neighbors if b_id in set(seen_obs_batches)
        ] or len(seen_obs_batches) != len(set(seen_obs_batches)):
            raise ValueError(
                f"tampered_or_inconsistent_report: overlap_observations out of order or duplicated on page {expected_idx}"
            )

        status = item.status
        if owner.batch_id in missing_batch_set:
            if status != "missing" or item.diagnostics != ("batch_result_missing",):
                raise ValueError(
                    f"tampered_or_inconsistent_report: page {expected_idx} in missing batch must be missing with ('batch_result_missing',)"
                )
        else:
            if status == "missing":
                if item.diagnostics != ("core_page_omitted_from_batch_result",):
                    raise ValueError(
                        f"tampered_or_inconsistent_report: omitted page {expected_idx} must have ('core_page_omitted_from_batch_result',)"
                    )
            elif status in _VALID_REPORTED_STATUSES:
                if status != "ok" and not item.diagnostics:
                    raise ValueError(
                        f"tampered_or_inconsistent_report: non-ok page {expected_idx} requires diagnostics"
                    )
            else:
                raise ValueError(
                    f"tampered_or_inconsistent_report: invalid status {status!r} on page {expected_idx}"
                )

        if status == "ok":
            ok_pages.append(expected_idx)
            exp_covered, exp_retry, exp_review = True, False, False
        elif status == "partial":
            partial_pages.append(expected_idx)
            exp_covered, exp_retry, exp_review = True, False, True
        elif status == "needs_review":
            needs_review_pages.append(expected_idx)
            exp_covered, exp_retry, exp_review = False, False, True
        elif status == "failed":
            failed_pages.append(expected_idx)
            exp_covered, exp_retry, exp_review = False, True, False
        else:  # "missing"
            missing_pages.append(expected_idx)
            exp_covered, exp_retry, exp_review = False, True, False

        if (
            not isinstance(item.counts_as_covered, bool)
            or item.counts_as_covered != exp_covered
            or not isinstance(item.requires_retry, bool)
            or item.requires_retry != exp_retry
            or not isinstance(item.requires_review, bool)
            or item.requires_review != exp_review
        ):
            raise ValueError(
                f"tampered_or_inconsistent_report: page {expected_idx} flags disagree with status {status!r}"
            )

        if exp_covered:
            covered_count += 1
        if exp_retry:
            retry_pages.append(expected_idx)

    retry_page_set = set(retry_pages)
    expected_retry_batches: list[str] = []
    expected_completed_batches: list[str] = []
    for spec in plan.batches:
        if any(p in retry_page_set for p in spec.core_page_indices):
            expected_retry_batches.append(spec.batch_id)
        else:
            expected_completed_batches.append(spec.batch_id)

    if failed_pages or missing_pages:
        expected_status: CoverageStatus = "incomplete"
    elif partial_pages or needs_review_pages:
        expected_status = "needs_review"
    else:
        expected_status = "complete_raw"

    expected_can_claim_complete = (
        expected_status == "complete_raw"
        and len(ok_pages) == plan.page_count
        and not partial_pages
        and not needs_review_pages
        and not failed_pages
        and not missing_pages
    )
    expected_ratio = covered_count / float(plan.page_count)

    if (
        report.status != expected_status
        or report.can_claim_complete != expected_can_claim_complete
        or isinstance(report.covered_core_pages, bool)
        or report.covered_core_pages != covered_count
        or report.ok_pages != tuple(ok_pages)
        or report.partial_pages != tuple(partial_pages)
        or report.needs_review_pages != tuple(needs_review_pages)
        or report.failed_pages != tuple(failed_pages)
        or report.missing_pages != tuple(missing_pages)
        or report.retry_page_indices != tuple(retry_pages)
        or report.retry_batch_ids != tuple(expected_retry_batches)
        or report.completed_batch_ids != tuple(expected_completed_batches)
        or not isinstance(report.coverage_ratio, float)
        or not math.isfinite(report.coverage_ratio)
        or report.coverage_ratio != expected_ratio
    ):
        raise ValueError(
            "tampered_or_inconsistent_report: summary fields or retry lists disagree with per-page coverage"
        )

    expected_payload = {
        "rule_version": BATCH_PLAN_RULE_VERSION,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "document_id": plan.document_id,
        "document_sha256": plan.document_sha256,
        "page_count": plan.page_count,
        "status": expected_status,
        "can_claim_complete": expected_can_claim_complete,
        "can_freeze_benchmark": False,
        "total_core_pages": plan.page_count,
        "covered_core_pages": covered_count,
        "ok_pages": ok_pages,
        "partial_pages": partial_pages,
        "needs_review_pages": needs_review_pages,
        "failed_pages": failed_pages,
        "missing_pages": missing_pages,
        "retry_page_indices": retry_pages,
        "retry_batch_ids": expected_retry_batches,
        "completed_batch_ids": expected_completed_batches,
        "missing_batch_ids": list(report.missing_batch_ids),
        "pages": [
            {
                "page_index": item.page_index,
                "owner_batch_id": item.owner_batch_id,
                "owner_batch_index": item.owner_batch_index,
                "status": item.status,
                "diagnostics": list(item.diagnostics),
                "overlap_observations": [
                    [b_id, st] for b_id, st in item.overlap_observations
                ],
                "counts_as_covered": item.counts_as_covered,
                "requires_retry": item.requires_retry,
                "requires_review": item.requires_review,
            }
            for item in report.pages
        ],
    }
    expected_sha256 = _canonical_hash(expected_payload)
    if (
        not isinstance(report.report_sha256, str)
        or not _SHA256_RE.fullmatch(report.report_sha256)
        or report.report_sha256 != expected_sha256
    ):
        raise ValueError(
            "tampered_or_inconsistent_report: report_sha256 mismatch"
        )


def evaluate_coverage(
    plan: BatchPlan,
    results: Sequence[BatchResultInput],
) -> CoverageReport:
    """Evaluate per-page core coverage and failure/review accounting for a plan.

    Handling of duplicate, conflicting, out-of-plan, and overlap results:
    1. Out-of-plan batch IDs, mismatched ``batch_index``, or reported pages outside
       the batch's planned ``window_page_indices`` fail closed with ``ValueError``.
    2. If a ``BatchPageReport`` declares an explicit ``role``, it must match the
       page's planned role in that batch (``core``, ``overlap_before``, or
       ``overlap_after``) or ``ValueError`` is raised.
    3. Exact duplicate ``BatchResultInput`` submissions for the same ``batch_id``
       (identical page statuses, diagnostics, roles, and operation_id, regardless
       of page list order) are idempotently deduplicated. Conflicting duplicate
       submissions for the same ``batch_id`` raise ``ValueError``.
    4. Context overlap pages (``overlap_before`` / ``overlap_after``) are recorded
       only as ``overlap_observations`` on the target physical page and **never**
       satisfy core coverage for a missing, failed, partial, or needs_review core
       page in its owning batch (SPEC 4.2, 19.4).
    5. Only a core page reported by its owning batch with ``status == "ok"`` or
       ``status == "partial"`` counts as covered; ``partial`` and ``needs_review``
       prevent ``complete_raw`` and transition the report to ``needs_review``
       (unless any page is ``failed`` or ``missing``, which forces ``incomplete``).
       ``can_claim_complete`` is ``True`` only when every core page in ``0 .. P-1``
       is ``ok``. ``can_freeze_benchmark`` is always ``False`` at this stage.
    """
    batches_by_id = _validate_plan_integrity(plan)
    if not isinstance(results, (list, tuple)):
        raise ValueError("invalid_results: must be a list or tuple of BatchResultInput")

    accepted_by_batch_id: dict[str, BatchResultInput] = {}
    accepted_signatures: dict[str, dict[str, Any]] = {}

    for idx, res in enumerate(results):
        if not isinstance(res, BatchResultInput):
            raise ValueError(
                f"invalid_results[{idx}]: expected BatchResultInput, got {type(res).__name__}"
            )
        spec = batches_by_id.get(res.batch_id)
        if spec is None:
            raise ValueError(f"out_of_plan_batch_id: {res.batch_id!r}")
        if res.batch_index != spec.batch_index:
            raise ValueError(
                f"batch_index_mismatch: batch {res.batch_id} expected index {spec.batch_index}, got {res.batch_index}"
            )

        expected_roles = {
            entry.page_index: entry.role for entry in spec.page_entries
        }
        for page_rep in res.pages:
            if page_rep.page_index not in expected_roles:
                raise ValueError(
                    f"out_of_batch_window_page: page_index {page_rep.page_index} is not in {res.batch_id} window {spec.window_page_indices}"
                )
            planned_role = expected_roles[page_rep.page_index]
            if page_rep.role is not None and page_rep.role != planned_role:
                raise ValueError(
                    f"page_role_mismatch: page_index {page_rep.page_index} in {res.batch_id} has planned role {planned_role!r}, got {page_rep.role!r}"
                )

        sig = _batch_result_signature(res)
        if res.batch_id in accepted_signatures:
            if accepted_signatures[res.batch_id] != sig:
                raise ValueError(
                    f"conflicting_duplicate_batch_result: {res.batch_id} reported with different payload"
                )
            continue
        accepted_by_batch_id[res.batch_id] = res
        accepted_signatures[res.batch_id] = sig

    # Map each physical page to its unique core owner batch and collect core & overlap reports.
    owner_by_page: dict[int, BatchSpec] = {}
    core_reports: dict[int, BatchPageReport] = {}
    overlap_obs: dict[int, list[tuple[str, ReportedPageStatusCode]]] = {
        p: [] for p in range(plan.page_count)
    }

    for spec in plan.batches:
        for p_idx in spec.core_page_indices:
            owner_by_page[p_idx] = spec
        res = accepted_by_batch_id.get(spec.batch_id)
        if res is None:
            continue
        role_map = {e.page_index: e.role for e in spec.page_entries}
        for page_rep in sorted(res.pages, key=lambda x: x.page_index):
            role = role_map[page_rep.page_index]
            if role == "core":
                core_reports[page_rep.page_index] = page_rep
            else:
                overlap_obs[page_rep.page_index].append(
                    (spec.batch_id, page_rep.status)
                )

    page_items: list[PageCoverageItem] = []
    ok_pages: list[int] = []
    partial_pages: list[int] = []
    needs_review_pages: list[int] = []
    failed_pages: list[int] = []
    missing_pages: list[int] = []
    retry_pages: list[int] = []
    covered_count = 0

    for p_idx in range(plan.page_count):
        owner = owner_by_page[p_idx]
        core_rep = core_reports.get(p_idx)
        obs = tuple(overlap_obs[p_idx])

        if core_rep is None:
            status: PageStatusCode = "missing"
            if owner.batch_id in accepted_by_batch_id:
                diagnostics: tuple[str, ...] = ("core_page_omitted_from_batch_result",)
            else:
                diagnostics = ("batch_result_missing",)
        else:
            status = core_rep.status
            diagnostics = core_rep.diagnostics

        if status == "ok":
            ok_pages.append(p_idx)
            counts_as_covered = True
            requires_retry = False
            requires_review = False
        elif status == "partial":
            partial_pages.append(p_idx)
            counts_as_covered = True
            requires_retry = False
            requires_review = True
        elif status == "needs_review":
            needs_review_pages.append(p_idx)
            counts_as_covered = False
            requires_retry = False
            requires_review = True
        elif status == "failed":
            failed_pages.append(p_idx)
            counts_as_covered = False
            requires_retry = True
            requires_review = False
        else:  # "missing"
            missing_pages.append(p_idx)
            counts_as_covered = False
            requires_retry = True
            requires_review = False

        if counts_as_covered:
            covered_count += 1
        if requires_retry:
            retry_pages.append(p_idx)

        page_items.append(
            PageCoverageItem(
                page_index=p_idx,
                owner_batch_id=owner.batch_id,
                owner_batch_index=owner.batch_index,
                status=status,
                diagnostics=diagnostics,
                overlap_observations=obs,
                counts_as_covered=counts_as_covered,
                requires_retry=requires_retry,
                requires_review=requires_review,
            )
        )

    retry_batch_ids: list[str] = []
    completed_batch_ids: list[str] = []
    missing_batch_ids: list[str] = []

    retry_page_set = set(retry_pages)
    for spec in plan.batches:
        if spec.batch_id not in accepted_by_batch_id:
            missing_batch_ids.append(spec.batch_id)
        if any(p in retry_page_set for p in spec.core_page_indices):
            retry_batch_ids.append(spec.batch_id)
        else:
            completed_batch_ids.append(spec.batch_id)

    if failed_pages or missing_pages:
        overall_status: CoverageStatus = "incomplete"
    elif partial_pages or needs_review_pages:
        overall_status = "needs_review"
    else:
        overall_status = "complete_raw"

    can_claim_complete = (
        overall_status == "complete_raw"
        and len(ok_pages) == plan.page_count
        and not partial_pages
        and not needs_review_pages
        and not failed_pages
        and not missing_pages
    )

    coverage_ratio = covered_count / float(plan.page_count)
    if not math.isfinite(coverage_ratio):
        raise ValueError("non_finite_coverage_ratio")

    report_payload = {
        "rule_version": BATCH_PLAN_RULE_VERSION,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "document_id": plan.document_id,
        "document_sha256": plan.document_sha256,
        "page_count": plan.page_count,
        "status": overall_status,
        "can_claim_complete": can_claim_complete,
        "can_freeze_benchmark": False,
        "total_core_pages": plan.page_count,
        "covered_core_pages": covered_count,
        "ok_pages": ok_pages,
        "partial_pages": partial_pages,
        "needs_review_pages": needs_review_pages,
        "failed_pages": failed_pages,
        "missing_pages": missing_pages,
        "retry_page_indices": retry_pages,
        "retry_batch_ids": retry_batch_ids,
        "completed_batch_ids": completed_batch_ids,
        "missing_batch_ids": missing_batch_ids,
        "pages": [
            {
                "page_index": item.page_index,
                "owner_batch_id": item.owner_batch_id,
                "owner_batch_index": item.owner_batch_index,
                "status": item.status,
                "diagnostics": list(item.diagnostics),
                "overlap_observations": [
                    [b_id, st] for b_id, st in item.overlap_observations
                ],
                "counts_as_covered": item.counts_as_covered,
                "requires_retry": item.requires_retry,
                "requires_review": item.requires_review,
            }
            for item in page_items
        ],
    }
    report_sha256 = _canonical_hash(report_payload)

    return CoverageReport(
        rule_version=BATCH_PLAN_RULE_VERSION,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        document_id=plan.document_id,
        document_sha256=plan.document_sha256,
        page_count=plan.page_count,
        status=overall_status,
        can_claim_complete=can_claim_complete,
        can_freeze_benchmark=False,
        total_core_pages=plan.page_count,
        covered_core_pages=covered_count,
        ok_pages=tuple(ok_pages),
        partial_pages=tuple(partial_pages),
        needs_review_pages=tuple(needs_review_pages),
        failed_pages=tuple(failed_pages),
        missing_pages=tuple(missing_pages),
        retry_page_indices=tuple(retry_pages),
        retry_batch_ids=tuple(retry_batch_ids),
        completed_batch_ids=tuple(completed_batch_ids),
        missing_batch_ids=tuple(missing_batch_ids),
        coverage_ratio=coverage_ratio,
        pages=tuple(page_items),
        report_sha256=report_sha256,
    )


def select_retry_batches(
    plan: BatchPlan,
    report: CoverageReport,
) -> tuple[BatchSpec, ...]:
    """Return the ordered subset of planned batches that still require execution or retry.

    Batches whose core pages have all reached ``ok``, ``partial``, or ``needs_review``
    are reused and not re-scheduled (SPEC 4.2, T07).
    """
    _validate_plan_integrity(plan)
    _validate_report_integrity(plan, report)

    retry_set = set(report.retry_batch_ids)
    return tuple(b for b in plan.batches if b.batch_id in retry_set)


# Re-export retry API for convenience
from lab_parser.batch_retry import (  # noqa: E402
    MergedRetryResult,
    OperationAttemptRecord,
    RetryPlan,
    RetryTargetPage,
    derive_retry_plan,
    execute_and_merge_retry,
    execute_retry,
    merge_retry_results,
)
