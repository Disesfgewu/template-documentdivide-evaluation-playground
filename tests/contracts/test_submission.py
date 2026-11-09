"""Run-bound JSONL preflight: real serialized inputs, no DB/provider claim."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from lab_contracts import method, submission
from lab_contracts.method import BatchRejected, payload_hash, strict_loads
from lab_contracts.submission import (RunContext, load_context, main,
                                      preflight_jsonl, read_regular)

EX = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "valid"
LINE = json.loads((EX / "SubmissionLine.spec_example.json").read_text())
REQUEST = json.loads((EX / "QueryRequest.spec_example.json").read_text())


def context_dict():
    return {"run_id": LINE["run_id"], "benchmark_revision_id": LINE["benchmark_revision_id"],
            "method_revision_id": LINE["method_revision_id"],
            "documents": {key: {"sha256": value, "page_count": 300}
                          for key, value in LINE["document_hashes"].items()},
            "queries": [{"request": copy.deepcopy(REQUEST), "attempt_id": LINE["attempt_id"]}]}


def context():
    return load_context(json.dumps(context_dict()).encode())


def batch(*values):
    return ("\n".join(v if isinstance(v, str) else json.dumps(v) for v in values) + "\n").encode()


class SubmissionPreflightTests(unittest.TestCase):
    def test_valid_original_payload_hash_and_duplicate(self):
        raw = json.dumps(LINE).replace('0.1,', '0.100000000000000001,')
        rows = preflight_jsonl(batch(raw, raw), context())
        self.assertEqual([r.report.status for r in rows], ["validated", "validated"])
        self.assertEqual(rows[1].report.duplicate_of, 1)
        self.assertEqual(rows[0].report.payload_hash, payload_hash(strict_loads(raw)))
        self.assertNotEqual(rows[0].report.payload_hash, payload_hash(LINE))
        self.assertEqual(rows[0].submission.response.answer.text, LINE["response"]["answer"]["text"])

    def test_invalid_first_line_cannot_select_later_binding_or_duplicate(self):
        bad = copy.deepcopy(LINE); bad["run_id"] = "wrong"
        rows = preflight_jsonl(batch(bad, LINE, LINE), context())
        self.assertEqual([r.report.status for r in rows], ["rejected", "validated", "validated"])
        self.assertEqual(rows[0].report.reason_codes, ["run_id_mismatch"])
        self.assertIsNone(rows[0].submission)
        self.assertEqual(rows[2].report.duplicate_of, 2)

    def test_each_identity_binding(self):
        for field in ("run_id", "benchmark_revision_id", "method_revision_id", "attempt_id"):
            with self.subTest(field=field):
                bad = copy.deepcopy(LINE); bad[field] = "wrong"
                row = preflight_jsonl(batch(bad), context())[0]
                self.assertIn(field + "_mismatch", row.report.reason_codes)
        for field in ("request_id", "index_id"):
            with self.subTest(field=field):
                bad = copy.deepcopy(LINE); bad["response"][field] = "wrong"
                self.assertIn(field + "_mismatch", preflight_jsonl(batch(bad), context())[0].report.reason_codes)
        bad = copy.deepcopy(LINE); bad["response"]["question_id"] = "wrong"
        self.assertEqual(preflight_jsonl(batch(bad), context())[0].report.reason_codes, ["unknown_question"])
        for hashes in ({"other": "a" * 64}, {**LINE["document_hashes"], "extra": "a" * 64},
                       {key: "b" * 64 for key in LINE["document_hashes"]}):
            bad = copy.deepcopy(LINE); bad["document_hashes"] = hashes
            self.assertIn("document_hashes_mismatch", preflight_jsonl(batch(bad), context())[0].report.reason_codes)

    def test_multiple_attempts_have_individual_request_bindings(self):
        metadata = context_dict()
        second = copy.deepcopy(metadata["queries"][0])
        second["attempt_id"] = "attempt-2"
        second["request"]["request_id"] = "request-2"
        second["request"]["index_id"] = "index-2"
        metadata["queries"].append(second)
        c = load_context(json.dumps(metadata).encode())
        line = copy.deepcopy(LINE); line["attempt_id"] = "attempt-2"
        wrong = preflight_jsonl(batch(line), c)[0]
        self.assertEqual(wrong.report.reason_codes, ["request_id_mismatch", "index_id_mismatch"])
        line["response"].update(request_id="request-2", index_id="index-2")
        rows = preflight_jsonl(batch(LINE, line), c)
        self.assertEqual([r.report.status for r in rows], ["validated", "validated"])
        self.assertTrue(all(r.report.duplicate_of is None for r in rows))

    def test_invalid_sources_preserve_answer_hits_citations_and_hash(self):
        bad = copy.deepcopy(LINE)
        regions = bad["response"]["retrieved_hits"][0]["regions"]
        regions[0]["page_index"] = 300
        regions[0]["document_sha256"] = "f" * 64
        regions.append({**regions[0], "document_id": "unknown"})
        row = preflight_jsonl(batch(bad), context())[0]
        self.assertEqual(row.report.status, "validated")
        self.assertEqual(row.report.invalid_regions[0].model_dump(),
                         {"hit_id": "h1", "region_index": 0,
                          "reason_codes": ["document_sha256_mismatch", "page_index_out_of_range"]})
        self.assertEqual(row.report.invalid_regions[1].reason_codes, ["unknown_document"])
        self.assertEqual(row.submission.model_dump(mode="json"),
                         method.SubmissionLine.model_validate_json(json.dumps(bad)).model_dump(mode="json"))
        self.assertEqual(row.report.payload_hash, payload_hash(bad))
        good = copy.deepcopy(LINE)
        good["response"]["retrieved_hits"][0]["regions"][0]["page_index"] = 0
        good["response"]["retrieved_hits"][1]["regions"][0]["page_index"] = 299
        self.assertEqual(preflight_jsonl(batch(good), context())[0].report.invalid_regions, [])

    def test_conflict_after_valid_does_not_replace_first(self):
        bad = copy.deepcopy(LINE); bad["response"]["answer"]["text"] = "different answer"
        rows = preflight_jsonl(batch(LINE, bad, LINE), context())
        self.assertEqual(rows[1].report.reason_codes, ["duplicate_in_batch"])
        self.assertEqual(rows[2].report.duplicate_of, 1)
        self.assertIsNone(rows[1].submission)

    def test_absent_versus_explicit_null_remains_hash_conflict(self):
        first = copy.deepcopy(LINE); first["response"].pop("reason", None)
        second = copy.deepcopy(first); second["response"]["reason"] = None
        rows = preflight_jsonl(batch(first, second), context())
        self.assertEqual(rows[0].report.status, "validated")
        self.assertEqual(rows[1].report.reason_codes, ["duplicate_in_batch"])
        self.assertNotEqual(rows[0].report.payload_hash, rows[1].report.payload_hash)

    def test_line_errors_isolated_and_count_physical_lines(self):
        bad = copy.deepcopy(LINE); bad["response"]["citations"][0]["hit_id"] = "missing"
        rows = preflight_jsonl(batch("{", "", "{}", bad, LINE), context())
        self.assertEqual([r.report.line for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual([r.report.reason_codes for r in rows],
                         [["invalid_json"], ["invalid_json"], ["schema_invalid"], ["schema_invalid"], []])
        self.assertTrue(rows[-1].submission)
        duplicate_key = json.dumps(LINE)[:-1] + ',"run_id":"other"}'
        self.assertEqual(preflight_jsonl(batch(duplicate_key), context())[0].report.reason_codes, ["invalid_json"])

    def test_escaped_lone_surrogate_isolated(self):
        bad = json.dumps(LINE).replace('"text": "', '"text": "\\ud800', 1)
        rows = preflight_jsonl(batch(bad, LINE), context())
        self.assertEqual(rows[0].report.status, "rejected")
        self.assertEqual(rows[1].report.status, "validated")

    def test_embedded_bom_rejected_per_line_and_crlf_preserved(self):
        rows = preflight_jsonl(batch(LINE, "\ufeff{}", LINE), context())
        self.assertEqual([r.report.status for r in rows], ["validated", "rejected", "validated"])
        self.assertEqual(rows[1].report.reason_codes, ["invalid_json"])
        self.assertEqual(rows[2].report.duplicate_of, 1)
        self.assertEqual(preflight_jsonl(batch(LINE).replace(b"\n", b"\r\n"), context())[0].report.status,
                         "validated")

    def test_batch_bounds_encoding_and_line_count(self):
        for body, code in ((b"\xff", "invalid_encoding"), (b"\xef\xbb\xbf{}", "invalid_encoding"),
                           (b"", "empty_submission"), (b"{}\n" * 101, "too_many_lines")):
            with self.subTest(code=code), self.assertRaises(BatchRejected) as caught:
                preflight_jsonl(body, context())
            self.assertEqual(caught.exception.code, code)
        with mock.patch.object(method, "MAX_JSONL_BYTES", len(batch(LINE))):
            self.assertEqual(preflight_jsonl(batch(LINE), context())[0].report.status, "validated")
            with self.assertRaises(BatchRejected):
                preflight_jsonl(batch(LINE) + b" ", context())
        with mock.patch.object(method, "MAX_JSONL_LINE_BYTES", 2):
            self.assertEqual(preflight_jsonl(batch(LINE), context())[0].report.reason_codes, ["line_too_large"])

    def test_strict_context_and_revalidation_of_mutated_nested_context(self):
        for mutation in (lambda c: c.update(extra="secret"),
                         lambda c: c["queries"].append(c["queries"][0]),
                         lambda c: c["queries"][0]["request"].update(run_id="wrong"),
                         lambda c: c["documents"][next(iter(c["documents"]))].update(page_count=True)):
            c = context_dict(); mutation(c)
            with self.assertRaisesRegex(ValueError, "invalid_context"):
                load_context(json.dumps(c).encode())
        raw = json.dumps(context_dict())[:-1] + ',"run_id":"wrong"}'
        with self.assertRaisesRegex(ValueError, "invalid_context"):
            load_context(raw.encode())
        c = context(); c.queries.clear()
        with self.assertRaisesRegex(ValueError, "invalid_context"):
            preflight_jsonl(batch(LINE), c)


class SubmissionCLITests(unittest.TestCase):
    def test_regular_read_cap_and_reject_special_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "file"; path.write_bytes(b"abc")
            self.assertEqual(read_regular(str(path), 3), b"abc")
            with self.assertRaisesRegex(ValueError, "input_too_large"):
                read_regular(str(path), 2)
            fifo = Path(d) / "pipe"; os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "input_not_regular_file"):
                read_regular(str(fifo), 100)
            with self.assertRaisesRegex(ValueError, "input_not_regular_file"):
                read_regular("/dev/zero", 100)

    def test_checked_in_context_example_and_report_cap(self):
        example = EX.parent / "internal" / "SubmissionRunContext.json"
        self.assertEqual(load_context(example.read_bytes()), context())
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "input.jsonl"; path.write_bytes(batch(LINE))
            with mock.patch.object(submission, "MAX_REPORT_BYTES", 5), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--context", str(example), "--input", str(path)]), 2)
            self.assertEqual(json.loads(output.getvalue()), {"status": "rejected_batch", "code": "report_too_large"})

    def test_cli_no_write_and_no_payload_in_report(self):
        with tempfile.TemporaryDirectory() as d:
            cpath = Path(d) / "context.json"; cpath.write_text(json.dumps(context_dict()))
            ipath = Path(d) / "submission.jsonl"; ipath.write_bytes(batch(LINE))
            proc = subprocess.run([sys.executable, "-m", "lab_contracts.submission", "--context", str(cpath),
                                   "--input", str(ipath)], capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["persisted"])
            self.assertEqual((result["validated"], result["rejected"]), (1, 0))
            self.assertNotIn(LINE["response"]["answer"]["text"], proc.stdout)
            self.assertEqual(ipath.read_bytes(), batch(LINE))
            ipath.write_bytes(batch("bad", LINE))
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--context", str(cpath), "--input", str(ipath)]), 1)
            self.assertEqual(json.loads(output.getvalue())["rejected"], 1)
            cpath.write_text('{"secret":"DO_NOT_ECHO"}')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--context", str(cpath), "--input", str(ipath)]), 2)
            self.assertEqual(json.loads(output.getvalue()),
                             {"status": "rejected_batch", "code": "invalid_input_or_context"})
            self.assertNotIn("DO_NOT_ECHO", output.getvalue())


if __name__ == "__main__":
    unittest.main()
