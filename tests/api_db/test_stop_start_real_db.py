"""`scripts/demo.sh ui` reuses the web project and owner; `status` counts what a wipe would destroy and finds artifacts the volume
lost (Issue #194). The two deploy scripts run in-process against real PostgreSQL and a real artifact directory."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, FreshDatabase, GenerateApiFixtures

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def run_script(name, url, root):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": url, "LAB_ARTIFACT_ROOT": str(root),
           "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only", "LAB_DB_CONNECT_TIMEOUT_S": "5"}
    out = io.StringIO()
    with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
        code = module.main()
    assert code == 0
    return dict(line.split("=", 1) for line in out.getvalue().splitlines() if "=" in line)


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class StopStartRealDbTests(GenerateApiFixtures, FreshDatabase):
    def test_ui_creates_the_session_once_then_reuses_the_same_project_and_owner_with_a_fresh_token(self):
        first = run_script("ui_session", self.url, self.root)
        again = run_script("ui_session", self.url, self.root)
        self.assertEqual((first["SESSION"], again["SESSION"]), ("created", "reused"))
        self.assertEqual(first["PROJECT_ID"], again["PROJECT_ID"])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM projects WHERE name = 'web-workflow'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM actors WHERE display = 'web owner'").fetchone()[0], 1)
        for session in (first, again):         # both tokens are the same owner's and are accepted
            res = self.client.get(f"/api/v1/workspace?project_id={session['PROJECT_ID']}",
                                  headers={"Authorization": f"Bearer {session['TOKEN']}"})
            self.assertEqual(res.status_code, 200, res.text)

    def test_status_counts_what_a_wipe_would_destroy_and_reports_artifacts_the_volume_lost(self):
        empty = run_script("data_status", self.url, self.root)
        self.assertEqual((empty["DOCUMENTS"], empty["BENCHMARKS"], empty["DANGLING_ARTIFACTS"]), ("0", "0", "0"))
        project, owner = self.planned()
        status = run_script("data_status", self.url, self.root)
        self.assertEqual((status["DOCUMENTS"], status["BENCHMARKS"], status["FROZEN_BENCHMARKS"], status["DANGLING_ARTIFACTS"]),
                         ("1", "1", "0", "0"))
        self.assertTrue(status["MIGRATION"])
        objects = [p for p in Path(self.root).rglob("*") if p.is_file()]
        self.assertTrue(objects)
        objects[0].unlink()                    # the artifact volume lost an object while the database kept its row
        self.assertEqual(run_script("data_status", self.url, self.root)["DANGLING_ARTIFACTS"], "1")
