"""Real Browser + real FastAPI/PostgreSQL acceptance test (Issue #83, Refs #13, R21, R23, T76).

Exercised against:
- Real PostgreSQL (FreshDatabase with migrations and pgvector)
- Real FastAPI backend with DownloadAdmissionMiddleware and LocalArtifactStore
- Built web distribution (web/dist)
- Headless Chrome / Chromium via CDP
- Synthetic F01 (21 pages)
- Signed identities only (issue_actor_token)
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

# Repository setup
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (
    _REPO_ROOT,
    _REPO_ROOT / "db",
    _REPO_ROOT / "services" / "api",
    _REPO_ROOT / "services" / "parser",
    _REPO_ROOT / "contracts",
):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_db.storage import LocalArtifactStore
from fixtures.generators import synthetic_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_browser_executable() -> str | None:
    if os.environ.get("CHROME_BIN") and os.path.exists(os.environ["CHROME_BIN"]):
        return os.environ["CHROME_BIN"]
    candidates = [
        "/home/martin/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RealBrowserDocumentUiAcceptanceTests(FreshDatabase):
    """Real headless browser acceptance test suite against real FastAPI and PostgreSQL."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.f01_21 = synthetic_pdf.build(21)
        cls.f01_sha256 = hashlib.sha256(cls.f01_21).hexdigest()
        cls.f01_size = len(cls.f01_21)

    def setUp(self) -> None:
        super().setUp()
        self.browser_bin = find_browser_executable()
        if not self.browser_bin:
            self.skipTest("No Chrome/Chromium executable found on host for browser acceptance")

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.artifact_root = Path(self.tmp.name).resolve() / "artifacts"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.store = LocalArtifactStore(self.artifact_root)

        # Write synthetic F01 21-page PDF to disk
        self.fixture_pdf = Path(self.tmp.name).resolve() / "f01_21_pages.pdf"
        self.fixture_pdf.write_bytes(self.f01_21)

        self.secret = "top-secret-signing-key-for-tests-only"
        self.env = {
            "LAB_ENV": "test",
            "LAB_PROVIDER_MODE": "mock",
            "LAB_DATABASE_URL": self.url,
            "LAB_ARTIFACT_ROOT": str(self.artifact_root),
            "LAB_AUTH_SECRET": self.secret,
            "LAB_DB_CONNECT_TIMEOUT_S": "5",
        }

    def _start_server(self, app, port: int):
        import uvicorn

        config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()

        # Wait for server ready
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except (ConnectionRefentError if hasattr(socket, "ConnectionRefentError") else OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError(f"Server on port {port} failed to start in time")
        return server

    def test_r21_r23_t76_real_browser_f01_upload_and_download_flow(self) -> None:
        """Full browser + real FastAPI/PostgreSQL acceptance run with synthetic F01."""
        from unittest.mock import patch
        from starlette.staticfiles import StaticFiles

        with patch.dict(os.environ, self.env, clear=True):
            settings = load_settings()

        app = create_app(settings=settings, checks={})
        web_dist = _REPO_ROOT / "web" / "dist"
        if not web_dist.exists() or not (web_dist / "index.html").exists():
            subprocess.run(["npm", "run", "build"], cwd=_REPO_ROOT / "web", check=True)

        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="static")

        port = find_free_port()
        server = self._start_server(app, port)
        self.addCleanup(setattr, server, "should_exit", True)

        # Set up real projects in PostgreSQL
        with self.connect() as conn:
            project_id, owner_id = self.make_project(conn, role="owner")
            other_project_id, other_id = self.make_project(conn, role="owner")

        owner_token = issue_actor_token(owner_id, self.secret)
        other_token = issue_actor_token(other_id, self.secret)

        download_tmp = Path(self.tmp.name) / "downloads"
        download_tmp.mkdir(parents=True, exist_ok=True)

        # Run headless browser acceptance runner
        cmd = [
            "node",
            str(_REPO_ROOT / "tests" / "api_db" / "accept_document_ui_browser.mjs"),
            "--url",
            f"http://127.0.0.1:{port}/",
            "--token",
            owner_token,
            "--project-id",
            str(project_id),
            "--pdf-path",
            str(self.fixture_pdf),
            "--expected-sha256",
            self.f01_sha256,
            "--expected-size",
            str(self.f01_size),
            "--expected-pages",
            "21",
            "--other-token",
            other_token,
            "--browser-path",
            self.browser_bin,
            "--download-dir",
            str(download_tmp),
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, f"Browser test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")

        # Verify PostgreSQL database directly
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # 1. document_versions
                cur.execute(
                    "SELECT * FROM document_versions WHERE project_id = %s",
                    (project_id,),
                )
                versions = cur.fetchall()
                # Confirm lost-response replay created NO duplicate version row
                self.assertEqual(len(versions), 1, f"Expected 1 version row, found {len(versions)}")
                row = versions[0]
                self.assertEqual(row["sha256"], self.f01_sha256)
                self.assertEqual(row["page_count"], 21)

                # 2. artifacts
                cur.execute("SELECT * FROM artifacts WHERE id = %s", (row["artifact_id"],))
                art = cur.fetchone()
                self.assertIsNotNone(art)
                self.assertEqual(art["sha256"], self.f01_sha256)
                self.assertEqual(art["state"], "committed")

                # 3. pages count
                cur.execute("SELECT count(*) as cnt FROM pages WHERE document_version_id = %s", (row["id"],))
                page_cnt = cur.fetchone()["cnt"]
                self.assertEqual(page_cnt, 21)

        # Verify artifact stored on disk byte-matches synthetic F01
        stored_bytes = self.store.read(self.f01_sha256)
        self.assertEqual(hashlib.sha256(stored_bytes).hexdigest(), self.f01_sha256)
        self.assertEqual(stored_bytes, self.f01_21)
