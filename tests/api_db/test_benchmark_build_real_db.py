"""Create a benchmark, pin a parse and read the material plan, through the real API on real PostgreSQL (P-B, Refs #7).

Reuses the parse-result fixtures of test_promote_real_db (real upload, real persist_parse_result)."""
from __future__ import annotations

import unittest
import uuid

from tests.api_db import test_promote_real_db as base     # a module import: the loader must not collect its test class here

ADMIN_URL, marked = base.ADMIN_URL, base.marked


def _only_helpers(cls):
    """The helpers and fixtures of PromoteRealDbTests without its own test methods."""
    for name in dir(base.PromoteRealDbTests):
        if name.startswith("test_"):
            setattr(cls, name, None)
    return cls


@_only_helpers
@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class BenchmarkBuildRealDbTests(base.PromoteRealDbTests):
    def post(self, actor, path, project, json_body=None):
        return self.client.post(f"{path}?project_id={project}", headers=self.headers(actor), json=json_body if json_body is not None else {})

    def create(self, project, owner, doc, **extra):
        res = self.post(owner, "/api/v1/benchmarks", project, {"document_version_id": str(doc), **extra})
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def test_the_whole_flow_create_pin_plan_and_read(self):
        project, owner, doc, sha = self.seed()
        # page 1 clean, page 2 has no element (needs_review), page 3 has evidence but hidden text
        job = self.parsed(project, owner, doc, sha, mutate=lambda b: marked(b, 2, "needs_review", ["text_not_visible"]))
        bench = self.create(project, owner, doc, logical_id="bench-flow")
        self.assertEqual((bench["status"], bench["logical_id"], bench["revision"], bench["n_target"], bench["q_min"]),
                         ("parsing", "bench-flow", 1, 20, 2))
        # no plan before the pin
        res = self.client.get(f"/api/v1/benchmarks/bench-flow/revisions/1/plan?project_id={project}", headers=self.headers(owner))
        self.assertEqual(res.status_code, 409, res.text)

        res = self.post(owner, "/api/v1/benchmarks/bench-flow/revisions/1/plan", project)
        self.assertEqual(res.status_code, 200, res.text)
        plan = res.json()
        self.assertEqual((plan["status"], plan["parse_job_id"], plan["promotion_rule"]), ("planning", str(job), "g8-clean-pages-v1"))
        self.assertEqual(plan["pages"]["usable"], [0])
        self.assertEqual(plan["pages"]["insufficient_material"], {"1": "status_needs_review", "2": "unsafe_text:text_not_visible"})
        self.assertEqual(plan["evidence_units"], 1)
        self.assertEqual(plan["buckets"]["general"]["status"], "material_available")
        self.assertEqual(plan["buckets"]["chart_table"]["status"], "insufficient_material")      # D5
        self.assertEqual(plan["buckets"]["cross_page"]["status"], "insufficient_material")       # one clean page
        self.assertEqual((plan["human_review"]["status"], plan["compliance_projection"]), ("skipped", "partial"))   # D6
        self.assertTrue(any("hidden text" in x.lower() for x in plan["limits"]))                 # residual G8 limit

        again = self.post(owner, "/api/v1/benchmarks/bench-flow/revisions/1/plan", project)      # idempotent
        self.assertEqual((again.status_code, again.json()), (200, plan))
        self.assertEqual(self.client.get(f"/api/v1/benchmarks/bench-flow/revisions/1/plan?project_id={project}",
                                         headers=self.headers(owner)).json(), plan)
        other = self.post(owner, "/api/v1/benchmarks/bench-flow/revisions/1/plan", project, {"parse_job_id": str(uuid.uuid4())})
        self.assertEqual(other.status_code, 409)                                                   # the pin never moves
        # the existing read endpoint now shows the new status
        state = self.client.get(f"/api/v1/benchmarks/bench-flow/revisions/1?project_id={project}", headers=self.headers(owner)).json()
        self.assertEqual(state["status"], "planning")
        with self.connect() as c:
            statuses = [r[0] for r in c.execute("SELECT to_status::text FROM benchmark_transitions bt JOIN benchmark_revisions br "
                                                "ON br.id = bt.benchmark_id WHERE br.id = %s ORDER BY bt.id", (bench["benchmark_revision_id"],))]
        self.assertEqual(statuses, ["parsing", "planning"])

    def test_planning_needs_a_succeeded_parse_and_names_the_next_step(self):
        project, owner, doc, sha = self.seed()
        self.create(project, owner, doc, logical_id="bench-noparse")
        res = self.post(owner, "/api/v1/benchmarks/bench-noparse/revisions/1/plan", project)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("parse", res.json()["error"]["message"])
        self.assertEqual(self.client.get(f"/api/v1/benchmarks/bench-noparse/revisions/1?project_id={project}",
                                         headers=self.headers(owner)).json()["status"], "parsing")    # unchanged

    def test_roles_unknown_ids_duplicates_and_bad_bodies(self):
        project, owner, doc, sha = self.seed()
        self.parsed(project, owner, doc, sha)
        with self.connect() as c:
            reviewer = self.add_member(c, project, "reviewer")
            participant = self.add_member(c, project, "participant")
            _, outsider = self.make_project(c)
        body = {"document_version_id": str(doc), "logical_id": "bench-roles"}
        self.assertEqual(self.post(outsider, "/api/v1/benchmarks", project, body).status_code, 404)        # not a member
        for actor in (participant, reviewer):                                                                # reviewer: comments only
            self.assertEqual(self.post(actor, "/api/v1/benchmarks", project, body).status_code, 403)
        self.assertEqual(self.post(owner, "/api/v1/benchmarks", project, {"document_version_id": str(uuid.uuid4())}).status_code, 404)
        for bad in ({}, {"document_version_id": "x"}, {"document_version_id": str(doc), "extra": 1},
                    {"document_version_id": str(doc), "logical_id": "bad id!"}):
            self.assertEqual(self.post(owner, "/api/v1/benchmarks", project, bad).status_code, 400, bad)
        self.create(project, owner, doc, logical_id="bench-roles")
        self.assertEqual(self.post(owner, "/api/v1/benchmarks", project, body).status_code, 409)           # duplicate logical_id
        self.assertEqual(self.post(owner, "/api/v1/benchmarks/nope/revisions/1/plan", project).status_code, 404)
        self.assertEqual(self.post(participant, "/api/v1/benchmarks/bench-roles/revisions/1/plan", project).status_code, 403)
        self.assertEqual(self.post(outsider, "/api/v1/benchmarks/bench-roles/revisions/1/plan", project).status_code, 404)
        plan_url = f"/api/v1/benchmarks/bench-roles/revisions/1/plan?project_id={project}"
        self.assertEqual(self.client.get(plan_url, headers=self.headers(participant)).status_code, 403)    # no gold access
        self.assertEqual(self.client.get(plan_url, headers=self.headers(outsider)).status_code, 404)
        self.post(owner, "/api/v1/benchmarks/bench-roles/revisions/1/plan", project)
        self.assertEqual(self.client.get(plan_url, headers=self.headers(reviewer)).status_code, 200)       # reviewers may read it

    def test_the_planned_benchmark_is_the_one_the_database_pinned_and_audits_the_creation(self):
        project, owner, doc, sha = self.seed()
        job = self.parsed(project, owner, doc, sha)
        bench = self.create(project, owner, doc)
        self.assertTrue(bench["logical_id"].startswith("bench-"))                                          # generated id
        self.post(owner, f"/api/v1/benchmarks/{bench['logical_id']}/revisions/1/plan", project, {"parse_job_id": str(job)})
        with self.connect() as c:
            pin = c.execute("SELECT parse_job_id FROM benchmark_parse_sources WHERE benchmark_id = %s",
                            (bench["benchmark_revision_id"],)).fetchone()
            audit = c.execute("SELECT count(*) FROM audit_events WHERE project_id = %s AND action = 'benchmark.created'",
                              (project,)).fetchone()[0]
            lineage = c.execute("SELECT count(*) FROM lineage_edges le JOIN resource_versions rv ON rv.id = le.downstream_id "
                                "WHERE rv.logical_id = %s AND le.relation = 'derived_from'", (bench["logical_id"],)).fetchone()[0]
        self.assertEqual((pin[0], audit, lineage), (job, 1, 1))


if __name__ == "__main__":
    unittest.main()
