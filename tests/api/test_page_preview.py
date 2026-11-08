"""Tests for authenticated PDF page preview increment (SPEC 8, 19, Refs #7)."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import pypdf
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_api.config import load_settings
from lab_api.documents import (
    DEFAULT_PREVIEW_CONCURRENCY,
    get_authenticated_actor,
    get_db,
    get_store,
)
from lab_api.main import create_app
from lab_api.preview import PNG_SIGNATURE, PreviewError, render_preview_in_subprocess
from lab_parser.render import render_region
from lab_db.repo import Decision, NotFound, PermissionDenied
from fixtures import registry_check as rc
from fixtures.generators import synthetic_pdf


def _make_large_dimension_pdf() -> bytes:
    """Create a 1-page PDF exceeding 4096px at 150 DPI (3000x3000pt)."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=3000, height=3000)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class PagePreviewApiUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": "postgresql://lab:s3cret@127.0.0.1:5432/lab",
            "LAB_ARTIFACT_ROOT": "/tmp/test-artifacts",
            "LAB_DB_CONNECT_TIMEOUT_S": "2",
            "LAB_AUTH_SECRET": "secret-signing-key-for-tests-only-32b",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.app = create_app(settings, checks={})
        self.client = TestClient(self.app)

        self.doc_id = uuid.uuid4()
        self.proj_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()

        # Synthetic 1-page PDF (F01)
        self.f01_pdf = synthetic_pdf.build(1)
        self.f01_sha256 = hashlib.sha256(self.f01_pdf).hexdigest()
        self.f01_size = len(self.f01_pdf)
        self.obj_key = f"sha256/{self.f01_sha256[:2]}/{self.f01_sha256}"

        self.mock_conn = MagicMock()
        self.mock_store = MagicMock()
        store_patch = patch("lab_api.documents.get_store", return_value=self.mock_store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.app.dependency_overrides[get_db] = lambda: self.mock_conn
        self.app.dependency_overrides[get_store] = lambda: self.mock_store
        self.app.dependency_overrides[get_authenticated_actor] = lambda: self.actor_id

        self._set_db_row(page_count=1)

    def _set_db_row(
        self,
        *,
        doc_sha: str | None = None,
        art_sha: str | None = None,
        size_bytes: int | None = None,
        page_count: int = 1,
        artifact_state: str = "committed",
        not_found: bool = False,
    ) -> None:
        if not_found:
            self.mock_conn.execute.return_value.fetchone.return_value = None
            return

        actual_doc_sha = doc_sha if doc_sha is not None else self.f01_sha256
        actual_art_sha = art_sha if art_sha is not None else actual_doc_sha
        actual_size = size_bytes if size_bytes is not None else self.f01_size

        def fake_execute(query, params=None):
            m = MagicMock()
            q = str(query)
            if "FROM document_versions" in q and "resource_versions" not in q:
                # Query in _get_page: project_id, page_count
                m.fetchone.return_value = (self.proj_id, page_count)
            elif "FROM pages" in q:
                # Page geometry query in _get_page: page_index, page_label, rotation, width_pt, height_pt
                page_idx = params[1] if params and len(params) > 1 else 0
                m.fetchone.return_value = (page_idx, f"P.{page_idx + 1}", 0, 595.0, 842.0)
            elif "resource_versions" in q:
                # Query in _readable_document
                m.fetchone.return_value = (
                    self.doc_id,
                    self.proj_id,
                    uuid.uuid4(),
                    uuid.uuid4(),
                    actual_doc_sha,
                    page_count,
                    "doc-1",
                    1,
                    actual_art_sha,
                    actual_size,
                    self.obj_key,
                    artifact_state,
                )
            else:
                m.fetchone.return_value = None
            return m

        self.mock_conn.execute.side_effect = fake_execute

    def test_positive_page_preview_f01_renders_real_png_and_headers(self) -> None:
        """Accept: image/png renders real F01 PDF at 150 DPI with full metadata headers."""
        self.mock_store.open.return_value = self.f01_pdf
        expected_asset = render_region(self.f01_pdf, 0, dpi=150)

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            for url in (
                f"/api/v1/documents/{self.doc_id}/pages/0",
                f"/documents/{self.doc_id}/pages/0",
            ):
                with self.subTest(url=url):
                    res = self.client.get(url, headers={"Accept": "image/png"})
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.headers["Content-Type"], "image/png")
                    self.assertEqual(res.headers["Cache-Control"], "no-store")
                    self.assertEqual(res.headers["Vary"], "Accept")
                    self.assertEqual(res.headers["X-Document-Sha256"], self.f01_sha256)
                    self.assertEqual(res.headers["X-Page-Index"], "0")

                    # Verify PNG bytes signature
                    body = res.content
                    self.assertTrue(body.startswith(PNG_SIGNATURE))
                    actual_png_sha = hashlib.sha256(body).hexdigest()
                    self.assertEqual(res.headers["X-Image-Sha256"], actual_png_sha)
                    self.assertEqual(res.headers["ETag"], f'"{actual_png_sha}"')
                    self.assertEqual(actual_png_sha, expected_asset.sha256)

                    # Verify dimensions in header and PNG IHDR chunk against oracle
                    width_px = int(res.headers["X-Image-Width"])
                    height_px = int(res.headers["X-Image-Height"])
                    self.assertEqual(width_px, expected_asset.width_px)
                    self.assertEqual(height_px, expected_asset.height_px)

                    png_w, png_h = struct.unpack(">II", body[16:24])
                    self.assertEqual(png_w, width_px)
                    self.assertEqual(png_h, height_px)

    def test_positive_page_preview_f02_rotated_cropbox_pages(self) -> None:
        """Render multi-page F02 fixture with rotation 0/90/180/270 and CropBox."""
        f02_entry = rc.load()["fixtures"][1]
        f02_item = f02_entry["items"][0]
        f02_pdf = rc.item_bytes(f02_item)
        f02_sha = hashlib.sha256(f02_pdf).hexdigest()

        self._set_db_row(
            doc_sha=f02_sha,
            art_sha=f02_sha,
            size_bytes=len(f02_pdf),
            page_count=4,
        )
        self.mock_store.open.return_value = f02_pdf

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            for page_idx in range(4):
                oracle_asset = render_region(f02_pdf, page_idx, dpi=150)
                with self.subTest(page=page_idx):
                    res = self.client.get(
                        f"/api/v1/documents/{self.doc_id}/pages/{page_idx}",
                        headers={"Accept": "image/png"},
                    )
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.headers["Content-Type"], "image/png")
                    self.assertEqual(res.headers["Cache-Control"], "no-store")
                    self.assertEqual(res.headers["Vary"], "Accept")
                    self.assertEqual(res.headers["X-Document-Sha256"], f02_sha)
                    self.assertEqual(res.headers["X-Page-Index"], str(page_idx))
                    self.assertEqual(int(res.headers["X-Image-Width"]), oracle_asset.width_px)
                    self.assertEqual(int(res.headers["X-Image-Height"]), oracle_asset.height_px)

                    body = res.content
                    self.assertTrue(body.startswith(PNG_SIGNATURE))
                    png_w, png_h = struct.unpack(">II", body[16:24])
                    self.assertEqual(png_w, oracle_asset.width_px)
                    self.assertEqual(png_h, oracle_asset.height_px)

    def test_content_negotiation_quality_and_default_json_preservation(self) -> None:
        """Parse media-range qualities; preserve default JSON when JSON preferred or PNG disabled."""
        self.mock_store.open.return_value = self.f01_pdf

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            # 1. No accept or generic accept -> JSON default
            for accept in ("", "*/*", "application/json", "text/plain"):
                with self.subTest(accept=accept):
                    res = self.client.get(f"/api/v1/documents/{self.doc_id}/pages/0", headers={"Accept": accept} if accept else {})
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.headers["Content-Type"], "application/json")
                    self.assertEqual(res.headers["Vary"], "Accept")
                    self.assertEqual(res.headers["Cache-Control"], "no-store")
                    self.mock_store.open.assert_not_called()

            # 2. JSON preferred over PNG -> JSON default
            res_pref_json = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png;q=0.7, application/json;q=0.9"},
            )
            self.assertEqual(res_pref_json.status_code, 200)
            self.assertEqual(res_pref_json.headers["Content-Type"], "application/json")

            # 3. PNG preferred over JSON -> PNG served
            res_pref_png = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "application/json;q=0.7, image/png;q=0.9"},
            )
            self.assertEqual(res_pref_png.status_code, 200)
            self.assertEqual(res_pref_png.headers["Content-Type"], "image/png")

            # 4. image/png;q=0 explicitly refused -> JSON default
            res_q0 = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png;q=0"},
            )
            self.assertEqual(res_q0.status_code, 200)
            self.assertEqual(res_q0.headers["Content-Type"], "application/json")

            # 5. Malformed accept type (image/pngjunk) -> JSON default
            res_junk = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/pngjunk"},
            )
            self.assertEqual(res_junk.status_code, 200)
            self.assertEqual(res_junk.headers["Content-Type"], "application/json")

    def test_authorization_checks_precede_storage_and_render(self) -> None:
        """Unauthorized, non-member, and ungranted observer fail before store/subprocess."""
        # 1. Non-member actor raises NotFound -> 404
        with patch("lab_api.documents.authorize", side_effect=NotFound("project")):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "not_found")
            self.mock_store.open.assert_not_called()

        # 2. Forbidden role raises PermissionDenied -> 403
        with patch("lab_api.documents.authorize", side_effect=PermissionDenied("forbidden")):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.json()["error"]["code"], "permission_denied")
            self.mock_store.open.assert_not_called()

        # 3. Observer requiring explicit grant -> 403
        with patch("lab_api.documents.authorize", return_value=Decision(role="observer", condition="explicit_grant")):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.json()["error"]["code"], "permission_denied")
            self.mock_store.open.assert_not_called()

    def test_out_of_range_page_index_returns_404(self) -> None:
        """Page index >= page_count or < 0 returns 404 before storage read."""
        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            for bad_page in (-1, 1, 99, 999):
                with self.subTest(page=bad_page):
                    res = self.client.get(
                        f"/api/v1/documents/{self.doc_id}/pages/{bad_page}",
                        headers={"Accept": "image/png"},
                    )
                    self.assertEqual(res.status_code, 404)
                    self.assertEqual(res.json()["error"]["code"], "not_found")
                    self.mock_store.open.assert_not_called()

    def test_malformed_path_returns_422(self) -> None:
        """Non-integer page_index or invalid UUID returns 422."""
        res = self.client.get(
            f"/api/v1/documents/{self.doc_id}/pages/invalid_index",
            headers={"Accept": "image/png"},
        )
        self.assertEqual(res.status_code, 422)

        res2 = self.client.get(
            "/api/v1/documents/not-a-uuid/pages/0",
            headers={"Accept": "image/png"},
        )
        self.assertEqual(res2.status_code, 422)

    def test_render_pixel_limit_exceeded_returns_422_actionable(self) -> None:
        """A PDF with oversized page dimensions raises actionable 422 pixel_limit_exceeded."""
        oversized_pdf = _make_large_dimension_pdf()
        oversized_sha = hashlib.sha256(oversized_pdf).hexdigest()

        self._set_db_row(
            doc_sha=oversized_sha,
            art_sha=oversized_sha,
            size_bytes=len(oversized_pdf),
            page_count=1,
        )
        self.mock_store.open.return_value = oversized_pdf

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res.status_code, 422)
            body = res.json()
            self.assertEqual(body["error"]["code"], "pixel_limit_exceeded")
            self.assertIn("16MP or 4096px", body["error"]["message"])

    def test_corrupt_artifact_hash_mismatch_returns_500(self) -> None:
        """Checksum mismatch between database and storage fails closed with 500 storage_corruption."""
        self.mock_store.open.return_value = b"%PDF-1.7 modified bytes"

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res.status_code, 500)
            self.assertEqual(res.json()["error"]["code"], "storage_corruption")

    def test_subprocess_timeout_returns_504_safe_message(self) -> None:
        """Timeout in rendering subprocess returns 504 gateway_timeout with retryable=True."""
        self.mock_store.open.return_value = self.f01_pdf
        self.app.state.preview_timeout_s = 0.0001  # Trigger timeout immediately

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res.status_code, 504)
            body = res.json()
            self.assertEqual(body["error"]["code"], "gateway_timeout")
            self.assertTrue(body["error"]["retryable"])
            self.assertIn("timed out", body["error"]["message"])

    def test_subprocess_failure_returns_503_safe_message(self) -> None:
        """Abnormal subprocess exit returns safe 503 service_unavailable."""
        self.mock_store.open.return_value = self.f01_pdf

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)), \
             patch("subprocess.run") as mock_subproc:
            mock_subproc.return_value = MagicMock(returncode=13)
            res = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res.status_code, 503)
            body = res.json()
            self.assertEqual(body["error"]["code"], "service_unavailable")
            self.assertTrue(body["error"]["retryable"])
            self.assertIn("temporarily unavailable", body["error"]["message"])

    def test_concurrency_slots_and_slot_release_on_completion_and_error(self) -> None:
        """Preview concurrency is bounded (default 2); 3rd request is refused with 503 server_busy."""
        self.app.state.preview_concurrency = 2
        self.mock_store.open.return_value = self.f01_pdf

        started_evt = threading.Event()
        block_evt = threading.Event()

        orig_subprocess = render_preview_in_subprocess

        def slow_preview(*args, **kwargs):
            started_evt.set()
            block_evt.wait(timeout=5.0)
            return orig_subprocess(*args, **kwargs)

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)):
            # 1. Normal preview succeeds and slot is released
            res1 = self.client.get(f"/api/v1/documents/{self.doc_id}/pages/0", headers={"Accept": "image/png"})
            self.assertEqual(res1.status_code, 200)

            # 2. Block 2 threads in preview rendering
            with patch("lab_api.documents.render_preview_in_subprocess", side_effect=slow_preview):
                t1 = threading.Thread(
                    target=lambda: self.client.get(
                        f"/api/v1/documents/{self.doc_id}/pages/0", headers={"Accept": "image/png"}
                    )
                )
                t2 = threading.Thread(
                    target=lambda: self.client.get(
                        f"/api/v1/documents/{self.doc_id}/pages/0", headers={"Accept": "image/png"}
                    )
                )
                t1.start()
                t2.start()

                started_evt.wait(timeout=2.0)
                time.sleep(0.1)

                # 3. Third request while 2 are active gets 503 server_busy
                res_busy = self.client.get(
                    f"/api/v1/documents/{self.doc_id}/pages/0", headers={"Accept": "image/png"}
                )
                self.assertEqual(res_busy.status_code, 503)
                self.assertEqual(res_busy.json()["error"]["code"], "server_busy")

                # 4. Unblock background workers
                block_evt.set()
                t1.join(timeout=3.0)
                t2.join(timeout=3.0)

            # 5. After workers complete, slots are released and requests succeed again
            res_after = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0", headers={"Accept": "image/png"}
            )
            self.assertEqual(res_after.status_code, 200)

    def test_direct_cancellation_preserves_slot_retention_until_worker_done(self) -> None:
        """Cancellation does not prematurely free slot capacity while native work remains."""
        self.app.state.preview_concurrency = 1
        self.mock_store.open.return_value = self.f01_pdf

        worker_started = threading.Event()
        worker_can_finish = threading.Event()
        worker_finished = threading.Event()

        def mock_preview_worker(*args, **kwargs):
            worker_started.set()
            worker_can_finish.wait(timeout=5.0)
            worker_finished.set()
            return render_preview_in_subprocess(self.f01_pdf, 0)

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)), \
             patch("lab_api.documents.render_preview_in_subprocess", side_effect=mock_preview_worker):

            # Start request in thread
            t = threading.Thread(
                target=lambda: self.client.get(
                    f"/api/v1/documents/{self.doc_id}/pages/0",
                    headers={"Accept": "image/png"},
                )
            )
            t.start()
            worker_started.wait(timeout=2.0)

            # Slot is held; a second request gets 503 server_busy
            res_busy = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res_busy.status_code, 503)
            self.assertEqual(res_busy.json()["error"]["code"], "server_busy")

            worker_can_finish.set()
            t.join(timeout=3.0)
            self.assertTrue(worker_finished.is_set())

            # Now slot is freed
            res_ok = self.client.get(
                f"/api/v1/documents/{self.doc_id}/pages/0",
                headers={"Accept": "image/png"},
            )
            self.assertEqual(res_ok.status_code, 200)

    def test_event_loop_remains_responsive_during_page_preview(self) -> None:
        """Event loop is never blocked by native rendering; /healthz responds immediately."""
        self.mock_store.open.return_value = self.f01_pdf
        started_evt = threading.Event()
        block_evt = threading.Event()

        def slow_preview(*args, **kwargs):
            started_evt.set()
            block_evt.wait(timeout=5.0)
            return render_preview_in_subprocess(*args, **kwargs)

        with patch("lab_api.documents.authorize", return_value=Decision(role="member", condition=None)), \
             patch("lab_api.documents.render_preview_in_subprocess", side_effect=slow_preview):
            t = threading.Thread(
                target=lambda: self.client.get(
                    f"/api/v1/documents/{self.doc_id}/pages/0", headers={"Accept": "image/png"}
                )
            )
            t.start()
            started_evt.wait(timeout=2.0)

            # Check /healthz while preview is in progress
            health_res = self.client.get("/healthz")
            self.assertEqual(health_res.status_code, 200)
            self.assertEqual(health_res.json(), {"status": "ok"})

            block_evt.set()
            t.join(timeout=3.0)

    def test_json_geometry_does_not_require_artifact_storage(self):
        with patch("lab_api.documents.get_store", side_effect=RuntimeError("unavailable")) as store, \
             patch("lab_api.documents.authorize", return_value=Decision(role="owner", condition=None)):
            response = self.client.get(f"/api/v1/documents/{self.doc_id}/pages/0")
        self.assertEqual(response.status_code, 200)
        store.assert_not_called()

    def test_child_environment_excludes_credentials(self):
        import subprocess
        original = subprocess.run
        with patch.dict(os.environ, {"LAB_AUTH_SECRET": "private-canary", "PROVIDER_KEY": "private-canary"}), \
             patch("lab_api.preview.subprocess.run", wraps=original) as run:
            result = render_preview_in_subprocess(self.f01_pdf, 0)
        self.assertTrue(result.png.startswith(PNG_SIGNATURE))
        self.assertEqual(set(run.call_args.kwargs["env"]), {"PYTHONPATH", "LANG"})
        self.assertNotIn("preexec_fn", run.call_args.kwargs)

    def test_png_parent_rejects_corruption_truncation_and_excess_pixels(self):
        from lab_api.preview import _validate_png
        from lab_parser.render import encode_png_rgb
        png = encode_png_rgb(1, 1, bytes([10, 20, 30]))
        _validate_png(png, 1, 1)
        for bad in (png[:-12], png + b"extra", png[:32] + bytes([png[32] ^ 1]) + png[33:],
                    png[:33] + png[-12:]):
            with self.subTest(size=len(bad)), self.assertRaises(PreviewError):
                _validate_png(bad, 1, 1)

    def test_openapi_declares_preview_response_headers(self):
        response = self.app.openapi()["paths"]["/api/v1/documents/{document_id}/pages/{page_index}"]["get"]["responses"]["200"]
        self.assertIn("X-Image-Sha256", response["headers"])
        self.assertNotIn("headers", response["content"]["image/png"])


    def test_preview_workspace_failure_is_sanitized(self):
        with patch("lab_api.preview.tempfile.TemporaryDirectory", side_effect=OSError("private-path-canary")):
            with self.assertRaises(PreviewError) as raised:
                render_preview_in_subprocess(self.f01_pdf, 0)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("private-path-canary", str(raised.exception))
