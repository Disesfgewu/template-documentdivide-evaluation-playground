"""Real PostgreSQL/pgvector backup and restore verification tests (no SQLite, no fakes).

Requires:
- ``LAB_TEST_DATABASE_URL``: a PostgreSQL connection URL for a role allowed to
  CREATE DATABASE and CREATE EXTENSION on a server that ships pgvector
  (see ``deploy/compose.dev.yml``).
- ``pg_dump`` and ``pg_restore`` client executables on ``PATH``.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg

from lab_api.postgres_backup import (
    ARCHIVE_FILENAME,
    PostgresBackupError,
    create_postgres_backup,
    verify_backup_directory,
    verify_postgres_restore,
)

ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
HAS_PG_CLI = bool(shutil.which("pg_dump") and shutil.which("pg_restore"))


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class RealPostgresBackupRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not HAS_PG_CLI:
            raise AssertionError(
                "BLOCKED: pg_dump and pg_restore binaries are required on PATH for real PostgreSQL backup/restore tests"
            )
        suffix = uuid.uuid4().hex[:10]
        cls.source_db = f"lab_src_{suffix}"
        cls.empty_target_db = f"lab_restore_empty_{suffix}"
        cls.nonempty_target_db = f"lab_restore_nonempty_{suffix}"
        cls.addClassCleanup(cls._drop_databases)

        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{cls.source_db}"')
            conn.execute(f'CREATE DATABASE "{cls.empty_target_db}"')
            conn.execute(f'CREATE DATABASE "{cls.nonempty_target_db}"')

        cls.source_url = _with_db(ADMIN_URL, cls.source_db)
        cls.empty_target_url = _with_db(ADMIN_URL, cls.empty_target_db)
        cls.nonempty_target_url = _with_db(ADMIN_URL, cls.nonempty_target_db)

        with psycopg.connect(cls.source_url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION vector")
            conn.execute(
                """
                CREATE TABLE backup_probe (
                    id integer PRIMARY KEY,
                    label text NOT NULL,
                    embedding vector(3) NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO backup_probe (id, label, embedding)
                VALUES (1, 'alpha', '[1,0,0]'), (2, 'beta', '[0,1,0]')
                """
            )

        with psycopg.connect(cls.nonempty_target_url, autocommit=True) as conn:
            conn.execute("CREATE TABLE existing_data (id integer PRIMARY KEY)")
            conn.execute("INSERT INTO existing_data (id) VALUES (42)")

    @classmethod
    def _drop_databases(cls) -> None:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            for db in (cls.source_db, cls.empty_target_db, cls.nonempty_target_db):
                conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_real_pg_dump_verify_and_restore_into_disposable_empty_db(self) -> None:
        backup_dir = self.base / "pg_backup"
        manifest = create_postgres_backup(self.source_url, backup_dir)
        self.assertEqual(verify_backup_directory(backup_dir), manifest)

        # Reject restoring into the non-empty target database without altering its data.
        with self.assertRaises(PostgresBackupError) as cm:
            verify_postgres_restore(
                backup_dir,
                self.nonempty_target_url,
                confirm_disposable_target_database=self.nonempty_target_db,
                source_database_url=self.source_url,
            )
        self.assertEqual(cm.exception.code, "target_not_empty")
        with psycopg.connect(self.nonempty_target_url) as conn:
            rows = conn.execute("SELECT id FROM existing_data").fetchall()
        self.assertEqual(rows, [(42,)])

        # Reject restoring into a target whose database name matches the source
        # database name, even if the hostname is an alternate loopback IP (127.0.0.2).
        same_name_disp_db = f"lab_restore_src_{ uuid.uuid4().hex[:8] }"
        same_name_src_url = _with_db(ADMIN_URL, same_name_disp_db)
        p = urlsplit(same_name_src_url)
        alt_netloc = p.netloc.replace("127.0.0.1", "127.0.0.2").replace("localhost", "127.0.0.2")
        same_name_alt_host_url = urlunsplit((p.scheme, alt_netloc, "/" + same_name_disp_db, p.query, p.fragment))
        with self.assertRaises(PostgresBackupError) as cm_alias:
            verify_postgres_restore(
                backup_dir,
                same_name_alt_host_url,
                confirm_disposable_target_database=same_name_disp_db,
                source_database_url=same_name_src_url,
            )
        self.assertEqual(cm_alias.exception.code, "target_matches_source")

        # Restore into the disposable empty target database and verify data + pgvector.
        report = verify_postgres_restore(
            backup_dir,
            self.empty_target_url,
            confirm_disposable_target_database=self.empty_target_db,
            source_database_url=self.source_url,
        )
        self.assertEqual(report.archive_sha256, manifest.archive_sha256)
        self.assertGreaterEqual(report.restored_user_table_count, 1)

        with psycopg.connect(self.empty_target_url) as conn:
            restored_rows = conn.execute(
                "SELECT id, label, embedding <-> '[1,0,0]'::vector FROM backup_probe ORDER BY id"
            ).fetchall()
        self.assertEqual(len(restored_rows), 2)
        self.assertEqual(restored_rows[0][0], 1)
        self.assertEqual(restored_rows[0][1], "alpha")
        self.assertAlmostEqual(float(restored_rows[0][2]), 0.0)

        # A second restore into the now non-empty target must fail closed.
        with self.assertRaises(PostgresBackupError) as cm2:
            verify_postgres_restore(
                backup_dir,
                self.empty_target_url,
                confirm_disposable_target_database=self.empty_target_db,
                source_database_url=self.source_url,
            )
        self.assertEqual(cm2.exception.code, "target_not_empty")

        # Tampering with the archive bytes must fail before any DB restore.
        archive_path = backup_dir / ARCHIVE_FILENAME
        data = bytearray(archive_path.read_bytes())
        data[-1] ^= 0xFF
        archive_path.write_bytes(bytes(data))
        with self.assertRaises(PostgresBackupError) as cm3:
            verify_backup_directory(backup_dir)
        self.assertEqual(cm3.exception.code, "archive_digest_mismatch")


if __name__ == "__main__":
    unittest.main()
