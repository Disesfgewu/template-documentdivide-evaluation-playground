"""GET /runs/{id}/results on real PostgreSQL."""
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
from lab_db.repo import Change, record_change

try:
    from tests.db.base import FreshDatabase, ADMIN_URL, h
    from tests.db.test_runs import RunFixture
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RunResultsRealDbTests(FreshDatabase):
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
            pm = uuid.uuid4()
            rv = record_change(c, Change(f.p, roles["participant"], "method.register", "method_revision", "m_2", 1,
                                         h("m2"), "lab-c14n-1", "method.registered"))
            with c.transaction():
                c.execute("INSERT INTO method_revisions VALUES (%s, %s, %s, %s, '1.0', %s, 'jsonl', 'declared_only')",
                          (pm, f.p, rv, roles["participant"], h("cfg2")))
            run = f.run()
            with c.transaction():
                other_run = uuid.uuid4()
                c.execute("INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile) "
                          "VALUES (%s, %s, %s, %s, 'small')", (other_run, f.p, f.bench, pm))
            a1 = f.attempt(run, f.q)
            s1 = f.submission(run, a1, f.q)
            # created_at is explicit: now() is the same for everything inside one outer transaction
            old, ev = uuid.uuid4(), uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO evaluations VALUES (%s, %s, %s, 'grader@1', 'provisional', '2026-01-01T00:00:00Z')",
                          (old, run, f.bench))
                c.execute("INSERT INTO evaluations VALUES (%s, %s, %s, 'grader@2', 'provisional', '2026-01-02T00:00:00Z')",
                          (ev, run, f.bench))
            f.score(old, run, f.q, s1, raw=10.0, num=0.2, den=2.0)
            f.score(ev, run, f.q, s1, raw=75.0, num=1.5, den=2.0)
            f.score(ev, run, f.q2, None, raw=25.0, num=0.5, den=2.0)
            for metric, q, status, vals, why in (
                    ("faithfulness", f.q, "scored", (100.0, 2.0, 2.0), ()),
                    ("faithfulness", f.q2, "not_applicable", (None, None, None), ("no_claims",)),
                    ("recall_at_k", f.q, "pending", (None, None, None), ("awaiting_judge",))):
                with c.transaction():
                    c.execute("INSERT INTO question_scores VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                              (ev, run, f.bench, q, s1 if q == f.q else None, metric, status, *vals, list(why)))
        return f, roles, run, other_run, ev, old

    def get(self, actor, run, **params):
        return self.client.get(f"/api/v1/runs/{run}/results", params=params,
                               headers={"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"})

    def test_latest_evaluation_metrics_are_separate_and_na_is_not_averaged_in(self):
        f, roles, run, _, ev, _ = self.seed()
        res = self.get(f.a, run)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual((body["evaluation"]["id"], body["evaluation"]["status"]), (str(ev), "provisional"))
        metrics = {m["metric_name"]: m for m in body["metrics"]}
        self.assertEqual(metrics["correctness"], {"metric_name": "correctness", "scored": 2, "not_applicable": 0,
                                                   "pending": 0, "error": 0, "mean": 50.0})
        self.assertEqual((metrics["faithfulness"]["scored"], metrics["faithfulness"]["not_applicable"],
                          metrics["faithfulness"]["mean"]), (1, 1, 100.0))
        self.assertEqual((metrics["recall_at_k"]["pending"], metrics["recall_at_k"]["mean"]), (1, None))
        self.assertEqual(len(body["items"]), 5)
        self.assertEqual([i["ordinal"] for i in body["items"]], sorted(i["ordinal"] for i in body["items"]))
        na = [i for i in body["items"] if i["status"] == "not_applicable"][0]
        self.assertEqual((na["raw_value"], na["reason_codes"]), (None, ["no_claims"]))

    def test_explicit_evaluation_filters_and_paging(self):
        f, roles, run, _, ev, old = self.seed()
        body = self.get(f.a, run, evaluation_id=str(old)).json()
        self.assertEqual(body["metrics"][0]["mean"], 10.0)
        body = self.get(f.a, run, metric_name="correctness").json()
        self.assertEqual([m["metric_name"] for m in body["metrics"]], ["correctness"])
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(len(self.get(f.a, run, quota_bucket="general").json()["items"]), 5)
        self.assertEqual(self.get(f.a, run, quota_bucket="confusable").json()["items"], [])
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": "2"}
            if cursor:
                params["cursor"] = cursor
            page = self.get(f.a, run, **params).json()
            seen += [(i["ordinal"], i["metric_name"]) for i in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)
        self.assertEqual(seen, sorted(seen))

    def test_access(self):
        f, roles, run, other_run, ev, old = self.seed()
        self.assertEqual(self.get(roles["reviewer"], run).status_code, 200)
        self.assertEqual(self.get(roles["participant"], run).status_code, 404)  # not theirs
        self.assertEqual(self.get(roles["participant"], other_run).status_code, 403)  # theirs, no feedback profile
        self.assertEqual(self.get(roles["observer"], run).status_code, 403)
        with self.connect() as c:
            _, outsider = self.make_project(c)
        self.assertEqual(self.get(outsider, run).status_code, 404)
        self.assertEqual(self.get(f.a, other_run).status_code, 404)  # a run with no evaluation
        self.assertEqual(self.get(f.a, run, evaluation_id=str(uuid.uuid4())).status_code, 404)
        self.assertEqual(self.get(f.a, run, metric_name="total").status_code, 400)
        self.assertEqual(self.get(f.a, run, cursor="junk").status_code, 400)
        self.assertEqual(self.client.get(f"/api/v1/runs/{run}/results").status_code, 401)


if __name__ == "__main__":
    unittest.main()
