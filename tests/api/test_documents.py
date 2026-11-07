"""Unit tests for PDF intake, bounded multipart streaming, and error contracts."""
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

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

        # Simulate ASGI request streaming chunks
        async def fake_receive():
            pass

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


if __name__ == "__main__":
    unittest.main()
