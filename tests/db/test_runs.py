"""0003: runs, submissions and scores on a real PostgreSQL."""
import uuid

from psycopg import errors

from lab_db.repo import Change, record_change

from .base import FreshDatabase, h
from .test_benchmarks import Fixture


class RunFixture(Fixture):
    def __init__(self, test, conn, freeze=True):
        super().__init__(test, conn)
        c = conn
        self.fact_strict, self.fact_partial = uuid.uuid4(), uuid.uuid4()
        with c.transaction():
            c.execute("INSERT INTO facts VALUES (%s, %s, %s, 'f1', 'fact one', 1, NULL)",
                      (self.fact_strict, self.bench, self.q))
            c.execute("INSERT INTO facts VALUES (%s, %s, %s, 'f2', 'fact two', 1, 'half if unit missing')",
                      (self.fact_partial, self.bench, self.q))
        self.q2 = self.question("q_2", 1)
        if freeze:
            self.freeze()
        rv = record_change(c, Change(self.p, self.a, "method.register", "method_revision", "m_1", 1,
                                     h("m1"), "lab-c14n-1", "method.registered"))
        self.method = uuid.uuid4()
        with c.transaction():
            c.execute("INSERT INTO method_revisions VALUES (%s, %s, %s, %s, '1.0', %s, 'jsonl', 'declared_only')",
                      (self.method, self.p, rv, self.a, h("cfg")))

    def run(self):
        rid = uuid.uuid4()
        with self.c.transaction():
            self.c.execute("INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile) "
                           "VALUES (%s, %s, %s, %s, 'default')", (rid, self.p, self.bench, self.method))
        return rid

    def attempt(self, run, question, n=1):
        aid = uuid.uuid4()
        with self.c.transaction():
            self.c.execute("INSERT INTO attempts (id, run_id, benchmark_id, question_id, attempt_number, transport) "
                           "VALUES (%s, %s, %s, %s, %s, 'jsonl')", (aid, run, self.bench, question, n))
        return aid

    def submission(self, run, attempt, question, status="accepted", answer="answered"):
        sid, art = uuid.uuid4(), uuid.uuid4()
        with self.c.transaction():
            self.c.execute("INSERT INTO artifacts (id, project_id, object_key, object_version, sha256, size_bytes, "
                           "mime, retention, state, committed_at) VALUES (%s, %s, %s, 'v1', %s, 10, "
                           "'application/json', 'restricted', 'committed', now())", (art, self.p, f"sub/{sid}", h(str(sid))))
            self.c.execute("INSERT INTO submissions (id, run_id, attempt_id, question_id, status, answer_status, "
                           "payload_hash, c14n_version, payload_artifact_id) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                           "'lab-c14n-1', %s)", (sid, run, attempt, question, status,
                                                 answer if status == "accepted" else None, h(str(sid)), art))
        return sid

    def evaluation(self, run):
        eid = uuid.uuid4()
        with self.c.transaction():
            self.c.execute("INSERT INTO evaluations VALUES (%s, %s, %s, 'grader@1', 'provisional', now())",
                           (eid, run, self.bench))
        return eid

    def score(self, ev, run, question, submission, status="scored", raw=75.0, num=1.5, den=2.0, reasons=()):
        with self.c.transaction():
            self.c.execute("INSERT INTO question_scores VALUES (%s, %s, %s, %s, %s, 'correctness', %s, %s, %s, %s, %s)",
                           (ev, run, self.bench, question, submission, status, raw, num, den, list(reasons)))


class RunTests(FreshDatabase):
    def test_run_requires_frozen_benchmark(self):
        with self.connect() as c:
            f = RunFixture(self, c, freeze=False)
            with self.assertRaises(errors.CheckViolation):
                f.run()

    def test_attempt_and_submission_integrity(self):
        with self.connect() as c:
            f = RunFixture(self, c)
            run = f.run()
            a1 = f.attempt(run, f.q)
            with self.assertRaises(errors.UniqueViolation):
                f.attempt(run, f.q, 1)
            with self.assertRaises(errors.ForeignKeyViolation):
                f.attempt(run, uuid.uuid4())  # not a question of this benchmark
            with self.assertRaises(errors.ForeignKeyViolation):
                f.submission(run, a1, f.q2)  # question differs from the attempt's
            s1 = f.submission(run, a1, f.q)
            a2 = f.attempt(run, f.q, 2)
            with self.assertRaises(errors.UniqueViolation):
                f.submission(run, a2, f.q)  # first accepted answer per question is final
            f.submission(run, a2, f.q, status="late")  # kept, does not replace
            for stmt in ("UPDATE submissions SET payload_hash = %s WHERE id = %s",
                         "DELETE FROM submissions WHERE id = %s AND payload_hash <> %s"):
                with self.subTest(stmt), self.assertRaises(errors.IntegrityConstraintViolation):
                    with c.transaction():
                        args = (h("x"), s1) if stmt.startswith("UPDATE") else (s1, h("x"))
                        c.execute(stmt, args)

    def test_hits_and_citations(self):
        with self.connect() as c:
            f = RunFixture(self, c)
            run = f.run()
            s = f.submission(run, f.attempt(run, f.q), f.q)
            with c.transaction():
                c.execute("INSERT INTO retrieved_hits VALUES (%s, 'h1', 1, 'x'), (%s, 'h2', 2, '')", (s, s))
                c.execute("INSERT INTO citations VALUES (%s, 'c1', 'h1', '{0}')", (s,))
            with self.assertRaises(errors.UniqueViolation):
                with c.transaction():
                    c.execute("INSERT INTO retrieved_hits VALUES (%s, 'h3', 2, 'dup rank')", (s,))
            with self.assertRaises(errors.ForeignKeyViolation):
                with c.transaction():
                    c.execute("INSERT INTO citations VALUES (%s, 'c2', 'h9', '{0}')", (s,))


class ScoreTests(FreshDatabase):
    def setUp(self):
        self.c = self.connect()
        self.addCleanup(self.c.close)
        f = self.f = RunFixture(self, self.c)
        self.run = f.run()
        self.sub = f.submission(self.run, f.attempt(self.run, f.q), f.q)
        self.ev = f.evaluation(self.run)

    def test_score_row_consistency(self):
        f, ev, run = self.f, self.ev, self.run
        f.score(ev, run, f.q, self.sub)  # 75 = 100 * 1.5 / 2
        bad = [dict(raw=80.0), dict(den=0.0, raw=0.0, num=0.0),
               dict(status="pending", raw=0.0, reasons=["judge_down"]),
               dict(status="pending", raw=None, num=0.0, den=2.0, reasons=["judge_down"]),
               dict(status="not_applicable", raw=None, num=None, den=None)]
        for kw in bad:
            with self.subTest(kw), self.assertRaises(errors.CheckViolation):
                f.score(ev, run, f.q2, None, **kw)
        f.score(ev, run, f.q2, None, status="error", raw=None, num=None, den=None, reasons=["no_valid_output"])

    def test_score_must_use_the_accepted_submission_of_the_question(self):
        f = self.f
        other_run = f.run()
        foreign = f.submission(other_run, f.attempt(other_run, f.q), f.q)
        with self.assertRaises(errors.ForeignKeyViolation):
            f.score(self.ev, self.run, f.q, foreign)  # another run's submission
        a2 = f.attempt(self.run, f.q2)
        rejected = f.submission(self.run, a2, f.q2, status="rejected")
        with self.assertRaises(errors.ForeignKeyViolation):
            f.score(self.ev, self.run, f.q2, rejected)
        with self.assertRaises(errors.ForeignKeyViolation):
            f.score(self.ev, self.run, f.q2, self.sub)  # q's submission used for q2

    def test_t38_partial_credit_needs_rule(self):
        f, c = self.f, self.c
        ins = "INSERT INTO fact_scores VALUES (%s, %s, %s, %s, %s, %s, 'scored', 'r')"
        with self.assertRaises(errors.CheckViolation):
            with c.transaction():
                c.execute(ins, (self.ev, self.run, f.bench, f.q, f.fact_strict, 0.5))
        with c.transaction():
            c.execute(ins, (self.ev, self.run, f.bench, f.q, f.fact_partial, 0.5))
            c.execute(ins, (self.ev, self.run, f.bench, f.q, f.fact_strict, 1))

    def test_pending_blocks_publish_and_published_is_final(self):
        f, c = self.f, self.c
        f.score(self.ev, self.run, f.q, self.sub)
        f.score(self.ev, self.run, f.q2, None, status="pending", raw=None, num=None, den=None, reasons=["judge_down"])
        with self.assertRaises(errors.CheckViolation):
            with c.transaction():
                c.execute("UPDATE evaluations SET status = 'published' WHERE id = %s", (self.ev,))
        with c.transaction():
            c.execute("UPDATE question_scores SET status = 'error' WHERE evaluation_id = %s AND question_id = %s",
                      (self.ev, f.q2))
            c.execute("UPDATE evaluations SET status = 'published' WHERE id = %s", (self.ev,))
        for stmt, args in (("UPDATE question_scores SET reason_codes = '{x}' WHERE evaluation_id = %s", (self.ev,)),
                           ("DELETE FROM question_scores WHERE evaluation_id = %s", (self.ev,)),
                           ("UPDATE evaluations SET grader_revision = 'g2' WHERE id = %s", (self.ev,))):
            with self.subTest(stmt), self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute(stmt, args)
