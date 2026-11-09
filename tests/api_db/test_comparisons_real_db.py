"""GET /comparisons on real PostgreSQL."""
from __future__ import annotations

import hashlib
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
    from tests.db.test_runs import RunFixture
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ComparisonsRealDbTests(FreshDatabase):
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

    def evaluation(self, c, f, run, grader, created):
        ev = uuid.uuid4()
        with c.transaction():  # created_at is explicit: now() is constant inside one outer transaction
            c.execute("INSERT INTO evaluations VALUES (%s, %s, %s, %s, 'provisional', %s)",
                      (ev, run, f.bench, grader, created))
        return ev

    def seed(self):
        with self.connect() as c:
            f = RunFixture(self, c)
            roles = {r: self.add_member(c, f.p, r) for r in ("participant", "reviewer", "observer")}
            a, b, other_grader, bare, twin = (f.run() for _ in range(5))
            self.twin = twin
            big = uuid.uuid4()  # budget_profile is immutable, so it is set on insert
            with c.transaction():
                c.execute("INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile) "
                          "VALUES (%s, %s, %s, %s, 'large')", (big, f.p, f.bench, f.method))
            # run a: an older evaluation must not be the one compared
            old = self.evaluation(c, f, a, "g@0", "2026-01-01T00:00:00Z")
            f.score(old, a, f.q, None, raw=10.0, num=0.2, den=2.0)
            latest = self.evaluation(c, f, a, "g@1", "2026-01-02T00:00:00Z")
            f.score(latest, a, f.q, None, raw=80.0, num=1.6, den=2.0)
            f.score(latest, a, f.q2, None, raw=20.0, num=0.4, den=2.0)
            # run b: same questions, one of them not applicable (must not be averaged in as zero)
            eb = self.evaluation(c, f, b, "g@1", "2026-01-02T00:00:00Z")
            f.score(eb, b, f.q, None, raw=40.0, num=0.8, den=2.0)
            f.score(eb, b, f.q2, None, status="not_applicable", raw=None, num=None, den=None, reasons=("no_claims",))
            # run twin: the same grader and the same scored questions as run a (different values)
            et = self.evaluation(c, f, twin, "g@1", "2026-01-02T00:00:00Z")
            f.score(et, twin, f.q, None, raw=60.0, num=1.2, den=2.0)
            f.score(et, twin, f.q2, None, raw=40.0, num=0.8, den=2.0)
            # run big: same grader, other budget profile, only one question scored
            ec = self.evaluation(c, f, big, "g@1", "2026-01-02T00:00:00Z")
            f.score(ec, big, f.q, None, raw=60.0, num=1.2, den=2.0)
            # run other_grader: other grader revision, same questions
            ed = self.evaluation(c, f, other_grader, "g@2", "2026-01-02T00:00:00Z")
            f.score(ed, other_grader, f.q, None, raw=70.0, num=1.4, den=2.0)
            f.score(ed, other_grader, f.q2, None, raw=30.0, num=0.6, den=2.0)
        return f, roles, a, b, big, other_grader, bare, latest

    def get(self, actor, *runs, **params):
        return self.client.get("/api/v1/comparisons", params={"run_ids": ",".join(map(str, runs)), **params},
                               headers={"Authorization": f"Bearer {issue_actor_token(actor, self.secret)}"})

    @staticmethod
    def question_hash(*ids):
        return hashlib.sha256(",".join(sorted(map(str, ids))).encode()).hexdigest()

    def test_comparable_runs_use_latest_evaluation(self):
        f, _, a, b, *_rest, latest = self.seed()
        res = self.get(f.a, a, self.twin)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["comparable"], body["blockers"])
        self.assertEqual(body["blockers"], [])
        self.assertEqual([r["run_id"] for r in body["runs"]], [str(a), str(self.twin)])
        ra, rt = body["runs"]
        self.assertEqual(ra["evaluation"]["id"], str(latest))
        self.assertEqual(ra["evaluation"]["grader_revision"], "g@1")
        both = self.question_hash(f.q, f.q2)
        self.assertEqual(ra["metrics"], [{"metric_name": "correctness", "scored": 2, "not_applicable": 0, "pending": 0,
                                           "error": 0, "mean": 50.0, "scored_hash": both}])
        self.assertEqual(rt["metrics"], [{"metric_name": "correctness", "scored": 2, "not_applicable": 0, "pending": 0,
                                           "error": 0, "mean": 50.0, "scored_hash": both}])
        self.assertEqual(ra["question_subset"], rt["question_subset"])
        self.assertEqual(ra["question_subset"]["count"], 2)
        self.assertEqual(len(ra["question_subset"]["hash"]), 64)
        self.assertTrue(body["provisional"])

    def test_means_over_different_scored_questions_are_not_comparable_and_na_is_not_a_zero(self):
        # Run a averaged two scored questions; run b has one scored and one N/A. Same question
        # subset, but not the same scored set, so the two means must not be presented as comparable.
        f, _, a, b, *_ = self.seed()
        body = self.get(f.a, a, b).json()
        self.assertFalse(body["comparable"])
        self.assertEqual(body["blockers"], [{
            "condition": "scored_question_set", "metric": "correctness",
            "values": {str(a): {"scored": 2, "hash": self.question_hash(f.q, f.q2)},
                       str(b): {"scored": 1, "hash": self.question_hash(f.q)}}}])
        rb = body["runs"][1]
        self.assertEqual(rb["metrics"], [{"metric_name": "correctness", "scored": 1, "not_applicable": 1, "pending": 0,
                                           "error": 0, "mean": 40.0, "scored_hash": self.question_hash(f.q)}])
        self.assertEqual(body["runs"][0]["question_subset"], rb["question_subset"])

    def test_pending_rows_block(self):
        f, _, a, b, *_ = self.seed()
        insert = "INSERT INTO question_scores VALUES (%s, %s, %s, %s, NULL, 'correctness', %s, %s, %s, %s, %s)"
        with self.connect() as c:
            ev = self.evaluation(c, f, self.twin, "g@1", "2026-01-03T00:00:00Z")  # newer than the seeded one
            with c.transaction():
                c.execute(insert, (ev, self.twin, f.bench, f.q, "pending", None, None, None, ["judge_down"]))
                c.execute(insert, (ev, self.twin, f.bench, f.q2, "scored", 40.0, 0.8, 2.0, []))
        body = self.get(f.a, a, self.twin).json()
        self.assertFalse(body["comparable"])
        conditions = {x["condition"]: x for x in body["blockers"]}
        self.assertEqual(conditions["unfinished_scoring"]["values"], {str(self.twin): {"pending": 1, "error": 0}})
        self.assertIn("scored_question_set", conditions)  # run a scored two questions, the twin only one

    def test_request_order_is_kept(self):
        f, _, a, b, *_ = self.seed()
        self.assertEqual([r["run_id"] for r in self.get(f.a, b, a).json()["runs"]], [str(b), str(a)])

    def test_each_incomparable_condition_is_reported(self):
        f, _, a, b, big, other_grader, bare, _ = self.seed()
        cases = ((big, ["budget_profile", "question_subset", "scored_question_set"]), (other_grader, ["grader_revision"]),
                 (bare, ["evaluation"]))
        for other, expected in cases:
            with self.subTest(expected=expected):
                body = self.get(f.a, a, other).json()
                self.assertFalse(body["comparable"])
                self.assertEqual(sorted(x["condition"] for x in body["blockers"]), expected)
                self.assertEqual(len(body["runs"]), 2)
        body = self.get(f.a, a, bare).json()
        self.assertIsNone(body["runs"][1]["evaluation"])
        self.assertEqual(body["runs"][1]["metrics"], [])

    def test_access(self):
        f, roles, a, b, *_ = self.seed()
        self.assertEqual(self.get(roles["reviewer"], a, b).status_code, 200)
        self.assertEqual(self.get(roles["participant"], a, b).status_code, 404)  # not their runs
        self.assertEqual(self.get(roles["observer"], a, b).status_code, 403)
        with self.connect() as c:
            _, outsider = self.make_project(c)
            g = RunFixture(self, c)  # a second project with its own run
            foreign = g.run()
        self.assertEqual(self.get(outsider, a, b).status_code, 404)
        self.assertEqual(self.get(f.a, a, foreign).status_code, 404)  # no access to the other project
        self.assertEqual(self.get(f.a, a, uuid.uuid4()).status_code, 404)
        self.assertEqual(self.get(f.a, a).status_code, 400)
        self.assertEqual(self.get(f.a, a, b, limit="1").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/comparisons", params={"run_ids": f"{a},{b}"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
