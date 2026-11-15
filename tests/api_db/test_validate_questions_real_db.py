"""Automated validation of generated questions on real PostgreSQL (P-D slice 4, Refs #7). lab-gen is the fake stub."""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

from psycopg import errors

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api import generate_questions as gq, validate_questions as vq
from lab_api.generate_questions import confusable_target, cross_page_target, general_target
from lab_db.generation import GenerationError
from lab_db.repo import PermissionDenied
from tests.api_db.test_generate_job_real_db import ADMIN_URL, FreshDatabase, GenerateFixtures

BIG = 50_000_000


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class ValidateQuestionsRealDbTests(GenerateFixtures, FreshDatabase):
    def generated(self, mode="questions", role=None):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench, cap=BIG)
        out = self.process(project, job, mode, build_calls=gq.make_build_calls(), on_results=gq.make_on_results(), role=role)
        self.assertEqual((out.status, self.bench_status(bench)), ("succeeded", "validating"))
        return project, owner, bench

    def validate(self, project, owner, bench, mode, **kw):
        from lab_db.generation import enqueue_generate_job
        with self.connect() as c:
            job = enqueue_generate_job(c, project_id=project, actor_id=owner, benchmark_id=bench, model="fake-model",
                                       max_output_tokens=500, budget_cap_micros=kw.pop("cap", BIG), currency="USD",
                                       pricing_revision="t1", phase="validate")
        out = self.process(project, job, mode, build_calls=vq.make_build_calls(), on_results=vq.make_on_results(),
                           role=kw.pop("role", None))
        return job, out

    def verdicts(self, bench):
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT v.verdict FROM question_validations v JOIN questions q ON q.id = v.question_id "
                                            "WHERE v.benchmark_id = %s ORDER BY q.ordinal", (bench,))]

    def test_grounded_questions_with_a_positive_validator_pass(self):
        project, owner, bench = self.generated()
        job, out = self.validate(project, owner, bench, "validate_ok")
        self.assertEqual((out.status, out.reason), ("succeeded", "generated"))
        n = general_target(3) + cross_page_target(3) + confusable_target(3)
        self.assertEqual(self.verdicts(bench), ["pass"] * n)
        self.assertEqual(self.bench_status(bench), "validating")                      # never ready_to_freeze/frozen here
        with self.connect() as c:
            rev, llm = c.execute("SELECT validator_revision, llm_checks FROM question_validations WHERE benchmark_id = %s LIMIT 1",
                                 (bench,)).fetchone()
            reason = c.execute("SELECT reason FROM audit_events WHERE project_id = %s AND action = 'benchmark.questions_validated'",
                               (project,)).fetchone()[0]
        self.assertIn("same-model", rev)
        self.assertIn("not independent", llm["independence"])
        self.assertIn(f"pass={n}", reason)
        self.assertIn("human_review=skipped", reason)
        ledger = self.calls(job)
        self.assertEqual(len(ledger), n)                                              # one paid call per question, on the ledger
        self.assertTrue(all(r[1] == "ok" for r in ledger))

    def test_the_whole_flow_works_for_a_non_superuser_worker_role_under_row_level_security(self):
        role = self.app_role()
        project, owner, bench = self.generated(role=role)          # generation, writing gold, as the application role
        n = general_target(3) + cross_page_target(3) + confusable_target(3)
        job, out = self.validate(project, owner, bench, "validate_ok", role=role)
        self.assertEqual((out.status, out.reason), ("succeeded", "generated"))     # build_calls could read gold_answers
        self.assertEqual(self.verdicts(bench), ["pass"] * n)

    def test_a_needs_review_question_can_be_validated_again_but_a_decided_one_cannot(self):
        project, owner, bench = self.generated()
        self.validate(project, owner, bench, "validate_garbage")
        self.assertEqual(set(self.verdicts(bench)), {"needs_review"})
        job2, out2 = self.validate(project, owner, bench, "validate_ok")             # a new job may decide them
        self.assertEqual((out2.status, out2.reason), ("succeeded", "generated"))
        with self.connect() as c:
            latest = [r[0] for r in c.execute("SELECT DISTINCT ON (question_id) verdict FROM question_validations "
                                              "WHERE benchmark_id = %s ORDER BY question_id, created_at DESC", (bench,))]
        self.assertEqual(set(latest), {"pass"})
        job3, out3 = self.validate(project, owner, bench, "validate_ok")             # all decided: nothing left
        self.assertEqual((out3.status, out3.reason), ("failed", "no_calls"))

    def test_a_question_whose_source_unit_cannot_be_loaded_is_left_unvalidated_not_judged_on_less_evidence(self):
        project, owner, bench = self.generated()
        n = general_target(3) + cross_page_target(3) + confusable_target(3)
        qid = uuid.uuid4()
        with self.connect() as c:
            c.execute("SELECT set_config('lab.actor_id', %s, false)", (str(owner),))
            c.execute("INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, payload) "
                      "VALUES (%s, %s, 'q-ghost', 999, 'What is the ghost?', %s, 'general', '{}')", (qid, bench, "a" * 64))
            for kind, payload in (("semantic", {"reference_answer": "a ghost", "required_facts": [{"statement": "a ghost"}]}),
                                  ("source", {"evidence_sets": [{"set_id": "s", "evidence_ids": ["ghost-unit"]}]})):
                c.execute("INSERT INTO gold_answers (id, benchmark_id, question_id, kind, payload) VALUES (%s, %s, %s, %s, %s)",
                          (uuid.uuid4(), bench, qid, kind, __import__("psycopg").types.json.Jsonb(payload)))
            c.commit()
        job, out = self.validate(project, owner, bench, "validate_ok")
        self.assertEqual((out.status, out.reason), ("succeeded", "generated"))
        self.assertEqual(self.verdicts(bench), ["pass"] * n)                         # the ghost question has no verdict at all
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM question_validations WHERE question_id = %s", (qid,)).fetchone()[0], 0)

    def test_negative_validator_flags_and_ungrounded_questions_fail_and_garbage_needs_review(self):
        project, owner, bench = self.generated()
        self.validate(project, owner, bench, "validate_mixed")
        v = self.verdicts(bench)
        self.assertEqual(sorted(set(v)), ["fail", "pass"])                           # i % 3 == 0 passes, 1 and 2 fail
        self.assertEqual(v[:3], ["pass", "fail", "fail"])
        project, owner, bench = self.generated()
        self.validate(project, owner, bench, "validate_garbage")
        self.assertEqual(set(self.verdicts(bench)), {"needs_review"})
        project, owner, bench = self.generated("questions_ungrounded")
        self.validate(project, owner, bench, "validate_ok")                          # the validator says yes, the code says no
        self.assertEqual(set(self.verdicts(bench)), {"fail"})

    def test_questions_are_never_deleted_and_decided_ones_are_not_validated_again(self):
        project, owner, bench = self.generated()
        self.validate(project, owner, bench, "validate_mixed")
        n = general_target(3) + cross_page_target(3) + confusable_target(3)
        self.assertEqual(self.count_q(bench), n)
        job2, out2 = self.validate(project, owner, bench, "validate_ok")             # nothing left to validate
        self.assertEqual((out2.status, out2.reason), ("failed", "no_calls"))
        self.assertEqual(len(self.verdicts(bench)), n)

    def test_only_a_validating_benchmark_takes_a_validation_job_and_roles_are_checked(self):
        project, owner, bench = self.planning_benchmark()
        from lab_db.generation import enqueue_generate_job
        with self.connect() as c, self.assertRaises(GenerationError):                 # still planning
            enqueue_generate_job(c, project_id=project, actor_id=owner, benchmark_id=bench, model="m", max_output_tokens=500,
                                 budget_cap_micros=10, currency="USD", pricing_revision="t1", phase="validate")
        project, owner, bench = self.generated()
        with self.connect() as c:
            participant = self.add_member(c, project, "participant")
        with self.connect() as c, self.assertRaises(PermissionDenied):
            enqueue_generate_job(c, project_id=project, actor_id=participant, benchmark_id=bench, model="m", max_output_tokens=500,
                                 budget_cap_micros=10, currency="USD", pricing_revision="t1", phase="validate")
        with self.connect() as c, self.assertRaises(GenerationError):                 # and a generation job cannot be started now
            enqueue_generate_job(c, project_id=project, actor_id=owner, benchmark_id=bench, model="m", max_output_tokens=500,
                                 budget_cap_micros=10, currency="USD", pricing_revision="t1")

    def test_validations_are_append_only_and_hidden_from_participants(self):
        project, owner, bench = self.generated()
        self.validate(project, owner, bench, "validate_ok")
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):
            c.execute("UPDATE question_validations SET verdict = 'pass' WHERE benchmark_id = %s", (bench,))
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):
            c.execute("DELETE FROM question_validations WHERE benchmark_id = %s", (bench,))
        from lab_db.repo import grant_app_privileges
        with self.connect() as c:
            participant = self.add_member(c, project, "participant")
            reviewer = self.add_member(c, project, "reviewer")
            role = f"lab_app_{uuid.uuid4().hex[:10]}"
            c.execute(f'CREATE ROLE "{role}" NOLOGIN')
            grant_app_privileges(c, role)
            c.commit()
        self.addCleanup(self.drop_role, role)

        def visible(actor):
            with self.connect() as c, c.transaction():
                c.execute(f'SET LOCAL ROLE "{role}"')
                c.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))
                return c.execute("SELECT count(*) FROM question_validations WHERE benchmark_id = %s", (bench,)).fetchone()[0]
        n = general_target(3) + cross_page_target(3) + confusable_target(3)
        self.assertEqual((visible(owner), visible(reviewer), visible(participant)), (n, n, 0))

    def count_q(self, bench):
        with self.connect() as c:
            return c.execute("SELECT count(*) FROM questions WHERE benchmark_id = %s", (bench,)).fetchone()[0]

    def drop_role(self, role):
        with self.connect(autocommit=True) as c:
            c.execute(f'DROP OWNED BY "{role}"')
            c.execute(f'DROP ROLE IF EXISTS "{role}"')


if __name__ == "__main__":
    unittest.main()
