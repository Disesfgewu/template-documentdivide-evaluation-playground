"""Bounded, deterministic judge calibration eligibility evaluator (SPEC 17.6, 18, 19.8).

This module evaluates supplied human-labelled/adjudicated calibration cases and
fixed judge output records against SPEC calibration gates. It never fabricates
human labels, invokes model providers, claims human work occurred, or grants
automatic final approval.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import Enum
from fractions import Fraction
import math
from typing import Annotated, Any, Callable, Literal, Optional

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator

from .base import Contract, FiniteFloat, ResourceId, Sha256, check_unique
from .benchmark import QuotaBucket, SPECIAL_BUCKETS
from .method import StrictJSONError, payload_hash, strict_loads
from .score import MetricResult, ReasonCode

CALIBRATION_EVALUATOR_REVISION = "judge-calibration-gate-1"

# SPEC 17.6 & 19.8 minimum calibration dataset and accuracy thresholds.
MIN_TOTAL_CASES = 40
MIN_SPECIAL_BUCKET_CASES = 8
MIN_MULTIMODAL_CASES = 12
MIN_CROSS_PAGE_MULTIMODAL_CASES = 4
MIN_FACT_AGREEMENT_RATIO = Fraction(9, 10)  # >= 90%
MAX_REPEAT_SCORE_SPREAD = Fraction(10, 1)   # > 10 points requires human review
REQUIRED_REPEAT_RUNS = 3

# Bounded input limits so untrusted or pathological payloads cannot exhaust memory/CPU.
MAX_CALIBRATION_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_CALIBRATION_CASES = 500
MAX_FACTS_PER_CASE = 64
MAX_METRICS_PER_CASE = 8
MAX_REPEAT_RUNS_PER_CASE = 10
MAX_TEXT_CHARS = 4000

NonBlankId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"\S")]
NonBlankText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT_CHARS, pattern=r"\S")]
PositiveWeight = Annotated[FiniteFloat, Field(gt=0.0, le=1_000_000.0)]

DETERMINISTIC_METRIC_SET: frozenset[str] = frozenset({
    "recall_at_k",
    "complete_at_k",
    "textual_consistency",
    "localization_precision",
    "citation_precision",
})


class AnswerScenarioCategory(str, Enum):
    """Required answer/source calibration scenario categories (SPEC 17.6)."""

    full_correct = "full_correct"
    partial_correct = "partial_correct"
    wrong_answer = "wrong_answer"
    wrong_source = "wrong_source"
    abstained = "abstained"
    extra_contradiction = "extra_contradiction"


class MultimodalFailureCategory(str, Enum):
    """Required multimodal answer calibration coverage categories (SPEC 19.8)."""

    wrong_value = "wrong_value"
    wrong_axis = "wrong_axis"
    wrong_unit = "wrong_unit"
    wrong_condition = "wrong_condition"
    wrong_source = "wrong_source"
    estimation_tolerance = "estimation_tolerance"


def _validate_fact_label_value(v: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("fact score label must be 0.0, 0.5, or 1.0")
    if v not in (0.0, 0.5, 1.0):
        raise ValueError("fact score label must be 0.0, 0.5, or 1.0")
    return float(v)


def _format_percent(ratio: Fraction) -> str:
    with localcontext() as ctx:
        ctx.prec = 50
        d = (Decimal(ratio.numerator) * Decimal(100)) / Decimal(ratio.denominator)
        q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return "0.00" if q.is_zero() else str(q)


def _reject_floating_revision(value: str, field_name: str) -> None:
    low = value.strip().lower()
    if low in {"latest", "unknown", "main", "master", "head"} or low.endswith("@latest"):
        raise ValueError(f"{field_name} must be a pinned revision, not {value!r}")


class CalibrationFactSpec(Contract):
    """Gold fact definition for a calibration case (SPEC 16.2, 16.5)."""

    fact_id: NonBlankId
    weight: PositiveWeight
    partial_rubric_defined: bool
    has_major_contradiction: bool


class AnnotatorFactLabel(Contract):
    """Single fact label assigned by a human annotator or adjudication."""

    fact_id: NonBlankId
    score: FiniteFloat
    reason: NonBlankText

    @model_validator(mode="after")
    def _check_score(self):
        _validate_fact_label_value(self.score)
        return self


class AnnotatorAssessment(Contract):
    """Independent human annotator assessment for one calibration case."""

    annotator_id: NonBlankId
    assessed_at: AwareDatetime
    fact_labels: Annotated[list[AnnotatorFactLabel], Field(min_length=1, max_length=MAX_FACTS_PER_CASE)]
    major_contradiction_detected: bool
    notes: Optional[NonBlankText] = None

    @model_validator(mode="after")
    def _unique_facts(self):
        check_unique([f.fact_id for f in self.fact_labels], "annotator fact_id")
        return self


class AdjudicationRecord(Contract):
    """Explicit adjudication resolving annotator disagreements into gold (SPEC 17.6)."""

    adjudicator_id: NonBlankId
    adjudicated_at: AwareDatetime
    resolution_status: Literal["resolved", "pending", "unresolved"]
    rationale: NonBlankText
    adjudicated_fact_labels: Annotated[list[AnnotatorFactLabel], Field(min_length=1, max_length=MAX_FACTS_PER_CASE)]
    major_contradiction_confirmed: bool

    @model_validator(mode="after")
    def _unique_facts(self):
        check_unique([f.fact_id for f in self.adjudicated_fact_labels], "adjudicated fact_id")
        return self


class CalibrationCaseRecord(Contract):
    """Independently human-labelled and adjudicated calibration case (SPEC 17.6, 19.8)."""

    case_id: NonBlankId
    question_id: NonBlankId
    submission_id: NonBlankId
    submission_sha256: Sha256
    quota_bucket: QuotaBucket
    is_multimodal: bool
    is_cross_page_multimodal: bool
    scenario_categories: Annotated[
        list[AnswerScenarioCategory],
        Field(min_length=1, max_length=len(AnswerScenarioCategory)),
    ]
    multimodal_categories: Annotated[
        list[MultimodalFailureCategory],
        Field(max_length=len(MultimodalFailureCategory)),
    ]
    facts: Annotated[list[CalibrationFactSpec], Field(min_length=1, max_length=MAX_FACTS_PER_CASE)]
    annotator_a: AnnotatorAssessment
    annotator_b: AnnotatorAssessment
    adjudication: Optional[AdjudicationRecord] = None
    golden_deterministic_metrics: Annotated[list[MetricResult], Field(min_length=1, max_length=MAX_METRICS_PER_CASE)]

    @model_validator(mode="after")
    def _validate_structure(self):
        check_unique(self.scenario_categories, "scenario_category")
        check_unique(self.multimodal_categories, "multimodal_category")
        check_unique([f.fact_id for f in self.facts], "case fact_id")
        check_unique([m.metric for m in self.golden_deterministic_metrics], "golden metric")
        if self.is_cross_page_multimodal and not self.is_multimodal:
            raise ValueError("is_cross_page_multimodal requires is_multimodal=True")
        if self.is_cross_page_multimodal and self.quota_bucket is not QuotaBucket.cross_page:
            raise ValueError("is_cross_page_multimodal requires quota_bucket='cross_page'")
        if self.quota_bucket is QuotaBucket.chart_table and not self.is_multimodal:
            raise ValueError("chart_table calibration case must have is_multimodal=True")
        if not self.is_multimodal and self.multimodal_categories:
            raise ValueError("non-multimodal case must not declare multimodal_categories")
        for m in self.golden_deterministic_metrics:
            if m.question_id != self.question_id:
                raise ValueError(
                    f"golden metric {m.metric} question_id {m.question_id!r} != case question_id {self.question_id!r}"
                )
            if m.metric not in DETERMINISTIC_METRIC_SET:
                raise ValueError(f"golden_deterministic_metrics cannot contain non-deterministic metric {m.metric!r}")
            if m.status not in ("scored", "not_applicable"):
                raise ValueError(f"golden deterministic metric {m.metric} must be scored or not_applicable, got {m.status!r}")
        return self


class JudgeFactOutput(Contract):
    """Judge output for a single required fact."""

    fact_id: NonBlankId
    status: Literal["scored", "pending", "error"]
    score: Optional[FiniteFloat] = None
    reason_codes: Annotated[list[ReasonCode], Field(max_length=16)]
    rationale: Optional[NonBlankText] = None

    @model_validator(mode="after")
    def _validate_status(self):
        check_unique(self.reason_codes, "reason_code")
        if self.status == "scored":
            if self.score is None:
                raise ValueError("scored judge fact requires score")
            _validate_fact_label_value(self.score)
            if self.rationale is None:
                raise ValueError("scored judge fact requires rationale")
        else:
            if self.score is not None:
                raise ValueError(f"{self.status} judge fact must not carry a numeric score")
            if not self.reason_codes:
                raise ValueError(f"{self.status} judge fact requires reason_codes")
        return self


class JudgeRepeatRun(Contract):
    """One run from the 3x fixed-input stability check (SPEC 17.6)."""

    run_index: Annotated[int, Field(ge=1, le=MAX_REPEAT_RUNS_PER_CASE)]
    status: Literal["scored", "pending", "error"]
    correctness_score: Optional[Annotated[FiniteFloat, Field(ge=0.0, le=100.0)]] = None
    fact_scores: dict[NonBlankId, FiniteFloat]

    @model_validator(mode="after")
    def _validate_run(self):
        if self.status == "scored":
            if self.correctness_score is None:
                raise ValueError("scored repeat run requires correctness_score")
            if not self.fact_scores:
                raise ValueError("scored repeat run requires fact_scores")
            for fid, score in self.fact_scores.items():
                _validate_fact_label_value(score)
        else:
            if self.correctness_score is not None or self.fact_scores:
                raise ValueError(f"{self.status} repeat run must not carry scores")
        return self


class JudgeCaseOutputRecord(Contract):
    """Fixed judge output record for a calibration case."""

    case_id: NonBlankId
    question_id: NonBlankId
    submission_id: NonBlankId
    submission_sha256: Sha256
    grader_revision: NonBlankId
    judge_model_revision: NonBlankId
    judge_prompt_version: NonBlankId
    status: Literal["scored", "pending", "error"]
    fact_outputs: Annotated[list[JudgeFactOutput], Field(max_length=MAX_FACTS_PER_CASE)]
    correctness_result: MetricResult
    deterministic_metrics: Annotated[list[MetricResult], Field(max_length=MAX_METRICS_PER_CASE)]
    repeat_runs: Annotated[list[JudgeRepeatRun], Field(max_length=MAX_REPEAT_RUNS_PER_CASE)]
    stability_human_reviewed: bool = False
    stability_reviewer_id: Optional[NonBlankId] = None
    stability_reviewed_at: Optional[AwareDatetime] = None

    @model_validator(mode="after")
    def _validate_record(self):
        _reject_floating_revision(self.grader_revision, "grader_revision")
        _reject_floating_revision(self.judge_model_revision, "judge_model_revision")
        _reject_floating_revision(self.judge_prompt_version, "judge_prompt_version")
        check_unique([f.fact_id for f in self.fact_outputs], "judge fact_id")
        check_unique([m.metric for m in self.deterministic_metrics], "judge deterministic metric")
        check_unique([r.run_index for r in self.repeat_runs], "repeat_run run_index")
        if (self.stability_reviewer_id is None) != (self.stability_reviewed_at is None):
            raise ValueError("stability_reviewer_id and stability_reviewed_at must both be set or both be null")
        if self.stability_human_reviewed and self.stability_reviewer_id is None:
            raise ValueError("stability_human_reviewed=True requires stability_reviewer_id and stability_reviewed_at")
        if not self.stability_human_reviewed and self.stability_reviewer_id is not None:
            raise ValueError("stability_reviewer_id requires stability_human_reviewed=True")
        if self.correctness_result.metric != "correctness":
            raise ValueError("correctness_result.metric must be 'correctness'")
        if self.correctness_result.question_id != self.question_id:
            raise ValueError("correctness_result.question_id must match case question_id")
        if self.correctness_result.grader_revision != self.grader_revision:
            raise ValueError("correctness_result.grader_revision must match grader_revision")
        for m in self.deterministic_metrics:
            if m.metric not in DETERMINISTIC_METRIC_SET:
                raise ValueError(f"deterministic_metrics cannot contain {m.metric!r}")
            if m.question_id != self.question_id:
                raise ValueError(f"deterministic metric {m.metric} question_id mismatch")
            if m.grader_revision != self.grader_revision:
                raise ValueError(f"deterministic metric {m.metric} grader_revision mismatch")
        return self


class CalibrationEvaluationInput(Contract):
    """Complete input bundle for evaluating a grader revision's calibration gate."""

    calibration_set_id: NonBlankId
    target_grader_revision: NonBlankId
    target_judge_model_revision: NonBlankId
    target_judge_prompt_version: NonBlankId
    cases: Annotated[list[CalibrationCaseRecord], Field(min_length=1, max_length=MAX_CALIBRATION_CASES)]
    judge_outputs: Annotated[list[JudgeCaseOutputRecord], Field(max_length=MAX_CALIBRATION_CASES)]

    @model_validator(mode="after")
    def _validate_target_pins(self):
        _reject_floating_revision(self.target_grader_revision, "target_grader_revision")
        _reject_floating_revision(self.target_judge_model_revision, "target_judge_model_revision")
        _reject_floating_revision(self.target_judge_prompt_version, "target_judge_prompt_version")
        return self


@dataclass(frozen=True)
class CalibrationDiagnostic:
    path: str
    code: str
    detail: str


@dataclass(frozen=True)
class CalibrationEvaluationReport:
    evaluator_revision: str
    calibration_set_id: str
    target_grader_revision: str
    target_judge_model_revision: str
    target_judge_prompt_version: str
    input_snapshot_sha256: str
    status: Literal["eligible", "ineligible"]
    final_approval_granted: Literal[False]
    human_work_verified_by_gate: Literal[False]
    total_supplied_cases: int
    valid_adjudicated_cases: int
    evaluated_cases: int
    missing_or_pending_cases: int
    bucket_counts: dict[str, int]
    multimodal_cases: int
    cross_page_multimodal_cases: int
    scenario_counts: dict[str, int]
    multimodal_category_counts: dict[str, int]
    fact_agreement_numerator: int
    fact_agreement_denominator: int
    fact_agreement_display: Optional[str]
    major_contradiction_full_correct_count: int
    deterministic_metric_mismatches: int
    scored_deterministic_metric_counts: dict[str, int]
    repeat_instability_unreviewed_count: int
    blocking_reasons: tuple[str, ...]
    diagnostics: tuple[CalibrationDiagnostic, ...]


def _metric_matches_golden(golden: MetricResult, actual: MetricResult) -> list[str]:
    reasons: list[str] = []
    if golden.status != actual.status:
        reasons.append(f"status {actual.status!r} != golden {golden.status!r}")
        return reasons
    if golden.status == "not_applicable":
        return reasons
    # Counts are evidence too: equal ratios can hide incorrect denominators.
    assert golden.numerator is not None and golden.denominator is not None
    assert actual.numerator is not None and actual.denominator is not None
    g_num = Fraction(Decimal(repr(golden.numerator)))
    g_den = Fraction(Decimal(repr(golden.denominator)))
    a_num = Fraction(Decimal(repr(actual.numerator)))
    a_den = Fraction(Decimal(repr(actual.denominator)))
    g_ratio = g_num / g_den
    a_ratio = a_num / a_den
    if (actual.numerator, actual.denominator) != (golden.numerator, golden.denominator):
        reasons.append("numerator or denominator differs from golden")
    if g_ratio != a_ratio:
        reasons.append(
            f"ratio {actual.numerator}/{actual.denominator} != golden {golden.numerator}/{golden.denominator}"
        )
    if golden.display_value != actual.display_value:
        reasons.append(f"display_value {actual.display_value!r} != golden {golden.display_value!r}")
    return reasons


def parse_calibration_input(body: bytes | str) -> CalibrationEvaluationInput:
    """Strictly parse and validate a JSON calibration bundle with byte bounds."""
    raw_bytes = body.encode("utf-8") if isinstance(body, str) else body
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise ValueError("calibration payload must be bytes or str")
    if len(raw_bytes) > MAX_CALIBRATION_PAYLOAD_BYTES:
        raise ValueError(f"calibration payload exceeds {MAX_CALIBRATION_PAYLOAD_BYTES} bytes")
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raise StrictJSONError("UTF-8 BOM is not allowed")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJSONError("payload is not valid UTF-8") from exc
    parsed = strict_loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("calibration payload root must be a JSON object")
    return CalibrationEvaluationInput.model_validate_json(text)


def snapshot_sha256(bundle: CalibrationEvaluationInput) -> str:
    """Compute canonical SHA-256 over the revalidated calibration input bundle."""
    revalidated = CalibrationEvaluationInput.model_validate_json(bundle.model_dump_json())
    raw = strict_loads(revalidated.model_dump_json())
    raw["evaluator_revision"] = CALIBRATION_EVALUATOR_REVISION
    return payload_hash(raw)


def evaluate_judge_calibration(
    bundle: CalibrationEvaluationInput,
    authorize_human_identity: Optional[Callable[[str, Literal["annotator", "adjudicator", "stability_reviewer"], str], bool]] = None,
) -> CalibrationEvaluationReport:
    """Evaluate a calibration bundle against SPEC 17.6, 18, and 19.8 gates.

    ``authorize_human_identity(actor_id, role, case_id)`` is an optional callback
    to verify that supplied human annotator/adjudicator/reviewer IDs are authorized
    human identities in the caller's trusted store. If provided, any non-True return
    or raised exception blocks eligibility with ``unauthorized_human_identity``.
    Whether or not provided, ``human_work_verified_by_gate`` and
    ``final_approval_granted`` are always ``False``: this pure evaluator checks
    supplied records and never claims human work occurred or grants final PM/release
    approval.
    """
    if not isinstance(bundle, CalibrationEvaluationInput):
        raise TypeError("bundle must be a CalibrationEvaluationInput")
    # Revalidate defensive copy so in-place mutations or model_construct cannot bypass checks.
    bundle = CalibrationEvaluationInput.model_validate_json(bundle.model_dump_json())
    snap_hash = snapshot_sha256(bundle)

    diagnostics: list[CalibrationDiagnostic] = []
    blocking_codes: list[str] = []

    def add_diag(path: str, code: str, detail: str) -> None:
        diagnostics.append(CalibrationDiagnostic(path=path, code=code, detail=detail))
        if code not in blocking_codes:
            blocking_codes.append(code)

    def check_identity(actor_id: str, role: Literal["annotator", "adjudicator", "stability_reviewer"], case_id: str, path: str) -> bool:
        if authorize_human_identity is None:
            return True
        try:
            ok = authorize_human_identity(actor_id, role, case_id)
        except Exception:
            add_diag(path, "unauthorized_human_identity", f"{role} authorization callback failed for {actor_id!r}")
            return False
        if ok is not True:
            add_diag(path, "unauthorized_human_identity", f"{role} {actor_id!r} is not an authorized human identity")
            return False
        return True

    # 1. Check duplicate case_ids or question_ids in cases and judge_outputs.
    case_id_counts = Counter(c.case_id for c in bundle.cases)
    question_id_counts = Counter(c.question_id for c in bundle.cases)
    judge_case_id_counts = Counter(o.case_id for o in bundle.judge_outputs)
    known_case_ids = set(case_id_counts)

    for idx, case in enumerate(bundle.cases):
        cpath = f"cases[{idx}]"
        if case_id_counts[case.case_id] > 1:
            add_diag(f"{cpath}.case_id", "duplicate_case_id", f"duplicate case_id {case.case_id!r}")
        if question_id_counts[case.question_id] > 1:
            add_diag(f"{cpath}.question_id", "duplicate_question_id", f"duplicate question_id {case.question_id!r}")

    outputs_by_case_id: dict[str, JudgeCaseOutputRecord] = {}
    for idx, out in enumerate(bundle.judge_outputs):
        opath = f"judge_outputs[{idx}]"
        if judge_case_id_counts[out.case_id] > 1:
            add_diag(f"{opath}.case_id", "duplicate_judge_output_case_id", f"duplicate judge output for case_id {out.case_id!r}")
        elif out.case_id in known_case_ids:
            outputs_by_case_id[out.case_id] = out
        if out.case_id not in known_case_ids:
            add_diag(f"{opath}.case_id", "orphan_judge_output", f"judge output references unknown case_id {out.case_id!r}")

    # 2. Validate each human case (annotator independence, fact alignment, adjudication).
    valid_gold_cases: dict[str, tuple[CalibrationCaseRecord, dict[str, float], bool]] = {}
    bucket_counts: Counter[str] = Counter({b.value: 0 for b in QuotaBucket})
    scenario_counts: Counter[str] = Counter({c.value: 0 for c in AnswerScenarioCategory})
    mm_category_counts: Counter[str] = Counter({c.value: 0 for c in MultimodalFailureCategory})
    multimodal_cases = 0
    cross_page_multimodal_cases = 0

    for idx, case in enumerate(bundle.cases):
        cpath = f"cases[{case.case_id}]"
        if case_id_counts[case.case_id] > 1 or question_id_counts[case.question_id] > 1:
            continue

        case_ok = True
        expected_fact_specs = {f.fact_id: f for f in case.facts}
        expected_fact_ids = set(expected_fact_specs)

        # Annotator identity checks
        id_a = case.annotator_a.annotator_id
        id_b = case.annotator_b.annotator_id
        if id_a == id_b:
            add_diag(f"{cpath}.annotators", "duplicate_annotator_identity", f"annotator_a and annotator_b must be distinct ({id_a!r})")
            case_ok = False
        if not check_identity(id_a, "annotator", case.case_id, f"{cpath}.annotator_a.annotator_id"):
            case_ok = False
        if not check_identity(id_b, "annotator", case.case_id, f"{cpath}.annotator_b.annotator_id"):
            case_ok = False

        labels_a = {fl.fact_id: fl.score for fl in case.annotator_a.fact_labels}
        labels_b = {fl.fact_id: fl.score for fl in case.annotator_b.fact_labels}
        if set(labels_a) != expected_fact_ids:
            add_diag(f"{cpath}.annotator_a.fact_labels", "annotator_fact_mismatch", "annotator_a fact_ids do not match case facts")
            case_ok = False
        if set(labels_b) != expected_fact_ids:
            add_diag(f"{cpath}.annotator_b.fact_labels", "annotator_fact_mismatch", "annotator_b fact_ids do not match case facts")
            case_ok = False

        for aname, labels in (("annotator_a", labels_a), ("annotator_b", labels_b)):
            for fid, score in labels.items():
                fspec = expected_fact_specs.get(fid)
                if fspec is not None and score == 0.5 and not fspec.partial_rubric_defined:
                    add_diag(
                        f"{cpath}.{aname}.fact_labels[{fid}]",
                        "partial_score_without_rubric",
                        f"fact {fid!r} assigned 0.5 by {aname} without partial_rubric_defined",
                    )
                    case_ok = False

        has_fact_disagreement = (
            set(labels_a) == expected_fact_ids
            and set(labels_b) == expected_fact_ids
            and any(labels_a[fid] != labels_b[fid] for fid in expected_fact_ids)
        )
        has_contradiction_disagreement = (
            case.annotator_a.major_contradiction_detected != case.annotator_b.major_contradiction_detected
        )
        has_disagreement = has_fact_disagreement or has_contradiction_disagreement

        gold_fact_labels: dict[str, float] = {}
        gold_major_contradiction = False

        if case.adjudication is not None:
            adj = case.adjudication
            if adj.adjudicator_id in {id_a, id_b}:
                add_diag(
                    f"{cpath}.adjudication.adjudicator_id",
                    "adjudicator_not_independent",
                    f"adjudicator_id {adj.adjudicator_id!r} must be distinct from annotators",
                )
                case_ok = False
            if not check_identity(adj.adjudicator_id, "adjudicator", case.case_id, f"{cpath}.adjudication.adjudicator_id"):
                case_ok = False
            if adj.resolution_status != "resolved":
                add_diag(
                    f"{cpath}.adjudication.resolution_status",
                    "unadjudicated_disagreement",
                    f"adjudication resolution_status is {adj.resolution_status!r}",
                )
                case_ok = False
            adj_labels = {fl.fact_id: fl.score for fl in adj.adjudicated_fact_labels}
            if set(adj_labels) != expected_fact_ids:
                add_diag(
                    f"{cpath}.adjudication.adjudicated_fact_labels",
                    "adjudication_fact_mismatch",
                    "adjudicated_fact_labels fact_ids do not match case facts",
                )
                case_ok = False
            for fid, score in adj_labels.items():
                fspec = expected_fact_specs.get(fid)
                if fspec is not None and score == 0.5 and not fspec.partial_rubric_defined:
                    add_diag(
                        f"{cpath}.adjudication.adjudicated_fact_labels[{fid}]",
                        "partial_score_without_rubric",
                        f"fact {fid!r} adjudicated as 0.5 without partial_rubric_defined",
                    )
                    case_ok = False
            gold_fact_labels = adj_labels
            gold_major_contradiction = adj.major_contradiction_confirmed
        else:
            if has_disagreement:
                add_diag(
                    f"{cpath}.adjudication",
                    "unadjudicated_disagreement",
                    "annotators disagree on fact labels or major contradiction and no adjudication record is provided",
                )
                case_ok = False
            else:
                gold_fact_labels = dict(labels_a)
                gold_major_contradiction = case.annotator_a.major_contradiction_detected

        # Check gold scenario_categories consistency with gold fact labels and contradiction flags.
        any_fact_contradiction = any(f.has_major_contradiction for f in case.facts)
        if (any_fact_contradiction or AnswerScenarioCategory.extra_contradiction in case.scenario_categories) and not gold_major_contradiction:
            add_diag(
                f"{cpath}.facts",
                "contradiction_flag_inconsistent",
                "case declares fact contradiction or extra_contradiction scenario but gold major_contradiction is False",
            )
            case_ok = False
        if gold_major_contradiction and not any_fact_contradiction and AnswerScenarioCategory.extra_contradiction not in case.scenario_categories:
            add_diag(
                f"{cpath}.scenario_categories",
                "contradiction_flag_inconsistent",
                "gold major_contradiction is True but neither fact.has_major_contradiction nor extra_contradiction scenario is set",
            )
            case_ok = False
        for f in case.facts:
            if f.has_major_contradiction and gold_fact_labels.get(f.fact_id) not in (None, 0.0):
                add_diag(
                    f"{cpath}.facts[{f.fact_id}]",
                    "contradicted_fact_nonzero_gold",
                    f"fact {f.fact_id!r} has_major_contradiction=True but gold score is {gold_fact_labels.get(f.fact_id)}",
                )
                case_ok = False
        if gold_fact_labels and set(gold_fact_labels) == expected_fact_ids:
            all_gold_one = all(s == 1.0 for s in gold_fact_labels.values())
            all_gold_zero = all(s == 0.0 for s in gold_fact_labels.values())
            if AnswerScenarioCategory.full_correct in case.scenario_categories and (not all_gold_one or gold_major_contradiction):
                add_diag(
                    f"{cpath}.scenario_categories",
                    "scenario_label_inconsistent",
                    "full_correct scenario requires all gold fact labels == 1.0 and no major contradiction",
                )
                case_ok = False
            if AnswerScenarioCategory.partial_correct in case.scenario_categories and (all_gold_one or all_gold_zero):
                add_diag(
                    f"{cpath}.scenario_categories",
                    "scenario_label_inconsistent",
                    "partial_correct scenario requires a strictly partial gold outcome (not all 1.0 and not all 0.0)",
                )
                case_ok = False
            if AnswerScenarioCategory.abstained in case.scenario_categories and not all_gold_zero:
                add_diag(
                    f"{cpath}.scenario_categories",
                    "scenario_label_inconsistent",
                    "abstained scenario requires all gold fact labels == 0.0 (SPEC 16.5)",
                )
                case_ok = False
            if AnswerScenarioCategory.wrong_answer in case.scenario_categories and all_gold_one:
                add_diag(
                    f"{cpath}.scenario_categories",
                    "scenario_label_inconsistent",
                    "wrong_answer scenario cannot have all gold fact labels == 1.0",
                )
                case_ok = False

        # Check golden deterministic metrics cover all 5 required deterministic metrics.
        golden_metrics_by_name = {m.metric: m for m in case.golden_deterministic_metrics}
        if set(golden_metrics_by_name) != DETERMINISTIC_METRIC_SET:
            missing_m = sorted(DETERMINISTIC_METRIC_SET - set(golden_metrics_by_name))
            add_diag(
                f"{cpath}.golden_deterministic_metrics",
                "missing_golden_deterministic_metrics",
                f"case is missing golden deterministic metrics: {missing_m}",
            )
            case_ok = False

        if case_ok:
            valid_gold_cases[case.case_id] = (case, gold_fact_labels, gold_major_contradiction)
            bucket_counts[case.quota_bucket.value] += 1
            if case.is_multimodal:
                multimodal_cases += 1
            if case.is_cross_page_multimodal:
                cross_page_multimodal_cases += 1
            for sc in case.scenario_categories:
                scenario_counts[sc.value] += 1
            for mc in case.multimodal_categories:
                mm_category_counts[mc.value] += 1

    # 3. Check dataset size & coverage thresholds on valid adjudicated cases (SPEC 17.6 & 19.8).
    if len(valid_gold_cases) < MIN_TOTAL_CASES:
        add_diag(
            "cases",
            "insufficient_total_cases",
            f"requires >= {MIN_TOTAL_CASES} valid human-adjudicated cases, got {len(valid_gold_cases)}",
        )
    for sb in SPECIAL_BUCKETS:
        cnt = bucket_counts[sb.value]
        if cnt < MIN_SPECIAL_BUCKET_CASES:
            add_diag(
                f"cases.quota_bucket[{sb.value}]",
                "insufficient_special_bucket_coverage",
                f"special bucket {sb.value!r} requires >= {MIN_SPECIAL_BUCKET_CASES} cases, got {cnt}",
            )
    if multimodal_cases < MIN_MULTIMODAL_CASES:
        add_diag(
            "cases.multimodal",
            "insufficient_multimodal_cases",
            f"requires >= {MIN_MULTIMODAL_CASES} multimodal human-labelled cases (SPEC 19.8), got {multimodal_cases}",
        )
    if cross_page_multimodal_cases < MIN_CROSS_PAGE_MULTIMODAL_CASES:
        add_diag(
            "cases.cross_page_multimodal",
            "insufficient_cross_page_multimodal_cases",
            f"requires >= {MIN_CROSS_PAGE_MULTIMODAL_CASES} cross-page multimodal cases (SPEC 19.8), got {cross_page_multimodal_cases}",
        )
    for sc in AnswerScenarioCategory:
        if scenario_counts[sc.value] < 1:
            add_diag(
                f"cases.scenario_categories[{sc.value}]",
                "missing_scenario_category_coverage",
                f"calibration set must include at least 1 case for scenario {sc.value!r}",
            )
    for mc in MultimodalFailureCategory:
        if mm_category_counts[mc.value] < 1:
            add_diag(
                f"cases.multimodal_categories[{mc.value}]",
                "missing_multimodal_category_coverage",
                f"calibration set must include at least 1 multimodal case for category {mc.value!r}",
            )

    # 4. Evaluate judge output records against valid cases.
    evaluated_cases = 0
    missing_or_pending_cases = 0
    fact_agree_num = 0
    fact_agree_den = 0
    major_contradiction_full_correct_count = 0
    deterministic_mismatches = 0
    scored_deterministic_counts: Counter[str] = Counter({m: 0 for m in sorted(DETERMINISTIC_METRIC_SET)})
    repeat_instability_unreviewed = 0

    for case_id, (case, gold_fact_labels, gold_major_contradiction) in valid_gold_cases.items():
        opath = f"judge_outputs[{case_id}]"
        jout = outputs_by_case_id.get(case_id)
        if jout is None:
            missing_or_pending_cases += 1
            if judge_case_id_counts[case_id] <= 1:
                add_diag(opath, "missing_judge_output", f"no judge output record supplied for case {case_id!r}")
            continue

        # Verify identity & revision bindings
        if jout.question_id != case.question_id:
            add_diag(f"{opath}.question_id", "judge_case_binding_mismatch", f"question_id {jout.question_id!r} != {case.question_id!r}")
        if jout.submission_id != case.submission_id or jout.submission_sha256 != case.submission_sha256:
            add_diag(
                f"{opath}.submission",
                "judge_case_binding_mismatch",
                "submission_id or submission_sha256 does not match calibration case",
            )
        if (
            jout.grader_revision != bundle.target_grader_revision
            or jout.judge_model_revision != bundle.target_judge_model_revision
            or jout.judge_prompt_version != bundle.target_judge_prompt_version
        ):
            add_diag(
                f"{opath}.revisions",
                "grader_revision_mismatch",
                "judge output grader/model/prompt revision does not match target calibration revisions",
            )

        # Preserve missing/pending/error judge outputs as ineligible, never coerced to 0 or passed.
        if jout.status in ("pending", "error") or jout.correctness_result.status in ("pending", "error", "not_applicable"):
            missing_or_pending_cases += 1
            add_diag(
                f"{opath}.status",
                "judge_output_not_evaluated",
                f"judge output status={jout.status!r}, correctness_result.status={jout.correctness_result.status!r}",
            )
            continue

        expected_fact_specs = {f.fact_id: f for f in case.facts}
        jfacts = {f.fact_id: f for f in jout.fact_outputs}
        if set(jfacts) != set(expected_fact_specs):
            missing_or_pending_cases += 1
            add_diag(
                f"{opath}.fact_outputs",
                "judge_fact_set_mismatch",
                "judge fact_outputs do not cover the exact case facts",
            )
            continue

        if any(f.status != "scored" or f.score is None for f in jout.fact_outputs):
            missing_or_pending_cases += 1
            add_diag(
                f"{opath}.fact_outputs",
                "judge_output_not_evaluated",
                "one or more judge fact_outputs are pending or error",
            )
            continue

        # Check partial rubric rule and compute expected correctness from judge fact scores (SPEC 16.5)
        num_frac = Fraction(0)
        den_frac = Fraction(0)
        for fid, fspec in expected_fact_specs.items():
            jf = jfacts[fid]
            assert jf.score is not None
            if jf.score == 0.5 and not fspec.partial_rubric_defined:
                add_diag(
                    f"{opath}.fact_outputs[{fid}]",
                    "partial_score_without_rubric",
                    f"judge assigned 0.5 to fact {fid!r} without partial_rubric_defined",
                )
            w_frac = Fraction(Decimal(repr(fspec.weight)))
            s_frac = Fraction(Decimal(repr(jf.score)))
            num_frac += w_frac * s_frac
            den_frac += w_frac

        # Verify correctness_result matches the judge's own fact scores (including numerator and denominator)
        assert jout.correctness_result.numerator is not None and jout.correctness_result.denominator is not None
        actual_num = Fraction(Decimal(repr(jout.correctness_result.numerator)))
        actual_den = Fraction(Decimal(repr(jout.correctness_result.denominator)))
        expected_num_float = float(Decimal(num_frac.numerator) / Decimal(num_frac.denominator))
        expected_den_float = float(Decimal(den_frac.numerator) / Decimal(den_frac.denominator))
        if (
            actual_den == 0
            or not math.isclose(jout.correctness_result.numerator, expected_num_float, rel_tol=1e-12, abs_tol=0.0)
            or not math.isclose(jout.correctness_result.denominator, expected_den_float, rel_tol=1e-12, abs_tol=0.0)
            or not math.isclose(float(actual_num / actual_den), float(num_frac / den_frac), rel_tol=1e-12, abs_tol=0.0)
        ):
            add_diag(
                f"{opath}.correctness_result",
                "judge_correctness_inconsistent_with_facts",
                "correctness_result numerator, denominator, or ratio does not match weighted sum of judge fact_outputs",
            )

        # Fact label agreement calculation
        for fid, gold_score in gold_fact_labels.items():
            fact_agree_den += 1
            if jfacts[fid].score == gold_score:
                fact_agree_num += 1

        # Major contradiction scored fully correct check (SPEC 9.2, 17.6):
        # Neither a contradicted fact nor a case with confirmed major contradiction may be scored 1.0 / 100.0.
        contradiction_scored_full = False
        for fid, fspec in expected_fact_specs.items():
            if fspec.has_major_contradiction and jfacts[fid].score == 1.0:
                contradiction_scored_full = True
                add_diag(
                    f"{opath}.fact_outputs[{fid}]",
                    "major_contradiction_scored_full_correct",
                    f"fact {fid!r} has a major contradiction but judge scored it 1.0",
                )
        if gold_major_contradiction and (
            num_frac == den_frac or all(jfacts[fid].score == 1.0 for fid in expected_fact_specs)
        ):
            contradiction_scored_full = True
            add_diag(
                f"{opath}.correctness_result",
                "major_contradiction_scored_full_correct",
                "case has confirmed major contradiction but judge scored full correctness (100%)",
            )
        if contradiction_scored_full:
            major_contradiction_full_correct_count += 1

        # Deterministic source/rule metric equality check (SPEC 17.6: 100% match with golden)
        golden_metrics = {m.metric: m for m in case.golden_deterministic_metrics}
        actual_metrics = {m.metric: m for m in jout.deterministic_metrics}
        if set(actual_metrics) != DETERMINISTIC_METRIC_SET:
            deterministic_mismatches += 1
            add_diag(
                f"{opath}.deterministic_metrics",
                "deterministic_metric_mismatch",
                f"judge deterministic_metrics missing required metrics: {sorted(DETERMINISTIC_METRIC_SET - set(actual_metrics))}",
            )
        else:
            for mname in sorted(DETERMINISTIC_METRIC_SET):
                g_m = golden_metrics[mname]
                a_m = actual_metrics[mname]
                if a_m.status in ("pending", "error"):
                    deterministic_mismatches += 1
                    add_diag(
                        f"{opath}.deterministic_metrics[{mname}]",
                        "judge_output_not_evaluated",
                        f"deterministic metric {mname} has status {a_m.status!r}",
                    )
                    continue
                diffs = _metric_matches_golden(g_m, a_m)
                if diffs:
                    deterministic_mismatches += 1
                    add_diag(
                        f"{opath}.deterministic_metrics[{mname}]",
                        "deterministic_metric_mismatch",
                        f"metric {mname} mismatch: {'; '.join(diffs)}",
                    )
                elif g_m.status == "scored" and a_m.status == "scored":
                    scored_deterministic_counts[mname] += 1

        # 3x Repeat runs stability check (SPEC 17.6):
        # Fixed judge input re-run 3 times; first valid policy-compliant run is the primary output;
        # spread > 10 points requires human review.
        if len(jout.repeat_runs) < REQUIRED_REPEAT_RUNS:
            add_diag(
                f"{opath}.repeat_runs",
                "insufficient_repeat_runs",
                f"requires at least {REQUIRED_REPEAT_RUNS} repeat runs, got {len(jout.repeat_runs)}",
            )
        else:
            expected_indices = list(range(1, len(jout.repeat_runs) + 1))
            actual_indices = [r.run_index for r in sorted(jout.repeat_runs, key=lambda x: x.run_index)]
            if actual_indices != expected_indices:
                add_diag(
                    f"{opath}.repeat_runs",
                    "non_contiguous_repeat_runs",
                    f"repeat_runs indices must be 1..{len(jout.repeat_runs)}, got {actual_indices}",
                )
            scored_runs = [r for r in sorted(jout.repeat_runs, key=lambda x: x.run_index) if r.status == "scored"]
            if len(scored_runs) < REQUIRED_REPEAT_RUNS:
                add_diag(
                    f"{opath}.repeat_runs",
                    "judge_output_not_evaluated",
                    f"requires at least {REQUIRED_REPEAT_RUNS} scored repeat runs, got {len(scored_runs)}",
                )
            else:
                # Primary output must match the first valid run (no cherry-picking highest score, SPEC 17.6).
                first_run = scored_runs[0]
                if dict(first_run.fact_scores) != {fid: jfacts[fid].score for fid in expected_fact_specs}:
                    add_diag(
                        f"{opath}.repeat_runs[1]",
                        "first_valid_judge_output_mismatch",
                        "primary judge fact_outputs must match the first valid repeat run (no cherry-picking)",
                    )
                scores_frac = []
                for r in scored_runs:
                    assert r.correctness_score is not None
                    if set(r.fact_scores) != set(expected_fact_specs):
                        add_diag(
                            f"{opath}.repeat_runs[{r.run_index}].fact_scores",
                            "repeat_run_fact_mismatch",
                            f"repeat run {r.run_index} fact_ids do not match case facts",
                        )
                        continue
                    r_num = sum(
                        Fraction(Decimal(repr(expected_fact_specs[fid].weight)))
                        * Fraction(Decimal(repr(r.fact_scores[fid])))
                        for fid in expected_fact_specs
                    )
                    r_expected = (r_num * 100) / den_frac
                    r_actual = Fraction(Decimal(repr(r.correctness_score)))
                    if abs(r_actual - r_expected) > Fraction(1, 1_000_000):
                        add_diag(
                            f"{opath}.repeat_runs[{r.run_index}].correctness_score",
                            "repeat_run_score_inconsistent",
                            f"repeat run {r.run_index} correctness_score does not match its weighted fact_scores",
                        )
                    scores_frac.append(r_expected)
                if scores_frac:
                    spread = max(scores_frac) - min(scores_frac)
                    if spread > MAX_REPEAT_SCORE_SPREAD:
                        if not jout.stability_human_reviewed or jout.stability_reviewer_id is None:
                            repeat_instability_unreviewed += 1
                            add_diag(
                                f"{opath}.repeat_runs",
                                "repeat_run_spread_requires_review",
                                f"repeat run correctness spread is {float(spread):.2f} (> 10.0) and stability_human_reviewed is False",
                            )
                        else:
                            if not check_identity(
                                jout.stability_reviewer_id,
                                "stability_reviewer",
                                case.case_id,
                                f"{opath}.stability_reviewer_id",
                            ):
                                repeat_instability_unreviewed += 1

        evaluated_cases += 1

    # 5. Deterministic metric scored coverage gate (each required metric needs >= 1 scored matching comparison).
    for mname in sorted(DETERMINISTIC_METRIC_SET):
        if scored_deterministic_counts[mname] < 1:
            add_diag(
                f"judge_outputs.deterministic_metrics[{mname}]",
                "missing_scored_deterministic_metric_coverage",
                f"deterministic metric {mname!r} has 0 scored matching comparisons across valid evaluated cases (requires >= 1)",
            )

    # 6. Fact label agreement gate (>= 90%).
    fact_agreement_display: Optional[str] = None
    if fact_agree_den > 0:
        agree_ratio = Fraction(fact_agree_num, fact_agree_den)
        fact_agreement_display = _format_percent(agree_ratio)
        if agree_ratio < MIN_FACT_AGREEMENT_RATIO:
            add_diag(
                "judge_outputs.fact_agreement",
                "fact_agreement_below_threshold",
                f"fact label agreement is {fact_agree_num}/{fact_agree_den} ({fact_agreement_display}%), requires >= 90.00%",
            )
    else:
        add_diag(
            "judge_outputs.fact_agreement",
            "no_evaluated_facts",
            "no evaluated facts available to compute fact label agreement",
        )

    status: Literal["eligible", "ineligible"] = "eligible" if not blocking_codes else "ineligible"

    return CalibrationEvaluationReport(
        evaluator_revision=CALIBRATION_EVALUATOR_REVISION,
        calibration_set_id=bundle.calibration_set_id,
        target_grader_revision=bundle.target_grader_revision,
        target_judge_model_revision=bundle.target_judge_model_revision,
        target_judge_prompt_version=bundle.target_judge_prompt_version,
        input_snapshot_sha256=snap_hash,
        status=status,
        final_approval_granted=False,
        human_work_verified_by_gate=False,
        total_supplied_cases=len(bundle.cases),
        valid_adjudicated_cases=len(valid_gold_cases),
        evaluated_cases=evaluated_cases,
        missing_or_pending_cases=missing_or_pending_cases,
        bucket_counts=dict(bucket_counts),
        multimodal_cases=multimodal_cases,
        cross_page_multimodal_cases=cross_page_multimodal_cases,
        scenario_counts=dict(scenario_counts),
        multimodal_category_counts=dict(mm_category_counts),
        fact_agreement_numerator=fact_agree_num,
        fact_agreement_denominator=fact_agree_den,
        fact_agreement_display=fact_agreement_display,
        major_contradiction_full_correct_count=major_contradiction_full_correct_count,
        deterministic_metric_mismatches=deterministic_mismatches,
        scored_deterministic_metric_counts=dict(scored_deterministic_counts),
        repeat_instability_unreviewed_count=repeat_instability_unreviewed,
        blocking_reasons=tuple(blocking_codes),
        diagnostics=tuple(diagnostics),
    )
