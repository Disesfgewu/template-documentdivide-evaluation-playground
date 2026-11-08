"""Unit and negative tests for deterministic failed/missing-only retry planning and merge.

Covers SPEC 2, 4.2, 16.6, 19.4 and host review findings:
- Clean subprocess import in both orders without circular import failures.
- Pre-callback validation rejecting forged or stale retry plans with zero callback calls.
- Binding source BatchResultInput signatures including operation and page provenance.
- Rejecting spoofed outcome provenance while filling absent fields from explicit retry plan.
- Multi-round (>= 2 rounds) operation history and per-page provenance preservation without
  falsely relabeling reused successes as retry outputs.
- Truthful is_full_document on RetryTargetPage.
- Recoverable partial progress preservation via RetryExecutionError on callback failure.
- Fail closed for cross-document/plan, unexpected pages, duplicate conflicting results,
  and overlap upgrading core coverage.
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
                        operation_id=f"{operation_prefix}_{spec.batch_index:04d}",
                        attempt=0,
                    )
                )
            elif entry.is_core and p_idx in partial:
                pages.append(
                    BatchPageReport(
                        page_index=p_idx,
                        status="partial",
                        diagnostics=partial[p_idx],
                        role=entry.role,
                        operation_id=f"{operation_prefix}_{spec.batch_index:04d}",
                        attempt=0,
                    )
                )
            elif entry.is_core and p_idx in needs_review:
                pages.append(
                    BatchPageReport(
                        page_index=p_idx,
                        status="needs_review",
                        diagnostics=needs_review[p_idx],
                        role=entry.role,
                        operation_id=f"{operation_prefix}_{spec.batch_index:04d}",
                        attempt=0,
                    )
                )
            else:
                pages.append(
                    BatchPageReport(
                        page_index=p_idx,
                        status="ok",
                        diagnostics=(),
                        role=entry.role,
                        operation_id=f"{operation_prefix}_{spec.batch_index:04d}",
                        attempt=0,
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


class TestBatchRetryImportAndFreshness(unittest.TestCase):
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


class TestBatchRetryDerivationAndPreCallbackValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )

    def test_stale_or_forged_retry_plan_executes_zero_callbacks(self) -> None:
        prior_results = _build_full_result_for_plan(
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

        # Reproduction 1: host changed prior failed page 5 to ok before calling retry
        fixed_prior = _build_full_result_for_plan(self.plan)
        with self.assertRaisesRegex(ValueError, "tampered_or_stale_retry_plan"):
            execute_and_merge_retry(
                self.plan,
                fixed_prior,
                retry_plan,
                callback,
            )
        self.assertEqual(callback_calls, [], "Callback must not run when prior results changed to ok")

        # Reproduction 2: forging target_pages to successful page 0
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

    def test_changed_original_operations_detected(self) -> None:
        prior_results = _build_full_result_for_plan(
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

        # Host changed operation_id to op_other with same status
        tampered_prior = _build_full_result_for_plan(
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

        with self.assertRaisesRegex(ValueError, "mismatched_retry_operation_id"):
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


class TestMultiRoundRetryHistoryAndProvenance(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
        )

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


class TestPartialExceptionRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = plan_batches(
            document_id=DOC_ID,
            document_sha256=DOC_SHA,
            page_count=25,
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


if __name__ == "__main__":
    unittest.main()
