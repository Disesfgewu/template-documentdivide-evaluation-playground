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
        self.persisted = []

    def persist(self, conn, project, lease, batch):
        self.persisted.append((project, lease.job_id, lease.token, batch))

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

    def process(self, project, job, run, ttl=60, persist=None):
        with self.connect() as c:
            return process_parse_job(c, project, job, store=self.store, persist=persist or self.persist,
                                     worker="test-worker", max_pdf_bytes=64 * 1024 * 1024,
                                     ttl_seconds=ttl, run=run)

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
        self.assertEqual(len(self.persisted), 1)
        self.assertEqual(self.persisted[0][1], job)
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
        self.assertEqual((seen, self.persisted), ([], []))

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
        self.assertEqual(self.persisted, [])

    def test_transient_error_requeues_until_attempts_run_out(self):
        project, job = self.queue()

        def boom(data, **kw):
            raise InspectionError("timeout")

        self.assertEqual(self.process(project, job, boom).status, "queued")
        self.assertEqual(self.job_state(job)[:2], ("queued", 1))
        self.assertEqual(self.process(project, job, boom).status, "queued")
        last = self.process(project, job, boom)  # attempt 3 of 3
        self.assertEqual((last.status, last.reason), ("failed", "timeout"))
        self.assertEqual(self.job_state(job)[:2], ("failed", 3))

    def test_deterministic_error_fails_immediately(self):
        project, job = self.queue()

        def boom(data, **kw):
            raise InspectionError("child_failed")

        outcome = self.process(project, job, boom)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "child_failed"))
        self.assertEqual(self.job_state(job)[:2], ("failed", 1))

    def test_cancel_while_running_stops_the_parser_and_persists_nothing(self):
        project, job = self.queue()
        started = threading.Event()

        def run(data, cancelled, **kw):
            started.set()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if cancelled():
                    raise InspectionError("cancelled")
                time.sleep(0.05)
            return GOOD

        def cancel():
            started.wait(10)
            with self.connect() as c, c.transaction():
                c.execute("SELECT set_config('lab.transition_reason', 'user cancel', true)")
                c.execute("UPDATE jobs SET status = 'cancel_requested', lease_owner = NULL, lease_token = NULL, "
                          "lease_expires_at = NULL WHERE id = %s", (job,))

        t = threading.Thread(target=cancel)
        t.start()
        outcome = self.process(project, job, run, ttl=1)
        t.join(10)
        self.assertEqual((outcome.status, outcome.reason), ("abandoned", "lease_lost_or_cancelled"))
        self.assertEqual(self.persisted, [])
        self.assertEqual(self.job_state(job)[0], "cancel_requested")

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
            raise RuntimeError("disk full")

        outcome = self.process(project, job, self.ok_run(), persist=broken)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "persist_failed"))
        self.assertEqual(self.job_state(job)[0], "failed")

    def test_a_tampered_original_is_never_parsed(self):
        project, job = self.queue()
        blobs = [p for p in self.root.rglob("*") if p.is_file()]
        self.assertEqual(len(blobs), 1)
        blobs[0].write_bytes(b"%PDF-1.4 tampered")
        outcome = self.process(project, job, self.ok_run(seen := []))
        self.assertEqual((outcome.status, outcome.reason, seen), ("failed", "artifact_unreadable", []))


if __name__ == "__main__":
    unittest.main()
