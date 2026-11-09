"""F06 vectors are internally consistent: recomputed here from the SPEC 16.5
formulas, and every expected value is representable as a MetricResult."""
import json
import unittest

from lab_contracts.score import MetricResult, display_ratio

from pathlib import Path

V = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "F06" / "vectors.json").read_text())


def metric(name, exp):
    if exp.get("status") == "not_applicable":
        body = dict(status="not_applicable", raw_value=None, display_value=None,
                    numerator=None, denominator=None, reason_codes=["no_applicable_checks"])
    else:
        body = dict(status="scored", raw_value=float(exp["raw"]),
                    display_value=display_ratio(float(exp["numerator"]), float(exp["denominator"])),
                    numerator=float(exp["numerator"]), denominator=float(exp["denominator"]), reason_codes=[])
    return MetricResult.model_validate_json(json.dumps(dict(
        question_id="q", metric=name, grader_revision="spec", support_refs=[], rounding="lab-round-1", **body)))


class F06VectorTests(unittest.TestCase):
    def test_evidence_vectors_follow_16_5(self):
        for v in V["evidence_vectors"]:
            m = set(v["matched"])
            # Recall@k = 100 * max_j |M ∩ G_j| / |G_j|; Complete@k = 100 iff some G_j ⊆ M.
            best = max(v["gold_sets"], key=lambda g: (len(m & set(g)) / len(set(g)), -len(g)))
            recall = v["expected"]["recall_at_k"]
            with self.subTest(v["id"]):
                self.assertEqual(recall["numerator"] / recall["denominator"], len(m & set(best)) / len(set(best)))
                self.assertEqual(recall["raw"], 100.0 * recall["numerator"] / recall["denominator"])
                complete = any(set(g) <= m for g in v["gold_sets"])
                self.assertEqual(v["expected"]["complete_at_k"]["raw"], 100.0 if complete else 0.0)
                for name, exp in v["expected"].items():
                    metric(name, exp)

    def test_correctness_vectors_follow_16_5(self):
        for v in V["correctness_vectors"]:
            with self.subTest(v["id"]):
                partial = any(f["score"] == 0.5 for f in v["facts"])
                if partial and not v["partial_rubric_defined"]:
                    self.assertEqual(v["expected"], {"error": "partial_score_without_rubric"})
                    continue
                num = sum(f["weight"] * f["score"] for f in v["facts"])
                den = sum(f["weight"] for f in v["facts"])
                exp = v["expected"]["correctness"]
                self.assertEqual((exp["numerator"], exp["denominator"]), (num, den))
                self.assertEqual(exp["raw"], 100.0 * num / den)
                metric("correctness", exp)

    def test_spec_t38_value(self):
        exp = next(v for v in V["correctness_vectors"] if v["id"] == "T38.weighted_half")
        self.assertEqual(exp["expected"]["correctness"]["raw"], 75.0)

    def test_textual_vectors(self):
        for v in V["textual_vectors"]:
            with self.subTest(v["id"]):
                exp = v["expected"]["textual_consistency"]
                if v["applicable_checks"] == 0:
                    self.assertEqual(exp, {"status": "not_applicable"})
                else:
                    self.assertEqual(exp["raw"], 100.0 * v["passed_checks"] / v["applicable_checks"])
                metric("textual_consistency", exp)


if __name__ == "__main__":
    unittest.main()
