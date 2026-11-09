"""Unit and negative tests for PDF page-batch planning and coverage accounting.

Covers deterministic subcases of SPEC 2, 4.2, 16.6, 19.4 (R01, R02, R07, R28;
T03, T07, T14, T94, T97):
- Validated positive finite limits and 2000-page ceiling.
- Core pages partitioned exactly once in strictly increasing order.
- Bounded adjacent overlap preserving original 0-based physical page_index.
- Deterministic plan/batch/report identities and ordering.
- 500, 600, and 2000 page batch plans within explicit bounds.
- Per-page coverage accounting that never claims complete when any core page is
  missing, failed, partial, or needs_review.
- Prevention of context overlap from masquerading as core coverage.
- Explicit handling of exact duplicate vs conflicting duplicate vs out-of-plan
  batch/page results.
- Retry batch selection reusing succeeded/reviewed batches without re-running them.
"""

from __future__ import annotations

import dataclasses
import unittest

from lab_parser.batch_coverage import (
    BATCH_PLAN_RULE_VERSION,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_PAGES,
    DEFAULT_OVERLAP_PAGES,
    DEFAULT_PARALLEL_BATCHES,
    BatchLimits,
    BatchPageEntry,
    BatchPageReport,
    BatchPlan,
    BatchResultInput,
    CoverageReport,
    evaluate_coverage,
    plan_batches,
    select_retry_batches,
)

DOC_ID = "doc_001"
DOC_SHA = "a" * 64


def _make_ok_result_for_batch(batch) -> BatchResultInput:
    return BatchResultInput(
        batch_id=batch.batch_id,
        batch_index=batch.batch_index,
        pages=tuple(
            BatchPageReport(
                page_index=entry.page_index,
                status="ok",
                diagnostics=(),
                role=entry.role,
            )
            for entry in batch.page_entries
        ),
        operation_id=f"op_{batch.batch_index:04d}",
    )


class TestBatchLimitsValidation(unittest.TestCase):
    def test_defaults_match_spec_16_6_and_19_4(self) -> None:
        limits = BatchLimits()
        self.assertEqual(limits.max_pages, DEFAULT_MAX_PAGES)
        self.assertEqual(limits.max_pages, 2000)
        self.assertEqual(limits.batch_size, DEFAULT_BATCH_SIZE)
        self.assertEqual(limits.batch_size, 10)
        self.assertEqual(limits.overlap_pages, DEFAULT_OVERLAP_PAGES)
        self.assertEqual(limits.overlap_pages, 1)
        self.assertEqual(limits.parallel_batches, DEFAULT_PARALLEL_BATCHES)
        self.assertEqual(limits.parallel_batches, 2)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            limits.batch_size = 5  # type: ignore[misc]

    def test_rejects_non_int_boolean_zero_negative_and_out_of_bound_limits(self) -> None:
        invalid_cases = [
            {"max_pages": 0},
            {"max_pages": -1},
            {"max_pages": 2001},
            {"max_pages": True},
            {"max_pages": 100.0},
            {"max_pages": "100"},
            {"batch_size": 0},
            {"batch_size": -5},
            {"batch_size": False},
            {"batch_size": 10.5},
            {"batch_size": 21, "max_pages": 20},
            {"overlap_pages": -1},
            {"overlap_pages": True},
            {"overlap_pages": 1.0},
            {"overlap_pages": 10, "batch_size": 10},
            {"overlap_pages": 11, "batch_size": 10},
            {"parallel_batches": 0},
            {"parallel_batches": -1},
            {"parallel_batches": True},
            {"parallel_batches": 2.0},
            {"parallel_batches": 25, "max_pages": 20},
        ]
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    BatchLimits(**kwargs)  # type: ignore[arg-type]

    def test_accepts_zero_overlap_and_custom_valid_bounds(self) -> None:
        limits = BatchLimits(
            max_pages=600,
            batch_size=25,
            overlap_pages=0,
            parallel_batches=4,
        )
        self.assertEqual(limits.overlap_pages, 0)
        self.assertEqual(limits.batch_size, 25)


class TestPlanBatchesPartitionAndOverlap(unittest.TestCase):
    def test_single_page_document_is_full_document_without_overlap(self) -> None:
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=1,
        )
        self.assertEqual(plan.rule_version, BATCH_PLAN_RULE_VERSION)
        self.assertEqual(plan.batch_count, 1)
        b0 = plan.batches[0]
        self.assertEqual(b0.batch_index, 0)
        self.assertEqual(b0.core_start, 0)
        self.assertEqual(b0.core_end, 0)
        self.assertEqual(b0.core_page_indices, (0,))
        self.assertEqual(b0.overlap_before_indices, ())
        self.assertEqual(b0.overlap_after_indices, ())
        self.assertEqual(b0.window_page_indices, (0,))
        self.assertTrue(b0.is_full_document)
        self.assertEqual(
            b0.page_entries,
            (
                BatchPageEntry(
                    batch_page_offset=0,
                    page_index=0,
                    role="core",
                    is_core=True,
                ),
            ),
        )

    def test_boundary_10_11_25_pages_core_partition_and_adjacent_overlap(self) -> None:
        # 25 pages with default batch_size=10, overlap=1 -> 3 batches:
        # batch 0: core 0..9, overlap_before (), overlap_after (10,)
        # batch 1: core 10..19, overlap_before (9,), overlap_after (20,)
        # batch 2: core 20..24, overlap_before (19,), overlap_after ()
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )
        self.assertEqual(plan.batch_count, 3)

        b0, b1, b2 = plan.batches
        self.assertEqual(b0.core_page_indices, tuple(range(0, 10)))
        self.assertEqual(b0.overlap_before_indices, ())
        self.assertEqual(b0.overlap_after_indices, (10,))
        self.assertEqual(b0.window_page_indices, tuple(range(0, 11)))
        self.assertFalse(b0.is_full_document)

        self.assertEqual(b1.core_page_indices, tuple(range(10, 20)))
        self.assertEqual(b1.overlap_before_indices, (9,))
        self.assertEqual(b1.overlap_after_indices, (20,))
        self.assertEqual(b1.window_page_indices, tuple(range(9, 21)))
        self.assertFalse(b1.is_full_document)

        # Verify original 0-based page_index is preserved alongside batch_page_offset
        self.assertEqual(b1.page_entries[0].batch_page_offset, 0)
        self.assertEqual(b1.page_entries[0].page_index, 9)
        self.assertEqual(b1.page_entries[0].role, "overlap_before")
        self.assertFalse(b1.page_entries[0].is_core)

        self.assertEqual(b1.page_entries[1].batch_page_offset, 1)
        self.assertEqual(b1.page_entries[1].page_index, 10)
        self.assertEqual(b1.page_entries[1].role, "core")
        self.assertTrue(b1.page_entries[1].is_core)

        self.assertEqual(b1.page_entries[-1].batch_page_offset, 11)
        self.assertEqual(b1.page_entries[-1].page_index, 20)
        self.assertEqual(b1.page_entries[-1].role, "overlap_after")
        self.assertFalse(b1.page_entries[-1].is_core)

        self.assertEqual(b2.core_page_indices, tuple(range(20, 25)))
        self.assertEqual(b2.overlap_before_indices, (19,))
        self.assertEqual(b2.overlap_after_indices, ())
        self.assertEqual(b2.window_page_indices, tuple(range(19, 25)))
        self.assertFalse(b2.is_full_document)

    def test_large_documents_500_600_2000_pages_exact_core_partition(self) -> None:
        for page_count, expected_batches in ((19, 2), (20, 2), (21, 3), (500, 50), (600, 60), (2000, 200)):
            with self.subTest(page_count=page_count):
                plan = plan_batches(
                    document_id=DOC_ID,
                    document_sha256=DOC_SHA,
                    page_count=page_count,
                )
                self.assertEqual(plan.batch_count, expected_batches)
                self.assertEqual(len(plan.batches), expected_batches)

                # Core pages must partition 0 .. page_count - 1 exactly once in order.
                all_core: list[int] = []
                seen_batch_ids: set[str] = set()
                for idx, batch in enumerate(plan.batches):
                    self.assertEqual(batch.batch_index, idx)
                    self.assertNotIn(batch.batch_id, seen_batch_ids)
                    seen_batch_ids.add(batch.batch_id)
                    self.assertFalse(batch.is_full_document)
                    all_core.extend(batch.core_page_indices)
                    self.assertLessEqual(
                        len(batch.window_page_indices),
                        plan.limits.batch_size + 2 * plan.limits.overlap_pages,
                    )
                self.assertEqual(all_core, list(range(page_count)))

    def test_deterministic_identities_and_sensitivity_to_inputs(self) -> None:
        plan_a = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=500,
        )
        plan_b = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=500,
        )
        self.assertEqual(plan_a, plan_b)
        self.assertEqual(plan_a.plan_id, plan_b.plan_id)
        self.assertEqual(plan_a.plan_sha256, plan_b.plan_sha256)

        plan_diff_sha = plan_batches(
            document_id=DOC_ID,
            document_sha256="b" * 64,
            page_count=500,
        )
        self.assertNotEqual(plan_a.plan_sha256, plan_diff_sha.plan_sha256)
        self.assertNotEqual(plan_a.plan_id, plan_diff_sha.plan_id)
        self.assertNotEqual(
            plan_a.batches[0].batch_id, plan_diff_sha.batches[0].batch_id
        )

        plan_diff_overlap = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=500,
            limits=BatchLimits(overlap_pages=2),
        )
        self.assertNotEqual(plan_a.plan_sha256, plan_diff_overlap.plan_sha256)

    def test_plan_batches_rejects_invalid_inputs_and_over_limit_pages(self) -> None:
        for bad_pages in (0, -1, 2001, True, 10.0, "500"):
            with self.subTest(page_count=bad_pages):
                with self.assertRaises(ValueError):
                    plan_batches(
                        document_id=DOC_ID,
                        document_sha256=DOC_SHA,
                        page_count=bad_pages,  # type: ignore[arg-type]
                    )

        for bad_doc_id in ("", " doc", "doc ", "a" * 129, "doc\n1", None, 123):
            with self.subTest(document_id=bad_doc_id):
                with self.assertRaises(ValueError):
                    plan_batches(
                        document_id=bad_doc_id,  # type: ignore[arg-type]
                        document_sha256=DOC_SHA,
                        page_count=10,
                    )

        for bad_sha in ("", "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n", None):
            with self.subTest(document_sha256=bad_sha):
                with self.assertRaises(ValueError):
                    plan_batches(
                        document_id=DOC_ID,
                        document_sha256=bad_sha,  # type: ignore[arg-type]
                        page_count=10,
                    )


class TestCoverageAccountingAndInvariants(unittest.TestCase):
    def test_complete_500_page_coverage_and_order_invariance(self) -> None:
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=500,
        )
        results_forward = [_make_ok_result_for_batch(b) for b in plan.batches]
        results_reverse = list(reversed(results_forward))

        rep_fwd = evaluate_coverage(plan, results_forward)
        rep_rev = evaluate_coverage(plan, results_reverse)

        self.assertEqual(rep_fwd, rep_rev)
        self.assertEqual(rep_fwd.report_sha256, rep_rev.report_sha256)
        self.assertEqual(rep_fwd.status, "complete_raw")
        self.assertTrue(rep_fwd.can_claim_complete)
        self.assertFalse(rep_fwd.can_freeze_benchmark)
        self.assertEqual(rep_fwd.total_core_pages, 500)
        self.assertEqual(rep_fwd.covered_core_pages, 500)
        self.assertEqual(rep_fwd.coverage_ratio, 1.0)
        self.assertEqual(rep_fwd.ok_pages, tuple(range(500)))
        self.assertEqual(rep_fwd.partial_pages, ())
        self.assertEqual(rep_fwd.needs_review_pages, ())
        self.assertEqual(rep_fwd.failed_pages, ())
        self.assertEqual(rep_fwd.missing_pages, ())
        self.assertEqual(rep_fwd.retry_page_indices, ())
        self.assertEqual(rep_fwd.retry_batch_ids, ())
        self.assertEqual(rep_fwd.missing_batch_ids, ())
        self.assertEqual(len(rep_fwd.completed_batch_ids), 50)
        self.assertEqual(select_retry_batches(plan, rep_fwd), ())

        # Boundary page 9 (core in batch 0, overlap_before in batch 1) and
        # boundary page 10 (overlap_after in batch 0, core in batch 1) record overlap observations.
        self.assertEqual(
            rep_fwd.pages[9].overlap_observations,
            ((plan.batches[1].batch_id, "ok"),),
        )
        self.assertEqual(
            rep_fwd.pages[10].overlap_observations,
            ((plan.batches[0].batch_id, "ok"),),
        )

    def test_overlap_page_never_masquerades_as_core_coverage(self) -> None:
        """SPEC 4.2 & 19.4: If page 10 is ok as overlap_after in batch 0, but
        missing or failed or needs_review in its owning batch 1, page 10 must NOT
        be counted as ok core coverage."""
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=20,
        )
        b0, b1 = plan.batches
        res_b0 = _make_ok_result_for_batch(b0)  # includes page 10 as overlap_after="ok"

        # Case 1: Batch 1 is completely missing. Page 10 must be "missing", not "ok".
        rep_missing_b1 = evaluate_coverage(plan, [res_b0])
        self.assertEqual(rep_missing_b1.status, "incomplete")
        self.assertFalse(rep_missing_b1.can_claim_complete)
        self.assertEqual(rep_missing_b1.covered_core_pages, 10)
        self.assertEqual(rep_missing_b1.missing_pages, tuple(range(10, 20)))
        p10 = rep_missing_b1.pages[10]
        self.assertEqual(p10.status, "missing")
        self.assertFalse(p10.counts_as_covered)
        self.assertTrue(p10.requires_retry)
        self.assertEqual(p10.overlap_observations, ((b0.batch_id, "ok"),))

        # Case 2: Batch 1 reports page 10 as "failed", while batch 0 reported page 10 as "ok" overlap.
        b1_pages_failed_10 = [
            BatchPageReport(
                page_index=entry.page_index,
                status="failed" if entry.page_index == 10 else "ok",
                diagnostics=("ocr_timeout",) if entry.page_index == 10 else (),
                role=entry.role,
            )
            for entry in b1.page_entries
        ]
        res_b1_failed = BatchResultInput(
            batch_id=b1.batch_id,
            batch_index=b1.batch_index,
            pages=tuple(b1_pages_failed_10),
        )
        rep_failed_10 = evaluate_coverage(plan, [res_b0, res_b1_failed])
        self.assertEqual(rep_failed_10.status, "incomplete")
        self.assertFalse(rep_failed_10.can_claim_complete)
        self.assertEqual(rep_failed_10.covered_core_pages, 19)
        self.assertEqual(rep_failed_10.failed_pages, (10,))
        self.assertEqual(rep_failed_10.retry_page_indices, (10,))
        self.assertEqual(rep_failed_10.retry_batch_ids, (b1.batch_id,))
        self.assertEqual(select_retry_batches(plan, rep_failed_10), (b1,))

        # Case 3: Batch 1 omits core page 10 from its reported pages list.
        b1_pages_omitted_10 = [
            BatchPageReport(
                page_index=entry.page_index,
                status="ok",
                diagnostics=(),
                role=entry.role,
            )
            for entry in b1.page_entries
            if entry.page_index != 10
        ]
        res_b1_omitted = BatchResultInput(
            batch_id=b1.batch_id,
            batch_index=b1.batch_index,
            pages=tuple(b1_pages_omitted_10),
        )
        rep_omitted_10 = evaluate_coverage(plan, [res_b0, res_b1_omitted])
        self.assertEqual(rep_omitted_10.status, "incomplete")
        self.assertFalse(rep_omitted_10.can_claim_complete)
        self.assertEqual(rep_omitted_10.missing_pages, (10,))
        self.assertEqual(
            rep_omitted_10.pages[10].diagnostics,
            ("core_page_omitted_from_batch_result",),
        )

    def test_needs_review_and_partial_prevent_complete_without_forcing_retry(self) -> None:
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=20,
        )
        b0, b1 = plan.batches
        res_b0 = _make_ok_result_for_batch(b0)

        # Page 12 is "needs_review" (does not count as covered; requires review, not retry).
        # Page 15 is "partial" (counts as covered; requires review, prevents complete_raw).
        b1_pages = []
        for entry in b1.page_entries:
            if entry.page_index == 12:
                b1_pages.append(
                    BatchPageReport(
                        page_index=12,
                        status="needs_review",
                        diagnostics=("low_ocr_confidence",),
                        role=entry.role,
                    )
                )
            elif entry.page_index == 15:
                b1_pages.append(
                    BatchPageReport(
                        page_index=15,
                        status="partial",
                        diagnostics=("figure_crop_fallback",),
                        role=entry.role,
                    )
                )
            else:
                b1_pages.append(
                    BatchPageReport(
                        page_index=entry.page_index,
                        status="ok",
                        diagnostics=(),
                        role=entry.role,
                    )
                )
        res_b1 = BatchResultInput(
            batch_id=b1.batch_id,
            batch_index=b1.batch_index,
            pages=tuple(b1_pages),
        )

        report = evaluate_coverage(plan, [res_b0, res_b1])
        self.assertEqual(report.status, "needs_review")
        self.assertFalse(report.can_claim_complete)
        self.assertFalse(report.can_freeze_benchmark)
        self.assertEqual(report.covered_core_pages, 19)
        self.assertEqual(report.needs_review_pages, (12,))
        self.assertEqual(report.partial_pages, (15,))
        self.assertEqual(report.failed_pages, ())
        self.assertEqual(report.missing_pages, ())
        self.assertEqual(report.retry_page_indices, ())
        self.assertEqual(report.retry_batch_ids, ())
        self.assertEqual(select_retry_batches(plan, report), ())
        self.assertTrue(report.pages[12].requires_review)
        self.assertFalse(report.pages[12].counts_as_covered)
        self.assertTrue(report.pages[15].requires_review)
        self.assertTrue(report.pages[15].counts_as_covered)

    def test_failed_overlap_does_not_invalidate_ok_core_in_owner_batch(self) -> None:
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=20,
        )
        b0, b1 = plan.batches
        res_b0 = _make_ok_result_for_batch(b0)
        # In batch 1, overlap_before page 9 fails, but all core pages 10..19 are ok.
        b1_pages = [
            BatchPageReport(
                page_index=entry.page_index,
                status="failed" if entry.page_index == 9 else "ok",
                diagnostics=("overlap_timeout",) if entry.page_index == 9 else (),
                role=entry.role,
            )
            for entry in b1.page_entries
        ]
        res_b1 = BatchResultInput(
            batch_id=b1.batch_id,
            batch_index=b1.batch_index,
            pages=tuple(b1_pages),
        )
        report = evaluate_coverage(plan, [res_b0, res_b1])
        self.assertEqual(report.status, "complete_raw")
        self.assertTrue(report.can_claim_complete)
        self.assertEqual(report.pages[9].status, "ok")
        self.assertEqual(
            report.pages[9].overlap_observations,
            ((b1.batch_id, "failed"),),
        )

    def test_exact_duplicate_batch_result_is_idempotent(self) -> None:
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=15,
        )
        b0, b1 = plan.batches
        res_b0 = _make_ok_result_for_batch(b0)
        res_b0_reordered = BatchResultInput(
            batch_id=b0.batch_id,
            batch_index=b0.batch_index,
            pages=tuple(reversed(res_b0.pages)),
            operation_id=res_b0.operation_id,
        )
        res_b1 = _make_ok_result_for_batch(b1)

        rep_single = evaluate_coverage(plan, [res_b0, res_b1])
        rep_dup = evaluate_coverage(plan, [res_b0, res_b1, res_b0_reordered])
        self.assertEqual(rep_single, rep_dup)

    def test_rejects_conflicting_duplicate_out_of_plan_and_role_spoofing(self) -> None:
        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=20,
        )
        b0, b1 = plan.batches
        res_b0 = _make_ok_result_for_batch(b0)

        # 1. Conflicting duplicate for same batch_id (different status on page 0)
        b0_conflict_pages = list(res_b0.pages)
        b0_conflict_pages[0] = BatchPageReport(
            page_index=0,
            status="failed",
            diagnostics=("corrupt_stream",),
            role="core",
        )
        res_b0_conflict = BatchResultInput(
            batch_id=b0.batch_id,
            batch_index=b0.batch_index,
            pages=tuple(b0_conflict_pages),
            operation_id=res_b0.operation_id,
        )
        with self.assertRaisesRegex(ValueError, "conflicting_duplicate_batch_result"):
            evaluate_coverage(plan, [res_b0, res_b0_conflict])

        # 2. Out-of-plan batch_id
        res_unknown_batch = BatchResultInput(
            batch_id="batch_unknown_0000",
            batch_index=0,
            pages=(BatchPageReport(page_index=0, status="ok"),),
        )
        with self.assertRaisesRegex(ValueError, "out_of_plan_batch_id"):
            evaluate_coverage(plan, [res_unknown_batch])

        # 3. Mismatched batch_index for valid batch_id
        res_wrong_idx = BatchResultInput(
            batch_id=b0.batch_id,
            batch_index=1,
            pages=res_b0.pages,
        )
        with self.assertRaisesRegex(ValueError, "batch_index_mismatch"):
            evaluate_coverage(plan, [res_wrong_idx])

        # 4. Out-of-window page reported in batch 0 (page 15 is in batch 1, not batch 0 window 0..10)
        res_out_of_window = BatchResultInput(
            batch_id=b0.batch_id,
            batch_index=0,
            pages=res_b0.pages + (BatchPageReport(page_index=15, status="ok"),),
        )
        with self.assertRaisesRegex(ValueError, "out_of_batch_window_page"):
            evaluate_coverage(plan, [res_out_of_window])

        # 5. Overlap page spoofing role="core" (page 10 in batch 0 is overlap_after, not core)
        spoofed_pages = [
            BatchPageReport(
                page_index=entry.page_index,
                status="ok",
                diagnostics=(),
                role="core",  # wrong for page 10 in batch 0
            )
            for entry in b0.page_entries
        ]
        res_spoofed_role = BatchResultInput(
            batch_id=b0.batch_id,
            batch_index=0,
            pages=tuple(spoofed_pages),
        )
        with self.assertRaisesRegex(ValueError, "page_role_mismatch"):
            evaluate_coverage(plan, [res_spoofed_role])

        # 6. Duplicate page within a single BatchResultInput
        with self.assertRaisesRegex(ValueError, "duplicate_page_in_batch_result"):
            BatchResultInput(
                batch_id=b0.batch_id,
                batch_index=0,
                pages=(
                    BatchPageReport(page_index=0, status="ok"),
                    BatchPageReport(page_index=0, status="ok"),
                ),
            )

        # 7. Non-ok page without diagnostics
        for st in ("partial", "needs_review", "failed"):
            with self.subTest(status=st):
                with self.assertRaisesRegex(
                    ValueError, "non_ok_page_requires_diagnostics"
                ):
                    BatchPageReport(page_index=0, status=st, diagnostics=())  # type: ignore[arg-type]

        # 8. Tampered plan rejected by evaluate_coverage and select_retry_batches
        tampered_plan = dataclasses.replace(plan, plan_sha256="f" * 64)
        with self.assertRaisesRegex(ValueError, "tampered_or_inconsistent_plan"):
            evaluate_coverage(tampered_plan, [res_b0])
        rep_partial = evaluate_coverage(plan, [res_b0])
        with self.assertRaisesRegex(ValueError, "plan_report_mismatch"):
            other_plan = plan_batches(
                document_id=DOC_ID,
                document_sha256="c" * 64,
                page_count=20,
            )
            select_retry_batches(other_plan, rep_partial)

    def test_host_review_repro_tampered_batch_count_and_report_integrity(self) -> None:
        """Host review on PR #61 reproduced two defects:
        (1) select_retry_batches only checked plan identity, not report integrity:
            plan 2 pages batch_size=1, r = evaluate_coverage(plan, []),
            select_retry_batches(plan, dataclasses.replace(r, retry_batch_ids=()))
            returned () despite missing pages.
        (2) _validate_plan_integrity ignored plan.batch_count and accepted
            dataclasses.replace(plan, batch_count=0).
        Both must fail closed, even if hashes are recomputed from inconsistent fields."""
        from lab_parser.batch_coverage import _canonical_hash

        plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=2,
            limits=BatchLimits(batch_size=1, overlap_pages=0),
        )
        rep = evaluate_coverage(plan, [])

        # Defect (2) reproduction: tampered batch_count on plan
        for bad_count in (0, 1, 3, -1, True):
            with self.subTest(bad_batch_count=bad_count):
                bad_plan = dataclasses.replace(plan, batch_count=bad_count)  # type: ignore[arg-type]
                with self.assertRaisesRegex(
                    ValueError, "tampered_or_inconsistent_plan"
                ):
                    evaluate_coverage(bad_plan, [])
                with self.assertRaisesRegex(
                    ValueError, "tampered_or_inconsistent_plan"
                ):
                    select_retry_batches(bad_plan, rep)

        # Defect (1) exact reproduction: retry_batch_ids replaced with ()
        tampered_retry_empty = dataclasses.replace(rep, retry_batch_ids=())
        with self.assertRaisesRegex(ValueError, "tampered_or_inconsistent_report"):
            select_retry_batches(plan, tampered_retry_empty)

        # Even if an attacker recomputes report_sha256 over inconsistent retry_batch_ids=(),
        # full structural consistency checks must still reject it.
        forged_payload = {
            "rule_version": rep.rule_version,
            "plan_id": rep.plan_id,
            "plan_sha256": rep.plan_sha256,
            "document_id": rep.document_id,
            "document_sha256": rep.document_sha256,
            "page_count": rep.page_count,
            "status": rep.status,
            "can_claim_complete": rep.can_claim_complete,
            "can_freeze_benchmark": rep.can_freeze_benchmark,
            "total_core_pages": rep.total_core_pages,
            "covered_core_pages": rep.covered_core_pages,
            "ok_pages": list(rep.ok_pages),
            "partial_pages": list(rep.partial_pages),
            "needs_review_pages": list(rep.needs_review_pages),
            "failed_pages": list(rep.failed_pages),
            "missing_pages": list(rep.missing_pages),
            "retry_page_indices": list(rep.retry_page_indices),
            "retry_batch_ids": [],
            "completed_batch_ids": list(rep.completed_batch_ids),
            "missing_batch_ids": list(rep.missing_batch_ids),
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
                for item in rep.pages
            ],
        }
        forged_rep = dataclasses.replace(
            rep,
            retry_batch_ids=(),
            report_sha256=_canonical_hash(forged_payload),
        )
        with self.assertRaisesRegex(ValueError, "tampered_or_inconsistent_report"):
            select_retry_batches(plan, forged_rep)

        # Other tampered report fields: page status/flags, summary lists, ratio, hash
        p0_tampered = dataclasses.replace(rep.pages[0], status="ok")
        with self.assertRaisesRegex(ValueError, "tampered_or_inconsistent_report"):
            select_retry_batches(
                plan, dataclasses.replace(rep, pages=(p0_tampered, rep.pages[1]))
            )

        p0_flags_tampered = dataclasses.replace(
            rep.pages[0], status="ok", counts_as_covered=True, requires_retry=False
        )
        # Page 0 is in missing_batch_ids, so claiming status="ok" while its batch is in missing_batch_ids is inconsistent
        with self.assertRaisesRegex(ValueError, "tampered_or_inconsistent_report"):
            select_retry_batches(
                plan,
                dataclasses.replace(rep, pages=(p0_flags_tampered, rep.pages[1])),
            )

        for field_override in (
            {"status": "complete_raw"},
            {"can_claim_complete": True},
            {"can_freeze_benchmark": True},
            {"covered_core_pages": 2},
            {"missing_pages": (0,)},
            {"retry_page_indices": (0,)},
            {"completed_batch_ids": (plan.batches[0].batch_id,)},
            {"missing_batch_ids": ()},
            {"coverage_ratio": 1.0},
            {"report_sha256": "0" * 64},
        ):
            with self.subTest(field_override=field_override):
                with self.assertRaisesRegex(
                    ValueError, "tampered_or_inconsistent_report"
                ):
                    select_retry_batches(
                        plan, dataclasses.replace(rep, **field_override)  # type: ignore[arg-type]
                    )


if __name__ == "__main__":
    unittest.main()
