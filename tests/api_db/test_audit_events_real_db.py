"""GET /audit-events on real PostgreSQL."""
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

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class AuditEventsRealDbTests(FreshDatabase):
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

    def event(self, c, project, actor, op, action, outcome, day, resource=None, rtype="document_version"):
        with c.transaction():
            c.execute("INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, "
                      "outcome, operation_id, occurred_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                      "timestamptz '2026-01-01 00:00+00' + make_interval(days => %s))",
                      (uuid.uuid4(), project, actor, action, rtype, resource, outcome, op, day))

    def seed(self):
        with self.connect() as c:
            p, owner = self.make_project(c)
            other = self.add_member(c, p, "maintainer")
            participant = self.add_member(c, p, "participant")
            op = uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO operations (id, project_id, kind, actor_id, status) VALUES (%s, %s, 'k', %s, 'intent')",
                          (op, p, owner))
            res = uuid.uuid4()
            self.event(c, p, owner, op, "document.imported", "succeeded", 1, res)
            self.event(c, p, owner, op, "document.imported", "denied", 2)
            self.event(c, p, other, op, "benchmark.frozen", "succeeded", 3, rtype="benchmark_revision")
            self.event(c, p, other, op, "document.imported", "failed", 4)
            self.event(c, p, owner, op, "benchmark.frozen", "succeeded", 5, rtype="benchmark_revision")
            # another project's event must never appear
            p2, owner2 = self.make_project(c)
            op2 = uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO operations (id, project_id, kind, actor_id, status) VALUES (%s, %s, 'k', %s, 'intent')",
                          (op2, p2, owner2))
            self.event(c, p2, owner2, op2, "document.imported", "succeeded", 1)
        return p, owner, other, participant, res, owner2

    def get(self, actor, project, **params):
        return self.client.get("/api/v1/audit-events", params={"project_id": str(project), **params},
                               headers={"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"})

    def test_newest_first_paging_covers_exactly_the_project(self):
        p, owner, *_ = self.seed()
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": "2"}
            if cursor:
                params["cursor"] = cursor
            body = self.get(owner, p, **params).json()
            seen += [i["id"] for i in body["items"]]
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), 5)
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertEqual(len(set(seen)), 5)

    def test_filters(self):
        p, owner, other, _, res, _ = self.seed()
        count = lambda **kw: len(self.get(owner, p, **kw).json()["items"])
        self.assertEqual(count(action="document.imported"), 3)
        self.assertEqual(count(action="document.imported", outcome="denied"), 1)
        self.assertEqual(count(actor_id=str(other)), 2)
        self.assertEqual(count(resource_id=str(res)), 1)
        self.assertEqual(count(resource_type="benchmark_revision"), 2)
        self.assertEqual(count(since="2026-01-03T00:00:00+00:00"), 4)  # events are on Jan 2..6; lower bound inclusive
        self.assertEqual(count(until="2026-01-03T00:00:00+00:00"), 1)  # upper bound exclusive: only Jan 2
        self.assertEqual(count(since="2026-01-02T00:00:00+00:00", until="2026-01-04T00:00:00+00:00"), 2)
        self.assertEqual(count(action="no.such"), 0)

    def test_access_and_validation(self):
        p, owner, other, participant, _, owner2 = self.seed()
        self.assertEqual(self.get(other, p).status_code, 200)  # maintainer
        self.assertEqual(self.get(participant, p).status_code, 403)
        self.assertEqual(self.get(owner2, p).status_code, 404)  # member of a different project
        self.assertEqual(self.get(owner, p, outcome="x").status_code, 400)
        self.assertEqual(self.get(owner, p, cursor="junk").status_code, 400)
        self.assertEqual(self.get(owner, p, bogus="1").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/audit-events", params={"project_id": str(p)}).status_code, 401)

    def test_item_shape(self):
        p, owner, *_ = self.seed()
        item = self.get(owner, p, limit="1").json()["items"][0]
        self.assertEqual(set(item), {"id", "event_id", "actor_id", "action", "resource_type", "resource_id",
                                     "before_revision", "after_revision", "reason", "outcome", "operation_id",
                                     "trace_id", "occurred_at"})


if __name__ == "__main__":
    unittest.main()
