"""GET /logs on real PostgreSQL."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
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
from lab_db.logs import log_event

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

TRACE = "ab" * 16


def day(n):
    return datetime(2026, 1, n, tzinfo=timezone.utc)


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class LogQueryRealDbTests(FreshDatabase):
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

    def log(self, c, project, level, name, d, **ids):
        return log_event(c, level=level, event_name=name, message="m", service="api", component="c",
                         operation_name="op", occurred_at=day(d), project_id=project,
                         attributes={"note": "ATTR-SECRET"}, error_message="ERR-SECRET", **ids)

    def seed(self):
        with self.connect() as c:
            p, owner = self.make_project(c)
            participant = self.add_member(c, p, "participant")
            self.run_id, self.job_id = uuid.uuid4(), uuid.uuid4()
            self.log(c, p, "INFO", "job.started", 2, job_id=self.job_id, trace_id=TRACE)
            self.log(c, p, "WARN", "job.slow", 3, job_id=self.job_id)
            self.log(c, p, "ERROR", "run.failed", 4, run_id=self.run_id, trace_id=TRACE)
            self.log(c, p, "INFO", "run.started", 5, run_id=self.run_id)
            p2, owner2 = self.make_project(c)
            self.log(c, p2, "INFO", "job.started", 2)
            self.log(c, None, "INFO", "system.boot", 2)  # no project: unreachable here
        return p, owner, participant, owner2

    def get(self, actor, project, **params):
        return self.client.get("/api/v1/logs", params={"project_id": str(project), **params},
                               headers={"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"})

    def test_only_this_projects_events_never_attributes_or_error_message(self):
        p, owner, *_ = self.seed()
        res = self.get(owner, p)
        self.assertEqual(res.status_code, 200, res.text)
        items = res.json()["items"]
        self.assertEqual(len(items), 4)
        self.assertNotIn("system.boot", res.text)
        self.assertNotIn("SECRET", res.text)
        self.assertEqual([i["id"] for i in items], sorted((i["id"] for i in items), reverse=True))

    def test_paging_and_filters(self):
        p, owner, *_ = self.seed()
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": "3"}
            if cursor:
                params["cursor"] = cursor
            body = self.get(owner, p, **params).json()
            seen += [i["id"] for i in body["items"]]
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(set(seen)), 4)
        n = lambda **kw: len(self.get(owner, p, **kw).json()["items"])
        self.assertEqual(n(level="INFO"), 2)
        self.assertEqual(n(level="ERROR"), 1)
        self.assertEqual(n(event_name="job.started"), 1)
        self.assertEqual(n(trace_id=TRACE), 2)
        self.assertEqual(n(job_id=str(self.job_id)), 2)
        self.assertEqual(n(run_id=str(self.run_id)), 2)
        self.assertEqual(n(since="2026-01-04T00:00:00+00:00"), 2)
        self.assertEqual(n(until="2026-01-04T00:00:00+00:00"), 2)
        self.assertEqual(n(level="INFO", run_id=str(self.run_id)), 1)

    def test_access_and_validation(self):
        p, owner, participant, owner2 = self.seed()
        self.assertEqual(self.get(participant, p).status_code, 403)
        self.assertEqual(self.get(owner2, p).status_code, 404)
        self.assertEqual(self.get(owner, p, level="debug").status_code, 400)
        self.assertEqual(self.get(owner, p, attributes="x").status_code, 400)
        self.assertEqual(self.get(owner, p, cursor="junk").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/logs", params={"project_id": str(p)}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
