"""Consumer for ``document.parse`` jobs (PM plan item 5, Refs #7). See docs/PARSE_WORKER.md.

``process_parse_job`` claims a queued job with a fenced lease, reads the document's
original bytes, runs the parser in its resource-bounded child while a heartbeat (on its own
connection) keeps the lease alive and watches for cancellation, and then stores the result
and marks the job succeeded in ONE transaction (``jobs.finish(..., hook=persist)``).

Persist contract: ``persist(conn, project_id, lease, batch)`` runs inside that transaction,
after the lease check, with the job row locked and still ``running`` under ``lease``. It must
be idempotent on (job_id, operation_id) and write only through ``conn``. If it raises, nothing
is stored and the job is failed with ``persist_failed``. There is no default persist, so the
function cannot report success for a result nobody stored.

Failure policy (fixed reason codes, never document or parser text):
* deterministic problems (unsupported profile or page selection, bad PDF, quarantined parser
  output, resource exhaustion, unreadable artifact, failed persist, unexpected error) -> ``failed``;
* transient ones (timeout, process unavailable, I/O) -> ``queued`` while attempts remain, else
  ``failed``. The caller's loop must back off between attempts; there is no not-before column;
* lease lost -> nothing is persisted, the new owner decides (``abandoned``);
* database or connection failure (including inside ``persist``) -> ``abandoned(infrastructure_error)``:
  the job stays ``running`` until its lease expires, then it is retried;
* cancel requested -> the child is stopped and the worker acknowledges (``cancelled``).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
import uuid
from typing import Callable

import psycopg
import psycopg.errors

from lab_db import jobs
from lab_db.storage import LocalArtifactStore
from lab_parser.isolation import InspectionError
from lab_parser.normalize import NormalizedBatch
from lab_parser.batch_parse import PartialParseFailure, parse_in_batches

__all__ = ["ParseOutcome", "SUPPORTED_PROFILE", "process_parse_job"]

SUPPORTED_PROFILE = "unstructured-oss-fast"
_TRANSIENT = {"timeout", "process_unavailable", "inspection_io_failed"}
_MAX_BEAT_INTERVAL = 2.0   # also the worst-case cancel latency, whatever the lease ttl
_BEAT_RETRIES = 3

Persist = Callable[[psycopg.Connection, uuid.UUID, jobs.Lease, NormalizedBatch], None]


@dataclass(frozen=True)
class ParseOutcome:
    job_id: uuid.UUID
    status: str  # succeeded | failed | queued | cancelled | abandoned
    reason: str


class _Heartbeat:
    """Extends the lease on its own connection; trips ``lost`` when the lease is gone or the
    job is no longer running (cancelled or taken over). Transient errors are retried."""

    def __init__(self, connect, project_id, lease, ttl_seconds):
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._connect, self._project, self._lease, self._ttl = connect, project_id, lease, ttl_seconds
        self._interval = max(0.05, min(ttl_seconds / 3, _MAX_BEAT_INTERVAL))
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _beat_once(self, conn):
        failures = 0
        while True:
            try:
                if conn is None:
                    conn = self._connect()
                jobs.heartbeat(conn, self._project, self._lease, self._ttl)
                return conn
            except jobs.LeaseLost:
                raise
            except Exception:
                failures += 1
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                if failures >= _BEAT_RETRIES or self._stop.wait(0.2):
                    raise

    def _run(self):
        conn = None
        try:
            while not self._stop.wait(self._interval):
                conn = self._beat_once(conn)
        except Exception:
            self.lost.set()
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def _fail(conn, project_id, lease, code) -> ParseOutcome:
    jobs.finish(conn, project_id, lease, "failed", code)
    return ParseOutcome(lease.job_id, "failed", code)


# The database or its connection failed: nothing can be written, and a code that says the job
# or the result was bad would be a lie. The job stays 'running' until its lease expires and is
# then retried (or finalized by fail_exhausted).
_INFRASTRUCTURE = (psycopg.OperationalError, psycopg.InterfaceError)
# OperationalError subclasses that say "this statement is the problem", not "the database is gone":
# retrying them cannot help, so they are ordinary (deterministic) failures.
_DETERMINISTIC_DB = (psycopg.errors.QueryCanceled, psycopg.errors.ProgramLimitExceeded)


def is_infrastructure(exc: BaseException) -> bool:
    """A connection-level database failure (connection lost, server down, interface closed)."""
    return isinstance(exc, _INFRASTRUCTURE) and not isinstance(exc, _DETERMINISTIC_DB)


def _unreachable(job_id) -> ParseOutcome:
    return ParseOutcome(job_id, "abandoned", "infrastructure_error")


def _stopped(conn, project_id, job_id) -> ParseOutcome:
    """The lease is gone while we ran: acknowledge a cancel, otherwise leave it to the new owner."""
    try:
        if jobs.acknowledge_cancel(conn, project_id, job_id):
            return ParseOutcome(job_id, "cancelled", "cancel_acknowledged")
    except Exception as exc:
        if is_infrastructure(exc):
            return _unreachable(job_id)  # the cancel is acknowledged by whoever runs next
        raise
    return ParseOutcome(job_id, "abandoned", "lease_lost")


def process_parse_job(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    store: LocalArtifactStore,
    persist: Persist,
    worker: str,
    max_pdf_bytes: int,
    connect: Callable[[], psycopg.Connection],
    ttl_seconds: int = 60,
    python: str | None = None,
    run: Callable[..., NormalizedBatch] = parse_in_batches,
) -> ParseOutcome:
    """Claim and execute one parse job. Raises ``jobs.LeaseUnavailable`` if it is not claimable.

    ``connect`` opens a second connection for the heartbeat thread (``conn`` is never shared
    across threads). Every path after the claim ends in a terminal or requeued job, or in
    ``abandoned`` when the lease is already gone; a job whose lease expired with attempts used
    up is finalized as ``failed(lease_exhausted)`` the next time anyone asks to claim it."""
    try:
        lease = jobs.acquire(conn, project_id, job_id, worker, ttl_seconds)
    except jobs.LeaseUnavailable:
        try:
            jobs.fail_exhausted(conn, project_id, job_id)
        except Exception as exc:
            if not is_infrastructure(exc):
                raise
        raise
    except Exception as exc:
        if is_infrastructure(exc):
            return _unreachable(job_id)  # nothing was claimed; the job is still queued or expired
        raise
    try:
        return _execute(conn, project_id, lease, store, persist, max_pdf_bytes, connect,
                        ttl_seconds, python, run)
    except jobs.LeaseLost:
        return _stopped(conn, project_id, job_id)
    except Exception as exc:
        if is_infrastructure(exc):
            return _unreachable(job_id)
        try:
            return _fail(conn, project_id, lease, "internal_error")
        except jobs.LeaseLost:
            return _stopped(conn, project_id, job_id)
        except Exception as again:
            if is_infrastructure(again):
                return _unreachable(job_id)
            raise


def _execute(conn, project_id, lease, store, persist, max_pdf_bytes, connect, ttl_seconds, python, run):
    job_id = lease.job_id
    with conn.transaction():  # no implicit transaction may stay open across the parse
        row = conn.execute(
            """
            SELECT p.profile, p.page_selection, p.budget_ref, p.operation_id,
                   dv.id, dv.sha256, dv.page_count, a.size_bytes, a.object_key, a.state::text, a.sha256,
                   j.attempt, j.max_attempts
            FROM parse_jobs p
            JOIN jobs j ON j.project_id = p.project_id AND j.id = p.job_id
            JOIN document_versions dv ON dv.project_id = p.project_id AND dv.id = p.document_version_id
            JOIN artifacts a ON a.id = dv.artifact_id
            WHERE p.project_id = %s AND p.job_id = %s
            """,
            (project_id, job_id),
        ).fetchone()
    if row is None:
        return _fail(conn, project_id, lease, "parse_job_missing")
    (profile, selection, budget_ref, operation_id, doc_id, doc_sha, page_count, size, object_key,
     art_state, art_sha, attempt, max_attempts) = row
    if profile != SUPPORTED_PROFILE:
        return _fail(conn, project_id, lease, "profile_unsupported")  # hi_res/MinerU: blocked, not run
    if selection is not None and list(selection) != list(range(page_count)):
        return _fail(conn, project_id, lease, "page_selection_unsupported")
    if art_state != "committed" or art_sha != doc_sha or size > max_pdf_bytes:
        return _fail(conn, project_id, lease, "artifact_unavailable")
    try:
        data = store.open(object_key, expected_size=size, max_bytes=max_pdf_bytes)
    except Exception:
        return _fail(conn, project_id, lease, "artifact_unreadable")
    if hashlib.sha256(data).hexdigest() != doc_sha:
        return _fail(conn, project_id, lease, "artifact_unreadable")

    with _Heartbeat(connect, project_id, lease, ttl_seconds) as beat:
        try:
            batch = run(data, python=python, cancelled=beat.lost.is_set, document_id=str(doc_id),
                        budget_ref=budget_ref or "unbudgeted", operation_id=str(operation_id))
            error = None
        except InspectionError as exc:
            batch, error = None, exc.code
        except PartialParseFailure as exc:
            # Some pages stayed failed after the retry budget: never published as a success. The count
            # goes into the job's transition reason (never page content).
            batch, error = None, f"{exc.code}:{len(exc.failed_pages)}"
    if beat.lost.is_set() or error == "cancelled":
        return _stopped(conn, project_id, job_id)
    if error is not None:
        if error in _TRANSIENT and attempt < max_attempts:
            jobs.finish(conn, project_id, lease, "queued", error)
            return ParseOutcome(job_id, "queued", error)
        return _fail(conn, project_id, lease, error)
    if batch.quarantine_reason is not None:
        return _fail(conn, project_id, lease, "quarantined:" + batch.quarantine_reason)
    try:
        jobs.finish(conn, project_id, lease, "succeeded", "parsed",
                    hook=lambda c: persist(c, project_id, lease, batch))
    except jobs.LeaseLost:
        raise
    except Exception as exc:
        if is_infrastructure(exc):
            raise
        return _fail(conn, project_id, lease, "persist_failed")
    return ParseOutcome(job_id, "succeeded", "parsed")
