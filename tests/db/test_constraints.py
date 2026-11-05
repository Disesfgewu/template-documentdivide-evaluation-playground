"""T47 and immutability/append-only guards on a real PostgreSQL."""
import uuid

import psycopg
from psycopg import errors

from lab_db.repo import Change, record_change

from .base import FreshDatabase, h


def change(p, a, logical="bench_1", revision=1, **kw):
    return Change(project_id=p, actor_id=a, operation_kind="benchmark.create", resource_type="benchmark_revision",
                  logical_id=logical, revision=revision, content_hash=h(f"{logical}{revision}"),
                  c14n_version="lab-c14n-1", audit_action="benchmark.created", **kw)


class ProjectIsolationTests(FreshDatabase):
    def test_t47_cross_project_lineage_rejected(self):
        with self.connect() as c:
            p1, a1 = self.make_project(c)
            p2, a2 = self.make_project(c)
            up = record_change(c, change(p1, a1, "doc_1"))
            with self.assertRaises(errors.ForeignKeyViolation):
                record_change(c, change(p2, a2, "bench_x", lineage=[(up, "derived_from")]))
            # Nothing half-written in p2.
            self.assertEqual(c.execute("SELECT count(*) FROM resource_versions WHERE project_id = %s",
                                       (p2,)).fetchone()[0], 0)

    def test_t47_duplicate_revision_rejected(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            record_change(c, change(p, a, revision=1))
            with self.assertRaises(errors.UniqueViolation):
                record_change(c, change(p, a, revision=1))
            record_change(c, change(p, a, revision=2))

    def test_t47_missing_upstream_rejected_no_orphans(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            with self.assertRaises(errors.ForeignKeyViolation):
                record_change(c, change(p, a, lineage=[(uuid.uuid4(), "derived_from")]))
            for t in ("resource_versions", "operations", "audit_events", "lineage_edges"):
                self.assertEqual(c.execute(f"SELECT count(*) FROM {t} WHERE project_id = %s", (p,)).fetchone()[0],
                                 0, t)

    def test_referenced_resource_cannot_be_deleted(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            rv = record_change(c, change(p, a))
            with self.assertRaises(psycopg.Error):
                with c.transaction():
                    c.execute("DELETE FROM resource_versions WHERE id = %s", (rv,))
            with self.assertRaises(errors.ForeignKeyViolation):
                with c.transaction():
                    c.execute("DELETE FROM projects WHERE id = %s", (p,))


class ImmutabilityTests(FreshDatabase):
    def test_frozen_version_is_immutable(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            rv = record_change(c, change(p, a))
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("UPDATE resource_versions SET content_hash = %s WHERE id = %s", (h("x"), rv))
            with c.transaction():
                c.execute("UPDATE resource_versions SET frozen = true WHERE id = %s", (rv,))
            for stmt in ("UPDATE resource_versions SET frozen = false WHERE id = %s",
                         "UPDATE resource_versions SET c14n_version = 'v2' WHERE id = %s"):
                with self.subTest(stmt), self.assertRaises(errors.IntegrityConstraintViolation):
                    with c.transaction():
                        c.execute(stmt, (rv,))

    def test_audit_is_append_only(self):
        with self.connect() as c:
            p, a = self.make_project(c)
            record_change(c, change(p, a))
            for stmt in ("UPDATE audit_events SET reason = 'edited'", "DELETE FROM audit_events",
                         "TRUNCATE audit_events"):
                with self.subTest(stmt), self.assertRaises(errors.IntegrityConstraintViolation):
                    with c.transaction():
                        c.execute(stmt)
            self.assertEqual(c.execute("SELECT count(*) FROM audit_events").fetchone()[0], 1)

    def test_artifact_lifecycle(self):
        with self.connect() as c:
            p, _ = self.make_project(c)

            def new(state_sql=""):
                aid = uuid.uuid4()
                with c.transaction():
                    c.execute("INSERT INTO artifacts (id, project_id, object_key, object_version, sha256, size_bytes, "
                              "mime, retention) VALUES (%s, %s, %s, 'v1', %s, 10, 'application/pdf', 'standard')",
                              (aid, p, f"k/{aid}", h(str(aid))))
                return aid

            staged = new()
            with c.transaction():
                c.execute("DELETE FROM artifacts WHERE id = %s", (staged,))  # staged clean-up allowed
            a1 = new()
            with c.transaction():
                c.execute("UPDATE artifacts SET state = 'committed', committed_at = now() WHERE id = %s", (a1,))
            bad = [("UPDATE artifacts SET sha256 = %s WHERE id = %s", (h("y"), a1)),
                   ("DELETE FROM artifacts WHERE id = %s", (a1,)),
                   ("UPDATE artifacts SET state = 'staged', committed_at = NULL WHERE id = %s", (a1,)),
                   ("UPDATE artifacts SET state = 'tombstoned', tombstoned_at = now() WHERE id = %s", (a1,))]
            for stmt, args in bad:  # the last one lacks tombstone_reason
                with self.subTest(stmt), self.assertRaises(psycopg.Error):
                    with c.transaction():
                        c.execute(stmt, args)
            with c.transaction():
                c.execute("UPDATE artifacts SET state = 'tombstoned', tombstoned_at = now(), "
                          "tombstone_reason = 'retention expired' WHERE id = %s", (a1,))
            with self.assertRaises(errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute("UPDATE artifacts SET tombstone_reason = 'changed' WHERE id = %s", (a1,))
