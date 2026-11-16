"""Issue #197, built from the PM's real run: gold evidence is the units that support each fact (not the page furniture), a changed
number or direction is never 'stated', no passage means the facts are judged against the answer alone, and app.log shows the decisions."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab_api.app_log import ContextScope, bound_value, log_op, preview_value, set_question
from lab_api.evidence_support import NotAttributable, boilerplate_ids, support_units
from lab_api.method_judge import JUDGE_PROMPT_REVISION, SYSTEM, build_judge_call, number_check
from lab_contracts.benchmark import EvidenceUnit
from lab_contracts.source import RegionKind, SourceRegion
from lab_contracts.score import MetricResult

SHA = "a" * 64


def unit(eid, text, page=0, bbox=(0.1, 0.3, 0.9, 0.4)):
    region = SourceRegion(document_id="d", document_sha256=SHA, page_index=page, bbox=tuple(bbox), kind=RegionKind.text, quote=None)
    return EvidenceUnit(evidence_id=eid, regions=[region], raw_content=text, normalized_content=text, extractor_revision="x",
                        ocr_confidence=None, asset_refs=[])


def paper_page(page, body):
    """A page of the PM's paper: running header, author line, body units, page number."""
    units = [unit(f"p{page}-hdr", "Preprint. Under review at ISCA 2018", page, (0.1, 0.01, 0.9, 0.04)),
             unit(f"p{page}-auth", "A. Author, B. Author, C. Author", page, (0.1, 0.05, 0.9, 0.07))]
    units += [unit(f"p{page}-b{i}", t, page) for i, t in enumerate(body)]
    units.append(unit(f"p{page}-num", str(39 + page), page, (0.45, 0.95, 0.55, 0.98)))
    return units


BODY = ["PreSET's access latency is 19% lower than the Baseline.",
        "Flip-N-Write's access latency is 10% higher than the Baseline.",
        "The memory array uses Ge2Sb2Te5 cells with a high resistance ratio and fast switching.",
        "We evaluate eight SPEC workloads on a four-core system with a shared last-level cache.",
        "Table 3 lists the simulated system configuration and the PCM timing parameters."]


class SupportUnitsTests(unittest.TestCase):
    def setUp(self):
        self.pages = {1: paper_page(1, BODY), 2: paper_page(2, ["Another page about wear leveling in phase change memory."])}
        self.boiler = boilerplate_ids(self.pages)

    def test_page_furniture_is_never_gold(self):
        for suffix in ("hdr", "auth", "num"):
            self.assertIn(f"p1-{suffix}", self.boiler, suffix)
        self.assertNotIn("p1-b0", self.boiler)

    def test_a_fact_is_supported_by_the_unit_that_carries_it_not_the_whole_page(self):
        got = support_units("PreSET is 19% lower than Baseline", self.pages[1], self.boiler)
        self.assertEqual([u.evidence_id for u in got], ["p1-b0"])           # 1 of the page's 8 units: not 8, not the header, not '40'
        got = support_units("Flip-N-Write is 10% higher than Baseline", self.pages[1], self.boiler)
        self.assertEqual([u.evidence_id for u in got], ["p1-b1"])

    def test_a_fact_spread_over_two_units_takes_both_and_nothing_else(self):
        got = support_units("The memory array uses Ge2Sb2Te5 cells with eight SPEC workloads on a four-core system", self.pages[1], self.boiler)
        self.assertEqual([u.evidence_id for u in got], ["p1-b2", "p1-b3"])

    def test_a_header_does_not_support_a_fact_even_if_it_shares_words(self):
        with self.assertRaises(NotAttributable):
            support_units("The Preprint was under review at ISCA 2018", self.pages[1], self.boiler)

    def test_a_fact_the_page_does_not_carry_is_not_attributable(self):
        for fact in ("PreSET lowers the energy by 37%", "Graphene transistors switch in two picoseconds"):
            with self.subTest(fact=fact), self.assertRaises(NotAttributable):
                support_units(fact, self.pages[1], self.boiler)


class NumberCheckTests(unittest.TestCase):
    FACTS = ["PreSET is 19% lower than Baseline", "Flip-N-Write is 10% higher than Baseline"]
    SWAPPED = "PreSET's access latency is 10% higher than Baseline, while Flip-N-Write's is 19% lower"      # the PM's ordinal-9 answer
    RIGHT = "Flip-N-Write's access latency is 10% higher than Baseline, while PreSET's is 19% lower"

    def test_the_pms_swapped_answer_states_neither_fact(self):
        for fact in self.FACTS:
            ok, code = number_check(fact, self.SWAPPED)
            self.assertFalse(ok, fact)
            self.assertEqual(code, "number_other_entity")

    def test_the_right_answer_and_a_paraphrase_pass(self):
        for answer in (self.RIGHT, "PreSET reduces latency by 19% relative to Baseline; Flip-N-Write is 10% higher than Baseline"):
            for fact in self.FACTS:
                self.assertEqual(number_check(fact, answer), (True, ""), (fact, answer))

    def test_a_missing_or_different_number_fails(self):
        self.assertEqual(number_check("Latency is 5 ms", "Latency is 7 ms"), (False, "number_absent"))
        self.assertEqual(number_check("Latency is 5 ms", "It is fast"), (False, "number_absent"))

    def test_the_opposite_direction_fails_even_with_the_right_number_and_entity(self):
        self.assertEqual(number_check("PreSET is 19% lower than Baseline", "PreSET is 19% higher than Baseline"), (False, "number_opposite_direction"))

    def test_thousands_separators_and_trailing_zeros_are_the_same_number(self):
        self.assertTrue(number_check("The ratio is 1,000 ohms", "about 1000 ohms")[0])
        self.assertTrue(number_check("It takes 2.50 ns", "It takes 2.5 ns")[0])

    def test_a_fact_without_a_number_is_left_to_the_judge(self):
        self.assertEqual(number_check("GST has a high endurance", "Ge2Sb2Te5 (GST), because of its high endurance"), (True, ""))


class JudgePromptTests(unittest.TestCase):
    def test_v2_is_strict_about_numbers_direction_and_entity_and_has_a_contrastive_example(self):
        self.assertEqual(JUDGE_PROMPT_REVISION, "judge-v2")
        for word in ("every number", "direction", "entity", "contradicted", "19% lower", "10% higher"):
            self.assertIn(word, SYSTEM)

    def test_no_passage_is_stated_explicitly_and_the_facts_are_still_judged(self):
        # the PM's ordinal 16: a correct answer whose only quote was invented, so no passage is offered
        call = build_judge_call("j0016", ["Ge2Sb2Te5 (GST)", "high resistance ratio"], "GST, because of its high amorphous-to-crystalline resistance ratio",
                                [("c1", "GST has a high resistance ratio", [])], {})
        self.assertIn("NO PASSAGES WERE OFFERED", call.user)
        self.assertIn("against the answer only", call.user)
        self.assertIn("NO PASSAGES WERE OFFERED", SYSTEM)
        self.assertIn("judge every fact against the answer alone", SYSTEM)

    def test_with_a_passage_the_message_does_not_claim_there_is_none(self):
        call = build_judge_call("j1", ["A"], "A", [("c1", "A", ["p1-c0"])], {"p1-c0": "A is true"})
        self.assertNotIn("NO PASSAGES", call.user)
        self.assertIn('<hit id="p1-c0">', call.user)


class AppLogReadableTests(unittest.TestCase):
    def log(self, fn):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, LAB_APP_LOG="full", LAB_APP_LOG_FILE=d + "/app.log"):
            fn()
            return Path(d + "/app.log").read_text()

    def result(self):
        return MetricResult(question_id="q1", metric="recall_at_k", status="scored", raw_value=12.5, display_value="12.50", rounding="lab-round-1",
                            numerator=1, denominator=8, reason_codes=["contradicted_fact"], grader_revision="g1", support_refs=[])

    def test_a_metric_result_is_serialised_as_its_fields_not_object(self):
        out = bound_value([self.result()])
        self.assertNotIn("<object>", out)
        for part in ("recall_at_k", "scored", "12.5", "'numerator': 1", "'denominator': 8", "contradicted_fact"):
            self.assertIn(part, out)

    def test_short_ids_and_verdicts_have_no_hash_wrapper_and_long_text_keeps_its_preview(self):
        self.assertEqual(preview_value(["f1", "stated"]), "['f1', 'stated']")
        long = "x" * 200
        self.assertIn("sha256:", preview_value(long))
        self.assertEqual(preview_value("y" * 39), repr("y" * 39))

    def test_every_line_in_a_question_scope_carries_its_key_and_ordinal(self):
        def run():
            with ContextScope(service="api", job_id="j1"):
                set_question("q-abc", 9)
                log_op("question_summary", stage="judge", ordinal=9, fact_verdicts=["f1=stated"])
                set_question(None)
                log_op("after", stage="judge")
        lines = [l for l in self.log(run).splitlines() if "question_summary" in l or "FUNCTION:{after" in l]
        self.assertIn("q=q-abc#9", lines[0])
        self.assertIn("ordinal=9", lines[0])
        self.assertIn("'f1=stated'", lines[0])
        self.assertNotIn("q=", lines[1])
