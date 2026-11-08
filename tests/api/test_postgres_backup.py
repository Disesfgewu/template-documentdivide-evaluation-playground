"""Unit and failure-mode tests for PostgreSQL backup and restore verification."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import psycopg

from lab_api import __main__ as cli
from lab_api.postgres_backup import (
    ARCHIVE_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_FORMAT,
    PGDMP_MAGIC,
    BackupLimits,
    BackupManifest,
    PostgresBackupError,
    create_postgres_backup,
    verify_backup_directory,
    verify_postgres_restore,
)

SECRET_PW = "s3cret-pg-pw-991"
SOURCE_HOST = "pg-source-internal.local"
TARGET_HOST = "pg-verify-internal.local"
SOURCE_URL = f"postgresql://lab_operator:{SECRET_PW}@{SOURCE_HOST}:5432/lab_source"
TARGET_DB = "lab_restore_verify_01"
TARGET_URL = f"postgresql://lab_operator:{SECRET_PW}@{TARGET_HOST}:5432/{TARGET_DB}"


class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else ([row] if row is not None else [])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(
        self,
        dbname: str,
        *,
        in_recovery: bool = False,
        other_backends: int = 0,
        extra_schemas: int = 0,
        user_relations: int = 0,
        user_procs: int = 0,
        user_types: int = 0,
        event_triggers: int = 0,
        extensions: tuple[str, ...] = ("plpgsql", "vector"),
        restored_tables: int = 2,
        restored_objects: int = 4,
    ) -> None:
        self.dbname = dbname
        self.in_recovery = in_recovery
        self.other_backends = other_backends
        self.extra_schemas = extra_schemas
        self.user_relations = user_relations
        self.user_procs = user_procs
        self.user_types = user_types
        self.event_triggers = event_triggers
        self.extensions = extensions
        self.restored_tables = restored_tables
        self.restored_objects = restored_objects
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        norm = " ".join(sql.split())
        self.queries.append(norm)
        for forbidden in ("CREATE DATABASE", "DROP DATABASE", "DROP SCHEMA", "TRUNCATE"):
            if forbidden in norm.upper():
                raise AssertionError(f"forbidden SQL executed: {norm}")
        if norm == "SELECT current_database()":
            return _FakeCursor(row=(self.dbname,))
        if norm == "SELECT current_database(), pg_is_in_recovery()":
            return _FakeCursor(row=(self.dbname, self.in_recovery))
        if "FROM pg_stat_activity" in norm:
            return _FakeCursor(row=(self.other_backends,))
        if "FROM pg_namespace" in norm:
            return _FakeCursor(row=(self.extra_schemas,))
        if "FROM pg_class c" in norm and "c.relkind IN ('r', 'p')" in norm:
            return _FakeCursor(row=(self.restored_tables,))
        if "FROM pg_class c" in norm and "c.relkind IN ('r', 'p', 'v', 'm', 'S', 'i')" in norm:
            return _FakeCursor(row=(self.restored_objects,))
        if "FROM pg_class c" in norm:
            return _FakeCursor(row=(self.user_relations,))
        if "FROM pg_proc p" in norm:
            return _FakeCursor(row=(self.user_procs,))
        if "FROM pg_type t" in norm:
            return _FakeCursor(row=(self.user_types,))
        if "FROM pg_event_trigger" in norm:
            return _FakeCursor(row=(self.event_triggers,))
        if "SELECT extname FROM pg_extension" in norm:
            return _FakeCursor(rows=[(e,) for e in self.extensions])
        raise AssertionError(f"unexpected query: {norm}")


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body + "\n", encoding="utf-8")
    path.chmod(0o700)
    return path


class PostgresBackupUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.backup_dir = self.base / "backup_out"
        self.env_log = self.base / "env_log.txt"
        self.args_log = self.base / "args_log.txt"
        self.stdin_log = self.base / "stdin_log.txt"

        self.good_dump = _write_executable(
            self.bin_dir / "pg_dump",
            f'printf "%s\\n" "$@" > "{self.args_log}"\n'
            f'env > "{self.env_log}"\n'
            'printf "PGDMP-custom-archive-payload-0123456789"\n',
        )
        self.good_restore = _write_executable(
            self.bin_dir / "pg_restore",
            f'printf "%s\\n" "$@" >> "{self.args_log}"\n'
            f'env >> "{self.env_log}"\n'
            f'cat >> "{self.stdin_log}"\n'
            f'printf "\\n" >> "{self.stdin_log}"\n'
            'if [ "${1:-}" = "--list" ]; then\n'
            '  printf "; Archive created at 2026-11-07\\n1; 1259 16385 TABLE public items lab\\n2; 1259 16390 INDEX public items_idx lab\\n"\n'
            'fi\n',
        )

    def _assert_no_secret_leaked(self, text: str) -> None:
        for secret in (SECRET_PW, SOURCE_HOST, TARGET_HOST, SOURCE_URL, TARGET_URL):
            self.assertNotIn(secret, text)

    def test_create_verify_and_restore_happy_path_without_credential_exposure(self) -> None:
        conns = {
            "lab_source": _FakeConnection("lab_source"),
            TARGET_DB: _FakeConnection(TARGET_DB, restored_tables=2, restored_objects=3),
        }

        def fake_connect(**kwargs):
            self.assertNotIn("conninfo", kwargs)
            return conns[kwargs["dbname"]]

        with mock.patch("psycopg.connect", side_effect=fake_connect):
            manifest = create_postgres_backup(
                SOURCE_URL,
                self.backup_dir,
                pg_dump_bin=self.good_dump,
            )
            verified = verify_backup_directory(self.backup_dir)
            self.assertEqual(manifest, verified)
            self.assertEqual(manifest.format, MANIFEST_FORMAT)
            self.assertEqual(manifest.archive_filename, ARCHIVE_FILENAME)
            self.assertEqual(
                manifest.archive_sha256,
                hashlib.sha256(b"PGDMP-custom-archive-payload-0123456789").hexdigest(),
            )
            self._assert_no_secret_leaked(json.dumps(manifest.to_dict()))
            self._assert_no_secret_leaked(
                (self.backup_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
            )

            report = verify_postgres_restore(
                self.backup_dir,
                TARGET_URL,
                confirm_disposable_target_database=TARGET_DB,
                source_database_url=SOURCE_URL,
                pg_restore_bin=self.good_restore,
            )

        self.assertEqual(report.archive_sha256, manifest.archive_sha256)
        self.assertEqual(report.restored_catalog_entry_count, 2)
        self.assertEqual(report.restored_user_table_count, 2)
        self.assertEqual(report.restored_user_object_count, 3)

        # Process argv must never contain credentials, URLs, hosts, or mutable archive paths.
        recorded_args = self.args_log.read_text(encoding="utf-8")
        self._assert_no_secret_leaked(recorded_args)
        self.assertNotIn(str(self.backup_dir), recorded_args)
        self.assertIn("--format=custom", recorded_args)
        self.assertIn("--single-transaction", recorded_args)
        self.assertIn("--exit-on-error", recorded_args)
        self.assertNotIn("--create", recorded_args)
        self.assertNotIn("--clean", recorded_args)

        # Both pg_restore --list and pg_restore received the exact verified bytes via stdin.
        stdin_lines = self.stdin_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            stdin_lines,
            [
                "PGDMP-custom-archive-payload-0123456789",
                "PGDMP-custom-archive-payload-0123456789",
            ],
        )

        # Subprocess environment carries PGPASSWORD/PGHOST, never arbitrary caller env.
        recorded_env = self.env_log.read_text(encoding="utf-8")
        self.assertIn(f"PGPASSWORD={SECRET_PW}", recorded_env)
        self.assertIn(f"PGHOST={SOURCE_HOST}", recorded_env)
        self.assertIn(f"PGHOST={TARGET_HOST}", recorded_env)

        # Directory and file permissions are restricted.
        self.assertEqual(stat.S_IMODE(self.backup_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.backup_dir / ARCHIVE_FILENAME).stat().st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE((self.backup_dir / MANIFEST_FILENAME).stat().st_mode), 0o600
        )

    def test_url_validation_and_redaction_on_failure(self) -> None:
        bad_urls = [
            "",
            f"sqlite:///{SECRET_PW}.db",
            f"mysql://u:{SECRET_PW}@{SOURCE_HOST}/db",
            f"postgresql://:{SECRET_PW}@{SOURCE_HOST}/db",
            f"postgresql://u:{SECRET_PW}@/db",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/a/b",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/db#frag",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}:99999/db",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/db;drop",
            f"postgresql://u;bad:{SECRET_PW}@{SOURCE_HOST}/db",
            f"postgresql://u:{SECRET_PW}\n@{SOURCE_HOST}/db",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/db?passfile=/etc/passwd",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/db?options=-c%20log_statement=all",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/db?sslmode=invalid",
            f"postgresql://u:{SECRET_PW}@{SOURCE_HOST}/db?sslmode=require&sslmode=disable",
        ]
        for bad in bad_urls:
            with self.subTest(bad=bad[:32]):
                with self.assertRaises(PostgresBackupError) as cm:
                    create_postgres_backup(
                        bad, self.backup_dir, pg_dump_bin=self.good_dump
                    )
                self._assert_no_secret_leaked(str(cm.exception))
                self.assertIsNone(cm.exception.__cause__)
                self.assertFalse(self.backup_dir.exists())

    def test_disposable_target_guards_and_dns_alias_or_loopback_bypass_rejected(self) -> None:
        with mock.patch(
            "psycopg.connect", return_value=_FakeConnection("lab_source")
        ):
            create_postgres_backup(
                SOURCE_URL, self.backup_dir, pg_dump_bin=self.good_dump
            )

        # 1. Missing or mismatched confirmation string.
        for bad_confirm, expected_code in (
            ("", "missing_target_confirmation"),
            (None, "missing_target_confirmation"),
            ("wrong_db_name", "target_confirmation_mismatch"),
        ):
            with self.subTest(confirm=bad_confirm):
                with self.assertRaises(PostgresBackupError) as cm:
                    verify_postgres_restore(
                        self.backup_dir,
                        TARGET_URL,
                        confirm_disposable_target_database=bad_confirm,  # type: ignore[arg-type]
                        pg_restore_bin=self.good_restore,
                    )
                self.assertEqual(cm.exception.code, expected_code)

        # 2. Target database names that are production/default or lack disposable prefix.
        for unsafe_db in (
            "lab",
            "postgres",
            "production",
            "prod",
            "main",
            "template1",
            "analytics_db",
            "user_data",
        ):
            url = f"postgresql://lab_operator:{SECRET_PW}@{TARGET_HOST}:5432/{unsafe_db}"
            with self.subTest(unsafe_db=unsafe_db):
                with self.assertRaises(PostgresBackupError) as cm:
                    verify_postgres_restore(
                        self.backup_dir,
                        url,
                        confirm_disposable_target_database=unsafe_db,
                        pg_restore_bin=self.good_restore,
                    )
                self.assertEqual(cm.exception.code, "unsafe_target_database_name")
                self._assert_no_secret_leaked(str(cm.exception))

        # 3. Defect #1 regression: target database name matches source database name
        # via DNS alias, alternate IP (e.g. 127.0.0.2), or different port — must be
        # rejected even when source_database_url is omitted (via manifest.source_database_sha256)
        # and when source_database_url is explicitly passed.
        src_disposable_named = f"postgresql://lab_operator:{SECRET_PW}@127.0.0.1:5432/{TARGET_DB}"
        bdir2 = self.base / "backup_same_dbname"
        with mock.patch("psycopg.connect", return_value=_FakeConnection(TARGET_DB)):
            create_postgres_backup(src_disposable_named, bdir2, pg_dump_bin=self.good_dump)

        for bypass_host in ("localhost", "127.0.0.2", "db-alias.internal.example", "10.0.0.99"):
            bypass_target_url = f"postgresql://lab_operator:{SECRET_PW}@{bypass_host}:5432/{TARGET_DB}"
            with self.subTest(bypass_host=bypass_host):
                # Without explicit source_database_url (caught via manifest.source_database_sha256)
                with self.assertRaises(PostgresBackupError) as cm:
                    verify_postgres_restore(
                        bdir2,
                        bypass_target_url,
                        confirm_disposable_target_database=TARGET_DB,
                        pg_restore_bin=self.good_restore,
                    )
                self.assertEqual(cm.exception.code, "target_matches_source")

                # With explicit source_database_url (caught even before manifest read)
                with self.assertRaises(PostgresBackupError) as cm2:
                    verify_postgres_restore(
                        self.backup_dir,
                        bypass_target_url,
                        confirm_disposable_target_database=TARGET_DB,
                        source_database_url=src_disposable_named,
                        pg_restore_bin=self.good_restore,
                    )
                self.assertEqual(cm2.exception.code, "target_matches_source")

        # 4. Provided source_database_url does not match the archive manifest's source.
        other_src = f"postgresql://lab_operator:{SECRET_PW}@other.host:5432/other_db"
        with self.assertRaises(PostgresBackupError) as cm:
            verify_postgres_restore(
                self.backup_dir,
                TARGET_URL,
                confirm_disposable_target_database=TARGET_DB,
                source_database_url=other_src,
                pg_restore_bin=self.good_restore,
            )
        self.assertEqual(cm.exception.code, "source_endpoint_mismatch")

    def test_post_verification_archive_replacement_or_symlink_swap_race_is_defeated(self) -> None:
        """Defect #2 regression: replacing or symlink-swapping database.pgdump after
        verification must NOT affect pg_restore because pg_restore reads from the
        privately staged unlinked file descriptor rather than reopening by pathname."""
        verified_payload = b"PGDMP-verified-immutable-bytes-12345"
        tampered_payload = b"PGDMP-MALICIOUS-SWAPPED-PAYLOAD-99999"

        dump_bin = _write_executable(
            self.bin_dir / "pg_dump_race",
            f'printf "%s" "{verified_payload.decode("ascii")}"',
        )
        with mock.patch("psycopg.connect", return_value=_FakeConnection("lab_source")):
            manifest = create_postgres_backup(
                SOURCE_URL, self.backup_dir, pg_dump_bin=dump_bin
            )

        archive_path = self.backup_dir / ARCHIVE_FILENAME
        outside_evil = self.base / "evil.pgdump"
        outside_evil.write_bytes(tampered_payload)

        # Simulate an attacker or concurrent process swapping database.pgdump with a
        # symlink to evil.pgdump right when _verify_target_database_empty runs
        # (after archive verification, right before pg_restore --list and restore).
        conns = {
            TARGET_DB: _FakeConnection(TARGET_DB, restored_tables=1, restored_objects=1),
        }

        def connect_and_swap_archive(**kwargs):
            if archive_path.exists() or archive_path.is_symlink():
                archive_path.unlink()
                archive_path.symlink_to(outside_evil)
            return conns[kwargs["dbname"]]

        with mock.patch("psycopg.connect", side_effect=connect_and_swap_archive):
            report = verify_postgres_restore(
                self.backup_dir,
                TARGET_URL,
                confirm_disposable_target_database=TARGET_DB,
                source_database_url=SOURCE_URL,
                pg_restore_bin=self.good_restore,
            )

        self.assertEqual(report.archive_sha256, manifest.archive_sha256)
        # Verify that pg_restore received the original verified bytes, NOT tampered_payload!
        received = self.stdin_log.read_bytes()
        self.assertIn(verified_payload, received)
        self.assertNotIn(tampered_payload, received)

    def test_nonempty_in_use_or_mismatched_target_fails_closed_before_restore(self) -> None:
        with mock.patch(
            "psycopg.connect", return_value=_FakeConnection("lab_source")
        ):
            create_postgres_backup(
                SOURCE_URL, self.backup_dir, pg_dump_bin=self.good_dump
            )

        self.args_log.unlink(missing_ok=True)
        cases = [
            ("wrong_connected_db", _FakeConnection("different_db"), "target_endpoint_mismatch"),
            ("in_recovery_replica", _FakeConnection(TARGET_DB, in_recovery=True), "target_endpoint_mismatch"),
            ("concurrent_active_backend", _FakeConnection(TARGET_DB, other_backends=1), "target_in_use"),
            ("extra_schema", _FakeConnection(TARGET_DB, extra_schemas=1), "target_not_empty"),
            ("existing_table", _FakeConnection(TARGET_DB, user_relations=1), "target_not_empty"),
            ("existing_function", _FakeConnection(TARGET_DB, user_procs=1), "target_not_empty"),
            ("existing_type", _FakeConnection(TARGET_DB, user_types=1), "target_not_empty"),
            ("existing_event_trigger", _FakeConnection(TARGET_DB, event_triggers=1), "target_not_empty"),
            (
                "unexpected_extension",
                _FakeConnection(TARGET_DB, extensions=("plpgsql", "dblink")),
                "target_not_empty",
            ),
        ]
        for label, fake_conn, expected_code in cases:
            with self.subTest(case=label):
                with mock.patch("psycopg.connect", return_value=fake_conn):
                    with self.assertRaises(PostgresBackupError) as cm:
                        verify_postgres_restore(
                            self.backup_dir,
                            TARGET_URL,
                            confirm_disposable_target_database=TARGET_DB,
                            pg_restore_bin=self.good_restore,
                        )
                    self.assertEqual(cm.exception.code, expected_code)
                # pg_restore must NOT have been invoked when emptiness check fails.
                self.assertFalse(self.args_log.exists())

    def test_archive_integrity_tampering_and_manifest_corruption_rejected(self) -> None:
        with mock.patch(
            "psycopg.connect", return_value=_FakeConnection("lab_source")
        ):
            create_postgres_backup(
                SOURCE_URL, self.backup_dir, pg_dump_bin=self.good_dump
            )

        archive_file = self.backup_dir / ARCHIVE_FILENAME
        manifest_file = self.backup_dir / MANIFEST_FILENAME
        orig_archive = archive_file.read_bytes()
        orig_manifest = manifest_file.read_bytes()

        # 1. Corrupt archive byte (same length, bad SHA-256).
        archive_file.write_bytes(orig_archive[:-1] + b"X")
        with self.assertRaises(PostgresBackupError) as cm:
            verify_backup_directory(self.backup_dir)
        self.assertEqual(cm.exception.code, "archive_digest_mismatch")

        # 2. Truncated archive (size mismatch).
        archive_file.write_bytes(orig_archive[:-5])
        with self.assertRaises(PostgresBackupError) as cm:
            verify_backup_directory(self.backup_dir)
        self.assertEqual(cm.exception.code, "archive_size_mismatch")

        # 3. Non-PGDMP header even when manifest hash matches.
        bad_payload = b"SQL--not-custom-format-payload"
        archive_file.write_bytes(bad_payload)
        doc = json.loads(orig_manifest)
        doc["archive_sha256"] = hashlib.sha256(bad_payload).hexdigest()
        doc["archive_size_bytes"] = len(bad_payload)
        manifest_file.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(PostgresBackupError) as cm:
            verify_backup_directory(self.backup_dir)
        self.assertEqual(cm.exception.code, "invalid_archive_header")

        # 4. Duplicate JSON key in manifest.
        archive_file.write_bytes(orig_archive)
        manifest_file.write_bytes(
            b'{"format":"lab-pg-backup-manifest-v1","format":"lab-pg-backup-manifest-v1"}'
        )
        with self.assertRaises(PostgresBackupError) as cm:
            verify_backup_directory(self.backup_dir)
        self.assertEqual(cm.exception.code, "invalid_manifest")

        # 5. Extra unexpected file in backup directory.
        manifest_file.write_bytes(orig_manifest)
        extra = self.backup_dir / "unexpected.txt"
        extra.write_text("extra", encoding="utf-8")
        with self.assertRaises(PostgresBackupError) as cm:
            verify_backup_directory(self.backup_dir)
        self.assertEqual(cm.exception.code, "invalid_backup_directory")
        extra.unlink()

    def test_symlinks_and_unsafe_paths_fail_closed(self) -> None:
        with mock.patch(
            "psycopg.connect", return_value=_FakeConnection("lab_source")
        ):
            # Relative path rejected.
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL, Path("relative/backup"), pg_dump_bin=self.good_dump
                )
            self.assertEqual(cm.exception.code, "unsafe_path")

            # Parent is a symlink rejected.
            real_parent = self.base / "real_parent"
            real_parent.mkdir()
            sym_parent = self.base / "sym_parent"
            sym_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL, sym_parent / "out", pg_dump_bin=self.good_dump
                )
            self.assertEqual(cm.exception.code, "unsafe_path")

            # Explicit executable symlink rejected.
            sym_dump = self.bin_dir / "sym_pg_dump"
            sym_dump.symlink_to(self.good_dump)
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL, self.backup_dir, pg_dump_bin=sym_dump
                )
            self.assertEqual(cm.exception.code, "unsafe_executable")

            # Create valid backup, then replace archive with symlink.
            create_postgres_backup(
                SOURCE_URL, self.backup_dir, pg_dump_bin=self.good_dump
            )

        outside = self.base / "outside.pgdump"
        outside.write_bytes(PGDMP_MAGIC + b"outside")
        archive_path = self.backup_dir / ARCHIVE_FILENAME
        archive_path.unlink()
        archive_path.symlink_to(outside)
        with self.assertRaises(PostgresBackupError) as cm:
            verify_backup_directory(self.backup_dir)
        self.assertEqual(cm.exception.code, "unsafe_path")

    def test_tool_failure_timeout_and_output_overflow_cleanup_destination(self) -> None:
        fail_dump = _write_executable(
            self.bin_dir / "pg_dump_fail",
            f'printf "PGDMP-partial"\nprintf "fatal error: {SECRET_PW}\\n" >&2\nexit 1',
        )
        slow_dump = _write_executable(
            self.bin_dir / "pg_dump_slow",
            "sleep 5",
        )
        noisy_dump = _write_executable(
            self.bin_dir / "pg_dump_noisy",
            'head -c 8192 /dev/zero >&2',
        )
        huge_dump = _write_executable(
            self.bin_dir / "pg_dump_huge",
            'printf "PGDMP"\nhead -c 8192 /dev/zero',
        )

        with mock.patch(
            "psycopg.connect", return_value=_FakeConnection("lab_source")
        ):
            # 1. Non-zero exit removes partial backup_dir and never leaks stderr credentials.
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL, self.backup_dir, pg_dump_bin=fail_dump
                )
            self.assertEqual(cm.exception.code, "tool_failed")
            self._assert_no_secret_leaked(str(cm.exception))
            self.assertFalse(self.backup_dir.exists())

            # 2. Timeout kills child and cleans up backup_dir.
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL,
                    self.backup_dir,
                    limits=BackupLimits(timeout_seconds=0.15),
                    pg_dump_bin=slow_dump,
                )
            self.assertEqual(cm.exception.code, "tool_timeout")
            self.assertFalse(self.backup_dir.exists())

            # 3. Stderr output overflow kills child and cleans up backup_dir.
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL,
                    self.backup_dir,
                    limits=BackupLimits(max_tool_output_bytes=1024),
                    pg_dump_bin=noisy_dump,
                )
            self.assertEqual(cm.exception.code, "output_limit_exceeded")
            self.assertFalse(self.backup_dir.exists())

            # 4. Archive size overflow cleans up backup_dir.
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL,
                    self.backup_dir,
                    limits=BackupLimits(max_archive_bytes=2048),
                    pg_dump_bin=huge_dump,
                )
            self.assertIn(
                cm.exception.code, ("output_limit_exceeded", "size_limit_exceeded")
            )
            self.assertFalse(self.backup_dir.exists())

    def test_failure_cleanup_does_not_remove_replacement_directory(self) -> None:
        original = self.base / "renamed_original"
        def swap_then_fail(*args, **kwargs):
            self.backup_dir.rename(original)
            self.backup_dir.mkdir()
            (self.backup_dir / "keep.txt").write_text("keep", encoding="utf-8")
            raise PostgresBackupError("tool_failed")

        with mock.patch("psycopg.connect", return_value=_FakeConnection("lab_source")), \
             mock.patch("lab_api.postgres_backup._run_bounded_command", side_effect=swap_then_fail):
            with self.assertRaises(PostgresBackupError):
                create_postgres_backup(SOURCE_URL, self.backup_dir, pg_dump_bin=self.good_dump)
        self.assertEqual((self.backup_dir / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertFalse((original / ARCHIVE_FILENAME).exists())

    def test_existing_destination_is_never_overwritten_or_deleted(self) -> None:
        self.backup_dir.mkdir(mode=0o700)
        marker = self.backup_dir / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with mock.patch(
            "psycopg.connect", return_value=_FakeConnection("lab_source")
        ):
            with self.assertRaises(PostgresBackupError) as cm:
                create_postgres_backup(
                    SOURCE_URL, self.backup_dir, pg_dump_bin=self.good_dump
                )
            self.assertEqual(cm.exception.code, "destination_exists")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_cli_subcommands_and_redaction(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        env = {"LAB_BACKUP_SOURCE_DATABASE_URL": f"mysql://u:{SECRET_PW}@h/db"}
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["pg-backup", str(self.backup_dir)])
        self.assertEqual(rc, 1)
        self.assertIn("invalid_database_url", err.getvalue())
        self._assert_no_secret_leaked(out.getvalue() + err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["pg-verify-archive", "relative_dir"])
        self.assertEqual(rc, 1)
        self.assertIn("unsafe_path", err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(
            os.environ, {"LAB_RESTORE_TARGET_DATABASE_URL": TARGET_URL}, clear=True
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["pg-verify-restore", str(self.backup_dir), "wrong_confirm"])
        self.assertEqual(rc, 1)
        self.assertIn("target_confirmation_mismatch", err.getvalue())
        self._assert_no_secret_leaked(out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
