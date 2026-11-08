"""Public question set endpoint on real PostgreSQL: paging order, role and
freeze visibility, and that no gold leaves the service."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
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
    from tests.db.test_benchmarks import Fixture
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class PublicQuestionsRealDbTests(FreshDatabase):
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

    def seed(self, n=5, freeze=True):
        with self.connect() as c:
            f = Fixture(self, c)
            f.q  # ordinal 0 exists already (q_1)
            with c.transaction():
                c.execute("UPDATE questions SET payload = %s WHERE id = %s",
                          (json.dumps({"question_id": "q_1", "language": "en", "answer": "SECRET-ANSWER",
                                       "facts": ["SECRET-FACT"]}), f.q))
            for i in range(1, n):
                f.question(f"q_{i + 1}", i)
            with c.transaction():
                c.execute("UPDATE questions SET payload = payload || %s::jsonb WHERE benchmark_id = %s",
                          (json.dumps({"language": "en"}), f.bench))
            expected_ids = [str(row[0]) for row in c.execute(
                "SELECT id FROM questions WHERE benchmark_id = %s ORDER BY ordinal", (f.bench,)).fetchall()]
            f.evidence_set(f.q, "s1", [f.e1])
            roles = {r: self.add_member(c, f.p, r) for r in ("participant", "reviewer", "observer")}
            if freeze:
                f.freeze()
        f.expected_question_ids = expected_ids
        return f, roles

    def get(self, actor, project, path="/api/v1/benchmarks/bench_1/revisions/1/questions", **params):
        token = issue_actor_token(actor, self.secret)
        return self.client.get(path, params={"project_id": str(project), **params},
                               headers={"Authorization": f"Bearer {token}"})

    def test_paging_is_ordered_complete_and_gold_free(self):
        f, roles = self.seed()
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": "2"}
            if cursor:
                params["cursor"] = cursor
            res = self.get(roles["participant"], f.p, **params)
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            seen += [i["id"] for i in body["items"]]
            self.assertLessEqual(len(body["items"]), 2)
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(seen, f.expected_question_ids)
        whole = self.get(roles["participant"], f.p).json()
        self.assertEqual(whole["status"], "frozen")
        self.assertEqual(len(whole["items"]), 5)
        text = json.dumps(whole)
        for secret in ("SECRET-ANSWER", "SECRET-FACT", "evidence_sets", "facts"):
            self.assertNotIn(secret, text)
        self.assertEqual(whole["items"][0]["language"], "en")

    def test_roles_and_freeze_visibility(self):
        f, roles = self.seed(freeze=False)
        self.assertEqual(self.get(roles["participant"], f.p).status_code, 404)  # draft looks absent
        self.assertEqual(self.get(f.a, f.p).status_code, 200)  # owner sees the draft
        self.assertEqual(self.get(roles["reviewer"], f.p).status_code, 200)
        self.assertEqual(self.get(roles["observer"], f.p).status_code, 403)

    def test_outsider_and_unknown_revisions_are_404(self):
        f, roles = self.seed()
        other_project, other_actor = None, None
        with self.connect() as c:
            other_project, other_actor = self.make_project(c)
        self.assertEqual(self.get(other_actor, f.p).status_code, 404)  # not a member
        self.assertEqual(self.get(other_actor, other_project).status_code, 404)  # member, nothing there
        self.assertEqual(self.get(f.a, f.p, path="/api/v1/benchmarks/bench_1/revisions/2/questions").status_code, 404)
        self.assertEqual(self.get(f.a, f.p, path="/api/v1/benchmarks/nope/revisions/1/questions").status_code, 404)

    def test_bad_cursor_and_limit_are_400(self):
        f, roles = self.seed()
        self.assertEqual(self.get(f.a, f.p, cursor="garbage").status_code, 400)
        self.assertEqual(self.get(f.a, f.p, cursor="").status_code, 400)
        self.assertEqual(self.get(f.a, f.p, limit="0").status_code, 400)
        self.assertEqual(self.get(f.a, f.p, limit="9999").status_code, 400)

    def test_unauthenticated_is_401(self):
        f, _ = self.seed()
        res = self.client.get("/api/v1/benchmarks/bench_1/revisions/1/questions", params={"project_id": str(f.p)})
        self.assertEqual(res.status_code, 401)


if __name__ == "__main__":
    unittest.main()
