"""Unit and negative tests for deterministic failed/missing-only retry planning and merge.

Covers SPEC 2, 4.2, 16.6, 19.4:
- Derives executable failed/missing-only original physical page selections from
  validated prior results without re-scheduling successful core pages.
- Retains per-page successful results, operation provenance, and immutable originals.
- Deterministic counting callback proving only failed/missing pages execute and
  successes are preserved.
- Zero callback calls when no retries are required.
- Merge of retry outcomes preserving prior operation history, immutable originals,
  and coverage calculation using the existing evaluate_coverage contract.
- Failed/missing pages remain visible if retries fail.
- Honest full_document and benchmark freeze flags.
- Fail closed for cross-document/plan, unexpected pages, duplicate conflicting
  results, altering successful/partial/needs_review core outcomes, or overlap
  upgrading core coverage.
"""

from __future__ import annotations

import dataclasses
import unittest

from lab_parser.batch_coverage import (
    BATCH_PLAN_RULE_VERSION,
    BatchPageReport,
    BatchPlan,
    BatchResultInput,
    CoverageReport,
    evaluate_coverage,
    plan_batches,
)
from lab_parser.batch_retry import (
    MergedRetryResult,
    OperationAttemptRecord,
    RetryPlan,
    RetryTargetPage,
    derive_retry_plan,
    execute_and_merge_retry,
    execute_retry,
    merge_retry_results,
)

DOC_ID = "doc_test_retry"
DOC_SHA = "b" * 64


def _build_full_result_for_plan(
    plan: BatchPlan,
    *,
    failed_core_pages: dict[int, tuple[str, ...]] | None = None,
    partial_core_pages: dict[int, tuple[str, ...]] | None = None,
    needs_review_core_pages: dict[int, tuple[str, ...]] | None = None,
    omitted_core_pages: set[int] | None = None,
    operation_prefix: str = "initial_op",
) -> list[BatchResultInput]:
    failed = failed_core_pages or {}
    partial = partial_core_pages or {}
    needs_review = needs_review_core_pages or {}
    omitted = omitted_core_pages or set()

    results: list[BatchResultInput] = []
    for spec in plan.batches:
        pages: list[BatchPageReport] = []
        for entry in spec.page_entries:
            p_idx = entry.page_index
            if entry.is_core and p_idx in omitted:
                continue
            if entry.is_core and p_idx in failed:
                pages.append(
                    BatchPageReport(
                        page_index=p_idx,
                        status="failed",
                        diagnostics=failed[p_idx],
                        role=entry.role,
                    )
                )
            elif entry.is_core and p_idx in partial:
                pages.append(
                    BatchPageReport(
                        page_index=p_idx,
                        status="partial",
                        diagnostics=partial[p_idx],
                        role=entry.role,
                    )
                )
            elif entry.is_core and p_idx in needs_review:
                pages.append(
                    BatchPageReport(
                        page_index=p_idx,
                        status="needs_review",
                        diagnostics=needs_review[p_idx],
                        role=entry.role,
                    )
                )
            else:
                pages.append(
                    BatchPageReport(
                        page_index=p_idx,
                        status="ok",
                        diagnostics=(),
                        role=entry.role,
                    )
                )
        results.append(
            BatchResultInput(
                batch_id=spec.batch_id,
                batch_index=spec.batch_index,
                pages=tuple(pages),
                operation_id=f"{operation_prefix}_{spec.batch_index:04d}",
            )
        )
    return results


class TestBatchRetryDerivationAndExecution(unittest.TestCase):
    def setUp(self) -> None:
        # 25 pages -> 3 batches: batch 0 (0..9), batch 1 (10..19), batch 2 (20..24)
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )

    def test_derive_retry_plan_failed_and_missing_only(self) -> None:
        # Batch 0: page 5 fails
        # Batch 2: page 22 is omitted (missing)
        # All other core pages are OK
        prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("ocr_timeout",)},
            omitted_core_pages={22},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_001",
            attempt=1,
        )

        self.assertIsInstance(retry_plan, RetryPlan)
        self.assertEqual(retry_plan.document_id, DOC_ID)
        self.assertEqual(retry_plan.document_sha256, DOC_SHA)
        self.assertEqual(retry_plan.plan_id, self.plan.plan_id)
        self.assertEqual(retry_plan.retry_operation_id, "retry_op_001")
        self.assertEqual(retry_plan.attempt, 1)

        # STRICTLY failed and missing core pages only!
        self.assertEqual(retry_plan.target_page_indices, (5, 22))
        self.assertEqual(
            retry_plan.target_batch_ids,
            (self.plan.batches[0].batch_id, self.plan.batches[2].batch_id),
        )

        # Examine targets
        t5 = retry_plan.target_pages[0]
        self.assertEqual(t5.page_index, 5)
        self.assertEqual(t5.owner_batch_id, self.plan.batches[0].batch_id)
        self.assertEqual(t5.prior_status, "failed")
        self.assertEqual(t5.prior_diagnostics, ("ocr_timeout",))
        self.assertEqual(t5.prior_operation_id, "initial_op_0000")
        # Referenced overlap reports should contain neighbor pages from batch 0 (e.g. 0..4, 6..10)
        overlap_indices = {rep.page_index for rep in t5.referenced_overlap_reports}
        self.assertIn(4, overlap_indices)
        self.assertIn(6, overlap_indices)
        self.assertNotIn(5, overlap_indices)

        t22 = retry_plan.target_pages[1]
        self.assertEqual(t22.page_index, 22)
        self.assertEqual(t22.owner_batch_id, self.plan.batches[2].batch_id)
        self.assertEqual(t22.prior_status, "missing")
        self.assertEqual(t22.prior_diagnostics, ("core_page_omitted_from_batch_result",))

    def test_deterministic_counting_callback_executes_only_failed_pages(self) -> None:
        prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("ocr_timeout",)},
            omitted_core_pages={22},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_001",
            attempt=1,
        )

        call_counts: dict[int, int] = {p: 0 for p in range(self.plan.page_count)}

        def counting_callback(target: RetryTargetPage) -> BatchPageReport:
            call_counts[target.page_index] += 1
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                diagnostics=(),
                role="core",
            )

        merged = execute_and_merge_retry(
            self.plan,
            prior_results,
            retry_plan,
            counting_callback,
        )

        # Proves only failed/missing pages executed:
        self.assertEqual(call_counts[5], 1)
        self.assertEqual(call_counts[22], 1)
        for p in range(self.plan.page_count):
            if p not in (5, 22):
                self.assertEqual(call_counts[p], 0, f"Page {p} should not have been called")

        # Proves successful merge and honest coverage:
        self.assertEqual(merged.coverage_report.status, "complete_raw")
        self.assertTrue(merged.coverage_report.can_claim_complete)
        self.assertFalse(merged.coverage_report.can_freeze_benchmark)
        self.assertEqual(len(merged.coverage_report.ok_pages), 25)
        self.assertEqual(merged.coverage_report.retry_page_indices, ())

        # Proves immutable originals preserved:
        b0_orig = prior_results[0]
        p5_orig = next(p for p in b0_orig.pages if p.page_index == 5)
        self.assertEqual(p5_orig.status, "failed")

        # Operation history tracks attempts:
        self.assertEqual(len(merged.operation_history), 4)  # 3 initial + 1 retry
        retry_hist = merged.operation_history[-1]
        self.assertEqual(retry_hist.operation_id, "retry_op_001")
        self.assertEqual(retry_hist.attempt, 1)
        self.assertEqual(retry_hist.page_indices, (5, 22))

    def test_zero_callback_calls_when_no_retries_required(self) -> None:
        # All pages OK initially
        prior_results = _build_full_result_for_plan(self.plan)
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_empty",
            attempt=1,
        )

        self.assertEqual(retry_plan.target_pages, ())
        self.assertEqual(retry_plan.target_page_indices, ())
        self.assertEqual(retry_plan.target_batch_ids, ())

        callback_calls = []

        def callback(target: RetryTargetPage) -> BatchPageReport:
            callback_calls.append(target.page_index)
            return BatchPageReport(page_index=target.page_index, status="ok")

        merged = execute_and_merge_retry(
            self.plan,
            prior_results,
            retry_plan,
            callback,
        )

        self.assertEqual(callback_calls, [])
        self.assertEqual(len(merged.merged_results), len(prior_results))
        self.assertEqual(merged.coverage_report.status, "complete_raw")

    def test_partial_and_needs_review_are_never_reprocessed(self) -> None:
        # Page 3 is partial, page 7 is needs_review, page 12 is failed
        prior_results = _build_full_result_for_plan(
            self.plan,
            partial_core_pages={3: ("partial_table",)},
            needs_review_core_pages={7: ("low_confidence_formula",)},
            failed_core_pages={12: ("render_crash",)},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_core_only",
            attempt=1,
        )

        # ONLY page 12 should be scheduled! Pages 3 and 7 must be reused without retry
        self.assertEqual(retry_plan.target_page_indices, (12,))

        def callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                diagnostics=(),
            )

        merged = execute_and_merge_retry(
            self.plan,
            prior_results,
            retry_plan,
            callback,
        )

        # After retrying page 12 to ok, document still has partial page 3 and needs_review page 7
        self.assertEqual(merged.coverage_report.status, "needs_review")
        self.assertFalse(merged.coverage_report.can_claim_complete)
        self.assertEqual(merged.coverage_report.partial_pages, (3,))
        self.assertEqual(merged.coverage_report.needs_review_pages, (7,))
        self.assertEqual(merged.coverage_report.failed_pages, ())
        self.assertEqual(merged.coverage_report.retry_page_indices, ())

    def test_failed_pages_remain_visible_if_retry_fails(self) -> None:
        prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("initial_fail",)},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_still_fails",
            attempt=2,
        )

        def failing_callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="failed",
                diagnostics=("retry_still_failed",),
            )

        merged = execute_and_merge_retry(
            self.plan,
            prior_results,
            retry_plan,
            failing_callback,
        )

        self.assertEqual(merged.coverage_report.status, "incomplete")
        self.assertFalse(merged.coverage_report.can_claim_complete)
        self.assertEqual(merged.coverage_report.failed_pages, (5,))
        self.assertEqual(merged.coverage_report.pages[5].diagnostics, ("retry_still_failed",))


class TestBatchRetryFailClosedNegativeCases(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )
        self.prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("initial_error",)},
            partial_core_pages={3: ("partial_block",)},
        )
        self.retry_plan = derive_retry_plan(
            self.plan,
            self.prior_results,
            retry_operation_id="retry_op_fail_closed",
            attempt=1,
        )

    def test_rejects_cross_document_and_plan_mismatch(self) -> None:
        other_plan = plan_batches(
            document_id="other_doc",
            document_sha256="c" * 64,
            page_count=25,
        )
        with self.assertRaisesRegex(ValueError, "cross_document_or_plan_mismatch"):
            merge_retry_results(
                other_plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=5, status="ok")],
            )

    def test_rejects_tampered_retry_plan_hash(self) -> None:
        tampered = dataclasses.replace(self.retry_plan, retry_plan_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "tampered_retry_plan"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                tampered,
                [BatchPageReport(page_index=5, status="ok")],
            )

    def test_rejects_mismatched_prior_report(self) -> None:
        diff_results = _build_full_result_for_plan(self.plan)
        with self.assertRaisesRegex(ValueError, "prior_report_mismatch"):
            merge_retry_results(
                self.plan,
                diff_results,
                self.retry_plan,
                [BatchPageReport(page_index=5, status="ok")],
            )

    def test_rejects_unexpected_page_out_of_bounds_or_not_in_retry_plan(self) -> None:
        # Page 99 is completely out of document bounds
        with self.assertRaisesRegex(ValueError, "unexpected_page"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=99, status="ok")],
            )

        # Page 8 was OK, not in retry plan
        with self.assertRaisesRegex(ValueError, "cannot_reprocess_or_alter_successful_core_page"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=8, status="ok")],
            )

    def test_rejects_altering_successful_partial_or_needs_review_core_pages(self) -> None:
        # Page 3 was partial in prior results
        with self.assertRaisesRegex(ValueError, "cannot_reprocess_or_alter_successful_core_page"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=3, status="ok")],
            )

    def test_rejects_overlap_upgrading_core_coverage(self) -> None:
        # Callback returns role="overlap_before" for core retry target 5
        def spoof_overlap(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                role="overlap_before",
            )

        with self.assertRaisesRegex(ValueError, "overlap_cannot_upgrade_core_coverage"):
            execute_retry(self.retry_plan, spoof_overlap)

        with self.assertRaisesRegex(ValueError, "overlap_cannot_upgrade_core_coverage"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=5, status="ok", role="overlap_after")],
            )

    def test_rejects_conflicting_duplicate_retry_outcomes(self) -> None:
        conflicting = [
            BatchPageReport(page_index=5, status="ok"),
            BatchPageReport(page_index=5, status="failed", diagnostics=("differing_error",)),
        ]
        with self.assertRaisesRegex(ValueError, "conflicting_duplicate_retry_page_result"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                conflicting,
            )

    def test_idempotent_exact_duplicate_retry_outcomes_accepted(self) -> None:
        identical = [
            BatchPageReport(page_index=5, status="ok"),
            BatchPageReport(page_index=5, status="ok"),
        ]
        merged = merge_retry_results(
            self.plan,
            self.prior_results,
            self.retry_plan,
            identical,
        )
        self.assertEqual(merged.coverage_report.pages[5].status, "ok")


if __name__ == "__main__":
    unittest.main()
