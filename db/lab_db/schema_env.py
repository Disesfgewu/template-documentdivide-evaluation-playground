"""The Alembic environment shared by ``schema/env.py`` and test script dirs.

Online mode only: a migration is transactional SQL verified against recorded
checksums, which ``--sql`` offline output cannot enforce.
"""
from __future__ import annotations

import os

from alembic.util.exc import CommandError
from alembic.script import ScriptDirectory

from lab_db.migrate import LOCK_KEY, Migration, MigrationError

VERSION_TABLE = "alembic_version"
_DEPLOYMENT_HELP = "a deployment id is required: alembic -x deployment=<id> upgrade head"


def script_migrations(script: ScriptDirectory) -> list[Migration]:
    """The migrations the script directory applies, in order. Fails unless the
    revisions form one linear chain whose ids match their versions (1..n)."""
    try:
        heads = script.get_heads()
        revisions = list(script.walk_revisions())
    except (KeyError, CommandError) as exc:  # Alembic's own errors for a broken chain
        raise MigrationError(f"the Alembic revision chain is broken: {type(exc).__name__}: {exc}") from exc
    if len(heads) > 1:
        raise MigrationError(f"several Alembic heads {sorted(heads)}: merge or renumber the revisions")
    found: list[Migration] = []
    for rev in reversed(revisions):  # base first
        mig = getattr(rev.module, "MIGRATION", None)
        if not isinstance(mig, Migration):
            raise MigrationError(f"revision {rev.revision} has no MIGRATION")
        expected_down = f"{mig.version - 1:04d}" if mig.version > 1 else None
        if rev.revision != f"{mig.version:04d}" or rev.down_revision != expected_down:
            raise MigrationError(
                f"revision {rev.revision} (down {rev.down_revision}) does not match migration {mig.name}")
        found.append(mig)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must be 1..n without gaps, got {versions}")
    return found


def _sqlalchemy_url(url: str | None) -> str:
    if not url:
        raise MigrationError("no database URL: set LAB_MIGRATION_DATABASE_URL")
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    if url.startswith("postgresql+psycopg://"):
        return url
    raise MigrationError("the migration database URL must be a postgresql:// URL")


def verify_and_adopt(raw, migrations: list[Migration]) -> None:
    """Refuse a database that does not match this code, and stamp one that was
    migrated by the earlier runner. ``raw`` is the psycopg connection."""
    known = {m.version: m for m in migrations}
    if raw.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None:
        return
    rows = raw.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    for version, checksum in rows:
        mig = known.get(version)
        if mig is None:
            raise MigrationError(f"the database has migration {version}, which this code does not contain")
        if mig.checksum != checksum:
            raise MigrationError(f"{mig.name} was changed after being applied (checksum mismatch)")
    stamped = raw.execute(f"SELECT to_regclass('{VERSION_TABLE}')").fetchone()[0] is not None
    if rows and not stamped:
        versions = [v for v, _ in rows]
        if versions != list(range(1, len(versions) + 1)):
            raise MigrationError(f"recorded migrations are not 1..n: {versions}")
        raw.execute(f"CREATE TABLE {VERSION_TABLE} (version_num VARCHAR(32) NOT NULL, "
                    f"CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
        raw.execute(f"INSERT INTO {VERSION_TABLE} (version_num) VALUES (%s)", (f"{versions[-1]:04d}",))


def run() -> None:
    from alembic import context
    from sqlalchemy import create_engine, pool

    config = context.config
    if context.is_offline_mode():
        raise MigrationError("offline (--sql) mode is not supported: migrations are verified against recorded checksums")
    deployment = config.attributes.get("deployment") or context.get_x_argument(as_dictionary=True).get("deployment")
    if not deployment:
        raise MigrationError(_DEPLOYMENT_HELP)
    config.attributes["deployment"] = deployment
    url = _sqlalchemy_url(config.attributes.get("url") or os.environ.get("LAB_MIGRATION_DATABASE_URL"))
    migrations = script_migrations(ScriptDirectory.from_config(config))

    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            raw.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))  # session-level: held across commits
            raw.commit()
            try:
                verify_and_adopt(raw, migrations)
                raw.commit()
                context.configure(connection=connection, version_table=VERSION_TABLE, transaction_per_migration=True)
                with context.begin_transaction():
                    context.run_migrations()
            finally:
                raw.rollback()
                raw.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                raw.commit()
    finally:
        engine.dispose()
