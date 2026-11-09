"""Database integration tests for LocalArtifactStore and store_and_register."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from psycopg.rows import dict_row

from lab_db.artifacts import ChecksumMismatch
from lab_db.storage import LocalArtifactStore, store_and_register

try:
    from .base import FreshDatabase
except ImportError:
    try:
        from tests.db.base import FreshDatabase
    except ImportError:
        from base import FreshDatabase  # type: ignore[no-redef]


class StorageDatabaseTests(FreshDatabase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.store = LocalArtifactStore(self.root)

    def test_store_and_register_commits_artifact_row(self) -> None:
        data = b"%PDF-1.7 valid artifact payload"
        expected_sha256 = hashlib.sha256(data).hexdigest()

        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            artifact_id = store_and_register(
                conn,
                self.store,
                project_id,
                data,
                mime="application/pdf",
            )
            self.assertIsInstance(artifact_id, uuid.UUID)

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM artifacts WHERE project_id = %s",
                    (project_id,),
                )
                rows = cur.fetchall()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("id", row.get("artifact_id")), artifact_id)
        self.assertEqual(row.get("state", row.get("status")), "committed")
        self.assertEqual(row["sha256"], expected_sha256)
        self.assertEqual(row.get("size_bytes", row.get("size")), len(data))
        self.assertEqual(
            row["object_key"],
            f"sha256/{expected_sha256[:2]}/{expected_sha256}",
        )
        self.assertEqual(row["object_version"], "1")
        self.assertEqual(row["mime"], "application/pdf")
        self.assertEqual(row["retention"], "restricted")
        self.assertEqual(self.store.open(row["object_key"]), data)

    def test_tamper_before_commit_raises_checksum_mismatch_and_keeps_staged(self) -> None:
        data = b"artifact payload before tamper"
        expected_sha256 = hashlib.sha256(data).hexdigest()

        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            with patch.object(
                self.store,
                "verify",
                return_value=("0" * 64, len(data)),
            ):
                with self.assertRaises(ChecksumMismatch):
                    store_and_register(
                        conn,
                        self.store,
                        project_id,
                        data,
                        mime="application/octet-stream",
                    )

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM artifacts WHERE project_id = %s",
                    (project_id,),
                )
                rows = cur.fetchall()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("state", row.get("status")), "staged")
        self.assertEqual(row["sha256"], expected_sha256)
        self.assertEqual(row.get("size_bytes", row.get("size")), len(data))

    def test_disk_corruption_before_verify_raises_checksum_mismatch_and_keeps_staged(
        self,
    ) -> None:
        data = b"artifact payload corrupted on disk before verify"
        expected_sha256 = hashlib.sha256(data).hexdigest()
        real_verify = LocalArtifactStore.verify

        def corrupt_then_verify(object_key: str, *args, **kwargs) -> tuple[str, int]:
            (self.root / object_key).write_bytes(b"corrupted on disk")
            return real_verify(self.store, object_key, *args, **kwargs)

        with self.connect() as conn:
            project_id, _ = self.make_project(conn)
            with patch.object(self.store, "verify", side_effect=corrupt_then_verify):
                with self.assertRaises(ChecksumMismatch):
                    store_and_register(
                        conn,
                        self.store,
                        project_id,
                        data,
                        mime="application/octet-stream",
                    )

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM artifacts WHERE project_id = %s",
                    (project_id,),
                )
                rows = cur.fetchall()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("state", row.get("status")), "staged")
        self.assertEqual(row["sha256"], expected_sha256)
        self.assertEqual(row.get("size_bytes", row.get("size")), len(data))


if __name__ == "__main__":
    unittest.main()
