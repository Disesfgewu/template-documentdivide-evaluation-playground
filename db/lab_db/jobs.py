"""Job leases with fencing tokens and fenced graph checkpoints (SPEC 12.3,
16.6). A worker holds a job only while its lease token is the job's current
token and the lease has not expired; every write it makes is checked against
that token, so a stale worker cannot overwrite a newer one."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Optional

import psycopg
from psycopg.types.json import Jsonb


class LeaseUnavailable(Exception):
    """The job is not claimable (not queued, held by a live lease, or max_attempts reached)."""


class ParseJobActive(Exception):
    """The document version already has a queued or running parse job (a new one is refused)."""

    def __init__(self, job_id: uuid.UUID) -> None:
        self.job_id = job_id
        super().__init__(f"parse job {job_id} is already active for this document version")


# A job in one of these states still owns the document version's parse; succeeded, failed
# and cancelled ones do not (a failed one is retried through its own job, not a second job).
ACTIVE_JOB_STATUSES = ("queued", "running", "paused_budget", "waiting_review", "cancel_requested")


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
    hook: Optional[Callable[[psycopg.Connection], None]] = None,
) -> None:
    """running -> succeeded/failed/paused_budget/waiting_review/cancel_requested/queued.
    The DB rejects anything else; leaving 'running' always drops the lease.

    ``hook`` runs inside the same transaction, after the lease check and before the status
    update, with the job row locked and still 'running' under this lease. Whatever it writes
    commits or rolls back together with the status change, so a result can never be stored
    for a job that did not finish, nor a job finish without its result. A hook that raises
    rolls everything back and the exception propagates (the job stays 'running')."""
    with conn.transaction():
        _check(conn, project_id, lease)
        _reason(conn, reason)
        if hook is not None:
            hook(conn)
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


def fail_exhausted(conn: psycopg.Connection, project_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    """Finalize a job stuck 'running' whose lease expired and whose attempts are used up.

    ``acquire`` refuses such a job (attempt + 1 > max_attempts), so without this nobody could
    ever move it. Returns True when it was moved to 'failed'."""
    with conn.transaction():
        _reason(conn, "lease_exhausted")
        row = conn.execute(
            """
            UPDATE jobs
            SET status = 'failed', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                row_version = row_version + 1
            WHERE project_id = %s AND id = %s AND status = 'running'
              AND attempt >= max_attempts AND lease_expires_at < clock_timestamp()
            RETURNING 1
            """,
            (project_id, job_id),
        ).fetchone()
    return row is not None


def acknowledge_cancel(conn: psycopg.Connection, project_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    """cancel_requested -> cancelled, called by the worker once it has stopped its work.

    Returns False when the job is not in 'cancel_requested' (nothing is changed)."""
    with conn.transaction():
        _reason(conn, "cancel_acknowledged")
        row = conn.execute(
            """
            UPDATE jobs SET status = 'cancelled', row_version = row_version + 1
            WHERE project_id = %s AND id = %s AND status = 'cancel_requested'
            RETURNING 1
            """,
            (project_id, job_id),
        ).fetchone()
    return row is not None


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


def enqueue_parse_job(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    operation_id: uuid.UUID,
    document_version_id: uuid.UUID,
    profile: str,
    page_selection: Optional[list[int]] = None,
    budget_ref: Optional[str] = None,
    max_attempts: int = 3,
) -> uuid.UUID:
    """Create a queued ``document.parse`` job bound to its input, in the caller's transaction.

    The operation (kind ``document.parse``, status ``intent``) was created by the
    caller together with its idempotency key; it is marked succeeded here with
    its audit event, so a request is either fully recorded or not at all. The
    job is retryable up to ``max_attempts``. Page indices are checked against
    the document by the database (ascending, inside the page count).
    """
    job_id = uuid.uuid4()
    with conn.transaction():
        # One active parse job per document version: without a cap a client that sends no
        # Idempotency-Key could queue unlimited identical jobs (and later provider cost). The
        # advisory lock serializes concurrent requests for the same version.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"parse_job:{project_id}:{document_version_id}",))
        active = conn.execute(
            "SELECT j.id FROM parse_jobs p JOIN jobs j ON j.project_id = p.project_id AND j.id = p.job_id "
            "WHERE p.project_id = %s AND p.document_version_id = %s AND j.status::text = ANY(%s) "
            "ORDER BY p.created_at LIMIT 1",
            (project_id, document_version_id, list(ACTIVE_JOB_STATUSES)),
        ).fetchone()
        if active is not None:
            raise ParseJobActive(active[0])
        conn.execute(
            "INSERT INTO jobs (id, project_id, kind, stage, max_attempts, retryable) "
            "VALUES (%s, %s, 'document.parse', 'queued', %s, true)",
            (job_id, project_id, max_attempts),
        )
        conn.execute(
            "INSERT INTO parse_jobs (job_id, project_id, document_version_id, profile, page_selection, "
            "budget_ref, operation_id, requested_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (job_id, project_id, document_version_id, profile, page_selection, budget_ref, operation_id, actor_id),
        )
        conn.execute("UPDATE operations SET status = 'succeeded', finished_at = now() WHERE id = %s", (operation_id,))
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, "
            "outcome, operation_id) VALUES (%s, %s, %s, 'parse.requested', 'parse_job', %s, 'succeeded', %s)",
            (uuid.uuid4(), project_id, actor_id, job_id, operation_id),
        )
    return job_id
