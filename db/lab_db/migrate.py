"""Versioned SQL migrations (SPEC 10.1).

Each file ``NNNN_name.sql`` runs once, inside one transaction together with
its row in ``schema_migrations`` (version, sha256 checksum, deployment id).
A failing migration rolls back completely. An already-applied file whose
checksum changed is refused: migrations are never edited after release.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
LOCK_KEY = 0x1AB0_DB01  # pg_advisory_xact_lock key: one migrator at a time


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        m = NAME.match(path.name)
        if not m:
            raise MigrationError(f"bad migration file name: {path.name}")
        found.append(Migration(int(m.group(1)), path.name, path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must be 1..n without gaps, got {versions}")
    return found


BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    deployment  text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)"""


def migrate(conninfo: str, deployment: str, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations; return the versions applied."""
    migrations = discover() if migrations is None else migrations
    applied_now = []
    with psycopg.connect(conninfo) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            conn.execute(BOOKKEEPING)
            done = {v: c for v, c in conn.execute("SELECT version, checksum FROM schema_migrations")}
        for mig in migrations:
            if mig.version in done:
                if done[mig.version] != mig.checksum:
                    raise MigrationError(f"{mig.name} was changed after being applied (checksum mismatch)")
                continue
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                # Re-check under the lock: another migrator may have applied it.
                row = conn.execute("SELECT checksum FROM schema_migrations WHERE version = %s",
                                   (mig.version,)).fetchone()
                if row is not None:
                    if row[0] != mig.checksum:
                        raise MigrationError(f"{mig.name} was changed after being applied (checksum mismatch)")
                    continue
                conn.execute(mig.sql)
                conn.execute("INSERT INTO schema_migrations (version, name, checksum, deployment) "
                             "VALUES (%s, %s, %s, %s)", (mig.version, mig.name, mig.checksum, deployment))
                applied_now.append(mig.version)
    return applied_now


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url-env", default="LAB_MIGRATION_DATABASE_URL",
                    help="environment variable holding the migration-role URL (never passed on the command line)")
    ap.add_argument("--deployment", required=True)
    args = ap.parse_args(argv)
    import os
    url = os.environ.get(args.database_url_env)
    if not url:
        print(f"{args.database_url_env} is not set", file=sys.stderr)
        return 2
    try:
        applied = migrate(url, args.deployment)
    except (MigrationError, psycopg.Error) as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"applied: {applied or 'none (up to date)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
