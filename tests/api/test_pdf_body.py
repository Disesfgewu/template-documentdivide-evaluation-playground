"""Regression tests for ASGI application/pdf request-body reader (Refs #67).

Tests positive streaming, exact limit equality, limit plus-one, malformed /
duplicate / misleading Content-Length, wrong Content-Type, premature disconnect,
bounded consumption, and error redaction using actual ASGI messages.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest
from typing import Any

from starlette.requests import Request

from lab_api.main import create_app
from lab_api.pdf_body import (
    DEFAULT_MAX_PDF_BYTES,
    PdfRequestBodyError,
    PdfRequestBodyReader,
    read_pdf_body,
    read_pdf_request_body,
)

# Dynamically import inspect_pdf from services/parser without requiring
# parser to be installed in services/api site-packages.
_PARSER_ROOT = Path(__file__).resolve().parents[2] / "services" / "parser"
if str(_PARSER_ROOT) not in sys.path:
    sys.path.insert(0, str(_PARSER_ROOT))

from lab_parser.importer import inspect_pdf


# Deterministic synthetic 1-page PDF fixture (516 bytes)
SYNTHETIC_PDF_BYTES = (
    b"%PDF-1.3\n"
    b"%\xe2\xe3\xcf\xd3\n"
    b"1 0 obj\n"
    b"<<\n"
    b"/Producer (pypdf)\n"
    b">>\n"
    b"endobj\n"
    b"2 0 obj\n"
    b"<<\n"
    b"/Type /Pages\n"
    b"/Count 1\n"
    b"/Kids [ 4 0 R ]\n"
    b">>\n"
    b"endobj\n"
    b"3 0 obj\n"
    b"<<\n"
    b"/Type /Catalog\n"
    b"/Pages 2 0 R\n"
    b">>\n"
    b"endobj\n"
    b"4 0 obj\n"
    b"<<\n"
    b"/Type /Page\n"
    b"/Resources <<\n"
    b">>\n"
    b"/MediaBox [ 0.0 0.0 100 100 ]\n"
    b"/Parent 2 0 R\n"
    b"/Contents 5 0 R\n"
    b">>\n"
    b"endobj\n"
    b"5 0 obj\n"
    b"<<\n"
    b"/Length 0\n"
    b">>\n"
    b"stream\n"
    b"\n"
    b"endstream\n"
    b"endobj\n"
    b"xref\n"
    b"0 6\n"
    b"0000000000 65535 f \n"
    b"0000000015 00000 n \n"
    b"0000000054 00000 n \n"
    b"0000000113 00000 n \n"
    b"0000000162 00000 n \n"
    b"0000000272 00000 n \n"
    b"trailer\n"
    b"<<\n"
    b"/Size 6\n"
    b"/Root 3 0 R\n"
    b"/Info 1 0 R\n"
    b">>\n"
    b"startxref\n"
    b"321\n"
    b"%%EOF\n"
)


def make_scope(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
    method: str = "POST",
    path: str = "/upload",
) -> dict[str, Any]:
    if headers is None:
        headers = [
            (b"content-type", b"application/pdf"),
            (b"content-length", str(len(SYNTHETIC_PDF_BYTES)).encode("ascii")),
        ]
    return {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": headers,
    }


def make_receive(messages: list[dict[str, Any]]):
    it = iter(messages)

    async def receive() -> dict[str, Any]:
        try:
            return next(it)
        except StopIteration:
            raise RuntimeError("receive() called beyond available messages") from None

    return receive


class PdfRequestBodyReaderTests(unittest.IsolatedAsyncioTestCase):
    """Test ASGI request body reading, boundary enforcement, and error redaction."""

    async def test_valid_pdf_single_chunk_preserves_exact_bytes(self):
        scope = make_scope()
        receive = make_receive([
            {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False}
        ])

        data = await read_pdf_request_body(scope, receive)
        self.assertEqual(data, SYNTHETIC_PDF_BYTES)
        # Verify exact sha256 matches
        self.assertEqual(hashlib.sha256(data).hexdigest(), hashlib.sha256(SYNTHETIC_PDF_BYTES).hexdigest())

    async def test_inspect_pdf_compatibility(self):
        """Preserve exact raw bytes for downstream inspect_pdf validation."""
        scope = make_scope()
        receive = make_receive([
            {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False}
        ])

        data = await read_pdf_request_body(scope, receive)
        result = inspect_pdf(data)
        self.assertEqual(result.outcome, "accept")
        self.assertIsNone(result.reason)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.size_bytes, len(SYNTHETIC_PDF_BYTES))
        self.assertEqual(result.sha256, hashlib.sha256(SYNTHETIC_PDF_BYTES).hexdigest())

    async def test_streaming_multiple_chunks_without_content_length(self):
        """Chunked transfer encoding without Content-Length header is supported."""
        scope = make_scope(headers=[(b"content-type", b"application/pdf")])
        part1 = SYNTHETIC_PDF_BYTES[:100]
        part2 = SYNTHETIC_PDF_BYTES[100:250]
        part3 = SYNTHETIC_PDF_BYTES[250:]

        receive = make_receive([
            {"type": "http.request", "body": part1, "more_body": True},
            {"type": "http.request", "body": part2, "more_body": True},
            {"type": "http.request", "body": part3, "more_body": False},
        ])

        data = await read_pdf_request_body(scope, receive)
        self.assertEqual(data, SYNTHETIC_PDF_BYTES)

    async def test_streaming_multiple_chunks_with_exact_content_length(self):
        scope = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", str(len(SYNTHETIC_PDF_BYTES)).encode("ascii")),
            ]
        )
        part1 = SYNTHETIC_PDF_BYTES[:200]
        part2 = SYNTHETIC_PDF_BYTES[200:]

        receive = make_receive([
            {"type": "http.request", "body": part1, "more_body": True},
            {"type": "http.request", "body": part2, "more_body": False},
        ])

        data = await read_pdf_request_body(scope, receive)
        self.assertEqual(data, SYNTHETIC_PDF_BYTES)

    async def test_empty_chunks_in_stream_handled_safely(self):
        scope = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", str(len(SYNTHETIC_PDF_BYTES)).encode("ascii")),
            ]
        )
        receive = make_receive([
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False},
        ])

        data = await read_pdf_request_body(scope, receive)
        self.assertEqual(data, SYNTHETIC_PDF_BYTES)

    async def test_content_type_case_insensitive_and_with_parameters(self):
        for ct in (
            b"application/pdf",
            b"APPLICATION/PDF",
            b"application/pdf; charset=binary",
            b"Application/Pdf; name=upload.pdf",
        ):
            with self.subTest(ct=ct):
                scope = make_scope(
                    headers=[
                        (b"content-type", ct),
                        (b"content-length", str(len(SYNTHETIC_PDF_BYTES)).encode("ascii")),
                    ]
                )
                receive = make_receive([
                    {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False}
                ])
                data = await read_pdf_request_body(scope, receive)
                self.assertEqual(data, SYNTHETIC_PDF_BYTES)

    async def test_starlette_request_wrapper_supported(self):
        scope = make_scope()
        receive = make_receive([
            {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False}
        ])
        req = Request(scope, receive)
        data = await read_pdf_request_body(req)
        self.assertEqual(data, SYNTHETIC_PDF_BYTES)

    async def test_alias_read_pdf_body(self):
        scope = make_scope()
        receive = make_receive([
            {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False}
        ])
        data = await read_pdf_body(scope, receive)
        self.assertEqual(data, SYNTHETIC_PDF_BYTES)

    async def test_class_reader_interface(self):
        reader = PdfRequestBodyReader(max_bytes=1024)
        self.assertEqual(reader.max_bytes, 1024)

        scope = make_scope()
        receive = make_receive([
            {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False}
        ])
        data = await reader.read(scope, receive)
        self.assertEqual(data, SYNTHETIC_PDF_BYTES)

        # Also callable directly
        receive2 = make_receive([
            {"type": "http.request", "body": SYNTHETIC_PDF_BYTES, "more_body": False}
        ])
        data2 = await reader(scope, receive2)
        self.assertEqual(data2, SYNTHETIC_PDF_BYTES)


class LimitBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Test exact limit equality and limit plus-one bounds."""

    async def test_exact_byte_limit_accepted(self):
        limit = 500
        payload = b"x" * limit
        # 1. Single chunk with Content-Length
        scope1 = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", str(limit).encode("ascii")),
            ]
        )
        receive1 = make_receive([
            {"type": "http.request", "body": payload, "more_body": False}
        ])
        res1 = await read_pdf_request_body(scope1, receive1, max_bytes=limit)
        self.assertEqual(len(res1), limit)

        # 2. Multi-chunk chunked without Content-Length
        scope2 = make_scope(headers=[(b"content-type", b"application/pdf")])
        receive2 = make_receive([
            {"type": "http.request", "body": payload[:250], "more_body": True},
            {"type": "http.request", "body": payload[250:], "more_body": False},
        ])
        res2 = await read_pdf_request_body(scope2, receive2, max_bytes=limit)
        self.assertEqual(len(res2), limit)

    async def test_limit_plus_one_with_content_length_rejected_early(self):
        limit = 500
        scope = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", str(limit + 1).encode("ascii")),
            ]
        )

        called = False

        async def uncalled_receive():
            nonlocal called
            called = True
            return {"type": "http.request", "body": b"x", "more_body": False}

        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, uncalled_receive, max_bytes=limit)

        self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual(cm.exception.code, "file_too_large")
        self.assertFalse(called, "receive() should not be called when Content-Length exceeds limit")

    async def test_limit_plus_one_streamed_without_content_length_rejected(self):
        limit = 500
        scope = make_scope(headers=[(b"content-type", b"application/pdf")])

        receive = make_receive([
            {"type": "http.request", "body": b"x" * 250, "more_body": True},
            {"type": "http.request", "body": b"x" * 251, "more_body": False},  # 501 bytes total
        ])

        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive, max_bytes=limit)

        self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual(cm.exception.code, "file_too_large")

    async def test_bounded_streaming_stops_consuming_on_limit_exceeded(self):
        """Verify the reader stops consuming ASGI messages once the limit is exceeded."""
        limit = 100
        scope = make_scope(headers=[(b"content-type", b"application/pdf")])

        call_count = 0

        async def stream_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"type": "http.request", "body": b"a" * 80, "more_body": True}
            if call_count == 2:
                # Pushes total to 120 > 100
                return {"type": "http.request", "body": b"b" * 40, "more_body": True}
            raise AssertionError("Third message should never be pulled after limit breach")

        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, stream_receive, max_bytes=limit)

        self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual(call_count, 2)

    async def test_invalid_max_bytes_configuration(self):
        scope = make_scope()
        receive = make_receive([])
        for invalid in (0, -1, "500", True, False, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await read_pdf_request_body(scope, receive, max_bytes=invalid)  # type: ignore
            with self.subTest(invalid_reader=invalid), self.assertRaises(ValueError):
                PdfRequestBodyReader(max_bytes=invalid)  # type: ignore


class ContentLengthBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Test missing, malformed, duplicate, and misleading Content-Length headers."""

    async def test_missing_content_length_allowed(self):
        scope = make_scope(headers=[(b"content-type", b"application/pdf")])
        receive = make_receive([
            {"type": "http.request", "body": b"x" * 50, "more_body": False}
        ])
        data = await read_pdf_request_body(scope, receive, max_bytes=100)
        self.assertEqual(len(data), 50)

    async def test_malformed_content_length_rejected(self):
        for bad_cl in (b"-1", b"abc", b"12.34", b"+10", b"", b" ", b"1e6"):
            with self.subTest(bad_cl=bad_cl):
                scope = make_scope(
                    headers=[
                        (b"content-type", b"application/pdf"),
                        (b"content-length", bad_cl),
                    ]
                )
                receive = make_receive([])
                with self.assertRaises(PdfRequestBodyError) as cm:
                    await read_pdf_request_body(scope, receive)
                self.assertEqual(cm.exception.status_code, 400)
                self.assertEqual(cm.exception.code, "invalid_content_length")

    async def test_duplicate_content_length_headers_rejected(self):
        # Multiple separate headers
        scope = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", b"10"),
                (b"content-length", b"10"),
            ]
        )
        receive = make_receive([])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "invalid_content_length")

        # Comma separated in single header
        scope2 = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", b"10, 10"),
            ]
        )
        with self.assertRaises(PdfRequestBodyError) as cm2:
            await read_pdf_request_body(scope2, receive)
        self.assertEqual(cm2.exception.status_code, 400)
        self.assertEqual(cm2.exception.code, "invalid_content_length")

    async def test_misleading_content_length_smaller_than_stream_rejected(self):
        """Header claims 5 bytes, but client streams 10 bytes."""
        scope = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", b"5"),
            ]
        )
        receive = make_receive([
            {"type": "http.request", "body": b"1234567890", "more_body": False}
        ])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive, max_bytes=100)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "invalid_content_length")

    async def test_misleading_content_length_larger_than_stream_rejected(self):
        """Header claims 20 bytes, but stream ends at 10 bytes."""
        scope = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", b"20"),
            ]
        )
        receive = make_receive([
            {"type": "http.request", "body": b"1234567890", "more_body": False}
        ])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive, max_bytes=100)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "invalid_content_length")


class ContentTypeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Test wrong, missing, or duplicate Content-Type rejection."""

    async def test_missing_content_type_rejected(self):
        scope = make_scope(headers=[(b"content-length", b"10")])
        receive = make_receive([])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 415)
        self.assertEqual(cm.exception.code, "invalid_content_type")

    async def test_duplicate_content_type_headers_rejected(self):
        scope = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-type", b"application/pdf"),
            ]
        )
        receive = make_receive([])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 415)
        self.assertEqual(cm.exception.code, "invalid_content_type")

    async def test_wrong_content_types_rejected(self):
        for wrong in (
            b"text/plain",
            b"application/json",
            b"image/png",
            b"application/octet-stream",
            b"multipart/form-data; boundary=xyz",
            b"application/x-pdf",
        ):
            with self.subTest(wrong=wrong):
                scope = make_scope(headers=[(b"content-type", wrong)])
                receive = make_receive([])
                with self.assertRaises(PdfRequestBodyError) as cm:
                    await read_pdf_request_body(scope, receive)
                self.assertEqual(cm.exception.status_code, 415)
                self.assertEqual(cm.exception.code, "invalid_content_type")


class DisconnectAndMalformedStreamTests(unittest.IsolatedAsyncioTestCase):
    """Test premature disconnects and malformed ASGI message sequences."""

    async def test_premature_disconnect_initial_message(self):
        scope = make_scope()
        receive = make_receive([
            {"type": "http.disconnect"}
        ])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "client_disconnect")

    async def test_premature_disconnect_mid_stream(self):
        scope = make_scope(headers=[(b"content-type", b"application/pdf")])
        receive = make_receive([
            {"type": "http.request", "body": b"chunk1", "more_body": True},
            {"type": "http.disconnect"},
        ])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "client_disconnect")

    async def test_non_http_scope_rejected(self):
        scope = make_scope(scope_type="websocket")
        receive = make_receive([])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "invalid_request")

    async def test_invalid_scope_mapping_rejected(self):
        receive = make_receive([])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body("not-a-mapping", receive)  # type: ignore
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "invalid_request")

    async def test_invalid_receive_callable_rejected(self):
        scope = make_scope()
        with self.assertRaises(ValueError):
            await read_pdf_request_body(scope, None)

    async def test_unexpected_asgi_message_type_rejected(self):
        scope = make_scope()
        receive = make_receive([
            {"type": "http.foo"}
        ])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "invalid_request")

    async def test_invalid_message_body_type_rejected(self):
        scope = make_scope()
        receive = make_receive([
            {"type": "http.request", "body": 12345, "more_body": False}  # type: ignore
        ])
        with self.assertRaises(PdfRequestBodyError) as cm:
            await read_pdf_request_body(scope, receive)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "invalid_request")


class ErrorRedactionAndContractTests(unittest.IsolatedAsyncioTestCase):
    """Verify error messages never leak sensitive canaries, paths, or body content."""

    CANARY_SECRET = "canary-secret-auth-token-9999"
    CANARY_BODY = b"canary-confidential-pdf-body-content"
    CANARY_PATH = "/confidential/path/to/server/artifacts"

    async def test_error_never_echoes_canary_headers_bodies_or_paths(self):
        # 1. Invalid content type with secret in parameter
        scope1 = make_scope(
            headers=[
                (b"content-type", f"text/plain; token={self.CANARY_SECRET}".encode("ascii")),
            ],
            path=self.CANARY_PATH,
        )
        with self.assertRaises(PdfRequestBodyError) as cm1:
            await read_pdf_request_body(scope1, make_receive([]))
        err_text1 = str(cm1.exception) + " " + repr(cm1.exception) + " " + cm1.exception.detail
        self.assertNotIn(self.CANARY_SECRET, err_text1)
        self.assertNotIn(self.CANARY_PATH, err_text1)

        # 2. Malformed content length with secret
        scope2 = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
                (b"content-length", f"bad-{self.CANARY_SECRET}".encode("ascii")),
            ],
            path=self.CANARY_PATH,
        )
        with self.assertRaises(PdfRequestBodyError) as cm2:
            await read_pdf_request_body(scope2, make_receive([]))
        err_text2 = str(cm2.exception) + " " + repr(cm2.exception) + " " + cm2.exception.detail
        self.assertNotIn(self.CANARY_SECRET, err_text2)
        self.assertNotIn(self.CANARY_PATH, err_text2)

        # 3. Payload too large with secret body content
        scope3 = make_scope(
            headers=[
                (b"content-type", b"application/pdf"),
            ],
            path=self.CANARY_PATH,
        )
        receive3 = make_receive([
            {"type": "http.request", "body": self.CANARY_BODY, "more_body": False}
        ])
        with self.assertRaises(PdfRequestBodyError) as cm3:
            await read_pdf_request_body(scope3, receive3, max_bytes=10)
        err_text3 = str(cm3.exception) + " " + repr(cm3.exception) + " " + cm3.exception.detail
        self.assertNotIn("canary-confidential", err_text3)
        self.assertNotIn(self.CANARY_PATH, err_text3)

    def test_error_response_conforms_to_contract_structure(self):
        err = PdfRequestBodyError(413, "file_too_large", "request body exceeds size limit")
        payload = err.to_error_body("req-test-42")
        self.assertIn("error", payload)
        err_body = payload["error"]
        self.assertEqual(err_body["code"], "file_too_large")
        self.assertEqual(err_body["message"], "request body exceeds size limit")
        self.assertFalse(err_body["retryable"])
        self.assertEqual(err_body["request_id"], "req-test-42")
        self.assertEqual(err_body["details"], {})
        self.assertIsNone(err_body["retry_after"])

        # Also verify JSON serialization works cleanly
        serialized = json.dumps(payload)
        self.assertIn("file_too_large", serialized)

    async def test_to_response_and_send_response(self):
        err = PdfRequestBodyError(415, "invalid_content_type", "Content-Type must be application/pdf")
        response = err.to_response("req-123")
        self.assertEqual(response.status_code, 415)

        sent_messages: list[dict[str, Any]] = []

        async def fake_send(msg: dict[str, Any]) -> None:
            sent_messages.append(msg)

        await err.send_response(fake_send, "req-123")
        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0]["type"], "http.response.start")
        self.assertEqual(sent_messages[0]["status"], 415)
        self.assertEqual(sent_messages[1]["type"], "http.response.body")
        parsed = json.loads(sent_messages[1]["body"].decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "invalid_content_type")
        self.assertEqual(parsed["error"]["request_id"], "req-123")


class DocumentRoutesRegistrationTest(unittest.TestCase):
    """Ensure the FastAPI app exposes the real document routes."""

    def test_create_app_has_document_routes(self):
        from unittest import mock
        from lab_api.config import load_settings

        test_env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": "postgresql://lab:s3cret-pw@127.0.0.1:1/lab",
            "LAB_ARTIFACT_ROOT": "/nonexistent-lab-artifacts",
            "LAB_DB_CONNECT_TIMEOUT_S": "2",
        }
        with mock.patch.dict("os.environ", test_env, clear=True):
            settings = load_settings()
            app = create_app(settings=settings, checks={})

        route_paths = []
        for route in app.routes:
            p = getattr(route, "path", None)
            if p:
                route_paths.append(p)
            elif hasattr(route, "original_router"):
                route_paths.extend([sub.path for sub in route.original_router.routes])
        self.assertIn("/api/v1/documents", route_paths)
        self.assertIn("/documents", route_paths)
        self.assertIn("/api/v1/documents/{document_id}/original", route_paths)
        self.assertIn("/documents/{document_id}/original", route_paths)
        self.assertIn("/api/v1/documents/{document_id}", route_paths)
        self.assertIn("/documents/{document_id}", route_paths)
        self.assertIn("/api/v1/documents/{document_id}/pages/{page_index}", route_paths)
