"""Actual completion/failure observations never replace ASGI behavior."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch
from lab_api.app_log_http import AppLogMiddleware
from lab_api.generate_worker import GenModelConfig
from lab_api.generate_worker_main import GenerateWorker
from lab_api.parse_worker import ParseOutcome
from lab_db import jobs


class HttpCompletionTests(unittest.TestCase):
    def exercise(self, app, send_error=None, disconnect=False):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, LAB_APP_LOG="full", LAB_APP_LOG_FILE=d + "/app.log"):
            sent = []
            async def receive():
                return {"type": "http.disconnect" if disconnect else "http.request"}
            async def send(message):
                if send_error is not None and message["type"] == send_error[0]:
                    raise send_error[1]
                sent.append(message)
            caught = None
            try:
                asyncio.run(AppLogMiddleware(app)({"type": "http", "method": "GET"}, receive, send))
            except BaseException as exc:
                caught = exc
            return Path(d + "/app.log").read_text(), caught, sent

    def test_success_waits_for_final_body_and_requested_trailers(self):
        for complete in (False, True):
            for trailers in (False, True):
                with self.subTest(complete=complete, trailers=trailers):
                    async def app(scope, receive, send):
                        await send({"type": "http.response.start", "status": 200, "trailers": trailers})
                        await send({"type": "http.response.body", "body": b"first", "more_body": True})
                        if complete:
                            await send({"type": "http.response.body", "body": b"last"})
                            if trailers:
                                await send({"type": "http.response.trailers", "headers": []})
                    text, caught, _ = self.exercise(app)
                    self.assertIsNone(caught)
                    self.assertEqual("[RETURN]" in text, complete)
                    self.assertEqual("[ERROR]" in text, not complete)
        async def missing_trailers(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "trailers": True})
            await send({"type": "http.response.body", "body": b""})
        text, _, _ = self.exercise(missing_trailers)
        self.assertIn("incomplete_response", text)
        self.assertNotIn("[RETURN]", text)

    def test_partial_stream_failure_send_failures_and_cancellation_preserve_exception(self):
        for kind in ("app", "http.response.start", "http.response.body", "cancel"):
            with self.subTest(kind=kind):
                error = asyncio.CancelledError() if kind == "cancel" else RuntimeError("PRIVATE_EXCEPTION_SENTINEL")
                async def app(scope, receive, send):
                    await send({"type": "http.response.start", "status": 200})
                    await send({"type": "http.response.body", "body": b"first", "more_body": True})
                    raise error
                text, caught, _ = self.exercise(app, (kind, error) if kind.startswith("http.") else None)
                self.assertIs(caught, error)
                self.assertIn("[ERROR]", text)
                self.assertNotIn("[RETURN]", text)
                self.assertNotIn("PRIVATE_EXCEPTION_SENTINEL", text)

    def test_disconnect_is_not_success(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await receive()
        text, caught, _ = self.exercise(app, disconnect=True)
        self.assertIsNone(caught)
        self.assertIn("request_disconnected", text)
        self.assertNotIn("[RETURN]", text)


class PhaseLifetimeTests(unittest.TestCase):
    def test_all_handle_exits_clean_phase_and_success_keeps_real_phase(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = ("validate", None)
        worker = GenerateWorker(lambda: conn, name="synthetic-worker", lab_gen_cmd=["false"],
                                cfg=GenModelConfig("anthropic", "https://example.com", "example.com", 1, 1), api_key="synthetic")
        for kind in ("queued", "abandoned", "lease", "exception", "succeeded"):
            with self.subTest(kind=kind):
                job = uuid.uuid4()
                outcome = ParseOutcome(job, kind, "lease_lost" if kind == "abandoned" else "generated")
                error = jobs.LeaseUnavailable("synthetic") if kind == "lease" else RuntimeError("synthetic") if kind == "exception" else None
                with patch("lab_api.generate_worker_main.process_generate_job", return_value=outcome, side_effect=error):
                    with self.assertLogs("lab.parse_worker", level="INFO") as logs:
                        result = worker._handle(uuid.uuid4(), job)
                self.assertFalse(worker._phases)
                if kind == "succeeded":
                    self.assertIs(result, outcome)
                    self.assertTrue(any("succeeded (validate)" in line for line in logs.output))
