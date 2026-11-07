"""Unit and boundary tests for judge calibration eligibility evaluator (SPEC 17.6, 18, 19.8)."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from lab_contracts.benchmark import QuotaBucket
from lab_contracts.calibration import (
    AdjudicationRecord,
    AnnotatorAssessment,
    AnnotatorFactLabel,
    AnswerScenarioCategory,
    CALIBRATION_EVALUATOR_REVISION,
    CalibrationCaseRecord,
    CalibrationEvaluationInput,
    CalibrationFactSpec,
    DETERMINISTIC_METRIC_SET,
    JudgeCaseOutputRecord,
    JudgeFactOutput,
    JudgeRepeatRun,
    MAX_CALIBRATION_PAYLOAD_BYTES,
    MultimodalFailureCategory,
    evaluate_judge_calibration,
    parse_calibration_input,
    snapshot_sha256,
)
from lab_contracts.method import StrictJSONError
from lab_contracts.score import MetricResult, display_ratio

TS_A = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
TS_B = datetime(2026, 11, 6, 10, 5, tzinfo=timezone.utc)
TS_ADJ = datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc)
TS_REV = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _sha(idx: int) -> str:
    return f"{idx:064x}"


def _make_metric(
    question_id: str,
    metric_name: str,
    grader_revision: str = "grader@1",
    numerator: float | None = 1.0,
    denominator: float | None = 1.0,
    status: str = "scored",
    reason_codes: list[str] | None = None,
) -> MetricResult:
    if status == "scored":
        assert numerator is not None and denominator is not None
        raw = 100.0 * numerator / denominator
        disp = display_ratio(numerator, denominator)
        rc = reason_codes or []
    elif status == "not_applicable":
        numerator = None
        denominator = None
        raw = None
        disp = None
        rc = reason_codes or ["no_applicable_checks"]
    else:
        numerator = None
        denominator = None
        raw = None
        disp = None
        rc = reason_codes or ["grader_fault"]
    return MetricResult(
        question_id=question_id,
        metric=metric_name,
        status=status,
        raw_value=raw,
        display_value=disp,
        rounding="lab-round-1",
        numerator=numerator,
        denominator=denominator,
        reason_codes=rc,
        grader_revision=grader_revision,
        support_refs=[],
    )


def _default_deterministic_metrics(
    question_id: str,
    grader_revision: str = "grader@1",
    *,
    textual_applicable: bool = True,
) -> list[MetricResult]:
    out: list[MetricResult] = []
    for m in sorted(DETERMINISTIC_METRIC_SET):
        if m == "textual_consistency" and not textual_applicable:
            out.append(_make_metric(question_id, m, grader_revision=grader_revision, status="not_applicable"))
        else:
            out.append(_make_metric(question_id, m, grader_revision=grader_revision, numerator=1.0, denominator=1.0))
    return out


def _build_case_and_output(
    index: int,
    *,
    bucket: QuotaBucket,
    is_multimodal: bool = False,
    is_cross_page_multimodal: bool = False,
    scenarios: list[AnswerScenarioCategory] | None = None,
    mm_categories: list[MultimodalFailureCategory] | None = None,
    gold_score: float = 1.0,
    judge_score: float | None = None,
    has_contradiction: bool = False,
    judge_contradiction_score: float = 0.0,
    disagreement: bool = False,
    adjudicated: bool = True,
    adjudication_status: str = "resolved",
    annotator_a_id: str = "human_alice",
    annotator_b_id: str = "human_bob",
    adjudicator_id: str = "human_carol",
    grader_revision: str = "grader@1",
    judge_model_revision: str = "judge-model@2026-11",
    judge_prompt_version: str = "judge-prompt@v1",
) -> tuple[CalibrationCaseRecord, JudgeCaseOutputRecord]:
    cid = f"case_{index:03d}"
    qid = f"q_{index:03d}"
    sid = f"sub_{index:03d}"
    ssha = _sha(index + 1)

    if scenarios is None:
        if has_contradiction:
            scenarios = [AnswerScenarioCategory.extra_contradiction]
        elif gold_score == 1.0:
            scenarios = [AnswerScenarioCategory.full_correct]
        elif gold_score == 0.5:
            scenarios = [AnswerScenarioCategory.partial_correct]
        else:
            scenarios = [AnswerScenarioCategory.wrong_answer]

    if mm_categories is None:
        mm_categories = [MultimodalFailureCategory.wrong_value] if is_multimodal else []

    if has_contradiction:
        gold_score = 0.0
        actual_judge_score = judge_contradiction_score if judge_score is None else judge_score
    else:
        actual_judge_score = gold_score if judge_score is None else judge_score

    partial_rubric = (gold_score == 0.5 or actual_judge_score == 0.5 or disagreement)
    fact_id = "f1"
    facts = [
        CalibrationFactSpec(
            fact_id=fact_id,
            weight=1.0,
            partial_rubric_defined=partial_rubric,
            has_major_contradiction=has_contradiction,
        )
    ]

    score_a = gold_score
    score_b = (0.0 if gold_score == 1.0 else 1.0) if disagreement else gold_score
    if has_contradiction and not disagreement:
        score_b = 0.0

    ann_a = AnnotatorAssessment(
        annotator_id=annotator_a_id,
        assessed_at=TS_A,
        fact_labels=[AnnotatorFactLabel(fact_id=fact_id, score=score_a, reason="Annotator A check")],
        major_contradiction_detected=has_contradiction,
    )
    ann_b = AnnotatorAssessment(
        annotator_id=annotator_b_id,
        assessed_at=TS_B,
        fact_labels=[AnnotatorFactLabel(fact_id=fact_id, score=score_b, reason="Annotator B check")],
        major_contradiction_detected=has_contradiction,
    )

    adj: AdjudicationRecord | None = None
    if disagreement and adjudicated:
        adj = AdjudicationRecord(
            adjudicator_id=adjudicator_id,
            adjudicated_at=TS_ADJ,
            resolution_status=adjudication_status,
            rationale="Adjudicated consensus between A and B",
            adjudicated_fact_labels=[AnnotatorFactLabel(fact_id=fact_id, score=gold_score, reason="Adjudicated label")],
            major_contradiction_confirmed=has_contradiction,
        )

    textual_app = (index % 2 == 0)
    det_metrics = _default_deterministic_metrics(
        qid, grader_revision=grader_revision, textual_applicable=textual_app
    )
    case = CalibrationCaseRecord(
        case_id=cid,
        question_id=qid,
        submission_id=sid,
        submission_sha256=ssha,
        quota_bucket=bucket,
        is_multimodal=is_multimodal,
        is_cross_page_multimodal=is_cross_page_multimodal,
        scenario_categories=scenarios,
        multimodal_categories=mm_categories if is_multimodal else [],
        facts=facts,
        annotator_a=ann_a,
        annotator_b=ann_b,
        adjudication=adj,
        golden_deterministic_metrics=det_metrics,
    )

    j_corr = _make_metric(
        qid,
        "correctness",
        grader_revision=grader_revision,
        numerator=actual_judge_score,
        denominator=1.0,
    )
    j_out = JudgeCaseOutputRecord(
        case_id=cid,
        question_id=qid,
        submission_id=sid,
        submission_sha256=ssha,
        grader_revision=grader_revision,
        judge_model_revision=judge_model_revision,
        judge_prompt_version=judge_prompt_version,
        status="scored",
        fact_outputs=[
            JudgeFactOutput(
                fact_id=fact_id,
                status="scored",
                score=actual_judge_score,
                reason_codes=["matched_fact"] if actual_judge_score == 1.0 else ["fact_error"],
                rationale="Judge evaluated fact against reference",
            )
        ],
        correctness_result=j_corr,
        deterministic_metrics=_default_deterministic_metrics(
            qid, grader_revision=grader_revision, textual_applicable=textual_app
        ),
        repeat_runs=[
            JudgeRepeatRun(
                run_index=r_idx,
                status="scored",
                correctness_score=100.0 * actual_judge_score,
                fact_scores={fact_id: actual_judge_score},
            )
            for r_idx in (1, 2, 3)
        ],
    )
    return case, j_out


def build_valid_40_case_bundle(
    *,
    num_disagreements_judge_vs_gold: int = 0,
) -> CalibrationEvaluationInput:
    """Construct a minimal compliant 40-case calibration bundle meeting all SPEC 17.6 & 19.8 gates:
    - 40 total cases
    - 10 general, 10 cross_page (including 4 cross-page multimodal), 10 chart_table (all multimodal), 10 confusable
      (>= 8 per special bucket, 14 multimodal >= 12, 4 cross-page multimodal >= 4)
    - All 6 AnswerScenarioCategory covered
    - All 6 MultimodalFailureCategory covered
    - Explicit annotator disagreement + adjudication on several cases
    """
    all_scenarios = list(AnswerScenarioCategory)
    all_mm_cats = list(MultimodalFailureCategory)
    cases: list[CalibrationCaseRecord] = []
    outputs: list[JudgeCaseOutputRecord] = []

    for i in range(40):
        if i < 10:
            bucket = QuotaBucket.general
            is_mm = False
            is_cp_mm = False
        elif i < 20:
            bucket = QuotaBucket.cross_page
            is_mm = i < 14  # 4 cross-page multimodal cases (indices 10..13)
            is_cp_mm = is_mm
        elif i < 30:
            bucket = QuotaBucket.chart_table
            is_mm = True  # 10 chart_table multimodal cases
            is_cp_mm = False
        else:
            bucket = QuotaBucket.confusable
            is_mm = False
            is_cp_mm = False

        scen = all_scenarios[i % len(all_scenarios)]
        has_contra = scen is AnswerScenarioCategory.extra_contradiction
        if has_contra:
            gold_score = 0.0
        elif scen is AnswerScenarioCategory.full_correct:
            gold_score = 1.0
        elif scen is AnswerScenarioCategory.partial_correct:
            gold_score = 0.5
        elif scen is AnswerScenarioCategory.wrong_source:
            gold_score = 1.0
        else:
            gold_score = 0.0

        # Introduce judge vs gold label disagreement on the last `num_disagreements_judge_vs_gold` non-contradiction cases
        judge_score = gold_score
        if i >= 40 - num_disagreements_judge_vs_gold:
            # Ensure we don't flip a contradiction case to 1.0 unless specifically testing contradiction failure
            if has_contra:
                judge_score = 0.5
            else:
                judge_score = 0.0 if gold_score == 1.0 else 1.0

        mm_cats = [all_mm_cats[i % len(all_mm_cats)]] if is_mm else []
        disagreement = (i % 5 == 0)

        c, o = _build_case_and_output(
            i,
            bucket=bucket,
            is_multimodal=is_mm,
            is_cross_page_multimodal=is_cp_mm,
            scenarios=[scen],
            mm_categories=mm_cats,
            gold_score=gold_score,
            judge_score=judge_score,
            has_contradiction=has_contra,
            disagreement=disagreement,
            adjudicated=True,
        )
        cases.append(c)
        outputs.append(o)

    return CalibrationEvaluationInput(
        calibration_set_id="cal_set_v1",
        target_grader_revision="grader@1",
        target_judge_model_revision="judge-model@2026-11",
        target_judge_prompt_version="judge-prompt@v1",
        cases=cases,
        judge_outputs=outputs,
    )


class CalibrationGatePositiveAndBoundaryTests(unittest.TestCase):
    def test_compliant_40_case_bundle_is_eligible_without_granting_final_approval(self):
        bundle = build_valid_40_case_bundle(num_disagreements_judge_vs_gold=0)
        report = evaluate_judge_calibration(bundle)
        self.assertEqual(report.status, "eligible")
        self.assertEqual(report.blocking_reasons, ())
        self.assertEqual(report.diagnostics, ())
        self.assertFalse(report.final_approval_granted)
        self.assertFalse(report.human_work_verified_by_gate)
        self.assertEqual(report.evaluator_revision, CALIBRATION_EVALUATOR_REVISION)
        self.assertEqual(report.total_supplied_cases, 40)
        self.assertEqual(report.valid_adjudicated_cases, 40)
        self.assertEqual(report.evaluated_cases, 40)
        self.assertEqual(report.missing_or_pending_cases, 0)
        self.assertEqual(report.fact_agreement_numerator, 40)
        self.assertEqual(report.fact_agreement_denominator, 40)
        self.assertEqual(report.fact_agreement_display, "100.00")
        self.assertEqual(report.multimodal_cases, 14)
        self.assertEqual(report.cross_page_multimodal_cases, 4)

    def test_fact_agreement_exact_90_percent_boundary_passes_and_below_90_blocks(self):
        # 36/40 = 90.00% -> eligible
        bundle_90 = build_valid_40_case_bundle(num_disagreements_judge_vs_gold=4)
        report_90 = evaluate_judge_calibration(bundle_90)
        self.assertEqual(report_90.status, "eligible")
        self.assertEqual(report_90.fact_agreement_numerator, 36)
        self.assertEqual(report_90.fact_agreement_denominator, 40)
        self.assertEqual(report_90.fact_agreement_display, "90.00")

        # 35/40 = 87.50% -> ineligible
        bundle_875 = build_valid_40_case_bundle(num_disagreements_judge_vs_gold=5)
        report_875 = evaluate_judge_calibration(bundle_875)
        self.assertEqual(report_875.status, "ineligible")
        self.assertIn("fact_agreement_below_threshold", report_875.blocking_reasons)
        self.assertEqual(report_875.fact_agreement_display, "87.50")

    def test_total_case_count_boundary_39_blocks_40_passes(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        raw["cases"] = raw["cases"][:39]
        raw["judge_outputs"] = raw["judge_outputs"][:39]
        short_bundle = CalibrationEvaluationInput.model_validate_json(json.dumps(raw))
        report = evaluate_judge_calibration(short_bundle)
        self.assertEqual(report.status, "ineligible")
        self.assertIn("insufficient_total_cases", report.blocking_reasons)

    def test_special_bucket_coverage_boundary_8_passes_7_blocks(self):
        # Move 2 confusable cases to general -> 8 confusable (passes); move 3 -> 7 confusable (blocks)
        bundle = build_valid_40_case_bundle()
        raw_8 = json.loads(bundle.model_dump_json())
        for idx in (30, 31):
            raw_8["cases"][idx]["quota_bucket"] = "general"
        rep_8 = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_8)))
        self.assertEqual(rep_8.status, "eligible")
        self.assertEqual(rep_8.bucket_counts["confusable"], 8)

        raw_7 = json.loads(bundle.model_dump_json())
        for idx in (30, 31, 32):
            raw_7["cases"][idx]["quota_bucket"] = "general"
        rep_7 = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_7)))
        self.assertEqual(rep_7.status, "ineligible")
        self.assertIn("insufficient_special_bucket_coverage", rep_7.blocking_reasons)
        self.assertEqual(rep_7.bucket_counts["confusable"], 7)

    def test_multimodal_and_cross_page_multimodal_coverage_boundaries(self):
        # Reduce cross-page multimodal from 4 to 3 -> blocks on insufficient_cross_page_multimodal_cases
        bundle = build_valid_40_case_bundle()
        raw_cp3 = json.loads(bundle.model_dump_json())
        raw_cp3["cases"][10]["is_cross_page_multimodal"] = False
        raw_cp3["cases"][10]["is_multimodal"] = False
        raw_cp3["cases"][10]["multimodal_categories"] = []
        rep_cp3 = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_cp3)))
        self.assertEqual(rep_cp3.status, "ineligible")
        self.assertIn("insufficient_cross_page_multimodal_cases", rep_cp3.blocking_reasons)

        # Reduce total multimodal from 14 to 11 (while keeping 4 cross-page multimodal) -> blocks on insufficient_multimodal_cases
        raw_mm11 = json.loads(bundle.model_dump_json())
        for idx in (20, 21, 22):
            raw_mm11["cases"][idx]["quota_bucket"] = "general"
            raw_mm11["cases"][idx]["is_multimodal"] = False
            raw_mm11["cases"][idx]["multimodal_categories"] = []
        rep_mm11 = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_mm11)))
        self.assertEqual(rep_mm11.status, "ineligible")
        self.assertIn("insufficient_multimodal_cases", rep_mm11.blocking_reasons)
        self.assertEqual(rep_mm11.multimodal_cases, 11)
        self.assertEqual(rep_mm11.cross_page_multimodal_cases, 4)

    def test_missing_scenario_or_multimodal_category_blocks(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        for c in raw["cases"]:
            if "abstained" in c["scenario_categories"]:
                c["scenario_categories"] = ["wrong_answer"]
            if "wrong_axis" in c["multimodal_categories"]:
                c["multimodal_categories"] = ["wrong_value"]
        rep = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep.status, "ineligible")
        self.assertIn("missing_scenario_category_coverage", rep.blocking_reasons)
        self.assertIn("missing_multimodal_category_coverage", rep.blocking_reasons)


class CalibrationContradictionAndGoldenRulesTests(unittest.TestCase):
    def test_major_contradiction_scored_full_correct_blocks(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        # Find the first extra_contradiction case and set judge output to 1.0 / 100.0
        contra_idx = next(i for i, c in enumerate(raw["cases"]) if "extra_contradiction" in c["scenario_categories"])
        jout = raw["judge_outputs"][contra_idx]
        jout["fact_outputs"][0]["score"] = 1.0
        jout["correctness_result"]["numerator"] = 1.0
        jout["correctness_result"]["raw_value"] = 100.0
        jout["correctness_result"]["display_value"] = "100.00"
        for rr in jout["repeat_runs"]:
            rr["correctness_score"] = 100.0
            rr["fact_scores"]["f1"] = 1.0

        rep = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep.status, "ineligible")
        self.assertEqual(rep.major_contradiction_full_correct_count, 1)
        self.assertIn("major_contradiction_scored_full_correct", rep.blocking_reasons)

    def test_deterministic_source_or_rule_metric_mismatch_blocks(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        # Change recall_at_k on case 0 from 100.00 to 50.00
        for m in raw["judge_outputs"][0]["deterministic_metrics"]:
            if m["metric"] == "recall_at_k":
                m["numerator"] = 0.5
                m["denominator"] = 1.0
                m["raw_value"] = 50.0
                m["display_value"] = "50.00"
        rep = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep.status, "ineligible")
        self.assertEqual(rep.deterministic_metric_mismatches, 1)
        self.assertIn("deterministic_metric_mismatch", rep.blocking_reasons)

    def test_deterministic_metrics_all_na_or_single_metric_all_na_blocks_while_mixed_na_passes(self):
        # 1. Mixed valid N/A (odd cases have textual_consistency N/A, even cases scored) is eligible
        bundle = build_valid_40_case_bundle()
        rep_mixed = evaluate_judge_calibration(bundle)
        self.assertEqual(rep_mixed.status, "eligible")
        self.assertEqual(rep_mixed.scored_deterministic_metric_counts["textual_consistency"], 20)
        self.assertEqual(rep_mixed.scored_deterministic_metric_counts["recall_at_k"], 40)

        # 2. One metric (textual_consistency) N/A across all 40 cases -> blocks with missing_scored_deterministic_metric_coverage
        raw_one_na = json.loads(bundle.model_dump_json())
        for c, o in zip(raw_one_na["cases"], raw_one_na["judge_outputs"]):
            qid = c["question_id"]
            na_tc = json.loads(_make_metric(qid, "textual_consistency", status="not_applicable").model_dump_json())
            c["golden_deterministic_metrics"] = [
                na_tc if m["metric"] == "textual_consistency" else m for m in c["golden_deterministic_metrics"]
            ]
            o["deterministic_metrics"] = [
                na_tc if m["metric"] == "textual_consistency" else m for m in o["deterministic_metrics"]
            ]
        rep_one_na = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_one_na)))
        self.assertEqual(rep_one_na.status, "ineligible")
        self.assertIn("missing_scored_deterministic_metric_coverage", rep_one_na.blocking_reasons)
        self.assertEqual(rep_one_na.scored_deterministic_metric_counts["textual_consistency"], 0)
        self.assertEqual(
            [d.path for d in rep_one_na.diagnostics if d.code == "missing_scored_deterministic_metric_coverage"],
            ["judge_outputs.deterministic_metrics[textual_consistency]"],
        )

        # 3. All 5 deterministic metrics N/A across all 40 cases -> blocks with all 5 diagnostics
        raw_all_na = json.loads(bundle.model_dump_json())
        for c, o in zip(raw_all_na["cases"], raw_all_na["judge_outputs"]):
            qid = c["question_id"]
            na_all = [
                json.loads(_make_metric(qid, m, status="not_applicable").model_dump_json())
                for m in sorted(DETERMINISTIC_METRIC_SET)
            ]
            c["golden_deterministic_metrics"] = na_all
            o["deterministic_metrics"] = na_all
        rep_all_na = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_all_na)))
        self.assertEqual(rep_all_na.status, "ineligible")
        self.assertIn("missing_scored_deterministic_metric_coverage", rep_all_na.blocking_reasons)
        self.assertEqual(
            set(rep_all_na.scored_deterministic_metric_counts.values()),
            {0},
        )
        self.assertEqual(
            [d.path for d in rep_all_na.diagnostics if d.code == "missing_scored_deterministic_metric_coverage"],
            [f"judge_outputs.deterministic_metrics[{m}]" for m in sorted(DETERMINISTIC_METRIC_SET)],
        )

    def test_missing_pending_and_error_judge_outputs_are_preserved_as_ineligible(self):
        # 1. Missing judge output record
        bundle = build_valid_40_case_bundle()
        raw_missing = json.loads(bundle.model_dump_json())
        raw_missing["judge_outputs"].pop()
        rep_missing = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_missing)))
        self.assertEqual(rep_missing.status, "ineligible")
        self.assertEqual(rep_missing.missing_or_pending_cases, 1)
        self.assertEqual(rep_missing.evaluated_cases, 39)
        self.assertIn("missing_judge_output", rep_missing.blocking_reasons)

        # 2. Pending judge output record
        raw_pending = json.loads(bundle.model_dump_json())
        j0 = raw_pending["judge_outputs"][0]
        j0["status"] = "pending"
        j0["fact_outputs"][0] = {
            "fact_id": "f1",
            "status": "pending",
            "score": None,
            "reason_codes": ["provider_timeout"],
            "rationale": None,
        }
        j0["correctness_result"] = {
            "question_id": j0["question_id"],
            "metric": "correctness",
            "status": "pending",
            "raw_value": None,
            "display_value": None,
            "rounding": "lab-round-1",
            "numerator": None,
            "denominator": None,
            "reason_codes": ["provider_timeout"],
            "grader_revision": "grader@1",
            "support_refs": [],
        }
        rep_pending = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw_pending)))
        self.assertEqual(rep_pending.status, "ineligible")
        self.assertEqual(rep_pending.missing_or_pending_cases, 1)
        self.assertIn("judge_output_not_evaluated", rep_pending.blocking_reasons)


class CalibrationAnnotatorAdjudicationAndStabilityTests(unittest.TestCase):
    def test_duplicate_annotator_or_non_independent_adjudicator_blocks(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        # Same annotator A and B on case 1
        raw["cases"][1]["annotator_b"]["annotator_id"] = raw["cases"][1]["annotator_a"]["annotator_id"]
        # Adjudicator same as annotator A on case 0
        raw["cases"][0]["adjudication"]["adjudicator_id"] = raw["cases"][0]["annotator_a"]["annotator_id"]
        rep = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep.status, "ineligible")
        self.assertIn("duplicate_annotator_identity", rep.blocking_reasons)
        self.assertIn("adjudicator_not_independent", rep.blocking_reasons)

    def test_unadjudicated_or_pending_disagreement_blocks(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        # Case 0 has disagreement; remove adjudication
        raw["cases"][0]["adjudication"] = None
        # Case 5 has disagreement; mark adjudication pending
        raw["cases"][5]["adjudication"]["resolution_status"] = "pending"
        rep = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep.status, "ineligible")
        self.assertIn("unadjudicated_disagreement", rep.blocking_reasons)
        self.assertEqual(rep.valid_adjudicated_cases, 38)

    def test_authorize_human_identity_callback_enforced(self):
        bundle = build_valid_40_case_bundle()
        # Reject human_carol as adjudicator
        rep = evaluate_judge_calibration(
            bundle,
            authorize_human_identity=lambda actor, role, cid: actor != "human_carol",
        )
        self.assertEqual(rep.status, "ineligible")
        self.assertIn("unauthorized_human_identity", rep.blocking_reasons)

        # Callback raising exception also blocks without leaking exception text
        def boom(actor: str, role: str, cid: str) -> bool:
            raise RuntimeError("secret db connection string")

        rep_err = evaluate_judge_calibration(bundle, authorize_human_identity=boom)
        self.assertEqual(rep_err.status, "ineligible")
        self.assertIn("unauthorized_human_identity", rep_err.blocking_reasons)
        for d in rep_err.diagnostics:
            self.assertNotIn("secret db connection string", d.detail)

    def test_repeat_run_spread_over_10_requires_human_review_and_forbids_cherry_picking(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        # Case 2 has partial_rubric_defined=True (scenario partial_correct, score 0.5).
        # Make repeat_runs [50.0, 100.0, 50.0] -> spread = 50.0 > 10.0, unreviewed -> blocks
        raw["judge_outputs"][2]["repeat_runs"][1]["correctness_score"] = 100.0
        raw["judge_outputs"][2]["repeat_runs"][1]["fact_scores"]["f1"] = 1.0
        rep_unrev = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep_unrev.status, "ineligible")
        self.assertIn("repeat_run_spread_requires_review", rep_unrev.blocking_reasons)
        self.assertEqual(rep_unrev.repeat_instability_unreviewed_count, 1)

        # When reviewed by an authorized reviewer -> passes
        raw["judge_outputs"][2]["stability_human_reviewed"] = True
        raw["judge_outputs"][2]["stability_reviewer_id"] = "human_dave"
        raw["judge_outputs"][2]["stability_reviewed_at"] = TS_REV.isoformat()
        rep_rev = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep_rev.status, "eligible")

        # Cherry-picking run 2 (1.0) as the primary output when run 1 was 0.5 -> blocks
        raw["judge_outputs"][2]["fact_outputs"][0]["score"] = 1.0
        raw["judge_outputs"][2]["correctness_result"]["numerator"] = 1.0
        raw["judge_outputs"][2]["correctness_result"]["raw_value"] = 100.0
        raw["judge_outputs"][2]["correctness_result"]["display_value"] = "100.00"
        rep_cherry = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep_cherry.status, "ineligible")
        self.assertIn("first_valid_judge_output_mismatch", rep_cherry.blocking_reasons)

    def test_duplicate_ids_and_revision_mismatches_block(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        raw["cases"][1]["case_id"] = raw["cases"][0]["case_id"]
        raw["judge_outputs"][1]["case_id"] = raw["judge_outputs"][0]["case_id"]
        raw["judge_outputs"][2]["judge_prompt_version"] = "judge-prompt@v2"
        rep = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep.status, "ineligible")
        self.assertIn("duplicate_case_id", rep.blocking_reasons)
        self.assertIn("duplicate_judge_output_case_id", rep.blocking_reasons)
        self.assertIn("grader_revision_mismatch", rep.blocking_reasons)
        self.assertEqual(rep.valid_adjudicated_cases, 38)

    def test_scenario_label_and_contradiction_inconsistency_blocks(self):
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        # Case 4 has scenario wrong_answer (gold_score 0.0); tag it as full_correct without changing gold -> blocks
        raw["cases"][4]["scenario_categories"] = ["full_correct"]
        rep = evaluate_judge_calibration(CalibrationEvaluationInput.model_validate_json(json.dumps(raw)))
        self.assertEqual(rep.status, "ineligible")
        self.assertIn("scenario_label_inconsistent", rep.blocking_reasons)


class CalibrationMalformedInputAndSizeProtectionTests(unittest.TestCase):
    def test_parse_calibration_input_roundtrip_and_snapshot_hash_determinism(self):
        bundle = build_valid_40_case_bundle()
        parsed = parse_calibration_input(bundle.model_dump_json())
        self.assertEqual(snapshot_sha256(bundle), snapshot_sha256(parsed))

    def test_payload_size_limit_bom_duplicate_keys_and_invalid_json_rejected(self):
        with self.assertRaises(ValueError):
            parse_calibration_input(b"x" * (MAX_CALIBRATION_PAYLOAD_BYTES + 1))
        with self.assertRaises(StrictJSONError):
            parse_calibration_input(b"\xef\xbb\xbf{}")
        with self.assertRaises(StrictJSONError):
            parse_calibration_input('{"a": 1, "a": 2}')
        with self.assertRaises(StrictJSONError):
            parse_calibration_input('{"a": NaN}')

    def test_invalid_fact_score_and_floating_revisions_rejected_by_contract(self):
        with self.assertRaises(ValidationError):
            AnnotatorFactLabel(fact_id="f1", score=0.75, reason="invalid score")
        bundle = build_valid_40_case_bundle()
        raw = json.loads(bundle.model_dump_json())
        raw["target_grader_revision"] = "latest"
        with self.assertRaises(ValidationError):
            CalibrationEvaluationInput.model_validate_json(json.dumps(raw))


if __name__ == "__main__":
    unittest.main()
