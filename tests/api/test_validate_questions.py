"""Code checks, reply parsing and verdict rules of the automated validation (P-D slice 4, Refs #7): pure, no database."""
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.validate_questions import VALIDATOR_REVISION, code_checks, parse_validator_reply, verdict_for

PAGE = "The filing deadline is March 31 and the fee is 1,250 dollars for large companies."


class CodeCheckTests(unittest.TestCase):
    def test_grounded_answer_and_facts_pass(self):
        c = code_checks(PAGE, "The deadline is March 31.", ["The filing deadline is March 31", "The fee is 1,250 dollars"])
        self.assertTrue(c["passed"])
        self.assertEqual(c["numbers_missing_from_evidence"], 0)

    def test_a_number_that_is_not_in_the_text_fails(self):
        c = code_checks(PAGE, "The fee is 9,999 dollars.", ["The fee is 1,250 dollars"])
        self.assertFalse(c["passed"])
        self.assertEqual(c["numbers_missing_from_evidence"], 1)

    def test_a_number_must_match_as_a_whole_number_not_as_a_substring(self):
        page = "Revenue grew in 2019 to 12,500 units."
        self.assertFalse(code_checks(page, "It was 1.", ["Revenue grew in 2019"])["passed"])        # "1" is only inside "2019"
        self.assertFalse(code_checks(page, "It was 125.", ["Revenue grew in 2019"])["passed"])      # "125" is only inside "12,500"
        self.assertTrue(code_checks(page, "It was 12500.", ["Revenue grew in 2019"])["passed"])     # separators are ignored
        self.assertTrue(code_checks(page, "In 2019.", ["Revenue grew in 2019"])["passed"])

    def test_a_fact_that_is_mostly_not_in_the_text_fails(self):
        c = code_checks(PAGE, "March.", ["Penguins migrate south every winter"])
        self.assertFalse(c["passed"])
        self.assertLess(c["fact_coverage"][0], 0.6)

    def test_matching_ignores_case_and_unicode_width_and_works_for_cjk(self):
        self.assertTrue(code_checks("ＦＩＬＩＮＧ DEADLINE march", "deadline", ["Filing Deadline"])["passed"])
        self.assertTrue(code_checks("申報期限為三月三十一日", "三月三十一日", ["申報期限為三月三十一日"])["passed"])
        self.assertFalse(code_checks("申報期限為三月三十一日", "十二月", ["完全無關的敘述內容"])["passed"])
        # CJK is matched by character BIGRAMS: a fact that is mostly (>= 60% of its bigrams) in the page passes ...
        self.assertTrue(code_checks("申報期限為三月三十一日，逾期加徵滯納金", "三月三十一日", ["申報期限是三月三十一日"])["passed"])
        # ... a fact on another topic fails ...
        self.assertFalse(code_checks("申報期限為三月三十一日", "三月", ["滯納金依逾期天數加徵"])["passed"])
        # ... and scrambled common ideographs that merely occur somewhere in the page (high per-character coverage, no shared
        # bigram) fail too, which single-character matching would have let through
        self.assertFalse(code_checks("申報期限為三月三十一日", "三月", ["日期限申月報"])["passed"])
        self.assertFalse(code_checks("申報期限為三月三十一日", "三月", ["限月日十三"])["passed"])

    def test_cjk_tokens_are_bigrams_and_other_words_need_three_characters(self):
        from lab_api.validate_questions import content_tokens
        self.assertEqual(content_tokens("申報期限"), ["申報", "報期", "期限"])
        self.assertEqual(content_tokens("月"), ["月"])
        self.assertEqual(sorted(content_tokens("The deadline is ab 申報")), ["deadline", "the", "申報"])
        self.assertEqual(content_tokens("ＦＩＬＩＮＧ"), ["filing"])                      # NFKC + case folding first


class ReplyTests(unittest.TestCase):
    def test_only_the_exact_boolean_json_parses(self):
        good = '{"supported": true, "answerable_from_evidence": true, "needs_outside_context": false}'
        self.assertEqual(parse_validator_reply(good), (True, True, False))
        self.assertEqual(parse_validator_reply("```json\n" + good + "\n```"), (True, True, False))
        for bad in ("", "yes", "[]", '{"supported": true}', good.replace("false", '"no"'), good.replace("}", ', "x": 1}'),
                    good.replace("true", "1", 1), "Sure: " + good):
            self.assertIsNone(parse_validator_reply(bad), bad)


class VerdictTests(unittest.TestCase):
    ok = {"passed": True}
    bad = {"passed": False}

    def test_pass_needs_both_the_code_checks_and_the_validator(self):
        self.assertEqual(verdict_for(self.ok, (True, True, False))[0], "pass")

    def test_every_negative_is_a_fail_and_unknown_is_never_a_pass(self):
        self.assertEqual(verdict_for(self.bad, (True, True, False))[0], "fail")          # the validator cannot rescue a bad grounding
        self.assertEqual(verdict_for(self.bad, None)[0], "fail")
        self.assertEqual(verdict_for(self.ok, (False, True, False))[0], "fail")
        self.assertEqual(verdict_for(self.ok, (True, False, False))[0], "fail")
        self.assertEqual(verdict_for(self.ok, (True, True, True))[0], "fail")
        self.assertEqual(verdict_for(self.ok, None)[0], "needs_review")

    def test_the_revision_says_what_the_validator_is(self):
        self.assertIn("same-model", VALIDATOR_REVISION)


if __name__ == "__main__":
    unittest.main()
