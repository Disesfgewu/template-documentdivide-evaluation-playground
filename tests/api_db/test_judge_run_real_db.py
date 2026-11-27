"""The semantic judge job on a finished answering run, against real PostgreSQL with a fake lab-gen (Refs #12, #7)."""
from __future__ import annotations

import unittest
import uuid

# this import puts db/, services/ and contracts/ on sys.path (the shared fixtures bootstrap it): keep it FIRST
from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB
from lab_db.storage import LocalArtifactStore
from lab_api.generate_worker_main import GenerateWorker
from lab_api import method_judge

BODY = {"model": "fake-model", "budget_cap_micros": 50_000_000}
FREEZE = "/api/v1/benchmarks/bench-gen/revisions/1/freeze"
RUN = "/api/v1/benchmarks/bench-gen/revisions/1/method-runs"
JUDGED = ("correctness", "faithfulness", "citation_precision", "citation_completeness")


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class JudgeRunRealDbTests(GenerateApiFixtures, FreshDatabase):
    def frozen(self):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker("validate_ok")
        self.assertEqual(self.api("post", owner, FREEZE, project, {"compliance": "partial"}).status_code, 200)
        return project, owner

    def worker(self, mode):
        return GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="judge-test", interval=1, backoff=1,
                              grace=5, once_timeout=120, store=LocalArtifactStore(self.root), extra_env={"FAKE_LAB_GEN_MODE": mode})

    def answered(self, answer_mode="answer_ok"):
        project, owner = self.frozen()
        res = self.api("post", owner, RUN, project, BODY)
        self.assertEqual(res.status_code, 202, res.text)
        self.worker(answer_mode).run_once()
        return project, owner, res.json()

    def judge(self, project, owner, run, mode, cap=50_000_000):
        res = self.api("post", owner, f"/api/v1/runs/{run['run_id']}/judge-runs", project, {"model": "fake-model", "budget_cap_micros": cap})
        self.assertEqual(res.status_code, 202, res.text)
        self.worker(mode).run_once()
        out = self.client.get(f"/api/v1/runs/{run['run_id']}/results", headers=self.headers(owner))
        self.assertEqual(out.status_code, 200, out.text)
        return res.json(), out.json()

    def metric(self, body, name):
        return {m["metric_name"]: m for m in body["metrics"]}[name]

    def test_a_good_judge_scores_the_semantic_metrics_in_a_new_evaluation_and_records_the_limitation(self):
        project, owner, run = self.answered()
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        with self.connect() as c:
            old = c.execute("SELECT id FROM evaluations WHERE run_id = %s", (run["run_id"],)).fetchone()[0]
        started, body = self.judge(project, owner, run, "judge_ok")
        self.assertEqual(started["judge_prompt_sha256"], method_judge.JUDGE_PROMPT_SHA256)
        job_view = self.client.get(f"/api/v1/jobs/{started['job_id']}", headers=self.headers(owner)).json()
        self.assertEqual((job_view["status"], job_view["stage"]), ("succeeded", "done"))           # a succeeded judge job is not 'queued' (PM, #215)
        for name in JUDGED:
            m = self.metric(body, name)
            self.assertEqual((m["scored"], m["pending"], m["error"]), (n, 0, 0), name)
            if name == "correctness":
                self.check_correctness(m, body, n)
            else:
                self.assertEqual(m["mean"], 100.0, name)
        self.assertEqual(self.metric(body, "recall_at_k")["scored"], n)               # the rule-based rows are carried over
        self.assertNotEqual(body["evaluation"]["id"], str(old))
        self.assertTrue(body["provisional"])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM evaluations WHERE run_id = %s", (run["run_id"],)).fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT count(*) FROM question_scores WHERE evaluation_id = %s AND metric_name = ANY(%s)",
                                       (old, list(JUDGED))).fetchone()[0], n * 4)                 # the old rows are untouched (pending)
            self.assertEqual(c.execute("SELECT count(*) FROM question_scores WHERE evaluation_id = %s AND status = 'pending'", (old,)).fetchone()[0], n * 4)
            rows = c.execute("SELECT DISTINCT judge_model, judge_prompt_revision, judge_prompt_sha256, judge_limitation FROM question_scores "
                             "WHERE evaluation_id = %s AND metric_name = ANY(%s)", (body["evaluation"]["id"], list(JUDGED))).fetchall()
            self.assertEqual(rows, [("fake-model", method_judge.JUDGE_PROMPT_REVISION, method_judge.JUDGE_PROMPT_SHA256, method_judge.JUDGE_LIMITATION)])
            self.assertIn("same provider/model family", rows[0][3])
            self.assertEqual(c.execute("SELECT count(*) FROM question_scores WHERE evaluation_id = %s AND metric_name = 'recall_at_k' "
                                       "AND judge_model IS NOT NULL", (body["evaluation"]["id"],)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT status::text FROM runs WHERE id = %s", (run["run_id"],)).fetchone()[0], "completed")

    def test_contradicted_facts_and_unsupported_claims_lower_the_scores_derived_in_code(self):
        project, owner, run = self.answered()
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        _, body = self.judge(project, owner, run, "judge_mixed")
        corr, faith = self.metric(body, "correctness"), self.metric(body, "faithfulness")
        self.assertEqual((corr["scored"], faith["scored"]), (n, n))
        self.assertLess(corr["mean"], 100.0)
        self.assertGreater(corr["mean"], 0.0)
        self.assertLess(faith["mean"], 100.0)

    def _all_errors(self, mode, code, why=None):
        project, owner, run = self.answered()
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        _, body = self.judge(project, owner, run, mode)
        for name in JUDGED:
            m = self.metric(body, name)
            self.assertEqual((m["scored"], m["error"]), (0, n), (mode, name))
        with self.connect() as c:
            reasons = c.execute("SELECT DISTINCT reason_codes FROM question_scores WHERE evaluation_id = %s AND metric_name = 'correctness'",
                                (body["evaluation"]["id"],)).fetchall()
            self.assertEqual([r[0] for r in reasons], [[code] + ([why] if why else [])])

    def test_an_unparseable_verdict_is_an_error_never_a_guessed_number(self):
        self._all_errors("judge_bad_json", "invalid_judge_reply", "not_json")                # the generic code plus WHY, no model text

    def test_prose_and_a_fence_around_the_verdict_are_tolerated(self):
        project, owner, run = self.answered()
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        _, body = self.judge(project, owner, run, "judge_prose")
        for name in JUDGED:
            m = self.metric(body, name)
            self.assertEqual((m["scored"], m["error"]), (n, 0), name)
            if name == "correctness":
                self.check_correctness(m, body, n)
            else:
                self.assertEqual(m["mean"], 100.0, name)

    # --- a re-judge of the error rows only (Issue #215 item 3) -------------------------------------------------------------------------------------

    def redo(self, project, owner, run, mode, **extra):
        res = self.api("post", owner, f"/api/v1/runs/{run['run_id']}/judge-runs", project, {"model": "fake-model", "budget_cap_micros": 50_000_000, **extra})
        if res.status_code == 202:
            self.worker(mode).run_once()
        return res

    def scores(self, run, evaluation=None):
        with self.connect() as c:
            ev = evaluation or c.execute("SELECT id FROM evaluations WHERE run_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (run["run_id"],)).fetchone()[0]
            return ev, {(q, m): (st, v, rc, jm) for q, m, st, v, rc, jm in c.execute(
                "SELECT question_id, metric_name, status, raw_value, reason_codes, judge_model FROM question_scores WHERE evaluation_id = %s", (ev,))}

    def test_only_the_error_rows_are_judged_again_with_the_same_model_and_everything_else_is_carried_over(self):
        project, owner, run = self.answered()
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        self.judge(project, owner, run, "judge_bad_json")                          # every judged row is an error (an unusable reply)
        first, before = self.scores(run)
        errors = {q for (q, m), (st, *_) in before.items() if m == "correctness" and st == "error"}
        self.assertEqual(len(errors), n)
        # a run with nothing left to redo, a different model, an unknown flag combination: all refused with a reason
        res = self.redo(project, owner, run, "judge_ok", judge_errors_only=True, model="another-model")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("model_differs", res.json()["error"]["message"])
        res = self.redo(project, owner, run, "judge_ok", judge_errors_only=True)
        self.assertEqual(res.status_code, 202, res.text)
        job = res.json()
        self.assertEqual((job["scope"], job["questions_to_judge_again"]), ("errors", n))
        second, after = self.scores(run)
        self.assertNotEqual(first, second)                                          # a NEW evaluation; the old one is untouched
        self.assertEqual(self.scores(run, first)[1], before)
        self.assertTrue(all(st == "scored" for (q, m), (st, *_) in after.items() if m == "correctness"))
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT stage FROM jobs WHERE id = %s", (job["job_id"],)).fetchone()[0], "done")
            self.assertEqual(c.execute("SELECT count(*) FROM llm_calls WHERE job_id = %s", (job["job_id"],)).fetchone()[0], n)       # in the ledger: one call per redone question
            self.assertEqual(c.execute("SELECT grader_revision FROM evaluations WHERE id = %s", (second,)).fetchone()[0],
                             c.execute("SELECT grader_revision FROM evaluations WHERE id = %s", (first,)).fetchone()[0])
        # nothing is left to redo now
        again = self.redo(project, owner, run, "judge_ok", judge_errors_only=True)
        self.assertEqual(again.status_code, 409, again.text)
        self.assertIn("no_judge_errors_to_redo", again.json()["error"]["message"])

    def test_the_rows_that_were_judged_fine_are_copied_unchanged_and_only_the_failed_ones_cost_calls(self):
        project, owner, run = self.answered()
        self.judge(project, owner, run, "judge_prose")                              # all judged fine
        ok_eval, ok_rows = self.scores(run)
        # make two questions' judged rows an error the judge side caused, in a new evaluation, the way a bad reply would have left them
        with self.connect() as c:
            ev = uuid.uuid4()
            c.execute("INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) SELECT %s, run_id, benchmark_id, grader_revision, status "
                      "FROM evaluations WHERE id = %s", (ev, ok_eval))
            c.execute("INSERT INTO question_scores (evaluation_id, run_id, benchmark_id, question_id, submission_id, metric_name, status, raw_value, numerator, "
                      "denominator, reason_codes, judge_model, judge_prompt_revision, judge_prompt_sha256, judge_limitation) SELECT %s, run_id, benchmark_id, "
                      "question_id, submission_id, metric_name, status, raw_value, numerator, denominator, reason_codes, judge_model, judge_prompt_revision, "
                      "judge_prompt_sha256, judge_limitation FROM question_scores WHERE evaluation_id = %s", (ev, ok_eval))
            bad = [r[0] for r in c.execute("SELECT DISTINCT question_id FROM question_scores WHERE evaluation_id = %s AND metric_name = 'correctness' "
                                           "AND status = 'scored' LIMIT 2", (ev,))]
            for q in bad:
                for m in ("correctness", "faithfulness", "citation_precision", "citation_completeness"):
                    c.execute("UPDATE question_scores SET status = 'error', raw_value = NULL, numerator = NULL, denominator = NULL, "
                              "reason_codes = ARRAY['invalid_judge_reply', 'not_json'] WHERE evaluation_id = %s AND question_id = %s AND metric_name = %s", (ev, q, m))
            c.commit()
        _, damaged = self.scores(run, ev)
        res = self.redo(project, owner, run, "judge_ok", judge_errors_only=True)
        self.assertEqual((res.status_code, res.json()["questions_to_judge_again"]), (202, 2), res.text)
        new, after = self.scores(run)
        self.assertNotIn(new, (ok_eval, ev))
        for key, row in damaged.items():
            if key[0] not in bad:
                self.assertEqual(after[key], row, key)                              # not touched: value, reason and judge model are the same
        self.assertTrue(all(after[(q, "correctness")][0] == "scored" for q in bad))
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM llm_calls WHERE job_id = %s", (res.json()["job_id"],)).fetchone()[0], 2)
        # the judge_errors_only flag means nothing to another kind of run
        answer = self.api("post", owner, RUN, project, {**BODY, "judge_errors_only": True})
        self.assertEqual(answer.status_code, 400, answer.text)

    def test_a_run_that_was_never_judged_has_nothing_to_judge_again(self):
        project, owner, run = self.answered()
        res = self.redo(project, owner, run, "judge_ok", judge_errors_only=True)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("nothing_was_judged_yet", res.json()["error"]["message"])

    def check_correctness(self, m, body, n):
        """The fake judge says every fact is stated. The code check (#197) overrules it where the fixture's lexical retrieval handed the answering
        stub a different page, so the answer lacks the fact's number: those questions lose credit and are counted; the rest score 100."""
        overruled = body["judge"]["questions_overruled_by_number_check"]
        self.assertLessEqual(overruled, 3)
        self.assertGreaterEqual(m["mean"], 100.0 * (n - overruled) / n)
        self.assertEqual(m["mean"] == 100.0, overruled == 0)

    def test_a_contradicted_fact_is_scored_like_a_missing_one_but_is_marked_and_counted_in_the_results(self):
        project, owner, run = self.answered()
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        _, body = self.judge(project, owner, run, "judge_contradicted")
        self.assertEqual(self.metric(body, "correctness")["scored"], n)
        with self.connect() as c:
            reasons = c.execute("SELECT DISTINCT reason_codes FROM question_scores WHERE evaluation_id = %s AND metric_name = 'correctness'",
                                (body["evaluation"]["id"],)).fetchall()
        self.assertEqual([r[0] for r in reasons], [["contradicted_fact"]])
        self.assertEqual(body["judge"]["questions_with_a_contradicted_fact"], n)
        self.assertEqual(body["judge"]["judged_questions"], n)
        self.assertEqual(body["judge"]["prompt_revision"], method_judge.JUDGE_PROMPT_REVISION)
        self.assertIn("same provider/model family", body["judge"]["limitation"])

    def test_results_without_a_judged_evaluation_have_no_judge_summary(self):
        project, owner, run = self.answered()
        out = self.client.get(f"/api/v1/runs/{run['run_id']}/results", headers=self.headers(owner)).json()
        self.assertIsNone(out["judge"])

    def test_an_invented_fact_is_an_error_for_the_question(self):
        self._all_errors("judge_invented_fact", "judge_fact_mismatch", "fact_extra")

    def test_an_omitted_claim_is_an_error_for_the_question(self):
        self._all_errors("judge_omitted_claim", "judge_claim_mismatch", "claim_missing")

    def test_a_supporting_hit_the_claim_does_not_cite_is_an_error_for_the_question(self):
        self._all_errors("judge_foreign_hit", "judge_claim_mismatch", "supporting_hit_not_cited")

    def test_model_abstentions_are_not_judged_and_method_failures_stay_errors(self):
        project, owner, run = self.answered("answer_mixed")
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        with self.connect() as c:
            answered = c.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND answer_status = 'answered'", (run["run_id"],)).fetchone()[0]
            abst = c.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND answer_status = 'abstained' AND failure_reason IS NULL", (run["run_id"],)).fetchone()[0]
            failed = c.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND failure_reason IS NOT NULL", (run["run_id"],)).fetchone()[0]
        self.assertEqual(answered + abst + failed, n)
        started, body = self.judge(project, owner, run, "judge_ok")
        self.assertEqual(started["answered_submissions"], answered)
        corr = self.metric(body, "correctness")
        self.assertEqual((corr["error"], corr["scored"]), (failed, answered + abst))
        faith = self.metric(body, "faithfulness")
        self.assertEqual((faith["scored"], faith["not_applicable"], faith["error"]), (answered, abst, failed))
        with self.connect() as c:
            calls = c.execute("SELECT count(*) FROM (SELECT DISTINCT ON (job_id, attempt, call_id) * FROM llm_calls WHERE job_id = %s "
                              "ORDER BY job_id, attempt, call_id, seq DESC) s WHERE status = 'ok'", (started["job_id"],)).fetchone()[0]
            self.assertEqual(calls, answered)                                              # no call, no spend for abstentions/failures
            self.assertEqual(c.execute("SELECT count(*) FROM question_scores WHERE evaluation_id = %s AND judge_model IS NOT NULL AND status = 'not_applicable'",
                                       (body["evaluation"]["id"],)).fetchone()[0], 0)

    def test_a_budget_that_fits_only_some_calls_records_the_rest_as_budget_exhausted(self):
        project, owner, run = self.answered()
        n = self.api("get", owner, self.GEN, project).json()["questions"]
        _, body = self.judge(project, owner, run, "judge_ok", cap=20_000)
        m = self.metric(body, "faithfulness")
        self.assertGreater(m["scored"], 0)
        self.assertGreater(m["error"], 0)
        self.assertEqual(m["scored"] + m["error"], n)

    def test_only_a_finished_answering_run_by_an_owner_or_maintainer_can_be_judged_one_at_a_time(self):
        project, owner = self.frozen()
        baseline = self.api("post", owner, "/api/v1/benchmarks/bench-gen/revisions/1/baseline-run", project, {})
        self.assertEqual(baseline.status_code, 201, baseline.text)
        self.assertEqual(self.api("post", owner, f"/api/v1/runs/{baseline.json()['run_id']}/judge-runs", project, BODY).status_code, 409)   # no model answers
        queued = self.api("post", owner, RUN, project, BODY).json()
        self.assertEqual(self.api("post", owner, f"/api/v1/runs/{queued['run_id']}/judge-runs", project, BODY).status_code, 409)           # not finished
        self.worker("answer_ok").run_once()
        with self.connect() as c:
            maintainer = self.add_member(c, project, "maintainer")
            reviewer = self.add_member(c, project, "reviewer")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        url = f"/api/v1/runs/{queued['run_id']}/judge-runs"
        for who, code in ((reviewer, 403), (outsider, 404)):
            self.assertEqual(self.api("post", who, url, project, BODY).status_code, code, who)
        for bad in ({}, {"model": "m", "budget_cap_micros": 10, "max_output_tokens": 501}):
            self.assertEqual(self.api("post", owner, url, project, bad).status_code, 400, bad)
        self.assertEqual(self.api("post", maintainer, url, project, BODY).status_code, 202)
        self.assertEqual(self.api("post", owner, url, project, BODY).status_code, 409)                  # one active judge job
        self.assertEqual(self.api("post", owner, f"/api/v1/runs/{uuid.uuid4()}/judge-runs", project, BODY).status_code, 404)
        self.worker("judge_ok").run_once()                 # drain the queued judge job so it cannot leak into the next test

    def test_re_judging_creates_another_evaluation_and_never_updates_old_rows(self):
        project, owner, run = self.answered()
        _, first = self.judge(project, owner, run, "judge_ok")
        with self.connect() as c:
            before = c.execute("SELECT md5(string_agg(question_id::text || metric_name || status || coalesce(raw_value::text, ''), ',' "
                               "ORDER BY question_id, metric_name)) FROM question_scores WHERE evaluation_id = %s", (first["evaluation"]["id"],)).fetchone()[0]
        _, second = self.judge(project, owner, run, "judge_mixed")
        self.assertNotEqual(first["evaluation"]["id"], second["evaluation"]["id"])
        with self.connect() as c:
            after = c.execute("SELECT md5(string_agg(question_id::text || metric_name || status || coalesce(raw_value::text, ''), ',' "
                              "ORDER BY question_id, metric_name)) FROM question_scores WHERE evaluation_id = %s", (first["evaluation"]["id"],)).fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(c.execute("SELECT count(*) FROM evaluations WHERE run_id = %s", (run["run_id"],)).fetchone()[0], 3)

    def test_a_question_the_owner_excluded_before_the_freeze_is_never_judged(self):
        project, owner = self.planned()
        self.api("post", owner, self.GEN, project, BODY)
        self.run_worker("questions")
        self.api("post", owner, self.VAL, project, BODY)
        self.run_worker("validate_ok")
        review = self.api("get", owner, "/api/v1/benchmarks/bench-gen/revisions/1/questions/review", project).json()
        gone = review["items"][0]["id"]
        res = self.api("post", owner, f"/api/v1/benchmarks/bench-gen/revisions/1/questions/{gone}/exclusion", project,
                       {"excluded": True, "reason": "not a good question"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.api("post", owner, FREEZE, project, {"compliance": "partial"}).status_code, 200)
        run = self.api("post", owner, RUN, project, BODY).json()
        self.worker("answer_ok").run_once()
        _, body = self.judge(project, owner, run, "judge_ok")
        n = review["total"] - 1
        self.assertEqual(self.metric(body, "faithfulness")["scored"], n)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM question_scores WHERE evaluation_id = %s AND question_id = %s",
                                       (body["evaluation"]["id"], gone)).fetchone()[0], 0)

    def test_a_judge_score_without_the_limitation_cannot_be_stored(self):
        import psycopg
        project, owner, run = self.answered()
        self.judge(project, owner, run, "judge_ok")
        with self.connect() as c:
            with self.assertRaises(psycopg.errors.CheckViolation):
                c.execute("UPDATE question_scores SET judge_limitation = NULL WHERE metric_name = 'correctness' AND judge_model IS NOT NULL")


if __name__ == "__main__":
    unittest.main()
