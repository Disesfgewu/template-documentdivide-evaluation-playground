"""jobs.finish hook atomicity, fail_exhausted and acknowledge_cancel on a real PostgreSQL (Refs #7)."""
import time

from lab_db import jobs

from .base import FreshDatabase
from .test_jobs_logs import JobFixture


class FinishHookTests(FreshDatabase):
    def setUp(self):
        with self.connect() as c, c.transaction():
            c.execute("CREATE TABLE IF NOT EXISTS hook_rows (job_id uuid NOT NULL, seen_status text NOT NULL)")
            c.execute("TRUNCATE hook_rows")

    def rows(self, c):
        return c.execute("SELECT count(*) FROM hook_rows").fetchone()[0]

    def test_hook_runs_under_the_lease_and_commits_with_the_status_change(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            lease = jobs.acquire(c, f.p, f.job, "w1")

            def hook(conn):
                status = conn.execute("SELECT status::text FROM jobs WHERE id = %s", (f.job,)).fetchone()[0]
                conn.execute("INSERT INTO hook_rows VALUES (%s, %s)", (f.job, status))

            jobs.finish(c, f.p, lease, "succeeded", "done", hook=hook)
            self.assertEqual(f.status(), "succeeded")
            self.assertEqual(c.execute("SELECT seen_status FROM hook_rows").fetchone()[0], "running")

    def test_a_failing_hook_rolls_back_its_writes_and_leaves_the_job_running(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            lease = jobs.acquire(c, f.p, f.job, "w1")

            def hook(conn):
                conn.execute("INSERT INTO hook_rows VALUES (%s, 'x')", (f.job,))
                raise RuntimeError("crash between the write and the status change")

            with self.assertRaises(RuntimeError):
                jobs.finish(c, f.p, lease, "succeeded", "done", hook=hook)
            self.assertEqual((f.status(), self.rows(c)), ("running", 0))
            jobs.finish(c, f.p, lease, "failed", "persist_failed")  # the same lease is still valid
            self.assertEqual(f.status(), "failed")

    def test_a_stale_lease_never_runs_the_hook(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            old = jobs.acquire(c, f.p, f.job, "w1", ttl_seconds=1)
            time.sleep(1.3)
            jobs.acquire(c, f.p, f.job, "w2")
            calls = []
            with self.assertRaises(jobs.LeaseLost):
                jobs.finish(c, f.p, old, "succeeded", "late", hook=lambda conn: calls.append(1))
            self.assertEqual(calls, [])

    def test_fail_exhausted_only_finalizes_expired_exhausted_running_jobs(self):
        with self.connect() as c:
            f = JobFixture(self, c, max_attempts=2)
            self.assertFalse(jobs.fail_exhausted(c, f.p, f.job))      # queued: untouched
            jobs.acquire(c, f.p, f.job, "w1", ttl_seconds=1)
            time.sleep(1.3)
            self.assertFalse(jobs.fail_exhausted(c, f.p, f.job))      # attempts remain
            jobs.acquire(c, f.p, f.job, "w2", ttl_seconds=30)
            self.assertFalse(jobs.fail_exhausted(c, f.p, f.job))      # exhausted but lease still live
            self.assertEqual(f.status(), "running")
            c.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second', "
                      "row_version = row_version + 1 WHERE id = %s", (f.job,))
            c.commit()
            self.assertTrue(jobs.fail_exhausted(c, f.p, f.job))
            self.assertEqual(f.status(), "failed")
            reason = c.execute("SELECT reason FROM job_transitions WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                               (f.job,)).fetchone()[0]
            self.assertEqual(reason, "lease_exhausted")

    def test_acknowledge_cancel_only_moves_cancel_requested_to_cancelled(self):
        with self.connect() as c:
            f = JobFixture(self, c)
            self.assertFalse(jobs.acknowledge_cancel(c, f.p, f.job))  # queued
            lease = jobs.acquire(c, f.p, f.job, "w1")
            self.assertFalse(jobs.acknowledge_cancel(c, f.p, f.job))  # running
            jobs.finish(c, f.p, lease, "cancel_requested", "user asked")
            self.assertTrue(jobs.acknowledge_cancel(c, f.p, f.job))
            self.assertEqual(f.status(), "cancelled")
            self.assertFalse(jobs.acknowledge_cancel(c, f.p, f.job))  # terminal
