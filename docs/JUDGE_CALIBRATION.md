# Judge Calibration Gate Evaluator (`judge-calibration-gate-1`)

`contracts/lab_contracts/calibration.py` implements the bounded, deterministic
judge calibration eligibility evaluator for SPEC sections 9.2, 16.5, 17.6, 18,
and 19.8 (Refs #12).

This module is a pure, stateless policy and contract primitive over supplied
human-adjudicated calibration case records and fixed judge output records. It
never fabricates human labels, never calls LLM providers, never claims human
annotation occurred, and never grants automatic final release or PM approval
(`final_approval_granted=False`, `human_work_verified_by_gate=False`).

## Enforced Calibration Gates

Given a `CalibrationEvaluationInput` bundle, `evaluate_judge_calibration(...)`
validates and enforces:

1. **Dataset Size & Bucket Coverage (SPEC 17.6, 19.8)**
   - At least `40` valid human-adjudicated calibration cases (`MIN_TOTAL_CASES = 40`).
   - At least `8` cases in each special quota bucket (`cross_page`, `chart_table`,
     `confusable`; `MIN_SPECIAL_BUCKET_CASES = 8`).
   - At least `12` multimodal cases (`is_multimodal=True`, `MIN_MULTIMODAL_CASES = 12`),
     which may be part of the 40 total cases, including at least `4` cross-page
     multimodal cases (`is_cross_page_multimodal=True`, `MIN_CROSS_PAGE_MULTIMODAL_CASES = 4`).
   - At least 1 case covering each required answer scenario category
     (`full_correct`, `partial_correct`, `wrong_answer`, `wrong_source`,
     `abstained`, `extra_contradiction`) and each required multimodal failure/rubric
     category (`wrong_value`, `wrong_axis`, `wrong_unit`, `wrong_condition`,
     `wrong_source`, `estimation_tolerance`).

2. **Two-Annotator Independence & Explicit Adjudication (SPEC 17.6)**
   - Every case requires two independent human assessments (`annotator_a`,
     `annotator_b`) with distinct `annotator_id` values.
   - If the two annotators disagree on any fact score (`0.0`, `0.5`, `1.0`) or
     on `major_contradiction_detected`, an explicit `AdjudicationRecord` with
     `resolution_status="resolved"` and an `adjudicator_id` distinct from both
     annotators is required to establish the gold labels. Unadjudicated or
     `pending`/`unresolved` disagreements block eligibility.
   - An optional `authorize_human_identity(actor_id, role, case_id)` callback
     lets callers verify annotator, adjudicator, and stability-reviewer IDs
     against trusted identity storage. Exceptions or non-`True` returns block
     eligibility without leaking internal error strings.
   - Partial credit (`0.5`) is rejected unless `partial_rubric_defined=True`
     on that fact (SPEC 16.2, 16.5, T38).

3. **Judge Accuracy, Contradiction & Golden Rule Gates (SPEC 9.2, 16.5, 17.6)**
   - **Fact label agreement >= 90%**: Exact rational ratio of matching fact
     labels across all evaluated cases must be $\ge 9/10$ (`90.00%`).
   - **Zero major contradictions scored fully correct**: Any fact with
     `has_major_contradiction=True` scored `1.0`, or any case with confirmed
     major contradiction scored `100%` correctness, blocks the grader revision.
   - **100% deterministic source/rule metric equality & scored coverage**: Every
     case must carry golden `MetricResult` values for the 5 deterministic metrics
     (`recall_at_k`, `complete_at_k`, `textual_consistency`,
     `localization_precision`, `citation_precision`), and the grader output must
     match their exact status, rational ratio, and `lab-round-1` display value.
     Legitimate per-case `not_applicable` is preserved, while each of the 5
     required deterministic metrics must have at least 1 genuinely `scored`
     matching comparison across valid evaluated cases (`missing_scored_deterministic_metric_coverage`
     blocks all-`not_applicable` metrics).
   - **Missing / pending / error preservation**: Missing judge outputs or
     outputs with `status` in `("pending", "error")` are never coerced to zero
     or skipped; they are counted in `missing_or_pending_cases` and block
     calibration eligibility (`judge_output_not_evaluated` / `missing_judge_output`).
   - **3x Fixed-Input Repeat Stability**: Each case requires at least 3 repeat
     runs; the primary judge output must match the first valid run (forbidding
     cherry-picking across repeats), and any case whose correctness spread
     across repeats exceeds `10` points requires `stability_human_reviewed=True`
     with a verified reviewer identity.

## Usage

```python
from lab_contracts.calibration import (
    evaluate_judge_calibration,
    parse_calibration_input,
)

bundle = parse_calibration_input(raw_json_bytes)
report = evaluate_judge_calibration(
    bundle,
    authorize_human_identity=lambda actor_id, role, case_id: True,
)
assert report.status in ("eligible", "ineligible")
assert report.final_approval_granted is False
```

## Verification and Remaining Gates

Run the deterministic unit and contract checks with:

```bash
PYTHONPATH=contracts python3 -m unittest tests.contracts.test_judge_calibration -v
scripts/check.sh --scope 'contracts fixtures governance'
```

This increment provides the deterministic calibration gate evaluator for #12.
Real 40-case human annotation and two-person adjudication datasets, live LLM
judge execution (T84), PostgreSQL persistence (#5), #10/#11 integration, and PM
review/release gates remain open.
