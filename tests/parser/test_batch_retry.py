"""Unit and negative tests for deterministic failed/missing-only retry planning and merge.

Covers SPEC 2, 4.2, 16.6, 19.4 and host review findings:
- All 13 original tests from initial foundation (92b910b) restored alongside new regressions.
- Legacy input fixture with only parent batch operation_id; materializing inherited producer
  onto retained page projections without inventing missing operations across >= 2 rounds.
- Binding source BatchResultInput signatures and OperationAttemptRecords; rejecting forged
  or mutated prior_history before any callback runs.
- Rejecting spoofed outcome provenance while filling absent fields from explicit retry plan.
- Wrapping all callback return validation failures (None, wrongpage, overlap, spoofop) in
  RetryExecutionError to preserve completed progress.
- Enforcing history chaining (MergedRetryResult or explicit prior_history) when retries > 0.
- Input length limits on results, history, and outcomes.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
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
    MAX_BATCH_RESULTS_INPUT,
    MAX_HISTORY_RECORDS,
    MAX_RETRY_OUTCOMES_INPUT,
    MergedRetryResult,
    OperationAttemptRecord,
    RetryExecutionError,
    RetryPlan,
    RetryTargetPage,
    derive_retry_plan,
    execute_and_merge_retry,
    execute_retry,
    merge_retry_results,
)

DOC_ID = "doc_test_retry"
DOC_SHA = "b" * 64


def _build_legacy_result_for_plan(
    plan: BatchPlan,
    *,
    failed_core_pages: dict[int, tuple[str, ...]] | None = None,
    partial_core_pages: dict[int, tuple[str, ...]] | None = None,
    needs_review_core_pages: dict[int, tuple[str, ...]] | None = None,
    omitted_core_pages: set[int] | None = None,
    operation_prefix: str = "initial_op",
) -> list[BatchResultInput]:
    """Legacy fixture: only parent BatchResultInput has operation_id; pages have None."""
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


class OriginalBatchRetryDerivationAndExecution(unittest.TestCase):
    """Restored exact 5 tests from 92b910b TestBatchRetryDerivationAndExecution."""

    def setUp(self) -> None:
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )

    def test_derive_retry_plan_failed_and_missing_only(self) -> None:
        prior_results = _build_legacy_result_for_plan(
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

        self.assertEqual(retry_plan.target_page_indices, (5, 22))
        self.assertEqual(
            retry_plan.target_batch_ids,
            (self.plan.batches[0].batch_id, self.plan.batches[2].batch_id),
        )

        t5 = retry_plan.target_pages[0]
        self.assertEqual(t5.page_index, 5)
        self.assertEqual(t5.owner_batch_id, self.plan.batches[0].batch_id)
        self.assertEqual(t5.prior_status, "failed")
        self.assertEqual(t5.prior_diagnostics, ("ocr_timeout",))
        self.assertEqual(t5.prior_operation_id, "initial_op_0000")

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
        prior_results = _build_legacy_result_for_plan(
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

        self.assertEqual(call_counts[5], 1)
        self.assertEqual(call_counts[22], 1)
        for p in range(self.plan.page_count):
            if p not in (5, 22):
                self.assertEqual(call_counts[p], 0, f"Page {p} should not have been called")

        self.assertEqual(merged.coverage_report.status, "complete_raw")
        self.assertTrue(merged.coverage_report.can_claim_complete)
        self.assertFalse(merged.coverage_report.can_freeze_benchmark)
        self.assertEqual(len(merged.coverage_report.ok_pages), 25)
        self.assertEqual(merged.coverage_report.retry_page_indices, ())

        b0_orig = prior_results[0]
        p5_orig = next(p for p in b0_orig.pages if p.page_index == 5)
        self.assertEqual(p5_orig.status, "failed")

        self.assertEqual(len(merged.operation_history), 4)
        retry_hist = merged.operation_history[-1]
        self.assertEqual(retry_hist.operation_id, "retry_op_001")
        self.assertEqual(retry_hist.attempt, 1)
        self.assertEqual(retry_hist.page_indices, (5, 22))

    def test_zero_callback_calls_when_no_retries_required(self) -> None:
        prior_results = _build_legacy_result_for_plan(self.plan)
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
        prior_results = _build_legacy_result_for_plan(
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

        self.assertEqual(merged.coverage_report.status, "needs_review")
        self.assertFalse(merged.coverage_report.can_claim_complete)
        self.assertEqual(merged.coverage_report.partial_pages, (3,))
        self.assertEqual(merged.coverage_report.needs_review_pages, (7,))
        self.assertEqual(merged.coverage_report.failed_pages, ())
        self.assertEqual(merged.coverage_report.retry_page_indices, ())

    def test_failed_pages_remain_visible_if_retry_fails(self) -> None:
        prior_results = _build_legacy_result_for_plan(
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


class OriginalBatchRetryFailClosedNegativeCases(unittest.TestCase):
    """Restored exact 8 tests from 92b910b TestBatchRetryFailClosedNegativeCases."""

    def setUp(self) -> None:
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )
        self.prior_results = _build_legacy_result_for_plan(
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
        with self.assertRaisesRegex(ValueError, "tampered_or_stale_retry_plan"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                tampered,
                [BatchPageReport(page_index=5, status="ok")],
            )

    def test_rejects_mismatched_prior_report(self) -> None:
        diff_results = _build_legacy_result_for_plan(self.plan)
        with self.assertRaisesRegex(ValueError, "tampered_or_stale_retry_plan"):
            merge_retry_results(
                self.plan,
                diff_results,
                self.retry_plan,
                [BatchPageReport(page_index=5, status="ok")],
            )

    def test_rejects_unexpected_page_out_of_bounds_or_not_in_retry_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected_page"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=99, status="ok")],
            )

        with self.assertRaisesRegex(ValueError, "cannot_reprocess_or_alter_successful_core_page"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=8, status="ok")],
            )

    def test_rejects_altering_successful_partial_or_needs_review_core_pages(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot_reprocess_or_alter_successful_core_page"):
            merge_retry_results(
                self.plan,
                self.prior_results,
                self.retry_plan,
                [BatchPageReport(page_index=3, status="ok")],
            )

    def test_rejects_overlap_upgrading_core_coverage(self) -> None:
        def spoof_overlap(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                role="overlap_before",
            )

        with self.assertRaises(RetryExecutionError) as ctx:
            execute_retry(self.retry_plan, spoof_overlap)
        self.assertIn("overlap_cannot_upgrade_core_coverage", str(ctx.exception))

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


class TestHostReviewRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )

    def test_import_in_fresh_subprocess_both_orders(self) -> None:
        cmd1 = [
            sys.executable,
            "-c",
            "from lab_parser.batch_retry import derive_retry_plan; print('ok1')",
        ]
        cmd2 = [
            sys.executable,
            "-c",
            "import lab_parser.batch_coverage; from lab_parser.batch_retry import derive_retry_plan; print('ok2')",
        ]
        res1 = subprocess.run(cmd1, capture_output=True, text=True)
        self.assertEqual(res1.returncode, 0, f"cmd1 failed: {res1.stderr}")
        self.assertIn("ok1", res1.stdout)

        res2 = subprocess.run(cmd2, capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0, f"cmd2 failed: {res2.stderr}")
        self.assertIn("ok2", res2.stdout)

    def test_stale_or_forged_retry_plan_executes_zero_callbacks(self) -> None:
        prior_results = _build_legacy_result_for_plan(
            self.plan,
            failed_core_pages={5: ("timeout",)},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_001",
            attempt=1,
        )

        callback_calls = []

        def callback(target: RetryTargetPage) -> BatchPageReport:
            callback_calls.append(target.page_index)
            return BatchPageReport(page_index=target.page_index, status="ok")

        fixed_prior = _build_legacy_result_for_plan(self.plan)
        with self.assertRaisesRegex(ValueError, "tampered_or_stale_retry_plan"):
            execute_and_merge_retry(
                self.plan,
                fixed_prior,
                retry_plan,
                callback,
            )
        self.assertEqual(callback_calls, [], "Callback must not run when prior results changed to ok")

        forged_target = RetryTargetPage(
            page_index=0,
            owner_batch_id=self.plan.batches[0].batch_id,
            owner_batch_index=0,
            batch_page_offset=0,
            prior_status="failed",
            prior_diagnostics=("forged",),
            prior_operation_id="initial_op_0000",
            referenced_overlap_indices=(),
            referenced_overlap_reports=(),
            is_full_document=False,
        )
        forged_retry_plan = dataclasses.replace(
            retry_plan,
            target_pages=(forged_target,),
            target_page_indices=(0,),
        )
        with self.assertRaisesRegex(ValueError, "tampered_or_stale_retry_plan"):
            execute_retry(
                forged_retry_plan,
                callback,
            )
        self.assertEqual(callback_calls, [], "Callback must not run when target_pages forged to page 0")

    def test_forged_history_rejected_before_callback(self) -> None:
        prior_results = _build_legacy_result_for_plan(
            self.plan,
            failed_core_pages={5: ("timeout",)},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_hist",
            attempt=1,
        )

        # Host probe: fabricated record with page9000 and invented batch
        forged_history = (
            OperationAttemptRecord("fabricated", 0, (9000,), ("invented_batch",)),
        )
        tampered_rp = dataclasses.replace(retry_plan, prior_history=forged_history)

        callback_calls = []

        def callback(target: RetryTargetPage) -> BatchPageReport:
            callback_calls.append(target.page_index)
            return BatchPageReport(page_index=target.page_index, status="ok")

        with self.assertRaises(ValueError):
            execute_retry(tampered_rp, callback)
        self.assertEqual(callback_calls, [], "Callback must not run on tampered prior_history")

    def test_changed_original_operations_detected(self) -> None:
        prior_results = _build_legacy_result_for_plan(
            self.plan,
            failed_core_pages={5: ("ocr_fail",)},
            operation_prefix="op_first",
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_bind",
            attempt=1,
        )

        tampered_prior = _build_legacy_result_for_plan(
            self.plan,
            failed_core_pages={5: ("ocr_fail",)},
            operation_prefix="op_other",
        )
        with self.assertRaisesRegex(ValueError, "tampered_or_stale_retry_plan"):
            merge_retry_results(
                self.plan,
                tampered_prior,
                retry_plan,
                [BatchPageReport(page_index=5, status="ok")],
            )

    def test_callback_validation_failures_wrapped_in_retry_execution_error(self) -> None:
        prior_results = _build_legacy_result_for_plan(
            self.plan,
            failed_core_pages={5: ("f5",), 22: ("f22",)},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_val_fail",
            attempt=1,
        )

        # Return None on page 22 after succeeding on page 5
        def bad_return_callback(target: RetryTargetPage) -> BatchPageReport:
            if target.page_index == 5:
                return BatchPageReport(page_index=5, status="ok")
            return None  # type: ignore[return-value]

        with self.assertRaises(RetryExecutionError) as ctx:
            execute_and_merge_retry(self.plan, prior_results, retry_plan, bad_return_callback)
        err = ctx.exception
        self.assertEqual(len(err.completed_outcomes), 1)
        self.assertEqual(err.completed_outcomes[0].page_index, 5)
        self.assertIsNotNone(err.partial_merged_result)
        self.assertEqual(err.partial_merged_result.coverage_report.pages[5].status, "ok")
        self.assertEqual(err.partial_merged_result.coverage_report.pages[22].status, "failed")

    def test_legacy_input_materializes_inherited_batch_op_across_two_rounds(self) -> None:
        # Legacy input: pages have operation_id None, parent batch has initial_op_0000
        initial_results = _build_legacy_result_for_plan(
            self.plan,
            failed_core_pages={5: ("initial_fail",)},
            operation_prefix="initial_op",
        )

        # Round 1: retry page 5 with op='retry_round_1', attempt 1; fails again
        rp1 = derive_retry_plan(
            self.plan,
            initial_results,
            retry_operation_id="retry_round_1",
            attempt=1,
        )

        def r1_callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="failed",
                diagnostics=("r1_still_failed",),
            )

        merged1 = execute_and_merge_retry(
            self.plan,
            initial_results,
            rp1,
            r1_callback,
        )

        # Reused successful page 0 must have inherited known batch op 'initial_op_0000'
        b0_m1 = merged1.merged_results[0]
        p0_rep = next(p for p in b0_m1.pages if p.page_index == 0)
        self.assertEqual(p0_rep.operation_id, "initial_op_0000")
        self.assertEqual(p0_rep.attempt, 0)

        # Round 2: derive directly passing MergedRetryResult
        rp2 = derive_retry_plan(
            self.plan,
            merged1,
            retry_operation_id="retry_round_2",
            attempt=2,
        )

        def r2_callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                diagnostics=(),
            )

        merged2 = execute_and_merge_retry(
            self.plan,
            merged1.merged_results,
            rp2,
            r2_callback,
            prior_history=merged1.operation_history,
        )

        # Reused success page 0 STILL has initial_op_0000
        b0_m2 = merged2.merged_results[0]
        p0_m2 = next(p for p in b0_m2.pages if p.page_index == 0)
        self.assertEqual(p0_m2.operation_id, "initial_op_0000")

        # Page 5 has retry_round_2 and attempt 2
        p5_m2 = next(p for p in b0_m2.pages if p.page_index == 5)
        self.assertEqual(p5_m2.operation_id, "retry_round_2")
        self.assertEqual(p5_m2.attempt, 2)

        # Full history is preserved
        self.assertEqual(len(merged2.operation_history), 5)

    def test_missing_history_chaining_rejected(self) -> None:
        # If results have attempt > 0, caller must pass prior_history
        retried_page = BatchPageReport(page_index=0, status="ok", attempt=1, operation_id="r1")
        results = [
            BatchResultInput(
                batch_id=self.plan.batches[0].batch_id,
                batch_index=0,
                pages=(retried_page,),
            )
        ]
        with self.assertRaisesRegex(ValueError, "missing_required_prior_history"):
            derive_retry_plan(
                self.plan,
                results,
                retry_operation_id="r2",
                attempt=2,
                prior_history=(),
            )

    def test_length_bounds_rejected(self) -> None:
        prior_results = _build_legacy_result_for_plan(self.plan)
        oversized_results = prior_results * (MAX_BATCH_RESULTS_INPUT + 1)
        with self.assertRaisesRegex(ValueError, "prior_results exceeds maximum"):
            derive_retry_plan(
                self.plan,
                oversized_results,
                retry_operation_id="r_bound",
                attempt=1,
            )


def _build_full_result_for_plan(plan, **kwargs):
    return [dataclasses.replace(result, pages=tuple(
        dataclasses.replace(page, operation_id=result.operation_id, attempt=0)
        for page in result.pages))
        for result in _build_legacy_result_for_plan(plan, **kwargs)]


class RestoredExplicitRetryRegressions(unittest.TestCase):
    def setUp(self):
        self.plan = plan_batches(document_id=DOC_ID, document_sha256=DOC_SHA, page_count=25)

    def test_rejects_spoofed_outcome_provenance(self) -> None:
        prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("ocr_fail",)},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_op_real",
            attempt=1,
        )

        def spoof_callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                operation_id="spoofed_op",
                attempt=99,
            )

        with self.assertRaisesRegex(RetryExecutionError, "mismatched_retry_operation_id"):
            execute_retry(retry_plan, spoof_callback)

        def absent_callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                operation_id=None,
                attempt=None,
            )

        outcomes = execute_retry(retry_plan, absent_callback)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].operation_id, "retry_op_real")
        self.assertEqual(outcomes[0].attempt, 1)

    def test_truthful_is_full_document_on_target_page(self) -> None:
        # Multi-page doc -> target page has is_full_document=False
        prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("timeout",)},
        )
        rp = derive_retry_plan(self.plan, prior_results, retry_operation_id="op_multi", attempt=1)
        self.assertFalse(rp.target_pages[0].is_full_document)

        # 1-page doc -> target page has is_full_document=True
        single_plan = plan_batches(document_id="single", document_sha256="c" * 64, page_count=1)
        single_res = [
            BatchResultInput(
                batch_id=single_plan.batches[0].batch_id,
                batch_index=0,
                pages=(BatchPageReport(page_index=0, status="failed", diagnostics=("err",)),),
                operation_id="single_op",
            )
        ]
        rp_single = derive_retry_plan(single_plan, single_res, retry_operation_id="op_single", attempt=1)
        self.assertTrue(rp_single.target_pages[0].is_full_document)

    def test_two_failed_retry_rounds_preserves_multiround_history_and_reused_successes(self) -> None:
        # Initial run: batch 0 has pages 0..9, page 5 failed with initial_op_0000
        initial_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("initial_fail",)},
            operation_prefix="initial_op",
        )

        # Round 1: retry page 5 with op='retry_round_1', attempt 1; fails again!
        rp1 = derive_retry_plan(
            self.plan,
            initial_results,
            retry_operation_id="retry_round_1",
            attempt=1,
        )

        def r1_callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="failed",
                diagnostics=("r1_still_failed",),
            )

        merged1 = execute_and_merge_retry(
            self.plan,
            initial_results,
            rp1,
            r1_callback,
        )

        # Verify Round 1 merge:
        self.assertEqual(merged1.coverage_report.status, "incomplete")
        self.assertFalse(merged1.coverage_report.can_claim_complete)
        self.assertEqual(merged1.coverage_report.failed_pages, (5,))

        # Successful pages in batch 0 must preserve original provenance (initial_op_0000, attempt 0), NOT retry_round_1!
        b0_m1 = merged1.merged_results[0]
        p0_rep = next(p for p in b0_m1.pages if p.page_index == 0)
        self.assertEqual(p0_rep.operation_id, "initial_op_0000")
        self.assertEqual(p0_rep.attempt, 0)

        # Page 5 has retry_round_1
        p5_rep1 = next(p for p in b0_m1.pages if p.page_index == 5)
        self.assertEqual(p5_rep1.operation_id, "retry_round_1")
        self.assertEqual(p5_rep1.attempt, 1)

        # Batch 0 has multiple producer ops -> batch operation_id is None
        self.assertIsNone(b0_m1.operation_id)

        # Round 2: retry page 5 with op='retry_round_2', attempt 2; succeeds!
        rp2 = derive_retry_plan(
            self.plan,
            merged1.merged_results,
            retry_operation_id="retry_round_2",
            attempt=2,
            prior_history=merged1.operation_history,
        )

        def r2_callback(target: RetryTargetPage) -> BatchPageReport:
            return BatchPageReport(
                page_index=target.page_index,
                status="ok",
                diagnostics=(),
            )

        merged2 = execute_and_merge_retry(
            self.plan,
            merged1.merged_results,
            rp2,
            r2_callback,
            prior_history=merged1.operation_history,
        )

        # Verify Round 2 merge:
        self.assertEqual(merged2.coverage_report.status, "complete_raw")
        self.assertTrue(merged2.coverage_report.can_claim_complete)

        # Page 0 STILL has initial_op_0000!
        b0_m2 = merged2.merged_results[0]
        p0_m2 = next(p for p in b0_m2.pages if p.page_index == 0)
        self.assertEqual(p0_m2.operation_id, "initial_op_0000")
        self.assertEqual(p0_m2.attempt, 0)

        # Page 5 has retry_round_2 and attempt 2
        p5_m2 = next(p for p in b0_m2.pages if p.page_index == 5)
        self.assertEqual(p5_m2.operation_id, "retry_round_2")
        self.assertEqual(p5_m2.attempt, 2)

        # Full operation history is preserved: 3 initial ops + round 1 op + round 2 op
        self.assertEqual(len(merged2.operation_history), 5)
        op_ids = [h.operation_id for h in merged2.operation_history]
        self.assertIn("initial_op_0000", op_ids)
        self.assertIn("retry_round_1", op_ids)
        self.assertIn("retry_round_2", op_ids)

        h_r1 = next(h for h in merged2.operation_history if h.operation_id == "retry_round_1")
        self.assertEqual(h_r1.attempt, 1)
        self.assertEqual(h_r1.page_indices, (5,))

        h_r2 = next(h for h in merged2.operation_history if h.operation_id == "retry_round_2")
        self.assertEqual(h_r2.attempt, 2)
        self.assertEqual(h_r2.page_indices, (5,))

    def test_attempt_progression_and_duplicate_operation_id_rejected(self) -> None:
        prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("fail",)},
            operation_prefix="initial_op",
        )

        # Reusing initial_op_0000
        with self.assertRaisesRegex(ValueError, "duplicate_operation_id"):
            derive_retry_plan(
                self.plan,
                prior_results,
                retry_operation_id="initial_op_0000",
                attempt=1,
            )

        # Attempt 0 < minimum 1
        with self.assertRaisesRegex(ValueError, "invalid_attempt"):
            derive_retry_plan(
                self.plan,
                prior_results,
                retry_operation_id="retry_op_bad_attempt",
                attempt=0,
            )

        # Attempt 1 when prior history already reached attempt 1
        history_at_1 = (
            OperationAttemptRecord(
                operation_id="prior_op",
                attempt=1,
                page_indices=(5,),
                batch_ids=(self.plan.batches[0].batch_id,),
            ),
        )
        with self.assertRaisesRegex(ValueError, "invalid_attempt_progression"):
            derive_retry_plan(
                self.plan,
                prior_results,
                retry_operation_id="retry_op_stale_attempt",
                attempt=1,
                prior_history=history_at_1,
            )

    def test_callback_exception_preserves_recoverable_progress(self) -> None:
        # Batch 0: page 5 fails
        # Batch 2: page 22 fails
        prior_results = _build_full_result_for_plan(
            self.plan,
            failed_core_pages={5: ("f5",), 22: ("f22",)},
        )
        retry_plan = derive_retry_plan(
            self.plan,
            prior_results,
            retry_operation_id="retry_crash_op",
            attempt=1,
        )

        def crashing_callback(target: RetryTargetPage) -> BatchPageReport:
            if target.page_index == 5:
                return BatchPageReport(page_index=5, status="ok", diagnostics=())
            raise RuntimeError("provider timeout on page 22")

        with self.assertRaises(RetryExecutionError) as ctx:
            execute_and_merge_retry(
                self.plan,
                prior_results,
                retry_plan,
                crashing_callback,
            )

        err = ctx.exception
        self.assertEqual(len(err.completed_outcomes), 1)
        self.assertEqual(err.completed_outcomes[0].page_index, 5)
        self.assertEqual(err.failed_target.page_index, 22)
        self.assertIsInstance(err.cause, RuntimeError)

        # Partial result is preserved without fabricating success:
        self.assertIsNotNone(err.partial_merged_result)
        partial_rep = err.partial_merged_result.coverage_report
        self.assertEqual(partial_rep.pages[5].status, "ok")
        # Page 22 remained failed visible:
        self.assertEqual(partial_rep.pages[22].status, "failed")
        self.assertEqual(partial_rep.status, "incomplete")
        self.assertFalse(partial_rep.can_claim_complete)

    def test_unrelated_history_cannot_replace_known_source_lineage(self):
        initial = _build_legacy_result_for_plan(self.plan, failed_core_pages={5: ("failed",)})
        first_plan = derive_retry_plan(self.plan, initial, retry_operation_id="retry1")
        first = execute_and_merge_retry(self.plan, initial, first_plan,
            lambda target: BatchPageReport(target.page_index, "failed", ("again",)))
        unrelated = (OperationAttemptRecord("unrelated", 0, (0,), (self.plan.batches[0].batch_id,)),)
        for source in (first, first.merged_results):
            with self.subTest(source=type(source).__name__):
                with self.assertRaisesRegex(ValueError, "prior_history_mismatch"):
                    derive_retry_plan(self.plan, source, retry_operation_id="retry2",
                        attempt=2, prior_history=unrelated)
        # Correct captured history remains usable and retains all producers.
        second_plan = derive_retry_plan(self.plan, first, retry_operation_id="retry2", attempt=2)
        second = execute_and_merge_retry(self.plan, first.merged_results, second_plan,
            lambda target: BatchPageReport(target.page_index, "ok"))
        self.assertEqual(second.operation_history[:-1], first.operation_history)


if __name__ == "__main__":
    unittest.main()
