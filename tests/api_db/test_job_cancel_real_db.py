"""POST /api/v1/jobs/{id}/cancel on real PostgreSQL (PM-DECISION order item a, Refs #7)."""
from __future__ import annotations

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


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class JobCancelRealDbTests(FreshDatabase):
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

    def headers(self, actor):
        return {"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"}

    def seed(self):
        with self.connect() as c:
            project, owner = self.make_project(c)
            members = {r: self.add_member(c, project, r) for r in ("maintainer", "reviewer", "participant", "observer")}
            _, outsider = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=c{uuid.uuid4().hex[:6]}",
                               headers={**self.headers(owner), "Content-Type": "application/pdf"}, content=synthetic_pdf.build(3))
        self.assertEqual(res.status_code, 201, res.text)
        res = self.client.post(f"/api/v1/documents/{res.json()['id']}/parse", headers=self.headers(owner), json={})
        self.assertEqual(res.status_code, 202, res.text)
        return project, owner, members, outsider, uuid.UUID(res.json()["job_id"])

    def cancel(self, actor, job):
        return self.client.post(f"/api/v1/jobs/{job}/cancel", headers=self.headers(actor))

    def state(self, job):
        with self.connect() as c:
            status = c.execute("SELECT status::text FROM jobs WHERE id = %s", (job,)).fetchone()[0]
            audits = c.execute("SELECT actor_id, outcome FROM audit_events WHERE resource_id = %s "
                               "AND action = 'job.cancel_requested'", (job,)).fetchall()
            reasons = [r[0] for r in c.execute("SELECT reason FROM job_transitions WHERE job_id = %s ORDER BY id", (job,))]
        return status, audits, reasons

    def test_a_queued_job_is_cancelled_at_once_with_audit_in_the_same_transaction(self):
        project, owner, members, outsider, job = self.seed()
        res = self.cancel(owner, job)
        self.assertEqual((res.status_code, res.json()), (200, {"job_id": str(job), "status": "cancelled", "changed": True}))
        status, audits, reasons = self.state(job)
        self.assertEqual(status, "cancelled")
        self.assertEqual(audits, [(owner, "succeeded")])
        self.assertEqual(reasons[-1], "cancel requested by user")
        with self.connect() as c:  # a cancelled job is not claimable and frees the document version
            with self.assertRaises(jobs.LeaseUnavailable):
                jobs.acquire(c, project, job, "late-worker")

    def test_repeating_the_request_is_idempotent_and_writes_nothing(self):
        _, owner, members, _, job = self.seed()
        self.cancel(owner, job)
        again = self.cancel(members["maintainer"], job)
        self.assertEqual((again.status_code, again.json()["status"], again.json()["changed"]), (200, "cancelled", False))
        status, audits, _ = self.state(job)
        self.assertEqual(len(audits), 1)

    def test_roles_outsiders_unknown_jobs_and_missing_auth(self):
        _, owner, members, outsider, job = self.seed()
        self.assertEqual(self.client.post(f"/api/v1/jobs/{job}/cancel").status_code, 401)
        self.assertEqual(self.cancel(outsider, job).status_code, 404)                 # non-member
        self.assertEqual(self.cancel(owner, uuid.uuid4()).status_code, 404)           # unknown
        for role in ("reviewer", "participant", "observer"):
            self.assertEqual(self.cancel(members[role], job).status_code, 403, role)
        self.assertEqual(self.state(job)[0], "queued")                                # none of that changed anything
        self.assertEqual(self.cancel(members["maintainer"], job).status_code, 200)    # maintainers may, like queuing

    def test_a_job_whose_worker_died_is_cancelled_straight_away(self):
        project, owner, _, _, job = self.seed()
        with self.connect() as c:
            jobs.acquire(c, project, job, "dead-worker", ttl_seconds=1)   # never heartbeats again
        time.sleep(1.4)                                                    # the lease has expired, status is still running
        res = self.cancel(owner, job)
        self.assertEqual((res.status_code, res.json()["status"], res.json()["changed"]), (200, "cancelled", True))
        status, audits, reasons = self.state(job)
        self.assertEqual((status, len(audits)), ("cancelled", 1))
        self.assertEqual(reasons[-2], "cancel requested by user")
        self.assertIn("lease had expired", reasons[-1])
        with self.connect() as c:  # the document version is free again: a new parse can be queued
            row = c.execute("SELECT document_version_id FROM parse_jobs WHERE job_id = %s", (job,)).fetchone()
        again = self.client.post(f"/api/v1/documents/{row[0]}/parse", headers=self.headers(owner), json={})
        self.assertEqual(again.status_code, 202, again.text)

    def test_a_job_stuck_in_cancel_requested_can_be_recovered_by_repeating_the_request(self):
        project, owner, _, _, job = self.seed()
        with self.connect() as c:
            jobs.acquire(c, project, job, "worker")                        # live lease: a real worker could be running it
        first = self.cancel(owner, job)
        self.assertEqual((first.json()["status"], first.json()["changed"]), ("cancel_requested", True))
        again = self.cancel(owner, job)                                    # recent: still waiting for the worker
        self.assertEqual((again.json()["status"], again.json()["changed"]), ("cancel_requested", False))
        with patch.object(jobs, "CANCEL_ACK_GRACE_SECONDS", 0.0):          # the worker never acknowledged
            recovered = self.cancel(owner, job)
        self.assertEqual((recovered.status_code, recovered.json()["status"], recovered.json()["changed"]),
                         (200, "cancelled", True))
        self.assertEqual(len(self.state(job)[1]), 2)                       # two effective requests, two audit events

    def test_authorization_is_repeated_inside_the_write_transaction(self):
        from lab_db.repo import NotFound, PermissionDenied
        project, owner, members, outsider, job = self.seed()
        with self.connect() as c:
            with self.assertRaises(NotFound):                              # non-member: no way around the check
                jobs.request_cancel(c, project, job, outsider)
            c.execute("UPDATE project_members SET role = 'participant' WHERE project_id = %s AND actor_id = %s",
                      (project, members["maintainer"]))
            c.commit()
            with self.assertRaises(PermissionDenied):                      # demoted after the earlier check
                jobs.request_cancel(c, project, job, members["maintainer"])
        self.assertEqual(self.state(job)[0], "queued")

    def test_a_finished_job_cannot_be_cancelled(self):
        project, owner, _, _, job = self.seed()
        with self.connect() as c:
            lease = jobs.acquire(c, project, job, "w")
            jobs.finish(c, project, lease, "succeeded", "done")
        res = self.cancel(owner, job)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.state(job)[0], "succeeded")

    def test_cancelling_a_running_job_is_observed_by_the_worker_within_the_bound(self):
        project, owner, _, _, job = self.seed()
        started = threading.Event()
        persisted = []

        def run(data, cancelled, **kw):
            started.set()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if cancelled():
                    raise InspectionError("cancelled")
                time.sleep(0.05)
            raise AssertionError("the cancel was never observed")

        result = {}

        def work():
            with self.connect() as c:
                result["outcome"] = process_parse_job(
                    c, project, job, store=self.store, persist=lambda *a: persisted.append(1), worker="w",
                    max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, run=run)  # default 60 s lease

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(started.wait(30))
        res = self.cancel(owner, job)
        self.assertEqual((res.status_code, res.json()["status"], res.json()["changed"]), (200, "cancel_requested", True))
        began = time.monotonic()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - began, 6, "the worker must see the cancel within the 2 s heartbeat bound")
        self.assertEqual((result["outcome"].status, result["outcome"].reason), ("cancelled", "cancel_acknowledged"))
        self.assertEqual(persisted, [])
        status, audits, reasons = self.state(job)
        self.assertEqual((status, len(audits)), ("cancelled", 1))
        self.assertIn("cancel_acknowledged", reasons)
        # A second request now is a no-op.
        self.assertEqual(self.cancel(owner, job).json()["changed"], False)


if __name__ == "__main__":
    unittest.main()
