"""Bounded, cancellation-safe blocking work and admission for document I/O
(PM review on #73: event-loop blocking, cancelled worker, unbounded queue)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import anyio
from fastapi.testclient import TestClient

from lab_api import documents
from lab_api.documents import ApiError


def fake_request(**state) -> types.SimpleNamespace:
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(**state)))


def wait_in_thread(event: threading.Event, timeout: float = 5.0) -> bool:
    return event.wait(timeout)


class BlockingWorkTests(unittest.IsolatedAsyncioTestCase):
    async def start_blocked_job(self, req):
        started, release, log = threading.Event(), threading.Event(), []

        def worker():
            started.set()
            release.wait(5)
            log.append("worker-finished")
            return "done"

        task = asyncio.ensure_future(documents._run_blocking(req, "upload", worker))
        await asyncio.get_running_loop().run_in_executor(None, wait_in_thread, started)
        self.assertTrue(started.is_set())
        return task, release, log

    async def test_event_loop_stays_responsive_during_blocking_work(self) -> None:
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.ensure_future(ticker())
        try:
            await documents._run_blocking(fake_request(), "upload", lambda: threading.Event().wait(0.4))
        finally:
            t.cancel()
        self.assertGreater(ticks, 10, "the event loop was blocked while the worker ran")

    async def test_native_task_cancel_waits_for_the_worker(self) -> None:
        """The first attempt (run_in_executor) returned on cancel while the
        worker still held the request's connection."""
        task, release, log = await self.start_blocked_job(fake_request())
        task.cancel()
        await asyncio.sleep(0.1)
        self.assertFalse(task.done(), "cancellation must wait for the running worker")
        self.assertEqual(log, [])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(log, ["worker-finished"], "the worker must have finished before the cancel completed")

    async def test_anyio_cancel_scope_waits_for_the_worker(self) -> None:
        req = fake_request()
        started, release, log = threading.Event(), threading.Event(), []

        def worker():
            started.set()
            release.wait(5)
            log.append("worker-finished")

        async def run():
            with anyio.move_on_after(0.1) as scope:
                await documents._run_blocking(req, "upload", worker)
            return scope.cancelled_caught

        task = asyncio.ensure_future(run())
        await asyncio.get_running_loop().run_in_executor(None, wait_in_thread, started)
        await asyncio.sleep(0.3)  # the deadline has passed
        self.assertFalse(task.done(), "the deadline must not abandon a running worker")
        release.set()
        self.assertTrue(await task)
        self.assertEqual(log, ["worker-finished"])

    async def test_worker_error_reaches_the_caller_and_cancelled_errors_are_retrieved(self) -> None:
        def boom():
            raise ApiError(409, "conflict", "x")

        with self.assertRaises(ApiError):
            await documents._run_blocking(fake_request(), "meta", boom)
        # cancelled while the worker fails: no "exception was never retrieved"
        req = fake_request()
        started, release = threading.Event(), threading.Event()

        def late_boom():
            started.set()
            release.wait(5)
            raise ApiError(500, "internal_error", "late")

        task = asyncio.ensure_future(documents._run_blocking(req, "upload", late_boom))
        await asyncio.get_running_loop().run_in_executor(None, wait_in_thread, started)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_thread_pool_is_bounded_per_pool(self) -> None:
        req = fake_request(upload_concurrency=2)
        running, peak, lock = 0, 0, threading.Lock()

        def work():
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            threading.Event().wait(0.1)
            with lock:
                running -= 1

        await asyncio.gather(*(documents._run_blocking(req, "upload", work) for _ in range(6)))
        self.assertEqual(peak, 2)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_slots_are_refused_without_waiting_and_released(self) -> None:
        req = fake_request(upload_concurrency=1)
        first = documents._take_slot(req, "upload", 2, "uploads")
        with self.assertRaises(ApiError) as ctx:
            documents._take_slot(req, "upload", 2, "uploads")
        self.assertEqual(
            (ctx.exception.status_code, ctx.exception.code, ctx.exception.retryable), (503, "server_busy", True)
        )
        first.release()
        documents._take_slot(req, "upload", 2, "uploads").release()

    async def test_invalid_configured_concurrency_falls_back_to_the_default(self) -> None:
        for bad in (0, -1, True, "4", None, 1.5):
            with self.subTest(bad=bad):
                req = fake_request(upload_concurrency=bad)
                held = [documents._take_slot(req, "upload", 2, "uploads") for _ in range(2)]
                with self.assertRaises(ApiError):
                    documents._take_slot(req, "upload", 2, "uploads")
                for h in held:
                    h.release()

    async def test_upload_and_download_have_separate_budgets(self) -> None:
        req = fake_request(upload_concurrency=1, download_concurrency=1)
        up = documents._take_slot(req, "upload", 2, "uploads")
        down = documents._take_slot(req, "download", 2, "downloads")
        up.release()
        down.release()


class UploadEndpointCase(unittest.TestCase):
    """Shared app wiring for the endpoint tests below (no tests of its own)."""

    def setUp(self) -> None:
        from lab_api.config import load_settings
        from lab_api.documents import get_authenticated_actor, get_db, get_store
        from lab_api.main import create_app

        env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": "postgresql://lab:s3cret@127.0.0.1:5432/lab",
            "LAB_ARTIFACT_ROOT": "/tmp/test-artifacts",
            "LAB_DB_CONNECT_TIMEOUT_S": "2",
            "LAB_AUTH_SECRET": "secret-signing-key-for-tests-only-32b",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.app = create_app(settings, checks={})
        self.app.state.upload_concurrency = 1
        self.project = uuid.uuid4()
        self.app.dependency_overrides[get_db] = lambda: MagicMock()
        self.app.dependency_overrides[get_store] = lambda: MagicMock()
        self.app.dependency_overrides[get_authenticated_actor] = lambda: uuid.uuid4()
        self.pdf = b"%PDF-1.7 tiny"
        self.url = f"/api/v1/projects/{self.project}/documents"


class UploadEndpointAdmissionTests(UploadEndpointCase):
    """End to end: a busy server refuses before reading the body, and the
    slot is released after both success and failure."""

    def test_busy_server_refuses_before_reading_the_body_then_recovers(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def slow_core(conn, store, ctx, raw):
            entered.set()
            release.wait(5)
            return 201, {"ok": True}

        first_result: dict = {}

        def first():
            with mock.patch.object(documents, "_authorize_upload", lambda *a: None), \
                    mock.patch.object(documents, "_upload_core", slow_core):
                first_result["r"] = TestClient(self.app).post(
                    self.url, content=self.pdf, headers={"Content-Type": "application/pdf"}
                )

        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(entered.wait(5), "the first upload never reached the worker")
        try:
            read_calls = []
            real_read = documents.read_upload_body

            async def spy(request, max_bytes):
                read_calls.append(1)
                return await real_read(request, max_bytes)

            with mock.patch.object(documents, "_authorize_upload", lambda *a: None), \
                    mock.patch.object(documents, "read_upload_body", spy):
                busy = TestClient(self.app).post(
                    self.url, content=self.pdf, headers={"Content-Type": "application/pdf"}
                )
            self.assertEqual(busy.status_code, 503)
            err = busy.json()["error"]
            self.assertEqual((err["code"], err["retryable"]), ("server_busy", True))
            self.assertEqual(read_calls, [], "a refused request's body must not be read")
        finally:
            release.set()
            t.join(10)
        self.assertEqual(first_result["r"].status_code, 201)

        # The slot came back: a new request is admitted.
        with mock.patch.object(documents, "_authorize_upload", lambda *a: None), \
                mock.patch.object(documents, "_upload_core", lambda *a: (201, {"ok": True})):
            again = TestClient(self.app).post(self.url, content=self.pdf, headers={"Content-Type": "application/pdf"})
        self.assertEqual(again.status_code, 201)

    def test_slot_is_released_when_the_worker_raises_or_the_body_is_rejected(self) -> None:
        def failing_core(conn, store, ctx, raw):
            raise ApiError(409, "conflict", "x")

        with mock.patch.object(documents, "_authorize_upload", lambda *a: None):
            with mock.patch.object(documents, "_upload_core", failing_core):
                for _ in range(3):  # would exhaust a single slot if it leaked
                    r = TestClient(self.app).post(self.url, content=self.pdf, headers={"Content-Type": "application/pdf"})
                    self.assertEqual(r.status_code, 409)
            for _ in range(3):
                r = TestClient(self.app).post(self.url, content=b"{}", headers={"Content-Type": "application/json"})
                self.assertEqual(r.status_code, 415)

    def test_unauthorized_request_takes_no_slot_and_reads_no_body(self) -> None:
        def deny(*a):
            raise ApiError(403, "permission_denied", "no")

        read_calls = []

        async def spy(request, max_bytes):
            read_calls.append(1)
            raise AssertionError("body must not be read")

        with mock.patch.object(documents, "_authorize_upload", deny), mock.patch.object(documents, "read_upload_body", spy):
            for _ in range(3):
                r = TestClient(self.app).post(self.url, content=self.pdf, headers={"Content-Type": "application/pdf"})
                self.assertEqual(r.status_code, 403)
        self.assertEqual(read_calls, [])


def asgi_request(body: bytes, content_type: str, *, state=None, chunk: int = 7):
    """A real starlette Request over a canned ASGI body."""
    from starlette.requests import Request

    pending = [body[i : i + chunk] for i in range(0, len(body), chunk)] or [b""]

    async def receive():
        part = pending.pop(0)
        return {"type": "http.request", "body": part, "more_body": bool(pending)}

    scope = {
        "type": "http", "method": "POST", "path": "/x", "query_string": b"",
        "headers": [(b"content-type", content_type.encode())],
        "app": types.SimpleNamespace(state=types.SimpleNamespace(**(state or {}))),
    }
    return Request(scope, receive)


class BodyReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_kinds_accumulate_in_a_bytearray_and_parse_to_bytes(self) -> None:
        pdf = b"%PDF-1.7 abcdefghij"
        raw = await documents.read_upload_body(asgi_request(pdf, "application/pdf"), 1000)
        self.assertEqual((raw.kind, type(raw.body)), ("pdf", bytearray))
        self.assertEqual(documents.parse_upload(raw, 1000), (pdf, None))
        self.assertIs(type(documents.parse_upload(raw, 1000)[0]), bytes)

        multipart = (b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n"
                     b"Content-Type: application/pdf\r\n\r\n" + pdf + b"\r\n--B--\r\n")
        raw = await documents.read_upload_body(asgi_request(multipart, "multipart/form-data; boundary=B"), 1000)
        self.assertEqual((raw.kind, type(raw.body)), ("multipart", bytearray))
        self.assertEqual(documents.parse_upload(raw, 1000), (pdf, None))

    async def test_a_body_that_never_finishes_loses_its_slot_with_408(self) -> None:
        async def never(*args, **kwargs):
            await asyncio.sleep(30)

        req = asgi_request(b"x", "application/pdf", state={"upload_read_timeout_s": 0.05})
        with mock.patch.object(documents, "read_upload_body", never):
            started = asyncio.get_running_loop().time()
            with self.assertRaises(ApiError) as ctx:
                await documents._read_upload_within_deadline(req, 1000)
            self.assertLess(asyncio.get_running_loop().time() - started, 2)
        self.assertEqual((ctx.exception.status_code, ctx.exception.code, ctx.exception.retryable),
                         (408, "request_timeout", True))

    async def test_invalid_timeouts_fall_back_to_the_default(self) -> None:
        for bad in (0, -1, True, "5", None):
            with self.subTest(bad=bad):
                req = asgi_request(b"%PDF", "application/pdf", state={"upload_read_timeout_s": bad})
                raw = await documents._read_upload_within_deadline(req, 100)
                self.assertEqual(bytes(raw.body), b"%PDF")


class OversizeCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_upload_path_reports_payload_too_large(self) -> None:
        """SPEC 16.4 clients should not need to know the content type to
        recognise 'body too large' (it used to be file_too_large for PDFs)."""
        multipart = (b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n"
                     b"Content-Type: application/pdf\r\n\r\n" + b"x" * 200 + b"\r\n--B--\r\n")
        cases = {
            "pdf, streamed": asgi_request(b"x" * 200, "application/pdf"),
            "multipart, streamed": asgi_request(multipart * 400, "multipart/form-data; boundary=B"),
        }
        for label, req in cases.items():
            with self.subTest(label):
                with self.assertRaises(ApiError) as ctx:
                    await documents.read_upload_body(req, 100)
                self.assertEqual((ctx.exception.status_code, ctx.exception.code), (413, "payload_too_large"))


class UploadEndpointDeadlineAndReauthTests(UploadEndpointCase):
    def test_slow_body_gets_408_and_the_slot_is_released(self) -> None:
        self.app.state.upload_read_timeout_s = 0.1

        async def never(request, max_bytes):
            await asyncio.sleep(30)

        with mock.patch.object(documents, "_authorize_upload", lambda *a: None):
            with mock.patch.object(documents, "read_upload_body", never):
                for _ in range(3):  # a leaked slot would turn the 2nd into a 503
                    r = TestClient(self.app).post(self.url, content=self.pdf, headers={"Content-Type": "application/pdf"})
                    self.assertEqual((r.status_code, r.json()["error"]["code"]), (408, "request_timeout"))
            with mock.patch.object(documents, "_upload_core", lambda *a: (201, {"ok": True})):
                ok = TestClient(self.app).post(self.url, content=self.pdf, headers={"Content-Type": "application/pdf"})
        self.assertEqual(ok.status_code, 201)

    def test_authorization_is_checked_again_inside_the_write_transaction(self) -> None:
        """A role revoked while a slow client was still sending must stop the write."""
        from lab_api.documents import get_db

        conn = MagicMock()
        in_tx = {"now": False}

        class Tx:
            def __enter__(self):
                in_tx["now"] = True
                return conn

            def __exit__(self, *exc):
                in_tx["now"] = False
                return False

        conn.transaction.side_effect = Tx
        conn.execute.return_value.fetchone.return_value = None
        self.app.dependency_overrides[get_db] = lambda: conn

        calls: list[bool] = []

        def authorize_then_revoke(c, project_id, actor_id):
            calls.append(in_tx["now"])
            if len(calls) == 2:
                raise ApiError(403, "permission_denied", "role revoked")

        with mock.patch.object(documents, "_authorize_upload", authorize_then_revoke), \
                mock.patch.object(documents, "ingest_document") as ingest, \
                mock.patch.object(documents, "inspect_pdf") as inspect:
            r = TestClient(self.app).post(self.url, content=self.pdf, headers={"Content-Type": "application/pdf"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "permission_denied"))
        self.assertEqual(calls, [False, True], "once before the body, once inside the transaction")
        ingest.assert_not_called()
        inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
