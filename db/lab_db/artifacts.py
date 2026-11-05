"""Artifact staging, commit verification, tombstone, and orphan GC (SPEC 10.2, T80)."""

from __future__ import annotations

import datetime
import uuid

import psycopg


class ArtifactError(Exception):
    """Base error for artifact lifecycle operations."""


class ChecksumMismatch(ArtifactError):
    """Raised when observed object sha256 or size_bytes does not match staged metadata."""


class InvalidState(ArtifactError):
    """Raised when an artifact state transition or argument is invalid for the operation."""


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
) -> list[uuid.UUID]:
    """Delete unreferenced 'staged' artifacts older than (now - grace) and return deleted IDs."""
    if grace < datetime.timedelta(0):
        raise ValueError("grace timedelta must be non-negative")

    with conn.transaction():
        # A concurrent writer that references a row we are deleting makes the
        # whole statement fail (FK RESTRICT); the caller simply retries later.
        rows = conn.execute(
            """
            DELETE FROM artifacts a
            WHERE a.state = 'staged'
              AND a.created_at < (coalesce(%s::timestamptz, now()) - %s)
              AND NOT EXISTS (SELECT 1 FROM document_versions dv WHERE dv.artifact_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.payload_artifact_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM node_operations nop WHERE nop.result_artifact_id = a.id)
            RETURNING a.id
            """,
            (now, grace),
        ).fetchall()
    return [r[0] for r in rows]


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
