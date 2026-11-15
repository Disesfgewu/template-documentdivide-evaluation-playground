"""The owner sets HOW MANY questions to generate; the budget cap is derived from exactly those calls (Issue #186), real PostgreSQL."""
from __future__ import annotations

import unittest

from tests.api_db.test_benchmark_generate_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateApiFixtures, STUB
from lab_api import run_estimate as est
from lab_api.generate_worker_main import GenerateWorker
from lab_db.storage import LocalArtifactStore

BASE = "/api/v1/benchmarks/bench-gen/revisions/1"
PLAN_TOTAL = 18        # a 3-page document: 14 general + 2 cross_page + 2 confusable


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class QuestionCountRealDbTests(GenerateApiFixtures, FreshDatabase):
    def gen(self, owner, project, body):
        return self.api("post", owner, self.GEN, project, {"model": "fake-model", **body})

    def worker(self, mode):
        return GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="count-test", interval=1, backoff=1, grace=5,
                              once_timeout=120, store=LocalArtifactStore(self.root), extra_env={"FAKE_LAB_GEN_MODE": mode})

    def estimate(self, owner, project, query):
        return self.client.get(f"{BASE}/estimate?project_id={project}&{query}", headers=self.headers(owner))

    def ledger(self, owner, project):
        return self.api("get", owner, self.GEN, project).json()

    def test_the_estimate_for_ten_questions_is_ten_calls_in_the_plans_proportions_with_a_usd_figure(self):
        project, owner = self.planned()
        res = self.estimate(owner, project, "phase=generate&question_count=10")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual((body["calls"], body["question_count"], body["plan_total"]), (10, 10, PLAN_TOTAL))
        self.assertEqual(sum(body["split"].values()), 10)
        self.assertEqual(body["split"], {"general": 8, "cross_page": 1, "confusable": 1})
        self.assertGreater(body["estimated_max_micros"], 0)
        self.assertEqual(body["currency"], "USD")
        self.assertRegex(body["estimated_max_usd"], r"^\d+\.\d{4}$")
        default = self.estimate(owner, project, "phase=generate").json()
        self.assertEqual((default["calls"], default["question_count"]), (PLAN_TOTAL, PLAN_TOTAL))
        self.assertLess(body["estimated_max_micros"], default["estimated_max_micros"])        # fewer questions, lower worst case

    def test_ten_questions_send_ten_calls_and_the_derived_cap_fits_exactly_those_calls(self):
        project, owner = self.planned()
        est_body = self.estimate(owner, project, "phase=generate&question_count=10").json()
        res = self.gen(owner, project, {"question_count": 10})                                # NO budget: it is derived
        self.assertEqual(res.status_code, 202, res.text)
        body = res.json()
        self.assertTrue(body["budget_derived"])
        self.assertEqual(body["budget_cap_micros"], est_body["estimated_max_micros"])
        self.assertEqual(body["requested"], {"general": 8, "cross_page": 1, "confusable": 1})
        self.worker("questions").run_once()
        progress = self.ledger(owner, project)
        self.assertEqual(progress["ledger"]["calls_by_state"], {"ok": 10})                    # exactly 10 calls, none skipped for budget
        self.assertEqual(progress["questions"], 10)
        self.assertEqual(progress["by_bucket"], {"general": 8, "cross_page": 1, "confusable": 1})
        self.assertLessEqual(progress["ledger"]["spent_micros"], est_body["estimated_max_micros"])
        self.assertEqual(progress["job"]["status"], "succeeded")

    def test_without_a_count_or_a_budget_the_whole_plan_is_generated_inside_its_derived_cap(self):
        project, owner = self.planned()
        res = self.gen(owner, project, {})
        self.assertEqual(res.status_code, 202, res.text)
        self.assertEqual(res.json()["estimate"]["calls"], PLAN_TOTAL)
        self.worker("questions").run_once()
        progress = self.ledger(owner, project)
        self.assertEqual(progress["ledger"]["calls_by_state"], {"ok": PLAN_TOTAL})
        self.assertEqual(progress["questions"], PLAN_TOTAL)

    def test_an_explicit_cap_still_works_and_a_count_above_the_plan_or_on_other_phases_is_refused(self):
        project, owner = self.planned()
        self.assertEqual(self.gen(owner, project, {"question_count": PLAN_TOTAL + 1}).status_code, 400)
        self.assertEqual(self.gen(owner, project, {"question_count": 0}).status_code, 400)
        self.assertEqual(self.estimate(owner, project, "phase=generate&question_count=0").status_code, 400)
        self.assertEqual(self.estimate(owner, project, "phase=validate&question_count=3").status_code, 400)
        self.assertEqual(self.estimate(owner, project, "phase=nonsense").status_code, 400)
        res = self.gen(owner, project, {"question_count": 4, "budget_cap_micros": 50_000_000})
        self.assertEqual(res.status_code, 202, res.text)
        self.assertFalse(res.json()["budget_derived"])
        self.assertEqual(res.json()["budget_cap_micros"], 50_000_000)
        self.worker("questions").run_once()
        self.assertEqual(self.ledger(owner, project)["questions"], 4)

    def test_a_failed_generation_says_why(self):
        project, owner = self.planned()
        self.assertEqual(self.gen(owner, project, {"question_count": 10, "budget_cap_micros": 5}).status_code, 202)   # 5 micro-USD: nothing fits
        self.worker("questions").run_once()
        job = self.ledger(owner, project)["job"]
        self.assertEqual((job["status"], job["failure_reason"]), ("failed", "budget_exhausted"))
        ok = self.ledger(owner, project)["ledger"]["calls_by_state"]
        self.assertEqual(ok, {"skipped_budget": 10})                                           # only the 10 asked for, all skipped

    def test_validation_and_the_model_runs_derive_their_caps_and_the_calls_fit(self):
        project, owner = self.planned()
        self.assertEqual(self.gen(owner, project, {"question_count": 6}).status_code, 202)
        self.worker("questions").run_once()
        v_est = self.estimate(owner, project, "phase=validate").json()
        self.assertEqual(v_est["calls"], 6)
        res = self.api("post", owner, self.VAL, project, {"model": "fake-model"})                     # no budget
        self.assertEqual(res.status_code, 202, res.text)
        self.assertEqual((res.json()["budget_derived"], res.json()["budget_cap_micros"]), (True, v_est["estimated_max_micros"]))
        self.worker("validate_ok").run_once()
        self.assertEqual(self.ledger(owner, project)["validation"]["pass"], 6)
        self.assertEqual(self.api("post", owner, f"{BASE}/freeze", project, {"compliance": "partial"}).status_code, 200)
        a_est = self.estimate(owner, project, "phase=answer").json()
        self.assertEqual(a_est["calls"], 6)
        run = self.api("post", owner, f"{BASE}/method-runs", project, {"model": "fake-model"})
        self.assertEqual(run.status_code, 202, run.text)
        self.assertEqual((run.json()["budget_derived"], run.json()["budget_cap_micros"]), (True, a_est["estimated_max_micros"]))
        self.worker("answer_ok").run_once()
        results = self.client.get(f"/api/v1/runs/{run.json()['run_id']}/results", headers=self.headers(owner)).json()
        self.assertEqual(results["submissions"]["answered"], 6)                                # the bound covered every real prompt
        self.assertEqual(results["submissions"]["method_failures"], {})
        j = self.api("post", owner, f"/api/v1/runs/{run.json()['run_id']}/judge-runs", project, {"model": "fake-model"})
        self.assertEqual(j.status_code, 202, j.text)
        self.assertTrue(j.json()["budget_derived"])
        self.assertEqual(j.json()["estimate"]["calls"], 6)
        self.worker("judge_ok").run_once()
        run_id = run.json()["run_id"]
        self.assertEqual(self.estimate(owner, project, f"phase=judge&run_id={run_id}").json()["calls"], 6)
        self.assertEqual(self.estimate(owner, project, "phase=judge").status_code, 404)                 # a judge estimate needs its run
        judged = self.client.get(f"/api/v1/runs/{run.json()['run_id']}/results", headers=self.headers(owner)).json()
        self.assertEqual(judged["judge"]["judged_questions"], 6)
        self.assertEqual(judged["submissions"]["method_failures"], {})

    def test_without_configured_prices_nothing_is_derived_and_the_ceiling_is_enforced(self):
        from types import SimpleNamespace as S
        none = S(gen_price_in_micros_per_mtok=None, gen_price_out_micros_per_mtok=None)
        with self.assertRaises(est.EstimateError) as ctx:
            est.prices(none)
        self.assertIn("prices are not configured", str(ctx.exception))
        with self.assertRaises(est.EstimateError) as ctx:
            est._result("generate", 3, est.CEILING_MICROS + 1)
        self.assertIn("ceiling", str(ctx.exception))
        self.assertEqual(est.usd(50_000), "0.0500")
        self.assertEqual(est.split_question_count(3, PLAN_TOTAL), (14, 2, 2))
        self.assertEqual(est.split_question_count(3, 1), (1, 0, 0))
        for n in range(1, PLAN_TOTAL + 1):
            self.assertEqual(sum(est.split_question_count(3, n)), n, n)


if __name__ == "__main__":
    unittest.main()
