"""Artifact staging, commit verification, tombstone, and orphan GC (SPEC 10.2, T80)."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import uuid

import psycopg
from psycopg import errors, sql


class ArtifactError(Exception):
    """Base error for artifact lifecycle operations."""


class ChecksumMismatch(ArtifactError):
    """Raised when observed object sha256 or size_bytes does not match staged metadata."""


class InvalidState(ArtifactError):
    """Raised when an artifact state transition or argument is invalid for the operation."""


@dataclass(frozen=True)
class GcResult:
    deleted: list[uuid.UUID]
    skipped: list[uuid.UUID]


_FIND_ARTIFACT_FK_REFS_SQL = """
SELECT ns.nspname, cls.relname, a.attname
FROM pg_constraint c
JOIN pg_class cls ON cls.oid = c.conrelid
JOIN pg_namespace ns ON ns.oid = cls.relnamespace
JOIN unnest(c.conkey, c.confkey) AS k(attnum, refattnum) ON true
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum AND NOT a.attisdropped
JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = k.refattnum AND NOT fa.attisdropped
WHERE c.contype = 'f'
  AND c.confrelid = 'artifacts'::regclass
  AND fa.attname = 'id'
ORDER BY ns.nspname, cls.relname, a.attname
"""


def stage(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    object_key: str,
    object_version: str,
    sha256: str,
    size_bytes: int,
    mime: str,
    retention: str = "standard",
) -> uuid.UUID:
    """Insert a new artifact row in 'staged' state and return its UUID."""
    artifact_id = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO artifacts (
                id, project_id, object_key, object_version,
                sha256, size_bytes, mime, state, retention
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'staged', %s)
            """,
            (
                artifact_id,
                project_id,
                object_key,
                object_version,
                sha256,
                size_bytes,
                mime,
                retention,
            ),
        )
    return artifact_id


def commit(
    conn: psycopg.Connection,
    artifact_id: uuid.UUID,
    observed_sha256: str,
    observed_size: int,
) -> None:
    """Verify observed sha256 and size against staged metadata and transition to 'committed'."""
    with conn.transaction():
        row = conn.execute(
            """
            SELECT state, sha256, size_bytes
            FROM artifacts
            WHERE id = %s
            FOR UPDATE
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise InvalidState(f"Artifact {artifact_id} does not exist")

        state, expected_sha256, expected_size = row
        if state != "staged":
            raise InvalidState(
                f"Cannot commit artifact {artifact_id} from state {state!r}; expected 'staged'"
            )

        if observed_sha256 != expected_sha256 or observed_size != expected_size:
            raise ChecksumMismatch(
                f"Artifact {artifact_id} verification failed: "
                f"expected (sha256={expected_sha256!r}, size={expected_size}), "
                f"observed (sha256={observed_sha256!r}, size={observed_size})"
            )

        conn.execute(
            """
            UPDATE artifacts
            SET state = 'committed', committed_at = now()
            WHERE id = %s
            """,
            (artifact_id,),
        )


def collect_staged(
    conn: psycopg.Connection,
    grace: datetime.timedelta,
    now: datetime.datetime | None = None,
) -> GcResult:
    """Delete unreferenced 'staged' artifacts older than (now - grace).

    Discovers all foreign key columns referencing ``artifacts(id)`` dynamically
    from ``pg_catalog``, selects non-``legal_hold`` staged rows older than the
    cutoff with ``FOR UPDATE SKIP LOCKED``, and deletes each row inside its own
    SAVEPOINT so a concurrent reference (``ForeignKeyViolation``) skips only
    that row instead of aborting the batch.
    """
    if grace < datetime.timedelta(0):
        raise ValueError("grace timedelta must be non-negative")

    deleted: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    with conn.transaction():
        fk_refs = conn.execute(_FIND_ARTIFACT_FK_REFS_SQL).fetchall()
        not_exists_clauses = [
            sql.SQL("AND NOT EXISTS (SELECT 1 FROM {tbl} r WHERE r.{col} = a.id)").format(
                tbl=sql.Identifier(nspname, relname),
                col=sql.Identifier(attname),
            )
            for nspname, relname, attname in fk_refs
        ]
        select_query = sql.SQL(
            """
            SELECT a.id
            FROM artifacts a
            WHERE a.state = 'staged'
              AND a.retention <> 'legal_hold'
              AND a.created_at < (coalesce(%s::timestamptz, now()) - %s)
              {not_exists}
            ORDER BY a.created_at, a.id
            FOR UPDATE OF a SKIP LOCKED
            """
        ).format(not_exists=sql.SQL("\n              ").join(not_exists_clauses))

        candidates = [r[0] for r in conn.execute(select_query, (now, grace)).fetchall()]
        for art_id in candidates:
            try:
                with conn.transaction():
                    cur = conn.execute(
                        "DELETE FROM artifacts WHERE id = %s AND state = 'staged' AND retention <> 'legal_hold'",
                        (art_id,),
                    )
                    if cur.rowcount == 1:
                        deleted.append(art_id)
                    else:
                        skipped.append(art_id)
            except errors.ForeignKeyViolation:
                skipped.append(art_id)

    return GcResult(deleted=deleted, skipped=skipped)


def tombstone(
    conn: psycopg.Connection,
    artifact_id: uuid.UUID,
    reason: str,
) -> None:
    """Transition a 'committed' artifact to 'tombstoned' with a non-empty reason."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("tombstone reason must be a non-empty string")

    with conn.transaction():
        row = conn.execute(
            """
            SELECT state
            FROM artifacts
            WHERE id = %s
            FOR UPDATE
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise InvalidState(f"Artifact {artifact_id} does not exist")

        (state,) = row
        if state != "committed":
            raise InvalidState(
                f"Cannot tombstone artifact {artifact_id} from state {state!r}; expected 'committed'"
            )

        conn.execute(
            """
            UPDATE artifacts
            SET state = 'tombstoned',
                tombstoned_at = now(),
                tombstone_reason = %s
            WHERE id = %s
            """,
            (reason, artifact_id),
        )
