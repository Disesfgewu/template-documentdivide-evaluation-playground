"""The runnable parse worker on real PostgreSQL (Refs #7): polling, concurrency, backoff, stop.

The parser is a synthetic double that returns a real normalizer batch; the real parser child is
covered by the CI-only e2e acceptance."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.parse_worker_main import Backoff, Worker
from lab_db.storage import LocalArtifactStore
from lab_parser.isolation import InspectionError
from fixtures.generators import synthetic_pdf
from tests.api_db.test_parse_results_real_db import make_batch

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


class BackoffTests(unittest.TestCase):
    def test_delay_doubles_caps_and_expires(self):
        now = [100.0]
        backoff = Backoff(5, clock=lambda: now[0], jitter=lambda: 1.0)
        job = uuid.uuid4()
        self.assertEqual([backoff.hit(job) for _ in range(3)], [5, 10, 20])
        self.assertEqual(backoff.waiting(), {job})
        now[0] += 21
        self.assertEqual(backoff.waiting(), set())     # expired, strikes kept
        self.assertEqual(backoff.hit(job), 40)         # so the next one keeps doubling
        for _ in range(10):
            delay = backoff.hit(job)
        self.assertEqual(delay, 300)                   # capped
        backoff.clear(job)
        self.assertEqual(backoff.hit(job), 5)

    def test_jitter_spreads_the_delay_and_vanished_jobs_are_forgotten(self):
        now = [0.0]
        low, high = Backoff(10, clock=lambda: now[0], jitter=lambda: 0.5), Backoff(10, clock=lambda: now[0], jitter=lambda: 1.5)
        job = uuid.uuid4()
        self.assertEqual((low.hit(job), high.hit(job)), (5, 15))
        import random
        samples = {Backoff(10).hit(job) for _ in range(20)}
        self.assertTrue(all(5 <= x <= 15 for x in samples) and len(samples) > 1)
        now[0] += 5000                    # far beyond four times the cap
        self.assertEqual(low.waiting(), set())
        self.assertEqual(low._strikes, {})


def good_run(data, **kw):
    return make_batch(kw["document_id"], kw["operation_id"])


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ParseWorkerMainRealDbTests(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url,
                    "LAB_ARTIFACT_ROOT": str(self.root), "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only",
                    "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, self.env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)
        self.store = LocalArtifactStore(self.root)

    def queue(self):
        with self.connect() as c:
            project, owner = self.make_project(c)
        headers = {"Authorization": f"Bearer {issue_actor_token(owner, self.secret)}"}
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=w{uuid.uuid4().hex[:6]}",
                               headers={**headers, "Content-Type": "application/pdf"}, content=synthetic_pdf.build(3))
        self.assertEqual(res.status_code, 201, res.text)
        res = self.client.post(f"/api/v1/documents/{res.json()['id']}/parse", headers=headers, json={})
        self.assertEqual(res.status_code, 202, res.text)
        return uuid.UUID(res.json()["job_id"])

    def status(self, job):
        with self.connect() as c:
            return c.execute("SELECT status::text, attempt FROM jobs WHERE id = %s", (job,)).fetchone()

    def worker(self, run, **kw):
        kw.setdefault("interval", 1)
        kw.setdefault("jitter", lambda: 1.0)
        return Worker(self.connect, self.store, name="test-worker", run=run, **kw)

    def drain_foreign_jobs(self):
        """Jobs queued by earlier tests in this class must not be claimed by this one."""
        with self.connect() as c, c.transaction():
            c.execute("SELECT set_config('lab.transition_reason', 'test cleanup', true)")
            c.execute("UPDATE jobs SET status = 'cancelled', row_version = row_version + 1 "
                      "WHERE kind = 'document.parse' AND status = 'queued'")

    def test_once_processes_every_queued_job_with_bounded_concurrency(self):
        self.drain_foreign_jobs()
        jobs_ = [self.queue() for _ in range(4)]
        lock, running, peak = threading.Lock(), [0], [0]

        def run(data, **kw):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.6)
            with lock:
                running[0] -= 1
            return good_run(data, **kw)

        counts = self.worker(run, concurrency=2).run_once()
        self.assertEqual(counts, {"succeeded:parsed": 4})
        self.assertEqual(peak[0], 2)
        self.assertTrue(all(self.status(j)[0] == "succeeded" for j in jobs_))

    def test_a_transient_failure_waits_out_its_backoff_before_the_retry(self):
        self.drain_foreign_jobs()
        job = self.queue()
        calls = []

        def run(data, **kw):
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise InspectionError("timeout")
            return good_run(data, **kw)

        worker = self.worker(run, backoff=1.5)
        self.assertEqual(worker.run_once(), {"queued:timeout": 1})
        self.assertEqual((len(calls), self.status(job)[0]), (1, "queued"))   # not retried at once
        worker.run_once()
        self.assertEqual(len(calls), 1)                                     # still inside the backoff
        time.sleep(1.6)
        self.assertEqual(worker.run_once()["succeeded:parsed"], 1)
        self.assertGreaterEqual(calls[1] - calls[0], 1.5)
        self.assertEqual(self.status(job), ("succeeded", 2))

    def test_a_claim_that_keeps_failing_is_backed_off_not_hot_spun(self):
        from lab_api import parse_worker_main as module
        from lab_db import jobs
        self.drain_foreign_jobs()
        job = self.queue()
        calls = []

        def never_claimable(*a, **kw):
            calls.append(time.monotonic())
            raise jobs.LeaseUnavailable("taken")

        with patch.object(module, "process_parse_job", never_claimable):
            worker = self.worker(good_run, backoff=1.0, interval=1)
            self.assertEqual(worker.run_once(), {"queued:not_claimable": 1})   # --once terminates
            self.assertEqual(len(calls), 1)
            thread = threading.Thread(target=worker.run_forever)
            thread.start()
            time.sleep(3.5)
            worker.stop.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(len(calls), 4, calls)       # 1 s, 2 s, 4 s backoff: a few attempts, not thousands
        self.assertEqual(self.status(job)[0], "queued")

    def test_infrastructure_abandonment_is_backed_off_too(self):
        from lab_api import parse_worker_main as module
        from lab_api.parse_worker import ParseOutcome
        self.drain_foreign_jobs()
        self.queue()
        calls = []

        def unreachable(conn, project_id, job_id, **kw):
            calls.append(1)
            return ParseOutcome(job_id, "abandoned", "infrastructure_error")

        with patch.object(module, "process_parse_job", unreachable):
            worker = self.worker(good_run, backoff=2.0, interval=1)
            thread = threading.Thread(target=worker.run_forever)
            thread.start()
            time.sleep(2.5)
            worker.stop.set()
            thread.join(10)
        self.assertLessEqual(len(calls), 2, calls)

    def test_grace_is_enforced_and_a_second_signal_skips_it(self):
        self.drain_foreign_jobs()
        job = self.queue()
        started = threading.Event()

        def stubborn(data, cancelled, **kw):
            started.set()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if cancelled():
                    raise InspectionError("cancelled")
                time.sleep(0.05)
            return good_run(data, **kw)

        for second_signal in (False, True):
            with self.subTest(second_signal=second_signal):
                started.clear()
                self.drain_foreign_jobs()
                job = self.queue()
                worker = self.worker(stubborn, grace=0.5 if not second_signal else 60)
                thread = threading.Thread(target=worker.run_forever)
                thread.start()
                self.assertTrue(started.wait(30))
                began = time.monotonic()
                worker.stop.set()
                if second_signal:
                    time.sleep(0.3)
                    worker.abort.set()
                thread.join(30)
                self.assertFalse(thread.is_alive(), "the worker must exit although the parser never finishes by itself")
                self.assertLess(time.monotonic() - began, 15)
                self.assertEqual(self.status(job)[0], "running")  # left to lease expiry, never marked finished

    def test_stop_lets_the_running_job_finish_and_claims_nothing_new(self):
        self.drain_foreign_jobs()
        first, second = self.queue(), self.queue()
        started, release = threading.Event(), threading.Event()

        def run(data, **kw):
            started.set()
            release.wait(30)
            return good_run(data, **kw)

        worker = self.worker(run, concurrency=1, grace=20)
        thread = threading.Thread(target=worker.run_forever)
        thread.start()
        self.assertTrue(started.wait(30))
        worker.stop.set()
        time.sleep(0.5)
        release.set()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        statuses = sorted(self.status(j)[0] for j in (first, second))
        self.assertEqual(statuses, ["queued", "succeeded"])               # one finished, none newly claimed

    def test_an_idle_worker_stops_within_one_interval(self):
        self.drain_foreign_jobs()
        worker = self.worker(good_run, interval=2)
        thread = threading.Thread(target=worker.run_forever)
        thread.start()
        time.sleep(0.5)
        began = time.monotonic()
        worker.stop.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - began, 3)

    def test_an_unreachable_database_does_not_crash_the_worker(self):
        def broken():
            raise psycopg.OperationalError("database down")

        worker = Worker(broken, self.store, name="w", run=good_run, interval=1)
        self.assertEqual(worker.run_once(), {})

    def test_the_command_line_validates_its_bounds(self):
        env = {**os.environ, **self.env, "PYTHONPATH": os.pathsep.join(
            str(_REPO_ROOT / p) for p in ("", "db", "services/api", "services/parser", "contracts"))}
        for bad in (["--concurrency", "9"], ["--interval", "0"], ["--backoff", "0"]):
            with self.subTest(bad):
                result = subprocess.run([sys.executable, "-m", "lab_api.parse_worker_main", *bad],
                                        env=env, capture_output=True, text=True, timeout=60, cwd=str(_REPO_ROOT))
                self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
