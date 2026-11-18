"""Per-page staging of a parse result on real PostgreSQL (Issue #205, PM-DECISION GO on #204)."""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
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
from lab_api.parse_results import record_from_batch
from lab_db import jobs, parse_staging as ps
from lab_db.parse_results import _canonical
from lab_db.promote import PromoteError, promote_parse_result
from lab_db.storage import LocalArtifactStore
from fixtures.generators import synthetic_pdf
from tests.api_db.test_parse_results_real_db import PAGES, make_batch

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

IDENTITY = {"document_sha256": "a" * 64, "profile": "unstructured-oss-fast", "adapter_revision": "r1", "batch_size": 10, "operation_id": "op", "pages_total": PAGES}


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ParseStagingRealDbTests(FreshDatabase):
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

    def headers(self, actor):
        return {"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"}

    def seed(self):
        with self.connect() as c:
            project, owner = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=s{uuid.uuid4().hex[:6]}",
                               headers={**self.headers(owner), "Content-Type": "application/pdf"}, content=synthetic_pdf.build(PAGES))
        self.assertEqual(res.status_code, 201, res.text)
        doc = uuid.UUID(res.json()["id"])
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(owner), json={})
        job = uuid.UUID(res.json()["job_id"])
        with self.connect() as c:
            op = c.execute("SELECT operation_id FROM parse_jobs WHERE job_id = %s", (job,)).fetchone()[0]
        return project, owner, doc, job, op

    def record(self, s):
        return record_from_batch(make_batch(s[2], s[4]))

    def header_of(self, record):
        result = {k: v for k, v in record.result.items() if k != "pages"}
        return {"manifest": record.manifest, "capabilities": record.capabilities, "result": result, "extra_artifacts": ()}, record

    def stage_all(self, c, project, lease, record, skip=()):
        for p in record.pages:
            if p.page_index not in skip:
                ps.stage_page(c, project, lease, p.page_index, p.status, p.diagnostics, p.evidence, p.observations)
        ps.stage_run(c, project, lease, "b0", record.manifest, record.capabilities, record.raw_artifacts, record.result["raw_result_refs"])

    # --- the write path ---------------------------------------------------------------------------------------------------------------
    def test_pages_are_staged_one_by_one_and_progress_counts_them_without_any_final_row(self):
        s = self.seed()
        project, _, doc, job, _ = s
        record = self.record(s)
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            self.assertEqual(ps.begin(c, project, lease, IDENTITY), ps.Resume(False, frozenset()))
            self.assertEqual(ps.progress(c, job), (0, PAGES))
            for n, p in enumerate(record.pages, start=1):
                size = ps.stage_page(c, project, lease, p.page_index, p.status, p.diagnostics, p.evidence, p.observations)
                self.assertGreater(size, 0)
                self.assertEqual(ps.progress(c, job), (n, PAGES))
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (job,)).fetchone()[0], 0)

    def test_a_page_write_needs_the_jobs_lease(self):
        s = self.seed()
        project, _, _, job, _ = s
        record = self.record(s)
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            stale = jobs.Lease(job, lease.token + 1)
            p = record.pages[0]
            with self.assertRaises(jobs.LeaseLost):
                ps.stage_page(c, project, stale, p.page_index, p.status, p.diagnostics, p.evidence, p.observations)
            jobs.finish(c, project, lease, "failed", "x")
            with self.assertRaises(jobs.LeaseLost):                              # a job that is no longer running takes no more pages
                ps.stage_page(c, project, lease, p.page_index, p.status, p.diagnostics, p.evidence, p.observations)

    # --- readers cannot see staging, by construction ----------------------------------------------------------------------------------
    def test_a_running_job_with_staged_pages_is_invisible_to_the_api_and_to_promote(self):
        s = self.seed()
        project, owner, doc, job, _ = s
        record = self.record(s)
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            self.stage_all(c, project, lease, record)
            self.assertEqual(ps.progress(c, job), (PAGES, PAGES))
            for suffix in ("", "/pages/0", "/pages/1"):
                self.assertEqual(self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}{suffix}", headers=self.headers(owner)).status_code, 404, suffix)
            c.execute("SELECT 1")
        with self.connect() as c:
            bench = self.benchmark_for(c, project, owner, doc)
            with self.assertRaises(PromoteError) as ctx:
                with c.transaction():
                    promote_parse_result(c, project, bench, job, owner)
            self.assertEqual(str(ctx.exception), "parse_result_not_found")

    def benchmark_for(self, c, project, owner, doc):
        from lab_db.repo import Change, record_change
        rv = record_change(c, Change(project, owner, "benchmark.create", "benchmark_revision", f"b{uuid.uuid4().hex[:6]}", 1,
                                     hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(), "lab-c14n-1", "benchmark.created"))
        bench = uuid.uuid4()
        with c.transaction():
            c.execute("INSERT INTO benchmark_revisions (id, project_id, resource_id, document_version_id, n_target, q_min) VALUES (%s, %s, %s, %s, 20, 2)",
                      (bench, project, rv, doc))
        return bench

    def test_a_failed_job_with_staged_pages_is_invisible_and_its_staging_is_discarded(self):
        s = self.seed()
        project, owner, doc, job, _ = s
        record = self.record(s)
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            self.stage_all(c, project, lease, record)
            jobs.finish(c, project, lease, "failed", "pages_failed:1")
            self.assertEqual(self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}", headers=self.headers(owner)).status_code, 404)
            self.assertEqual(ps.discard_staging(c, job), PAGES)
            self.assertIsNone(ps.progress(c, job))
            self.assertEqual(c.execute("SELECT count(*) FROM parse_staging_runs WHERE job_id = %s", (job,)).fetchone()[0], 0)

    def test_no_module_outside_the_parse_worker_reads_the_staging_tables(self):
        allowed = {"db/lab_db/parse_staging.py", "services/api/lab_api/parse_staged.py", "services/api/lab_api/parse_worker_main.py", "db/lab_db/repo.py", "db/lab_db/migrations/0025_parse_staging.sql", "services/api/lab_api/parse_worker.py", "services/api/lab_api/jobs.py", "db/lab_db/schema/versions/0025_parse_staging.py"}
        offenders = []
        for sub in ("db", "services", "scoring", "contracts"):
            for path in (_REPO_ROOT / sub).rglob("*"):
                if path.suffix not in (".py", ".sql") or "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(_REPO_ROOT).as_posix()
                if rel not in allowed and re.search(r"parse_staging", path.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(rel)
        self.assertEqual(offenders, [])

    # --- the finish: atomic, same record hash as the old whole-record write ------------------------------------------------------------
    def test_finish_copies_to_the_final_tables_deletes_staging_and_keeps_the_old_record_hash(self):
        s = self.seed()
        project, owner, doc, job, _ = s
        header, record = self.header_of(self.record(s))
        old_digest = hashlib.sha256(_canonical(record)).hexdigest()
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            self.stage_all(c, project, lease, record)
            info = {}
            jobs.finish(c, project, lease, "succeeded", "parsed", hook=lambda conn: info.update(ps.finish_from_staging(conn, self.store, project, lease, header, PAGES)))
            self.assertEqual(info["record_sha256"], old_digest)                     # byte for byte what the whole-record canonical form gave
            self.assertEqual(c.execute("SELECT record_sha256 FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0], old_digest)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (job,)).fetchone()[0], PAGES)
            self.assertIsNone(ps.progress(c, job))
            self.assertEqual(c.execute("SELECT count(*) FROM parse_staging WHERE job_id = %s", (job,)).fetchone()[0], 0)
            ids = c.execute("SELECT raw_artifact_ids FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0]
            self.assertEqual(len(ids), info["raw_artifacts"])
            for aid in ids:
                state, key, sha = c.execute("SELECT state::text, object_key, sha256 FROM artifacts WHERE id = %s", (aid,)).fetchone()
                self.assertEqual(state, "committed")
                self.assertEqual(hashlib.sha256(self.store.open(key)).hexdigest(), sha)
        res = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}", headers=self.headers(owner))
        self.assertEqual(res.status_code, 200, res.text)                           # after success the ordinary readers see it
        self.assertEqual([p["page_index"] for p in res.json()["pages"]], list(range(PAGES)))

    def test_finish_with_a_missing_page_stores_nothing_and_the_job_stays_running(self):
        s = self.seed()
        project, _, _, job, _ = s
        header, record = self.header_of(self.record(s))
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            self.stage_all(c, project, lease, record, skip={1})
            with self.assertRaises(ps.StagingIncomplete):
                jobs.finish(c, project, lease, "succeeded", "parsed", hook=lambda conn: ps.finish_from_staging(conn, self.store, project, lease, header, PAGES))
            self.assertEqual(c.execute("SELECT status::text FROM jobs WHERE id = %s", (job,)).fetchone()[0], "running")
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0], 0)
            self.assertEqual(ps.progress(c, job), (PAGES - 1, PAGES))                # staging is kept: the next attempt resumes

    # --- resume -----------------------------------------------------------------------------------------------------------------------
    def test_a_second_attempt_with_the_same_identity_resumes_and_a_changed_identity_restarts(self):
        s = self.seed()
        project, _, _, job, _ = s
        record = self.record(s)
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            p0 = record.pages[0]
            ps.stage_page(c, project, lease, p0.page_index, p0.status, p0.diagnostics, p0.evidence, p0.observations)
            jobs.finish(c, project, lease, "queued", "released: worker shutdown")      # the worker went away; staging stays
            lease2 = jobs.acquire(c, project, job, "w2")
            resumed = ps.begin(c, project, lease2, IDENTITY)
            self.assertEqual(resumed, ps.Resume(True, frozenset({0})))
            ps.stage_page(c, project, lease2, p0.page_index, p0.status, p0.diagnostics, p0.evidence, p0.observations)     # the same page again: no duplicate
            self.assertEqual(c.execute("SELECT count(*) FROM parse_staging_pages WHERE job_id = %s", (job,)).fetchone()[0], 1)
            jobs.finish(c, project, lease2, "queued", "released: worker shutdown")
            lease3 = jobs.acquire(c, project, job, "w3")
            restarted = ps.begin(c, project, lease3, {**IDENTITY, "adapter_revision": "r2"})
            self.assertEqual(restarted, ps.Resume(False, frozenset()))
            self.assertEqual(ps.progress(c, job), (0, PAGES))                            # nothing of the old identity is mixed in

    def test_a_page_stored_again_with_other_content_is_a_conflict(self):
        s = self.seed()
        project, _, _, job, _ = s
        record = self.record(s)
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            p0 = record.pages[0]
            ps.stage_page(c, project, lease, 0, "ok", [], p0.evidence, p0.observations)
            with self.assertRaises(ps.StagingConflict):
                ps.stage_page(c, project, lease, 0, "needs_review", ["region_without_ink"], p0.evidence, p0.observations)

    # --- cleanup ----------------------------------------------------------------------------------------------------------------------
    def test_the_sweep_removes_terminal_and_stale_staging_and_leaves_a_live_job_alone(self):
        project, owner, doc, job, _ = self.seed()
        record = self.record((project, owner, doc, job, uuid.uuid4()))
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            p0 = record.pages[0]
            ps.stage_page(c, project, lease, 0, p0.status, p0.diagnostics, p0.evidence, p0.observations)
            ps.sweep_orphans(c)
            self.assertEqual(ps.progress(c, job), (1, PAGES))                         # running and just touched: untouched
            c.execute("UPDATE parse_staging SET touched_at = now() - interval '2 hours' WHERE job_id = %s", (job,))
            ps.sweep_orphans(c)
            self.assertEqual(ps.progress(c, job), (1, PAGES))                         # a running job is not swept for being quiet a while
            jobs.finish(c, project, lease, "failed", "pages_failed:1")                   # crash before discard: the failed job's staging is an orphan
            self.assertGreaterEqual(ps.sweep_orphans(c), 1)
            self.assertIsNone(ps.progress(c, job))

    def test_an_untouched_queued_job_is_swept_only_after_the_period(self):
        project, owner, doc, job, _ = self.seed()
        record = self.record((project, owner, doc, job, uuid.uuid4()))
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, IDENTITY)
            p0 = record.pages[0]
            ps.stage_page(c, project, lease, 0, p0.status, p0.diagnostics, p0.evidence, p0.observations)
            jobs.finish(c, project, lease, "queued", "released: worker shutdown")
            c.execute("UPDATE parse_staging SET touched_at = now() - interval '23 hours' WHERE job_id = %s", (job,))
            ps.sweep_orphans(c)
            self.assertEqual(ps.progress(c, job), (1, PAGES))                         # a queued job retried within a day resumes
            c.execute("UPDATE parse_staging SET touched_at = now() - interval '25 hours' WHERE job_id = %s", (job,))
            ps.sweep_orphans(c)
            self.assertIsNone(ps.progress(c, job))


if __name__ == "__main__":
    unittest.main()
