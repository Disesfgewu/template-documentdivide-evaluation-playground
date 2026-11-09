"""Actual local TCP HTTP integration; no provider, persistence or scheduler claim."""
import json
import socketserver
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from lab_api.method_client import MethodClient, MethodClientError
from lab_contracts.method import PrepareRequest, QueryRequest


QUERY = {
    "protocol_version": "1.0", "request_id": "req", "run_id": "run",
    "question_id": "q", "index_id": "idx", "question": "Question?",
    "budget": {"top_k": 10, "max_evidence_tokens": 12000,
               "max_visual_regions": 8, "timeout_seconds": 180},
}
ANSWER = {
    "protocol_version": "1.0", "request_id": "req", "question_id": "q", "index_id": "idx",
    "status": "answered", "answer": {"text": "Answer", "claims": []},
    "retrieved_hits": [], "citations": [],
}
PREPARE = {
    "protocol_version": "1.0", "request_id": "prep",
    "documents": [{"document_id": "doc", "sha256": "a" * 64, "page_count": 1,
                   "pdf_url": "https://example.invalid/doc.pdf",
                   "url_expires_at": "2030-01-01T00:00:00Z"}],
    "method_revision": {"method_id": "method", "revision": "rev"},
    "config": {}, "config_hash": "b" * 64,
}


def model(cls, data):
    return cls.model_validate_json(json.dumps(data))


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, content))
        status, body, headers, drip = self.server.responses.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in headers.items():
            self.send_header(key, value)
        if "Transfer-Encoding" not in headers and "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            if drip:
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(drip)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class MethodClientTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.requests, self.server.responses = [], []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.client = MethodClient(self.origin)
        self.query = model(QueryRequest, QUERY)
        self.prepare = model(PrepareRequest, PREPARE)
        self.deadline = time.monotonic() + 5

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def reply(self, body, status=200, headers=None, drip=0):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.server.responses.append((status, body, headers or {}, drip))

    def error(self, code, action):
        with self.assertRaises(MethodClientError) as caught:
            action()
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(caught.exception.code, code)

    def invoke(self):
        return self.client.query(self.query, deadline=self.deadline)

    def test_sync_query_and_capabilities_use_real_http(self):
        self.reply({"protocol_version": "1.0", "method_id": "m", "revision": "r",
                    "supported_modalities": ["text"], "async_query": True,
                    "multi_region": True, "max_parallel_queries": 2})
        self.assertEqual(self.client.capabilities().method_id, "m")
        self.reply(ANSWER)
        self.assertEqual(self.invoke().answer.text, "Answer")
        self.assertEqual(self.server.requests[0][:2], ("GET", "/v1/capabilities"))
        self.assertEqual(json.loads(self.server.requests[1][2]), QUERY)

    def test_async_query_and_cancel_bind_job_and_request(self):
        self.reply({"job_id": "job/a?x#secret", "poll_after_ms": 30}, 202)
        pending = self.invoke()
        self.reply({"job_id": pending.job_id, "kind": "query", "status": "succeeded",
                    "progress": 1, "result": ANSWER})
        done = self.client.poll(pending.job_id, self.query, deadline=self.deadline)
        self.assertEqual(done.result.answer.text, "Answer")
        self.assertEqual(self.server.requests[1][1], "/v1/jobs/job%2Fa%3Fx%23secret")
        self.reply({"job_id": pending.job_id, "kind": "query", "status": "running", "progress": None}, 202)
        self.assertEqual(self.client.cancel(pending.job_id, self.query,
                                          deadline=self.deadline).status, "running")
        self.assertTrue(self.server.requests[-1][1].endswith("/cancel"))

    def test_prepare_and_terminal_manifest_binding(self):
        self.reply({"job_id": "prepjob", "poll_after_ms": 0}, 202)
        self.assertEqual(self.client.prepare(self.prepare).job_id, "prepjob")
        result = {"index_id": "idx", "index_fingerprint": "fp", "document_hashes": {"doc": "a" * 64},
                  "method_revision": PREPARE["method_revision"], "status": "ready"}
        job = {"job_id": "prepjob", "kind": "prepare", "status": "succeeded", "progress": 1, "result": result}
        self.reply(job)
        self.assertEqual(self.client.poll("prepjob", self.prepare, deadline=self.deadline).result.index_id, "idx")
        result["document_hashes"]["doc"] = "c" * 64
        self.reply(job)
        self.error("response_binding_mismatch", lambda: self.client.poll("prepjob", self.prepare, deadline=self.deadline))

    def test_invalid_strict_json_schema_and_versions(self):
        for body in [b'{"secret":"private", "secret":2}', b'{"value":NaN}', b'\xff', b'[]',
                     {**ANSWER, "protocol_version": "9.0"}, {**ANSWER, "extra": "private"}]:
            with self.subTest(body=type(body).__name__):
                self.reply(body)
                self.error("invalid_response", self.invoke)
        nested = json.dumps(ANSWER).replace('"text": "Answer"', '"text":"Answer","text":"private"').encode()
        self.reply(nested)
        self.error("invalid_response", self.invoke)

    def test_sync_and_async_identity_mismatch(self):
        for field in ["request_id", "question_id", "index_id"]:
            answer = {**ANSWER, field: "other"}
            self.reply(answer)
            self.error("response_binding_mismatch", self.invoke)
            self.reply({"job_id": "job", "kind": "query", "status": "succeeded", "progress": 1, "result": answer})
            self.error("response_binding_mismatch", lambda: self.client.poll("job", self.query, deadline=self.deadline))
        self.reply({"job_id": "other", "kind": "query", "status": "running", "progress": None})
        self.error("response_binding_mismatch", lambda: self.client.poll("job", self.query, deadline=self.deadline))
        self.reply({"job_id": "job", "kind": "prepare", "status": "running", "progress": None})
        self.error("response_binding_mismatch", lambda: self.client.poll("job", self.query, deadline=self.deadline))

    def test_redirect_and_503_never_retry_or_follow(self):
        for status in [302, 307, 503]:
            self.reply(b'private secret provider payload', status, {"Location": "http://127.0.0.1:1/private"})
            before = len(self.server.requests)
            self.error("unexpected_http_status", self.invoke)
            self.assertEqual(len(self.server.requests), before + 1)

    def test_bounded_content_length_and_chunked_response(self):
        self.client = MethodClient(self.origin, max_response_bytes=20)
        self.reply(b'x' * 21)
        self.error("response_too_large", self.invoke)
        self.reply(b'15\r\n' + b'x' * 21 + b'\r\n0\r\n\r\n', headers={"Transfer-Encoding": "chunked"})
        self.error("response_too_large", self.invoke)

    def test_valid_json_with_truncated_http_frame_is_rejected(self):
        raw = json.dumps(ANSWER).encode()
        self.reply(raw, headers={"Content-Length": str(len(raw) + 100)})
        self.error("incomplete_response", self.invoke)

    def test_slow_trickle_has_total_deadline(self):
        self.reply(json.dumps(ANSWER).encode(), drip=.02)
        self.deadline = time.monotonic() + .12
        start = time.monotonic()
        self.error("timeout", self.invoke)
        self.assertLess(time.monotonic() - start, .7)

    def test_http10_slow_eof_response_has_total_deadline(self):
        class Http10Handler(Handler):
            protocol_version = "HTTP/1.0"
        self.server.RequestHandlerClass = Http10Handler
        self.reply(json.dumps(ANSWER).encode(), drip=.02)
        self.deadline = time.monotonic() + .12
        start = time.monotonic()
        self.error("timeout", self.invoke)
        self.assertLess(time.monotonic() - start, .7)

    def test_tls_handshake_stall_respects_deadline(self):
        class Stall(socketserver.BaseRequestHandler):
            def handle(self):
                time.sleep(.5)
        with socketserver.TCPServer(("127.0.0.1", 0), Stall) as stalled:
            thread = threading.Thread(target=stalled.serve_forever, daemon=True)
            thread.start()
            try:
                client = MethodClient(f"https://127.0.0.1:{stalled.server_address[1]}")
                start = time.monotonic()
                self.error("timeout", lambda: client.query(self.query, deadline=start + .12))
                self.assertLess(time.monotonic() - start, .7)
            finally:
                stalled.shutdown()
                thread.join()

    def test_expired_poll_sends_nothing(self):
        self.error("timeout", lambda: self.client.poll("job", self.query, deadline=time.monotonic() - 1))
        self.assertEqual(self.server.requests, [])

    def test_origin_and_limits_are_explicit_and_sanitized(self):
        for origin in ["http://example.com", "http://user:secret@127.0.0.1", "http://127.0.0.1/path",
                       "http://127.0.0.1?token=secret", "ftp://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:999999"]:
            self.error("invalid_origin", lambda: MethodClient(origin))
        for timeout in [0, -1, True, float('nan'), 181]:
            self.error("invalid_timeout", lambda: MethodClient(self.origin, timeout_seconds=timeout))
        for limit in [0, True, 16 * 1024 * 1024 + 1]:
            self.error("invalid_response_limit", lambda: MethodClient(self.origin, max_response_bytes=limit))
        for job_id in ["", ".", ".."]:
            self.error("invalid_job_id", lambda: self.client.poll(job_id, self.query, deadline=self.deadline))
        for deadline in [True, None, "secret", float("nan")]:
            self.error("invalid_deadline", lambda: self.client.query(self.query, deadline=deadline))
            self.error("invalid_deadline", lambda: self.client.poll("job", self.query, deadline=deadline))
