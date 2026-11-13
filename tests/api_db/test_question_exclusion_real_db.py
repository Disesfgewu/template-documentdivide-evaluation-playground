"""The owner excludes questions before the freeze (migration 0019), against real PostgreSQL (Refs #10, #12, Issue #7)."""
from __future__ import annotations

import unittest
import uuid

from psycopg import errors

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, FreshDatabase, GenerateApiFixtures

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
BASE = "/api/v1/benchmarks/bench-gen/revisions/1"
FREEZE = f"{BASE}/freeze"
REVIEW = f"{BASE}/questions/review"


def exclusion(qid):
    return f"{BASE}/questions/{qid}/exclusion"


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class QuestionExclusionRealDbTests(GenerateApiFixtures, FreshDatabase):
    def validated(self, mode="validate_mixed"):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker(mode)
        return project, owner

    def review(self, owner, project):
        res = self.api("get", owner, REVIEW, project)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def exclude(self, owner, project, qid, excluded=True, reason="the validator could not support it"):
        return self.api("post", owner, exclusion(qid), project, {"excluded": excluded, "reason": reason})

    def exclude_all_not_passing(self, owner, project):
        rv = self.review(owner, project)
        bad = [i for i in rv["items"] if i["verdict"] != "pass"]
        for i in bad:
            self.assertEqual(self.exclude(owner, project, i["id"]).status_code, 200)
        return rv, bad

    def test_a_mixed_benchmark_cannot_freeze_until_the_owner_excludes_the_failures_then_it_freezes_without_them(self):
        project, owner = self.validated()
        rv = self.review(owner, project)
        passing = [i for i in rv["items"] if i["verdict"] == "pass"]
        failing = [i for i in rv["items"] if i["verdict"] != "pass"]
        self.assertTrue(passing and failing)
        self.assertFalse(rv["can_freeze"])
        self.assertEqual(self.api("post", owner, FREEZE, project, {"compliance": "partial"}).status_code, 409)
        _, bad = self.exclude_all_not_passing(owner, project)
        after = self.review(owner, project)
        self.assertEqual((after["total"], after["active"], after["excluded"]), (len(rv["items"]), len(passing), len(failing)))
        self.assertTrue(after["can_freeze"])
        self.assertTrue(all(i["exclusion_reason"] == "the validator could not support it" for i in after["items"] if i["excluded"]))
        progress = self.api("get", owner, self.GEN, project).json()
        self.assertEqual((progress["questions"], progress["excluded"]), (len(passing), len(failing)))
        self.assertEqual((progress["validation"]["fail"], progress["validation"]["unvalidated"]), (0, 0))   # over the non-excluded questions
        res = self.api("post", owner, FREEZE, project, {"compliance": "partial"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["questions"], len(passing))
        self.assertEqual([e["id"] for e in body["excluded_questions"]], [i["id"] for i in failing])
        with self.connect() as c:                                                   # recorded, never deleted
            bench = c.execute("SELECT id FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()[0]
            self.assertEqual(c.execute("SELECT count(*) FROM questions WHERE benchmark_id = %s", (bench,)).fetchone()[0], len(rv["items"]))
            self.assertEqual(c.execute("SELECT count(*) FROM active_questions WHERE benchmark_id = %s", (bench,)).fetchone()[0], len(passing))
            self.assertEqual(c.execute("SELECT count(*) FROM question_exclusions WHERE benchmark_id = %s", (bench,)).fetchone()[0], len(failing))
            self.assertEqual(c.execute("SELECT count(*) FROM audit_events WHERE project_id = %s AND action = 'question.excluded'", (project,)).fetchone()[0], len(failing))

    def test_a_frozen_benchmark_scores_only_the_questions_that_were_not_excluded_and_results_list_the_exclusions(self):
        project, owner = self.validated()
        rv, bad = self.exclude_all_not_passing(owner, project)
        self.assertEqual(self.api("post", owner, FREEZE, project, {"compliance": "partial"}).status_code, 200)
        run = self.api("post", owner, f"{BASE}/baseline-run", project, {})
        self.assertEqual(run.status_code, 201, run.text)
        active = rv["total"] - len(bad)
        self.assertEqual(run.json()["questions"], active)
        res = self.client.get(f"/api/v1/runs/{run.json()['run_id']}/results", headers=self.headers(owner))
        body = res.json()
        self.assertEqual(max(m["scored"] for m in body["metrics"]), active)
        self.assertEqual(len(body["excluded_questions"]), len(bad))
        self.assertTrue(all(e["reason"] for e in body["excluded_questions"]))
        listed = self.api("get", owner, f"{BASE}/questions", project).json()["items"]
        self.assertEqual(len(listed), active)                                 # the public list shows what is frozen

    def test_excluding_everything_leaves_nothing_to_freeze_and_the_database_gate_agrees(self):
        project, owner = self.validated("validate_ok")
        for i in self.review(owner, project)["items"]:
            self.assertEqual(self.exclude(owner, project, i["id"]).status_code, 200)
        res = self.api("post", owner, FREEZE, project, {"compliance": "partial"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("no questions", res.text)
        with self.connect() as c:
            bench, version = c.execute("SELECT id, row_version FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()
            with self.assertRaises(errors.CheckViolation):
                with c.transaction():
                    c.execute("SELECT set_config('lab.actor_id', %s, true)", (str(owner),))
                    c.execute("UPDATE benchmark_revisions SET status = 'ready_to_freeze', row_version = %s WHERE id = %s", (version + 1, bench))
                    c.execute("UPDATE benchmark_revisions SET status = 'frozen', compliance = 'partial', frozen_at = now(), "
                              "row_version = %s WHERE id = %s", (version + 2, bench))

    def test_the_database_gate_still_needs_every_non_excluded_question_to_pass(self):
        project, owner = self.validated()
        rv = self.review(owner, project)
        failing = [i for i in rv["items"] if i["verdict"] != "pass"]
        self.assertEqual(self.exclude(owner, project, failing[0]["id"]).status_code, 200)      # one of several failures
        res = self.api("post", owner, FREEZE, project, {"compliance": "partial"})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("non-excluded", res.text)

    def test_restoring_brings_the_question_back_and_decisions_are_validated(self):
        project, owner = self.validated("validate_ok")
        qid = self.review(owner, project)["items"][0]["id"]
        self.assertEqual(self.exclude(owner, project, qid, excluded=False).status_code, 409)       # already included
        self.assertEqual(self.exclude(owner, project, qid).status_code, 200)
        self.assertEqual(self.exclude(owner, project, qid).status_code, 409)                       # already excluded
        self.assertEqual(self.exclude(owner, project, qid, excluded=False, reason="mistake").status_code, 200)
        rv = self.review(owner, project)
        self.assertEqual(rv["excluded"], 0)
        for bad in ({}, {"excluded": True}, {"excluded": True, "reason": "  "}, {"excluded": "yes", "reason": "x"},
                    {"excluded": True, "reason": "x" * 501}, {"excluded": True, "reason": "x", "extra": 1}):
            self.assertEqual(self.api("post", owner, exclusion(qid), project, bad).status_code, 400, bad)
        self.assertEqual(self.exclude(owner, project, str(uuid.uuid4())).status_code, 404)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM question_exclusions WHERE question_id = %s", (qid,)).fetchone()[0], 2)

    def test_only_the_owner_excludes_and_only_owner_or_maintainer_reads_the_review(self):
        project, owner = self.validated("validate_ok")
        qid = self.review(owner, project)["items"][0]["id"]
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            participant = self.add_member(c, project, "participant")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        self.assertEqual(self.api("get", maintainer, REVIEW, project).status_code, 200)
        self.assertEqual(self.api("get", participant, REVIEW, project).status_code, 403)
        self.assertEqual(self.api("get", outsider, REVIEW, project).status_code, 404)
        for who, code in ((maintainer, 403), (participant, 403), (outsider, 404)):
            self.assertEqual(self.exclude(who, project, qid).status_code, code, who)
        self.assertEqual(self.review(owner, project)["excluded"], 0)

    def test_after_the_freeze_nothing_can_be_excluded_or_restored_not_even_by_the_database(self):
        project, owner = self.validated("validate_ok")
        qid = self.review(owner, project)["items"][0]["id"]
        self.assertEqual(self.api("post", owner, FREEZE, project, {"compliance": "partial"}).status_code, 200)
        self.assertEqual(self.exclude(owner, project, qid).status_code, 409)
        with self.connect() as c:
            bench = c.execute("SELECT id FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()[0]
            with self.assertRaises(errors.IntegrityConstraintViolation):
                c.execute("INSERT INTO question_exclusions (id, benchmark_id, question_id, kind, reason, actor_id) "
                          "VALUES (%s, %s, %s, 'exclude', 'late', %s)", (uuid.uuid4(), bench, qid, owner))

    def test_the_log_is_append_only_and_a_non_owner_cannot_write_it_directly(self):
        project, owner = self.validated("validate_ok")
        qid = self.review(owner, project)["items"][0]["id"]
        self.assertEqual(self.exclude(owner, project, qid).status_code, 200)
        with self.connect() as c:
            bench = c.execute("SELECT id FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()[0]
            maintainer = self.add_member(c, project, "maintainer")
            with self.assertRaises(errors.InsufficientPrivilege):
                c.execute("INSERT INTO question_exclusions (id, benchmark_id, question_id, kind, reason, actor_id) "
                          "VALUES (%s, %s, %s, 'restore', 'sneaky', %s)", (uuid.uuid4(), bench, qid, maintainer))
        with self.connect() as c:
            with self.assertRaises(errors.IntegrityConstraintViolation):
                c.execute("UPDATE question_exclusions SET reason = 'edited'")
        with self.connect() as c:
            with self.assertRaises(errors.IntegrityConstraintViolation):
                c.execute("DELETE FROM question_exclusions")

    def test_an_application_role_reads_the_view_appends_exclusions_and_cannot_rewrite_them(self):
        project, owner = self.validated("validate_ok")
        qid = self.review(owner, project)["items"][0]["id"]
        role = self.app_role()
        with self.connect() as c:
            bench = c.execute("SELECT id FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()[0]
            with c.transaction():
                c.execute(f'SET LOCAL ROLE "{role}"')
                self.assertGreater(c.execute("SELECT count(*) FROM active_questions WHERE benchmark_id = %s", (bench,)).fetchone()[0], 0)
                c.execute("INSERT INTO question_exclusions (id, benchmark_id, question_id, kind, reason, actor_id) "
                          "VALUES (%s, %s, %s, 'exclude', 'app role', %s)", (uuid.uuid4(), bench, qid, owner))
            with self.assertRaises(errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute(f'SET LOCAL ROLE "{role}"')
                    c.execute("UPDATE question_exclusions SET reason = 'edited'")
            with self.assertRaises(errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute(f'SET LOCAL ROLE "{role}"')
                    c.execute("DELETE FROM question_exclusions")


if __name__ == "__main__":
    unittest.main()
