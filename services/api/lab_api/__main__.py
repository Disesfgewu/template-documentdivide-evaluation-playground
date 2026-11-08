"""CLI entrypoint for ``lab_api`` settings and PostgreSQL backup/restore verification.

Subcommands:
- ``python -m lab_api check-config``
- ``python -m lab_api pg-backup <backup_dir>`` (reads ``LAB_BACKUP_SOURCE_DATABASE_URL``)
- ``python -m lab_api pg-verify-archive <backup_dir>``
- ``python -m lab_api pg-verify-restore <backup_dir> <confirm_disposable_target_database>``
  (reads ``LAB_RESTORE_TARGET_DATABASE_URL`` and optional ``LAB_BACKUP_SOURCE_DATABASE_URL``)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .config import ConfigError, load_settings
from .postgres_backup import (
    PostgresBackupError,
    create_postgres_backup,
    verify_backup_directory,
    verify_postgres_restore,
)

_USAGE = (
    "usage: python -m lab_api check-config | "
    "pg-backup <backup_dir> | "
    "pg-verify-archive <backup_dir> | "
    "pg-verify-restore <backup_dir> <confirm_disposable_target_database>"
)


def main(argv: list[str]) -> int:
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "check-config" and len(argv) == 1:
        try:
            s = load_settings()
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"config ok: env={s.env.value} provider_mode={s.provider_mode.value}")
        return 0

    if cmd == "pg-backup" and len(argv) == 2:
        src_url = os.environ.get("LAB_BACKUP_SOURCE_DATABASE_URL", "")
        try:
            manifest = create_postgres_backup(src_url, Path(argv[1]))
        except PostgresBackupError as exc:
            print(f"pg-backup failed: {exc.code}", file=sys.stderr)
            return 1
        print(json.dumps(manifest.to_dict(), sort_keys=True))
        return 0

    if cmd == "pg-verify-archive" and len(argv) == 2:
        try:
            manifest = verify_backup_directory(Path(argv[1]))
        except PostgresBackupError as exc:
            print(f"pg-verify-archive failed: {exc.code}", file=sys.stderr)
            return 1
        print(json.dumps(manifest.to_dict(), sort_keys=True))
        return 0

    if cmd == "pg-verify-restore" and len(argv) == 3:
        target_url = os.environ.get("LAB_RESTORE_TARGET_DATABASE_URL", "")
        src_url = os.environ.get("LAB_BACKUP_SOURCE_DATABASE_URL") or None
        try:
            report = verify_postgres_restore(
                Path(argv[1]),
                target_url,
                confirm_disposable_target_database=argv[2],
                source_database_url=src_url,
            )
        except PostgresBackupError as exc:
            print(f"pg-verify-restore failed: {exc.code}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "archive_sha256": report.archive_sha256,
                    "archive_size_bytes": report.archive_size_bytes,
                    "restored_catalog_entry_count": report.restored_catalog_entry_count,
                    "restored_user_table_count": report.restored_user_table_count,
                    "restored_user_object_count": report.restored_user_object_count,
                },
                sort_keys=True,
            )
        )
        return 0

    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
