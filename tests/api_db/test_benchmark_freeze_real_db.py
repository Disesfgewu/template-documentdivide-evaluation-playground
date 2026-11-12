"""Freeze gate and POST .../freeze on real PostgreSQL (PM-DECISION item 2, Refs #10). lab-gen is the fake stub."""
from __future__ import annotations

import unittest
import uuid

from psycopg import errors

from lab_db.repo import FreezeIneligible, PermissionDenied, grant_app_privileges, transition_benchmark
from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, FreshDatabase, GenerateApiFixtures

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
FREEZE = "/api/v1/benchmarks/bench-gen/revisions/1/freeze"


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class BenchmarkFreezeRealDbTests(GenerateApiFixtures, FreshDatabase):
    def validated(self, validation_mode="validate_ok"):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker(validation_mode)
        return project, owner

    def status(self, project):
        with self.connect() as c:
            return c.execute("SELECT status::text, compliance FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()

    def test_the_owner_freezes_a_fully_validated_benchmark_as_partial_and_it_becomes_immutable(self):
        project, owner = self.validated()
        n = self.api("get", owner, self.GEN, project).json()["questions"]            # whatever the generator produced (general, cross_page)
        res = self.api("post", owner, FREEZE, project, {"compliance": "partial"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual((body["status"], body["compliance"], body["human_review"]), ("frozen", "partial", "skipped"))
        self.assertEqual((body["questions"], body["validation"]["pass"], body["validation"]["fail"]), (n, n, 0))
        self.assertGreaterEqual(n, 14)
        self.assertEqual((body["n_target"], body["q_min"]), (20, 2))
        self.assertEqual(body["shortfall"]["chart_table"], 2)                       # the missing buckets are stated, not hidden
        self.assertEqual(body["shortfall"]["confusable"], 2)
        self.assertEqual(body["shortfall"]["total"], 20 - n)
        self.assertTrue(any("PARTIAL" in x for x in body["limits"]) and any("not independent" in x for x in body["limits"]))
        self.assertEqual(self.status(project), ("frozen", "partial"))
        with self.connect() as c:
            self.assertTrue(c.execute("SELECT frozen FROM resource_versions rv JOIN benchmark_revisions b ON b.resource_id = rv.id "
                                      "WHERE b.project_id = %s", (project,)).fetchone()[0])
            with self.assertRaises(errors.IntegrityConstraintViolation):             # a frozen benchmark takes no new question
                c.execute("UPDATE questions SET question_text = 'x' WHERE benchmark_id = (SELECT id FROM benchmark_revisions WHERE project_id = %s)", (project,))
        again = self.api("post", owner, FREEZE, project, {"compliance": "partial"})
        self.assertEqual(again.status_code, 409)                                      # already frozen: not validating

    def test_a_failed_or_unvalidated_question_blocks_the_freeze_and_nothing_is_deleted(self):
        project, owner = self.validated("validate_mixed")
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        res = self.api("post", owner, FREEZE, project, {"compliance": "partial"})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("passing automated validation", res.json()["error"]["message"] if "error" in res.json() else res.text)
        self.assertEqual(self.status(project)[0], "validating")
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM questions q JOIN benchmark_revisions b ON b.id = q.benchmark_id "
                                       "WHERE b.project_id = %s", (project,)).fetchone()[0], n)
        project, owner = self.planned()                                               # generated but never validated
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.assertEqual(self.api("post", owner, FREEZE, project, {"compliance": "partial"}).status_code, 409)
        self.assertEqual(self.status(project)[0], "validating")

    def test_only_the_owner_may_freeze_and_the_body_must_acknowledge_partial(self):
        project, owner = self.validated()
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            reviewer = self.add_member(c, project, "reviewer")
            participant = self.add_member(c, project, "participant")
        outsider = uuid.uuid4()
        with self.connect() as c:
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        ok = {"compliance": "partial"}
        for who, code in ((maintainer, 403), (reviewer, 403), (participant, 403), (outsider, 404)):
            self.assertEqual(self.api("post", who, FREEZE, project, ok).status_code, code, who)
        for bad in ({}, {"compliance": "compliant"}, {"compliance": "partial", "x": 1}, {"compliance": True}):
            self.assertEqual(self.api("post", owner, FREEZE, project, bad).status_code, 400, bad)
        self.assertEqual(self.status(project)[0], "validating")                        # nothing happened
        self.assertEqual(self.api("post", owner, "/api/v1/benchmarks/nope/revisions/1/freeze", project, ok).status_code, 404)

    def test_the_database_gate_refuses_what_the_api_would_not_for_every_writer(self):
        project, owner = self.validated("validate_mixed")
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            bench, version = c.execute("SELECT id, row_version FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()
            version = transition_benchmark(c, project, bench, version, "ready_to_freeze", "test", owner)
            with self.assertRaises(FreezeIneligible) as ctx:                          # failed validations block even the owner
                transition_benchmark(c, project, bench, version, "frozen", "test", owner, compliance="partial")
            self.assertIn("lack a passing automated validation", str(ctx.exception))
            with self.assertRaises(PermissionDenied):                                  # python layer: owner only
                transition_benchmark(c, project, bench, version, "frozen", "test", maintainer, compliance="partial")
            with self.assertRaises(errors.InsufficientPrivilege):                      # raw SQL as a maintainer: the gate itself refuses
                with c.transaction():
                    c.execute("SELECT set_config('lab.actor_id', %s, true), set_config('lab.transition_reason', 'raw', true)", (str(maintainer),))
                    c.execute("UPDATE benchmark_revisions SET status = 'frozen', compliance = 'partial', frozen_at = now(), "
                              "row_version = %s WHERE id = %s", (version + 1, bench))
            with self.assertRaises(FreezeIneligible):                                  # 'compliant' cannot be proven
                transition_benchmark(c, project, bench, version, "frozen", "test", owner, compliance="compliant")
            self.assertEqual(c.execute("SELECT status::text FROM benchmark_revisions WHERE id = %s", (bench,)).fetchone()[0], "ready_to_freeze")

    def test_an_application_role_cannot_use_the_test_bypass(self):
        project, owner = self.validated("validate_mixed")
        role = self.app_role()
        with self.connect() as c:
            bench, version = c.execute("SELECT id, row_version FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()
            version = transition_benchmark(c, project, bench, version, "ready_to_freeze", "test", owner)
            with self.assertRaises(errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute(f'SET LOCAL ROLE "{role}"')
                    c.execute("SELECT set_config('lab.actor_id', %s, true), set_config('lab.transition_reason', 'x', true), "
                              "set_config('lab.test_allow_legacy_freeze', 'true', true)", (str(owner),))
                    c.execute("UPDATE benchmark_revisions SET status = 'frozen', compliance = 'partial', frozen_at = now(), "
                              "row_version = %s WHERE id = %s", (version + 1, bench))


if __name__ == "__main__":
    unittest.main()
