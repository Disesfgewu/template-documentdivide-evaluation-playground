"""GET /runs/{id} on real PostgreSQL."""
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
from lab_api.runs import _get_run
from lab_db.repo import Change, record_change

try:
    from tests.db.base import FreshDatabase, ADMIN_URL, h
    from tests.db.test_runs import RunFixture
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RunStatusRealDbTests(FreshDatabase):
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
            f = RunFixture(self, c)
            roles = {r: self.add_member(c, f.p, r) for r in ("participant", "reviewer", "observer")}
            participant_method = uuid.uuid4()
            rv = record_change(c, Change(f.p, roles["participant"], "method.register", "method_revision", "m_2", 1,
                                         h("m2"), "lab-c14n-1", "method.registered"))
            with c.transaction():
                c.execute("INSERT INTO method_revisions VALUES (%s, %s, %s, %s, '1.0', %s, 'jsonl', 'declared_only')",
                          (participant_method, f.p, rv, roles["participant"], h("cfg2")))
            own_run = f.run()
            with c.transaction():
                other_run = uuid.uuid4()
                c.execute("INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile) "
                          "VALUES (%s, %s, %s, %s, 'small')", (other_run, f.p, f.bench, participant_method))
            a1 = f.attempt(own_run, f.q, 1)
            f.submission(own_run, a1, f.q, "accepted", "answered")
            a2 = f.attempt(own_run, f.q2, 1)
            f.submission(own_run, a2, f.q2, "rejected")
            a3 = f.attempt(own_run, f.q2, 2)
            f.submission(own_run, a3, f.q2, "accepted", "abstained")
        return f, roles, own_run, other_run

    def get(self, actor, run):
        return self.client.get(f"/api/v1/runs/{run}",
                               headers={"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"})

    def test_progress_counts_for_the_project_owner(self):
        f, roles, own_run, _ = self.seed()
        body = self.get(f.a, own_run).json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["budget_profile"], "default")
        self.assertEqual(body["benchmark"]["logical_id"], "bench_1")
        self.assertEqual(body["progress"], {"question_count": 2, "attempts": 3, "accepted_answered": 1,
                                            "accepted_abstained": 1, "pending": 0, "rejected_submissions": 1,
                                            "late_submissions": 0})

    def test_progress_uses_one_snapshot_when_a_writer_commits(self):
        # Interleave a separate transaction after the attempts SELECT has
        # executed, before its result is consumed. The old multi-query code
        # returned zero attempts and one accepted answer in this situation.
        with self.connect() as writer, self.connect() as reader:
            f = RunFixture(self, writer)
            run = f.run()
            writer.commit()  # publish the fixture before the reader starts

            class CommitAfterAttempts:
                def __init__(self):
                    self.committed = False

                def execute(proxy, sql, *args):
                    result = reader.execute(sql, *args)
                    if "FROM attempts" in sql and not proxy.committed:
                        proxy.committed = True
                        with writer.transaction():
                            attempt = f.attempt(run, f.q)
                            f.submission(run, attempt, f.q)
                            writer.execute("UPDATE runs SET status = 'running' WHERE id = %s", (run,))
                    return result

            proxy = CommitAfterAttempts()
            before = _get_run(proxy, f.a, run)
            self.assertTrue(proxy.committed)
            self.assertEqual(before["status"], "queued")
            self.assertEqual(before["progress"]["attempts"], 0)
            self.assertEqual(before["progress"]["accepted_answered"], 0)
            after = _get_run(reader, f.a, run)
            self.assertEqual(after["status"], "running")
            self.assertEqual(after["progress"]["attempts"], 1)
            self.assertEqual(after["progress"]["accepted_answered"], 1)

    def test_roles(self):
        f, roles, own_run, other_run = self.seed()
        self.assertEqual(self.get(roles["reviewer"], own_run).status_code, 200)
        self.assertEqual(self.get(roles["participant"], other_run).status_code, 200)  # their own method
        self.assertEqual(self.get(roles["participant"], own_run).status_code, 404)  # someone else's run
        self.assertEqual(self.get(roles["observer"], own_run).status_code, 403)

    def test_outsider_unknown_and_unauthenticated(self):
        f, roles, own_run, _ = self.seed()
        with self.connect() as c:
            _, outsider = self.make_project(c)
        self.assertEqual(self.get(outsider, own_run).status_code, 404)
        self.assertEqual(self.get(f.a, uuid.uuid4()).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/runs/{own_run}").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/runs/not-a-uuid",
                                         headers={"Authorization": f"Bearer {issue_actor_token(f.a, self.secret)}"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
