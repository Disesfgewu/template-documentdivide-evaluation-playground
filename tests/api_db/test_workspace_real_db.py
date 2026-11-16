"""GET /api/v1/workspace and the data surviving an API restart (Issue #194), against real PostgreSQL."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from lab_api.config import load_settings
from lab_api.main import create_app
from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, FreshDatabase, GenerateApiFixtures

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
BASE = "/api/v1/benchmarks/bench-gen/revisions/1"


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class WorkspaceRealDbTests(GenerateApiFixtures, FreshDatabase):
    def restarted_client(self):
        """A NEW API process on the same database and artifact root: what `scripts/demo.sh stop` then `start` gives."""
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url, "LAB_ARTIFACT_ROOT": str(self.root),
               "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only", "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        return TestClient(create_app(settings=settings, checks={}))

    def workspace(self, client, actor, project):
        return client.get(f"/api/v1/workspace?project_id={project}", headers=self.headers(actor))

    def test_an_empty_project_has_nothing_to_resume(self):
        project, owner = self.seed()[:2]
        body = self.workspace(self.client, owner, project).json()
        self.assertEqual((body["documents"][0]["benchmarks"], body["benchmarks"]), (0, []))

    def test_the_workspace_follows_the_benchmark_through_its_states_and_survives_a_restart(self):
        project, owner = self.planned()
        body = self.workspace(self.client, owner, project).json()
        (doc,), (bench,) = body["documents"], body["benchmarks"]
        self.assertEqual((bench["logical_id"], bench["revision"], bench["status"], bench["questions"], bench["uploads"]),
                         ("bench-gen", 1, "planning", 0, 0))
        self.assertEqual((bench["document_id"], doc["benchmarks"], doc["latest_parse_status"]), (doc["id"], 1, "succeeded"))
        self.assertEqual(bench["parse_job_id"], doc["latest_parse_job_id"])
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker("validate_ok")
        res = self.api("post", owner, f"{BASE}/freeze", project, {"compliance": "partial"})
        self.assertEqual(res.status_code, 200, res.text)
        before = self.workspace(self.client, owner, project).json()
        frozen = before["benchmarks"][0]
        self.assertEqual(frozen["status"], "frozen")
        self.assertTrue(frozen["frozen_at"] and frozen["questions"] == frozen["active_questions"] > 0)
        # a restarted API (new process, same database and artifact volume) shows the same thing and still serves the original PDF
        again = self.restarted_client()
        self.assertEqual(self.workspace(again, owner, project).json(), before)
        pdf = again.get(f"/api/v1/documents/{doc['id']}/original?project_id={project}", headers=self.headers(owner))
        self.assertEqual((pdf.status_code, pdf.content[:5]), (200, b"%PDF-"))
        self.assertEqual(again.get(f"{BASE}/export?project_id={project}", headers=self.headers(owner)).status_code, 200)

    def test_no_gold_or_question_text_in_the_workspace_and_strangers_are_refused(self):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        text = self.workspace(self.client, owner, project).text
        for word in ("question_text", "reference_answer", "gold", "evidence_ids"):
            self.assertNotIn(word, text)
        with self.connect() as c:
            _, outsider = self.make_project(c)
        res = self.workspace(self.client, outsider, project)
        self.assertIn(res.status_code, (403, 404))
        self.assertNotIn("documents", res.text)
        self.assertEqual(self.client.get(f"/api/v1/workspace?project_id={project}").status_code, 401)
