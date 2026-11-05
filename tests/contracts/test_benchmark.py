import copy
import json
import random
import unittest
from pathlib import Path

from pydantic import ValidationError

from lab_contracts import benchmark as bm
from lab_contracts.benchmark import (BenchmarkManifest, EvidenceUnit, Question, SemanticAnswer,
                                     SourceAnswer, TextualAnswer, ValidationRecord, check_manifest,
                                     target_counts, validate_bundle)

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
        {v["validation_id"]: vj(ValidationRecord, v) for v in raw["validations"]},
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
        # [[e18,e247],[e310]]: e310 alone supports every fact on one page.
        def alt(r):
            src = rec(r, "source_answers", "q_002")
            src["evidence_sets"].append({"set_id": "s2", "evidence_ids": ["e310"]})
            for fs in src["fact_support_sets"]:
                fs["support_sets"].append(["e310"])
        self.assertEqual(problems(edit(alt)), [
            ("source_answers[q_002].evidence_sets[s2]", "cross_page_single_page"),
            ("source_answers[q_002].fact_support_sets", "cross_page_single_page")])

    def test_cross_page_set_on_one_page_disqualifies(self):
        def same_page(r):
            for e in r["evidence"]:
                if e["evidence_id"] == "e247":
                    e["regions"][0]["page_index"] = 17
        self.assertEqual(problems(edit(same_page)), [
            ("source_answers[q_002].evidence_sets[s1]", "cross_page_single_page"),
            ("source_answers[q_002].fact_support_sets", "cross_page_single_page")])

    def test_single_page_via_fact_support_only(self):
        # The declared set spans two pages, but each fact also has an
        # alternative on page 300: one page can support the whole answer.
        def alt(r):
            for fs in rec(r, "source_answers", "q_002")["fact_support_sets"]:
                fs["support_sets"].append(["e310"])
        self.assertEqual(problems(edit(alt)),
                         [("source_answers[q_002].fact_support_sets", "cross_page_single_page")])

    def test_evidence_set_must_support_every_fact(self):
        raw = edit(lambda r: rec(r, "source_answers", "q_002")["evidence_sets"].append(
            {"set_id": "s2", "evidence_ids": ["e18", "e310"]}))
        self.assertEqual(problems(raw), [("source_answers[q_002].evidence_sets[s2]", "set_misses_fact_support")])

    def test_multiple_documents_rejected(self):
        def other_doc(r):
            for e in r["evidence"]:
                if e["evidence_id"] == "e247":
                    e["regions"][0].update(document_id="doc_002", document_sha256="b" * 64, page_index=17)
        self.assertIn(("evidence", "multiple_documents"), problems(edit(other_doc)))

    def test_chart_table_needs_visual_evidence(self):
        def chart(r):
            rec(r, "questions", "q_001")["quota_bucket"] = "chart_table"
        self.assertEqual(problems(edit(chart)),
                         [("source_answers[q_001].evidence_sets[s1]", "chart_table_without_visual_evidence")])

    def test_confusable_needs_distractor_and_distractor_not_gold(self):
        raw = edit(lambda r: rec(r, "source_answers", "q_003").update(distractors=[]))
        self.assertEqual(problems(raw), [("source_answers[q_003].distractors", "confusable_without_distractor")])
        raw = edit(lambda r: rec(r, "source_answers", "q_003")["distractors"][0].update(evidence_id="e6"))
        self.assertEqual(problems(raw), [("source_answers[q_003].distractors[e6]", "distractor_is_gold")])

    def test_distractor_used_as_support_elsewhere_is_gold(self):
        def via_fact_support(r):
            rec(r, "source_answers", "q_003")["fact_support_sets"][0]["support_sets"].append(["e5"])
        self.assertIn(("source_answers[q_003].distractors[e5]", "distractor_is_gold"), problems(edit(via_fact_support)))

        def via_textual(r):
            rec(r, "textual_answers", "q_003")["checks"] = [{
                "check_id": "t1", "fact_id": "f1", "kind": "numeric", "expected": 3.0, "interval": None,
                "unit": "year", "normalization_rule": None, "absolute_tolerance": 0.0,
                "relative_tolerance": None, "precision": 0, "supporting_evidence_ids": ["e5"]}]
        self.assertEqual(problems(edit(via_textual)), [("source_answers[q_003].distractors[e5]", "distractor_is_gold")])

    def test_evidence_key_must_match(self):
        q, s, t, src, ev, va = bundle()
        ev["e_other"] = ev.pop("e40")
        got = sorted((p.path, p.code) for p in validate_bundle(q, s, t, src, ev, va))
        self.assertIn(("evidence[e_other]", "evidence_key_mismatch"), got)


class TextCheckTests(unittest.TestCase):
    def check(self, **kw):
        base = {"check_id": "t1", "fact_id": "f1", "kind": "numeric", "expected": 12.0, "interval": None,
                "unit": "ms", "normalization_rule": None, "absolute_tolerance": None,
                "relative_tolerance": None, "precision": None, "supporting_evidence_ids": ["e1"]}
        base.update(kw)
        return vj(bm.TextCheck, base)

    def test_t99_point_boundaries(self):
        c = self.check(absolute_tolerance=0.5, relative_tolerance=0.01)  # max(0.5, 0.12) = 0.5
        self.assertTrue(c.numeric_matches(12.5))
        self.assertTrue(c.numeric_matches(11.5))
        self.assertFalse(c.numeric_matches(12.5001))
        r = self.check(relative_tolerance=0.1)  # 1.2
        self.assertTrue(r.numeric_matches(13.2 - 1e-9))
        self.assertFalse(r.numeric_matches(13.3))
        self.assertFalse(r.numeric_matches(float("nan")))

    def test_t99_relative_tolerance_at_zero(self):
        with self.assertRaises(ValidationError) as cm:
            self.check(expected=0.0, relative_tolerance=0.05)
        self.assertIn("relative tolerance vanishes at zero", str(cm.exception))
        z = self.check(expected=0.0, absolute_tolerance=0.01, relative_tolerance=0.05)
        self.assertTrue(z.numeric_matches(-0.01))
        self.assertFalse(z.numeric_matches(0.0101))

    def test_interval_is_not_widened(self):
        c = self.check(expected=None, interval=[11.5, 12.5])
        self.assertTrue(c.numeric_matches(11.5) and c.numeric_matches(12.5))
        self.assertFalse(c.numeric_matches(12.5000001))
        with self.assertRaises(ValidationError):
            self.check(expected=None, interval=[11.5, 12.5], precision=1)

    def test_numeric_matches_rejects_text_checks(self):
        q = self.check(kind="quote", expected="x", unit=None)
        with self.assertRaises(ValueError):
            q.numeric_matches(1.0)


class BundleFixtureTests(unittest.TestCase):
    """Every hand-written bundle under contracts/examples/bundles states the
    exact problems it must produce; this keeps fixtures and rules in sync."""

    def test_each_bundle_produces_its_declared_problems(self):
        files = sorted((EX / "bundles").glob("*.json"))
        self.assertGreaterEqual(len(files), 7)
        for f in files:
            raw = json.loads(f.read_text())
            with self.subTest(f.name):
                for key in ("_about", "_scenario", "_expected_problems", "_counts"):
                    self.assertIn(key, raw)
                self.assertIn("Synthetic contract fixture", raw["_about"])
                self.assertEqual(problems(raw), sorted(tuple(x) for x in raw["_expected_problems"]))
                self.assertEqual(len(raw["questions"]), raw["_counts"]["questions"])

    def test_declared_bucket_counts_match(self):
        for f in sorted((EX / "bundles").glob("*.json")):
            raw = json.loads(f.read_text())
            want = raw["_counts"].get("quota_buckets")
            if want is None:
                continue
            got = {b: 0 for b in want}
            for q in raw["questions"]:
                got[q["quota_bucket"]] += 1
            with self.subTest(f.name):
                self.assertEqual(got, want)


class ValidationRefTests(unittest.TestCase):
    def test_unknown_and_foreign_validation_refs(self):
        raw = edit(lambda r: rec(r, "source_answers", "q_001").update(validation_refs=["v_999"]))
        self.assertEqual(problems(raw), [("source_answers[q_001].validation_refs[v_999]", "unknown_validation_id")])
        raw = edit(lambda r: rec(r, "source_answers", "q_001").update(validation_refs=["v_003"]))
        self.assertEqual(problems(raw),
                         [("source_answers[q_001].validation_refs[v_003]", "validation_for_other_question")])


class FormulaTests(unittest.TestCase):
    def test_depth_limit_on_any_op(self):
        expr = {"ref": "a"}
        for _ in range(bm.MAX_EXPR_DEPTH):
            expr = {"op": "neg", "args": [expr]}
        with self.assertRaises(ValidationError) as cm:
            bm.Op.model_validate_json(json.dumps(expr))   # Op on its own, not via Calculation
        self.assertIn("deeper than", str(cm.exception))
        ok = {"ref": "a"}
        for _ in range(bm.MAX_EXPR_DEPTH - 1):
            ok = {"op": "neg", "args": [ok]}
        bm.Op.model_validate_json(json.dumps(ok))

    def test_unused_operand_rejected(self):
        sem = copy.deepcopy(RAW["semantic_answers"][1])
        sem["calculation"] = {"operands": [{"name": "a", "value": 1.0, "unit": None, "evidence_id": "e40"},
                                           {"name": "b", "value": 9.0, "unit": None, "evidence_id": "e18"}],
                              "formula": {"ref": "a"}, "answer_unit": None}
        with self.assertRaises(ValidationError) as cm:
            vj(SemanticAnswer, sem)
        self.assertIn("not used by the formula", str(cm.exception))


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
