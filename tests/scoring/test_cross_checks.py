"""Cross-checks of lab_scoring.rules against sources that do not share its
code: the F06 SPEC vectors (T37/T38/T39/T42) and a brute-force area
computation on a fine grid."""
import json
import random
import unittest
from fractions import Fraction as F
from pathlib import Path

from lab_scoring import rules as r

V = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "F06" / "vectors.json").read_text())


def frac(x):
    return F(str(x))


class F06ThroughEngineTests(unittest.TestCase):
    """The engine must reproduce every SPEC vector exactly, numerator and
    denominator included."""

    def check(self, m, exp):
        self.assertEqual(m.status, "scored")
        self.assertEqual((frac(m.numerator), frac(m.denominator)), (frac(exp["numerator"]), frac(exp["denominator"])))
        self.assertEqual(m.raw_value, exp["raw"])

    def test_evidence_vectors(self):
        for v in V["evidence_vectors"]:
            gold = [set(g) for g in v["gold_sets"]]
            matched = set(v["matched"])
            with self.subTest(v["id"]):
                self.check(r.recall_at_k("q", gold, matched, "g@1"), v["expected"]["recall_at_k"])
                self.check(r.complete_at_k("q", gold, matched, "g@1"), v["expected"]["complete_at_k"])

    def test_correctness_vectors(self):
        for v in V["correctness_vectors"]:
            facts = [(frac(f["weight"]), frac(f["score"])) for f in v["facts"]]
            m = r.correctness("q", facts, v["partial_rubric_defined"], "g@1", abstained=v.get("abstained", False))
            with self.subTest(v["id"]):
                if "error" in v["expected"]:
                    self.assertEqual((m.status, m.reason_codes, m.raw_value), ("error", [v["expected"]["error"]], None))
                else:
                    self.check(m, v["expected"]["correctness"])

    def test_textual_vectors(self):
        for v in V["textual_vectors"]:
            m = r.textual_consistency("q", v["passed_checks"], v["applicable_checks"], "g@1")
            exp = v["expected"]["textual_consistency"]
            with self.subTest(v["id"]):
                if exp.get("status") == "not_applicable":
                    self.assertEqual((m.status, m.raw_value, m.numerator), ("not_applicable", None, None))
                else:
                    self.check(m, exp)


GRID = 16  # every coordinate is a multiple of 1/GRID


def brute_area(rects, other=None):
    """Count covered 1/GRID cells by direct enumeration."""
    cells = 0
    for i in range(GRID):
        for j in range(GRID):
            cx, cy = F(2 * i + 1, 2 * GRID), F(2 * j + 1, 2 * GRID)
            inside = any(q.x0 <= cx <= q.x1 and q.y0 <= cy <= q.y1 for q in rects)
            if other is not None:
                inside = inside and any(q.x0 <= cx <= q.x1 and q.y0 <= cy <= q.y1 for q in other)
            cells += inside
    return F(cells, GRID * GRID)


def rand_rect(rng, page=0):
    x0, x1 = sorted(rng.sample(range(GRID + 1), 2))
    y0, y1 = sorted(rng.sample(range(GRID + 1), 2))
    return r.Region(page, F(x0, GRID), F(y0, GRID), F(x1, GRID), F(y1, GRID))


class GeometryBruteForceTests(unittest.TestCase):
    def test_union_and_intersection_match_brute_force(self):
        rng = random.Random(20261106)  # fixed seed: reproducible, not a fuzzer
        for case in range(150):
            a = [rand_rect(rng) for _ in range(rng.randint(1, 6))]
            b = [rand_rect(rng) for _ in range(rng.randint(1, 4))]
            with self.subTest(case=case):
                self.assertEqual(r.union_area(a), brute_area(a))
                self.assertEqual(r.intersection_area(a, b), brute_area(a, b))

    def test_localization_is_per_page_and_ignores_other_page_gold(self):
        ret = [r.Region(0, F(0), F(0), F(1, 2), F(1)), r.Region(1, F(0), F(0), F(1), F(1, 2))]
        gold = [r.Region(1, F(0), F(0), F(1), F(1, 4))]  # gold only on page 1
        m = r.localization_precision("q", ret, gold, "g@1")
        # page 0 adds 1/2 to the denominator and nothing to the numerator.
        self.assertEqual((frac(m.numerator), frac(m.denominator)), (F(1, 4), F(1)))
        self.assertEqual(m.raw_value, 25.0)

    def test_big_box_does_not_reach_unit_hit_on_another_page(self):
        gold = [r.Region(2, F(0), F(0), F(1, 2), F(1, 2))]
        self.assertFalse(r.unit_hit([r.Region(3, F(0), F(0), F(1), F(1))], gold))
        self.assertTrue(r.unit_hit([r.Region(2, F(0), F(0), F(1), F(1))], gold))

    def test_unit_needs_every_region(self):
        g1 = r.Region(0, F(0), F(0), F(1, 4), F(1, 4))
        g2 = r.Region(0, F(3, 4), F(3, 4), F(1), F(1))
        self.assertFalse(r.unit_hit([g1], [g1, g2]))
        self.assertTrue(r.unit_hit([g1, g2], [g1, g2]))


class BudgetTests(unittest.TestCase):
    def hits(self, *spec):
        return [r.Hit(rank=i + 1, evidence_tokens=t, visual_regions=v) for i, (t, v) in enumerate(spec)]

    def test_top_k_visual_and_exact_limits(self):
        res = r.apply_budget(self.hits((1, 0), (1, 0), (1, 0)), top_k=2, max_evidence_tokens=99, max_visual_regions=9)
        self.assertEqual(([h.rank for h in res.included], [h.rank for h in res.excluded], res.violation), ([1, 2], [3], True))
        res = r.apply_budget(self.hits((1, 5), (1, 3), (1, 0)), top_k=10, max_evidence_tokens=99, max_visual_regions=8)
        self.assertEqual(([h.rank for h in res.included], res.violation), ([1, 2, 3], False))  # exactly 8 fits
        res = r.apply_budget(self.hits((1, 5), (1, 4), (1, 0)), top_k=10, max_evidence_tokens=99, max_visual_regions=8)
        self.assertEqual(([h.rank for h in res.included], [h.rank for h in res.excluded]), ([1], [2, 3]))

    def test_unsorted_input_and_bad_ranks(self):
        hs = [r.Hit(2, 1, 0), r.Hit(1, 1, 0)]
        self.assertEqual([h.rank for h in r.apply_budget(hs, 10, 9, 9).included], [1, 2])
        for bad in ([r.Hit(1, 1, 0), r.Hit(3, 1, 0)], [r.Hit(1, 1, 0), r.Hit(1, 1, 0)], [r.Hit(0, 1, 0)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.apply_budget(bad, 10, 9, 9)


class AggregateTests(unittest.TestCase):
    def test_method_failure_must_be_a_real_zero_and_counts(self):
        ok = r.textual_consistency("q1", 1, 1, "g@1")
        na = r.textual_consistency("q2", 0, 0, "g@1")
        with self.assertRaises(ValueError):   # a failed question reported as N/A is hidden from the mean
            r.aggregate("textual_consistency", [ok, na], {"q2"})
        with self.assertRaises(ValueError):   # failed id absent from results
            r.aggregate("textual_consistency", [ok], {"q9"})
        with self.assertRaises(ValueError):   # duplicate question
            r.aggregate("textual_consistency", [ok, ok], set())
        with self.assertRaises(ValueError):   # mixed metrics
            r.aggregate("faithfulness", [ok], set())
        fail = r.textual_consistency("q3", 0, 2, "g@1", method_failed=True)
        agg = r.aggregate("textual_consistency", [ok, na, fail], {"q3"})
        self.assertEqual((agg.mean, agg.scored, agg.not_applicable, agg.failed_method, agg.provisional),
                         (F(50), 2, 1, 1, False))

    def test_grader_error_makes_it_provisional(self):
        ok = r.correctness("q1", [(F(1), F(1))], False, "g@1")
        err = r.correctness("q2", [(F(1), F(1, 2))], False, "g@1")
        agg = r.aggregate("correctness", [ok, err], set())
        self.assertEqual((agg.mean, agg.error, agg.provisional), (F(100), 1, True))


class AbstainAndCitationTests(unittest.TestCase):
    def test_abstained_cannot_score_on_answer_side(self):
        self.assertEqual(r.citation_completeness("q", 0, 0, "g@1", abstained=True).raw_value, 0.0)
        self.assertEqual(r.faithfulness("q", 0, 0, "g@1", abstained=True).raw_value, 0.0)
        self.assertEqual(r.citation_precision("q", [], True, "g@1").raw_value, 0.0)

    def test_region_key_order_does_not_create_new_citation(self):
        c = [("c1", frozenset({"r1", "r2"}), False), ("c1", frozenset({"r2", "r1"}), False), ("c2", frozenset({"r3"}), True)]
        m = r.citation_precision("q", c, True, "g@1")
        self.assertEqual((frac(m.numerator), frac(m.denominator)), (F(1), F(2)))
        with self.assertRaises(ValueError):
            r.citation_precision("q", c + [("c2", frozenset({"r3"}), False)], True, "g@1")

    def test_floats_are_refused(self):
        with self.assertRaises(ValueError):
            r.correctness("q", [(1.0, F(1))], False, "g@1")
        with self.assertRaises(ValueError):
            r.Region(0, 0.1, F(0), F(1), F(1))


if __name__ == "__main__":
    unittest.main()
