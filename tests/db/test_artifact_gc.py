"""Tests for artifact staging, commit verification, tombstone, and orphan GC (SPEC 10.2, T80)."""

from __future__ import annotations

import datetime
import uuid

from lab_db.artifacts import (
    ChecksumMismatch,
    InvalidState,
    collect_staged,
    commit,
    stage,
    tombstone,
)

from .base import FreshDatabase, h


class TestArtifactLifecycleAndGC(FreshDatabase):
    """Integration tests against real PostgreSQL for artifacts lifecycle and orphan collection."""

    def _stage_sample(
        self,
        conn,
        project_id: uuid.UUID,
        label: str,
        size_bytes: int = 1024,
        mime: str = "application/pdf",
        retention: str = "standard",
    ) -> tuple[uuid.UUID, str, int]:
        digest = h(f"content-{label}")
        aid = stage(
            conn,
            project_id=project_id,
            object_key=f"objects/{project_id}/{label}",
            object_version=f"v-{label}",
            sha256=digest,
            size_bytes=size_bytes,
            mime=mime,
            retention=retention,
        )
        return aid, digest, size_bytes

    def _get_artifact_row(self, conn, artifact_id: uuid.UUID):
        return conn.execute(
            """
            SELECT state, committed_at, tombstoned_at, tombstone_reason
            FROM artifacts
            WHERE id = %s
            """,
            (artifact_id,),
        ).fetchone()

    def test_stage_and_commit_verification(self) -> None:
        """Checksum or size mismatch raises ChecksumMismatch and keeps state 'staged'; match commits."""
        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            aid, digest, size = self._stage_sample(conn, project_id, "doc-1", size_bytes=2048)

            row = self._get_artifact_row(conn, aid)
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "staged")
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])

            # 1. Wrong sha256 -> ChecksumMismatch, still staged
            with self.assertRaises(ChecksumMismatch):
                commit(conn, aid, observed_sha256=h("wrong-content"), observed_size=size)
            row = self._get_artifact_row(conn, aid)
            self.assertEqual(row[0], "staged")
            self.assertIsNone(row[1])

            # 2. Wrong size -> ChecksumMismatch, still staged
            with self.assertRaises(ChecksumMismatch):
                commit(conn, aid, observed_sha256=digest, observed_size=size + 1)
            row = self._get_artifact_row(conn, aid)
            self.assertEqual(row[0], "staged")
            self.assertIsNone(row[1])

            # 3. Matching sha256 and size -> committed with committed_at set
            commit(conn, aid, observed_sha256=digest, observed_size=size)
            row = self._get_artifact_row(conn, aid)
            self.assertEqual(row[0], "committed")
            self.assertIsNotNone(row[1])
            self.assertIsNone(row[2])

            # 4. Re-committing an already committed artifact -> InvalidState
            with self.assertRaises(InvalidState):
                commit(conn, aid, observed_sha256=digest, observed_size=size)

    def test_collect_staged_orphans_and_preserves_active_or_referenced(self) -> None:
        """GC deletes expired unreferenced staged rows; keeps fresh, committed, tombstoned, and referenced staged rows."""
        with self.connect() as conn:
            project_id, _ = self.make_project(conn)

            # 1. Expired and unreferenced staged artifact -> should be deleted
            expired_orphan_id, _, _ = self._stage_sample(conn, project_id, "expired-orphan")
            # 2. Fresh unreferenced staged artifact -> should be kept
            fresh_staged_id, _, _ = self._stage_sample(conn, project_id, "fresh-staged")
            # 3. Expired committed artifact -> should be kept
            committed_id, c_hash, c_size = self._stage_sample(conn, project_id, "committed-old")
            # 4. Expired tombstoned artifact -> should be kept
            tombstoned_id, t_hash, t_size = self._stage_sample(conn, project_id, "tombstoned-old")
            # 5. Expired staged artifact referenced by node_operations -> should be kept
            ref_staged_id, _, _ = self._stage_sample(conn, project_id, "referenced-staged")

            # Backdate created_at on staged rows before committing/referencing.
            # created_at is immutable for the application (0007); test setup
            # simulates elapsed time by skipping triggers in this transaction only.
            with conn.transaction():
                conn.execute("SET LOCAL session_replication_role = replica")
                conn.execute(
                    """
                    UPDATE artifacts
                    SET created_at = now() - interval '2 days'
                    WHERE id = ANY(%s)
                    """,
                    ([expired_orphan_id, committed_id, tombstoned_id, ref_staged_id],),
                )

            commit(conn, committed_id, observed_sha256=c_hash, observed_size=c_size)
            commit(conn, tombstoned_id, observed_sha256=t_hash, observed_size=t_size)
            tombstone(conn, tombstoned_id, reason="scheduled retention policy cleanup")

            # Reference ref_staged_id from a table that may hold staged references
            # (0007 requires node_operations/submissions to reference committed
            # artifacts only). collect_staged discovers referencing FKs from the
            # catalog, so this table is protected without being hard-coded.
            with conn.transaction():
                conn.execute("CREATE TABLE IF NOT EXISTS test_gc_refs (artifact_id uuid NOT NULL "
                             "REFERENCES artifacts(id) ON DELETE RESTRICT)")
                conn.execute("INSERT INTO test_gc_refs VALUES (%s)", (ref_staged_id,))

            deleted_ids = collect_staged(conn, grace=datetime.timedelta(hours=24)).deleted
            self.assertEqual(deleted_ids, [expired_orphan_id])

            # Verify expired orphan is gone and all others remain intact
            self.assertIsNone(self._get_artifact_row(conn, expired_orphan_id))
            self.assertEqual(self._get_artifact_row(conn, fresh_staged_id)[0], "staged")
            self.assertEqual(self._get_artifact_row(conn, committed_id)[0], "committed")
            self.assertEqual(self._get_artifact_row(conn, tombstoned_id)[0], "tombstoned")
            self.assertEqual(self._get_artifact_row(conn, ref_staged_id)[0], "staged")

            # Also verify explicit `now` parameter works deterministically
            future_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)
            deleted_future = collect_staged(
                conn,
                grace=datetime.timedelta(hours=24),
                now=future_now,
            ).deleted
            self.assertEqual(deleted_future, [fresh_staged_id])
            self.assertIsNone(self._get_artifact_row(conn, fresh_staged_id))
            # Referenced staged artifact is still protected even when expired relative to future_now
            self.assertEqual(self._get_artifact_row(conn, ref_staged_id)[0], "staged")

    def test_tombstone_requires_reason_and_enforces_state_transitions(self) -> None:
        """Tombstone requires a non-empty reason and only transitions from 'committed'; terminal afterwards."""
        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            staged_id, s_hash, s_size = self._stage_sample(conn, project_id, "staged-only")
            art_id, a_hash, a_size = self._stage_sample(conn, project_id, "to-tombstone")

            # Cannot tombstone a staged artifact
            with self.assertRaises(InvalidState):
                tombstone(conn, staged_id, reason="premature tombstone")
            self.assertEqual(self._get_artifact_row(conn, staged_id)[0], "staged")

            commit(conn, art_id, observed_sha256=a_hash, observed_size=a_size)

            # Empty or whitespace-only reason must be rejected
            with self.assertRaises((ValueError, InvalidState)):
                tombstone(conn, art_id, reason="")
            with self.assertRaises((ValueError, InvalidState)):
                tombstone(conn, art_id, reason="   ")
            self.assertEqual(self._get_artifact_row(conn, art_id)[0], "committed")

            # Valid tombstone transitions committed -> tombstoned
            tombstone(conn, art_id, reason="redacted by owner request")
            row = self._get_artifact_row(conn, art_id)
            self.assertEqual(row[0], "tombstoned")
            self.assertIsNotNone(row[1])
            self.assertIsNotNone(row[2])
            self.assertEqual(row[3], "redacted by owner request")

            # Tombstoned artifact cannot be tombstoned again or committed again
            with self.assertRaises(InvalidState):
                tombstone(conn, art_id, reason="second tombstone")
            with self.assertRaises(InvalidState):
                commit(conn, art_id, observed_sha256=a_hash, observed_size=a_size)
