"""Real PostgreSQL tests for 0007_integrity_hardening.sql."""
from datetime import datetime, timedelta, timezone
import unittest
import uuid

import psycopg
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    IntegrityConstraintViolation,
    UniqueViolation,
)

from tests.db.base import FreshDatabase, h


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TestIntegrityHardening(FreshDatabase):
    """Covers items 1-11 of 0007_integrity_hardening.sql with positive and negative cases."""

    # ------------------------------------------------------------------
    # Shared fixture helpers
    # ------------------------------------------------------------------

    def make_resource(self, conn, project_id, actor_id, resource_type="doc", logical_id=None, revision=1):
        rid = uuid.uuid4()
        logical_id = logical_id or f"res-{rid.hex[:8]}"
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO resource_versions
                    (id, project_id, resource_type, logical_id, revision, content_hash, c14n_version, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, 'v1', %s)
                """,
                (rid, project_id, resource_type, logical_id, revision, h(rid.hex), actor_id),
            )
        return rid

    def make_operation(self, conn, project_id, actor_id, kind="test.op", status="intent"):
        op_id = uuid.uuid4()
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO operations (id, project_id, kind, actor_id, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (op_id, project_id, kind, actor_id, status),
            )
        return op_id

    def make_artifact(self, conn, project_id, state="committed", retention="standard"):
        art_id = uuid.uuid4()
        committed_at = utcnow() if state != "staged" else None
        tombstoned_at = utcnow() if state == "tombstoned" else None
        tombstone_reason = "retired" if state == "tombstoned" else None
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, project_id, object_key, object_version, sha256, size_bytes, mime,
                     state, retention, committed_at, tombstoned_at, tombstone_reason)
                VALUES (%s, %s, %s, 'v1', %s, 128, 'application/pdf', %s, %s, %s, %s, %s)
                """,
                (
                    art_id,
                    project_id,
                    f"obj/{art_id}",
                    h(art_id.hex),
                    state,
                    retention,
                    committed_at,
                    tombstoned_at,
                    tombstone_reason,
                ),
            )
        return art_id

    def make_document(self, conn, project_id, actor_id, page_count=2):
        res_id = self.make_resource(conn, project_id, actor_id, "document")
        art_id = self.make_artifact(conn, project_id, state="committed")
        doc_id = uuid.uuid4()
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO document_versions (id, project_id, resource_id, artifact_id, sha256, page_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (doc_id, project_id, res_id, art_id, h(doc_id.hex), page_count),
            )
            for idx in range(page_count):
                conn.execute(
                    """
                    INSERT INTO pages (document_version_id, page_index, page_label, rotation, width_pt, height_pt)
                    VALUES (%s, %s, %s, 0, 612, 792)
                    """,
                    (doc_id, idx, str(idx + 1)),
                )
        return doc_id

    def make_evidence_unit(self, conn, project_id, doc_id, key=None):
        eu_id = uuid.uuid4()
        key = key or f"eu-{eu_id.hex[:8]}"
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO evidence_units
                    (id, project_id, document_version_id, evidence_key, raw_content, normalized_content, extractor_revision)
                VALUES (%s, %s, %s, %s, 'raw', 'norm', 'ext-1')
                """,
                (eu_id, project_id, doc_id, key),
            )
        return eu_id

    def make_benchmark_with_question(self, conn, project_id, actor_id, doc_id, freeze=True):
        res_id = self.make_resource(conn, project_id, actor_id, "benchmark")
        bm_id = uuid.uuid4()
        q_id = uuid.uuid4()
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO benchmark_revisions
                    (id, project_id, resource_id, document_version_id, n_target, q_min)
                VALUES (%s, %s, %s, %s, 20, 2)
                """,
                (bm_id, project_id, res_id, doc_id),
            )
            conn.execute(
                """
                INSERT INTO questions
                    (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, payload)
                VALUES (%s, %s, 'q1', 0, 'What is X?', %s, 'general', '{}'::jsonb)
                """,
                (q_id, bm_id, h(f"q-{q_id.hex}")),
            )
            if freeze:
                conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
                conn.execute("SELECT set_config('lab.transition_reason', 'advance', true)")
                steps = ["parsing", "planning", "generating", "validating", "ready_to_freeze"]
                rv = 1
                for st in steps:
                    rv += 1
                    conn.execute(
                        "UPDATE benchmark_revisions SET status = %s, row_version = %s WHERE id = %s",
                        (st, rv, bm_id),
                    )
                rv += 1
                conn.execute(
                    """
                    UPDATE benchmark_revisions
                    SET status = 'frozen', compliance = 'compliant', frozen_at = now(), row_version = %s
                    WHERE id = %s
                    """,
                    (rv, bm_id),
                )
        return bm_id, q_id, res_id

    def make_method_and_run(self, conn, project_id, actor_id, bm_id):
        m_res = self.make_resource(conn, project_id, actor_id, "method")
        method_id = uuid.uuid4()
        run_id = uuid.uuid4()
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO method_revisions
                    (id, project_id, resource_id, owner_actor_id, protocol_version, config_hash, execution_mode, visibility)
                VALUES (%s, %s, %s, %s, '1.0', %s, 'http', 'lab_observed')
                """,
                (method_id, project_id, m_res, actor_id, h(method_id.hex)),
            )
            conn.execute(
                """
                INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile)
                VALUES (%s, %s, %s, %s, 'standard')
                """,
                (run_id, project_id, bm_id, method_id),
            )
        return method_id, run_id

    # ------------------------------------------------------------------
    # Item 1: Append-only tables & state/identity immutability
    # ------------------------------------------------------------------

    def test_01_append_only_tables(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            r1 = self.make_resource(conn, p, a, "doc")
            r2 = self.make_resource(conn, p, a, "doc")
            op_id = self.make_operation(conn, p, a)
            doc_id = self.make_document(conn, p, a, page_count=2)
            eu_id = self.make_evidence_unit(conn, p, doc_id)
            bm_id, q_id, _ = self.make_benchmark_with_question(conn, p, a, doc_id, freeze=True)
            method_id, run_id = self.make_method_and_run(conn, p, a, bm_id)

            # Insert valid rows into each of the 10 newly append-only tables
            attempt_id = uuid.uuid4()
            deriv_id = uuid.uuid4()
            deriv_in_id = uuid.uuid4()
            outbox_event_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO lineage_edges (project_id, upstream_id, downstream_id, relation, operation_id)
                    VALUES (%s, %s, %s, 'derived_from', %s)
                    """,
                    (p, r1, r2, op_id),
                )
                conn.execute(
                    """
                    INSERT INTO idempotency_keys (project_id, idempotency_scope, idempotency_key, request_hash, operation_id)
                    VALUES (%s, 'scope.a', 'k1', %s, %s)
                    """,
                    (p, h("req1"), op_id),
                )
                conn.execute(
                    """
                    INSERT INTO evidence_regions
                        (evidence_unit_id, ordinal, document_version_id, page_index, x0, y0, x1, y1, kind)
                    VALUES (%s, 0, %s, 0, 0.1, 0.1, 0.5, 0.5, 'text')
                    """,
                    (eu_id, doc_id),
                )
                conn.execute(
                    """
                    INSERT INTO attempts (id, run_id, benchmark_id, question_id, attempt_number, transport)
                    VALUES (%s, %s, %s, %s, 1, 'http')
                    """,
                    (attempt_id, run_id, bm_id, q_id),
                )
                conn.execute(
                    """
                    INSERT INTO derivations (id, project_id, kind, operation_id)
                    VALUES (%s, %s, 'summary', %s)
                    """,
                    (deriv_id, p, op_id),
                )
                conn.execute(
                    """
                    INSERT INTO derivation_inputs (id, project_id, derivation_id, evidence_unit_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (deriv_in_id, p, deriv_id, eu_id),
                )
                conn.execute(
                    """
                    INSERT INTO outbox_events (event_id, project_id, topic, payload, operation_id)
                    VALUES (%s, %s, 'topic.a', '{}'::jsonb, %s)
                    """,
                    (outbox_event_id, p, op_id),
                )
                conn.execute(
                    """
                    INSERT INTO outbox_consumptions (consumer, event_id)
                    VALUES ('worker-1', %s)
                    """,
                    (outbox_event_id,),
                )

            # Negative: UPDATE and DELETE must raise IntegrityConstraintViolation on all 10 tables
            mutations = [
                ("UPDATE lineage_edges SET relation = 'supersedes' WHERE operation_id = %s", (op_id,)),
                ("DELETE FROM lineage_edges WHERE operation_id = %s", (op_id,)),
                ("UPDATE idempotency_keys SET request_hash = %s WHERE project_id = %s", (h("x"), p)),
                ("DELETE FROM idempotency_keys WHERE project_id = %s", (p,)),
                ("UPDATE document_versions SET page_count = 3 WHERE id = %s", (doc_id,)),
                ("DELETE FROM document_versions WHERE id = %s", (doc_id,)),
                ("UPDATE pages SET rotation = 90 WHERE document_version_id = %s AND page_index = 0", (doc_id,)),
                ("DELETE FROM pages WHERE document_version_id = %s AND page_index = 1", (doc_id,)),
                ("UPDATE evidence_units SET raw_content = 'changed' WHERE id = %s", (eu_id,)),
                ("DELETE FROM evidence_units WHERE id = %s", (eu_id,)),
                ("UPDATE evidence_regions SET quote = 'q' WHERE evidence_unit_id = %s", (eu_id,)),
                ("DELETE FROM evidence_regions WHERE evidence_unit_id = %s", (eu_id,)),
                ("UPDATE attempts SET transport = 'jsonl' WHERE id = %s", (attempt_id,)),
                ("DELETE FROM attempts WHERE id = %s", (attempt_id,)),
                ("UPDATE method_revisions SET visibility = 'declared_only' WHERE id = %s", (method_id,)),
                ("DELETE FROM method_revisions WHERE id = %s", (method_id,)),
                ("UPDATE derivation_inputs SET evidence_unit_id = %s WHERE id = %s", (eu_id, deriv_in_id)),
                ("DELETE FROM derivation_inputs WHERE id = %s", (deriv_in_id,)),
                ("UPDATE outbox_consumptions SET consumed_at = now() WHERE event_id = %s", (outbox_event_id,)),
                ("DELETE FROM outbox_consumptions WHERE event_id = %s", (outbox_event_id,)),
            ]
            for sql, params in mutations:
                with self.subTest(sql=sql):
                    with self.assertRaises(IntegrityConstraintViolation):
                        with conn.transaction():
                            conn.execute(sql, params)

    def test_01_stateful_immutability_guards(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            doc_id = self.make_document(conn, p, a)
            eu_id = self.make_evidence_unit(conn, p, doc_id)

            # --- operations ---
            op_id = self.make_operation(conn, p, a, status="intent")
            with conn.transaction():
                conn.execute("UPDATE operations SET status = 'running' WHERE id = %s", (op_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE operations SET kind = 'other.kind' WHERE id = %s", (op_id,))
            with conn.transaction():
                conn.execute(
                    "UPDATE operations SET status = 'succeeded', finished_at = now() WHERE id = %s",
                    (op_id,),
                )
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE operations SET status = 'failed' WHERE id = %s", (op_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM operations WHERE id = %s", (op_id,))

            # --- outbox_events ---
            ev_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO outbox_events (event_id, project_id, topic, payload, operation_id) VALUES (%s, %s, 't', '{}'::jsonb, %s)",
                    (ev_id, p, op_id),
                )
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE outbox_events SET topic = 't2' WHERE event_id = %s", (ev_id,))
            with conn.transaction():
                conn.execute("UPDATE outbox_events SET delivered_at = now() WHERE event_id = %s", (ev_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE outbox_events SET delivered_at = now() WHERE event_id = %s", (ev_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM outbox_events WHERE event_id = %s", (ev_id,))

            # --- node_operations ---
            job_id = uuid.uuid4()
            node_op_id = uuid.uuid4()
            art_id = self.make_artifact(conn, p, state="committed")
            with conn.transaction():
                conn.execute(
                    "INSERT INTO jobs (id, project_id, kind, max_attempts) VALUES (%s, %s, 'job.a', 3)",
                    (job_id, p),
                )
                conn.execute(
                    """
                    INSERT INTO node_operations
                        (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status)
                    VALUES (%s, %s, 'w1', 'nr1', %s, 1, 'intent')
                    """,
                    (node_op_id, job_id, uuid.uuid4()),
                )
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE node_operations SET work_item_id = 'w2' WHERE id = %s", (node_op_id,))
            with conn.transaction():
                conn.execute(
                    "UPDATE node_operations SET status = 'succeeded', result_artifact_id = %s WHERE id = %s",
                    (art_id, node_op_id),
                )
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE node_operations SET status = 'failed' WHERE id = %s", (node_op_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM node_operations WHERE id = %s", (node_op_id,))

            # --- parse_runs & visual_objects ---
            raw_art = self.make_artifact(conn, p, state="committed")
            man_art = self.make_artifact(conn, p, state="committed")
            crop_art = self.make_artifact(conn, p, state="committed")
            pr_id = uuid.uuid4()
            vo_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO parse_runs
                        (id, project_id, document_version_id, parser_name, output_schema,
                         requested_profile, effective_profile, raw_artifact_id, manifest_artifact_id,
                         status, operation_id)
                    VALUES (%s, %s, %s, 'docling', 'v1', 'p1', 'p1', %s, %s, 'running', %s)
                    """,
                    (pr_id, p, doc_id, raw_art, man_art, op_id),
                )
                conn.execute(
                    """
                    INSERT INTO visual_objects
                        (id, project_id, parse_run_id, evidence_unit_id, object_type, crop_artifact_id, status)
                    VALUES (%s, %s, %s, %s, 'chart', %s, 'extracted')
                    """,
                    (vo_id, p, pr_id, eu_id, crop_art),
                )
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE parse_runs SET parser_name = 'other' WHERE id = %s", (pr_id,))
            with conn.transaction():
                conn.execute("UPDATE parse_runs SET status = 'succeeded' WHERE id = %s", (pr_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE parse_runs SET status = 'failed' WHERE id = %s", (pr_id,))

            # visual_objects transitions: extracted -> needs_review -> verified (terminal)
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE visual_objects SET object_type = 'table' WHERE id = %s", (vo_id,))
            with conn.transaction():
                conn.execute("UPDATE visual_objects SET status = 'needs_review' WHERE id = %s", (vo_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE visual_objects SET status = 'extracted' WHERE id = %s", (vo_id,))
            with conn.transaction():
                conn.execute("UPDATE visual_objects SET status = 'verified' WHERE id = %s", (vo_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE visual_objects SET status = 'rejected' WHERE id = %s", (vo_id,))

            # --- derivations ---
            deriv_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO derivations (id, project_id, kind, operation_id, status) VALUES (%s, %s, 'summary', %s, 'unverified')",
                    (deriv_id, p, op_id),
                )
                conn.execute(
                    "INSERT INTO derivation_inputs (project_id, derivation_id, evidence_unit_id) VALUES (%s, %s, %s)",
                    (p, deriv_id, eu_id),
                )
                conn.execute("UPDATE derivations SET status = 'verified' WHERE id = %s", (deriv_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE derivations SET status = 'unverified' WHERE id = %s", (deriv_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE derivations SET model = 'm2' WHERE id = %s", (deriv_id,))

    # ------------------------------------------------------------------
    # Item 2: evidence_regions must match evidence_unit document_version_id
    # ------------------------------------------------------------------

    def test_02_evidence_regions_same_document(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            doc1 = self.make_document(conn, p, a)
            doc2 = self.make_document(conn, p, a)
            eu1 = self.make_evidence_unit(conn, p, doc1)

            # Positive: same document_version_id
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO evidence_regions
                        (evidence_unit_id, ordinal, document_version_id, page_index, x0, y0, x1, y1, kind)
                    VALUES (%s, 0, %s, 0, 0.1, 0.1, 0.5, 0.5, 'text')
                    """,
                    (eu1, doc1),
                )

            # Negative: mismatched document_version_id raises ForeignKeyViolation
            with self.assertRaises(ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO evidence_regions
                            (evidence_unit_id, ordinal, document_version_id, page_index, x0, y0, x1, y1, kind)
                        VALUES (%s, 1, %s, 0, 0.1, 0.1, 0.5, 0.5, 'text')
                        """,
                        (eu1, doc2),
                    )

    # ------------------------------------------------------------------
    # Item 3: Artifact references on submissions and node_operations
    # ------------------------------------------------------------------

    def test_03_artifact_references_project_and_committed(self):
        with self.connect() as conn:
            p1, a1 = self.make_project(conn)
            p2, _ = self.make_project(conn)
            doc1 = self.make_document(conn, p1, a1)
            bm1, q1, _ = self.make_benchmark_with_question(conn, p1, a1, doc1, freeze=True)
            _, run1 = self.make_method_and_run(conn, p1, a1, bm1)

            attempt_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO attempts (id, run_id, benchmark_id, question_id, attempt_number, transport) VALUES (%s, %s, %s, %s, 1, 'http')",
                    (attempt_id, run1, bm1, q1),
                )

            art_staged_p1 = self.make_artifact(conn, p1, state="staged")
            art_committed_p2 = self.make_artifact(conn, p2, state="committed")
            art_committed_p1 = self.make_artifact(conn, p1, state="committed")

            # Negative: staged artifact on submission -> ForeignKeyViolation
            with self.assertRaises(ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO submissions
                            (id, run_id, attempt_id, question_id, status, answer_status, payload_hash, c14n_version, payload_artifact_id)
                        VALUES (%s, %s, %s, %s, 'accepted', 'answered', %s, 'v1', %s)
                        """,
                        (uuid.uuid4(), run1, attempt_id, q1, h("s1"), art_staged_p1),
                    )

            # Negative: other project's committed artifact on submission -> ForeignKeyViolation
            with self.assertRaises(ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO submissions
                            (id, run_id, attempt_id, question_id, status, answer_status, payload_hash, c14n_version, payload_artifact_id)
                        VALUES (%s, %s, %s, %s, 'accepted', 'answered', %s, 'v1', %s)
                        """,
                        (uuid.uuid4(), run1, attempt_id, q1, h("s2"), art_committed_p2),
                    )

            # Positive: same project, committed artifact on submission
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO submissions
                        (id, run_id, attempt_id, question_id, status, answer_status, payload_hash, c14n_version, payload_artifact_id)
                    VALUES (%s, %s, %s, %s, 'accepted', 'answered', %s, 'v1', %s)
                    """,
                    (uuid.uuid4(), run1, attempt_id, q1, h("s3"), art_committed_p1),
                )

            # node_operations: negative (staged / wrong project) and positive (committed, same project)
            job_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO jobs (id, project_id, kind, max_attempts) VALUES (%s, %s, 'job.n', 2)",
                    (job_id, p1),
                )
            with self.assertRaises(ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO node_operations
                            (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status, result_artifact_id)
                        VALUES (%s, %s, 'w', 'r', %s, 1, 'succeeded', %s)
                        """,
                        (uuid.uuid4(), job_id, uuid.uuid4(), art_staged_p1),
                    )
            with self.assertRaises(ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO node_operations
                            (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status, result_artifact_id)
                        VALUES (%s, %s, 'w', 'r', %s, 1, 'succeeded', %s)
                        """,
                        (uuid.uuid4(), job_id, uuid.uuid4(), art_committed_p2),
                    )
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO node_operations
                        (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status, result_artifact_id)
                    VALUES (%s, %s, 'w', 'r', %s, 1, 'succeeded', %s)
                    """,
                    (uuid.uuid4(), job_id, uuid.uuid4(), art_committed_p1),
                )

    # ------------------------------------------------------------------
    # Item 4: artifacts_guard strengthening
    # ------------------------------------------------------------------

    def test_04_artifacts_guard_hardening(self):
        with self.connect() as conn:
            p, _ = self.make_project(conn)
            art_staged = self.make_artifact(conn, p, state="staged", retention="standard")

            # Identity and retention are immutable even in staged state
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE artifacts SET mime = 'text/plain' WHERE id = %s", (art_staged,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE artifacts SET retention = 'restricted' WHERE id = %s", (art_staged,))

            # Positive: staged -> committed sets committed_at
            with conn.transaction():
                conn.execute(
                    "UPDATE artifacts SET state = 'committed', committed_at = now() WHERE id = %s",
                    (art_staged,),
                )

            # Negative: cannot alter committed_at after commit
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE artifacts SET committed_at = now() + interval '1 hour' WHERE id = %s",
                        (art_staged,),
                    )

            # Positive: committed -> tombstoned sets tombstoned_at and tombstone_reason
            with conn.transaction():
                conn.execute(
                    "UPDATE artifacts SET state = 'tombstoned', tombstoned_at = now(), tombstone_reason = 'expired' WHERE id = %s",
                    (art_staged,),
                )

            # legal_hold artifact cannot be deleted (even when staged) or tombstoned (when committed)
            art_hold = self.make_artifact(conn, p, state="staged", retention="legal_hold")
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM artifacts WHERE id = %s", (art_hold,))
            with conn.transaction():
                conn.execute(
                    "UPDATE artifacts SET state = 'committed', committed_at = now() WHERE id = %s",
                    (art_hold,),
                )
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE artifacts SET state = 'tombstoned', tombstoned_at = now(), tombstone_reason = 'hold' WHERE id = %s",
                        (art_hold,),
                    )

    # ------------------------------------------------------------------
    # Item 5 & Item 6: evaluations and question_scores
    # ------------------------------------------------------------------

    def test_05_06_evaluations_and_question_scores(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            doc_id = self.make_document(conn, p, a)

            # Create benchmark with 2 questions and freeze it
            res_id = self.make_resource(conn, p, a, "benchmark")
            bm_id = uuid.uuid4()
            q1 = uuid.uuid4()
            q2 = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO benchmark_revisions (id, project_id, resource_id, document_version_id, n_target, q_min) VALUES (%s, %s, %s, %s, 20, 2)",
                    (bm_id, p, res_id, doc_id),
                )
                conn.execute(
                    "INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, payload) VALUES (%s, %s, 'q1', 0, 'Q1?', %s, 'general', '{}'::jsonb)",
                    (q1, bm_id, h("q1")),
                )
                conn.execute(
                    "INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, payload) VALUES (%s, %s, 'q2', 1, 'Q2?', %s, 'general', '{}'::jsonb)",
                    (q2, bm_id, h("q2")),
                )
                conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(a),))
                for idx, st in enumerate(["parsing", "planning", "generating", "validating", "ready_to_freeze"], start=2):
                    conn.execute("UPDATE benchmark_revisions SET status = %s, row_version = %s WHERE id = %s", (st, idx, bm_id))
                conn.execute(
                    "UPDATE benchmark_revisions SET status = 'frozen', compliance = 'compliant', frozen_at = now(), row_version = 7 WHERE id = %s",
                    (bm_id,),
                )

            _, run_id = self.make_method_and_run(conn, p, a, bm_id)

            # Item 5 negative: cannot insert evaluation with status='published'
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) VALUES (%s, %s, %s, 'g1', 'published')",
                        (uuid.uuid4(), run_id, bm_id),
                    )

            # Positive: insert provisional evaluation
            eval_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) VALUES (%s, %s, %s, 'g1', 'provisional')",
                    (eval_id, run_id, bm_id),
                )

            # Create an accepted submission for q1 (and none for q2)
            att1 = uuid.uuid4()
            sub1 = uuid.uuid4()
            art1 = self.make_artifact(conn, p, state="committed")
            with conn.transaction():
                conn.execute(
                    "INSERT INTO attempts (id, run_id, benchmark_id, question_id, attempt_number, transport) VALUES (%s, %s, %s, %s, 1, 'http')",
                    (att1, run_id, bm_id, q1),
                )
                conn.execute(
                    """
                    INSERT INTO submissions
                        (id, run_id, attempt_id, question_id, status, answer_status, payload_hash, c14n_version, payload_artifact_id)
                    VALUES (%s, %s, %s, %s, 'accepted', 'answered', %s, 'v1', %s)
                    """,
                    (sub1, run_id, att1, q1, h("sub1"), art1),
                )

            # Item 6 negative: q1 has an accepted submission, so submission_id=NULL must raise ForeignKeyViolation
            with self.assertRaises(ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO question_scores
                            (evaluation_id, run_id, benchmark_id, question_id, submission_id, metric_name, status, raw_value, numerator, denominator)
                        VALUES (%s, %s, %s, %s, NULL, 'correctness', 'scored', 100, 1, 1)
                        """,
                        (eval_id, run_id, bm_id, q1),
                    )

            # Item 6 positive: q1 with submission_id = sub1 succeeds
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO question_scores
                        (evaluation_id, run_id, benchmark_id, question_id, submission_id, metric_name, status, raw_value, numerator, denominator)
                    VALUES (%s, %s, %s, %s, %s, 'correctness', 'scored', 100, 1, 1)
                    """,
                    (eval_id, run_id, bm_id, q1, sub1),
                )

            # Item 5 negative: cannot publish evaluation while q2 has no question_scores
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute("UPDATE evaluations SET status = 'published' WHERE id = %s", (eval_id,))

            # Insert q2 score (submission_id=NULL is valid because q2 has no accepted submission)
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO question_scores
                        (evaluation_id, run_id, benchmark_id, question_id, submission_id, metric_name, status, reason_codes)
                    VALUES (%s, %s, %s, %s, NULL, 'correctness', 'not_applicable', ARRAY['no_submission'])
                    """,
                    (eval_id, run_id, bm_id, q2),
                )
                conn.execute("UPDATE evaluations SET status = 'published' WHERE id = %s", (eval_id,))

            # Once published, scores cannot be inserted/updated/deleted
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM question_scores WHERE evaluation_id = %s", (eval_id,))

    # ------------------------------------------------------------------
    # Item 7: runs state machine and run_transitions
    # ------------------------------------------------------------------

    def test_07_runs_state_machine(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            doc_id = self.make_document(conn, p, a)
            bm_id, _, _ = self.make_benchmark_with_question(conn, p, a, doc_id, freeze=True)
            _, run_id = self.make_method_and_run(conn, p, a, bm_id)

            # Positive transitions: queued -> preparing -> running -> evaluating -> completed
            with conn.transaction():
                conn.execute("SELECT set_config('lab.transition_reason', '', true)")
                conn.execute("UPDATE runs SET status = 'preparing', row_version = 2 WHERE id = %s", (run_id,))
                conn.execute("SELECT set_config('lab.transition_reason', 'started', true)")
                conn.execute("UPDATE runs SET status = 'running', row_version = 3 WHERE id = %s", (run_id,))

            rows = conn.execute(
                "SELECT from_status, to_status, row_version, reason FROM run_transitions WHERE run_id = %s ORDER BY id",
                (run_id,),
            ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("queued", "preparing", 2, "unspecified"),
                    ("preparing", "running", 3, "started"),
                ],
            )

            # Negative: illegal transition running -> completed
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE runs SET status = 'completed', row_version = 4 WHERE id = %s", (run_id,))

            # Negative: wrong row_version increment
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE runs SET status = 'evaluating', row_version = 5 WHERE id = %s", (run_id,))

            # Negative: changing budget_profile
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE runs SET budget_profile = 'other', row_version = 4 WHERE id = %s", (run_id,))

            # Advance to terminal and verify terminal immutability + no DELETE + run_transitions append-only
            with conn.transaction():
                conn.execute("UPDATE runs SET status = 'evaluating', row_version = 4 WHERE id = %s", (run_id,))
                conn.execute("UPDATE runs SET status = 'completed', finalized_at = now(), row_version = 5 WHERE id = %s", (run_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE runs SET status = 'failed', row_version = 6 WHERE id = %s", (run_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("DELETE FROM run_transitions WHERE run_id = %s", (run_id,))

    # ------------------------------------------------------------------
    # Item 8: benchmark transitions, freeze checks, resource_versions sync
    # ------------------------------------------------------------------

    def test_08_benchmark_hardening(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            doc_id = self.make_document(conn, p, a)
            res_id = self.make_resource(conn, p, a, "benchmark")
            bm_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO benchmark_revisions (id, project_id, resource_id, document_version_id, n_target, q_min) VALUES (%s, %s, %s, %s, 20, 2)",
                    (bm_id, p, res_id, doc_id),
                )

            # Negative: missing lab.actor_id raises CheckViolation on status change
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute("SELECT set_config('lab.actor_id', '', true)")
                    conn.execute("UPDATE benchmark_revisions SET status = 'parsing', row_version = 2 WHERE id = %s", (bm_id,))

            # Advance to ready_to_freeze without questions
            with conn.transaction():
                conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(a),))
                conn.execute("SELECT set_config('lab.transition_reason', '', true)")
                for idx, st in enumerate(["parsing", "planning", "generating", "validating", "ready_to_freeze"], start=2):
                    conn.execute("UPDATE benchmark_revisions SET status = %s, row_version = %s WHERE id = %s", (st, idx, bm_id))

            # Negative: freezing with 0 questions raises CheckViolation
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(a),))
                    conn.execute(
                        "UPDATE benchmark_revisions SET status = 'frozen', compliance = 'compliant', frozen_at = now(), row_version = 7 WHERE id = %s",
                        (bm_id,),
                    )

            # Positive: add a question, then freeze; verify resource_versions.frozen is true and transitions logged
            with conn.transaction():
                conn.execute(
                    "INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, payload) VALUES (%s, %s, 'q1', 0, 'Q?', %s, 'general', '{}'::jsonb)",
                    (uuid.uuid4(), bm_id, h("bm8-q1")),
                )
                conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(a),))
                conn.execute("SELECT set_config('lab.transition_reason', 'freeze-now', true)")
                conn.execute(
                    "UPDATE benchmark_revisions SET status = 'frozen', compliance = 'compliant', frozen_at = now(), row_version = 7 WHERE id = %s",
                    (bm_id,),
                )

            rv_frozen = conn.execute("SELECT frozen FROM resource_versions WHERE id = %s", (res_id,)).fetchone()[0]
            self.assertTrue(rv_frozen)
            t_count = conn.execute("SELECT count(*) FROM benchmark_transitions WHERE benchmark_id = %s", (bm_id,)).fetchone()[0]
            self.assertEqual(t_count, 6)

    # ------------------------------------------------------------------
    # Item 9: jobs_guard strengthening
    # ------------------------------------------------------------------

    def test_09_jobs_guard_hardening(self):
        with self.connect() as conn:
            p, _ = self.make_project(conn)
            job_id = uuid.uuid4()
            exp = utcnow() + timedelta(minutes=10)
            with conn.transaction():
                conn.execute(
                    "INSERT INTO jobs (id, project_id, kind, max_attempts, retryable) VALUES (%s, %s, 'job.k', 2, true)",
                    (job_id, p),
                )

            # Negative: immutable fields (kind, max_attempts, retryable)
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE jobs SET max_attempts = 5, row_version = 2 WHERE id = %s", (job_id,))
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute("UPDATE jobs SET retryable = false, row_version = 2 WHERE id = %s", (job_id,))

            # Negative: attempt cannot jump by 2
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE jobs SET status = 'running', attempt = 2, lease_owner = 'w1', lease_token = 10, lease_expires_at = %s, row_version = 2 WHERE id = %s",
                        (exp, job_id),
                    )

            # Positive: queued -> running with token=10, attempt=1, and running -> running lease takeover with token=20
            with conn.transaction():
                conn.execute("SELECT set_config('lab.transition_reason', '', true)")
                conn.execute(
                    "UPDATE jobs SET status = 'running', attempt = 1, lease_owner = 'w1', lease_token = 10, lease_expires_at = %s, row_version = 2 WHERE id = %s",
                    (exp, job_id),
                )
                conn.execute("SELECT set_config('lab.transition_reason', 'takeover', true)")
                conn.execute(
                    "UPDATE jobs SET lease_owner = 'w2', lease_token = 20, lease_expires_at = %s, row_version = 3 WHERE id = %s",
                    (exp, job_id),
                )
                # Return to queued (clearing lease_token while max_lease_token remains 20)
                conn.execute(
                    "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, row_version = 4 WHERE id = %s",
                    (job_id,),
                )

            # Negative: re-entering running with token <= historical max_lease_token (20) must fail
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE jobs SET status = 'running', lease_owner = 'w3', lease_token = 15, lease_expires_at = %s, row_version = 5 WHERE id = %s",
                        (exp, job_id),
                    )
            with self.assertRaises(IntegrityConstraintViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE jobs SET status = 'running', lease_owner = 'w3', lease_token = 20, lease_expires_at = %s, row_version = 5 WHERE id = %s",
                        (exp, job_id),
                    )

            # Positive: re-entering running with token=21 > 20 succeeds
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET status = 'running', attempt = 2, lease_owner = 'w3', lease_token = 21, lease_expires_at = %s, row_version = 5 WHERE id = %s",
                    (exp, job_id),
                )

            # Negative: attempt cannot exceed max_attempts (2)
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE jobs SET attempt = 3, row_version = 6 WHERE id = %s",
                        (job_id,),
                    )

            transitions = conn.execute(
                "SELECT from_status, to_status, lease_token, reason FROM job_transitions WHERE job_id = %s ORDER BY id",
                (job_id,),
            ).fetchall()
            self.assertEqual(
                transitions,
                [
                    ("queued", "running", 10, "unspecified"),
                    ("running", "running", 20, "takeover"),
                    ("running", "queued", 20, "takeover"),
                    ("queued", "running", 21, "takeover"),
                ],
            )

    # ------------------------------------------------------------------
    # Item 10: node_operations job-scoped uniqueness
    # ------------------------------------------------------------------

    def test_10_node_operations_job_scoped_uniqueness(self):
        with self.connect() as conn:
            p, _ = self.make_project(conn)
            job1 = uuid.uuid4()
            job2 = uuid.uuid4()
            inv_id = uuid.uuid4()
            art = self.make_artifact(conn, p, state="committed")
            with conn.transaction():
                conn.execute("INSERT INTO jobs (id, project_id, kind, max_attempts) VALUES (%s, %s, 'j1', 2)", (job1, p))
                conn.execute("INSERT INTO jobs (id, project_id, kind, max_attempts) VALUES (%s, %s, 'j2', 2)", (job2, p))

                # Positive: two different jobs can use the same (work_item_id, node_revision, logical_invocation_id, attempt) and both succeed
                conn.execute(
                    """
                    INSERT INTO node_operations
                        (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status, result_artifact_id)
                    VALUES (%s, %s, 'wi-1', 'rev-1', %s, 1, 'succeeded', %s)
                    """,
                    (uuid.uuid4(), job1, inv_id, art),
                )
                conn.execute(
                    """
                    INSERT INTO node_operations
                        (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status, result_artifact_id)
                    VALUES (%s, %s, 'wi-1', 'rev-1', %s, 1, 'succeeded', %s)
                    """,
                    (uuid.uuid4(), job2, inv_id, art),
                )

            # Negative: duplicate (job_id, work_item_id, node_revision, logical_invocation_id, attempt) within same job fails
            with self.assertRaises(UniqueViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO node_operations
                            (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status)
                        VALUES (%s, %s, 'wi-1', 'rev-1', %s, 1, 'intent')
                        """,
                        (uuid.uuid4(), job1, inv_id),
                    )

            # Negative: second succeeded attempt for the same (job_id, work_item_id, node_revision, logical_invocation_id) fails
            with self.assertRaises(UniqueViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO node_operations
                            (id, job_id, work_item_id, node_revision, logical_invocation_id, attempt, status, result_artifact_id)
                        VALUES (%s, %s, 'wi-1', 'rev-1', %s, 2, 'succeeded', %s)
                        """,
                        (uuid.uuid4(), job1, inv_id, art),
                    )

    # ------------------------------------------------------------------
    # Item 11: NaN and Infinity numeric guards
    # ------------------------------------------------------------------

    def test_11_numeric_nan_and_infinity_guards(self):
        with self.connect() as conn:
            p, a = self.make_project(conn)
            doc_id = self.make_document(conn, p, a, page_count=2)
            op_id = self.make_operation(conn, p, a)
            raw_art = self.make_artifact(conn, p, state="committed")
            man_art = self.make_artifact(conn, p, state="committed")
            bm_id, q_id, _ = self.make_benchmark_with_question(conn, p, a, doc_id, freeze=True)
            _, run_id = self.make_method_and_run(conn, p, a, bm_id)

            # 1. pages.width_pt / height_pt cannot be NaN
            res2 = self.make_resource(conn, p, a, "document")
            doc2 = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO document_versions (id, project_id, resource_id, artifact_id, sha256, page_count) VALUES (%s, %s, %s, %s, %s, 1)",
                    (doc2, p, res2, raw_art, h("doc2")),
                )
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO pages (document_version_id, page_index, rotation, width_pt, height_pt) VALUES (%s, 0, 0, 'NaN'::numeric, 792)",
                        (doc2,),
                    )

            # 2. parse_runs.cost_usd: finite positive works, NaN fails
            pr_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO parse_runs
                        (id, project_id, document_version_id, parser_name, output_schema,
                         requested_profile, effective_profile, raw_artifact_id, manifest_artifact_id,
                         cost_usd, status, operation_id)
                    VALUES (%s, %s, %s, 'docling', 'v1', 'p1', 'p1', %s, %s, 1.25, 'running', %s)
                    """,
                    (pr_id, p, doc_id, raw_art, man_art, op_id),
                )
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO parse_runs
                            (id, project_id, document_version_id, parser_name, output_schema,
                             requested_profile, effective_profile, raw_artifact_id, manifest_artifact_id,
                             cost_usd, status, operation_id)
                        VALUES (%s, %s, %s, 'docling', 'v1', 'p1', 'p1', %s, %s, 'NaN'::numeric, 'running', %s)
                        """,
                        (uuid.uuid4(), p, doc_id, raw_art, man_art, op_id),
                    )

            # 3. visual_observations.estimate_low / estimate_high: finite works, NaN fails
            vo_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO visual_objects (id, project_id, parse_run_id, object_type) VALUES (%s, %s, %s, 'chart')",
                    (vo_id, p, pr_id),
                )
                conn.execute(
                    """
                    INSERT INTO visual_observations
                        (project_id, visual_object_id, layer, observation_type, data_artifact_id,
                         value_source, estimate_low, estimate_high, producer_operation_id)
                    VALUES (%s, %s, 'L2', 'point', %s, 'estimated_from_plot', 10.0, 20.0, %s)
                    """,
                    (p, vo_id, raw_art, op_id),
                )
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO visual_observations
                            (project_id, visual_object_id, layer, observation_type, data_artifact_id,
                             value_source, estimate_low, estimate_high, producer_operation_id)
                        VALUES (%s, %s, 'L2', 'point', %s, 'estimated_from_plot', 10.0, 'NaN'::numeric, %s)
                        """,
                        (p, vo_id, raw_art, op_id),
                    )

            # 4. question_scores: NaN or Infinity on numerator/denominator/raw_value fails
            eval_id = uuid.uuid4()
            with conn.transaction():
                conn.execute(
                    "INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) VALUES (%s, %s, %s, 'g1', 'provisional')",
                    (eval_id, run_id, bm_id),
                )
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO question_scores
                            (evaluation_id, run_id, benchmark_id, question_id, metric_name, status, numerator, reason_codes)
                        VALUES (%s, %s, %s, %s, 'correctness', 'not_applicable', 'NaN'::double precision, ARRAY['na'])
                        """,
                        (eval_id, run_id, bm_id, q_id),
                    )
            with self.assertRaises(CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO question_scores
                            (evaluation_id, run_id, benchmark_id, question_id, metric_name, status, denominator, reason_codes)
                        VALUES (%s, %s, %s, %s, 'correctness', 'not_applicable', 'Infinity'::double precision, ARRAY['na'])
                        """,
                        (eval_id, run_id, bm_id, q_id),
                    )


if __name__ == "__main__":
    unittest.main()
