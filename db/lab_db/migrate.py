"""Versioned SQL migrations (SPEC 10.1), run by Alembic.

The SQL files in ``migrations/`` stay the source of truth. Each has one thin
Alembic revision in ``schema/versions/`` that applies it, so the standard
``alembic upgrade head`` works and the guarantees of the original runner are
kept: every migration runs in its own transaction together with its row in
``schema_migrations`` (version, sha256 checksum, deployment id); a failing
migration rolls back completely; one migrator runs at a time (advisory lock);
an already-applied file whose checksum changed is refused. Migrations are
forward-only: there is no downgrade, restore from backup instead.

A database migrated by the earlier runner (``schema_migrations`` but no
``alembic_version``) is adopted: its checksums are verified and it is stamped
at its current version; nothing is re-run.

Run (PostgreSQL in Docker: ``docker compose -f deploy/compose.dev.yml up -d --wait``):
``cd db && LAB_MIGRATION_DATABASE_URL=postgresql://... alembic -x deployment=<id> upgrade head``
or ``PYTHONPATH=db python -m lab_db.migrate --deployment <id>``.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
LOCK_KEY = 0x1AB0_DB01  # pg_advisory_lock key: one migrator at a time


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


def read_migration(name: str, directory: Path = MIGRATIONS) -> Migration:
    """The Migration for one SQL file name (used by the revision files)."""
    m = NAME.match(name)
    if not m:
        raise MigrationError(f"bad migration file name: {name}")
    return Migration(int(m.group(1)), name, (directory / name).read_text(encoding="utf-8"))


def apply_migration(mig: Migration) -> None:
    """Called from a revision's ``upgrade()``: run the SQL and record it on the
    Alembic transaction of this one migration."""
    from alembic import context, op

    raw = op.get_bind().connection.driver_connection  # the psycopg connection
    deployment = context.config.attributes["deployment"]
    raw.execute(BOOKKEEPING)
    raw.execute(mig.sql)
    raw.execute(
        "INSERT INTO schema_migrations (version, name, checksum, deployment) VALUES (%s, %s, %s, %s)",
        (mig.version, mig.name, mig.checksum, deployment),
    )


_REVISION_TEMPLATE = '''"""{name}"""
from lab_db.migrate import Migration, apply_migration

revision = "{rev}"
down_revision = {down}
branch_labels = None
depends_on = None

MIGRATION = Migration({version}, {name!r}, {sql!r})


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1)")
'''


def _ephemeral_script_dir(migrations: list[Migration], target: Path) -> None:
    """An Alembic script directory holding exactly ``migrations`` (tests use it
    to apply a prefix or a modified set)."""
    (target / "versions").mkdir(parents=True)
    (target / "env.py").write_text("from lab_db.schema_env import run\n\nrun()\n", encoding="utf-8")
    for mig in migrations:
        rev = f"{mig.version:04d}"
        down = repr(f"{mig.version - 1:04d}") if mig.version > 1 else "None"
        (target / "versions" / f"{rev}.py").write_text(
            _REVISION_TEMPLATE.format(name=mig.name, rev=rev, down=down, version=mig.version, sql=mig.sql),
            encoding="utf-8",
        )


def _recorded_versions(conninfo: str) -> set[int]:
    with psycopg.connect(conninfo) as conn:
        if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None:
            return set()
        return {v for (v,) in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conninfo: str, deployment: str, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations with Alembic; return the versions applied.

    ``migrations=None`` uses the repository's revisions; a list builds a
    temporary script directory from exactly those migrations.
    """
    from alembic import command
    from alembic.config import Config

    before = _recorded_versions(conninfo)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        if migrations is None:
            cfg.set_main_option("script_location", str(SCHEMA_DIR))
        else:
            ephemeral = Path(tmp) / "schema"
            _ephemeral_script_dir(sorted(migrations, key=lambda m: m.version), ephemeral)
            cfg.set_main_option("script_location", str(ephemeral))
        cfg.attributes.update(url=conninfo, deployment=deployment)
        command.upgrade(cfg, "head")
    return sorted(_recorded_versions(conninfo) - before)


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
