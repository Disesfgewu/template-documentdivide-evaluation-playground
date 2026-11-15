"""Safety regressions for actual failures found in the first #191 implementation."""
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_api import app_log as log
from lab_api.generate_worker import CallSpec


def writer(path, who):
    os.environ.update(LAB_APP_LOG="full", LAB_APP_LOG_FILE=path)
    log.MAX_BYTES = 2000
    for n in range(50):
        log.log_op("concurrent_write", writer=who, sequence=n)


class SafetyTests(unittest.TestCase):
    def test_short_provider_text_and_prompt_objects_never_appear(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, LAB_APP_LOG="full", LAB_APP_LOG_FILE=d + "/app.log"):
            @log.app_traced(stage="generate")
            def build(text):
                return [CallSpec(id="call-1", system="PRIVATE_SYSTEM_PROMPT", user="PRIVATE_USER_PROMPT")]
            build("PRIVATE_SHORT_REPLY")
            log.log_op("unsafe_values", payload={"dsn": "postgresql://lab:pass@host/db", "nested": {"text": "PRIVATE_DOC_TEXT"}},
                       value="postgresql://lab:pass@host/db")
            text = Path(d + "/app.log").read_text()
            for secret in ("PRIVATE_SYSTEM_PROMPT", "PRIVATE_USER_PROMPT", "PRIVATE_SHORT_REPLY", "PRIVATE_DOC_TEXT", "postgresql://"):
                self.assertNotIn(secret, text)
            self.assertIn("sha256:", text)

    def test_logging_failure_does_not_replace_result_or_exception(self):
        class NoRepr:
            def __repr__(self):
                raise AssertionError("do not call repr")
        @log.app_traced(stage="validate")
        def okay(value):
            return value
        value = NoRepr()
        with patch.dict(os.environ, LAB_APP_LOG="full", LAB_APP_LOG_FILE="/dev/null/cannot-write"):
            before = log.get_log_failures()
            self.assertIs(okay(value), value)
            self.assertGreater(log.get_log_failures(), before)
            error = RuntimeError("private exception message")
            @log.app_traced(stage="validate")
            def fail():
                raise error
            with self.assertRaises(RuntimeError) as caught:
                fail()
            self.assertIs(caught.exception, error)

    def test_off_does_not_serialize_or_touch_disk(self):
        @log.app_traced(stage="validate")
        def okay(value):
            return value
        with patch.dict(os.environ, LAB_APP_LOG="off"), patch.object(log, "bound_value", side_effect=AssertionError), patch.object(log, "_write_log_line", side_effect=AssertionError):
            self.assertEqual(okay("x"), "x")

    def test_rotation_is_shared_across_processes_and_lines_are_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + "/app.log"
            procs = [multiprocessing.get_context("fork").Process(target=writer, args=(path, n)) for n in range(3)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(10)
                self.assertEqual(p.exitcode, 0)
            files = [p for p in Path(d).glob("app.log*") if not p.name.endswith(".lock")]
            self.assertLessEqual(len(files), 6)
            for p in files:
                self.assertEqual(p.stat().st_mode & 0o007, 0)
                for line in p.read_text().splitlines():
                    self.assertEqual(line.count("FUNCTION:"), 1)
                    self.assertIn("[OP]", line)
                    self.assertIn("concurrent_write", line)

    def test_finished_generation_worker_reports_the_actual_phase(self):
        from lab_api.generate_worker_main import GenerateWorker
        from lab_api.parse_worker import ParseOutcome
        import uuid
        worker = GenerateWorker.__new__(GenerateWorker)
        for phase in ("generate", "validate", "answer", "judge"):
            job = uuid.uuid4()
            worker._phases = {job: phase}
            reason = worker._outcome_log_reason(job, ParseOutcome(job, "succeeded", "generated"))
            self.assertEqual(reason, "generated" if phase == "generate" else phase)
            self.assertFalse(worker._phases)

    def test_page_status_logging_is_observational_even_with_an_incomplete_test_double(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, LAB_APP_LOG="full", LAB_APP_LOG_FILE=d + "/app.log"):
            before = log.get_log_failures()
            log.log_page_statuses(SimpleNamespace())
            self.assertGreater(log.get_log_failures(), before)
            log.log_page_statuses(SimpleNamespace(result=SimpleNamespace(pages=[SimpleNamespace(page_index=2, status="partial", diagnostics=[])])))
            text = Path(d + "/app.log").read_text()
            self.assertIn("parse_page", text)
            self.assertIn("page_index=2", text)
            self.assertIn("partial", text)
