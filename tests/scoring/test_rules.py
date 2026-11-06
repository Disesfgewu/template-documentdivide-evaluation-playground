"""Unit tests for lab_scoring.rules (SPEC 9.1, 9.3, 16.5; T37-T40, T42, T43)."""
from __future__ import annotations

from fractions import Fraction
import unittest

from lab_contracts.score import MetricResult
from lab_scoring import rules
from lab_scoring.rules import Hit, Region


REV = "grader-rev-1"


class TestScoringRules(unittest.TestCase):
    """Exact Fraction-based scoring rule tests with numerator/denominator verification."""

    def test_t37_recall_and_complete_at_k(self) -> None:
        g1 = {"a", "b"}
        g2 = {"c"}
        gold_sets = [g1, g2]

        # M={a} -> recall 50 (num 1, den 2), complete 0 (num 0, den 1)
        r1 = rules.recall_at_k("q1", gold_sets, {"a"}, REV)
        self.assertIsInstance(r1, MetricResult)
        self.assertEqual(r1.status, "scored")
        self.assertEqual(r1.numerator, 1.0)
        self.assertEqual(r1.denominator, 2.0)
        self.assertEqual(r1.raw_value, 50.0)
        self.assertEqual(r1.display_value, "50.00")
        self.assertEqual(r1.support_refs, ["a"])

        c1 = rules.complete_at_k("q1", gold_sets, {"a"}, REV)
        self.assertEqual(c1.status, "scored")
        self.assertEqual(c1.numerator, 0.0)
        self.assertEqual(c1.denominator, 1.0)
        self.assertEqual(c1.raw_value, 0.0)
        self.assertEqual(c1.display_value, "0.00")

        # M={a,b} -> recall 100 (num 2, den 2), complete 100 (num 1, den 1)
        r2 = rules.recall_at_k("q1", gold_sets, {"a", "b"}, REV)
        self.assertEqual(r2.status, "scored")
        self.assertEqual(r2.numerator, 2.0)
        self.assertEqual(r2.denominator, 2.0)
        self.assertEqual(r2.raw_value, 100.0)
        self.assertEqual(r2.display_value, "100.00")
        self.assertEqual(r2.support_refs, ["a", "b"])

        c2 = rules.complete_at_k("q1", gold_sets, {"a", "b"}, REV)
        self.assertEqual(c2.status, "scored")
        self.assertEqual(c2.numerator, 1.0)
        self.assertEqual(c2.denominator, 1.0)
        self.assertEqual(c2.raw_value, 100.0)
        self.assertEqual(c2.display_value, "100.00")
        self.assertEqual(c2.support_refs, ["a", "b"])

        # M={c} -> recall 100 (num 1, den 1), complete 100 (num 1, den 1)
        r3 = rules.recall_at_k("q1", gold_sets, {"c"}, REV)
        self.assertEqual(r3.status, "scored")
        self.assertEqual(r3.numerator, 1.0)
        self.assertEqual(r3.denominator, 1.0)
        self.assertEqual(r3.raw_value, 100.0)
        self.assertEqual(r3.display_value, "100.00")
        self.assertEqual(r3.support_refs, ["c"])

        c3 = rules.complete_at_k("q1", gold_sets, {"c"}, REV)
        self.assertEqual(c3.status, "scored")
        self.assertEqual(c3.numerator, 1.0)
        self.assertEqual(c3.denominator, 1.0)
        self.assertEqual(c3.raw_value, 100.0)
        self.assertEqual(c3.display_value, "100.00")
        self.assertEqual(c3.support_refs, ["c"])

        # Tie-breaking: both G1 and G2 have 100%, smaller |G_j| (G2) wins
        r_tie = rules.recall_at_k("q1", gold_sets, {"a", "b", "c"}, REV)
        self.assertEqual(r_tie.numerator, 1.0)
        self.assertEqual(r_tie.denominator, 1.0)
        self.assertEqual(r_tie.support_refs, ["c"])

    def test_t38_correctness_rubric_and_partial_scores(self) -> None:
        facts = [(Fraction(1), Fraction(1)), (Fraction(1), Fraction(1, 2))]

        # With rubric -> 75 (num 1.5, den 2.0)
        res_ok = rules.correctness("q2", facts, partial_rubric_defined=True, grader_revision=REV)
        self.assertEqual(res_ok.status, "scored")
        self.assertEqual(res_ok.numerator, 1.5)
        self.assertEqual(res_ok.denominator, 2.0)
        self.assertEqual(res_ok.raw_value, 75.0)
        self.assertEqual(res_ok.display_value, "75.00")

        # Without rubric -> error partial_score_without_rubric, no numbers
        res_err = rules.correctness("q2", facts, partial_rubric_defined=False, grader_revision=REV)
        self.assertEqual(res_err.status, "error")
        self.assertIsNone(res_err.raw_value)
        self.assertIsNone(res_err.display_value)
        self.assertIsNone(res_err.numerator)
        self.assertIsNone(res_err.denominator)
        self.assertEqual(res_err.reason_codes, ["partial_score_without_rubric"])

    def test_t39_textual_consistency(self) -> None:
        # passed 3 applicable 4 -> 75 (num 3, den 4)
        res = rules.textual_consistency("q3", passed=3, applicable=4, grader_revision=REV)
        self.assertEqual(res.status, "scored")
        self.assertEqual(res.numerator, 3.0)
        self.assertEqual(res.denominator, 4.0)
        self.assertEqual(res.raw_value, 75.0)
        self.assertEqual(res.display_value, "75.00")

        # applicable 0 -> N/A
        res_na = rules.textual_consistency("q3", passed=0, applicable=0, grader_revision=REV)
        self.assertEqual(res_na.status, "not_applicable")
        self.assertIsNone(res_na.raw_value)
        self.assertIsNone(res_na.display_value)
        self.assertIsNone(res_na.numerator)
        self.assertIsNone(res_na.denominator)
        self.assertEqual(res_na.reason_codes, ["no_applicable_checks"])

        # method_failed with applicable > 0 -> scored 0/4
        res_fail = rules.textual_consistency(
            "q3", passed=3, applicable=4, grader_revision=REV, method_failed=True
        )
        self.assertEqual(res_fail.status, "scored")
        self.assertEqual(res_fail.numerator, 0.0)
        self.assertEqual(res_fail.denominator, 4.0)
        self.assertEqual(res_fail.raw_value, 0.0)
        self.assertEqual(res_fail.reason_codes, ["method_failed"])

    def test_t40_geometry_dedup_and_budget(self) -> None:
        # Overlapping rectangles [0,0,0.5,0.5] and [0.25,0.25,0.75,0.75] -> union area = 7/16
        r1 = Region(0, Fraction(0), Fraction(0), Fraction(1, 2), Fraction(1, 2))
        r2 = Region(0, Fraction(1, 4), Fraction(1, 4), Fraction(3, 4), Fraction(3, 4))
        self.assertEqual(rules.union_area([r1, r2]), Fraction(7, 16))
        self.assertEqual(rules.intersection_area([r1], [r2]), Fraction(1, 16))

        # Duplicate citation does not change precision: 1 supported + 1 unsupported + duplicate unsupported -> 50
        citations = [
            ("c1", frozenset({"reg_a"}), True),
            ("c1", frozenset({"reg_b"}), False),
            ("c1", frozenset({"reg_b"}), False),
        ]
        cit_res = rules.citation_precision("q4", citations, answerable=True, grader_revision=REV)
        self.assertEqual(cit_res.status, "scored")
        self.assertEqual(cit_res.numerator, 1.0)
        self.assertEqual(cit_res.denominator, 2.0)
        self.assertEqual(cit_res.raw_value, 50.0)
        self.assertEqual(cit_res.display_value, "50.00")

        # Budget: ranks 1..4 tokens 100,100,5000,10 with max 5000 -> included 1,2 only, violation True
        hits = [
            Hit(rank=1, evidence_tokens=100, visual_regions=1, payload="h1"),
            Hit(rank=2, evidence_tokens=100, visual_regions=1, payload="h2"),
            Hit(rank=3, evidence_tokens=5000, visual_regions=1, payload="h3"),
            Hit(rank=4, evidence_tokens=10, visual_regions=1, payload="h4"),
        ]
        budget = rules.apply_budget(hits, top_k=10, max_evidence_tokens=5000, max_visual_regions=8)
        self.assertTrue(budget.violation)
        self.assertEqual([h.rank for h in budget.included], [1, 2])
        self.assertEqual([h.rank for h in budget.excluded], [3, 4])

    def test_t42_abstained_and_evidence_scores(self) -> None:
        facts = [(Fraction(2), Fraction(1)), (Fraction(3), Fraction(1))]
        corr = rules.correctness("q5", facts, partial_rubric_defined=True, grader_revision=REV, abstained=True)
        self.assertEqual(corr.status, "scored")
        self.assertEqual(corr.numerator, 0.0)
        self.assertEqual(corr.denominator, 5.0)
        self.assertEqual(corr.raw_value, 0.0)
        self.assertEqual(corr.reason_codes, ["abstained"])

        comp = rules.citation_completeness(
            "q5", claims_total=0, claims_fully_cited=0, grader_revision=REV, abstained=True
        )
        self.assertEqual(comp.status, "scored")
        self.assertEqual(comp.numerator, 0.0)
        self.assertEqual(comp.denominator, 1.0)
        self.assertEqual(comp.raw_value, 0.0)
        self.assertEqual(comp.reason_codes, ["abstained"])

        faith = rules.faithfulness(
            "q5", claims_total=0, claims_supported=0, grader_revision=REV, abstained=True
        )
        self.assertEqual(faith.status, "scored")
        self.assertEqual(faith.numerator, 0.0)
        self.assertEqual(faith.denominator, 1.0)
        self.assertEqual(faith.raw_value, 0.0)
        self.assertEqual(faith.reason_codes, ["abstained"])

        rec = rules.recall_at_k("q5", [{"e1"}], {"e1"}, REV)
        self.assertEqual(rec.status, "scored")
        self.assertEqual(rec.numerator, 1.0)
        self.assertEqual(rec.denominator, 1.0)
        self.assertEqual(rec.raw_value, 100.0)

    def test_t43_aggregate(self) -> None:
        r_ok = rules.correctness(
            "q1", [(Fraction(1), Fraction(1))], partial_rubric_defined=False, grader_revision=REV
        )
        r_fail = rules.correctness(
            "q2",
            [(Fraction(1), Fraction(1))],
            partial_rubric_defined=False,
            grader_revision=REV,
            abstained=True,
        )
        r_na = rules._na("q3", "correctness", REV, ["not_applicable_case"])
        r_pending = MetricResult.model_validate(
            {
                "question_id": "q4",
                "metric": "correctness",
                "status": "pending",
                "raw_value": None,
                "display_value": None,
                "rounding": "lab-round-1",
                "numerator": None,
                "denominator": None,
                "reason_codes": ["grader_pending"],
                "grader_revision": REV,
                "support_refs": [],
            }
        )

        self.assertEqual(r_ok.numerator, 1.0)
        self.assertEqual(r_ok.denominator, 1.0)
        self.assertEqual(r_fail.numerator, 0.0)
        self.assertEqual(r_fail.denominator, 1.0)

        agg = rules.aggregate(
            "correctness",
            [r_ok, r_fail, r_na, r_pending],
            method_failed_question_ids={"q2"},
        )
        self.assertEqual(agg.total, 4)
        self.assertEqual(agg.scored, 2)
        self.assertEqual(agg.not_applicable, 1)
        self.assertEqual(agg.pending, 1)
        self.assertEqual(agg.error, 0)
        self.assertEqual(agg.failed_method, 1)
        self.assertTrue(agg.provisional)
        self.assertEqual(agg.mean, Fraction(50))

    def test_coverage_unit_hit_and_localization_precision(self) -> None:
        gold = Region(0, Fraction(0), Fraction(0), Fraction(1), Fraction(1, 10))
        ret_same = Region(0, Fraction(0), Fraction(0), Fraction(1), Fraction(8, 100))
        ret_other_page = Region(1, Fraction(0), Fraction(0), Fraction(1), Fraction(1))

        cov = rules.coverage([ret_same], gold)
        self.assertEqual(cov, Fraction(4, 5))
        self.assertEqual(cov.numerator, 4)
        self.assertEqual(cov.denominator, 5)
        self.assertTrue(rules.unit_hit([ret_same], [gold], threshold=Fraction(4, 5)))
        self.assertFalse(rules.unit_hit([ret_same], [gold], threshold=Fraction(9, 10)))

        cov_other = rules.coverage([ret_other_page], gold)
        self.assertEqual(cov_other, Fraction(0))
        self.assertFalse(rules.unit_hit([ret_other_page], [gold], threshold=Fraction(4, 5)))

        # Localization: returned [0,0,1,1] gold [0,0,1,1/4] -> 25 (num 0.25, den 1.0)
        loc = rules.localization_precision(
            "q6",
            returned=[Region(0, Fraction(0), Fraction(0), Fraction(1), Fraction(1))],
            gold=[Region(0, Fraction(0), Fraction(0), Fraction(1), Fraction(1, 4))],
            grader_revision=REV,
        )
        self.assertEqual(loc.status, "scored")
        self.assertEqual(loc.numerator, 0.25)
        self.assertEqual(loc.denominator, 1.0)
        self.assertEqual(loc.raw_value, 25.0)
        self.assertEqual(loc.display_value, "25.00")

        # Empty returned -> scored 0 (num 0, den 1) with no_valid_regions
        loc_empty = rules.localization_precision("q6", returned=[], gold=[gold], grader_revision=REV)
        self.assertEqual(loc_empty.status, "scored")
        self.assertEqual(loc_empty.numerator, 0.0)
        self.assertEqual(loc_empty.denominator, 1.0)
        self.assertEqual(loc_empty.raw_value, 0.0)
        self.assertEqual(loc_empty.reason_codes, ["no_valid_regions"])

    def test_invalid_inputs_raise_value_error(self) -> None:
        # Invalid region bounds
        with self.assertRaises(ValueError):
            Region(0, Fraction(1, 2), Fraction(0), Fraction(1, 2), Fraction(1))
        with self.assertRaises(ValueError):
            Region(0, Fraction(-1, 10), Fraction(0), Fraction(1, 2), Fraction(1))
        with self.assertRaises(ValueError):
            Region(-1, Fraction(0), Fraction(0), Fraction(1, 2), Fraction(1))

        # Different pages in union_area / intersection_area
        p0 = Region(0, Fraction(0), Fraction(0), Fraction(1, 2), Fraction(1, 2))
        p1 = Region(1, Fraction(0), Fraction(0), Fraction(1, 2), Fraction(1, 2))
        with self.assertRaises(ValueError):
            rules.union_area([p0, p1])
        with self.assertRaises(ValueError):
            rules.intersection_area([p0], [p1])

        # Non-contiguous or duplicate ranks in apply_budget
        with self.assertRaises(ValueError):
            rules.apply_budget([Hit(rank=2, evidence_tokens=10, visual_regions=1)], 10, 100, 8)
        with self.assertRaises(ValueError):
            rules.apply_budget(
                [Hit(rank=1, evidence_tokens=10, visual_regions=1), Hit(rank=1, evidence_tokens=10, visual_regions=1)],
                10,
                100,
                8,
            )

        # Empty gold sets
        with self.assertRaises(ValueError):
            rules.recall_at_k("q1", [], {"a"}, REV)
        with self.assertRaises(ValueError):
            rules.recall_at_k("q1", [set()], {"a"}, REV)

        # Invalid correctness score or weight
        with self.assertRaises(ValueError):
            rules.correctness("q1", [(Fraction(1), Fraction(1, 3))], True, REV)
        with self.assertRaises(ValueError):
            rules.correctness("q1", [(Fraction(0), Fraction(1))], True, REV)

        # Invalid textual_consistency bounds
        with self.assertRaises(ValueError):
            rules.textual_consistency("q1", passed=5, applicable=4, grader_revision=REV)

        # Conflicting duplicate citations
        with self.assertRaises(ValueError):
            rules.citation_precision(
                "q1",
                [("c1", frozenset({"r1"}), True), ("c1", frozenset({"r1"}), False)],
                answerable=True,
                grader_revision=REV,
            )

        # Aggregate validation errors
        r_ok = rules.correctness("q1", [(Fraction(1), Fraction(1))], False, REV)
        with self.assertRaises(ValueError):
            rules.aggregate("recall_at_k", [r_ok], set())
        with self.assertRaises(ValueError):
            rules.aggregate("correctness", [r_ok], {"q1"})


if __name__ == "__main__":
    unittest.main()
