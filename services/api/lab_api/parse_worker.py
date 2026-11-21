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
import logging
import threading
import time
import uuid
from typing import Callable

import psycopg
import psycopg.errors

from lab_db import jobs, parse_staging as ps
from lab_db.storage import LocalArtifactStore
from lab_parser.isolation import InspectionError
from lab_parser.normalize import NormalizedBatch
from lab_parser.batch_parse import PartialParseFailure, StagedParse, parse_in_batches
from .parse_staged import StagingSinks, build_header, identity_of
from .app_log import app_traced, log_op, log_alarm, log_error, ContextScope, log_page_statuses

LOG = logging.getLogger("lab.parse_worker")

__all__ = ["ParseOutcome", "SUPPORTED_PROFILE", "process_parse_job"]

SUPPORTED_PROFILE = "unstructured-oss-fast"
_TRANSIENT = {"timeout", "process_unavailable", "inspection_io_failed", "text_extraction_empty"}
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
    jobs.finish(conn, project_id, lease, "failed", code, stage=_stage_for(code))
    _discard_if_terminal(conn, project_id, lease.job_id)         # a failed job's partial pages must not stay behind
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
            _discard_if_terminal(conn, project_id, job_id)
            return ParseOutcome(job_id, "cancelled", "cancel_acknowledged")
    except Exception as exc:
        if is_infrastructure(exc):
            return _unreachable(job_id)  # the cancel is acknowledged by whoever runs next
        raise
    return ParseOutcome(job_id, "abandoned", "lease_lost")


@app_traced(stage="parse", is_stage=True)
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
    shutdown: Callable[[], bool] | None = None,
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
                        ttl_seconds, python, run, shutdown)
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


def _accepts_sinks(run) -> bool:
    """True when ``run`` names the staging sinks explicitly (the real ``parse_in_batches`` does; a test double with ``**kw`` is not given them)."""
    import inspect
    if hasattr(run, "accepts_sinks"):
        return bool(run.accepts_sinks)
    try:
        params = inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
    return {"page_sink", "run_sink", "staged"} <= set(params)


def _discard_if_terminal(conn, project_id, job_id) -> None:
    """Delete a job's staging when the job has ended (failed or cancelled). A job that is queued or running keeps it: the next attempt resumes."""
    try:
        with conn.transaction():
            status = conn.execute("SELECT status::text FROM jobs WHERE project_id = %s AND id = %s", (project_id, job_id)).fetchone()
        if status is not None and status[0] in ("failed", "cancelled", "succeeded"):
            n = ps.discard_staging(conn, job_id)
            if n:
                log_op("staging_discarded", stage="parse", job_id=str(job_id), pages=n, job_status=status[0])
    except Exception as exc:  # noqa: BLE001 - never change the outcome; the orphan sweep removes it later
        LOG.warning("job %s: staging discard failed (%s); the orphan sweep will remove it", job_id, type(exc).__name__)


_STAGE_OF = {"page_write_failed": "storing", "page_too_large": "storing", "persist_failed": "finishing", "staging_incomplete": "finishing",
             "artifact_unavailable": "loading", "artifact_unreadable": "loading", "pages_failed": "parsing"}


def _stage_for(code: str) -> str:
    return _STAGE_OF.get(code.split(":")[0], "parsing")


def _finish_staged(conn, project_id, lease, store, staged, operation_id, page_count, connect, ttl_seconds) -> ParseOutcome:
    """The atomic succeeded transition of a staged parse: build the header from what is staged, then one transaction copies the staged pages into the final
    append-only tables (server side), commits the raw outputs as artifacts, deletes the staging and marks the job succeeded. A heartbeat keeps the lease
    alive meanwhile (the copy, the artifact commits and the hash pass are bounded but not instant)."""
    job_id = lease.job_id
    started = time.monotonic()
    try:
        jobs.set_stage(conn, project_id, lease, "finishing")
        with _Heartbeat(connect, project_id, lease, ttl_seconds):
            header = build_header(conn, job_id, operation_id, page_count)
            info: dict = {}
            jobs.finish(conn, project_id, lease, "succeeded", "parsed", stage="done",
                        hook=lambda c: info.update(ps.finish_from_staging(c, store, project_id, lease, header, page_count)))
    except jobs.LeaseLost:
        raise
    except Exception as exc:
        if is_infrastructure(exc):
            raise
        try:
            diag = getattr(exc, "diag", None)
            log_error("finish_from_staging", type(exc).__name__, "persist_failed", stage="parse", job_id=str(job_id), sqlstate=getattr(exc, "sqlstate", None),
                      constraint=getattr(diag, "constraint_name", None), table=getattr(diag, "table_name", None), pages=page_count)
        except Exception:  # noqa: BLE001
            pass
        return _fail(conn, project_id, lease, "staging_incomplete" if isinstance(exc, ps.StagingIncomplete) else "persist_failed")
    log_op("parse_finished", stage="parse", job_id=str(job_id), pages=info.get("pages"), bytes=info.get("bytes"), raw_artifacts=info.get("raw_artifacts"),
           finish_seconds=round(time.monotonic() - started, 2), record_sha256=info.get("record_sha256"))
    return ParseOutcome(job_id, "succeeded", "parsed")


def _execute(conn, project_id, lease, store, persist, max_pdf_bytes, connect, ttl_seconds, python, run,
             shutdown=None):
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

    staged_mode = _accepts_sinks(run)
    sinks = StagingSinks(connect, project_id, lease) if staged_mode else None
    batch, error = None, None
    try:
        if staged_mode:
            jobs.set_stage(conn, project_id, lease, "parsing")
        with _Heartbeat(connect, project_id, lease, ttl_seconds) as beat:
            for attempt_of_staging in (1, 2):
                extra = {}
                if staged_mode:
                    resume = ps.begin(conn, project_id, lease, identity_of(doc_sha, profile, operation_id, page_count))
                    staged, staged_obs = ps.staged_summary(conn, job_id) if resume.resumed else ({}, False)
                    if resume.resumed:
                        log_op("parse_resumed", stage="parse", job_id=str(job_id), pages_already_staged=len(staged), pages_total=page_count)
                    extra = {"page_sink": sinks.page, "run_sink": sinks.run, "staged": staged, "staged_any_observation": staged_obs}
                try:
                    batch = run(data, python=python, cancelled=beat.lost.is_set, document_id=str(doc_id),
                                budget_ref=budget_ref or "unbudgeted", operation_id=str(operation_id), **extra)
                    error = None
                except InspectionError as exc:
                    batch, error = None, exc.code
                except PartialParseFailure as exc:
                    # Some pages stayed failed after the retry budget: never published as a success. The count
                    # goes into the job's transition reason (never page content).
                    batch, error = None, f"{exc.code}:{len(exc.failed_pages)}"
                except ps.StagingConflict:
                    # a page came out different from what an earlier attempt stored: the stored pages cannot be trusted. Start again from page 0, once.
                    n = ps.discard_staging(conn, job_id)
                    log_alarm("parse_staging", "staging_conflict_restart", stage="parse", job_id=str(job_id), pages_discarded=n, attempt=attempt_of_staging)
                    if attempt_of_staging == 1:
                        continue
                    batch, error = None, "staging_conflict"
                except Exception as exc:  # noqa: BLE001 - a page write failed (already logged with the real class by the sink)
                    if not staged_mode or is_infrastructure(exc) or isinstance(exc, jobs.LeaseLost):
                        raise
                    batch, error = None, "page_too_large" if str(exc) == "page_too_large" else f"page_write_failed:{type(exc).__name__}"
                break
    finally:
        if sinks is not None:
            sinks.close()
    if beat.lost.is_set() or error == "cancelled":
        if shutdown is not None and shutdown() and not beat.lost.is_set():
            # The worker is shutting down and its parser children are already cancelled and reaped (the run
            # returned): give the job back at once instead of leaving it to the lease expiry. Only the current
            # holder can (fenced); a user cancel wins (LeaseLost -> _stopped acknowledges it); any other failure
            # falls back to the old behaviour, lease expiry.
            try:
                if jobs.release_for_shutdown(conn, project_id, lease) == "failed":
                    return ParseOutcome(job_id, "failed", "lease_exhausted")   # the last attempt was interrupted
                return ParseOutcome(job_id, "queued", "released: worker shutdown")
            except jobs.LeaseLost:
                LOG.info("job %s: shutdown release refused (lease lost or user cancel)", job_id)
            except Exception as exc:
                if not is_infrastructure(exc):
                    raise
                LOG.warning("job %s: shutdown release failed (infrastructure); the lease will expire", job_id)
        return _stopped(conn, project_id, job_id)
    if error is not None:
        if error in _TRANSIENT and attempt < max_attempts:
            jobs.finish(conn, project_id, lease, "queued", error)
            return ParseOutcome(job_id, "queued", error)
        return _fail(conn, project_id, lease, error)
    if isinstance(batch, StagedParse):
        return _finish_staged(conn, project_id, lease, store, batch, operation_id, page_count, connect, ttl_seconds)
    log_page_statuses(batch)
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
        # WHY it failed, without any document content: the exception class, and for a database error its SQLSTATE and constraint name.
        try:
            diag = getattr(exc, "diag", None)
            log_error("persist_parse_result", type(exc).__name__, "persist_failed", stage="parse", job_id=str(job_id),
                      sqlstate=getattr(exc, "sqlstate", None), constraint=getattr(diag, "constraint_name", None),
                      table=getattr(diag, "table_name", None), pages=len(getattr(getattr(batch, "result", None), "pages", ()) or ()),
                      evidence_units=len(getattr(batch, "evidence", ()) or ()))
        except Exception:  # noqa: BLE001 - the log line must never change the outcome
            pass
        from lab_db.parse_results import ParseResultTooLarge
        # a too-large result is its own, readable reason (the page shows the code), not the generic persist failure
        return _fail(conn, project_id, lease, "result_too_large" if isinstance(exc, ParseResultTooLarge) else "persist_failed")
    return ParseOutcome(job_id, "succeeded", "parsed")
