"""/readyz says what is TRUE about question generation (Issue #181), against real PostgreSQL with the fake lab-gen."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB
from lab_api.config import load_settings
from lab_api.generate_worker_main import GenerateWorker, connect_factory
from lab_api.main import create_app
from lab_api.readiness import generation_status

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class GenerationStatusRealDbTests(GenerateApiFixtures, FreshDatabase):
    def settings(self):
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url, "LAB_ARTIFACT_ROOT": str(self.root)}
        with mock.patch.dict(os.environ, env, clear=True):
            return load_settings()

    def readyz(self):
        with mock.patch.dict(os.environ, {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url,
                                           "LAB_ARTIFACT_ROOT": str(self.root)}, clear=True):
            return TestClient(create_app(load_settings())).get("/readyz")

    def clear_workers(self):
        with self.connect() as c:                           # the database is shared by the tests of this class
            c.execute("DELETE FROM worker_status")

    def test_without_a_worker_it_says_so_and_that_model_steps_will_fail(self):
        self.clear_workers()
        status = generation_status(self.settings())
        self.assertEqual(status["state"], "no_worker")
        self.assertIsNone(status["provider_host"])
        self.assertIn("model steps will fail", status["detail"])

    def test_a_running_worker_and_a_real_call_are_reported_without_any_key(self):
        self.clear_workers()
        project, owner = self.planned()
        self.assertEqual(self.api("post", owner, self.GEN, project, BODY).status_code, 202)
        GenerateWorker(connect_factory(self.url), lab_gen_cmd=STUB, cfg=CFG, api_key="sk-secret-test-key", name="status-test", interval=1,
                       backoff=1, grace=5, once_timeout=60, extra_env={"FAKE_LAB_GEN_MODE": "questions"}).run_once()
        res = self.readyz()
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        gen = body["generation"]
        self.assertEqual((gen["state"], gen["provider_host"], gen["protocol"]), ("worker_running", CFG.allowed_host, CFG.protocol))
        self.assertEqual(gen["last_real_call"]["model"], "fake-model")
        self.assertNotIn("sk-secret-test-key", res.text)
        with self.connect() as c:
            row = c.execute("SELECT * FROM worker_status").fetchall()
            self.assertEqual([r[0] for r in row], ["status-test"])
            self.assertNotIn("sk-secret-test-key", repr(row))

    def test_a_worker_that_stopped_reporting_is_no_worker_but_the_past_real_call_stays_visible(self):
        self.clear_workers()
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        GenerateWorker(connect_factory(self.url), lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="status-test", interval=1, backoff=1,
                       grace=5, once_timeout=60, extra_env={"FAKE_LAB_GEN_MODE": "questions"}).run_once()
        with self.connect() as c:
            c.execute("UPDATE worker_status SET updated_at = now() - interval '10 minutes'")
        gen = generation_status(self.settings())
        self.assertEqual(gen["state"], "no_worker")
        self.assertEqual(gen["last_real_call"]["model"], "fake-model")

    def test_an_unmigrated_database_is_unknown_not_a_guess(self):
        self.clear_workers()
        self.assertEqual(generation_status(self.settings())["state"], "no_worker")          # migrated: fine
        with self.connect() as c:
            c.execute("ALTER TABLE worker_status RENAME TO worker_status_gone")
        try:
            self.assertEqual(generation_status(self.settings())["state"], "unknown")
        finally:
            with self.connect() as c:                       # the database is shared by the tests of this class
                c.execute("ALTER TABLE worker_status_gone RENAME TO worker_status")


if __name__ == "__main__":
    unittest.main()
