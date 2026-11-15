"""GET /jobs/{id} on real PostgreSQL."""
from __future__ import annotations

import os
import sys
import tempfile
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
from lab_api.jobs import MAX_REASON_CHARS, REASON_TRUNCATED_MARKER, _get_job
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
        self.assertIn("worker-secret-name", body["transitions"][0]["reason"])  # benign worker name is preserved while secrets are redacted; lease token is never returned
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

    def test_concurrent_finish_interleaving_is_consistent(self):
        """Interleaving regression: second connection commits finish() between reads.
        Single statement snapshot ensures status and transitions are never mixed.
        """
        p, owner, roles, job, lease = self.seed()
        with self.connect() as c1, self.connect() as c2:
            orig_execute = c1.execute
            finish_committed = False

            def execute_hook(query, *args, **kwargs):
                nonlocal finish_committed
                res = orig_execute(query, *args, **kwargs)
                q = str(query)
                # Matches first jobs read (both new project_id gate and old SELECT project_id,kind... read)
                # before transitions are queried, so old code produces mixed snapshots.
                if not finish_committed and "FROM jobs" in q and "LEFT JOIN" not in q:
                    jobs.finish(c2, p, lease, "succeeded", "concurrent finish done")
                    finish_committed = True
                return res

            c1.execute = execute_hook
            out = _get_job(c1, owner, job)

            self.assertTrue(finish_committed)
            # Response must be a wholly consistent snapshot: status and latest transition match
            self.assertEqual(out["status"], "succeeded")
            self.assertEqual(out["transitions"][0]["to"], "succeeded")
            self.assertEqual(out["transitions"][0]["from"], "running")

            # Followup read via API reflects committed terminal state
            followup = self.get_as(owner, job).json()
            self.assertEqual(followup["status"], "succeeded")
            self.assertEqual(followup["transitions"][0]["to"], "succeeded")

    def test_secret_patterns_redacted_on_real_pg_retaining_audit_record(self):
        """Transition reasons with synthetic secrets are sanitized in API response,
        while raw DB audit records retain full diagnostic data (T69/SPEC 10.3).
        """
        p, owner, roles, job, lease = self.seed()
        raw_reason = (
            "failure detail: Authorization: Bearer synthetic-secret-token-12345\n"
            "url: https://user:secretpass123@llm.example.com/v1\n"
            "api_key=" + "sk-" + "synthetic9876543210abcdef\n"
            "Cookie: session=synth-session-456\n"
            "benign worker diagnostic note"
        )
        with self.connect() as c:
            jobs.finish(c, p, lease, "failed", raw_reason)

        for actor in (owner, roles["reviewer"]):
            res = self.get_as(actor, job)
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            transition_reason = body["transitions"][0]["reason"]

            # Secrets masked
            self.assertIn("Authorization: [REDACTED]", transition_reason)
            self.assertIn("https://user:[REDACTED]@llm.example.com/v1", transition_reason)
            self.assertIn("api_key=[REDACTED]", transition_reason)
            self.assertIn("Cookie: [REDACTED]", transition_reason)

            # Benign diagnostic note retained
            self.assertIn("benign worker diagnostic note", transition_reason)

            # No secret text in response body
            for secret in (
                "synthetic-secret-token-12345",
                "secretpass123",
                "sk-" + "synthetic9876543210abcdef",
                "synth-session-456",
            ):
                self.assertNotIn(secret, res.text)

            # No lease token or lease metadata leaked
            for banned in ("lease_token", "lease_owner", "lease_expires_at"):
                self.assertNotIn(banned, body)
            self.assertNotIn("token", res.text)

        # Underlying database record retains raw diagnostic reason for audit
        with self.connect() as c:
            db_reason = c.execute(
                "SELECT reason FROM job_transitions WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                (job,),
            ).fetchone()[0]
            self.assertIn("synthetic-secret-token-12345", db_reason)
            self.assertIn("secretpass123", db_reason)
            self.assertIn("sk-" + "synthetic9876543210abcdef", db_reason)
            self.assertFalse(body["transitions"][0]["reason_truncated"])

    def test_overlong_reason_fail_closed_and_bounded_retaining_raw_audit_record(self):
        """Transition reasons exceeding 2000 chars on real PG fail closed to fixed
        omission marker with reason_truncated=true, ensuring no boundary secrets leak
        and response remains bounded, while DB audit row is untouched.
        """
        p, owner, roles, job, lease = self.seed()
        pk = "PRIVATE" + " KEY"
        pem_key = (
            "-----BEGIN RSA " + pk + "-----\n"
            "MIIEowIBAAKCAQEA0syntheticsecretpgkeyfakefakefake\n"
            "-----END RSA " + pk + "-----\n"
        )
        jwt_token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        sk_key = "sk-" + "synthetic9876543210pgboundarykey"
        # Unicode filler and credential forms across boundary
        large_reason = (
            "診斷前綴 diagnostic: " + "中文多字元測試標記🚨" * 120 + "\n"
            "Authorization: Bearer synthetic-boundary-secret-bearer-999\n"
            "pem: " + pem_key + "\n"
            "jwt: " + jwt_token + "\n"
            "api_key=" + sk_key + "\n"
            "url: https://user:secretpasspgboundary@llm.example.com/v1\n"
            "結尾備註: " + "結尾長字串資料檢查" * 80
        )
        self.assertGreater(len(large_reason), MAX_REASON_CHARS)
        with self.connect() as c:
            jobs.finish(c, p, lease, "failed", large_reason)

        # Assert actual PostgreSQL rows before Python's fallback can hide an
        # accidentally unbounded SQL projection.
        with self.connect() as c:
            original_execute = c.execute
            snapshots = []

            def capture_snapshot(query, *args, **kwargs):
                cursor = original_execute(query, *args, **kwargs)
                if "FROM jobs j" in str(query) and "LEFT JOIN" in str(query):
                    rows = cursor.fetchall()
                    snapshots.append(rows)
                    self.assertEqual(rows[0][10], REASON_TRUNCATED_MARKER)
                    self.assertTrue(rows[0][12])
                    for row in rows:
                        if row[10] is not None:
                            self.assertLessEqual(len(row[10]), MAX_REASON_CHARS)
                            self.assertLessEqual(len(row[10].encode("utf-8")), 4 * MAX_REASON_CHARS)
                    return SimpleNamespace(fetchall=lambda: rows)
                return cursor

            c.execute = capture_snapshot
            _get_job(c, owner, job)
            self.assertEqual(len(snapshots), 1)

        for actor in (owner, roles["reviewer"]):
            res = self.get_as(actor, job)
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            t0 = body["transitions"][0]

            self.assertEqual(t0["reason"], REASON_TRUNCATED_MARKER)
            self.assertTrue(t0["reason_truncated"])
            self.assertLessEqual(len(t0["reason"]), MAX_REASON_CHARS)

            # No secrets or partial secrets leak in response
            for secret in (
                "synthetic-boundary-secret-bearer-999",
                "MIIEowIBAAKCAQEA0syntheticsecretpgkeyfakefakefake",
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
                sk_key,
                "secretpasspgboundary",
            ):
                self.assertNotIn(secret, res.text)

        # Underlying database audit row retains full, unmodified raw diagnostic text
        with self.connect() as c:
            db_reason = c.execute(
                "SELECT reason FROM job_transitions WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                (job,),
            ).fetchone()[0]
            self.assertEqual(db_reason, large_reason)
            self.assertIn("synthetic-boundary-secret-bearer-999", db_reason)
            self.assertIn("secretpasspgboundary", db_reason)

    def test_exact_boundary_chars_on_real_pg(self):
        """Exact 2000 chars returned intact; 2001 chars fail closed to omission marker."""
        p, owner, roles, job1, lease1 = self.seed()
        exact_at_cap = "b" * MAX_REASON_CHARS
        with self.connect() as c:
            jobs.finish(c, p, lease1, "succeeded", exact_at_cap)

        res1 = self.get_as(owner, job1)
        self.assertEqual(res1.status_code, 200, res1.text)
        t1 = res1.json()["transitions"][0]
        self.assertEqual(t1["reason"], exact_at_cap)
        self.assertFalse(t1["reason_truncated"])

        # Over cap by 1 char
        with self.connect() as c:
            job2 = uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO jobs (id, project_id, kind, stage, max_attempts, retryable) "
                          "VALUES (%s, %s, 'parse.document', 'render', 3, true)", (job2, p))
            lease2 = jobs.acquire(c, p, job2, "worker-boundary-tester")
            over_cap = "b" * (MAX_REASON_CHARS + 1)
            jobs.finish(c, p, lease2, "failed", over_cap)

        res2 = self.get_as(owner, job2)
        self.assertEqual(res2.status_code, 200, res2.text)
        t2 = res2.json()["transitions"][0]
        self.assertEqual(t2["reason"], REASON_TRUNCATED_MARKER)
        self.assertTrue(t2["reason_truncated"])


if __name__ == "__main__":
    unittest.main()
