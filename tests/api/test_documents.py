"""Unit tests for PDF intake, bounded multipart streaming, and error contracts."""
from __future__ import annotations

import hashlib
import io
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_api.documents import ApiError, _make_rejection_error_response, extract_upload_data
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


class RejectionResponseContractTests(unittest.TestCase):
    def test_standard_rejection_reasons_and_structure(self) -> None:
        """Verify each standard inspection rejection maps to its exact status, code, and details."""
        cases = [
            ("empty_file", 422, "empty_file"),
            ("not_a_pdf", 422, "invalid_pdf"),
            ("corrupt_pdf", 422, "corrupt_pdf"),
            ("encrypted_pdf", 422, "unsupported_encryption"),
            ("page_limit_exceeded", 422, "page_limit_exceeded"),
            ("file_too_large", 413, "payload_too_large"),
        ]
        for reason, expected_status, expected_code in cases:
            with self.subTest(reason=reason):
                resp = _make_rejection_error_response(
                    reason,
                    request_id="req-test-1",
                    sha256="abc123sha",
                    size_bytes=42,
                    page_count=None,
                )
                self.assertEqual(resp.status_code, expected_status)
                import json
                body = json.loads(resp.body.decode("utf-8"))
                err = body["error"]
                self.assertEqual(err["code"], expected_code)
                self.assertEqual(err["message"], f"document rejected: {reason}")
                self.assertFalse(err["retryable"])
                self.assertEqual(err["request_id"], "req-test-1")
                self.assertIsNone(err["retry_after"])
                self.assertEqual(
                    err["details"],
                    {
                        "reason": reason,
                        "sha256": "abc123sha",
                        "size_bytes": 42,
                        "page_count": None,
                    },
                )

    def test_invalid_page_geometry_rejection_structure(self) -> None:
        """Verify invalid_page_geometry maps to 422, fixed message, and error details."""
        resp = _make_rejection_error_response(
            "invalid_page_geometry",
            request_id="req-geom-99",
            error_type="KeyError",
        )
        self.assertEqual(resp.status_code, 422)
        import json
        body = json.loads(resp.body.decode("utf-8"))
        err = body["error"]
        self.assertEqual(err["code"], "invalid_page_geometry")
        self.assertEqual(err["message"], "failed to parse page geometry from PDF")
        self.assertFalse(err["retryable"])
        self.assertEqual(err["request_id"], "req-geom-99")
        self.assertIsNone(err["retry_after"])
        self.assertEqual(err["details"], {"error": "KeyError"})

    def test_unknown_reason_defaults_safely(self) -> None:
        """Unrecognized rejection reason falls back safely to 422 invalid_pdf."""
        resp = _make_rejection_error_response("unknown_anomaly", request_id="req-unk")
        self.assertEqual(resp.status_code, 422)
        import json
        body = json.loads(resp.body.decode("utf-8"))
        err = body["error"]
        self.assertEqual(err["code"], "invalid_pdf")
        self.assertEqual(err["message"], "document rejected: unknown_anomaly")


class AuthenticatedBoundedDownloadRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        from unittest import mock
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
        self.client = TestClient(self.app)
        self.doc_id = uuid.uuid4()
        self.proj_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()
        self.pdf_bytes = b"%PDF-1.7 regression test valid payload"
        self.doc_sha = hashlib.sha256(self.pdf_bytes).hexdigest()
        self.size_bytes = len(self.pdf_bytes)
        self.obj_key = f"sha256/{self.doc_sha[:2]}/{self.doc_sha}"

        self.mock_conn = MagicMock()
        self.mock_store = MagicMock()
        self.app.dependency_overrides[get_db] = lambda: self.mock_conn
        self.app.dependency_overrides[get_store] = lambda: self.mock_store
        self.app.dependency_overrides[get_authenticated_actor] = lambda: self.actor_id

    def _set_db_row(
        self,
        *,
        art_sha: str | None = None,
        size_bytes: int | str = 0,
        artifact_state: str = "committed",
        not_found: bool = False,
    ) -> None:
        if not_found:
            self.mock_conn.execute.return_value.fetchone.return_value = None
            return
        actual_art_sha = art_sha if art_sha is not None else self.doc_sha

        def fake_execute(query, params=None):
            m = MagicMock()
            if "resource_versions" in query:
                m.fetchone.return_value = (
                    self.doc_id,
                    self.proj_id,
                    uuid.uuid4(),
                    uuid.uuid4(),
                    self.doc_sha,
                    1,
                    "doc-1",
                    1,
                    actual_art_sha,
                    size_bytes,
                    self.obj_key,
                    artifact_state,
                )
            else:
                m.fetchone.return_value = (
                    self.proj_id,
                    self.doc_sha,
                    actual_art_sha,
                    self.obj_key,
                    size_bytes,
                    artifact_state,
                )
            return m

        self.mock_conn.execute.side_effect = fake_execute

    def test_authorization_precedes_storage_on_both_representations(self) -> None:
        """Non-members get 404, forbidden roles get 403, and storage is NEVER accessed."""
        from lab_db.repo import Decision, NotFound, PermissionDenied

        self._set_db_row(size_bytes=self.size_bytes)

        # 1. Non-member actor raises NotFound -> 404
        with patch("lab_api.documents.authorize", side_effect=NotFound("project")):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}/download", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                with self.subTest(auth="non_member", url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 404)
                    self.assertEqual(res.json()["error"]["code"], "not_found")
                    self.mock_store.open.assert_not_called()

        # 2. Forbidden role raises PermissionDenied -> 403
        with patch("lab_api.documents.authorize", side_effect=PermissionDenied("forbidden")):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}/download", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                with self.subTest(auth="forbidden_role", url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 403)
                    self.assertEqual(res.json()["error"]["code"], "permission_denied")
                    self.mock_store.open.assert_not_called()

        # 3. Observer requiring explicit grant -> 403
        with patch("lab_api.documents.authorize", return_value=Decision(role="observer", condition="explicit_grant")):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}/download", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                with self.subTest(auth="explicit_grant", url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 403)
                    self.assertEqual(res.json()["error"]["code"], "permission_denied")
                    self.mock_store.open.assert_not_called()

    def test_uncommitted_artifact_fails_closed_before_storage(self) -> None:
        """Uncommitted artifacts return 404 without touching storage."""
        from lab_db.repo import Decision

        self._set_db_row(size_bytes=self.size_bytes, artifact_state="staged")

        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                with self.subTest(url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 404)
                    self.assertEqual(res.json()["error"]["code"], "not_found")
                    self.mock_store.open.assert_not_called()

    def test_database_checksum_or_size_mismatch_fails_closed_before_storage(self) -> None:
        """Database checksum or size anomalies fail closed with 500 storage_corruption without touching storage."""
        from lab_db.repo import Decision

        # 1. Checksum mismatch in database
        self._set_db_row(art_sha="0" * 64, size_bytes=self.size_bytes)
        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                with self.subTest(defect="checksum_mismatch", url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 500)
                    self.assertEqual(res.json()["error"]["code"], "storage_corruption")
                    self.mock_store.open.assert_not_called()

        # 2. Invalid size_bytes in database (negative or string)
        for bad_size in (-1, "bad_size"):
            self._set_db_row(size_bytes=bad_size)
            with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
                for url, headers in [
                    (f"/api/v1/documents/{self.doc_id}/original", {}),
                    (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
                ]:
                    with self.subTest(defect="invalid_size", size=bad_size, url=url):
                        res = self.client.get(url, headers=headers)
                        self.assertEqual(res.status_code, 500)
                        self.assertEqual(res.json()["error"]["code"], "storage_corruption")
                        self.mock_store.open.assert_not_called()

    def test_corrupt_or_growing_storage_fails_closed_with_bounded_read(self) -> None:
        """Storage corruption or concurrent growth raises 500 storage_corruption; open is called with expected_size and max_bytes."""
        from lab_db.repo import Decision
        from lab_db.storage import StorageCorruption
        from lab_api.pdf_body import DEFAULT_MAX_PDF_BYTES

        self._set_db_row(size_bytes=self.size_bytes)
        self.mock_store.open.side_effect = StorageCorruption("object grew concurrently")

        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                self.mock_store.open.reset_mock()
                with self.subTest(url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 500)
                    self.assertEqual(res.json()["error"]["code"], "storage_corruption")
                    self.mock_store.open.assert_called_once_with(
                        self.obj_key,
                        expected_size=self.size_bytes,
                        max_bytes=DEFAULT_MAX_PDF_BYTES,
                    )

    def test_successful_download_on_both_representations(self) -> None:
        """Both download representations validate committed size and hash and return 200 with matching bytes."""
        from lab_db.repo import Decision
        from lab_api.pdf_body import DEFAULT_MAX_PDF_BYTES

        self._set_db_row(size_bytes=self.size_bytes)
        self.mock_store.open.return_value = self.pdf_bytes

        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            # 1. Original / download endpoints
            for path in [
                f"/api/v1/documents/{self.doc_id}/original",
                f"/documents/{self.doc_id}/original",
                f"/api/v1/documents/{self.doc_id}/download",
                f"/documents/{self.doc_id}/download",
            ]:
                self.mock_store.open.reset_mock()
                with self.subTest(path=path):
                    res = self.client.get(path)
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.headers["Content-Type"], "application/pdf")
                    self.assertEqual(res.headers["Content-Length"], str(self.size_bytes))
                    self.assertEqual(
                        res.headers["Content-Disposition"],
                        f'attachment; filename="{self.doc_id}.pdf"',
                    )
                    self.assertEqual(res.content, self.pdf_bytes)
                    self.mock_store.open.assert_called_once_with(
                        self.obj_key,
                        expected_size=self.size_bytes,
                        max_bytes=DEFAULT_MAX_PDF_BYTES,
                    )

            # 2. Document endpoint with Accept: application/pdf
            for path in [
                f"/api/v1/documents/{self.doc_id}",
                f"/documents/{self.doc_id}",
            ]:
                self.mock_store.open.reset_mock()
                with self.subTest(path=path):
                    res = self.client.get(path, headers={"Accept": "application/pdf"})
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.headers["Content-Type"], "application/pdf")
                    self.assertEqual(res.headers["Content-Length"], str(self.size_bytes))
                    self.assertEqual(
                        res.headers["Content-Disposition"],
                        f'attachment; filename="{self.doc_id}.pdf"',
                    )
                    self.assertEqual(res.content, self.pdf_bytes)
                    self.mock_store.open.assert_called_once_with(
                        self.obj_key,
                        expected_size=self.size_bytes,
                        max_bytes=DEFAULT_MAX_PDF_BYTES,
                    )

            # 3. Document endpoint without Accept: application/pdf returns metadata JSON without opening store
            self.mock_store.open.reset_mock()
            res_meta = self.client.get(f"/api/v1/documents/{self.doc_id}")
            self.assertEqual(res_meta.status_code, 200)
            self.assertEqual(res_meta.json()["sha256"], self.doc_sha)
            self.assertEqual(res_meta.json()["size_bytes"], self.size_bytes)
            self.mock_store.open.assert_not_called()

    def test_database_size_exceeding_server_ceiling_fails_closed_before_storage(self) -> None:
        """A database size exceeding the server-controlled ceiling fails closed with 500 storage_corruption without touching storage."""
        from lab_db.repo import Decision
        from lab_api.pdf_body import DEFAULT_MAX_PDF_BYTES

        # 1. Size exceeding default ceiling (512 MiB + 1 or huge terabyte size)
        for oversized in (DEFAULT_MAX_PDF_BYTES + 1, 10**12):
            self._set_db_row(size_bytes=oversized)
            with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
                for url, headers in [
                    (f"/api/v1/documents/{self.doc_id}/original", {}),
                    (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
                ]:
                    self.mock_store.open.reset_mock()
                    with self.subTest(oversized=oversized, url=url):
                        res = self.client.get(url, headers=headers)
                        self.assertEqual(res.status_code, 500)
                        self.assertEqual(res.json()["error"]["code"], "storage_corruption")
                        self.mock_store.open.assert_not_called()

        # 2. Custom server-controlled ceiling via app.state.max_pdf_bytes
        self.app.state.max_pdf_bytes = 1000
        self._set_db_row(size_bytes=1001)
        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            res = self.client.get(f"/api/v1/documents/{self.doc_id}/original")
            self.assertEqual(res.status_code, 500)
            self.assertEqual(res.json()["error"]["code"], "storage_corruption")
            self.mock_store.open.assert_not_called()

        # Exact ceiling size succeeds
        payload_1000 = b"x" * 1000
        sha_1000 = hashlib.sha256(payload_1000).hexdigest()
        self.mock_store.open.return_value = payload_1000
        self._set_db_row(size_bytes=1000, art_sha=sha_1000)
        self.doc_sha = sha_1000
        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            res_ok = self.client.get(f"/api/v1/documents/{self.doc_id}/original")
            self.assertEqual(res_ok.status_code, 200)
            self.assertEqual(res_ok.content, payload_1000)

    def test_adversarial_truncated_and_growing_objects_rejected(self) -> None:
        """Adversarial objects that grew or truncated fail closed with 500 storage_corruption on both download representations."""
        from lab_db.repo import Decision
        from lab_db.storage import StorageCorruption

        self._set_db_row(size_bytes=self.size_bytes)

        # 1. Object truncated
        self.mock_store.open.side_effect = StorageCorruption("object truncated")
        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                with self.subTest(case="truncated", url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 500)
                    self.assertEqual(res.json()["error"]["code"], "storage_corruption")

        # 2. Object grew concurrently beyond expected size
        self.mock_store.open.side_effect = StorageCorruption("object grew concurrently")
        with patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            for url, headers in [
                (f"/api/v1/documents/{self.doc_id}/original", {}),
                (f"/api/v1/documents/{self.doc_id}", {"Accept": "application/pdf"}),
            ]:
                with self.subTest(case="grew_concurrently", url=url):
                    res = self.client.get(url, headers=headers)
                    self.assertEqual(res.status_code, 500)
                    self.assertEqual(res.json()["error"]["code"], "storage_corruption")


if __name__ == "__main__":
    unittest.main()
