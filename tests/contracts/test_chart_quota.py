"""Visual / cross-page necessity gate and chart-quota coverage (SPEC 19.5, T17, T20)."""
import copy
import json
from pathlib import Path
import unittest

from lab_contracts.benchmark import EvidenceUnit, Question, SourceAnswer
from lab_contracts.chart_quota import (
    AblationResult, chart_coverage, decide_necessity, required_ablations,
)

RAW = json.loads((Path(__file__).resolve().parents[2] / "contracts/examples/bundles/minimal.json").read_text())


def evidence_unit(eid, page, kind):
    record = copy.deepcopy(next(e for e in RAW["evidence"] if e["evidence_id"] == "e18"))
    record["evidence_id"] = eid
    record["regions"][0]["page_index"] = page
    record["regions"][0]["kind"] = kind
    if kind == "page":
        record["regions"][0]["bbox"] = [0, 0, 1, 1]  # the contract requires the whole page
    return eid, EvidenceUnit.model_validate_json(json.dumps(record))


def make(qid, bucket, reasoning, units, modality=("text", "table")):
    """units: [(evidence_id, page, region kind)] all in one evidence set."""
    evidence = dict(evidence_unit(*u) for u in units)
    q = copy.deepcopy(next(r for r in RAW["questions"] if r["question_id"] == "q_002"))
    q.update(question_id=qid, quota_bucket=bucket, reasoning_type=reasoning, modality_requirements=list(modality))
    s = copy.deepcopy(next(r for r in RAW["source_answers"] if r["question_id"] == "q_002"))
    s.update(question_id=qid, validation_refs=[], fact_support_sets=[])
    s["evidence_sets"] = [{"set_id": "s1", "evidence_ids": [u[0] for u in units]}]
    return (Question.model_validate_json(json.dumps(q)), SourceAnswer.model_validate_json(json.dumps(s)), evidence)


def res(qid, removed, answerable, independent=True, verifier="v1"):
    return AblationResult(qid, frozenset(removed), answerable, verifier, independent)


CHART = [("fig1", 3, "figure"), ("txt1", 3, "text")]


class RequiredAblationTests(unittest.TestCase):
    def test_chart_question_requires_removing_all_visual_evidence(self):
        q, s, ev = make("q_c", "chart_table", "value_lookup", CHART + [("tab1", 4, "table")])
        (a,) = required_ablations(q, s, ev)
        self.assertEqual((a.kind, a.removed), ("visual", frozenset({"fig1", "tab1"})))

    def test_cross_page_requires_one_removal_per_page(self):
        q, s, ev = make("q_x", "cross_page", "cross_page_integration",
                        [("a", 1, "text"), ("b", 2, "text"), ("c", 2, "table")])
        got = {a.kind: a.removed for a in required_ablations(q, s, ev)}
        self.assertEqual(got, {"page:1": frozenset({"a"}), "page:2": frozenset({"b", "c"})})

    def test_image_text_integration_requires_visual_and_text_removals(self):
        q, s, ev = make("q_i", "general", "image_text_integration", CHART + [("txt2", 5, "text")])
        got = {a.kind: a.removed for a in required_ablations(q, s, ev)}
        self.assertEqual(got, {"visual": frozenset({"fig1"}), "text": frozenset({"txt1", "txt2"})})

    def test_cross_page_visual_chart_question_needs_visual_and_every_page(self):
        q, s, ev = make("q_cv", "chart_table", "cross_page_integration", [("f1", 1, "figure"), ("t2", 2, "text")])
        self.assertEqual(sorted(a.kind for a in required_ablations(q, s, ev)), ["page:1", "page:2", "visual"])

    def test_questions_outside_the_gate_have_no_required_removal(self):
        for bucket, reasoning in (("general", "value_lookup"), ("confusable", "comparison")):
            q, s, ev = make("q_g", bucket, reasoning, CHART)
            self.assertEqual(required_ablations(q, s, ev), ())

    def test_mismatched_source_and_unknown_evidence_are_refused(self):
        q, s, ev = make("q_c", "chart_table", "value_lookup", CHART)
        other = s.model_copy(update={"question_id": "q_other"})
        with self.assertRaisesRegex(ValueError, "source_answer_for_other_question"):
            required_ablations(q, other, ev)
        with self.assertRaisesRegex(ValueError, "unknown_evidence"):
            required_ablations(q, s, {})


class ChartDecisionTests(unittest.TestCase):
    def setUp(self):
        self.q, self.s, self.ev = make("q_c", "chart_table", "value_lookup", CHART)

    def decide(self, results):
        return decide_necessity(self.q, self.s, self.ev, results)

    def test_unanswerable_without_the_figure_is_necessary(self):
        d = self.decide([res("q_c", {"fig1"}, False)])
        self.assertEqual((d.verdict, d.passes_gate, d.reclassify_to), ("necessary", True, None))

    def test_still_answerable_without_the_figure_is_not_counted_and_reclassified(self):
        d = self.decide([res("q_c", {"fig1"}, True)])
        self.assertEqual(d.verdict, "not_necessary")
        self.assertFalse(d.passes_gate)
        self.assertEqual(d.reclassify_to, "general")
        self.assertIn("answerable_without:visual", d.reasons)

    def test_no_result_is_review_not_a_pass(self):
        d = self.decide([])
        self.assertEqual((d.verdict, d.passes_gate), ("needs_review", False))
        self.assertIn("missing_ablation:visual", d.reasons)

    def test_self_review_is_not_independent_evidence(self):
        d = self.decide([res("q_c", {"fig1"}, False, independent=False)])
        self.assertEqual(d.verdict, "needs_review")
        self.assertIn("self_review_ignored", d.reasons)
        # and a self-review cannot rescue or sink an independent result either
        d = self.decide([res("q_c", {"fig1"}, False, independent=False), res("q_c", {"fig1"}, False)])
        self.assertEqual(d.verdict, "necessary")

    def test_conflicting_independent_results_are_kept_as_review(self):
        d = self.decide([res("q_c", {"fig1"}, False, verifier="a"), res("q_c", {"fig1"}, True, verifier="b")])
        self.assertEqual(d.verdict, "needs_review")
        self.assertIn("conflicting_ablation:visual", d.reasons)

    def test_a_result_for_a_different_removal_or_question_does_not_count(self):
        d = self.decide([res("q_c", {"txt1"}, False), res("q_other", {"fig1"}, False)])
        self.assertEqual(d.verdict, "needs_review")

    def test_a_chart_question_without_visual_evidence_is_not_necessary(self):
        q, s, ev = make("q_t", "chart_table", "value_lookup", [("txt1", 3, "text")])
        d = decide_necessity(q, s, ev, [])
        self.assertEqual((d.verdict, d.reclassify_to), ("not_necessary", "general"))
        self.assertIn("nothing_to_remove:visual", d.reasons)

    def test_caption_only_page_kind_evidence_is_not_treated_as_visual(self):
        q, s, ev = make("q_p", "chart_table", "value_lookup", [("pg", 3, "page")])
        self.assertEqual(decide_necessity(q, s, ev, []).verdict, "not_necessary")

    def test_not_gated_questions_pass_without_results(self):
        q, s, ev = make("q_g", "general", "value_lookup", CHART)
        d = decide_necessity(q, s, ev, [])
        self.assertEqual((d.verdict, d.passes_gate), ("not_required", True))


class CrossPageDecisionTests(unittest.TestCase):
    def setUp(self):
        self.q, self.s, self.ev = make("q_x", "cross_page", "cross_page_integration",
                                       [("a", 1, "text"), ("b", 2, "text")])

    def decide(self, results):
        return decide_necessity(self.q, self.s, self.ev, results)

    def test_every_page_necessary(self):
        self.assertEqual(self.decide([res("q_x", {"a"}, False), res("q_x", {"b"}, False)]).verdict, "necessary")

    def test_one_droppable_page_disqualifies(self):
        d = self.decide([res("q_x", {"a"}, False), res("q_x", {"b"}, True)])
        self.assertEqual((d.verdict, d.reclassify_to), ("not_necessary", "general"))
        self.assertIn("answerable_without:page:2", d.reasons)

    def test_a_missing_page_check_is_review(self):
        d = self.decide([res("q_x", {"a"}, False)])
        self.assertEqual(d.verdict, "needs_review")
        self.assertIn("missing_ablation:page:2", d.reasons)

    def test_answerable_beats_missing(self):
        self.assertEqual(self.decide([res("q_x", {"a"}, True)]).verdict, "not_necessary")

    def test_single_page_evidence_is_not_cross_page(self):
        q, s, ev = make("q_x", "cross_page", "cross_page_integration", [("a", 1, "text"), ("b", 1, "table")])
        d = decide_necessity(q, s, ev, [res("q_x", {"a", "b"}, False)])
        self.assertEqual(d.verdict, "not_necessary")
        self.assertIn("single_page_evidence", d.reasons)


class SinglePageAndFullRemovalTests(unittest.TestCase):
    def test_chart_question_with_cross_page_reasoning_on_one_page_is_not_necessary(self):
        q, s, ev = make("q_x", "chart_table", "cross_page_integration",
                        [("f", 1, "figure"), ("t", 1, "text")])
        d = decide_necessity(q, s, ev, [res("q_x", {"f"}, False), res("q_x", {"f", "t"}, False)])
        self.assertEqual((d.verdict, d.reclassify_to), ("not_necessary", "general"))
        self.assertIn("single_page_evidence", d.reasons)
        report = chart_coverage(1, [q], {"q_x": d}, material_sufficient=True)
        self.assertFalse(report.has_cross_page_visual)

    def test_chart_question_with_cross_page_reasoning_on_two_pages_is_still_necessary(self):
        q, s, ev = make("q_x", "chart_table", "cross_page_integration",
                        [("f", 1, "figure"), ("t", 2, "text")])
        d = decide_necessity(q, s, ev, [res("q_x", {"f"}, False), res("q_x", {"t"}, False)])
        self.assertEqual(d.verdict, "necessary")

    def test_removal_of_every_cited_unit_is_not_a_pass(self):
        q, s, ev = make("q_x", "chart_table", "value_lookup", [("f", 1, "figure")])
        d = decide_necessity(q, s, ev, [res("q_x", {"f"}, False)])
        self.assertEqual(d.verdict, "needs_review")
        self.assertIn("removes_all_evidence:visual", d.reasons)

    def test_removal_leaving_a_unit_is_unchanged(self):
        q, s, ev = make("q_x", "chart_table", "value_lookup", [("f", 1, "figure"), ("t", 1, "text")])
        d = decide_necessity(q, s, ev, [res("q_x", {"f"}, False)])
        self.assertEqual(d.verdict, "necessary")


class IntegrationDecisionTests(unittest.TestCase):
    def test_both_removals_must_break_an_image_text_question(self):
        q, s, ev = make("q_i", "chart_table", "image_text_integration", CHART)
        both = [res("q_i", {"fig1"}, False), res("q_i", {"txt1"}, False)]
        self.assertEqual(decide_necessity(q, s, ev, both).verdict, "necessary")
        text_enough = [res("q_i", {"fig1"}, False), res("q_i", {"txt1"}, True)]
        d = decide_necessity(q, s, ev, text_enough)
        self.assertEqual(d.verdict, "not_necessary")
        self.assertIn("answerable_without:text", d.reasons)
        self.assertEqual(decide_necessity(q, s, ev, both[:1]).verdict, "needs_review")


def decided(spec):
    """spec: [(qid, reasoning, verdict)] chart_table questions."""
    questions, decisions = [], {}
    for qid, reasoning, verdict in spec:
        q, s, ev = make(qid, "chart_table", reasoning, CHART)
        questions.append(q)
        decisions[qid] = decide_necessity(q, s, ev, [res(qid, {"fig1"}, verdict != "necessary")]) \
            if verdict in ("necessary", "not_necessary") else decide_necessity(q, s, ev, [])
    return questions, decisions


class CoverageTests(unittest.TestCase):
    def test_three_distinct_types_with_integration_and_cross_page_is_compliant(self):
        # integration and cross-page need their own removals, so build those decisions explicitly
        qa, sa, ea = make("q_a", "chart_table", "value_lookup", CHART)
        qb, sb, eb = make("q_b", "chart_table", "image_text_integration", CHART)
        qc, sc, ec = make("q_c", "chart_table", "cross_page_integration", [("f1", 1, "figure"), ("t2", 2, "text")])
        decisions = {
            "q_a": decide_necessity(qa, sa, ea, [res("q_a", {"fig1"}, False)]),
            "q_b": decide_necessity(qb, sb, eb, [res("q_b", {"fig1"}, False), res("q_b", {"txt1"}, False)]),
            "q_c": decide_necessity(qc, sc, ec, [res("q_c", {"f1"}, False), res("q_c", {"f1"}, False),
                                                 res("q_c", {"t2"}, False)]),
        }
        # q_c needs its visual removal {f1} plus the page removals {f1} (page 1) and {t2} (page 2)
        self.assertEqual({d.verdict for d in decisions.values()}, {"necessary"})
        r = chart_coverage(3, [qa, qb, qc], decisions, material_sufficient=True)
        self.assertTrue(r.compliant)
        self.assertEqual(r.public_gaps, ())
        self.assertEqual((r.qualifying, r.shortfall, r.required_distinct_types), (3, 0, 3))
        self.assertEqual(dict(r.reasoning_types), {"value_lookup": 1, "image_text_integration": 1, "cross_page_integration": 1})

    def test_too_few_distinct_reasoning_types_is_a_gap(self):
        qs, ds = decided([("q1", "value_lookup", "necessary"), ("q2", "value_lookup", "necessary"),
                          ("q3", "value_lookup", "necessary")])
        r = chart_coverage(3, qs, ds, material_sufficient=False)
        self.assertFalse(r.compliant)
        self.assertIn("reasoning_type_diversity", r.public_gaps)

    def test_small_quota_needs_min_3_q_types(self):
        qs, ds = decided([("q1", "value_lookup", "necessary"), ("q2", "trend", "necessary")])
        r = chart_coverage(2, qs, ds, material_sufficient=False)
        self.assertEqual(r.required_distinct_types, 2)
        self.assertTrue(r.compliant)
        qs, ds = decided([("q1", "value_lookup", "necessary")])
        r = chart_coverage(1, qs, ds, material_sufficient=False)
        self.assertEqual(r.required_distinct_types, 1)
        self.assertTrue(r.compliant)

    def test_only_necessary_chart_questions_count(self):
        qs, ds = decided([("q1", "value_lookup", "necessary"), ("q2", "trend", "not_necessary"),
                          ("q3", "comparison", "needs_review")])
        r = chart_coverage(3, qs, ds, material_sufficient=False)
        self.assertEqual((r.qualifying, r.shortfall), (1, 2))
        self.assertIn("chart_quota_shortfall", r.public_gaps)
        self.assertFalse(r.compliant)

    def test_shortfall_alone_makes_the_chart_quota_non_compliant(self):
        # three necessary questions of three types satisfy diversity, but the quota is four
        qs, ds = decided([("q1", "value_lookup", "necessary"), ("q2", "trend", "necessary"),
                          ("q3", "comparison", "necessary")])
        r = chart_coverage(4, qs, ds, material_sufficient=False)
        self.assertEqual((r.shortfall, r.required_distinct_types), (1, 3))
        self.assertNotIn("reasoning_type_diversity", r.public_gaps)
        self.assertIn("chart_quota_shortfall", r.public_gaps)
        self.assertFalse(r.compliant)

    def test_a_question_without_a_decision_does_not_count(self):
        qs, _ = decided([("q1", "value_lookup", "necessary")])
        self.assertEqual(chart_coverage(1, qs, {}, material_sufficient=False).qualifying, 0)

    def test_other_buckets_do_not_count_toward_the_chart_quota(self):
        q, s, ev = make("q_g", "general", "value_lookup", CHART)
        d = {"q_g": decide_necessity(q, s, ev, [])}
        self.assertEqual(chart_coverage(1, [q], d, material_sufficient=False).qualifying, 0)

    def test_missing_integration_is_a_violation_only_with_enough_material(self):
        qs, ds = decided([("q1", "value_lookup", "necessary"), ("q2", "trend", "necessary"),
                          ("q3", "comparison", "necessary")])
        enough = chart_coverage(3, qs, ds, material_sufficient=True)
        self.assertFalse(enough.compliant)
        self.assertIn("image_text_integration_missing", enough.public_gaps)
        self.assertIn("cross_page_visual_missing", enough.public_gaps)
        scarce = chart_coverage(3, qs, ds, material_sufficient=False)
        self.assertTrue(scarce.compliant)  # reported publicly, not forced
        self.assertIn("image_text_integration_missing", scarce.public_gaps)

    def test_invalid_quota(self):
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                chart_coverage(bad, [], {}, material_sufficient=True)


if __name__ == "__main__":
    unittest.main()
