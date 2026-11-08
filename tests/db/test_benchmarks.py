"""0002: documents/evidence integrity and the benchmark state machine on a
real PostgreSQL."""
import json
import threading
import uuid

import psycopg
from psycopg import errors

from lab_db.repo import Change, FreezeIneligible, PermissionDenied, StaleVersion, record_change, transition_benchmark

from .base import FreshDatabase, h

HAPPY_PATH = ["parsing", "planning", "generating", "validating", "ready_to_freeze"]


class Fixture:
    """One project with a 3-page document, two evidence units, a second
    document, and a draft benchmark over the first document."""

    def __init__(self, test, conn):
        self.c = conn
        self.p, self.a = test.make_project(conn)
        self.doc = self.document(3, "doc_1")
        self.doc2 = self.document(2, "doc_2")
        self.e1 = self.evidence(self.doc, "e1", 0)
        self.e2 = self.evidence(self.doc, "e2", 2)
        self.e_other = self.evidence(self.doc2, "e9", 0)
        rv = record_change(conn, Change(self.p, self.a, "benchmark.create", "benchmark_revision", "bench_1", 1,
                                        h("bench"), "lab-c14n-1", "benchmark.created"))
        self.bench = uuid.uuid4()
        with conn.transaction():
            conn.execute("INSERT INTO benchmark_revisions (id, project_id, resource_id, document_version_id, n_target, q_min) "
                         "VALUES (%s, %s, %s, %s, 20, 2)", (self.bench, self.p, rv, self.doc))
        self.q = self.question("q_1", 0)

    def document(self, pages, logical):
        c = self.c
        art, dv = uuid.uuid4(), uuid.uuid4()
        rv = record_change(c, Change(self.p, self.a, "document.import", "document_version", logical, 1,
                                     h(logical), "raw-bytes", "document.imported"))
        with c.transaction():
            c.execute("INSERT INTO artifacts (id, project_id, object_key, object_version, sha256, size_bytes, mime, "
                      "retention, state, committed_at) VALUES (%s, %s, %s, 'v1', %s, 100, 'application/pdf', "
                      "'restricted', 'committed', now())", (art, self.p, f"pdf/{art}", h(logical)))
            c.execute("INSERT INTO document_versions (id, project_id, resource_id, artifact_id, sha256, page_count) "
                      "VALUES (%s, %s, %s, %s, %s, %s)", (dv, self.p, rv, art, h(logical), pages))
            for i in range(pages):
                c.execute("INSERT INTO pages VALUES (%s, %s, NULL, 0, 595, 842)", (dv, i))
        return dv

    def evidence(self, dv, key, page, kind="text"):
        eid = uuid.uuid4()
        with self.c.transaction():
            self.c.execute("INSERT INTO evidence_units (id, project_id, document_version_id, evidence_key, raw_content, "
                           "normalized_content, extractor_revision) VALUES (%s, %s, %s, %s, 'x', 'x', 'test@1')",
                           (eid, self.p, dv, key))
            self.c.execute("INSERT INTO evidence_regions VALUES (%s, 0, %s, %s, 0.1, 0.1, 0.5, 0.3, %s, 'x')",
                           (eid, dv, page, kind))
        return eid

    def question(self, key, ordinal, bucket="general"):
        qid = uuid.uuid4()
        with self.c.transaction():
            self.c.execute("INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, question_hash, "
                           "quota_bucket, payload) VALUES (%s, %s, %s, %s, 'Q?', %s, %s, %s)",
                           (qid, self.bench, key, ordinal, h(key), bucket, json.dumps({"question_id": key})))
        return qid

    def evidence_set(self, qid, key, members):
        sid = uuid.uuid4()
        with self.c.transaction():
            self.c.execute("INSERT INTO evidence_sets VALUES (%s, %s, %s, %s)", (sid, self.bench, qid, key))
            for m in members:
                self.c.execute("INSERT INTO evidence_set_members VALUES (%s, %s, %s)", (sid, self.bench, m))
        return sid

    def freeze(self):
        v = 1
        for s in HAPPY_PATH:
            v = transition_benchmark(self.c, self.p, self.bench, v, s, f"to {s}", self.a)
        return transition_benchmark(self.c, self.p, self.bench, v, "frozen", "freeze", self.a, compliance="partial")


class DocumentIntegrityTests(FreshDatabase):
    def test_page_and_region_bounds(self):
        with self.connect() as c:
            f = Fixture(self, c)
            with self.assertRaises(errors.CheckViolation):
                with c.transaction():
                    c.execute("INSERT INTO pages VALUES (%s, 3, NULL, 0, 595, 842)", (f.doc,))  # page_count = 3
            bad_regions = [
                ("INSERT INTO evidence_regions VALUES (%s, 1, %s, 0, 0.5, 0.1, 0.4, 0.3, 'text', 'x')", errors.CheckViolation),
                ("INSERT INTO evidence_regions VALUES (%s, 1, %s, 0, 0.1, 0.1, 0.5, 0.3, 'page', NULL)", errors.CheckViolation),
                ("INSERT INTO evidence_regions VALUES (%s, 1, %s, 7, 0.1, 0.1, 0.5, 0.3, 'text', 'x')", errors.ForeignKeyViolation),
            ]
            for stmt, exc in bad_regions:
                with self.subTest(stmt), self.assertRaises(exc):
                    with c.transaction():
                        c.execute(stmt, (f.e1, f.doc))

    def test_evidence_must_come_from_benchmark_document(self):
        with self.connect() as c:
            f = Fixture(self, c)
            f.evidence_set(f.q, "s1", [f.e1, f.e2])
            with self.assertRaises(errors.ForeignKeyViolation):
                f.evidence_set(f.q, "s2", [f.e1, f.e_other])
            with self.assertRaises(errors.ForeignKeyViolation):
                with c.transaction():
                    c.execute("INSERT INTO distractors VALUES (%s, %s, %s, 'r', 'c')", (f.q, f.bench, f.e_other))

    def test_member_benchmark_must_match_its_set(self):
        with self.connect() as c:
            f = Fixture(self, c)
            sid = f.evidence_set(f.q, "s1", [f.e1])
            with self.assertRaises(errors.ForeignKeyViolation):
                with c.transaction():
                    c.execute("INSERT INTO evidence_set_members VALUES (%s, %s, %s)", (sid, uuid.uuid4(), f.e2))

    def test_t15_duplicate_question_hash_rejected(self):
        with self.connect() as c:
            f = Fixture(self, c)
            with self.assertRaises(errors.UniqueViolation):
                with c.transaction():
                    c.execute("INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, "
                              "question_hash, quota_bucket, payload) VALUES (%s, %s, 'q_dup', 5, 'Q?', %s, "
                              "'general', '{}')", (uuid.uuid4(), f.bench, h("q_1")))


class StateMachineTests(FreshDatabase):
    def test_happy_path_and_transition_log(self):
        with self.connect() as c:
            f = Fixture(self, c)
            self.assertEqual(f.freeze(), 7)
            rows = c.execute("SELECT from_status::text, to_status::text, row_version FROM benchmark_transitions "
                             "WHERE benchmark_id = %s ORDER BY id", (f.bench,)).fetchall()
            self.assertEqual([r[1] for r in rows], HAPPY_PATH + ["frozen"])
            self.assertEqual(rows[0][:1], ("draft",))
            self.assertIsNotNone(c.execute("SELECT frozen_at FROM benchmark_revisions WHERE id = %s",
                                           (f.bench,)).fetchone()[0])
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("DELETE FROM benchmark_transitions")

    def test_illegal_transition_rejected(self):
        with self.connect() as c:
            f = Fixture(self, c)
            with self.assertRaises(errors.IntegrityConstraintViolation):
                transition_benchmark(c, f.p, f.bench, 1, "frozen", "skip everything", f.a, compliance="compliant")
            self.assertEqual(c.execute("SELECT status::text, row_version FROM benchmark_revisions WHERE id = %s",
                                       (f.bench,)).fetchone(), ("draft", 1))

    def test_freeze_as_compliant_fails_closed_ineligible(self):
        with self.connect() as c:
            f = Fixture(self, c)
            v = 1
            for s in HAPPY_PATH:
                v = transition_benchmark(c, f.p, f.bench, v, s, f"to {s}", f.a)
            with self.assertRaises(FreezeIneligible) as ctx:
                transition_benchmark(c, f.p, f.bench, v, "frozen", "freeze compliant", f.a, compliance="compliant")
            self.assertIn("cannot freeze as compliant", str(ctx.exception))
            # Verify fail-closed rollback: benchmark remains ready_to_freeze at row_version v and unfrozen
            st, ver, frozen_at = c.execute(
                "SELECT status::text, row_version, frozen_at FROM benchmark_revisions WHERE id = %s",
                (f.bench,)
            ).fetchone()
            self.assertEqual(st, "ready_to_freeze")
            self.assertEqual(ver, v)
            self.assertIsNone(frozen_at)

    def test_freeze_without_compliance_rejected(self):
        with self.connect() as c:
            f = Fixture(self, c)
            v = 1
            for s in HAPPY_PATH:
                v = transition_benchmark(c, f.p, f.bench, v, s, f"to {s}", f.a)
            with self.assertRaises(ValueError) as ctx:
                transition_benchmark(c, f.p, f.bench, v, "frozen", "freeze missing compliance", f.a, compliance=None)
            self.assertIn("compliance designation", str(ctx.exception))

    def test_freeze_partial_by_non_owner_rejected(self):
        with self.connect() as c:
            f = Fixture(self, c)
            maintainer = uuid.uuid4()
            with c.transaction():
                c.execute("INSERT INTO actors (id, display_name) VALUES (%s, 'maint')", (maintainer,))
                c.execute("INSERT INTO project_members (project_id, actor_id, role) VALUES (%s, %s, 'maintainer')",
                          (f.p, maintainer))
            v = 1
            for s in HAPPY_PATH:
                v = transition_benchmark(c, f.p, f.bench, v, s, f"to {s}", f.a)
            with self.assertRaises(PermissionDenied) as ctx:
                transition_benchmark(c, f.p, f.bench, v, "frozen", "maint partial freeze", maintainer, compliance="partial")
            self.assertIn("owner required", str(ctx.exception))

    def test_stale_row_version(self):
        with self.connect() as c:
            f = Fixture(self, c)
            transition_benchmark(c, f.p, f.bench, 1, "parsing", "start", f.a)
            with self.assertRaises(StaleVersion):
                transition_benchmark(c, f.p, f.bench, 1, "cancelled", "late writer", f.a)

    def test_concurrent_transitions_one_wins(self):
        with self.connect() as c:
            f = Fixture(self, c)
        outcomes, barrier = [], threading.Barrier(2)

        def worker(target):
            with psycopg.connect(self.url) as conn:
                barrier.wait()
                try:
                    transition_benchmark(conn, f.p, f.bench, 1, target, "race", f.a)
                    outcomes.append("ok")
                except StaleVersion:
                    outcomes.append("stale")

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("parsing", "cancelled")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(sorted(outcomes), ["ok", "stale"])

    def test_frozen_benchmark_and_children_are_immutable(self):
        with self.connect() as c:
            f = Fixture(self, c)
            sid = f.evidence_set(f.q, "s1", [f.e1])
            f.freeze()
            attempts = [
                ("UPDATE benchmark_revisions SET row_version = row_version + 1 WHERE id = %s", (f.bench,)),
                ("UPDATE questions SET question_text = 'changed' WHERE id = %s", (f.q,)),
                ("DELETE FROM evidence_set_members WHERE evidence_set_id = %s", (sid,)),
                ("INSERT INTO evidence_set_members VALUES (%s, %s, %s)", (sid, f.bench, f.e2)),
            ]
            for stmt, args in attempts:
                with self.subTest(stmt), self.assertRaises(errors.IntegrityConstraintViolation):
                    with c.transaction():
                        c.execute(stmt, args)
            with self.assertRaises(errors.IntegrityConstraintViolation):
                f.question("q_late", 9)
