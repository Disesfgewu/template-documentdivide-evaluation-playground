"""The pure part of the uploaded developer answers (Issue #189): strict file validation and locating the quoted text (no database)."""
import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts", _REPO / "scoring"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api import external_submission as ex
from lab_api.benchmark_external import FORMAT_EXAMPLE, format_doc

KEYS = {"q1", "q2", "q3"}


def line(**kw):
    return json.dumps({"question_id": "q1", "status": "answered", "answer": "A fee of 120 EUR.", "sources": [{"page": 2, "text": "The annual fee is 120 EUR."}], **kw})


def parse(*lines, pages=5):
    return ex.parse_file("\n".join(lines), KEYS, pages)


class ParseTests(unittest.TestCase):
    def test_a_good_file_with_a_header_an_answer_and_an_abstention(self):
        p = parse('{"method_name": "m", "method_version": "1", "notes": "n"}', line(),
                  json.dumps({"question_id": "q2", "status": "abstained", "reason": "not in the document"}))
        self.assertEqual((p.problems, p.header, sorted(p.entries)), ([], {"method_name": "m", "method_version": "1", "notes": "n"}, ["q1", "q2"]))
        self.assertEqual(p.entries["q1"].sources[0].page, 2)
        self.assertEqual(p.entries["q2"].status, "abstained")
        self.assertEqual(p.lines, 3)

    def test_the_documented_example_is_a_valid_file(self):
        keys = {"q-8f3a1c", "q-b02d77"}
        p = ex.parse_file("\n".join(FORMAT_EXAMPLE), keys, 10)
        self.assertEqual(p.problems, [])
        self.assertEqual(sorted(p.entries), sorted(keys))

    def test_every_problem_is_a_line_number_and_a_fixed_code_never_the_content(self):
        secret = "SECRET-CONTENT"
        bad = {
            "invalid_json": "{not json " + secret, "not_an_object": '["x"]', "unknown_keys": line(extra=secret),
            "bad_question_id": json.dumps({"question_id": 5, "status": "answered"}), "unknown_question": line(question_id="nope" + secret),
            "bad_status": line(status="maybe"), "bad_answer": line(answer="  "), "answer_too_long": line(answer="x" * 4001),
            "too_many_sources": line(sources=[{"page": 1, "text": "abcdefgh"}] * 11), "bad_source": line(sources=["x"]),
            "bad_page": line(sources=[{"page": "2", "text": "abcdefgh"}]), "page_out_of_range": line(sources=[{"page": 6, "text": "abcdefgh"}]),
            "bad_source_text": line(sources=[{"page": 1, "text": 5}]), "source_text_too_long": line(sources=[{"page": 1, "text": "x" * 2001}]),
            "source_text_too_short": line(sources=[{"page": 1, "text": " ab "}]),
            "reason_on_answered": line(reason="why"),
            "reason_missing": json.dumps({"question_id": "q1", "status": "abstained"}),
            "reason_too_long": json.dumps({"question_id": "q1", "status": "abstained", "reason": "r" * 2001}),
            "abstained_with_answer_or_sources": json.dumps({"question_id": "q1", "status": "abstained", "reason": "r", "answer": "a"}),
        }
        for code, text in bad.items():
            p = parse(text)
            self.assertEqual([x["code"] for x in p.problems], [code], code)
            self.assertEqual(p.problems[0]["line"], 1)
            self.assertNotIn(secret, json.dumps(p.problems))
            self.assertEqual(p.entries, {} if code != "reason_missing" else {}, code)

    def test_duplicates_headers_and_blank_lines(self):
        p = parse("", line(), "", line(answer="second"), '{"method_name": "late"}')
        self.assertEqual([(x["line"], x["code"]) for x in p.problems], [(4, "duplicate_question"), (5, "header_not_first")])
        self.assertEqual(p.entries["q1"].answer, "A fee of 120 EUR.")                      # the first line of a question wins
        self.assertEqual(p.ignored, 1)
        p = parse('{"method_name": "a"}', '{"method_name": "b"}', line())
        self.assertEqual([x["code"] for x in p.problems], ["duplicate_header"])
        self.assertEqual([x["code"] for x in parse('{"method_name": 5}').problems], ["header_invalid"])

    def test_the_problem_list_is_bounded(self):
        p = parse(*(["{bad"] * 500))
        self.assertEqual(len(p.problems), ex.MAX_PROBLEMS_LISTED)


class U:
    def __init__(self, key, raw, regions):
        self.key, self.raw, self.text, self.regions = key, raw, ex.norm_strip(raw), regions


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.a = U("e1", "The annual fee of the Gold plan is 120 EUR.", [(1, (0.1, 0.1, 0.9, 0.2), "text")])
        self.b = U("e2", "It is due in May.", [(1, (0.1, 0.3, 0.9, 0.4), "text")])
        self.pages = {1: [self.a, self.b]}

    def test_found_inside_one_unit_ignores_case_and_whitespace_and_gives_that_units_region(self):
        got = ex.locate("the ANNUAL   fee of\nthe gold plan", 1, self.pages)
        self.assertEqual((got.quality, got.found, got.coverage), ("exact_in_unit", True, 1.0))
        self.assertEqual(got.regions, [(1, (0.1, 0.1, 0.9, 0.2), "text")])

    def test_found_across_two_units_gives_both_regions(self):
        got = ex.locate("Gold plan is 120 EUR. It is due in May", 1, self.pages)
        self.assertEqual((got.quality, got.found), ("exact_in_page", True))
        self.assertEqual({r[1] for r in got.regions}, {self.a.regions[0][1], self.b.regions[0][1]})

    def test_a_quote_that_is_not_on_the_page_is_reported_with_the_whole_page_and_how_much_of_it_occurs(self):
        got = ex.locate("The annual fee of the Silver plan is 60 EUR and invented words everywhere", 1, self.pages)
        self.assertEqual((got.quality, got.found, got.regions), ("not_found", False, [(1, (0.0, 0.0, 1.0, 1.0), "page")]))
        self.assertTrue(0 < got.coverage < 1)
        none = ex.locate("completely unrelated sentence about nothing", 1, self.pages)
        self.assertEqual((none.quality, none.coverage), ("not_found", 0.0))

    def test_a_page_the_parse_has_no_text_for_cannot_confirm_anything(self):
        got = ex.locate("The annual fee of the Gold plan", 2, self.pages)
        self.assertEqual((got.quality, got.found, got.regions[0][2]), ("page_without_text", False, "page"))

    def test_cjk_quotes_match_with_or_without_spaces(self):
        c = U("c1", "申報期限為四月三十日，逾期加徵滯納金。", [(0, (0.1, 0.1, 0.9, 0.2), "text")])
        self.assertTrue(ex.locate("申報期限為 四月三十日", 0, {0: [c]}).found)


class FormatTests(unittest.TestCase):
    def test_the_documented_files_are_what_the_api_serves(self):
        docs = Path(__file__).resolve().parents[2] / "docs"
        doc = format_doc()
        self.assertEqual(json.loads((docs / "answers-file.schema.json").read_text(encoding="utf-8")), {"line": doc["line_schema"], "header": doc["header_schema"]})
        self.assertEqual((docs / "answers-file.example.jsonl").read_text(encoding="utf-8").splitlines(), doc["example_lines"])
        self.assertTrue((docs / "SUBMISSION_FORMAT.md").is_file())

    def test_the_format_description_has_an_example_and_a_schema(self):
        doc = format_doc()
        self.assertEqual(doc["format"], "lab-submission-1")
        self.assertEqual(len(doc["example_lines"]), 3)
        self.assertEqual(doc["line_schema"]["properties"]["status"]["enum"], ["answered", "abstained"])
        self.assertEqual(doc["line_schema"]["properties"]["sources"]["maxItems"], 10)
        self.assertEqual(doc["line_schema"]["properties"]["answer"]["maxLength"], 4000)
        self.assertEqual(doc["line_schema"]["properties"]["sources"]["items"]["properties"]["text"]["maxLength"], 2000)


if __name__ == "__main__":
    unittest.main()
