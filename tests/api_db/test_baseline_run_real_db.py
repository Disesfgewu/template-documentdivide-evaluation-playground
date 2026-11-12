"""Run the local lexical baseline on a frozen benchmark and read its scores (PM-DECISION item 3, Refs #10/#12).

The benchmark is built through the real API with the fake lab-gen stub (no model); the method is the real local lexical
baseline on the real uploaded PDF; the scores come from the real rule-based scorer and the real GET /runs/{id}/results."""
from __future__ import annotations

import unittest
import uuid

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, FreshDatabase, GenerateApiFixtures

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
FREEZE = "/api/v1/benchmarks/bench-gen/revisions/1/freeze"
BASELINE = "/api/v1/benchmarks/bench-gen/revisions/1/baseline-run"


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class BaselineRunRealDbTests(GenerateApiFixtures, FreshDatabase):
    def frozen(self):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker("validate_ok")
        res = self.api("post", owner, FREEZE, project, {"compliance": "partial"})
        self.assertEqual(res.status_code, 200, res.text)
        return project, owner

    def test_the_baseline_runs_on_a_frozen_benchmark_and_the_results_api_reports_per_metric_numbers(self):
        project, owner = self.frozen()
        res = self.api("post", owner, BASELINE, project)
        self.assertEqual(res.status_code, 201, res.text)
        run = res.json()
        self.assertEqual((run["status"], run["evaluation_status"]), ("completed", "provisional"))
        self.assertIn("correctness", run["pending_metrics"])
        self.assertTrue(any("semantic judge not run" in x for x in run["limits"]))
        n = run["questions"]
        self.assertGreaterEqual(n, 14)

        results = self.client.get(f"/api/v1/runs/{run['run_id']}/results?evaluation_id={run['evaluation_id']}",
                                  headers=self.headers(owner))
        self.assertEqual(results.status_code, 200, results.text)
        body = results.json()
        metrics = {m["metric_name"]: m for m in body["metrics"]} if "metrics" in body else body
        # every question was scored for the source metrics; the page-level baseline finds the (page-level) gold pages
        self.assertEqual(metrics["recall_at_k"]["scored"], n)
        self.assertEqual(metrics["complete_at_k"]["scored"], n)
        self.assertEqual(metrics["recall_at_k"]["mean"], 100.0)
        self.assertEqual(metrics["complete_at_k"]["mean"], 100.0)
        lp = metrics["localization_precision"]["mean"]
        self.assertTrue(0 < lp < 50, lp)                                          # coarse whole-page regions: low by construction
        self.assertEqual((metrics["textual_consistency"]["scored"], metrics["textual_consistency"]["not_applicable"]), (0, n))
        for name in ("correctness", "citation_precision", "citation_completeness", "faithfulness"):
            self.assertEqual((metrics[name]["scored"], metrics[name]["pending"]), (0, n), name)    # never guessed, never zero
            self.assertIsNone(metrics[name]["mean"], name)

        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status::text FROM runs WHERE id = %s", (run["run_id"],)).fetchone()[0], "completed")
            self.assertEqual(c.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND status = 'accepted'", (run["run_id"],)).fetchone()[0], n)
            self.assertGreater(c.execute("SELECT count(*) FROM retrieved_hits h JOIN submissions s ON s.id = h.submission_id "
                                         "WHERE s.run_id = %s", (run["run_id"],)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM question_scores WHERE run_id = %s", (run["run_id"],)).fetchone()[0], 8 * n)

    def test_only_a_frozen_benchmark_can_be_run_and_only_owner_or_maintainer_may_run_it(self):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")                                                  # validating, not frozen
        self.assertEqual(self.api("post", owner, BASELINE, project).status_code, 409)
        project, owner = self.frozen()
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            reviewer = self.add_member(c, project, "reviewer")
            participant = self.add_member(c, project, "participant")
        outsider = uuid.uuid4()
        with self.connect() as c:
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        for who, code in ((reviewer, 403), (participant, 403), (outsider, 404)):
            self.assertEqual(self.api("post", who, BASELINE, project).status_code, code, who)
        self.assertEqual(self.api("post", maintainer, BASELINE, project).status_code, 201)
        self.assertEqual(self.api("post", owner, "/api/v1/benchmarks/nope/revisions/1/baseline-run", project).status_code, 404)

    def test_a_too_large_benchmark_is_refused_and_revisions_do_not_collide(self):
        from unittest.mock import patch
        import lab_api.baseline_eval as be
        project, owner = self.frozen()
        with patch.object(be, "MAX_QUESTIONS", 5):
            res = self.api("post", owner, BASELINE, project)
        self.assertEqual(res.status_code, 409, res.text)                              # bounded, not a 500 or a timeout
        self.assertIn("synchronous_run", res.text)
        # two runs in a row take revision 1 and 2 of the method (the number is computed under a lock)
        r1 = self.api("post", owner, BASELINE, project).json()
        r2 = self.api("post", owner, BASELINE, project).json()
        self.assertNotEqual(r1["method_revision"], "")                                # both completed
        with self.connect() as c:
            revs = [r[0] for r in c.execute("SELECT rv.revision FROM method_revisions m JOIN resource_versions rv ON rv.id = m.resource_id "
                                            "WHERE m.project_id = %s ORDER BY rv.revision", (project,))]
        self.assertEqual(revs, [1, 2])

    def test_the_baseline_never_sees_gold_or_system_parse_output(self):
        project, owner = self.frozen()
        run = self.api("post", owner, BASELINE, project).json()
        with self.connect() as c:
            payload = c.execute("SELECT a.object_key FROM submissions s JOIN artifacts a ON a.id = s.payload_artifact_id "
                                "WHERE s.run_id = %s LIMIT 1", (run["run_id"],)).fetchone()
            hits = [r[0] for r in c.execute("SELECT h.content FROM retrieved_hits h JOIN submissions s ON s.id = h.submission_id "
                                            "WHERE s.run_id = %s", (run["run_id"],))]
        self.assertTrue(payload)
        for content in hits:                                       # hits are page text of the ORIGINAL PDF (pypdf), not stored evidence ids
            self.assertNotIn("g0000", content)
            self.assertNotIn("reference_answer", content)


if __name__ == "__main__":
    unittest.main()
