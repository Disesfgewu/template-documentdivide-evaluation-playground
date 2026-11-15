"""T48 idempotency under concurrency, T49 atomic business+audit+outbox,
role matrix and application-role privileges on a real PostgreSQL."""
import threading
import uuid

import psycopg
from psycopg import errors, sql

from lab_db.repo import (ROLE_MATRIX, ROLES, Change, IdempotencyConflict, NotFound, PermissionDenied,
                         authorize, claim_or_replay, grant_app_privileges, record_change)

from .base import FreshDatabase, h


def change(p, a, **kw):
    base = dict(project_id=p, actor_id=a, operation_kind="benchmark.create", resource_type="benchmark_revision",
                logical_id="bench_1", revision=1, content_hash=h("b1"), c14n_version="lab-c14n-1",
                audit_action="benchmark.created", outbox_topic="benchmark.created", outbox_payload={"n": 1})
    base.update(kw)
    return Change(**base)


def counts(c, p):
    return {t: c.execute(f"SELECT count(*) FROM {t} WHERE project_id = %s", (p,)).fetchone()[0]
            for t in ("resource_versions", "audit_events", "outbox_events", "operations")}


class AtomicChangeTests(FreshDatabase):
    def test_success_writes_all_parts(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            record_change(c, change(p, a))
            self.assertEqual(counts(c, p), {"resource_versions": 1, "audit_events": 1,
                                            "outbox_events": 1, "operations": 1})

    def test_t49_audit_failure_rolls_back_business_change(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            with self.assertRaises(errors.CheckViolation):
                record_change(c, change(p, a, audit_action="Not A Valid Action"))
            self.assertEqual(counts(c, p), {"resource_versions": 0, "audit_events": 0,
                                            "outbox_events": 0, "operations": 0})

    def test_t49_outbox_failure_rolls_back_and_retry_succeeds(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            with self.assertRaises(errors.CheckViolation):
                record_change(c, change(p, a, outbox_topic="BAD TOPIC"))
            self.assertEqual(counts(c, p)["resource_versions"], 0)
            record_change(c, change(p, a))  # the same change can be re-sent after recovery
            self.assertEqual(counts(c, p)["outbox_events"], 1)


class IdempotencyTests(FreshDatabase):
    def test_t48_twenty_concurrent_same_key(self):
        with self.connect() as c:
            p, a = self.make_project(c)
        results, failures = [], []
        barrier = threading.Barrier(20)

        def worker():
            try:
                with psycopg.connect(self.url) as conn:
                    barrier.wait()
                    results.append(claim_or_replay(conn, p, a, "run.create", "key-1", h("body"), "run.create"))
            except Exception as exc:  # surfaced below
                failures.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 20)
        self.assertEqual(sum(r.created for r in results), 1)
        self.assertEqual(len({r.operation_id for r in results}), 1)
        with self.connect() as c:
            # Losers' provisional operation rows were rolled back: one logical operation.
            self.assertEqual(c.execute("SELECT count(*) FROM operations WHERE project_id = %s",
                                       (p,)).fetchone()[0], 1)

    def test_t48_same_key_different_body_conflicts(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            first = claim_or_replay(c, p, a, "run.create", "key-2", h("body-a"), "run.create")
            self.assertTrue(first.created)
            with self.assertRaises(IdempotencyConflict):
                claim_or_replay(c, p, a, "run.create", "key-2", h("body-b"), "run.create")
            again = claim_or_replay(c, p, a, "run.create", "key-2", h("body-a"), "run.create")
            self.assertEqual((again.created, again.operation_id), (False, first.operation_id))


class RoleMatrixTests(FreshDatabase):
    # SPEC 16.4 table, transcribed independently of ROLE_MATRIX:
    # Y = allowed, N = denied, C = allowed only under a condition.
    SPEC = {
        "import_and_configure_parse":             "YYNNN",
        "read_licensed_pdf_and_public_questions": "YYYYC",
        "build_edit_freeze":                      "YYCNN",
        "read_gold_and_generation_context":       "YYYNN",
        "register_method_and_submit":             "YYNCN",
        "read_run_results":                       "YYYCC",
        "read_gold_trace_and_lineage":            "YYYNN",
        "read_run_trace":                         "YYYCN",
        "delete_or_change_membership":            "YNNNN",
    }

    def test_matrix_matches_spec_on_real_membership(self):
        self.assertEqual(set(self.SPEC), set(ROLE_MATRIX))
        with self.connect() as c:
            p, _ = self.make_project(c)
            members = {role: self.add_member(c, p, role) for role in ROLES}
            for action, row in self.SPEC.items():
                for role, mark in zip(ROLES, row):
                    with self.subTest(action=action, role=role):
                        if mark == "N":
                            with self.assertRaises(PermissionDenied):
                                authorize(c, p, members[role], action)
                        else:
                            d = authorize(c, p, members[role], action)
                            self.assertEqual(d.condition is None, mark == "Y")

    def test_non_member_gets_not_found(self):
        with self.connect() as c:
            p1, _ = self.make_project(c)
            _, outsider = self.make_project(c)
            with self.assertRaises(NotFound):
                authorize(c, p1, outsider, "read_run_results")


class AppRoleTests(FreshDatabase):
    def test_app_role_has_dml_only(self):
        role = f"lab_app_{uuid.uuid4().hex[:8]}"
        with self.connect(autocommit=True) as c:
            c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            self.addCleanup(self._drop_role, role)
        with self.connect() as c:
            grant_app_privileges(c, role)
            p, a = self.make_project(c)
            with c.transaction():
                c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                record_change(c, change(p, a))  # normal work succeeds
            denied = ["DROP TABLE audit_events", "ALTER TABLE resource_versions DISABLE TRIGGER ALL",
                      "TRUNCATE outbox_events", "DELETE FROM audit_events", "UPDATE audit_events SET reason = 'x'"]
            for stmt in denied:
                with self.subTest(stmt), self.assertRaises(psycopg.Error):
                    with c.transaction():
                        c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                        c.execute(stmt)

    def _drop_role(self, role):
        with self.connect(autocommit=True) as c:
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
