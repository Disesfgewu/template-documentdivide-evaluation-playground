"""Scale measurement of the staged parse write (Issue #205, PM addition (a)): 834 and 2000 pages of the density measured on the owner's real 834-page manual
(78,581 evidence units and as many observations: about 94 + 94 JSON objects of ~450 bytes per page, ~85 KB a page, 169 raw outputs).
The 834/2000-page cases exist only with LAB_SCALE=1 (they take a minute; CI counts a skipped test as a failure, so they are not defined otherwise); a small
case always runs. They PRINT the numbers the PR reports and assert only generous bounds."""
from __future__ import annotations

import json
import os
import resource
import sys
import tempfile
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
from lab_api.parse_staged import build_header
from lab_db import jobs, parse_staging as ps
from lab_db.storage import LocalArtifactStore
from fixtures.generators import synthetic_pdf
from tests.parser.test_batch_parse import fake_batch

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


def rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class StagingScaleTests(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url, "LAB_ARTIFACT_ROOT": str(self.root),
               "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only", "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)
        self.store = LocalArtifactStore(self.root)

    def seed(self, pages):
        with self.connect() as c:
            project, owner = self.make_project(c)
        headers = {"Authorization": f"Bearer {issue_actor_token(owner, self.secret)}"}
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=s{uuid.uuid4().hex[:6]}", headers={**headers, "Content-Type": "application/pdf"},
                               content=synthetic_pdf.build(pages))
        doc = uuid.UUID(res.json()["id"])
        job = uuid.UUID(self.client.post(f"/api/v1/documents/{doc}/parse", headers=headers, json={}).json()["job_id"])
        with self.connect() as c:
            op = c.execute("SELECT operation_id FROM parse_jobs WHERE job_id = %s", (job,)).fetchone()[0]
        return project, doc, job, op

    def measure(self, pages):
        project, doc, job, op = self.seed(pages)
        batch = fake_batch((0,), str(doc), str(op) + "-b0", "b")
        manifest, caps = batch.manifest.model_dump(mode="json"), batch.capabilities.model_dump(mode="json")
        filler = "x" * 330
        rss_before = rss_mib()
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, {"pages_total": pages, "document_sha256": "a" * 64})
            t = time.monotonic()
            biggest = 0
            for p in range(pages):
                ev = [{"evidence_id": f"e-{p:04d}{k:03d}" + "0" * 57, "raw_content": filler, "normalized_content": filler, "k": k} for k in range(94)]
                ob = [{"page_index": p, "source_ref": f"s-{p}-{k}", "text": filler, "k": k} for k in range(94)]
                biggest = max(biggest, ps.stage_page(c, project, lease, p, "ok", [], ev, ob, f"{op}-b{p // 10}"))
            stage_s = time.monotonic() - t
            for r in range(0, pages, 10):
                ps.stage_run(c, project, lease, f"{op}-b{r // 10}", manifest, caps, [os.urandom(100_000), json.dumps(manifest).encode()], [f"sha256:{r:064d}"[:71]])
            t = time.monotonic()
            progress = ps.progress(c, job)
            progress_ms = (time.monotonic() - t) * 1000
            rss_staged = rss_mib()
            t = time.monotonic()
            header = build_header(c, job, op, pages)
            header_s = time.monotonic() - t
            header_result_bytes = len(json.dumps(header["result"]))
            t = time.monotonic()
            info = {}
            jobs.finish(c, project, lease, "succeeded", "parsed", hook=lambda conn: info.update(ps.finish_from_staging(conn, self.store, project, lease, header, pages)))
            finish_s = time.monotonic() - t
            rss_done = rss_mib()
            final_bytes = c.execute("SELECT pg_total_relation_size('parse_page_results')").fetchone()[0]
            left = c.execute("SELECT (SELECT count(*) FROM parse_staging), (SELECT count(*) FROM parse_staging_pages), (SELECT count(*) FROM parse_staging_runs)").fetchone()
        print(f"\nSCALE {pages} pages: stage {stage_s:.1f}s ({stage_s / pages * 1000:.0f} ms/page, biggest page {biggest / 1024:.0f} KiB) | progress query {progress_ms:.1f} ms "
              f"({progress}) | build_header {header_s:.2f}s | finish txn {finish_s:.2f}s ({info['bytes'] / 1e6:.1f} MB of pages, {info['raw_artifacts']} raw artifacts) | "
              f"peak RSS MiB: start {rss_before:.0f}, staged {rss_staged:.0f}, finished {rss_done:.0f} | parse_page_results {final_bytes / 1e6:.1f} MB | staging left {left} | header result {header_result_bytes / 1e6:.1f} MB (16.8 MB column cap)")
        self.assertEqual(left, (0, 0, 0))
        self.assertLess(finish_s, 60.0)                          # the lease TTL: a longer finish would need the heartbeat (it has one)
        return finish_s

    def test_a_small_document_goes_through_the_same_path_and_leaves_nothing_behind(self):
        self.measure(60)

    if os.environ.get("LAB_SCALE") == "1":
        def test_834_pages(self):
            self.measure(834)

        def test_2000_pages(self):
            self.measure(2000)


if __name__ == "__main__":
    unittest.main()
