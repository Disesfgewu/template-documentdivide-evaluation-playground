"""Real PostgreSQL integration tests for authenticated PDF page preview (SPEC R01, 8, 19, Refs #7)."""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

# Ensure db, services/api, services/parser, contracts are available
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from fastapi.testclient import TestClient

from lab_api.auth import issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.preview import PNG_SIGNATURE
from lab_parser.render import render_region
from fixtures.generators import synthetic_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set (suite api-db needs PostgreSQL)")
class RealPdfPagePreviewTests(FreshDatabase):
    """End-to-end integration tests for PDF page preview with real PostgreSQL."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.f01_21 = synthetic_pdf.build(21)
        cls.f01_sha256 = hashlib.sha256(cls.f01_21).hexdigest()
        cls.f01_size = len(cls.f01_21)

    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.artifact_root = self.temp_dir.name

        self.secret = "test-secret-key-at-least-32-bytes-long-for-auth"
        # Must use self.url from FreshDatabase to connect to the migrated test database
        self.env_patch = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": self.url,
            "LAB_ARTIFACT_ROOT": self.artifact_root,
            "LAB_DB_CONNECT_TIMEOUT_S": "5",
            "LAB_AUTH_SECRET": self.secret,
        }
        with unittest.mock.patch.dict(os.environ, self.env_patch, clear=True):
            settings = load_settings()
        self.app = create_app(settings, checks={})
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        super().tearDown()

    def _upload_f01(self, project_id: uuid.UUID, owner_id: uuid.UUID) -> uuid.UUID:
        token = issue_actor_token(owner_id, self.secret)
        res = self.client.post(
            f"/api/v1/documents?project_id={project_id}&logical_id=f01-preview-doc",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/pdf",
            },
            content=self.f01_21,
        )
        self.assertEqual(res.status_code, 201, res.text)
        return uuid.UUID(res.json()["id"])

    def test_real_db_page_preview_positive_and_headers(self) -> None:
        """Fetch page preview with Accept: image/png against real PostgreSQL and storage."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        doc_id = self._upload_f01(project_id, owner_id)
        owner_token = issue_actor_token(owner_id, self.secret)

        res = self.client.get(
            f"/api/v1/documents/{doc_id}/pages/0",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Accept": "image/png",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Content-Type"], "image/png")
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        self.assertEqual(res.headers["Vary"], "Accept")
        self.assertEqual(res.headers["X-Document-Sha256"], self.f01_sha256)
        self.assertEqual(res.headers["X-Page-Index"], "0")

        # Measure actual dimensions using renderer oracle instead of hardcoded numbers
        expected_asset = render_region(self.f01_21, 0, dpi=150)
        self.assertEqual(res.headers["X-Image-Width"], str(expected_asset.width_px))
        self.assertEqual(res.headers["X-Image-Height"], str(expected_asset.height_px))

        body = res.content
        self.assertTrue(body.startswith(PNG_SIGNATURE))
        actual_sha = hashlib.sha256(body).hexdigest()
        self.assertEqual(res.headers["X-Image-Sha256"], actual_sha)
        self.assertEqual(res.headers["ETag"], f'"{actual_sha}"')
        self.assertEqual(actual_sha, expected_asset.sha256)

        png_w, png_h = struct.unpack(">II", body[16:24])
        self.assertEqual(png_w, expected_asset.width_px)
        self.assertEqual(png_h, expected_asset.height_px)

    def test_real_db_default_json_geometry_preserved(self) -> None:
        """Without Accept: image/png, endpoint returns JSON geometry from PostgreSQL pages table."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        doc_id = self._upload_f01(project_id, owner_id)
        owner_token = issue_actor_token(owner_id, self.secret)

        res = self.client.get(
            f"/api/v1/documents/{doc_id}/pages/0",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Content-Type"], "application/json")
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        self.assertEqual(res.headers["Vary"], "Accept")
        data = res.json()
        self.assertEqual(data["document_version_id"], str(doc_id))
        self.assertEqual(data["page_index"], 0)
        self.assertEqual(data["rotation"], 0)
        self.assertGreater(data["width_pt"], 0)
        self.assertGreater(data["height_pt"], 0)

    def test_real_db_authorization_boundaries(self) -> None:
        """Real PostgreSQL permission boundaries: cross-project is 404, ungranted observer is 403."""
        with self.connect() as conn:
            proj_a, owner_a = self.make_project(conn, role="owner")
            proj_b, owner_b = self.make_project(conn, role="owner")
            observer_a = self.add_member(conn, proj_a, "observer")

        doc_id = self._upload_f01(proj_a, owner_a)

        token_b = issue_actor_token(owner_b, self.secret)
        token_obs = issue_actor_token(observer_a, self.secret)

        # Cross-project actor gets 404
        res_cross = self.client.get(
            f"/api/v1/documents/{doc_id}/pages/0",
            headers={"Authorization": f"Bearer {token_b}", "Accept": "image/png"},
        )
        self.assertEqual(res_cross.status_code, 404)

        # Observer without explicit grant gets 403
        res_obs = self.client.get(
            f"/api/v1/documents/{doc_id}/pages/0",
            headers={"Authorization": f"Bearer {token_obs}", "Accept": "image/png"},
        )
        self.assertEqual(res_obs.status_code, 403)

        # Unauthenticated request gets 401
        res_unauth = self.client.get(
            f"/api/v1/documents/{doc_id}/pages/0",
            headers={"Accept": "image/png"},
        )
        self.assertEqual(res_unauth.status_code, 401)

    def test_real_db_out_of_range_page_returns_404(self) -> None:
        """Requesting page >= page_count returns 404."""
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")

        doc_id = self._upload_f01(project_id, owner_id)
        token = issue_actor_token(owner_id, self.secret)

        res = self.client.get(
            f"/api/v1/documents/{doc_id}/pages/21",
            headers={"Authorization": f"Bearer {token}", "Accept": "image/png"},
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "not_found")
