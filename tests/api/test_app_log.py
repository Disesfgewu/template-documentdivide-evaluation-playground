"""Tests for operational logging (Issue #191, Refs #14, #1).

Verifies:
- Line format regex on every line:
  ISO-8601 UTC timestamp, service name, context id, [MODE], FUNCTION:{signature}, status text, optional {Return: ...}
- Every stage name appears at least once with CALL and RETURN
- An injected failure produces ERROR with the reason code
- ALARM for a not-found quote and for a dropped reply
- Redaction test (key, token, DB URL absent)
- Bounded-value test (strings > 300 chars, bytes, dicts/lists)
- LAB_APP_LOG=off writes nothing
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts", _REPO / "scoring"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api import app_log
from lab_api.app_log import (
    ContextScope,
    app_traced,
    bound_value,
    format_log_line,
    get_log_path,
    log_alarm,
    log_error,
    log_event,
    log_op,
)

LOG_LINE_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z "
    r"[a-zA-Z0-9_-]+ "
    r"[a-zA-Z0-9_=,.-]+ "
    r"\[(CALL|RETURN|OP|ALARM|ERROR)\] "
    r"FUNCTION:\{[a-zA-Z0-9_.]+\(.*\)\} "
    r"[^{]+?"
    r"(?: \{Return: .*\})?$"
)

STAGES = [
    "upload",
    "parse",
    "promote",
    "plan",
    "generate",
    "validate",
    "exclusion",
    "freeze",
    "export",
    "external_submission",
    "rule_scoring",
    "judge",
    "results",
    "api",
]


class AppLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.tmp_dir, "test_app.log")
        os.environ["LAB_APP_LOG"] = "full"
        os.environ["LAB_APP_LOG_FILE"] = self.log_file
        os.environ["LAB_SERVICE_NAME"] = "generate-worker"

    def tearDown(self):
        os.environ.pop("LAB_APP_LOG", None)
        os.environ.pop("LAB_APP_LOG_FILE", None)
        os.environ.pop("LAB_SERVICE_NAME", None)
        if os.path.exists(self.log_file):
            try:
                os.remove(self.log_file)
            except OSError:
                pass

    def read_lines(self) -> list[str]:
        if not os.path.exists(self.log_file):
            return []
        with open(self.log_file, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def test_line_format_regex_matches_all_modes(self):
        with ContextScope(service="generate-worker", job_id="4718-test"):
            log_event("CALL", "process_generate_job", "started", stage="generate",
                      project_id="proj-1", job_id="4718-test", phase="validate")
            log_event("RETURN", "code_checks", "ok", return_val={"passed": True, "fact_coverage": [1.0, 0.83]},
                      stage="validate", shown_text="some text", reference_answer="ref", facts=[2])
            log_op("reserve_batch", status="reserved", count=5, stage="generate")
            log_alarm("locate", "quote not found on page 2: 'fee is 100 EUR'", stage="external_submission")
            log_error("run_lab_gen", "RuntimeError", "child_crashed", stage="generate")

        lines = self.read_lines()
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertRegex(line, LOG_LINE_REGEX, f"Line does not match fixed format: {line}")

    def test_every_stage_name_appears_with_call_and_return(self):
        with ContextScope(service="api", request_id="req-123"):
            for stage in STAGES:
                @app_traced(stage=stage, is_stage=True)
                def stage_func(dummy="val"):
                    return {"stage_done": stage}

                stage_func()

        lines = self.read_lines()
        self.assertGreater(len(lines), 0)

        for line in lines:
            self.assertRegex(line, LOG_LINE_REGEX)

        for stage in STAGES:
            with self.subTest(stage=stage):
                has_call = any(f"stage={stage!r}" in line and "[CALL]" in line for line in lines)
                has_return = any(f"stage={stage!r}" in line and "[RETURN]" in line for line in lines)
                self.assertTrue(has_call, f"Stage {stage} missing [CALL] in lines: {lines}")
                self.assertTrue(has_return, f"Stage {stage} missing [RETURN] in lines: {lines}")

    def test_injected_failure_produces_error_with_reason_code(self):
        class CustomError(Exception):
            code = "injected_failure_code"

        @app_traced(stage="validate")
        def failing_function():
            raise CustomError("something went wrong")

        with self.assertRaises(CustomError):
            with ContextScope(service="generate-worker", job_id="fail-1"):
                failing_function()

        lines = self.read_lines()
        self.assertTrue(any("[ERROR]" in l and "CustomError: injected_failure_code" in l for l in lines), lines)
        for line in lines:
            self.assertRegex(line, LOG_LINE_REGEX)

    def test_alarm_for_not_found_quote_and_dropped_reply(self):
        from lab_api import external_submission
        from lab_api.generate_questions import GenerationStats

        # 1. Not found quote
        external_submission.locate("Invented quote text never in document", 1, {1: []})

        # 2. Dropped reply
        stats = GenerationStats()
        stats.drop("check:contract_invalid")

        lines = self.read_lines()
        self.assertTrue(any("[ALARM]" in l and "quote_not_found" in l for l in lines), lines)
        self.assertTrue(any("[ALARM]" in l and "reply dropped: check:contract_invalid" in l for l in lines), lines)
        for line in lines:
            self.assertRegex(line, LOG_LINE_REGEX)

    def test_redaction_secrets_absent(self):
        secret_key = "sk-" + "mockkey1234567890abcdef1234567890"
        bearer_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.abcdef"
        db_url = "postgresql://lab:secretpass123@db:5432/lab"
        auth_header = "Authorization: Bearer my_secret_token"

        @app_traced(stage="generate")
        def func_with_secrets(api_key: str, token: str, url: str, auth: str, payload_text: str):
            return {"db_url": url, "auth": auth}

        func_with_secrets(api_key=secret_key, token=bearer_token, url=db_url,
                          auth=auth_header, payload_text=f"Connecting to {db_url} with {secret_key}")

        lines = self.read_lines()
        full_text = "\n".join(lines)

        self.assertNotIn(secret_key, full_text)
        self.assertNotIn("secretpass123", full_text)
        self.assertNotIn("my_secret_token", full_text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", full_text)
        self.assertIn("[REDACTED]", full_text)

    def test_bounded_value_truncation(self):
        # Long string > 300 chars
        long_str = "x" * 500
        bounded_str = bound_value(long_str)
        self.assertIn("<500 chars sha256:", bounded_str)
        self.assertLessEqual(len(bounded_str), 200)

        # PDF / raw bytes
        raw_bytes = b"%PDF-1.4 " + (b"0" * 1000)
        bounded_bytes = bound_value(raw_bytes)
        self.assertIn("<1009 bytes sha256:", bounded_bytes)
        self.assertNotIn("00000000", bounded_bytes)

        # Large dict
        large_dict = {f"k{i}": i for i in range(50)}
        bounded_dict = bound_value(large_dict)
        self.assertIn("len=50", bounded_dict)

        # Large list
        large_list = list(range(50))
        bounded_list = bound_value(large_list)
        self.assertIn("len=50", bounded_list)

    def test_lab_app_log_off_writes_nothing(self):
        os.environ["LAB_APP_LOG"] = "off"
        if os.path.exists(self.log_file):
            os.remove(self.log_file)

        @app_traced(stage="upload", is_stage=True)
        def dummy_upload():
            return "done"

        dummy_upload()
        log_op("op_test", "ok")
        log_alarm("alarm_test", "something")
        log_error("err_test", "Err", "code")

        self.assertFalse(os.path.exists(self.log_file) and os.path.getsize(self.log_file) > 0)


if __name__ == "__main__":
    unittest.main()
