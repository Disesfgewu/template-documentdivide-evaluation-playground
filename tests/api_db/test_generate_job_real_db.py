"""document.generate jobs and the llm_calls ledger on real PostgreSQL (P-D slice 2, Refs #7).

lab-gen is replaced by tests/fixtures/fake_lab_gen.py (no network, no model). The jobs, leases, budget math, authorization
and append-only guards are the real database's."""
from __future__ import annotations

import sys
import threading
import unittest
import uuid
from pathlib import Path

from psycopg import errors

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.generate_worker import CallSpec, GenModelConfig, process_generate_job
from lab_db.generation import (GenerationError, benchmark_budget, enqueue_generate_job, record_llm_calls,
                               request_sha256, reserve_batch)
from lab_db.repo import NotFound, PermissionDenied, transition_benchmark
from tests.api_db.test_promote_real_db import ADMIN_URL, FreshDatabase, PromoteFixtures

STUB = [sys.executable, str(_REPO / "tests" / "fixtures" / "fake_lab_gen.py")]
CFG = GenModelConfig(protocol="openai_chat", base_url="https://x.example-llm.com", allowed_host="x.example-llm.com",
                     input_micros_per_mtok=3_000_000, output_micros_per_mtok=15_000_000)
MODEL, CAP = "fake-model", 100_000


def build_calls(conn, project_id, benchmark_id):
    return [CallSpec("c0", "sys", "first prompt"), CallSpec("c1", "sys", "second prompt")]


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class GenerateJobRealDbTests(PromoteFixtures, FreshDatabase):
    def planning_benchmark(self):
        """A benchmark in 'planning' that pins a clean parse; returns (project, owner, bench)."""
        project, owner, doc, sha = self.seed()
        job = self.parsed(project, owner, doc, sha)
        bench = self.benchmark(project, owner, doc)
        self.promote(project, bench, job, owner)
        with self.connect() as c:
            for version, to in ((1, "parsing"), (2, "planning")):
                transition_benchmark(c, project, bench, version, to, "test", owner)
        return project, owner, bench

    def enqueue(self, project, owner, bench, cap=CAP, tokens=500):
        with self.connect() as c:
            return enqueue_generate_job(c, project_id=project, actor_id=owner, benchmark_id=bench, model=MODEL,
                                        max_output_tokens=tokens, budget_cap_micros=cap, currency="USD", pricing_revision="t1")

    def process(self, project, job, mode, **kw):
        seen = kw.pop("seen", None)

        def on_results(conn, p, b, rows):
            if seen is not None:
                seen.extend(rows)
        with self.connect() as c:
            return process_generate_job(c, project, job, worker="w1", lab_gen_cmd=STUB, cfg=CFG, build_calls=build_calls,
                                        connect=self.connect, api_key="k", on_results=on_results,
                                        extra_env={"FAKE_LAB_GEN_MODE": mode}, **kw)

    def calls(self, job):
        """The CURRENT state of each call (latest row per attempt and call), the ledger being append-only."""
        with self.connect() as c:
            return c.execute("SELECT call_id, status, usage_known, charged_micros, held_micros FROM ("
                             "SELECT DISTINCT ON (job_id, attempt, call_id) * FROM llm_calls WHERE job_id = %s "
                             "ORDER BY job_id, attempt, call_id, seq DESC) s ORDER BY call_id", (job,)).fetchall()

    def history(self, job):
        with self.connect() as c:
            return c.execute("SELECT call_id, attempt, status FROM llm_calls WHERE job_id = %s ORDER BY seq", (job,)).fetchall()

    def job_state(self, job):
        with self.connect() as c:
            return c.execute("SELECT status::text FROM jobs WHERE id = %s", (job,)).fetchone()[0]

    def bench_status(self, bench):
        with self.connect() as c:
            return c.execute("SELECT status::text FROM benchmark_revisions WHERE id = %s", (bench,)).fetchone()[0]

    def budget(self, project, bench):
        with self.connect() as c:
            return benchmark_budget(c, project, bench, CAP)

    # ---- enqueue ----------------------------------------------------------------------------------------------

    def test_enqueue_queues_a_job_and_moves_the_benchmark_to_generating(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        self.assertEqual((self.job_state(job), self.bench_status(bench)), ("queued", "generating"))
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM audit_events WHERE project_id = %s AND action = 'generate.requested'",
                                       (project,)).fetchone()[0], 1)
        with self.assertRaises(GenerationError):                              # no second job while it is generating
            self.enqueue(project, owner, bench)

    def test_enqueue_refusals_write_nothing(self):
        project, owner, bench = self.planning_benchmark()
        with self.connect() as c:
            participant = self.add_member(c, project, "participant")
            reviewer = self.add_member(c, project, "reviewer")
            outsider = uuid.uuid4()
            c.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'outsider')", (outsider,))
        for who, exc in ((participant, PermissionDenied), (reviewer, PermissionDenied), (outsider, NotFound)):
            with self.assertRaises(exc):
                self.enqueue(project, who, bench)
        for tokens in (501, 100000, 0, -1):
            with self.assertRaises(GenerationError):
                self.enqueue(project, owner, bench, tokens=tokens)
        with self.assertRaises(GenerationError):
            self.enqueue(project, owner, bench, cap=0)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM generate_jobs WHERE project_id = %s", (project,)).fetchone()[0], 0)
        self.assertEqual(self.bench_status(bench), "planning")

    def test_a_benchmark_that_is_not_planning_or_has_no_pin_takes_no_job(self):
        project, owner, doc, sha = self.seed()
        draft = self.benchmark(project, owner, doc)
        with self.assertRaises(GenerationError):
            self.enqueue(project, owner, draft)
        with self.connect() as c:
            for version, to in ((1, "parsing"), (2, "planning")):
                transition_benchmark(c, project, draft, version, to, "test", owner)
        with self.assertRaises(GenerationError):                              # planning but no pinned parse
            self.enqueue(project, owner, draft)

    # ---- processing -------------------------------------------------------------------------------------------

    def test_a_good_batch_succeeds_and_every_call_is_recorded_without_any_reply_text(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        seen = []
        out = self.process(project, job, "ok", seen=seen)
        self.assertEqual((out.status, out.reason), ("succeeded", "generated"))
        self.assertEqual(self.calls(job), [("c0", "ok", True, 315, 0), ("c1", "ok", True, 315, 0)])
        self.assertEqual([r["text"] for r in seen], ["reply 0", "reply 1"])    # the hook gets the text, once
        b = self.budget(project, bench)
        self.assertEqual((b.spent, b.held, b.remaining), (630, 0, CAP - 630))
        with self.connect() as c:
            row = c.execute("SELECT * FROM llm_calls WHERE job_id = %s LIMIT 1", (job,)).fetchone()
            self.assertFalse(any(isinstance(v, str) and ("reply" in v or "prompt" in v) for v in row))
            cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'llm_calls'")]
        self.assertFalse([k for k in cols if "text" in k or "prompt" in k or "key" in k])

    def test_unknown_usage_is_held_against_the_budget_never_counted_as_free(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        self.assertEqual(self.process(project, job, "unknown_usage").status, "succeeded")
        rows = self.calls(job)
        self.assertTrue(all(r[2] is False and r[3] == -1 and r[4] > 0 for r in rows), rows)
        b = self.budget(project, bench)
        self.assertEqual((b.spent, b.held), (0, sum(r[4] for r in rows)))
        self.assertLess(b.remaining, CAP)

    def test_a_partial_batch_fails_the_job_but_keeps_every_paid_row(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        seen = []
        out = self.process(project, job, "partial", seen=seen)
        self.assertEqual((out.status, out.reason), ("failed", "partial_calls"))
        self.assertEqual([r[:2] for r in self.calls(job)], [("c0", "ok"), ("c1", "failed")])
        self.assertEqual(seen, [])                                              # no results are consumed from a partial batch
        self.assertEqual(self.job_state(job), "failed")

    def test_a_child_that_crashes_times_out_or_prints_garbage_holds_the_whole_batch(self):
        for mode, kw in (("crash", {}), ("garbage", {}), ("sleep", {"child_timeout": 1.0}), ("overspend", {}), ("wrong_hash", {})):
            project, owner, bench = self.planning_benchmark()
            job = self.enqueue(project, owner, bench)
            out = self.process(project, job, mode, **kw)
            self.assertEqual((out.status, out.reason), ("failed", "lab_gen_failed"), mode)
            rows = self.calls(job)
            self.assertEqual([r[1] for r in rows], ["outcome_unknown"] * 2, mode)
            self.assertTrue(all(r[2] is False and r[3] == -1 and r[4] > 0 for r in rows), (mode, rows))
            self.assertGreater(self.budget(project, bench).held, 0, mode)

    def test_a_refused_batch_made_no_call_so_its_holds_are_released(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        out = self.process(project, job, "refused")
        self.assertEqual((out.status, out.reason), ("failed", "lab_gen_refused"))
        self.assertEqual([r[1:] for r in self.calls(job)], [("skipped_after_stop", False, -1, 0)] * 2)   # holds released
        self.assertEqual(self.budget(project, bench).held, 0)

    def hold(self, project, bench, job, attempt, call_id, amount, status="reserved"):
        with self.connect() as c:
            record_llm_calls(c, project, bench, job, attempt, MODEL, "USD", "t1", [{
                "call_id": call_id, "status": status, "reason": "", "usage_known": False, "input_tokens": 0,
                "output_tokens": 0, "charged_micros": -1, "held_micros": amount, "attempts": 0,
                "request_sha256": request_sha256(MODEL, "s", call_id), "response_length": 0, "response_sha256": None}])

    def test_an_exhausted_budget_spawns_nothing(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        self.hold(project, bench, job, 1, "earlier", CAP)                       # a previous holder used the whole budget
        out = self.process(project, job, "crash")                               # would leave outcome_unknown rows if spawned
        self.assertEqual((out.status, out.reason), ("failed", "budget_exhausted"))
        self.assertEqual([r[:2] for r in self.calls(job)], [("c0", "skipped_budget"), ("c1", "skipped_budget"), ("earlier", "reserved")])

    def test_the_budget_is_reserved_before_spawning_so_a_dead_worker_still_holds_it(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        batch = [("c0", request_sha256(MODEL, "s", "c0"), 40_000), ("c1", request_sha256(MODEL, "s", "c1"), 40_000)]
        with self.connect() as c:
            sendable, total = reserve_batch(c, project, bench, job, 1, CAP, MODEL, "USD", "t1", batch)
        self.assertEqual((sendable, total), (["c0", "c1"], 80_000))
        # ... the worker is SIGKILLed here: nothing else was written. The holds remain and the retry sees less budget.
        self.assertEqual(self.budget(project, bench).held, 80_000)
        with self.connect() as c:
            sendable, total = reserve_batch(c, project, bench, job, 2, CAP, MODEL, "USD", "t1", batch)
        self.assertEqual((sendable, total), ([], 0))                            # 20 000 left: neither 40 000 call fits
        self.assertEqual(self.budget(project, bench).held, 80_000)
        self.assertEqual([r for r in self.history(job) if r[1] == 2], [("c0", 2, "skipped_budget"), ("c1", 2, "skipped_budget")])

    def test_a_call_that_does_not_fit_stops_every_later_call_too(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        batch = [("a", "a" * 64, 60_000), ("b", "b" * 64, 60_000), ("c", "c" * 64, 1)]
        with self.connect() as c:
            sendable, total = reserve_batch(c, project, bench, job, 1, CAP, MODEL, "USD", "t1", batch)
        self.assertEqual((sendable, total), (["a"], 60_000))
        self.assertEqual([r[:2] for r in self.calls(job)], [("a", "reserved"), ("b", "skipped_budget"), ("c", "skipped_budget")])

    def test_two_workers_reserving_at_once_never_spend_the_same_budget(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        got, errors_seen = [], []

        def worker(attempt):
            try:
                batch = [(f"w{attempt}-{i}", f"{attempt}{i}".ljust(64, "0"), 30_000) for i in range(3)]
                with self.connect() as c:
                    got.append(reserve_batch(c, project, bench, job, attempt, CAP, MODEL, "USD", "t1", batch))
            except Exception as exc:                                              # pragma: no cover - reported below
                errors_seen.append(exc)
        threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors_seen, [])
        self.assertLessEqual(sum(total for _, total in got), CAP)               # 3 x 30 000 fit once, never twice
        self.assertEqual(sum(len(s) for s, _ in got), 3)
        self.assertLessEqual(self.budget(project, bench).held, CAP)

    def test_a_retry_of_a_call_an_earlier_attempt_skipped_is_recorded_not_dropped(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        self.hold(project, bench, job, 1, "c0", 0, status="skipped_budget")
        with self.connect() as c:
            n = record_llm_calls(c, project, bench, job, 2, MODEL, "USD", "t1", [{
                "call_id": "c0", "status": "ok", "reason": "", "usage_known": True, "input_tokens": 5, "output_tokens": 20,
                "charged_micros": 315, "held_micros": 0, "attempts": 1, "request_sha256": "a" * 64,
                "response_length": 3, "response_sha256": "b" * 64}])
        self.assertEqual(n, 1)                                                  # not silently dropped
        self.assertEqual(self.budget(project, bench).spent, 315)
        self.hold(project, bench, job, 1, "c0", 0, status="skipped_budget")     # a replay of the SAME attempt is a no-op
        self.assertEqual(len(self.history(job)), 2)

    # ---- database guards --------------------------------------------------------------------------------------

    def test_the_ledger_is_append_only_and_checks_the_cost_invariants(self):
        project, owner, bench = self.planning_benchmark()
        job = self.enqueue(project, owner, bench)
        self.process(project, job, "ok")
        sha = "a" * 64
        bad = [
            ("unknown usage as zero", dict(usage_known=False, charged_micros=0, held_micros=0, status="ok")),
            ("unknown usage not held", dict(usage_known=False, charged_micros=-1, held_micros=0, status="ok")),
            ("known usage with a hold", dict(usage_known=True, charged_micros=5, held_micros=5, status="ok")),
            ("above the token cap", dict(usage_known=True, charged_micros=5, held_micros=0, status="ok", output_tokens=501)),
        ]
        base = {"reason": "", "usage_known": True, "input_tokens": 1, "output_tokens": 1, "charged_micros": 5, "held_micros": 0,
                "attempts": 1, "request_sha256": sha, "response_length": 0, "response_sha256": None}
        for name, change in bad:
            row = {**base, **change, "call_id": "x-" + name[:5]}
            if name == "above the token cap":
                row["status"] = "ok"
            with self.connect() as c, self.assertRaises(errors.CheckViolation, msg=name):
                record_llm_calls(c, project, bench, job, 9, MODEL, "USD", "t1", [row])
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):
            c.execute("UPDATE llm_calls SET status = 'failed' WHERE job_id = %s", (job,))
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):
            c.execute("DELETE FROM llm_calls WHERE job_id = %s", (job,))
        with self.connect() as c, self.assertRaises(errors.IntegrityConstraintViolation):
            c.execute("UPDATE generate_jobs SET max_output_tokens = 501 WHERE job_id = %s", (job,))  # append-only guard or the 500 cap


if __name__ == "__main__":
    unittest.main()
