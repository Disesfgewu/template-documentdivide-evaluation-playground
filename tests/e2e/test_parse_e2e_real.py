"""End to end on real PostgreSQL with the REAL pinned Unstructured fast child (Refs #7).

Upload -> queue -> consumer (the real ``run_unstructured_fast``, no double) -> stored result ->
GET summary and pages. Every input is SYNTHETIC (fixture F01 text PDF from the repository
generator, plus a hand-built mixed-geometry PDF). Runs only when ``LAB_E2E_REAL_UNSTRUCTURED=1``
in an environment that has ``unstructured==0.27.36`` (the CI job ``e2e-parse``); otherwise it is
skipped, never passed. ``LAB_E2E_REPORT`` names a JSON file that receives versions and timings.
"""
from __future__ import annotations

import importlib.metadata as md
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.parse_results import make_persist
from lab_api.parse_worker import process_parse_job
from lab_db import jobs
from lab_db.storage import LocalArtifactStore
from fixtures.generators import synthetic_pdf
from deploy.unstructured_probe import synthetic_pdf as hand_built_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

ENABLED = os.environ.get("LAB_E2E_REAL_UNSTRUCTURED") == "1"
REPORT = {"synthetic": True, "python": sys.version.split()[0]}


def squash(text: str) -> str:
    """pdfminer emits rotated-page text letter by letter; compare without whitespace."""
    return "".join(text.split())


@unittest.skipUnless(ADMIN_URL and ENABLED, "needs LAB_TEST_DATABASE_URL and LAB_E2E_REAL_UNSTRUCTURED=1")
class ParseEndToEndTests(FreshDatabase):
    @classmethod
    def tearDownClass(cls):
        path = os.environ.get("LAB_E2E_REPORT")
        if path:
            Path(path).write_text(json.dumps(REPORT, indent=2, sort_keys=True))
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url,
               "LAB_ARTIFACT_ROOT": str(self.root), "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only",
               "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)
        self.store = LocalArtifactStore(self.root)
        with self.connect() as c:
            self.project, self.owner = self.make_project(c)

    def headers(self, **extra):
        return {"Authorization": f"Bearer {issue_actor_token(self.owner, self.secret)}", **extra}

    def upload_and_queue(self, pdf: bytes):
        res = self.client.post(f"/api/v1/documents?project_id={self.project}&logical_id=e{uuid.uuid4().hex[:6]}",
                               headers=self.headers(**{"Content-Type": "application/pdf"}), content=pdf)
        self.assertEqual(res.status_code, 201, res.text)
        doc = uuid.UUID(res.json()["id"])
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(), json={})
        self.assertEqual(res.status_code, 202, res.text)
        return doc, uuid.UUID(res.json()["job_id"])

    def consume(self, job, **kw):
        with self.connect() as c:
            return process_parse_job(c, self.project, job, store=self.store, persist=make_persist(self.store),
                                     worker="e2e", max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, **kw)

    def page(self, doc, job, index):
        res = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}/pages/{index}", headers=self.headers())
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_f01_21_pages_real_parse_to_page_results_and_an_idempotent_rerun(self):
        pdf = synthetic_pdf.build(21)
        doc, job = self.upload_and_queue(pdf)
        began = time.monotonic()
        outcome = self.consume(job)
        wall = time.monotonic() - began
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "parsed"))
        summary = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}", headers=self.headers()).json()
        self.assertEqual((summary["parser"], summary["parser_version"], summary["strategy"]),
                         ("unstructured", "0.27.36", "fast"))
        self.assertEqual([p["page_index"] for p in summary["pages"]], list(range(21)))
        texts = []
        for i in range(21):
            body = self.page(doc, job, i)
            seen = squash(" ".join(o["raw_content"] for o in body["observations"]))
            self.assertIn(squash(f"page {i + 1}"), seen, f"page {i + 1} text missing")
            token = squash(synthetic_pdf._page_text(i)[3])
            self.assertIn(token, seen, f"page {i + 1} token missing")
            self.assertTrue(body["evidence"], f"page {i + 1} has no evidence on a plain page")
            for unit in body["evidence"]:
                x0, y0, x1, y1 = unit["regions"][0]["bbox"]
                self.assertTrue(0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1, unit["regions"][0]["bbox"])
                self.assertEqual(unit["regions"][0]["page_index"], i)
            texts.append(seen)
        # A finished job cannot be claimed again; a new request re-parses with identical text.
        with self.assertRaises(jobs.LeaseUnavailable):
            self.consume(job)
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(), json={})
        self.assertEqual(res.status_code, 202, res.text)
        job2 = uuid.UUID(res.json()["job_id"])
        self.assertEqual(self.consume(job2).status, "succeeded")
        again = [squash(" ".join(o["raw_content"] for o in self.page(doc, job2, i)["observations"])) for i in range(21)]
        self.assertEqual(again, texts)
        REPORT["f01_21_pages"] = {"unstructured": md.version("unstructured"), "pdfminer.six": md.version("pdfminer.six"),
                                  "pypdf": md.version("pypdf"), "wall_seconds_first_job": round(wall, 2),
                                  "pages": 21, "pdf_bytes": len(pdf)}

    def test_pages_that_cannot_be_attested_keep_text_and_have_no_geometry(self):
        pdf = hand_built_pdf([(["Plain page alpha"], ""), (["Rotated page bravo"], "/Rotate 90"),
                              (["Cropped page charlie"], "/CropBox [50 50 662 842]")])
        doc, job = self.upload_and_queue(pdf)
        self.assertEqual(self.consume(job).status, "succeeded")
        pages = [self.page(doc, job, i) for i in range(3)]
        self.assertTrue(pages[0]["evidence"])
        for index, word in ((1, "bravo"), (2, "charlie")):
            self.assertEqual(pages[index]["evidence"], [], f"page {index} must carry no geometry")
            self.assertIn(word, squash(" ".join(o["raw_content"] for o in pages[index]["observations"])))
        REPORT["mixed_pages"] = {"evidence_pages": [i for i, p in enumerate(pages) if p["evidence"]],
                                 "synthetic": True}

    def test_cancel_stops_the_real_parser_and_stores_nothing(self):
        doc, job = self.upload_and_queue(synthetic_pdf.build(150))
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("outcome", self.consume(job)))
        worker.start()
        deadline = time.monotonic() + 60
        with self.connect() as c:
            while time.monotonic() < deadline:
                if c.execute("SELECT status::text FROM jobs WHERE id = %s", (job,)).fetchone()[0] == "running":
                    break
                c.rollback()
                time.sleep(0.1)
            time.sleep(1.0)  # let the child start
            with c.transaction():
                c.execute("SELECT set_config('lab.transition_reason', 'user cancel', true)")
                c.execute("UPDATE jobs SET status = 'cancel_requested', lease_owner = NULL, lease_token = NULL, "
                          "lease_expires_at = NULL, row_version = row_version + 1 WHERE id = %s", (job,))
        began = time.monotonic()
        worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["outcome"].status, "cancelled")
        self.assertLess(time.monotonic() - began, 15)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status::text FROM jobs WHERE id = %s", (job,)).fetchone()[0], "cancelled")
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0], 0)
        res = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}", headers=self.headers())
        self.assertEqual(res.status_code, 404)
        REPORT["cancel"] = {"outcome": "cancelled", "seconds_to_stop": round(time.monotonic() - began, 2)}


if __name__ == "__main__":
    unittest.main()
