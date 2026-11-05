import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from lab_contracts import method
from lab_contracts.method import (JobStatus, PrepareRequest, PrepareResult, QueryRequest,
                                  QueryResponse, SubmissionLine, parse_jsonl, parse_query_response,
                                  payload_hash)

EX = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "valid"


def load(name):
    return json.loads((EX / f"{name}.json").read_text())


RESP = load("QueryResponse.spec_example")
LINE = load("SubmissionLine.spec_example")


def vj(model, obj):
    # The API boundary is JSON; strict mode applies JSON rules (lists for
    # tuples, strings for enums/datetimes), so validate the way the API does.
    return model.model_validate_json(json.dumps(obj, ensure_ascii=False))


def jsonl(*objs_or_raw):
    parts = [o if isinstance(o, str) else json.dumps(o, ensure_ascii=False) for o in objs_or_raw]
    return ("\n".join(parts) + "\n").encode()


class QueryResponseTests(unittest.TestCase):
    def test_http_status_classes(self):
        def status(body, req=None):
            return parse_query_response(body, req)[0]
        self.assertEqual(status(b"{not json"), 400)
        self.assertEqual(status(b"\xff\xfe"), 400)                      # not UTF-8
        self.assertEqual(status("{}".encode("utf-16")), 400)              # UTF-16 is not accepted
        self.assertEqual(status(b"[" * 100_000), 400)                     # deep nesting, no crash
        self.assertEqual(status(json.dumps(RESP).replace('"latency_ms": 1350', '"latency_ms": NaN').encode()), 400)
        self.assertEqual(status(json.dumps(RESP).replace('"latency_ms": 1350', '"latency_ms": 1e999').encode()), 400)
        dup = json.dumps(RESP)[:-1] + ', "status": "abstained"}'
        self.assertEqual(status(dup.encode()), 400)                       # duplicate key
        bad = copy.deepcopy(RESP); bad["retrieved_hits"][1]["rank"] = 1
        code, model, reasons = parse_query_response(json.dumps(bad).encode())
        self.assertEqual((code, model, reasons), (422, None, ["schema_invalid"]))
        self.assertEqual(status(json.dumps(dict(RESP, score=1.0)).encode()), 422)
        with mock.patch.object(method, "MAX_QUERY_RESPONSE_BYTES", 100):
            self.assertEqual(status(json.dumps(RESP).encode()), 413)
        code, model, reasons = parse_query_response(json.dumps(RESP).encode())
        self.assertEqual((code, reasons), (200, []))
        self.assertEqual(model.citations[1].hit_id, "h2")

    def test_binding_enforced_when_request_given(self):
        req = vj(QueryRequest, load("QueryRequest.spec_example"))
        self.assertEqual(parse_query_response(json.dumps(RESP).encode(), req)[0], 200)
        other = req.model_copy(update={"request_id": "req_999"})
        self.assertEqual(parse_query_response(json.dumps(RESP).encode(), other)[:3:2],
                         (422, ["request_id_mismatch"]))

    def test_binding_to_request(self):
        req = vj(QueryRequest, load("QueryRequest.spec_example"))
        resp = vj(QueryResponse, RESP)
        self.assertEqual(resp.check_binding(req), [])
        other = req.model_copy(update={"question_id": "q_002", "index_id": "idx_9"})
        self.assertEqual(resp.check_binding(other), ["question_id_mismatch", "index_id_mismatch"])

    def test_wrong_page_or_hash_is_kept_not_rejected(self):
        # T35: structurally valid region on a wrong page/hash keeps the answer;
        # the region is flagged later, not the whole response rejected.
        r = copy.deepcopy(RESP)
        r["retrieved_hits"][1]["regions"][0]["page_index"] = 9999
        r["retrieved_hits"][1]["regions"][0]["document_sha256"] = "c" * 64
        resp = vj(QueryResponse, r)
        region = resp.retrieved_hits[1].regions[0]
        self.assertEqual(region.check_against_document("a" * 64, 300),
                         ["document_sha256_mismatch", "page_index_out_of_range"])
        self.assertTrue(resp.answer.text)

    def test_unknown_usage_is_null_not_zero(self):
        r = copy.deepcopy(RESP)
        r["telemetry"] = {k: None for k in r["telemetry"]}
        self.assertIsNone(vj(QueryResponse, r).telemetry.input_tokens)
        r.pop("telemetry")
        self.assertIsNone(vj(QueryResponse, r).telemetry)

    def test_empty_hits_and_citations_allowed(self):
        r = copy.deepcopy(RESP)
        r.update(retrieved_hits=[], citations=[])
        vj(QueryResponse, r)

    def test_image_hit_rules_pm_q3(self):
        # Mixed image + caption text with empty content: allowed.
        r = copy.deepcopy(RESP); h = r["retrieved_hits"][0]; h["content"] = ""
        h["regions"].append({**h["regions"][0], "kind": "figure", "quote": None})
        self.assertEqual(vj(QueryResponse, r).retrieved_hits[0].content, "")
        # Whitespace-only content with text regions only: rejected.
        r = copy.deepcopy(RESP); r["retrieved_hits"][0]["content"] = "  \n "
        with self.assertRaises(ValidationError) as cm:
            vj(QueryResponse, r)
        self.assertIn("at least one visual region", str(cm.exception))
        # A visual region on a wrong page/hash keeps the answer (flagged later).
        r = copy.deepcopy(RESP); h = r["retrieved_hits"][0]; h["content"] = ""
        h["regions"] = [{**h["regions"][0], "kind": "image", "quote": None, "page_index": 9999,
                         "document_sha256": "c" * 64}]
        self.assertTrue(vj(QueryResponse, r).answer.text)

    def test_repeated_region_indices_are_normalized_pm_q1(self):
        r = copy.deepcopy(RESP); r["citations"][0]["region_indices"] = [0, 0]
        cit = vj(QueryResponse, r).citations[0]
        self.assertEqual((cit.region_indices, cit.normalized_region_indices), ([0, 0], [0]))
        r = copy.deepcopy(RESP); r["citations"][0]["region_indices"] = [0, 5]
        with self.assertRaises(ValidationError):  # out of range still rejected
            vj(QueryResponse, r)

    def test_duplicate_claim_rejected_repeated_region_index_allowed(self):
        r = copy.deepcopy(RESP); r["answer"]["claims"][1]["claim_id"] = "c1"
        with self.assertRaises(ValidationError) as cm:
            vj(QueryResponse, r)
        self.assertIn("duplicate claim_id", str(cm.exception))
        r = copy.deepcopy(RESP); r["citations"][0]["region_indices"] = [0, 0]
        self.assertEqual(vj(QueryResponse, r).citations[0].region_indices, [0, 0])

    def test_limits(self):
        r = copy.deepcopy(RESP); r["answer"]["text"] = "x" * (method.MAX_ANSWER_CHARS + 1)
        with self.assertRaises(ValidationError) as cm:
            vj(QueryResponse, r)
        self.assertEqual(cm.exception.errors()[0]["type"], "string_too_long")
        r = copy.deepcopy(RESP); r["retrieval_trace"] = [{}] * (method.MAX_TRACE_ENTRIES + 1)
        with self.assertRaises(ValidationError) as cm:
            vj(QueryResponse, r)
        self.assertEqual(cm.exception.errors()[0]["type"], "too_long")


class PrepareTests(unittest.TestCase):
    def test_result_matches_request(self):
        req = vj(PrepareRequest, load("PrepareRequest.one_document"))
        res = vj(PrepareResult, load("PrepareResult.ready"))
        self.assertEqual(res.check_against_request(req), [])
        wrong = res.model_copy(update={"document_hashes": {"doc_001": "b" * 64}})
        self.assertEqual(wrong.check_against_request(req), ["document_hashes_mismatch"])
        extra = res.model_copy(update={"document_hashes": {"doc_001": "a" * 64, "doc_x": "a" * 64}})
        self.assertEqual(extra.check_against_request(req), ["document_hashes_mismatch"])


class JobStatusTests(unittest.TestCase):
    def test_query_result_round_trip(self):
        job = JobStatus.model_validate_json(json.dumps(
            {"job_id": "j", "kind": "query", "status": "succeeded", "progress": 1.0, "result": RESP}))
        self.assertIsInstance(job.result, QueryResponse)

    def test_cancelled_has_no_result_or_error(self):
        vj(JobStatus, {"job_id": "j", "kind": "query", "status": "cancelled", "progress": None})


class JsonlTests(unittest.TestCase):
    def codes(self, body):
        return [(r.status, r.code) for r, _ in parse_jsonl(body)]

    def test_lines_are_independent(self):
        # T33: a bad line never pollutes the others, and never crashes the batch.
        bad_rank = copy.deepcopy(LINE); bad_rank["response"]["retrieved_hits"][1]["rank"] = 1
        bad_rank["response"]["question_id"] = "q_002"
        q3 = copy.deepcopy(LINE); q3["response"]["question_id"] = "q_003"
        nan = json.dumps(q3).replace('"latency_ms": 1350', '"latency_ms": NaN')
        dup = json.dumps(q3)[:-1] + ', "run_id": "run_X"}'
        results = parse_jsonl(jsonl(LINE, "{oops", bad_rank, "[" * 100_000, nan, dup, q3))
        self.assertEqual([(r.line, r.status, r.code) for r, _ in results], [
            (1, "accepted", None), (2, "rejected", "invalid_json"),
            (3, "rejected", "schema_invalid"), (4, "rejected", "invalid_json"),
            (5, "rejected", "invalid_json"), (6, "rejected", "invalid_json"),
            (7, "accepted", None)])
        self.assertIsInstance(results[0][1], SubmissionLine)
        self.assertIsNone(results[2][1])

    def test_line_size_limit(self):
        q2 = copy.deepcopy(LINE); q2["response"]["question_id"] = "q_002"
        with mock.patch.object(method, "MAX_JSONL_LINE_BYTES", len(jsonl(LINE)) + 10):
            q2["response"]["answer"]["text"] += "x" * 100
            self.assertEqual(self.codes(jsonl(LINE, q2)), [("accepted", None), ("rejected", "line_too_large")])

    def test_identical_resend_marked_conflict_rejected(self):
        changed = copy.deepcopy(LINE); changed["response"]["answer"]["text"] = "方案 A。"
        changed["response"]["answer"]["claims"] = []; changed["response"]["citations"] = []
        r = parse_jsonl(jsonl(LINE, LINE, changed))
        self.assertEqual([(x.status, x.code, x.duplicate_of) for x, _ in r],
                         [("accepted", None, None), ("accepted", None, 1),
                          ("rejected", "duplicate_in_batch", None)])
        self.assertEqual(r[0][0].payload_hash, r[1][0].payload_hash)

    def test_batch_envelope_must_be_consistent(self):
        other_run = copy.deepcopy(LINE); other_run["run_id"] = "run_002"
        other_run["response"]["question_id"] = "q_002"
        self.assertEqual(self.codes(jsonl(LINE, other_run)),
                         [("accepted", None), ("rejected", "envelope_mismatch")])

    def test_payload_hash_golden_vectors(self):
        # Pinned: changing lab-c14n-2 must change CANONICAL_JSON_VERSION.
        # These lab-c14n-1 vectors are unchanged under lab-c14n-2.
        self.assertEqual(method.canonical_json({"b": 1.0, "a": [True, None, 0.5, "é"]}),
                         '{"a":[true,null,0.5,"é"],"b":1}'.encode())
        self.assertEqual(payload_hash({"b": 1, "a": 2}), payload_hash({"a": 2, "b": 1.0}))
        self.assertEqual(payload_hash({"a": 1}),
                         "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")
        self.assertNotEqual(payload_hash({"a": 1}), payload_hash({"a": 1.5}))
        with self.assertRaises(ValueError):
            method.canonical_json({"a": float("nan")})

    def test_payload_hash_equivalent_and_distinct_inputs(self):
        self.assertEqual(method.CANONICAL_JSON_VERSION, "lab-c14n-2")
        # Formatting (whitespace, key order) of the transmitted text does not matter.
        compact = method.strict_loads('{"a":[1,2],"b":"x"}')
        spaced = method.strict_loads('{ "b" : "x",\n  "a" : [ 1 , 2 ] }')
        self.assertEqual(payload_hash(compact), payload_hash(spaced))
        # Escaped and literal non-ASCII text is the same JSON string.
        self.assertEqual(payload_hash(method.strict_loads('{"a":"\\u00e9"}')),
                         payload_hash(method.strict_loads('{"a":"é"}')))
        # No Unicode normalization: NFC and NFD are different payloads.
        self.assertNotEqual(payload_hash({"a": "é"}), payload_hash({"a": "é"}))
        # Different content never collides with these near-equivalents.
        for a, b in (({"a": None}, {}), ({"a": [1, 2]}, {"a": [2, 1]}), ({"a": "1"}, {"a": 1}),
                     ({"a": True}, {"a": 1}), ({"a": 0}, {"a": False})):
            with self.subTest(a=a, b=b):
                self.assertNotEqual(payload_hash(a), payload_hash(b))
        with self.assertRaises(TypeError):
            method.canonical_json({"a": {1, 2}})
        for text in ('{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', '{"a":1,"a":1}'):
            with self.subTest(text=text), self.assertRaises(method.StrictJSONError):
                method.strict_loads(text)

    def test_numbers_hash_by_exact_token_value(self):
        # PR #26 review: raw tokens must not be rounded to binary64 first.
        h = lambda text: payload_hash(method.strict_loads(text))
        self.assertNotEqual(h('{"a":9007199254740992.0}'), h('{"a":9007199254740993.0}'))
        self.assertNotEqual(h('{"a":0.1}'), h('{"a":0.1000000000000000055511151231257827}'))
        self.assertEqual(method.canonical_json(method.strict_loads('[9007199254740993.0]')),
                         b"[9007199254740993]")
        # Mathematically equal spellings are one value.
        for group in (('1', '1.0', '1.000', '10e-1', '0.1e1', '1E0', '1e+0'),
                      ('0.1', '1e-1', '0.10', '10E-2'),
                      ('0', '0.0', '-0', '-0.0', '0e5'),
                      ('-2.5', '-25e-1'),
                      ('100000000000000000000000', '1e23', '1.0E+23')):
            with self.subTest(group=group):
                self.assertEqual(len({h("[%s]" % t) for t in group}), 1)
        # Python floats hash as their shortest repr, i.e. like the same text.
        self.assertEqual(payload_hash([0.1, 1.0, 1e-8, 1e23]), h("[0.1,1,1e-8,1e23]"))
        for text, canon in (("[0.5]", b"[0.5]"), ("[1e-7]", b"[0.0000001]"), ("[1e-8]", b"[1e-8]"),
                            ("[123.456e18]", b"[123456000000000000000]"), ("[1.5e21]", b"[1.5e21]"),
                            ("[-1.25e-300]", b"[-1.25e-300]"), ("[12345678901234567890123]",
                                                                 b"[1.2345678901234567890123e22]")):
            with self.subTest(text=text):
                self.assertEqual(method.canonical_json(method.strict_loads(text)), canon)
        # Values a model cannot hold as finite binary64 are rejected, never
        # silently turned into inf or 0.
        for text in ('[1e-999]', '[-1e-400]', '[1e999]', '[-1.8e308]'):
            with self.subTest(text=text), self.assertRaises(method.StrictJSONError):
                method.strict_loads(text)
        self.assertEqual(h("[0e-999]"), h("[0]"))
        self.assertEqual(h("[5e-324]"), h("[5E-324]"))  # smallest subnormal is fine

    def test_resend_with_different_number_is_conflict(self):
        q = copy.deepcopy(LINE); q["response"]["retrieval_trace"] = [{"measurement": "__N__"}]
        a = json.dumps(q, ensure_ascii=False).replace('"__N__"', "9007199254740992.0")
        b = json.dumps(q, ensure_ascii=False).replace('"__N__"', "9007199254740993.0")
        same = json.dumps(q, ensure_ascii=False).replace('"__N__"', "9007199254740992")
        tiny = json.dumps(q, ensure_ascii=False).replace('"__N__"', "1e-999")
        r = parse_jsonl(jsonl(a, tiny, b, same))
        self.assertEqual([(x.status, x.code, x.duplicate_of) for x, _ in r],
                         [("accepted", None, None), ("rejected", "invalid_json", None),
                          ("rejected", "duplicate_in_batch", None), ("accepted", None, 1)])
        self.assertNotEqual(r[0][0].payload_hash, r[2][0].payload_hash)
        self.assertEqual(r[0][0].payload_hash, r[3][0].payload_hash)
        self.assertIsInstance(r[3][1], SubmissionLine)

    def test_hash_is_of_raw_payload_not_model_dump(self):
        # Omitting an optional field and sending null are different payloads;
        # new optional model fields can never change an existing payload hash.
        without = copy.deepcopy(LINE); without["response"].pop("telemetry")
        with_null = copy.deepcopy(LINE); with_null["response"]["telemetry"] = None
        r1 = parse_jsonl(jsonl(without))[0][0].payload_hash
        r2 = parse_jsonl(jsonl(with_null))[0][0].payload_hash
        self.assertEqual(r1, payload_hash(without))
        self.assertNotEqual(r1, r2)

    def test_whole_batch_rejections(self):
        bom = b"\xef\xbb\xbf" + jsonl(LINE)
        for body, status, code in ((b"\xff\xfe{}\n", 415, "invalid_encoding"),
                                   (bom, 415, "invalid_encoding"),
                                   (b"", 422, "empty_submission"),
                                   (b"\n\n \r\n", 422, "empty_submission")):
            with self.subTest(code=code, body=body[:8]), self.assertRaises(method.BatchRejected) as cm:
                parse_jsonl(body)
            self.assertEqual((cm.exception.http_status, cm.exception.code), (status, code))
        self.assertEqual(self.codes(jsonl(LINE)), [("accepted", None)])  # same body without BOM
        with mock.patch.object(method, "MAX_JSONL_BYTES", 10), self.assertRaises(method.BatchRejected) as cm:
            parse_jsonl(jsonl(LINE))
        self.assertEqual(cm.exception.http_status, 413)
        with mock.patch.object(method, "MAX_JSONL_LINES", 1), self.assertRaises(method.BatchRejected) as cm:
            parse_jsonl(jsonl(LINE, LINE))
        self.assertEqual(cm.exception.code, "too_many_lines")

    def test_spec_16_6_batch_limits(self):
        self.assertEqual((method.MAX_JSONL_LINES, method.MAX_JSONL_BYTES), (100, 16 * 1024 * 1024))
        self.assertEqual(method.MAX_QUERY_RESPONSE_BYTES, 16 * 1024 * 1024)
        lines = []
        for i in range(101):
            x = copy.deepcopy(LINE); x["response"]["question_id"] = f"q_{i:03d}"
            lines.append(x)
        with self.assertRaises(method.BatchRejected) as cm:
            parse_jsonl(jsonl(*lines))
        self.assertEqual((cm.exception.http_status, cm.exception.code), (413, "too_many_lines"))
        self.assertEqual(len(parse_jsonl(jsonl(*lines[:100]))), 100)

    def test_crlf_and_blank_line(self):
        q2 = copy.deepcopy(LINE); q2["response"]["question_id"] = "q_002"
        body = (json.dumps(LINE) + "\r\n" + json.dumps(q2) + "\r\n\n").encode()
        self.assertEqual(self.codes(body), [("accepted", None), ("accepted", None), ("rejected", "invalid_json")])


if __name__ == "__main__":
    unittest.main()
