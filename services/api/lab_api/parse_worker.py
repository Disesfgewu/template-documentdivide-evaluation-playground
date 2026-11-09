"""Consumer for ``document.parse`` jobs (PM plan item 5, Refs #7).

One blocking call, ``process_parse_job``, claims a queued job with a fenced lease, reads
the document's original bytes, runs the parser in its resource-bounded child while a
heartbeat keeps the lease alive, and hands the normalized batch to ``persist``. The job is
marked succeeded only after ``persist`` returned; there is no default persistence, so the
function cannot report success for a result nobody stored.

Failure policy (fixed reason codes, never document or parser text):
* deterministic problems (unsupported profile or page selection, bad PDF, parser output
  quarantined, resource exhaustion, artifact unreadable, persistence failed) -> ``failed``;
* transient ones (timeout, process unavailable, I/O) -> back to ``queued`` while attempts
  remain, else ``failed``;
* lease lost or the job cancelled while running -> nothing is persisted and the job is left
  to whoever now owns it.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
import uuid
from typing import Callable

import psycopg

from lab_db import jobs
from lab_db.storage import LocalArtifactStore
from lab_parser.isolation import InspectionError
from lab_parser.normalize import NormalizedBatch
from lab_parser.unstructured_runner import run_unstructured_fast

__all__ = ["ParseOutcome", "SUPPORTED_PROFILE", "process_parse_job"]

SUPPORTED_PROFILE = "unstructured-oss-fast"
_TRANSIENT = {"timeout", "process_unavailable", "inspection_io_failed"}

Persist = Callable[[psycopg.Connection, uuid.UUID, jobs.Lease, NormalizedBatch], None]


@dataclass(frozen=True)
class ParseOutcome:
    job_id: uuid.UUID
    status: str  # succeeded | failed | queued | abandoned
    reason: str


class _Heartbeat:
    """Keeps the lease alive; trips ``lost`` when the lease is gone or the job was cancelled."""

    def __init__(self, conn, project_id, lease, ttl_seconds):
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._conn, self._project, self._lease, self._ttl = conn, project_id, lease, ttl_seconds
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(max(0.05, self._ttl / 3)):
            try:
                jobs.heartbeat(self._conn, self._project, self._lease, self._ttl)
            except jobs.LeaseLost:
                self.lost.set()  # includes cancel_requested: the job is no longer running
                return
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def _fail(conn, project_id, lease, code) -> ParseOutcome:
    jobs.finish(conn, project_id, lease, "failed", code)
    return ParseOutcome(lease.job_id, "failed", code)


def process_parse_job(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    store: LocalArtifactStore,
    persist: Persist,
    worker: str,
    max_pdf_bytes: int,
    ttl_seconds: int = 60,
    python: str | None = None,
    run: Callable[..., NormalizedBatch] = run_unstructured_fast,
) -> ParseOutcome:
    """Claim and execute one parse job. Raises ``jobs.LeaseUnavailable`` if it is not claimable."""
    lease = jobs.acquire(conn, project_id, job_id, worker, ttl_seconds)
    with conn.transaction():  # never leave an implicit transaction open under the heartbeat thread
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

    with _Heartbeat(conn, project_id, lease, ttl_seconds) as beat:
        try:
            batch = run(data, python=python, cancelled=beat.lost.is_set, document_id=str(doc_id),
                        budget_ref=budget_ref or "unbudgeted", operation_id=str(operation_id))
            error = None
        except InspectionError as exc:
            batch, error = None, exc.code
    if beat.lost.is_set() or error == "cancelled":
        return ParseOutcome(job_id, "abandoned", "lease_lost_or_cancelled")
    if error is not None:
        if error in _TRANSIENT and attempt < max_attempts:
            jobs.finish(conn, project_id, lease, "queued", error)
            return ParseOutcome(job_id, "queued", error)
        return _fail(conn, project_id, lease, error)
    if batch.quarantine_reason is not None:
        return _fail(conn, project_id, lease, "quarantined:" + batch.quarantine_reason)
    try:
        persist(conn, project_id, lease, batch)
    except jobs.LeaseLost:
        return ParseOutcome(job_id, "abandoned", "lease_lost_or_cancelled")
    except Exception:
        return _fail(conn, project_id, lease, "persist_failed")
    jobs.finish(conn, project_id, lease, "succeeded", "parsed")
    return ParseOutcome(job_id, "succeeded", "parsed")
