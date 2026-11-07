"""Unit tests for PDF intake, bounded multipart streaming, and error contracts."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_api.documents import ApiError, extract_upload_data
from fixtures.generators import synthetic_pdf


class MultipartIntakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.f01_1 = synthetic_pdf.build(1)

    async def test_bounded_multipart_stream_exceeding_limit_raises_413(self) -> None:
        """Stream chunks totaling more than max_bytes + 65536 raises 413 immediately without unbounded buffering."""
        limit = 1000

        app = FastAPI()

        @app.post("/test-upload")
        async def upload_route(req: Request):
            try:
                data, _ = await extract_upload_data(req, max_bytes=limit)
                return JSONResponse({"len": len(data)})
            except ApiError as exc:
                return exc.to_error_response("req-1")

        client = TestClient(app)

        # 1. Content-Length header exceeding limit fails fast
        res = client.post(
            "/test-upload",
            headers={
                "Content-Type": "multipart/form-data; boundary=BOUNDARY",
                "Content-Length": str(100000),
            },
            content=b"--BOUNDARY\r\n",
        )
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["error"]["code"], "payload_too_large")

        # 2. Chunked stream exceeding limit without Content-Length
        def chunk_generator():
            yield b"--BOUNDARY\r\n"
            yield b'Content-Disposition: form-data; name="file"; filename="doc.pdf"\r\n'
            yield b"Content-Type: application/pdf\r\n\r\n"
            yield b"x" * 70000
            yield b"\r\n--BOUNDARY--\r\n"

        res_chunked = client.post(
            "/test-upload",
            headers={"Content-Type": "multipart/form-data; boundary=BOUNDARY"},
            content=chunk_generator(),
        )
        self.assertEqual(res_chunked.status_code, 413)
        self.assertEqual(res_chunked.json()["error"]["code"], "payload_too_large")

    async def test_multipart_missing_pdf_raises_400(self) -> None:
        """Multipart payload without a PDF part raises 400 invalid_request."""
        app = FastAPI()

        @app.post("/test-upload")
        async def upload_route(req: Request):
            try:
                data, _ = await extract_upload_data(req, max_bytes=100000)
                return JSONResponse({"len": len(data)})
            except ApiError as exc:
                return exc.to_error_response("req-1")

        client = TestClient(app)

        body = (
            b"--BOUNDARY\r\n"
            b'Content-Disposition: form-data; name="not_pdf"; filename="doc.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"hello\r\n--BOUNDARY--\r\n"
        )
        res = client.post(
            "/test-upload",
            headers={"Content-Type": "multipart/form-data; boundary=BOUNDARY"},
            content=body,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_request")

    async def test_wrong_part_media_type_raises_415(self) -> None:
        """A multipart file part with non-PDF media type returns 415 unsupported_media_type."""
        app = FastAPI()

        @app.post("/test-upload")
        async def upload_route(req: Request):
            try:
                data, _ = await extract_upload_data(req, max_bytes=100000)
                return JSONResponse({"len": len(data)})
            except ApiError as exc:
                return exc.to_error_response("req-1")

        client = TestClient(app)

        body = (
            b"--BOUNDARY\r\n"
            b'Content-Disposition: form-data; name="file"; filename="doc.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"not a pdf content\r\n--BOUNDARY--\r\n"
        )
        res = client.post(
            "/test-upload",
            headers={"Content-Type": "multipart/form-data; boundary=BOUNDARY"},
            content=body,
        )
        self.assertEqual(res.status_code, 415)
        self.assertEqual(res.json()["error"]["code"], "unsupported_media_type")

    async def test_multiple_pdf_parts_raises_400(self) -> None:
        """Multiple PDF parts in a multipart body returns 400 invalid_request."""
        app = FastAPI()

        @app.post("/test-upload")
        async def upload_route(req: Request):
            try:
                data, _ = await extract_upload_data(req, max_bytes=100000)
                return JSONResponse({"len": len(data)})
            except ApiError as exc:
                return exc.to_error_response("req-1")

        client = TestClient(app)

        body = (
            b"--BOUNDARY\r\n"
            b'Content-Disposition: form-data; name="file1"; filename="a.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n"
            + self.f01_1
            + b"\r\n--BOUNDARY\r\n"
            b'Content-Disposition: form-data; name="file2"; filename="b.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n"
            + self.f01_1
            + b"\r\n--BOUNDARY--\r\n"
        )
        res = client.post(
            "/test-upload",
            headers={"Content-Type": "multipart/form-data; boundary=BOUNDARY"},
            content=body,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "invalid_request")

    async def test_unsupported_content_type_raises_415(self) -> None:
        """Unsupported content type returns 415 unsupported_media_type."""
        app = FastAPI()

        @app.post("/test-upload")
        async def upload_route(req: Request):
            try:
                data, _ = await extract_upload_data(req, max_bytes=100000)
                return JSONResponse({"len": len(data)})
            except ApiError as exc:
                return exc.to_error_response("req-1")

        client = TestClient(app)

        res = client.post(
            "/test-upload",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        self.assertEqual(res.status_code, 415)
        self.assertEqual(res.json()["error"]["code"], "unsupported_media_type")

    async def test_invalid_content_length_headers(self) -> None:
        """Negative or non-integer Content-Length returns 400 invalid_content_length."""
        app = FastAPI()

        @app.post("/test-upload")
        async def upload_route(req: Request):
            try:
                data, _ = await extract_upload_data(req, max_bytes=100000)
                return JSONResponse({"len": len(data)})
            except ApiError as exc:
                return exc.to_error_response("req-1")

        client = TestClient(app)

        for bad_cl in ("-5", "abc"):
            res = client.post(
                "/test-upload",
                headers={
                    "Content-Type": "multipart/form-data; boundary=BOUNDARY",
                    "Content-Length": bad_cl,
                },
                content=b"--BOUNDARY\r\n",
            )
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.json()["error"]["code"], "invalid_content_length")

    async def test_chunked_multipart_intake_success_via_feed_parser(self) -> None:
        """Chunked multipart intake streams through BytesFeedParser without full copy buffering."""
        app = FastAPI()

        @app.post("/test-upload")
        async def upload_route(req: Request):
            try:
                data, log_id = await extract_upload_data(req, max_bytes=100000)
                return JSONResponse({"len": len(data), "logical_id": log_id})
            except ApiError as exc:
                return exc.to_error_response("req-1")

        client = TestClient(app)

        def chunks():
            yield b"--BOUNDARY\r\n"
            yield b'Content-Disposition: form-data; name="logical_id"\r\n\r\n'
            yield b"chunked-doc\r\n"
            yield b"--BOUNDARY\r\n"
            yield b'Content-Disposition: form-data; name="file"; filename="upload.pdf"\r\n'
            yield b"Content-Type: application/pdf\r\n\r\n"
            yield self.f01_1[:100]
            yield self.f01_1[100:]
            yield b"\r\n--BOUNDARY--\r\n"

        res = client.post(
            "/test-upload",
            headers={"Content-Type": "multipart/form-data; boundary=BOUNDARY"},
            content=chunks(),
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["len"], len(self.f01_1))
        self.assertEqual(res.json()["logical_id"], "chunked-doc")


if __name__ == "__main__":
    unittest.main()
