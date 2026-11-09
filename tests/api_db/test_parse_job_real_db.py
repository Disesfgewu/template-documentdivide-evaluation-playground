"""POST /api/v1/documents/{id}/parse on real PostgreSQL (PM plan item 2, Refs #7)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from fixtures.generators import synthetic_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ParseJobRealDbTests(FreshDatabase):
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

    def headers(self, actor, **extra):
        return {"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}", **extra}

    def seed(self):
        """A project with members and a real 21-page document uploaded through the API."""
        with self.connect() as c:
            project, owner = self.make_project(c)
            others = {r: self.add_member(c, project, r) for r in ("maintainer", "reviewer", "participant", "observer")}
            _, outsider = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id=f01",
                               headers=self.headers(owner, **{"Content-Type": "application/pdf"}),
                               content=synthetic_pdf.build(21))
        self.assertEqual(res.status_code, 201, res.text)
        return project, owner, others, outsider, uuid.UUID(res.json()["id"])

    def count(self, table, project, where=""):
        with self.connect() as c:
            return c.execute(f"SELECT count(*) FROM {table} WHERE project_id = %s{where}", (project,)).fetchone()[0]

    def post(self, actor, doc, body=None, **headers):
        kwargs = {} if body is None else {"json": body}
        return self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(actor, **headers), **kwargs)

    def test_owner_queues_a_job_bound_to_its_input_with_audit(self):
        project, owner, others, outsider, doc = self.seed()
        res = self.post(owner, doc, {"profile": "mineru-standard", "page_selection": [0, 5, 20], "budget_ref": "budget-1"})
        self.assertEqual(res.status_code, 202, res.text)
        body = res.json()
        self.assertEqual((body["status"], body["kind"], body["profile"], body["page_selection"], body["budget_ref"]),
                         ("queued", "document.parse", "mineru-standard", [0, 5, 20], "budget-1"))
        job = uuid.UUID(body["job_id"])
        with self.connect() as c:
            c.row_factory = dict_row
            row = c.execute("SELECT j.status::text AS status, j.kind, j.max_attempts, j.retryable, p.* "
                            "FROM jobs j JOIN parse_jobs p ON p.job_id = j.id WHERE j.id = %s", (job,)).fetchone()
            self.assertEqual((row["status"], row["kind"], row["retryable"], row["max_attempts"]),
                             ("queued", "document.parse", True, 3))
            self.assertEqual((row["document_version_id"], row["project_id"], row["profile"], row["page_selection"],
                              row["budget_ref"], row["requested_by"]),
                             (doc, project, "mineru-standard", [0, 5, 20], "budget-1", owner))
            op = c.execute("SELECT kind, status FROM operations WHERE id = %s", (row["operation_id"],)).fetchone()
            self.assertEqual((op["kind"], op["status"]), ("document.parse", "succeeded"))
            audit = c.execute("SELECT action, resource_type, resource_id, outcome FROM audit_events "
                              "WHERE operation_id = %s", (row["operation_id"],)).fetchall()
            self.assertEqual([(a["action"], a["resource_type"], a["resource_id"], a["outcome"]) for a in audit],
                             [("parse.requested", "parse_job", job, "succeeded")])
        status = self.client.get(f"/api/v1/jobs/{job}", headers=self.headers(owner))
        self.assertEqual((status.status_code, status.json()["status"], status.json()["kind"]), (200, "queued", "document.parse"))

    def test_default_profile_and_maintainer_role(self):
        project, owner, others, outsider, doc = self.seed()
        res = self.post(others["maintainer"], doc)
        self.assertEqual(res.status_code, 202, res.text)
        self.assertEqual((res.json()["profile"], res.json()["page_selection"]), ("unstructured-oss-fast", None))

    def test_invalid_requests_create_nothing(self):
        project, owner, others, outsider, doc = self.seed()
        for body in ({"page_selection": [21]}, {"page_selection": [-1]}, {"page_selection": [3, 3]},
                     {"page_selection": [5, 2]}, {"page_selection": []}, {"page_selection": [True]},
                     {"page_selection": ["1"]}, {"profile": "Bad Profile"}, {"profile": ""},
                     {"unknown": 1}, {"budget_ref": ""}, {"budget_ref": "x" * 129}):
            with self.subTest(body=body):
                res = self.post(owner, doc, body)
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(res.json()["error"]["code"], "bad_request")
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(owner), content=b"{not json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.count("jobs", project), 0)
        self.assertEqual(self.count("parse_jobs", project), 0)
        self.assertEqual(self.count("operations", project, " AND kind = 'document.parse'"), 0)

    def test_idempotent_retry_replays_and_a_changed_request_conflicts(self):
        project, owner, others, outsider, doc = self.seed()
        first = self.post(owner, doc, {"page_selection": [1, 2]}, **{"Idempotency-Key": "k1"})
        self.assertEqual(first.status_code, 202, first.text)
        again = self.post(owner, doc, {"page_selection": [1, 2]}, **{"Idempotency-Key": "k1"})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertIs(again.json()["replayed"], True)
        self.assertEqual({k: v for k, v in again.json().items() if k != "replayed"}, first.json())
        changed = self.post(owner, doc, {"page_selection": [1, 3]}, **{"Idempotency-Key": "k1"})
        self.assertEqual((changed.status_code, changed.json()["error"]["code"]), (409, "conflict"))
        self.assertEqual(self.count("jobs", project), 1)
        other = self.post(owner, doc, {"page_selection": [1, 2]}, **{"Idempotency-Key": "k2"})
        self.assertEqual(other.status_code, 202)
        self.assertNotEqual(other.json()["job_id"], first.json()["job_id"])
        self.assertEqual(self.count("jobs", project), 2)

    def test_roles_outsiders_unknown_documents_and_missing_auth(self):
        project, owner, others, outsider, doc = self.seed()
        for role in ("reviewer", "participant", "observer"):
            self.assertEqual(self.post(others[role], doc).status_code, 403, role)
        self.assertEqual(self.post(outsider, doc).status_code, 404)
        self.assertEqual(self.post(owner, uuid.uuid4()).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/documents/{doc}/parse").status_code, 401)
        self.assertEqual(self.count("jobs", project), 0)

    def test_database_rejects_updates_and_out_of_range_pages_written_directly(self):
        project, owner, others, outsider, doc = self.seed()
        job = uuid.UUID(self.post(owner, doc, {"page_selection": [0]}).json()["job_id"])
        with self.connect() as c:
            with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("UPDATE parse_jobs SET profile = 'x' WHERE job_id = %s", (job,))
            with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("DELETE FROM parse_jobs WHERE job_id = %s", (job,))
            op = uuid.uuid4()
            new_job = uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO operations (id, project_id, kind, actor_id, status) "
                          "VALUES (%s, %s, 'document.parse', %s, 'intent')", (op, project, owner))
                c.execute("INSERT INTO jobs (id, project_id, kind, max_attempts) VALUES (%s, %s, 'document.parse', 1)",
                          (new_job, project))
            for pages in ([21], [3, 3], [4, 2]):
                with self.subTest(pages=pages):
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        with c.transaction():
                            c.execute("INSERT INTO parse_jobs (job_id, project_id, document_version_id, profile, "
                                      "page_selection, operation_id, requested_by) "
                                      "VALUES (%s, %s, %s, 'p', %s, %s, %s)", (new_job, project, doc, pages, op, owner))


if __name__ == "__main__":
    unittest.main()
