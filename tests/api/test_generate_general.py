"""General-bucket derive/validate path without a database (P-D slice 3, Refs #7): reply -> four validated parts."""
import json
import sys
import unittest
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_contracts.benchmark import EvidenceUnit, validate_bundle
from lab_contracts.source import RegionKind, SourceRegion
from lab_api.generate_general import PageEvidence, accept_replies, derive, general_target, question_hash
from lab_api.generate_prompts import PROMPT_VERSION, build_page_call

SHA = "a" * 64


def unit(eid, text, page=0, y=0.1):
    region = SourceRegion(document_id="doc", document_sha256=SHA, page_index=page, bbox=(0.1, y, 0.9, y + 0.1),
                          kind=RegionKind.text, quote=None)
    return EvidenceUnit(evidence_id=eid, regions=[region],
                        raw_content=text, normalized_content=text, extractor_revision="r1", ocr_confidence=None, asset_refs=[])


PAGE = PageEvidence(2, (unit("u1", "The filing deadline is March 31.", 2), unit("u2", "Late filings pay a fee.", 2, 0.3)))


def reply(**over):
    base = {"question": "When is the filing deadline?", "reference_answer": "March 31.",
            "facts": [{"statement": "The filing deadline is March 31.", "weight": 2}, {"statement": "It is in spring."}]}
    base.update(over)
    return json.dumps(base)


class DeriveTests(unittest.TestCase):
    def test_a_good_reply_gives_four_valid_parts_derived_by_code(self):
        d = derive(reply(), question_id="q1", ordinal=4, page=PAGE)
        self.assertIsNotNone(d)
        q, sem, txt, src = d.question, d.semantic, d.textual, d.source
        self.assertEqual((q.quota_bucket.value, q.ordinal, q.language, q.reasoning_type.value), ("general", 4, "en", "value_lookup"))
        self.assertEqual(q.tags, ["llm_generated", f"prompt:{PROMPT_VERSION}"])
        self.assertEqual(q.question_hash, question_hash("  when is the FILING deadline? "))
        self.assertEqual([(f.fact_id, f.weight) for f in sem.required_facts], [("f1", 2.0), ("f2", 1.0)])
        self.assertIsNone(sem.calculation)
        # a quote check only for the fact that appears verbatim in the page; none is invented for the other
        self.assertEqual([(c.fact_id, c.kind, c.supporting_evidence_ids) for c in txt.checks], [("f1", "quote", ["u1"])])
        self.assertEqual([s.evidence_ids for s in src.evidence_sets], [["u1", "u2"]])
        self.assertEqual(src.distractors, [])
        self.assertEqual(validate_bundle([q], [sem], [txt], [src], {u.evidence_id: u for u in PAGE.units}, {}), [])

    def test_invalid_replies_and_an_empty_page_give_nothing(self):
        for text in ("not json", reply(facts=[]), reply(question=""), reply(extra=1)):
            self.assertIsNone(derive(text, question_id="q", ordinal=0, page=PAGE), text)
        self.assertIsNone(derive(reply(), question_id="q", ordinal=0, page=PageEvidence(0, ())))
        self.assertIsNone(derive(reply(), question_id="", ordinal=0, page=PAGE))          # contract refuses an empty id


class AcceptTests(unittest.TestCase):
    def ids(self):
        n = iter(range(1000))
        return lambda: f"q{next(n)}"

    def test_invalid_dropped_and_counted_duplicates_dropped_target_respected(self):
        replies = [("g0-p2", reply()), ("g1-p2", "garbage"), ("g2-p2", reply(question="When is the filing DEADLINE?")),
                   ("g3-p2", reply(question="What do late filings pay?")), ("g4-p9", reply(question="Other page?")),
                   ("bad-id", reply(question="x?")), ("g5-p2", reply(question="One too many?"))]
        out, c = accept_replies(replies, {2: PAGE}, target=2, seen_hashes=set(), first_ordinal=7, new_id=self.ids())
        self.assertEqual([d.question.question_text for d in out], ["When is the filing deadline?", "What do late filings pay?"])
        self.assertEqual([d.question.ordinal for d in out], [7, 8])
        self.assertEqual((c.replies, c.accepted, c.invalid, c.duplicate, c.over_target), (7, 2, 3, 1, 1))

    def test_hashes_already_in_the_benchmark_count_as_duplicates(self):
        out, c = accept_replies([("g0-p2", reply())], {2: PAGE}, target=5,
                                seen_hashes={question_hash("When is the filing deadline?")}, first_ordinal=0)
        self.assertEqual((out, c.duplicate), ([], 1))

    def test_general_target_matches_the_material_plan(self):
        self.assertEqual(general_target(20, 2), 14)
        self.assertEqual(general_target(6, 2), 0)


class WorkerMainTests(unittest.TestCase):
    ENV = {"LAB_GEN_CMD": "/opt/lab-gen", "LAB_GEN_PROTOCOL": "p", "LAB_GEN_BASE_URL": "https://api.example.com",
           "LAB_GEN_ALLOWED_HOST": "api.example.com", "LAB_GEN_INPUT_MICROS_PER_MTOK": "1000000",
           "LAB_GEN_OUTPUT_MICROS_PER_MTOK": "5000000"}

    def test_configuration_is_required_never_assumed(self):
        from lab_api.generate_worker_main import GenerateWorker, config_from_env
        cmd, cfg = config_from_env(self.ENV)
        self.assertEqual((cmd, cfg.allowed_host, cfg.input_micros_per_mtok, cfg.output_micros_per_mtok),
                         (["/opt/lab-gen"], "api.example.com", 1_000_000, 5_000_000))
        for key in self.ENV:
            with self.assertRaises(ValueError):
                config_from_env({k: v for k, v in self.ENV.items() if k != key})
        with self.assertRaises(ValueError):
            config_from_env({**self.ENV, "LAB_GEN_INPUT_MICROS_PER_MTOK": "-1"})
        self.assertEqual(GenerateWorker.KIND, "document.generate")


class PromptExclusionTests(unittest.TestCase):
    def test_only_the_page_text_is_sent(self):
        call = build_page_call("g0-p2", 2, PAGE.texts())
        for secret in ("u1", "u2", "doc", SHA, "r1"):
            self.assertNotIn(secret, call.user)
        self.assertIn("The filing deadline is March 31.", call.user)


if __name__ == "__main__":
    unittest.main()
