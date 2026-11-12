"""General-bucket question generation on real PostgreSQL (P-D slice 3, Refs #7): build_calls from the pinned parse,
on_results writing questions/facts/evidence sets/gold, gold RLS, frozen guard. lab-gen is the fake stub; no model."""
from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

from psycopg import errors

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.generate_questions import cross_page_target, general_target, make_build_calls, make_on_results, question_hash
from lab_contracts.benchmark import SemanticAnswer, SourceAnswer, TextualAnswer
from lab_db.repo import grant_app_privileges
from tests.api_db.test_generate_job_real_db import ADMIN_URL, FreshDatabase, GenerateFixtures

BIG = 50_000_000      # the budget is not what these tests are about


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class GenerateQuestionsRealDbTests(GenerateFixtures, FreshDatabase):
    def run_batch(self, mode, cap=BIG):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench, cap=cap)
        out = self.process(project, job, mode, build_calls=make_build_calls(), on_results=make_on_results())
        return project, owner, bench, job, out

    def count(self, table, bench):
        with self.connect() as c:
            return c.execute(f"SELECT count(*) FROM {table} WHERE benchmark_id = %s", (bench,)).fetchone()[0]

    def audit_reason(self, project):
        with self.connect() as c:
            return c.execute("SELECT reason FROM audit_events WHERE project_id = %s AND action = 'benchmark.questions_generated'",
                             (project,)).fetchall()

    def test_build_calls_sends_only_the_clean_pages_text_and_nothing_else(self):
        project, owner, bench = self.planning_benchmark()
        with self.connect() as c:
            calls = list(make_build_calls()(c, project, bench))
        self.assertEqual(len(calls), general_target(3) + cross_page_target(3))   # N=20, Q=2 -> 14 general + 2 cross_page
        pages = {c.id.split("-p")[1] for c in calls}
        self.assertEqual(pages, {"0", "2"})                                   # only the promoted pages (1 and 3 of the fixture)
        for c in calls:
            self.assertIn("Text on page", c.user)
            for forbidden in (str(project), str(owner), str(bench), "diagnostic", ".pdf"):
                self.assertNotIn(forbidden, c.user + c.system)
        self.assertEqual(len({c.id for c in calls}), len(calls))

    def test_a_good_batch_writes_questions_facts_evidence_sets_and_all_three_gold_kinds(self):
        project, owner, bench, job, out = self.run_batch("questions")
        self.assertEqual((out.status, out.reason), ("succeeded", "generated"))
        n, x = general_target(3), cross_page_target(3)
        # 14 general (1 fact, 1 unit each) + 2 cross_page (2 facts, 2 units = both pages, each)
        self.assertEqual([self.count(t, bench) for t in ("questions", "facts", "evidence_sets", "gold_answers")],
                         [n + x, n + 2 * x, n + x, 3 * (n + x)])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(DISTINCT question_hash) FROM questions WHERE benchmark_id = %s", (bench,)).fetchone()[0], n + x)
            self.assertEqual(dict(c.execute("SELECT quota_bucket, count(*) FROM questions WHERE benchmark_id = %s GROUP BY 1", (bench,))),
                             {"general": n, "cross_page": x})
            self.assertEqual(c.execute("SELECT count(*) FROM evidence_set_members WHERE benchmark_id = %s", (bench,)).fetchone()[0], n + 2 * x)
            rows = c.execute("SELECT kind, payload FROM gold_answers WHERE benchmark_id = %s", (bench,)).fetchall()
        models = {"semantic": SemanticAnswer, "textual": TextualAnswer, "source": SourceAnswer}
        for kind, payload in rows:
            models[kind].model_validate_json(json.dumps(payload))                   # every stored gold satisfies its contract
        reason = self.audit_reason(project)
        self.assertEqual(len(reason), 1)
        self.assertIn(f"written={n}", reason[0][0])
        self.assertIn(f"cross_written={x}", reason[0][0])
        self.assertIn("shortfall=0", reason[0][0])
        self.assertIn("cross_shortfall=0", reason[0][0])
        self.assertIn("prompt=general-page-v1", reason[0][0])
        self.assertEqual(self.bench_status(bench), "validating")                     # generating -> validating, never frozen

    def test_unparseable_replies_write_nothing_and_are_counted(self):
        project, owner, bench, job, out = self.run_batch("badjson")
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(self.count("questions", bench), 0)
        self.assertEqual(self.bench_status(bench), "generating")                     # nothing was written: no validating
        self.assertIn(f"unparseable={general_target(3) + cross_page_target(3)}", self.audit_reason(project)[0][0])
        self.assertIn(f"shortfall={general_target(3)}", self.audit_reason(project)[0][0])    # recorded, never padded

    def test_duplicate_questions_are_dropped_by_hash(self):
        project, owner, bench, job, out = self.run_batch("dupes")
        self.assertEqual(self.count("questions", bench), 2)                          # one general + one cross_page survive
        self.assertIn(f"duplicate={general_target(3) - 1 + cross_page_target(3) - 1}", self.audit_reason(project)[0][0])

    def test_a_partial_batch_still_keeps_the_paid_valid_questions(self):
        project, owner, bench, job, out = self.run_batch("partial_questions")
        self.assertEqual((out.status, out.reason), ("failed", "partial_calls"))
        self.assertEqual(self.count("questions", bench), general_target(3) - 1 + cross_page_target(3))   # all ok replies but the failed second call

    def test_the_source_answer_lists_only_the_units_the_model_was_shown(self):
        from lab_api.generate_prompts import shown_pieces
        units = [("a", "x" * 40), ("b", "y" * 40), ("c", "z" * 40)]
        self.assertEqual([e for e, _ in shown_pieces(units, 60)], ["a", "b"])         # the cut lands inside b: c is never shown
        self.assertEqual(shown_pieces(units, 60)[1][1], "y" * 20)
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench, cap=BIG)
        self.process(project, job, "questions", build_calls=make_build_calls(page_chars=5), on_results=make_on_results(page_chars=5))
        with self.connect() as c:
            sources = c.execute("SELECT payload FROM gold_answers WHERE benchmark_id = %s AND kind = 'source'", (bench,)).fetchall()
        self.assertTrue(sources)
        for (payload,) in sources:
            self.assertIn(len(payload["evidence_sets"][0]["evidence_ids"]), (1, 2))    # one unit per page in the fixture, cut to 5 chars

    def test_a_unit_that_is_not_stored_skips_the_question_instead_of_raising(self):
        from unittest.mock import patch
        import lab_api.generate_questions as gq
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench, cap=BIG)
        real = gq.load_clean_pages

        def ghosts(conn, p, b):
            count, pages, doc = real(conn, p, b)
            return count, {k: [u.model_copy(update={"evidence_id": "ghost-" + u.evidence_id}) for u in v] for k, v in pages.items()}, doc
        with patch.object(gq, "load_clean_pages", ghosts):
            out = self.process(project, job, "questions", build_calls=make_build_calls(), on_results=make_on_results())
        self.assertEqual((out.status, out.reason), ("succeeded", "generated"))          # the finish was not rolled back
        self.assertEqual(self.count("questions", bench), 0)
        self.assertIn(f"invalid={general_target(3) + cross_page_target(3)}", self.audit_reason(project)[0][0])

    def test_cross_page_questions_span_two_pages_and_are_checked_by_the_contract_and_by_code(self):
        project, owner, bench, job, out = self.run_batch("questions")
        with self.connect() as c:
            rows = c.execute("SELECT q.id, q.payload, g.payload FROM questions q JOIN gold_answers g ON g.question_id = q.id AND g.kind = 'source' "
                             "WHERE q.benchmark_id = %s AND q.quota_bucket = 'cross_page'", (bench,)).fetchall()
            self.assertEqual(len(rows), cross_page_target(3))
            for qid, question, source in rows:
                self.assertEqual(question["reasoning_type"], "cross_page_integration")
                ids = source["evidence_sets"][0]["evidence_ids"]
                pages = {r[0] for r in c.execute("SELECT DISTINCT er.page_index FROM evidence_units eu JOIN evidence_regions er "
                                                  "ON er.evidence_unit_id = eu.id WHERE eu.evidence_key = ANY(%s)", (ids,))}
                self.assertEqual(len(pages), 2)                                       # the evidence set needs both pages
                supports = [fs["support_sets"][0] for fs in source["fact_support_sets"]]
                self.assertEqual(len({tuple(s) for s in supports}), 2)                # one fact per page, not both on one page

    def test_a_cross_page_question_whose_facts_are_not_on_the_claimed_pages_is_dropped(self):
        project, owner, bench, job, out = self.run_batch("questions_ungrounded")
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM questions WHERE benchmark_id = %s AND quota_bucket = 'cross_page'",
                                       (bench,)).fetchone()[0], 0)                    # code grounding of the page attribution failed
        self.assertIn(f"cross_written=0", self.audit_reason(project)[0][0])

    def test_a_document_with_one_clean_page_gets_no_cross_page_call(self):
        from lab_api.generate_prompts import select_page_pairs
        self.assertEqual(select_page_pairs([3], 2), [])
        self.assertEqual(select_page_pairs([], 2), [])

    def test_the_question_hash_ignores_case_spacing_and_unicode_width(self):
        self.assertEqual(question_hash("What  is\nthe deadline?"), question_hash("what is the DEADLINE?"))
        self.assertEqual(question_hash("ＡＢＣ"), question_hash("abc"))
        self.assertNotEqual(question_hash("a"), question_hash("b"))

    def test_gold_is_invisible_to_participants_cannot_be_written_by_them_and_is_append_only(self):
        project, owner, bench, job, out = self.run_batch("questions")
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
                return c.execute("SELECT count(*) FROM gold_answers WHERE benchmark_id = %s", (bench,)).fetchone()[0]
        n = 3 * (general_target(3) + cross_page_target(3))
        self.assertEqual((visible(owner), visible(reviewer), visible(participant)), (n, n, 0))
        with self.connect() as c:
            qid = c.execute("SELECT id FROM questions WHERE benchmark_id = %s LIMIT 1", (bench,)).fetchone()[0]
        for actor, allowed in ((participant, False), (reviewer, False), (owner, True)):
            with self.connect() as c:
                try:
                    with c.transaction():                                                   # roll the probe back either way
                        c.execute(f'SET LOCAL ROLE "{role}"')
                        c.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))
                        c.execute("INSERT INTO gold_answers (id, benchmark_id, question_id, kind, payload) "
                                  "VALUES (%s, %s, %s, 'source', '{}') ", (uuid.uuid4(), bench, qid))
                        raise RuntimeError("probe done")
                except RuntimeError:
                    self.assertTrue(allowed, f"{actor} was able to write gold")
                except errors.InsufficientPrivilege:                                        # RLS: new row violates the policy
                    self.assertFalse(allowed, f"{actor} could not write gold")
                except errors.UniqueViolation:                                              # the owner passed RLS; the row exists
                    self.assertTrue(allowed)
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):             # append-only
            c.execute("UPDATE gold_answers SET payload = '{}' WHERE benchmark_id = %s", (bench,))

    def test_the_polling_worker_picks_up_a_queued_generate_job_and_ignores_parse_jobs(self):
        from lab_api.generate_worker import GenModelConfig
        from lab_api.generate_worker_main import GenerateWorker
        from tests.api_db.test_generate_job_real_db import CFG, STUB
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench, cap=BIG)
        worker = GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="gen-test", interval=1,
                                backoff=1, grace=5, once_timeout=120, extra_env={"FAKE_LAB_GEN_MODE": "questions"})
        counts = worker.run_once()
        self.assertEqual(counts, {"succeeded:generated": 1})
        self.assertEqual(self.job_state(job), "succeeded")
        self.assertEqual(self.count("questions", bench), general_target(3) + cross_page_target(3))
        self.assertEqual(GenerateWorker(self.connect, lab_gen_cmd=STUB, cfg=CFG, api_key="k", name="again", interval=1,
                                        backoff=1, grace=5, once_timeout=30).run_once(), {})   # nothing left to claim

    def drop_role(self, role):
        with self.connect(autocommit=True) as c:
            c.execute(f'DROP OWNED BY "{role}"')
            c.execute(f'DROP ROLE IF EXISTS "{role}"')


if __name__ == "__main__":
    unittest.main()
