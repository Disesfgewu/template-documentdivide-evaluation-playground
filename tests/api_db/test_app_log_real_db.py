"""Actual PostgreSQL/fake-child workflow logging; no provider payload is published."""
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from tests.api_db import test_external_submission_real_db as ext


@unittest.skipUnless(ext.ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class AppLogFlowTests(unittest.TestCase):
    def test_external_flow_logs_real_stages_and_quote_failures(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, LAB_APP_LOG="full", LAB_APP_LOG_FILE=d + "/app.log"):
            ext.ExternalSubmissionRealDbTests.setUpClass()
            self.addCleanup(ext.ExternalSubmissionRealDbTests.tearDownClass)
            case = ext.ExternalSubmissionRealDbTests("test_the_whole_flow_export_to_answers_to_scores_with_fabricated_missing_and_abstained_questions")
            result = unittest.TestResult()
            case.run(result)
            ext.ExternalSubmissionRealDbTests("test_the_questions_package_has_only_ids_text_and_language_and_the_query_never_touches_gold").run(result)
            ext.ExternalSubmissionRealDbTests("test_the_judge_scores_an_uploaded_file_from_its_answer_and_its_submitted_passages").run(result)
            self.assertTrue(result.wasSuccessful(), str(result.errors + result.failures))
            self.assertFalse(result.skipped)
            text = Path(d + "/app.log").read_text()
            for line in text.splitlines():
                self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z \S+ \S+ \[(CALL|RETURN|OP|ALARM|ERROR)\] FUNCTION:\{")
            for stage in ("promote", "plan", "generate", "validate", "freeze", "export", "external_submission", "rule_scoring", "judge", "results", "api"):
                for mode in ("CALL", "RETURN"):
                    self.assertTrue(any(f"[{mode}]" in line and f"stage='{stage}'" in line for line in text.splitlines()), (stage, mode))
            # PM-DECISION #191: every function on the path of validation, rule scoring, quotes, judge and the freeze gate is logged
            for fn in ("code_checks", "verdict_for", "unit_hit", "quote_located", "validate_line", "build_judge_call", "check_verdict_ex",
                       "derive", "freeze_gate", "score_rule_metrics"):
                self.assertIn(fn, text, fn)
            self.assertIn("app_log_start", text.splitlines()[0])
            self.assertRegex(text, r"api_request.*\{Return: \{'status_code': \d+, 'duration_ms': \d+\}\}")
            self.assertRegex(text, r"<\d+ chars sha256:[0-9a-f]{16}>'")      # owner data: length + hash + a bounded preview
            self.assertIn("quote_not_found", text)
            self.assertIn("judge_source_filter", text)
            self.assertNotIn("postgresql://", text)
