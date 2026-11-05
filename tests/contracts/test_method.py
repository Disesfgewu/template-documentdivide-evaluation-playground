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
        # Pinned: changing lab-c14n-1 must change CANONICAL_JSON_VERSION.
        self.assertEqual(method.canonical_json({"b": 1.0, "a": [True, None, 0.5, "é"]}),
                         '{"a":[true,null,0.5,"é"],"b":1}'.encode())
        self.assertEqual(payload_hash({"b": 1, "a": 2}), payload_hash({"a": 2, "b": 1.0}))
        self.assertEqual(payload_hash({"a": 1}),
                         "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")
        self.assertNotEqual(payload_hash({"a": 1}), payload_hash({"a": 1.5}))
        with self.assertRaises(ValueError):
            method.canonical_json({"a": float("nan")})

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

    def test_crlf_and_blank_line(self):
        q2 = copy.deepcopy(LINE); q2["response"]["question_id"] = "q_002"
        body = (json.dumps(LINE) + "\r\n" + json.dumps(q2) + "\r\n\n").encode()
        self.assertEqual(self.codes(body), [("accepted", None), ("accepted", None), ("rejected", "invalid_json")])


if __name__ == "__main__":
    unittest.main()
