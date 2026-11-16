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
    def exercise(self, app, send_error=None, disconnect=False, path=None):
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
                asyncio.run(AppLogMiddleware(app)({"type": "http", "method": "GET", **({"path": path} if path else {})}, receive, send))
            except BaseException as exc:
                caught = exc
            f = Path(d + "/app.log")
            return (f.read_text() if f.exists() else ""), caught, sent

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


class HealthProbeNoiseTests(unittest.TestCase):
    """PM on #191: a success of /healthz or /readyz every few seconds is not logged; a state change is. Every response carries the logged id."""

    def run_probes(self, path, statuses):
        import lab_api.app_log_http as mod
        mod._last_probe_ok.clear()
        helper = HttpCompletionTests()
        lines = []
        for st in statuses:
            async def app(scope, receive, send, st=st):
                await send({"type": "http.response.start", "status": st})
                await send({"type": "http.response.body", "body": b"{}"})
            text, _, _ = helper.exercise(app, path=path)
            lines.append(text)
        return lines

    def test_successful_probes_write_nothing(self):
        for path in ("/healthz", "/readyz"):
            for text in self.run_probes(path, [200, 200, 200]):
                self.assertNotIn("api_request", text)

    def test_only_a_state_change_is_logged(self):
        texts = self.run_probes("/readyz", [200, 503, 503, 200, 200])
        self.assertEqual(["api_request" in t for t in texts], [False, True, False, True, False])
        self.assertIn("probe_failing", texts[1])
        self.assertIn("probe_recovered", texts[3])

    def test_other_routes_are_still_logged_with_call_and_return(self):
        text = self.run_probes("/api/v1/documents", [200])[0]
        self.assertIn("[CALL]", text)
        self.assertIn("[RETURN]", text)

    def test_the_response_carries_the_id_that_is_in_the_log(self):
        helper = HttpCompletionTests()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b"{}"})
        text, _, sent = helper.exercise(app, path="/api/v1/x")
        headers = dict(sent[0]["headers"])
        rid = headers[b"x-lab-request-id"].decode()
        self.assertRegex(rid, r"^req-[0-9a-f]{32}$")
        self.assertIn(rid, text)
        self.assertEqual(headers[b"content-type"], b"application/json")
