"""GET /jobs/{id} on real PostgreSQL."""
from __future__ import annotations

import os
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
from lab_db import jobs

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class JobStatusRealDbTests(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url,
               "LAB_ARTIFACT_ROOT": str(Path(tmp.name).resolve()),
               "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only", "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)

    def seed(self):
        with self.connect() as c:
            p, owner = self.make_project(c)
            roles = {r: self.add_member(c, p, r) for r in ("reviewer", "participant", "observer")}
            job = uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO jobs (id, project_id, kind, stage, max_attempts, retryable) "
                          "VALUES (%s, %s, 'parse.document', 'render', 3, true)", (job, p))
            lease = jobs.acquire(c, p, job, "worker-secret-name")
        return p, owner, roles, job, lease

    def get_as(self, actor, job):
        return self.client.get(f"/api/v1/jobs/{job}",
                               headers={"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"})

    def test_running_job_shows_status_and_transitions_but_no_lease(self):
        p, owner, roles, job, lease = self.seed()
        res = self.get_as(owner, job)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual((body["kind"], body["stage"], body["status"], body["attempt"], body["max_attempts"]),
                         ("parse.document", "render", "running", 1, 3))
        self.assertEqual([(t["from"], t["to"]) for t in body["transitions"]], [("queued", "running")])
        self.assertIn("worker-secret-name", body["transitions"][0]["reason"])  # reasons are returned as recorded; they name the worker, never the token
        for banned in ("lease_token", "lease_owner", "lease_expires_at"):
            self.assertNotIn(banned, body)
        self.assertNotIn("token", res.text)

    def test_transitions_newest_first_after_finish(self):
        p, owner, roles, job, lease = self.seed()
        with self.connect() as c:
            jobs.finish(c, p, lease, "succeeded", "done")
        body = self.get_as(roles["reviewer"], job).json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual([(t["from"], t["to"]) for t in body["transitions"]], [("running", "succeeded"), ("queued", "running")])

    def test_roles_outsiders_and_unknown(self):
        p, owner, roles, job, lease = self.seed()
        self.assertEqual(self.get_as(roles["participant"], job).status_code, 403)
        self.assertEqual(self.get_as(roles["observer"], job).status_code, 403)
        with self.connect() as c:
            _, outsider = self.make_project(c)
        self.assertEqual(self.get_as(outsider, job).status_code, 404)
        self.assertEqual(self.get_as(owner, uuid.uuid4()).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/jobs/{job}").status_code, 401)


if __name__ == "__main__":
    unittest.main()
