"""The staged parse path end to end on real PostgreSQL: the real batch runner with a synthetic per-batch parser, the real worker, the real finish transaction
(Issue #205, PM-DECISION GO on #204). The parser child is replaced by the synthetic batches of tests/parser/test_batch_parse.py."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
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
from lab_api.parse_worker import process_parse_job
from lab_db import jobs, parse_staging as ps
from lab_db.parse_results import _canonical
from lab_db.storage import LocalArtifactStore
from lab_parser.batch_parse import parse_in_batches
from lab_parser.isolation import InspectionError
from fixtures.generators import synthetic_pdf
from tests.parser.test_batch_parse import PAGES, fake_batch

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


class FakeParser:
    """The synthetic per-batch parser. Results are cached per (pages, operation) so two parses of the same job give the same bytes."""
    def __init__(self, fail=None, observe=None):
        self.calls, self.cache, self.fail, self.observe, self.lock = [], {}, fail, observe, threading.Lock()

    def __call__(self, data, *, geometry, page_indices, cancelled, python, document_id, budget_ref, operation_id):
        key = (tuple(page_indices), operation_id)
        with self.lock:
            self.calls.append(tuple(page_indices))
            n = sum(1 for c in self.calls if c == tuple(page_indices))
        if self.fail:
            self.fail(tuple(page_indices), n)
        if self.observe:
            self.observe(tuple(page_indices))
        with self.lock:
            if key not in self.cache:
                self.cache[key] = fake_batch(page_indices, document_id, operation_id, budget_ref)
            return self.cache[key]


def staged_run(parser):
    def run(data, *, document_id, budget_ref, operation_id, cancelled, python, page_sink, run_sink, staged, staged_any_observation):
        return parse_in_batches(data, document_id=document_id, budget_ref=budget_ref, operation_id=operation_id, run_batch=parser, cancelled=cancelled,
                                python=python, page_sink=page_sink, run_sink=run_sink, staged=staged, staged_any_observation=staged_any_observation)
    return run


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class StagedParseWorkerRealDbTests(FreshDatabase):
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

    def queue(self):
        with self.connect() as c:
            project, owner = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=w{uuid.uuid4().hex[:6]}",
                               headers={**self.headers(owner), "Content-Type": "application/pdf"}, content=synthetic_pdf.build(PAGES))
        self.assertEqual(res.status_code, 201, res.text)
        doc = uuid.UUID(res.json()["id"])
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(owner), json={})
        self.assertEqual(res.status_code, 202, res.text)
        self.owner, self.doc = owner, doc
        return project, uuid.UUID(res.json()["job_id"])

    def process(self, project, job, run, shutdown=None):
        with self.connect() as c:
            return process_parse_job(c, project, job, store=self.store, persist=lambda *a: self.fail("the staged path never calls persist"), worker="w",
                                     max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, ttl_seconds=60, run=run, shutdown=shutdown)

    def job_row(self, job):
        with self.connect() as c:
            return c.execute("SELECT status::text, stage, attempt FROM jobs WHERE id = %s", (job,)).fetchone()

    def job_api(self, project, job):
        return self.client.get(f"/api/v1/jobs/{job}?project_id={project}", headers=self.headers(self.owner)).json()

    # --- success ----------------------------------------------------------------------------------------------------------------------
    def test_a_staged_parse_succeeds_with_the_same_record_hash_as_the_whole_document_path(self):
        project, job = self.queue()
        parser = FakeParser()
        outcome = self.process(project, job, staged_run(parser))
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "parsed"))
        with self.connect() as c:
            stored = c.execute("SELECT record_sha256, operation_id FROM parse_results WHERE job_id = %s", (job,)).fetchone()
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (job,)).fetchone()[0], PAGES)
            self.assertIsNone(ps.progress(c, job))                                  # staging is gone
            self.assertEqual(c.execute("SELECT count(*) FROM parse_staging WHERE job_id = %s", (job,)).fetchone()[0], 0)
            ids = c.execute("SELECT raw_artifact_ids FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0]
            for aid in ids:
                state, key, sha = c.execute("SELECT state::text, object_key, sha256 FROM artifacts WHERE id = %s", (aid,)).fetchone()
                self.assertEqual(state, "committed")
                self.assertEqual(hashlib.sha256(self.store.open(key)).hexdigest(), sha)
        # the old path: the same cached batches merged in memory and written as one record
        old = parse_in_batches(synthetic_pdf.build(PAGES), document_id=str(self.doc), budget_ref="unbudgeted", operation_id=str(stored[1]), run_batch=parser)
        self.assertEqual(stored[0], hashlib.sha256(_canonical(record_from_batch(old))).hexdigest())
        self.assertEqual(self.job_row(job)[:2], ("succeeded", "finishing"))
        res = self.client.get(f"/api/v1/documents/{self.doc}/parse-results/{job}", headers=self.headers(self.owner))
        self.assertEqual(res.status_code, 200, res.text)
        done = self.job_api(project, job)["progress"]
        self.assertEqual((done["pages_done"], done["pages_total"]), (PAGES, PAGES))

    def test_while_the_job_runs_progress_counts_stored_pages_and_nothing_is_readable(self):
        project, job = self.queue()
        seen = []

        def observe(pages):
            if pages == tuple(range(20, 21)):                                      # the last batch is about to run: the first two are stored
                api = self.job_api(project, job)
                seen.append((api["status"], api["stage"], api["progress"]))
                seen.append(self.client.get(f"/api/v1/documents/{self.doc}/parse-results/{job}", headers=self.headers(self.owner)).status_code)
                seen.append(self.client.get(f"/api/v1/documents/{self.doc}/parse-results/{job}/pages/0", headers=self.headers(self.owner)).status_code)

        with patch.object(sys.modules["lab_parser.batch_parse"], "DEFAULT_LIMITS", sys.modules["lab_parser.batch_parse"].DEFAULT_LIMITS.__class__(
                max_pages=2000, batch_size=10, overlap_pages=0, parallel_batches=1)):
            # parallel_batches=1 makes the order deterministic: batch 0, batch 1, then the last page
            outcome = self.process(project, job, staged_run(FakeParser(observe=observe)))
        self.assertEqual(outcome.status, "succeeded")
        status, stage, progress = seen[0]
        self.assertEqual((status, stage), ("running", "parsing"))
        self.assertEqual((progress["pages_done"], progress["pages_total"]), (20, PAGES))
        self.assertEqual(seen[1:], [404, 404])                                       # a running job's result is invisible to the readers

    # --- failure: partial pages are not usable and not kept ----------------------------------------------------------------------------
    def test_pages_that_stay_failed_fail_the_job_in_the_parsing_stage_and_leave_no_staging(self):
        project, job = self.queue()

        def fail(pages, n):
            if set(pages) & set(range(10, 20)):
                raise InspectionError("child_failed")
        outcome = self.process(project, job, staged_run(FakeParser(fail=fail)))
        self.assertEqual((outcome.status, outcome.reason), ("failed", "pages_failed:10"))
        self.assertEqual(self.job_row(job)[:2], ("failed", "parsing"))              # the stage it failed in, not "queued"
        with self.connect() as c:
            self.assertIsNone(ps.progress(c, job))                                  # the 11 good pages were not left behind
            self.assertEqual(c.execute("SELECT count(*) FROM parse_staging_runs WHERE job_id = %s", (job,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0], 0)
        self.assertEqual(self.client.get(f"/api/v1/documents/{self.doc}/parse-results/{job}", headers=self.headers(self.owner)).status_code, 404)
        self.assertIsNone(self.job_api(project, job)["progress"])

    def test_a_page_write_that_fails_is_a_specific_reason_in_the_storing_stage_and_is_logged(self):
        project, job = self.queue()

        def broken(*a, **k):
            raise ValueError("SECRET-PAGE-TEXT must not be logged")

        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"LAB_APP_LOG": "full", "LAB_APP_LOG_FILE": d + "/app.log"}), patch.object(ps, "stage_page", broken):
            outcome = self.process(project, job, staged_run(FakeParser()))
            log = Path(d + "/app.log").read_text()
        self.assertEqual((outcome.status, outcome.reason), ("failed", "page_write_failed:ValueError"))
        self.assertEqual(self.job_row(job)[:2], ("failed", "storing"))
        line = [l for l in log.splitlines() if "stage_page" in l and "[ERROR]" in l][0]
        self.assertIn("ValueError", line)
        self.assertIn("page_write_failed", line)
        self.assertNotIn("SECRET-PAGE-TEXT", log)

    # --- resume ------------------------------------------------------------------------------------------------------------------------
    def test_after_an_interruption_the_next_attempt_resumes_and_gives_the_same_result(self):
        project, job = self.queue()
        parser = FakeParser()

        def interrupt(pages, n):
            if pages == (20,) and n == 1:
                raise InspectionError("cancelled")                                  # the worker is shutting down: its children are stopped
        parser.fail = interrupt
        outcome = self.process(project, job, staged_run(parser), shutdown=lambda: True)
        self.assertEqual((outcome.status, outcome.reason), ("queued", "released: worker shutdown"))   # back to the queue, the staging stays
        with self.connect() as c:
            self.assertEqual(ps.progress(c, job), (20, PAGES))                      # the two finished batches are stored
        before = len(parser.calls)
        outcome = self.process(project, job, staged_run(parser))
        self.assertEqual(outcome.status, "succeeded")
        second = parser.calls[before:]
        self.assertNotIn(tuple(range(0, 10)), second)                               # batches 0 and 1 were not run again
        self.assertNotIn(tuple(range(10, 20)), second)
        self.assertIn((20,), second)
        with self.connect() as c:
            stored = c.execute("SELECT record_sha256, operation_id FROM parse_results WHERE job_id = %s", (job,)).fetchone()
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (job,)).fetchone()[0], PAGES)     # no duplicate, no gap
        old = parse_in_batches(synthetic_pdf.build(PAGES), document_id=str(self.doc), budget_ref="unbudgeted", operation_id=str(stored[1]), run_batch=parser)
        self.assertEqual(stored[0], hashlib.sha256(_canonical(record_from_batch(old))).hexdigest())      # identical to an uninterrupted parse

    def test_a_changed_page_on_resume_discards_the_staging_and_restarts_from_page_zero(self):
        project, job = self.queue()
        parser = FakeParser()
        parser.fail = lambda pages, n: (_ for _ in ()).throw(InspectionError("cancelled")) if pages == (20,) and n == 1 else None
        self.process(project, job, staged_run(parser), shutdown=lambda: True)         # leaves 20 pages staged and the job queued
        parser.fail = None
        # the next attempt's parser answers batch 1 with different text: the stored copy of those pages cannot be trusted
        parser.cache = {k: v for k, v in parser.cache.items() if k[0] != tuple(range(0, 10))}      # batch 0 will be re-run only if it is not fully staged
        with self.connect() as c:
            c.execute("DELETE FROM parse_staging_pages WHERE job_id = %s AND page_index = 5", (job,))     # batch 0 is now partly staged: it must be re-run whole
            c.commit()
        changed = {}

        original = parser.__call__

        def different(data, *, geometry, page_indices, cancelled, python, document_id, budget_ref, operation_id):
            batch = original(data, geometry=geometry, page_indices=page_indices, cancelled=cancelled, python=python, document_id=document_id,
                             budget_ref=budget_ref, operation_id=operation_id)
            if tuple(page_indices) == tuple(range(0, 10)) and not changed:
                changed["once"] = True
                from tests.parser.test_batch_parse import fake_batch as fb
                return fb(page_indices, document_id, operation_id, budget_ref, blank={2})        # page 2 now has no text: a different page
            return batch

        outcome = self.process(project, job, staged_run(different))
        self.assertEqual(outcome.status, "succeeded")                                 # it restarted from 0 and finished
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (job,)).fetchone()[0], PAGES)

    def test_the_header_lists_every_evidence_id_but_leaves_the_list_out_beyond_the_limit_and_says_so(self):
        from lab_api import parse_staged
        project, job = self.queue()
        self.process(project, job, staged_run(FakeParser()))
        with self.connect() as c:
            kept = c.execute("SELECT result FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0]
        self.assertEqual(len(kept["normalized_evidence_refs"]), PAGES)           # a normal document: the list as before (and the hash as before)
        self.assertNotIn("evidence_refs_omitted", kept)
        project, job = self.queue()
        with patch.object(parse_staged, "MAX_HEADER_REFS_BYTES", 100):
            outcome = self.process(project, job, staged_run(FakeParser()))
        self.assertEqual(outcome.status, "succeeded")
        with self.connect() as c:
            omitted = c.execute("SELECT result FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0]
            self.assertEqual(c.execute("SELECT count(*) FROM parse_page_results WHERE job_id = %s", (job,)).fetchone()[0], PAGES)
        self.assertEqual((omitted["normalized_evidence_refs"], omitted["evidence_refs_omitted"]), ([], PAGES))

    # --- the worker: sinks through its wrapper, and the sweep ------------------------------------------------------------------------
    def test_the_worker_wrapper_hands_the_staging_sinks_to_the_real_runner_and_a_test_double_without_them_is_left_alone(self):
        from lab_api.parse_worker_main import Worker
        worker = Worker(self.connect, self.store, name="t")
        self.assertTrue(worker._run_with_abort().accepts_sinks)                  # the real parse_in_batches takes the sinks
        worker.run = lambda data, **kw: None
        self.assertFalse(worker._run_with_abort().accepts_sinks)                 # a double with **kw is not given them: the old persist contract

    def test_the_worker_sweeps_orphaned_staging_at_start_and_then_at_most_hourly(self):
        from lab_api.parse_worker_main import Worker
        project, job = self.queue()
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w1")
            ps.begin(c, project, lease, {"pages_total": PAGES})
            jobs.finish(c, project, lease, "failed", "pages_failed:1")             # crash before the discard: an orphan
            self.assertEqual(ps.progress(c, job), (0, PAGES))
        worker = Worker(self.connect, self.store, name="t")
        worker._sweep_staging(force=True)
        with self.connect() as c:
            self.assertIsNone(ps.progress(c, job))
        with self.connect() as c:                                                  # a new orphan is not swept again within the hour
            c.execute("INSERT INTO parse_staging (job_id, project_id, identity) VALUES (%s, %s, '{\"pages_total\": 1}')", (job, project))
            c.commit()
        worker._sweep_staging()
        with self.connect() as c:
            self.assertEqual(ps.progress(c, job), (0, 1))
        worker._last_sweep -= 3601
        worker._sweep_staging()
        with self.connect() as c:
            self.assertIsNone(ps.progress(c, job))

    # --- the legacy contract still works -----------------------------------------------------------------------------------------------
    def test_a_run_double_that_returns_a_batch_still_goes_through_persist(self):
        from types import SimpleNamespace
        project, job = self.queue()
        calls = []

        def run(data, **kw):
            return SimpleNamespace(quarantine_reason=None)

        with self.connect() as c:
            outcome = process_parse_job(c, project, job, store=self.store, persist=lambda conn, p, lease, batch: calls.append(1), worker="w",
                                        max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, ttl_seconds=60, run=run)
        self.assertEqual((outcome.status, calls), ("succeeded", [1]))


if __name__ == "__main__":
    unittest.main()
