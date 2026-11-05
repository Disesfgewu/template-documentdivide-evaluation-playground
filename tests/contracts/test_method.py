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
        self.assertEqual(parse_query_response(b"{not json")[0], 400)
        self.assertEqual(parse_query_response(b"\xff\xfe")[0], 400)
        bad = copy.deepcopy(RESP); bad["retrieved_hits"][1]["rank"] = 1
        self.assertEqual(parse_query_response(json.dumps(bad).encode())[0], 422)
        unknown = dict(RESP, score=1.0)
        self.assertEqual(parse_query_response(json.dumps(unknown).encode())[0], 422)
        code, model = parse_query_response(json.dumps(RESP).encode())
        self.assertEqual(code, 200)
        self.assertEqual(model.citations[1].hit_id, "h2")

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

    def test_duplicate_claim_and_region_index(self):
        r = copy.deepcopy(RESP); r["answer"]["claims"][1]["claim_id"] = "c1"
        with self.assertRaises(ValidationError):
            vj(QueryResponse, r)
        r = copy.deepcopy(RESP); r["citations"][0]["region_indices"] = [0, 0]
        with self.assertRaises(ValidationError):
            vj(QueryResponse, r)

    def test_limits(self):
        r = copy.deepcopy(RESP); r["answer"]["text"] = "x" * (method.MAX_ANSWER_CHARS + 1)
        with self.assertRaises(ValidationError):
            vj(QueryResponse, r)


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
    def test_lines_are_independent(self):
        # T33: a bad line never pollutes the others.
        bad_rank = copy.deepcopy(LINE); bad_rank["response"]["retrieved_hits"][1]["rank"] = 1
        bad_rank["response"]["question_id"] = "q_002"
        q3 = copy.deepcopy(LINE); q3["response"]["question_id"] = "q_003"
        results = parse_jsonl(jsonl(LINE, "{oops", bad_rank, q3))
        self.assertEqual([(r.line, r.status, r.code) for r, _ in results], [
            (1, "accepted", None), (2, "rejected", "invalid_json"),
            (3, "rejected", "schema_invalid"), (4, "accepted", None)])
        self.assertIsInstance(results[0][1], SubmissionLine)
        self.assertIsNone(results[2][1])

    def test_identical_resend_in_batch_is_idempotent_conflict_rejected(self):
        changed = copy.deepcopy(LINE); changed["response"]["answer"]["text"] = "方案 A。"
        changed["response"]["answer"]["claims"] = []; changed["response"]["citations"] = []
        r = parse_jsonl(jsonl(LINE, LINE, changed))
        self.assertEqual([x.status for x, _ in r], ["accepted", "accepted", "rejected"])
        self.assertEqual(r[0][0].payload_hash, r[1][0].payload_hash)
        self.assertEqual(r[2][0].code, "duplicate_in_batch")

    def test_payload_hash_is_canonical(self):
        a = vj(SubmissionLine, LINE)
        reordered = json.loads(json.dumps(LINE), object_pairs_hook=lambda p: dict(reversed(p)))
        b = vj(SubmissionLine, reordered)
        self.assertEqual(payload_hash(a), payload_hash(b))
        self.assertRegex(payload_hash(a), r"^[0-9a-f]{64}$")

    def test_whole_batch_rejections(self):
        for body, status, code in ((b"\xff\xfe{}\n", 415, "invalid_encoding"),
                                   ("﻿{}\n".encode(), 415, "invalid_encoding"),
                                   (b"", 422, "empty_submission")):
            with self.subTest(code=code), self.assertRaises(method.BatchRejected) as cm:
                parse_jsonl(body)
            self.assertEqual((cm.exception.http_status, cm.exception.code), (status, code))
        with mock.patch.object(method, "MAX_JSONL_BYTES", 10), self.assertRaises(method.BatchRejected) as cm:
            parse_jsonl(jsonl(LINE))
        self.assertEqual(cm.exception.http_status, 413)
        with mock.patch.object(method, "MAX_JSONL_LINES", 1), self.assertRaises(method.BatchRejected) as cm:
            parse_jsonl(jsonl(LINE, LINE))
        self.assertEqual(cm.exception.code, "too_many_lines")

    def test_crlf_and_blank_line(self):
        body = (json.dumps(LINE) + "\r\n\n").encode()
        r = parse_jsonl(body)
        self.assertEqual([(x.status, x.code) for x, _ in r], [("accepted", None), ("rejected", "invalid_json")])


if __name__ == "__main__":
    unittest.main()
