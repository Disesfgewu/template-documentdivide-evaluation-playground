"""The answering method as a job, on real PostgreSQL (PM-DECISION on Issue #7, item (a), Refs #12).

Benchmark built through the real API with the fake lab-gen stub, frozen, then the method run through the real API and the real
worker; the model's replies come from the stub (no model, no network)."""
from __future__ import annotations

import unittest
import uuid

# this import puts db/, services/ and contracts/ on sys.path (the shared fixtures bootstrap it): keep it FIRST
from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB
from lab_db.storage import LocalArtifactStore
from lab_api.generate_worker_main import GenerateWorker
from lab_api import answer_run

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
FREEZE = "/api/v1/benchmarks/bench-gen/revisions/1/freeze"
RUN = "/api/v1/benchmarks/bench-gen/revisions/1/method-runs"
FORBIDDEN = ("gold_answers", "facts", "evidence_units", "evidence_regions", "evidence_sets", "distractors", "question_validations",
             "parse_page_results", "parse_results", "benchmark_parse_sources", "evidence_unit_sources")


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class AnswerRunRealDbTests(GenerateApiFixtures, FreshDatabase):
    def frozen(self):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker("validate_ok")
        self.assertEqual(self.api("post", owner, FREEZE, project, {"compliance": "partial"}).status_code, 200)
        return project, owner

    def worker(self, mode):
        return GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="answer-test", interval=1, backoff=1,
                              grace=5, once_timeout=120, store=LocalArtifactStore(self.root), extra_env={"FAKE_LAB_GEN_MODE": mode})

    def run_method(self, project, owner, mode, cap=50_000_000):
        res = self.api("post", owner, RUN, project, {"model": "fake-model", "budget_cap_micros": cap})
        self.assertEqual(res.status_code, 202, res.text)
        counts = self.worker(mode).run_once()
        run = res.json()
        results = self.client.get(f"/api/v1/runs/{run['run_id']}/results", headers=self.headers(owner))
        self.assertEqual(results.status_code, 200, results.text)
        return run, counts, results.json()

    def metric(self, body, name):
        return {m["metric_name"]: m for m in body["metrics"]}[name]

    def test_a_good_run_answers_every_question_with_citations_and_scores_the_retrieval(self):
        project, owner = self.frozen()
        run, counts, body = self.run_method(project, owner, "answer_ok")
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        self.assertEqual(counts.get("succeeded:generated"), 1)
        self.assertEqual((body["submissions"]["answered"], body["submissions"]["abstained_by_model"], body["submissions"]["method_failures"]), (n, 0, {}))
        self.assertEqual((self.metric(body, "recall_at_k")["scored"], self.metric(body, "recall_at_k")["mean"]), (n, 100.0))
        for name in ("correctness", "citation_precision", "citation_completeness", "faithfulness"):
            self.assertEqual((self.metric(body, name)["scored"], self.metric(body, name)["pending"]), (0, n), name)   # judge not run
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status::text FROM runs WHERE id = %s", (run["run_id"],)).fetchone()[0], "completed")
            self.assertEqual(c.execute("SELECT count(*) FROM citations ci JOIN submissions s ON s.id = ci.submission_id WHERE s.run_id = %s",
                                       (run["run_id"],)).fetchone()[0], n)
            ledger = c.execute("SELECT count(*), sum(charged_micros) FROM (SELECT DISTINCT ON (job_id, attempt, call_id) * FROM llm_calls "
                               "WHERE job_id = %s ORDER BY job_id, attempt, call_id, seq DESC) s WHERE status = 'ok'", (run["job_id"],)).fetchone()
            self.assertEqual(ledger[0], n)                                            # one paid call per question, on the ledger
            self.assertGreater(ledger[1], 0)

    def test_model_abstentions_and_method_failures_are_counted_separately_and_failures_score_as_error(self):
        project, owner = self.frozen()
        run, counts, body = self.run_method(project, owner, "answer_mixed")
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        sub = body["submissions"]
        self.assertEqual(sub["answered"] + sub["abstained_by_model"] + sum(sub["method_failures"].values()), n)
        self.assertGreater(sub["answered"], 0)
        self.assertGreater(sub["abstained_by_model"], 0)
        self.assertEqual(set(sub["method_failures"]), {"invalid_model_reply"})        # bad JSON, a citation to an unretrieved hit, zero claims
        failed = sub["method_failures"]["invalid_model_reply"]
        corr = self.metric(body, "correctness")
        self.assertEqual((corr["error"], corr["pending"], corr["scored"]), (failed, n - failed, 0))   # error for failures, pending for the rest
        self.assertEqual(self.metric(body, "recall_at_k")["scored"], n)               # retrieval is still scored from the real hits
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status::text FROM runs WHERE id = %s", (run["run_id"],)).fetchone()[0], "completed_with_errors")
            reasons = c.execute("SELECT DISTINCT reason_codes FROM question_scores WHERE run_id = %s AND metric_name = 'correctness' "
                                "AND status = 'error'", (run["run_id"],)).fetchall()
            self.assertEqual([r[0] for r in reasons], [["invalid_model_reply"]])
            abst = c.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND answer_status = 'abstained' AND failure_reason IS NULL",
                             (run["run_id"],)).fetchone()[0]
            self.assertEqual(abst, sub["abstained_by_model"])

    def test_a_budget_that_fits_only_some_calls_records_the_rest_as_budget_exhausted(self):
        project, owner = self.frozen()
        run, counts, body = self.run_method(project, owner, "answer_ok", cap=30_000)
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        sub = body["submissions"]
        self.assertGreater(sub["answered"], 0)
        self.assertEqual(sub["answered"] + sub["method_failures"].get("budget_exhausted", 0), n)
        self.assertGreater(sub["method_failures"].get("budget_exhausted", 0), 0)
        self.assertEqual(self.metric(body, "recall_at_k")["scored"], n)

    def test_a_budget_too_small_for_any_call_still_records_every_question_and_finishes_the_run(self):
        project, owner = self.frozen()
        run, counts, body = self.run_method(project, owner, "answer_ok", cap=5)
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        self.assertEqual(body["submissions"], {"answered": 0, "abstained_by_model": 0, "no_answer_retrieval_only": 0, "method_failures": {"budget_exhausted": n}})
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status::text FROM runs WHERE id = %s", (run["run_id"],)).fetchone()[0], "completed_with_errors")
            self.assertEqual(c.execute("SELECT status::text FROM jobs WHERE id = %s", (run["job_id"],)).fetchone()[0], "failed")

    def test_only_a_frozen_benchmark_takes_a_method_run_only_owner_or_maintainer_and_one_run_at_a_time(self):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")                                                   # validating, not frozen
        self.assertEqual(self.api("post", owner, RUN, project, BODY).status_code, 409)
        project, owner = self.frozen()
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            reviewer = self.add_member(c, project, "reviewer")
            participant = self.add_member(c, project, "participant")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        for who, code in ((reviewer, 403), (participant, 403), (outsider, 404)):
            self.assertEqual(self.api("post", who, RUN, project, BODY).status_code, code, who)
        for bad in ({}, {"model": "m", "budget_cap_micros": 0}, {"model": "m", "budget_cap_micros": 10, "max_output_tokens": 501}):
            self.assertEqual(self.api("post", owner, RUN, project, bad).status_code, 400, bad)
        self.assertEqual(self.api("post", maintainer, RUN, project, BODY).status_code, 202)
        self.assertEqual(self.api("post", owner, RUN, project, BODY).status_code, 409)             # one active method run per benchmark
        self.assertEqual(self.api("post", owner, "/api/v1/benchmarks/nope/revisions/1/method-runs", project, BODY).status_code, 404)

    def test_a_unique_violation_on_the_method_revision_is_a_409_not_a_500(self):
        import psycopg.errors
        from unittest import mock
        project, owner = self.frozen()
        with mock.patch("lab_api.benchmark_method.start_answer_run", side_effect=psycopg.errors.UniqueViolation("dup")):
            res = self.api("post", owner, RUN, project, BODY)
        self.assertEqual(res.status_code, 409, res.text)

    def test_a_retrieval_that_does_not_reproduce_the_sent_prompt_is_not_scored_as_an_answer(self):
        from unittest import mock
        project, owner = self.frozen()
        real, calls = answer_run.local_methods.query, {"n": 0}

        def drifting(index, request):                       # every call returns different hit text: the re-run never matches
            calls["n"] += 1
            out = real(index, request)
            hits = [h.model_copy(update={"content": f"{h.content} drift{calls['n']}"}) for h in out.retrieved_hits]
            return out.model_copy(update={"retrieved_hits": hits})
        with mock.patch.object(answer_run.local_methods, "query", drifting):
            run, counts, body = self.run_method(project, owner, "answer_ok")
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        self.assertEqual(body["submissions"], {"answered": 0, "abstained_by_model": 0, "no_answer_retrieval_only": 0, "method_failures": {"outcome_unknown": n}})

    def test_the_participant_phase_never_touches_gold_or_system_parse_tables(self):
        project, owner = self.frozen()
        started = self.api("post", owner, RUN, project, BODY).json()

        class Recording:
            def __init__(self, conn):
                self._c, self.sql = conn, []

            def execute(self, query, *args, **kw):
                self.sql.append(str(query))
                return self._c.execute(query, *args, **kw)

            def __getattr__(self, name):
                return getattr(self._c, name)
        build, results = answer_run.make_answer_hooks(LocalArtifactStore(self.root), uuid.UUID(started["run_id"]))
        with self.connect() as c, c.transaction():
            with self.connect() as c2:
                bench = c2.execute("SELECT id FROM benchmark_revisions WHERE project_id = %s", (project,)).fetchone()[0]
            rec = Recording(c)
            calls = build(rec, project, bench)
            self.assertTrue(calls)
        joined = "\n".join(rec.sql).lower()
        import re
        for table in FORBIDDEN:
            self.assertIsNone(re.search(r"\b" + table + r"\b", joined), table)          # the participant phase reads none of them
        self.assertIn("set_config('lab.actor_id', '', true)", rec.sql[0])               # and it starts by UNSETTING the actor
        for call in calls:                                                                # the prompts carry the question and the pages only
            self.assertNotIn("reference_answer", call.user + call.system)
            self.assertNotIn(str(project), call.user)

    def test_with_the_actor_unset_the_gold_row_level_security_denies_gold_for_an_application_role(self):
        project, owner = self.frozen()
        role = self.app_role()
        with self.connect() as c:
            c.execute(f'SET ROLE "{role}"')
            with c.transaction():
                c.execute("SELECT set_config('lab.actor_id', '', true)")
                self.assertEqual(c.execute("SELECT count(*) FROM gold_answers").fetchone()[0], 0)      # defence in depth
                self.assertEqual(c.execute("SELECT count(*) FROM facts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
