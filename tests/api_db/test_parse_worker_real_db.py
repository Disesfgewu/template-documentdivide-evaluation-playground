"""document.parse consumer on real PostgreSQL (PM plan item 5, Refs #7).

The parser itself is replaced by synthetic doubles (the real one is covered by the
CI-only probe); everything else, leases, heartbeat, job transitions and artifact reads,
is real."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.parse_worker import process_parse_job
from lab_db import jobs
from lab_db.storage import LocalArtifactStore
from lab_parser.isolation import InspectionError
from fixtures.generators import synthetic_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

GOOD = SimpleNamespace(quarantine_reason=None)


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ParseWorkerRealDbTests(FreshDatabase):
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
        with self.connect() as c, c.transaction():
            c.execute("CREATE TABLE IF NOT EXISTS persisted_results (job_id uuid NOT NULL, lease_token bigint NOT NULL)")
            c.execute("TRUNCATE persisted_results")

    def persist(self, conn, project, lease, batch):
        """Stands in for the real persistence: asserts the contract, writes through ``conn``."""
        status, token = conn.execute("SELECT status::text, lease_token FROM jobs WHERE id = %s",
                                     (lease.job_id,)).fetchone()
        assert (status, token) == ("running", lease.token), (status, token)
        conn.execute("INSERT INTO persisted_results VALUES (%s, %s)", (lease.job_id, lease.token))

    def persisted_count(self):
        with self.connect() as c:
            return c.execute("SELECT count(*) FROM persisted_results").fetchone()[0]

    def headers(self, actor, **extra):
        return {"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}", **extra}

    def queue(self, body=None, pages=21):
        """A project, a real uploaded document and a queued parse job; returns (project, job)."""
        with self.connect() as c:
            project, owner = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=f{uuid.uuid4().hex[:6]}",
                               headers=self.headers(owner, **{"Content-Type": "application/pdf"}),
                               content=synthetic_pdf.build(pages))
        self.assertEqual(res.status_code, 201, res.text)
        doc = res.json()["id"]
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(owner), json=body or {})
        self.assertEqual(res.status_code, 202, res.text)
        return project, uuid.UUID(res.json()["job_id"])

    def process(self, project, job, run, ttl=60, persist=None, connect=None, shutdown=None):
        with self.connect() as c:
            return process_parse_job(c, project, job, store=self.store, persist=persist or self.persist,
                                     worker="test-worker", max_pdf_bytes=64 * 1024 * 1024,
                                     connect=connect or self.connect, ttl_seconds=ttl, run=run, shutdown=shutdown)

    def job_state(self, job):
        with self.connect() as c:
            status, attempt = c.execute("SELECT status::text, attempt FROM jobs WHERE id = %s", (job,)).fetchone()
            reasons = [r[0] for r in c.execute("SELECT reason FROM job_transitions WHERE job_id = %s ORDER BY id", (job,))]
        return status, attempt, reasons

    def ok_run(self, seen=None):
        def run(data, **kw):
            if seen is not None:
                seen.append((len(data), kw))
            return GOOD
        return run

    def test_success_persists_before_the_job_is_marked_succeeded(self):
        project, job = self.queue()
        seen = []
        outcome = self.process(project, job, self.ok_run(seen))
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "parsed"))
        self.assertEqual(self.persisted_count(), 1)
        self.assertGreater(seen[0][0], 100)  # the real uploaded bytes reached the parser
        status, attempt, reasons = self.job_state(job)
        self.assertEqual((status, attempt), ("succeeded", 1))
        self.assertEqual(reasons[-1], "parsed")
        with self.assertRaises(jobs.LeaseUnavailable):  # a finished job cannot be claimed again
            self.process(project, job, self.ok_run())

    def test_unsupported_profiles_are_failed_not_run(self):
        project, job = self.queue({"profile": "mineru-standard"})
        run = self.ok_run(seen := [])
        outcome = self.process(project, job, run)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "profile_unsupported"))
        self.assertEqual((seen, self.persisted_count()), ([], 0))

    def test_partial_page_selection_is_failed_and_the_full_range_is_accepted(self):
        project, job = self.queue({"page_selection": [0, 5]})
        outcome = self.process(project, job, self.ok_run(seen := []))
        self.assertEqual((outcome.status, outcome.reason, seen), ("failed", "page_selection_unsupported", []))
        project, job = self.queue({"page_selection": list(range(21))})
        self.assertEqual(self.process(project, job, self.ok_run()).status, "succeeded")

    def test_quarantined_output_is_failed_and_not_persisted(self):
        project, job = self.queue()
        outcome = self.process(project, job, lambda data, **kw: SimpleNamespace(quarantine_reason="invalid_unstructured_output"))
        self.assertEqual((outcome.status, outcome.reason), ("failed", "quarantined:invalid_unstructured_output"))
        self.assertEqual(self.persisted_count(), 0)

    def test_transient_error_requeues_until_attempts_run_out(self):
        project, job = self.queue()

        def boom(data, **kw):
            raise InspectionError("timeout")

        self.assertEqual(self.process(project, job, boom).status, "queued")
        self.assertEqual(self.job_state(job)[:2], ("queued", 1))
        with self.connect() as c:
            limit = c.execute("SELECT max_attempts FROM jobs WHERE id = %s", (job,)).fetchone()[0]   # 5 since the shutdown release
        for _ in range(limit - 2):
            self.assertEqual(self.process(project, job, boom).status, "queued")
        last = self.process(project, job, boom)  # the last attempt
        self.assertEqual((last.status, last.reason), ("failed", "timeout"))
        self.assertEqual(self.job_state(job)[:2], ("failed", limit))

    def test_deterministic_error_fails_immediately(self):
        project, job = self.queue()

        def boom(data, **kw):
            raise InspectionError("child_failed")

        outcome = self.process(project, job, boom)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "child_failed"))
        self.assertEqual(self.job_state(job)[:2], ("failed", 1))

    def test_cancel_while_running_stops_the_parser_acknowledges_and_persists_nothing(self):
        project, job = self.queue()
        started = threading.Event()

        def run(data, cancelled, **kw):
            started.set()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if cancelled():
                    raise InspectionError("cancelled")
                time.sleep(0.05)
            return GOOD

        def cancel():
            started.wait(10)
            with self.connect() as c, c.transaction():
                c.execute("SELECT set_config('lab.transition_reason', 'user cancel', true)")
                # The jobs CHECK allows a lease only on 'running', so a cancel request drops it.
                c.execute("UPDATE jobs SET status = 'cancel_requested', lease_owner = NULL, lease_token = NULL, "
                          "lease_expires_at = NULL, row_version = row_version + 1 WHERE id = %s", (job,))

        t = threading.Thread(target=cancel)
        t.start()
        began = time.monotonic()
        outcome = self.process(project, job, run)  # default 60 s ttl: latency must not scale with it
        elapsed = time.monotonic() - began
        t.join(10)
        self.assertEqual((outcome.status, outcome.reason), ("cancelled", "cancel_acknowledged"))
        self.assertLess(elapsed, 8)
        self.assertEqual(self.persisted_count(), 0)
        self.assertEqual(self.job_state(job)[0], "cancelled")

    def test_a_run_that_raises_anything_fails_the_job_instead_of_leaving_it_running(self):
        project, job = self.queue()

        def boom(data, **kw):
            raise RuntimeError("unexpected")

        outcome = self.process(project, job, boom)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "internal_error"))
        self.assertEqual(self.job_state(job)[0], "failed")

    def test_losing_the_lease_to_another_worker_abandons_without_persisting(self):
        project, job = self.queue()

        def run(data, cancelled, **kw):
            with self.connect() as c, c.transaction():  # another worker takes the job over
                c.execute("UPDATE jobs SET lease_token = lease_token + 1000, lease_owner = 'thief', "
                          "row_version = row_version + 1 WHERE id = %s", (job,))
            deadline = time.monotonic() + 20
            while not cancelled() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise InspectionError("cancelled")

        outcome = self.process(project, job, run)
        self.assertEqual((outcome.status, outcome.reason), ("abandoned", "lease_lost"))
        self.assertEqual(self.persisted_count(), 0)
        self.assertEqual(self.job_state(job)[0], "running")  # the thief owns it now

    def test_takeover_after_a_crash_runs_once_and_the_stale_writer_is_fenced(self):
        project, job = self.queue()
        with self.connect() as c:
            stale = jobs.acquire(c, project, job, "crashed-worker", ttl_seconds=1)  # never finishes
        time.sleep(1.4)
        outcome = self.process(project, job, self.ok_run())
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(self.job_state(job)[:2], ("succeeded", 2))
        self.assertEqual(self.persisted_count(), 1)
        calls = []
        with self.connect() as c, self.assertRaises(jobs.LeaseLost):
            jobs.finish(c, project, stale, "succeeded", "late", hook=lambda conn: calls.append(1))
        self.assertEqual((calls, self.persisted_count()), ([], 1))

    def test_a_job_whose_attempts_are_used_up_by_crashes_is_finalized_not_stuck(self):
        project, job = self.queue()
        with self.connect() as c:
            limit = c.execute("SELECT max_attempts FROM jobs WHERE id = %s", (job,)).fetchone()[0]   # 5 since the shutdown release
        for n in range(limit):
            with self.connect() as c:
                jobs.acquire(c, project, job, f"crashed-{n}", ttl_seconds=1)
            time.sleep(1.3)
        self.assertEqual(self.job_state(job)[:2], ("running", limit))
        with self.assertRaises(jobs.LeaseUnavailable):
            self.process(project, job, self.ok_run())
        status, attempt, reasons = self.job_state(job)
        self.assertEqual((status, attempt, reasons[-1]), ("failed", limit, "lease_exhausted"))

    def test_a_failed_heartbeat_connection_is_retried(self):
        project, job = self.queue()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("transient")
            return self.connect()

        def slow(data, **kw):
            time.sleep(4.5)
            return GOOD

        outcome = self.process(project, job, slow, ttl=2, connect=flaky)
        self.assertEqual(outcome.status, "succeeded")
        self.assertGreaterEqual(len(calls), 2)

    def test_heartbeat_keeps_a_short_lease_alive_during_a_long_parse(self):
        project, job = self.queue()

        def slow(data, **kw):
            time.sleep(4.5)  # longer than the 2 s lease
            return GOOD

        outcome = self.process(project, job, slow, ttl=2)
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(self.job_state(job)[:2], ("succeeded", 1))

    def test_failing_persistence_is_failed_not_succeeded(self):
        project, job = self.queue()

        def broken(conn, project, lease, batch):
            conn.execute("INSERT INTO persisted_results VALUES (%s, %s)", (lease.job_id, lease.token))
            raise RuntimeError("crash after the write, before the status change")

        outcome = self.process(project, job, self.ok_run(), persist=broken)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "persist_failed"))
        self.assertEqual(self.job_state(job)[0], "failed")
        self.assertEqual(self.persisted_count(), 0)  # the write rolled back with the status change

    def test_a_database_error_inside_persist_is_infrastructure_not_a_failed_job(self):
        import psycopg
        project, job = self.queue()

        def flaky(conn, project, lease, batch):
            raise psycopg.OperationalError("connection lost during persist")

        outcome = self.process(project, job, self.ok_run(), persist=flaky)
        self.assertEqual((outcome.status, outcome.reason), ("abandoned", "infrastructure_error"))
        self.assertEqual(self.job_state(job)[0], "running")  # retried after the lease expires

    def test_a_dead_connection_returns_instead_of_raising_and_the_job_is_retried_later(self):
        project, job = self.queue()
        with self.connect() as c:
            def kill(data, **kw):
                c.close()  # the worker's own connection dies while the parser runs
                return GOOD

            outcome = process_parse_job(c, project, job, store=self.store, persist=self.persist, worker="w",
                                        max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, ttl_seconds=1, run=kill)
        self.assertEqual((outcome.status, outcome.reason), ("abandoned", "infrastructure_error"))
        time.sleep(1.5)  # the lease expires; another worker takes the job over
        self.assertEqual(self.process(project, job, self.ok_run()).status, "succeeded")
        self.assertEqual(self.job_state(job)[:2], ("succeeded", 2))

    def test_a_dead_connection_on_the_lease_lost_path_returns_instead_of_raising(self):
        project, job = self.queue()
        with self.connect() as c:
            def run(data, cancelled, **kw):
                with self.connect() as other, other.transaction():  # another worker takes the job over
                    other.execute("UPDATE jobs SET lease_token = lease_token + 1000, lease_owner = 'thief', "
                                  "row_version = row_version + 1 WHERE id = %s", (job,))
                deadline = time.monotonic() + 20
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(0.05)
                c.close()  # the worker's connection dies before it can acknowledge or finish
                raise InspectionError("cancelled")

            outcome = process_parse_job(c, project, job, store=self.store, persist=self.persist, worker="w",
                                        max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, run=run)
        self.assertEqual((outcome.status, outcome.reason), ("abandoned", "infrastructure_error"))

    def test_claiming_with_a_dead_connection_returns_instead_of_raising(self):
        project, job = self.queue()
        c = self.connect()
        c.close()
        outcome = process_parse_job(c, project, job, store=self.store, persist=self.persist, worker="w",
                                    max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, run=self.ok_run())
        self.assertEqual((outcome.status, outcome.reason), ("abandoned", "infrastructure_error"))
        self.assertEqual(self.job_state(job)[:2], ("queued", 0))  # nothing was claimed

    def test_statement_level_database_errors_are_deterministic_not_infrastructure(self):
        import psycopg.errors
        project, job = self.queue()

        def cancelled_statement(conn, project, lease, batch):
            raise psycopg.errors.QueryCanceled("statement timeout")

        outcome = self.process(project, job, self.ok_run(), persist=cancelled_statement)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "persist_failed"))

    def test_a_dead_connection_while_finalizing_an_exhausted_job_still_reports_it_unclaimable(self):
        import psycopg
        project, job = self.queue()
        with self.connect() as c:
            jobs.finish(c, project, jobs.acquire(c, project, job, "first"), "succeeded", "done")  # terminal: not claimable

        def dead(*a, **kw):
            raise psycopg.OperationalError("connection lost")

        with self.connect() as c, patch.object(jobs, "fail_exhausted", dead):
            with self.assertRaises(jobs.LeaseUnavailable):  # the unclaimable answer survives the dead connection
                process_parse_job(c, project, job, store=self.store, persist=self.persist, worker="w",
                                  max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, run=self.ok_run())

    def test_pages_that_stay_failed_after_the_retry_budget_fail_the_job_and_publish_nothing(self):
        from lab_parser.batch_parse import PartialParseFailure
        project, job = self.queue()

        def partial(data, **kw):
            raise PartialParseFailure([3, 4])

        outcome = self.process(project, job, partial)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "pages_failed:2"))   # the count, never content
        self.assertEqual(self.persisted_count(), 0)
        self.assertEqual(self.job_state(job)[:2], ("failed", 1))   # deterministic: not requeued

    def test_a_batch_wide_transient_failure_is_requeued_with_its_own_code(self):
        from lab_parser.batch_parse import parse_in_batches
        project, job = self.queue()
        calls = []

        def failing_batch(data, **kw):
            calls.append(kw["page_indices"])
            raise InspectionError("timeout")

        def batched(data, **kw):
            return parse_in_batches(data, run_batch=failing_batch, **kw)

        outcome = self.process(project, job, batched)
        self.assertEqual((outcome.status, outcome.reason), ("queued", "timeout"))   # exactly as an unbatched run
        self.assertEqual(self.job_state(job)[:2], ("queued", 1))
        self.assertEqual(len(calls), 3)   # 21 pages: three batches, no page-by-page retry

    def test_a_result_over_the_size_cap_fails_the_job_and_stores_nothing(self):
        from lab_db.parse_results import ParseResultTooLarge
        project, job = self.queue()

        def too_large(conn, project, lease, batch):
            raise ParseResultTooLarge(str(lease.job_id))

        outcome = self.process(project, job, self.ok_run(), persist=too_large)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "persist_failed"))   # deterministic, not requeued
        self.assertEqual(self.job_state(job)[:2], ("failed", 1))
        self.assertEqual(self.persisted_count(), 0)

    def test_a_whole_document_total_text_loss_is_requeued_not_published_as_an_empty_success(self):
        # parse_in_batches raises this when no element exists anywhere while pypdf finds text (unit-tested there).
        project, job = self.queue()

        def lost(data, **kw):
            raise InspectionError("text_extraction_empty")

        outcome = self.process(project, job, lost)
        self.assertEqual((outcome.status, outcome.reason), ("queued", "text_extraction_empty"))
        self.assertEqual(self.persisted_count(), 0)
        self.assertEqual(self.job_state(job)[:2], ("queued", 1))

    # --- release on shutdown (PM-DECISION A, Refs #7) -------------------------------------------------------

    @staticmethod
    def aborted(data, **kw):
        """What the batch runner does when the worker aborts its parsers (children already reaped)."""
        raise InspectionError("cancelled")

    def test_a_shutdown_abort_releases_the_job_and_another_worker_takes_it_at_once(self):
        project, job = self.queue()
        outcome = self.process(project, job, self.aborted, shutdown=lambda: True)
        self.assertEqual((outcome.status, outcome.reason), ("queued", "released: worker shutdown"))
        status, attempt, reasons = self.job_state(job)
        self.assertEqual((status, attempt, reasons[-1]), ("queued", 1, "released: worker shutdown"))
        self.assertEqual(self.persisted_count(), 0)                       # nothing was stored for the aborted run
        with self.connect() as c:                                         # lease fields cleared by the guarded transition
            self.assertEqual(c.execute("SELECT lease_owner, lease_token, lease_expires_at FROM jobs WHERE id = %s",
                                       (job,)).fetchone(), (None, None, None))
        outcome = self.process(project, job, self.ok_run())               # no lease expiry to wait for
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "parsed"))
        self.assertEqual(self.job_state(job)[:2], ("succeeded", 2))       # the release still counted as an attempt
        self.assertEqual(self.persisted_count(), 1)

    def test_without_a_shutdown_an_aborted_run_is_still_left_to_the_lease_expiry(self):
        project, job = self.queue()
        for flag in (None, lambda: False):
            with self.subTest(flag):
                outcome = self.process(project, job, self.aborted, shutdown=flag)
                self.assertEqual((outcome.status, outcome.reason), ("abandoned", "lease_lost"))
                self.assertEqual(self.job_state(job)[0], "running")
                with self.connect() as c:                                 # let the lease expire for the next round
                    c.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second', "
                              "row_version = row_version + 1 WHERE id = %s", (job,))
                    c.commit()

    def test_only_the_current_lease_holder_can_release(self):
        project, job = self.queue()
        with self.connect() as c:
            old = jobs.acquire(c, project, job, "old", ttl_seconds=1)
        time.sleep(1.3)                                                   # expired: a release must not pretend it is live
        with self.connect() as c, self.assertRaises(jobs.LeaseLost):
            jobs.release_for_shutdown(c, project, old)
        with self.connect() as c:
            new = jobs.acquire(c, project, job, "new")                    # take-over by a new holder
        with self.connect() as c, self.assertRaises(jobs.LeaseLost):
            jobs.release_for_shutdown(c, project, old)                    # the stale holder is fenced out
        self.assertEqual(self.job_state(job)[:2], ("running", 2))
        with self.connect() as c:
            jobs.release_for_shutdown(c, project, new)                    # the current holder can
        self.assertEqual(self.job_state(job)[0], "queued")

    def test_a_user_cancel_wins_over_the_shutdown_release(self):
        project, job = self.queue()

        def cancelling(data, **kw):
            with self.connect() as c, c.transaction():
                c.execute("SELECT set_config('lab.transition_reason', 'user cancel', true)")
                c.execute("UPDATE jobs SET status = 'cancel_requested', lease_owner = NULL, lease_token = NULL, "
                          "lease_expires_at = NULL, row_version = row_version + 1 WHERE id = %s", (job,))
            raise InspectionError("cancelled")

        outcome = self.process(project, job, cancelling, shutdown=lambda: True)
        self.assertEqual((outcome.status, outcome.reason), ("cancelled", "cancel_acknowledged"))
        self.assertEqual(self.job_state(job)[0], "cancelled")             # never requeued
        self.assertNotIn("released: worker shutdown", self.job_state(job)[2])
        self.assertEqual(self.persisted_count(), 0)

    def test_a_failing_release_falls_back_to_the_lease_expiry(self):
        import psycopg
        project, job = self.queue()
        with patch.object(jobs, "release_for_shutdown", side_effect=psycopg.OperationalError("connection lost")):
            outcome = self.process(project, job, self.aborted, shutdown=lambda: True)
        self.assertEqual((outcome.status, outcome.reason), ("abandoned", "lease_lost"))
        self.assertEqual(self.job_state(job)[0], "running")               # exactly today's behaviour

    def test_parse_jobs_are_enqueued_with_five_attempts(self):
        project, job = self.queue()
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT max_attempts FROM jobs WHERE id = %s", (job,)).fetchone()[0], 5)
        for _ in range(4):                                                # four shutdown releases still leave a fifth try
            self.process(project, job, self.aborted, shutdown=lambda: True)
        self.assertEqual(self.process(project, job, self.ok_run()).status, "succeeded")
        self.assertEqual(self.job_state(job)[:2], ("succeeded", 5))

    def test_a_release_on_the_last_attempt_fails_the_job_instead_of_stranding_it_queued(self):
        """PM review of #143 (P1): a shutdown-abort when attempt == max_attempts must end terminal, never 'queued'."""
        project, job = self.queue()
        with self.connect() as c:
            limit = c.execute("SELECT max_attempts FROM jobs WHERE id = %s", (job,)).fetchone()[0]
        for n in range(1, limit):
            outcome = self.process(project, job, self.aborted, shutdown=lambda: True)
            self.assertEqual((outcome.status, outcome.reason), ("queued", "released: worker shutdown"), n)
        last = self.process(project, job, self.aborted, shutdown=lambda: True)      # the last attempt is interrupted
        self.assertEqual((last.status, last.reason), ("failed", "lease_exhausted"))
        status, attempt, reasons = self.job_state(job)
        self.assertEqual((status, attempt, reasons[-1]), ("failed", limit, "lease_exhausted"))
        with self.connect() as c, self.assertRaises(jobs.LeaseUnavailable):         # nothing can claim it, and it is terminal
            jobs.acquire(c, project, job, "late")
        with self.connect() as c:                                                    # the document version is free again
            active = c.execute("SELECT count(*) FROM parse_jobs p JOIN jobs j ON j.project_id = p.project_id AND j.id = p.job_id "
                               "WHERE p.job_id = %s AND j.status::text = ANY(%s)", (job, list(jobs.ACTIVE_JOB_STATUSES))).fetchone()[0]
        self.assertEqual(active, 0)
        self.assertEqual(self.persisted_count(), 0)

    def test_a_tampered_original_is_never_parsed(self):
        project, job = self.queue()
        blobs = [p for p in self.root.rglob("*") if p.is_file()]
        self.assertEqual(len(blobs), 1)
        blobs[0].write_bytes(b"%PDF-1.4 tampered")
        outcome = self.process(project, job, self.ok_run(seen := []))
        self.assertEqual((outcome.status, outcome.reason, seen), ("failed", "artifact_unreadable", []))


if __name__ == "__main__":
    unittest.main()
