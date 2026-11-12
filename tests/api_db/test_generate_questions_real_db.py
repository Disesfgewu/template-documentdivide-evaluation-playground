"""General-bucket question generation and gold_answers on real PostgreSQL (P-D slice 3, Refs #7, Refs #10).

lab-gen is tests/fixtures/fake_lab_gen.py in ``replies`` mode (canned reply text, no network, no model). Evidence is the
real promoted clean evidence of the synthetic 3-page document (clean pages 0 and 2); n_target 20 / q_min 2 gives a
general target of 14 calls. Jobs, leases, RLS, triggers and the transaction boundary are the real database's."""
from __future__ import annotations

import contextlib
import json
import sys
import unittest
import uuid
from pathlib import Path

from psycopg import errors
from psycopg.types.json import Jsonb

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.generate_general import build_calls, on_results
from lab_api.generate_worker import GenModelConfig, process_generate_job
from lab_db.generation import enqueue_generate_job
from lab_db.repo import grant_app_privileges, transition_benchmark
from tests.api_db.test_promote_real_db import ADMIN_URL, FreshDatabase, PromoteFixtures

STUB = [sys.executable, str(_REPO / "tests" / "fixtures" / "fake_lab_gen.py")]
CFG = GenModelConfig(protocol="openai_chat", base_url="https://x.example-llm.com", allowed_host="x.example-llm.com",
                     input_micros_per_mtok=3_000_000, output_micros_per_mtok=15_000_000)
MODEL, CAP, CALLS = "claude-haiku-4-5-20251001", 2_000_000, 14


def good(i, page_fact="Text on page 1"):
    return json.dumps({"question": f"Question number {i} about the page?", "reference_answer": f"Answer {i}.",
                       "facts": [{"statement": page_fact, "weight": 1}]})


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class GenerateQuestionsRealDbTests(PromoteFixtures, FreshDatabase):
    def generating(self):
        project, owner, doc, sha = self.seed()
        job = self.parsed(project, owner, doc, sha)
        bench = self.benchmark(project, owner, doc)
        self.promote(project, bench, job, owner)
        with self.connect() as c:
            for version, to in ((1, "parsing"), (2, "planning")):
                transition_benchmark(c, project, bench, version, to, "test", owner)
            gen = enqueue_generate_job(c, project_id=project, actor_id=owner, benchmark_id=bench, model=MODEL,
                                       max_output_tokens=500, budget_cap_micros=CAP, currency="USD", pricing_revision="t1")
        return project, owner, bench, gen

    def run_job(self, project, job, replies, builder=build_calls):
        with self.connect() as c:
            return process_generate_job(c, project, job, worker="w1", lab_gen_cmd=STUB, cfg=CFG, build_calls=builder,
                                        connect=self.connect, api_key="k", on_results=on_results,
                                        extra_env={"FAKE_LAB_GEN_MODE": "replies", "FAKE_LAB_GEN_REPLIES": json.dumps(replies)})

    def one(self, sql, *args):
        with self.connect() as c:
            return c.execute(sql, args).fetchone()

    def bench_state(self, bench):
        return self.one("SELECT b.status::text, (SELECT reason FROM benchmark_transitions t WHERE t.benchmark_id = b.id "
                        "ORDER BY t.id DESC LIMIT 1) FROM benchmark_revisions b WHERE b.id = %s", bench)

    def counts(self, bench):
        return tuple(self.one(f"SELECT count(*) FROM {t} WHERE benchmark_id = %s", bench)[0]
                     for t in ("questions", "facts", "evidence_sets", "evidence_set_members", "gold_answers"))

    # ---------------------------------------------------------------------------------------------------------

    def test_good_replies_write_questions_and_three_gold_rows_each_in_one_transaction(self):
        project, owner, bench, job = self.generating()
        with self.connect() as c:
            calls = build_calls(c, project, bench)
        self.assertEqual(len(calls), CALLS)
        self.assertEqual({c.id.split("-p")[1] for c in calls}, {"0", "2"})          # only the clean pages
        self.assertTrue(all("Text on page" in c.user and str(bench) not in c.user for c in calls))
        out = self.run_job(project, job, [good(i) for i in range(CALLS)])
        self.assertEqual((out.status, out.reason), ("succeeded", "generated"))
        self.assertEqual(self.counts(bench), (CALLS, CALLS, CALLS, CALLS, 3 * CALLS))   # one unit per clean page
        status, reason = self.bench_state(bench)
        self.assertEqual(status, "validating")
        self.assertIn(f"generated {CALLS} general question(s)", reason)
        self.assertIn("human review skipped", reason)
        with self.connect() as c:
            kinds = c.execute("SELECT question_id, array_agg(kind ORDER BY kind) FROM gold_answers WHERE benchmark_id = %s "
                              "GROUP BY question_id", (bench,)).fetchall()
            self.assertEqual({tuple(k) for _, k in kinds}, {("semantic", "source", "textual")})
            q, page_unit = c.execute(
                "SELECT q.id, (SELECT eu.id FROM evidence_units eu JOIN evidence_regions er ON er.evidence_unit_id = eu.id "
                "JOIN benchmark_revisions b ON b.document_version_id = eu.document_version_id "
                "WHERE b.id = q.benchmark_id AND eu.raw_content = 'Text on page 1') FROM questions q WHERE q.benchmark_id = %s AND q.ordinal = 0",
                (bench,)).fetchone()
            src = c.execute("SELECT payload FROM gold_answers WHERE question_id = %s AND kind = 'source'", (q,)).fetchone()[0]
            txt = c.execute("SELECT payload FROM gold_answers WHERE question_id = %s AND kind = 'textual'", (q,)).fetchone()[0]
            ok_calls = c.execute("SELECT count(*) FROM llm_calls WHERE job_id = %s AND status = 'ok'", (job,)).fetchone()[0]
        self.assertEqual(src["evidence_sets"][0]["evidence_ids"], [str(page_unit)])   # call g0 is page 0
        self.assertEqual([ch["supporting_evidence_ids"] for ch in txt["checks"]], [[str(page_unit)]])
        self.assertEqual(ok_calls, CALLS)

    def test_invalid_replies_are_dropped_and_counted_duplicates_dropped_and_the_shortfall_recorded(self):
        project, owner, bench, job = self.generating()
        out = self.run_job(project, job, ["garbage, not json", good(0), good(0)])      # cycled over 14 calls
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(self.counts(bench)[0], 1)                                     # never padded
        self.assertEqual(self.counts(bench)[4], 3)
        status, reason = self.bench_state(bench)
        self.assertEqual(status, "validating")
        self.assertIn("generated 1 general question(s) from 14 call(s)", reason)
        self.assertIn("dropped invalid 5, duplicate 8", reason)
        self.assertIn("shortfall: 1 general question(s) < q_min 2 (not padded)", reason)

    def test_a_lost_lease_writes_no_question_and_no_gold(self):
        project, owner, bench, job = self.generating()

        def expire_then_build(conn, p, b):
            with self.connect() as c:
                c.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second', "
                          "row_version = row_version + 1 WHERE id = %s", (job,))
            return build_calls(conn, p, b)
        out = self.run_job(project, job, [good(i) for i in range(CALLS)], builder=expire_then_build)
        self.assertEqual(out.status, "abandoned")
        self.assertEqual(self.counts(bench), (0, 0, 0, 0, 0))
        self.assertEqual(self.bench_state(bench)[0], "generating")
        # the paid calls are still in the ledger (never lost), but nothing derived from them was stored
        self.assertGreater(self.one("SELECT count(*) FROM llm_calls WHERE job_id = %s AND status <> 'reserved'", job)[0], 0)

    # ---- gold isolation and guards ------------------------------------------------------------------------

    @contextlib.contextmanager
    def as_actor(self, conn, role, actor):
        with conn.transaction():
            conn.execute(f'SET LOCAL ROLE "{role}"')
            conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))
            yield

    def app_role(self):
        role = f"lab_app_{uuid.uuid4().hex[:10]}"
        with self.connect(autocommit=True) as c:
            c.execute(f'CREATE ROLE "{role}" NOLOGIN')
            grant_app_privileges(c, role)

        def cleanup():
            with self.connect(autocommit=True) as c:
                c.execute(f'DROP OWNED BY "{role}"')
                c.execute(f'DROP ROLE IF EXISTS "{role}"')
        self.addCleanup(cleanup)
        return role

    def test_participant_and_observer_cannot_read_gold_answers_owner_and_reviewer_can(self):
        project, owner, bench, job = self.generating()
        self.run_job(project, job, [good(i) for i in range(CALLS)])
        role = self.app_role()
        with self.connect() as c:
            members = {r: self.add_member(c, project, r) for r in ("participant", "observer", "reviewer")}
        seen = {}
        for name, actor in (("owner", owner), *members.items()):
            with self.connect() as c, self.as_actor(c, role, actor):
                seen[name] = c.execute("SELECT count(*) FROM gold_answers WHERE benchmark_id = %s", (bench,)).fetchone()[0]
        self.assertEqual(seen, {"owner": 3 * CALLS, "reviewer": 3 * CALLS, "participant": 0, "observer": 0})
        with self.connect() as c, self.assertRaises(errors.InsufficientPrivilege):      # RLS WITH CHECK
            with self.as_actor(c, role, members["participant"]):
                c.execute("INSERT INTO gold_answers (project_id, benchmark_id, question_id, kind, payload) "
                          "VALUES (%s, %s, %s, 'semantic', '{}')", (project, bench, uuid.uuid4()))

    def test_gold_answers_are_append_only_and_a_frozen_benchmark_refuses_inserts(self):
        project, owner, bench, job = self.generating()
        self.run_job(project, job, [good(i) for i in range(CALLS)])
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):
            c.execute("UPDATE gold_answers SET payload = '{}' WHERE benchmark_id = %s", (bench,))
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):
            c.execute("DELETE FROM gold_answers WHERE benchmark_id = %s", (bench,))
        with self.connect() as c, self.assertRaises(errors.CheckViolation):              # payload size cap
            c.execute("INSERT INTO gold_answers (project_id, benchmark_id, question_id, kind, payload) "
                      "SELECT %s, %s, id, 'semantic', %s FROM questions WHERE benchmark_id = %s LIMIT 1",
                      (project, bench, Jsonb({"x": "y" * 300_000}), bench))
        with self.connect() as c:
            version = c.execute("SELECT row_version FROM benchmark_revisions WHERE id = %s", (bench,)).fetchone()[0]
            version = transition_benchmark(c, project, bench, version, "ready_to_freeze", "test", owner)
            transition_benchmark(c, project, bench, version, "frozen", "test", owner, compliance="partial")
            qid = c.execute("SELECT id FROM questions WHERE benchmark_id = %s LIMIT 1", (bench,)).fetchone()[0]
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation) as caught:
            c.execute("INSERT INTO gold_answers (project_id, benchmark_id, question_id, kind, payload) "
                      "VALUES (%s, %s, %s, 'semantic', '{\"a\": 1}')", (project, bench, qid))
        self.assertIn("frozen", str(caught.exception))
        self.assertNotIsInstance(caught.exception, errors.UniqueViolation)


if __name__ == "__main__":
    unittest.main()
