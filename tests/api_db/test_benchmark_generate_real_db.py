"""Start and watch generation through the real API on real PostgreSQL (P-D slice 3b, Refs #7). lab-gen is the fake stub."""
from __future__ import annotations

import unittest
import uuid

from tests.api_db.test_generate_job_real_db import ADMIN_URL, CFG, FreshDatabase, GenerateFixtures, STUB
from lab_api.generate_questions import cross_page_target, general_target
from lab_api.generate_worker_main import GenerateWorker


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class BenchmarkGenerateApiTests(GenerateFixtures, FreshDatabase):
    def api(self, method, actor, path, project, body=None):
        url = f"{path}?project_id={project}"
        if method == "post":
            return self.client.post(url, headers=self.headers(actor), json=body if body is not None else {})
        return self.client.get(url, headers=self.headers(actor))

    def planned(self):
        project, owner, doc, sha = self.seed()
        self.parsed(project, owner, doc, sha)
        res = self.api("post", owner, "/api/v1/benchmarks", project, {"document_version_id": str(doc), "logical_id": "bench-gen"})
        self.assertEqual(res.status_code, 201, res.text)
        res = self.api("post", owner, "/api/v1/benchmarks/bench-gen/revisions/1/plan", project)
        self.assertEqual(res.status_code, 200, res.text)
        return project, owner

    GEN = "/api/v1/benchmarks/bench-gen/revisions/1/generate"

    def test_the_whole_flow_start_run_and_watch_without_ever_returning_gold(self):
        project, owner = self.planned()
        res = self.api("get", owner, self.GEN, project)
        self.assertEqual((res.status_code, res.json()["job"], res.json()["questions"], res.json()["human_review"]),
                         (200, None, 0, "skipped"))
        res = self.api("post", owner, self.GEN, project, {"model": "fake-model", "budget_cap_micros": 50_000_000})
        self.assertEqual(res.status_code, 202, res.text)
        started = res.json()
        self.assertEqual((started["status"], started["benchmark_status"], started["max_output_tokens"], started["general_target"]),
                         ("queued", "generating", 500, general_target(3)))
        queued = self.api("get", owner, self.GEN, project).json()
        self.assertEqual((queued["job"]["status"], queued["benchmark_status"]), ("queued", "generating"))

        counts = GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="api-test", interval=1, backoff=1,
                                grace=5, once_timeout=120, extra_env={"FAKE_LAB_GEN_MODE": "questions"}).run_once()
        self.assertEqual(counts.get("succeeded:generated"), 1)             # other tests of this class leave queued jobs behind
        done = self.api("get", owner, self.GEN, project)
        self.assertEqual(done.status_code, 200, done.text)
        body = done.json()
        g, x = general_target(3), cross_page_target(3)
        n = g + x
        self.assertEqual((body["job"]["status"], body["questions"], body["shortfall"]), ("succeeded", n, 0))
        self.assertEqual(body["by_bucket"], {"general": g, "cross_page": x})
        self.assertEqual(body["ledger"]["calls_by_state"], {"ok": n})
        self.assertEqual((body["ledger"]["spent_micros"], body["ledger"]["held_micros"]), (315 * n, 0))
        self.assertIn(f"written={g}", body["last_generation"])
        for leak in ("statement number", "reference_answer", "It says item", "The document states"):
            self.assertNotIn(leak, done.text)                                  # counts only: no question text, no gold

    VAL = "/api/v1/benchmarks/bench-gen/revisions/1/validate"

    def test_validation_is_started_after_generation_and_reported_as_counts_only(self):
        project, owner = self.planned()
        self.assertEqual(self.api("post", owner, self.VAL, project, {"model": "fake-model", "budget_cap_micros": 50_000_000}).status_code, 409)   # still planning
        self.api("post", owner, self.GEN, project, {"model": "fake-model", "budget_cap_micros": 50_000_000})
        mk = lambda mode: GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="api-val", interval=1, backoff=1,
                                         grace=5, once_timeout=120, extra_env={"FAKE_LAB_GEN_MODE": mode})
        mk("questions").run_once()
        after_gen = self.api("get", owner, self.GEN, project).json()
        n = general_target(3) + cross_page_target(3)
        self.assertEqual((after_gen["benchmark_status"], after_gen["questions"], after_gen["validation"]["unvalidated"]), ("validating", n, n))
        started = self.api("post", owner, self.VAL, project, {"model": "fake-model", "budget_cap_micros": 300_000})
        self.assertEqual((started.status_code, started.json()["phase"], started.json()["benchmark_status"]), (202, "validate", "validating"))
        # the request is the budget of THIS run; the stored per-benchmark cap is what was already spent plus it
        self.assertEqual(started.json()["benchmark_cap_micros"], 315 * n + 300_000)
        self.assertEqual(self.api("post", owner, self.VAL, project, {"model": "fake-model", "budget_cap_micros": 1}).status_code, 409)
        mk("validate_mixed").run_once()
        done = self.api("get", owner, self.GEN, project)
        body = done.json()
        self.assertEqual((body["job"]["phase"], body["job"]["status"]), ("validate", "succeeded"))
        self.assertEqual(body["validation"]["pass"] + body["validation"]["fail"], n)
        self.assertGreater(body["validation"]["fail"], 0)
        self.assertEqual((body["validation"]["unvalidated"], body["benchmark_status"], body["human_review"]), (0, "validating", "skipped"))
        for leak in ("statement number", "supported", "reference_answer"):
            self.assertNotIn(leak, done.text)
        # the same report read through a NON-superuser role (row level security applies) still counts the verdicts
        from lab_api.benchmark_generate import _progress
        role = self.app_role()
        with self.connect() as c:
            c.execute(f'SET ROLE "{role}"')
            from lab_db.repo import authorize
            owner_id = owner
            report = _progress(c, owner_id, project, "bench-gen", 1)
        self.assertEqual(report["validation"]["pass"] + report["validation"]["fail"], n)

    def test_only_owner_or_maintainer_may_start_and_a_reviewer_may_read(self):
        project, owner = self.planned()
        with self.connect() as c:
            participant = self.add_member(c, project, "participant")
            reviewer = self.add_member(c, project, "reviewer")
            maintainer = self.add_member(c, project, "maintainer")
        outsider = uuid.uuid4()
        with self.connect() as c:
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        body = {"model": "fake-model", "budget_cap_micros": 1000}
        self.assertEqual(self.api("post", participant, self.GEN, project, body).status_code, 403)
        self.assertEqual(self.api("post", reviewer, self.GEN, project, body).status_code, 403)
        self.assertEqual(self.api("post", outsider, self.GEN, project, body).status_code, 404)
        self.assertEqual(self.api("get", participant, self.GEN, project).status_code, 403)
        self.assertEqual(self.api("get", outsider, self.GEN, project).status_code, 404)
        self.assertEqual(self.api("get", reviewer, self.GEN, project).status_code, 200)
        self.assertEqual(self.api("post", maintainer, self.GEN, project, body).status_code, 202)

    def test_bad_requests_and_wrong_states_are_refused_and_start_nothing(self):
        project, owner = self.planned()
        for body in ({"model": "m", "budget_cap_micros": 1000, "max_output_tokens": 501},
                     {"model": "m", "budget_cap_micros": 0}, {"model": "m"}, {"budget_cap_micros": 10},
                     {"model": "m", "budget_cap_micros": 10, "extra": 1}, {"model": "m", "budget_cap_micros": "10"}):
            self.assertEqual(self.api("post", owner, self.GEN, project, body).status_code, 400, body)
        self.assertEqual(self.api("get", owner, self.GEN, project).json()["job"], None)
        ok = {"model": "m", "budget_cap_micros": 1000}
        self.assertEqual(self.api("post", owner, self.GEN, project, ok).status_code, 202)
        self.assertEqual(self.api("post", owner, self.GEN, project, ok).status_code, 409)               # one active job
        self.assertEqual(self.api("post", owner, "/api/v1/benchmarks/nope/revisions/1/generate", project, ok).status_code, 404)

    def test_a_benchmark_that_is_not_planning_cannot_start(self):
        project, owner, doc, sha = self.seed()
        self.api("post", owner, "/api/v1/benchmarks", project, {"document_version_id": str(doc), "logical_id": "bench-gen"})
        res = self.api("post", owner, self.GEN, project, {"model": "m", "budget_cap_micros": 1000})   # still 'parsing'
        self.assertEqual(res.status_code, 409, res.text)


if __name__ == "__main__":
    unittest.main()
