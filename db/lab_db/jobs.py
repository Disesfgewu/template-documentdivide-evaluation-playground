"""Job leases with fencing tokens and fenced graph checkpoints (SPEC 12.3,
16.6). A worker holds a job only while its lease token is the job's current
token and the lease has not expired; every write it makes is checked against
that token, so a stale worker cannot overwrite a newer one."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb


class LeaseUnavailable(Exception):
    """The job is not claimable (not queued, held by a live lease, or max_attempts reached)."""


class LeaseLost(Exception):
    """The caller's fencing token is no longer the job's current lease."""


class GraphRevisionChanged(Exception):
    """A thread may not continue under a different workflow revision."""


@dataclass(frozen=True)
class Lease:
    job_id: uuid.UUID
    token: int


def _reason(conn: psycopg.Connection, reason: str) -> None:
    conn.execute("SELECT set_config('lab.transition_reason', %s, true)", (reason,))


def acquire(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    worker: str,
    ttl_seconds: int = 60,
) -> Lease:
    """Claim a queued job, or take over a running job whose lease expired
    (the old token stops working the moment a new one is issued).
    Both queued acquisition and expired-lease takeover increment ``attempt`` by 1
    and require ``attempt + 1 <= max_attempts``."""
    with conn.transaction():
        _reason(conn, f"lease acquired by {worker}")
        row = conn.execute(
            """
            UPDATE jobs
            SET status = 'running',
                lease_owner = %s,
                lease_token = nextval('lease_tokens'),
                lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                row_version = row_version + 1,
                attempt = attempt + 1
            WHERE project_id = %s
              AND id = %s
              AND attempt + 1 <= max_attempts
              AND (status = 'queued' OR (status = 'running' AND lease_expires_at < clock_timestamp()))
            RETURNING lease_token
            """,
            (worker, ttl_seconds, project_id, job_id),
        ).fetchone()
    if row is None:
        raise LeaseUnavailable(str(job_id))
    return Lease(job_id, row[0])


def _check(conn: psycopg.Connection, project_id: uuid.UUID, lease: Lease) -> None:
    """Lock the job row, then validate the lease against wall time.

    Lease grants and checks use ``clock_timestamp()``, never ``now()``:
    ``now()`` is frozen at transaction start, so inside a long caller
    transaction it would accept a lease that already expired on the wall
    clock. The expiry comparison is a separate statement issued after
    ``FOR UPDATE`` returns, because a locking SELECT may compute its target
    list before waiting for the row lock."""
    row = conn.execute(
        """
        SELECT status, lease_token, lease_expires_at
        FROM jobs
        WHERE project_id = %s AND id = %s
        FOR UPDATE
        """,
        (project_id, lease.job_id),
    ).fetchone()
    if row is None or row[0] != "running" or row[1] != lease.token:
        raise LeaseLost(f"job {lease.job_id} token {lease.token}")
    live = conn.execute("SELECT %s::timestamptz > clock_timestamp()", (row[2],)).fetchone()[0]
    if not live:
        raise LeaseLost(f"job {lease.job_id} token {lease.token} (expired)")


def heartbeat(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    lease: Lease,
    ttl_seconds: int = 60,
) -> None:
    with conn.transaction():
        _check(conn, project_id, lease)
        conn.execute(
            """
            UPDATE jobs
            SET lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                row_version = row_version + 1
            WHERE project_id = %s AND id = %s
            """,
            (ttl_seconds, project_id, lease.job_id),
        )


def finish(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    lease: Lease,
    status: str,
    reason: str,
) -> None:
    """running -> succeeded/failed/paused_budget/waiting_review/cancel_requested/queued.
    The DB rejects anything else; leaving 'running' always drops the lease."""
    with conn.transaction():
        _check(conn, project_id, lease)
        _reason(conn, reason)
        conn.execute(
            """
            UPDATE jobs
            SET status = %s,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                row_version = row_version + 1
            WHERE project_id = %s AND id = %s
            """,
            (status, project_id, lease.job_id),
        )


def save_checkpoint(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    lease: Lease,
    thread_id: str,
    workflow_revision: str,
    state_schema_version: int,
    state: dict,
    steps: int,
    last_node: str,
) -> int:
    """Append the next checkpoint for ``thread_id``; returns its sequence.
    Rejected if the lease is stale (fencing) or the revision changed."""
    with conn.transaction():
        _check(conn, project_id, lease)
        thread = conn.execute(
            """
            SELECT workflow_revision, job_id
            FROM graph_threads
            WHERE project_id = %s AND thread_id = %s
            FOR UPDATE
            """,
            (project_id, thread_id),
        ).fetchone()
        if thread is None:
            raise LookupError(f"unknown thread {thread_id}")
        if thread[1] != lease.job_id:
            raise LeaseLost(f"thread {thread_id} belongs to another job")
        if thread[0] != workflow_revision:
            raise GraphRevisionChanged(
                f"thread {thread_id} runs {thread[0]}, not {workflow_revision}"
            )
        seq = conn.execute(
            "SELECT coalesce(max(seq), 0) + 1 FROM graph_checkpoints WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO graph_checkpoints (
                thread_id, seq, lease_token, workflow_revision,
                state_schema_version, state, steps, last_node
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                thread_id,
                seq,
                lease.token,
                workflow_revision,
                state_schema_version,
                Jsonb(state),
                steps,
                last_node,
            ),
        )
    return seq


def load_checkpoint(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    thread_id: str,
) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT c.seq, c.workflow_revision, c.state_schema_version, c.state, c.steps, c.last_node
        FROM graph_checkpoints c
        JOIN graph_threads t ON t.thread_id = c.thread_id
        WHERE t.project_id = %s AND c.thread_id = %s
        ORDER BY c.seq DESC
        LIMIT 1
        """,
        (project_id, thread_id),
    ).fetchone()
    if row is None:
        return None
    keys = ("seq", "workflow_revision", "state_schema_version", "state", "steps", "last_node")
    return dict(zip(keys, row))
