import copy
import json
import random
import unittest
from pathlib import Path

from pydantic import ValidationError

from lab_contracts import benchmark as bm
from lab_contracts.benchmark import (BenchmarkManifest, EvidenceUnit, Question, SemanticAnswer,
                                     SourceAnswer, TextualAnswer, check_manifest, target_counts,
                                     validate_bundle)

EX = Path(__file__).resolve().parents[2] / "contracts" / "examples"
RAW = json.loads((EX / "bundles" / "minimal.json").read_text())


def vj(model, obj):
    return model.model_validate_json(json.dumps(obj, ensure_ascii=False))


def bundle(raw=None):
    raw = raw or RAW
    return (
        [vj(Question, q) for q in raw["questions"]],
        [vj(SemanticAnswer, s) for s in raw["semantic_answers"]],
        [vj(TextualAnswer, t) for t in raw["textual_answers"]],
        [vj(SourceAnswer, s) for s in raw["source_answers"]],
        {e["evidence_id"]: vj(EvidenceUnit, e) for e in raw["evidence"]},
    )


def problems(raw):
    return sorted((p.path, p.code) for p in validate_bundle(*bundle(raw)))


def edit(f):
    raw = copy.deepcopy(RAW)
    f(raw)
    return raw


def rec(raw, part, qid):
    return next(r for r in raw[part] if r["question_id"] == qid)


class TargetCountTests(unittest.TestCase):
    def test_t14_table(self):
        # SPEC T14: pages 1/19/20/21/500/600 -> N=20/20/20/21/500/600, Q=2/2/2/3/50/60.
        got = [target_counts(p) for p in (1, 19, 20, 21, 500, 600)]
        self.assertEqual(got, [(20, 2), (20, 2), (20, 2), (21, 3), (500, 50), (600, 60)])

    def test_exact_integer_ceiling(self):
        self.assertEqual(target_counts(101), (101, 11))
        self.assertEqual(target_counts(110), (110, 11))

    def test_rejects_non_positive_and_non_int(self):
        for bad in (0, -1, 20.0, True, "20"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                target_counts(bad)


class BundleTests(unittest.TestCase):
    def test_minimal_bundle_is_consistent(self):
        self.assertEqual(problems(RAW), [])

    def test_t13_order_independent(self):
        rng = random.Random(7)
        for _ in range(5):
            raw = copy.deepcopy(RAW)
            for part in ("questions", "semantic_answers", "textual_answers", "source_answers", "evidence"):
                rng.shuffle(raw[part])
            self.assertEqual(problems(raw), [])

    def test_t13_missing_and_duplicate_ids(self):
        raw = edit(lambda r: r["semantic_answers"].pop())
        self.assertIn(("semantic_answers[q_002]", "missing_record"), problems(raw))
        raw = edit(lambda r: r["textual_answers"].append(copy.deepcopy(r["textual_answers"][0])))
        self.assertIn(("textual_answers[3]", "duplicate_question_id"), problems(raw))
        raw = edit(lambda r: rec(r, "source_answers", "q_001").update(question_id="q_999"))
        got = problems(raw)
        self.assertIn(("source_answers[q_999]", "unknown_question_id"), got)
        self.assertIn(("source_answers[q_001]", "missing_record"), got)

    def test_t12_unknown_fact_and_evidence_are_located(self):
        raw = edit(lambda r: rec(r, "textual_answers", "q_002")["checks"][0].update(fact_id="f9"))
        self.assertEqual(problems(raw), [("textual_answers[q_002].checks[t1].fact_id", "unknown_fact_id")])
        raw = edit(lambda r: rec(r, "source_answers", "q_001")["evidence_sets"][0].update(evidence_ids=["e999"]))
        self.assertIn(("source_answers[q_001].evidence_sets[s1].evidence_ids[e999]", "unknown_evidence_id"),
                      problems(raw))

    def test_fact_without_support(self):
        raw = edit(lambda r: rec(r, "source_answers", "q_002")["fact_support_sets"].pop())
        self.assertEqual(problems(raw), [("source_answers[q_002].fact_support_sets[f2]", "fact_without_support")])

    def test_t15_duplicate_question_and_ordinal(self):
        def dup(r):
            q = copy.deepcopy(rec(r, "questions", "q_001"))
            q.update(question_id="q_004", ordinal=0)
            r["questions"].append(q)
            for part in ("semantic_answers", "textual_answers", "source_answers"):
                x = copy.deepcopy(rec(r, part, "q_001")); x["question_id"] = "q_004"; r[part].append(x)
        got = problems(edit(dup))
        self.assertIn(("questions[q_004].question_hash", "duplicate_question"), got)
        self.assertIn(("questions[q_004].ordinal", "duplicate_ordinal"), got)

    def test_cross_page_single_page_alternative_disqualifies(self):
        # [[e18,e247],[e310]]: e310 alone supports the answer on one page.
        raw = edit(lambda r: rec(r, "source_answers", "q_002")["evidence_sets"].append(
            {"set_id": "s2", "evidence_ids": ["e310"]}))
        self.assertEqual(problems(raw), [("source_answers[q_002].evidence_sets[s2]", "cross_page_single_page")])

    def test_cross_page_set_on_one_page_disqualifies(self):
        def same_page(r):
            for e in r["evidence"]:
                if e["evidence_id"] == "e247":
                    e["regions"][0]["page_index"] = 17
        self.assertEqual(problems(edit(same_page)),
                         [("source_answers[q_002].evidence_sets[s1]", "cross_page_single_page")])

    def test_confusable_needs_distractor_and_distractor_not_gold(self):
        raw = edit(lambda r: rec(r, "source_answers", "q_003").update(distractors=[]))
        self.assertEqual(problems(raw), [("source_answers[q_003].distractors", "confusable_without_distractor")])
        raw = edit(lambda r: rec(r, "source_answers", "q_003")["distractors"][0].update(evidence_id="e6"))
        self.assertEqual(problems(raw), [("source_answers[q_003].distractors[e6]", "distractor_is_gold")])

    def test_evidence_key_must_match(self):
        q, s, t, src, ev = bundle()
        ev["e_other"] = ev.pop("e40")
        got = sorted((p.path, p.code) for p in validate_bundle(q, s, t, src, ev))
        self.assertIn(("evidence[e_other]", "evidence_key_mismatch"), got)


class FormulaTests(unittest.TestCase):
    def test_depth_limit(self):
        expr = {"ref": "a"}
        for _ in range(bm.MAX_EXPR_DEPTH + 2):
            expr = {"op": "neg", "args": [expr]}
        sem = copy.deepcopy(RAW["semantic_answers"][1])
        sem["calculation"] = {"operands": [{"name": "a", "value": 1.0, "unit": None, "evidence_id": "e40"}],
                              "formula": expr, "answer_unit": None}
        with self.assertRaises(ValidationError) as cm:
            vj(SemanticAnswer, sem)
        self.assertIn("deeper than", str(cm.exception))


class ManifestTests(unittest.TestCase):
    def manifest(self, **kw):
        m = json.loads((EX / "valid" / "BenchmarkManifest.compliant_21_pages.json").read_text())
        m.update(kw)
        return vj(BenchmarkManifest, m)

    def test_counts_must_match_questions(self):
        q, *_ = bundle()
        m = self.manifest(page_count=1, n_target=20, q_min=2, n_valid=3, compliance="partial",
                          exceptions=["fixture"], quota_counts={"general": 1, "cross_page": 1,
                                                                "chart_table": 0, "confusable": 1})
        self.assertEqual(check_manifest(m, q), [])
        m2 = m.model_copy(update={"quota_counts": m.quota_counts.model_copy(update={"general": 2, "confusable": 0})})
        got = sorted((p.path, p.code) for p in check_manifest(m2, q))
        self.assertEqual(got, [("manifest.quota_counts.confusable", "count_mismatch"),
                               ("manifest.quota_counts.general", "count_mismatch")])


if __name__ == "__main__":
    unittest.main()
