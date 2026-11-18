"""Per-page staging of a parse job's result (Issue #205, PM-DECISION GO on #204). See db/lab_db/migrations/0025_parse_staging.sql.

* ``stage_page`` / ``stage_run`` store ONE page / ONE batch run in a short transaction of their own, fenced by the job's lease.
* Nothing in staging is ever read by a reader of parse results. ``finish_from_staging`` (the ``jobs.finish`` hook) copies it, server side, into the final
  append-only tables inside the job's atomic ``succeeded`` transition and deletes it; a failed or cancelled job's staging is deleted by ``discard_staging``
  (and by ``sweep_orphans`` for a crash in between). So storage cannot grow without bound from retries.
* ``begin`` decides RESUME vs RESTART by an exact identity check; a page that is stored again must have the same ``page_sha256`` or the whole staging is
  discarded (no mixed attempts).
* ``record_sha256`` is computed INCREMENTALLY, byte for byte what ``lab_db.parse_results._canonical`` produced for the whole record (key order
  capabilities < manifest < pages < result), so the identity of a result does not change and memory stays O(one page)."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Iterable, Optional

import psycopg
from psycopg.types.json import Jsonb

from .jobs import Lease, LeaseLost
from .storage import LocalArtifactStore, store_and_register

__all__ = ["StagingIdentityMismatch", "StagingConflict", "StagingIncomplete", "Resume", "begin", "stage_page", "stage_run", "progress", "staged_pages", "staged_summary",
           "finish_from_staging", "discard_staging", "sweep_orphans", "page_digest", "MAX_PAGE_BYTES", "ORPHAN_HOURS"]

MAX_PAGE_BYTES = 32 * 1024 * 1024        # canonical JSON of ONE page; the database also caps each JSON column at 16 MiB
MAX_RAW_ARTIFACTS = 20_000
ORPHAN_HOURS = 24


class StagingConflict(Exception):
    """A page was stored again with different content: the staging cannot be trusted and must be discarded."""


class StagingIdentityMismatch(Exception):
    """The staging belongs to another parse identity (document, profile, adapter, batch size, operation, page count)."""


class StagingIncomplete(Exception):
    """Finish was asked for before every page 0..pages_total-1 is staged."""


@dataclass(frozen=True)
class Resume:
    resumed: bool                     # True: pages of an earlier attempt of THIS job with the same identity were kept
    pages: frozenset[int]             # the page indexes already staged (empty after a restart)


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def page_digest(page_index: int, status: str, diagnostics: list, evidence: list, observations: list) -> tuple[str, bytes]:
    """(sha256, canonical bytes) of one page, in the same form ``parse_results._canonical`` uses for an element of ``pages``."""
    data = _dumps([page_index, status, diagnostics, evidence, observations]).encode("utf-8")
    return hashlib.sha256(data).hexdigest(), data


def _fence(conn: psycopg.Connection, project_id: uuid.UUID, lease: Lease) -> None:
    row = conn.execute("SELECT status::text, lease_token FROM jobs WHERE project_id = %s AND id = %s", (project_id, lease.job_id)).fetchone()
    if row is None or row[0] != "running" or row[1] != lease.token:
        raise LeaseLost(f"job {lease.job_id} is not running under token {lease.token}")


def begin(conn: psycopg.Connection, project_id: uuid.UUID, lease: Lease, identity: dict) -> Resume:
    """Open (or reopen) the staging of a job. Same identity: RESUME with the pages already stored. Anything else: discard it and RESTART."""
    with conn.transaction():
        _fence(conn, project_id, lease)
        row = conn.execute("SELECT identity FROM parse_staging WHERE job_id = %s FOR UPDATE", (lease.job_id,)).fetchone()
        if row is not None and row[0] == json.loads(_dumps(identity)):
            conn.execute("UPDATE parse_staging SET touched_at = now() WHERE job_id = %s", (lease.job_id,))
            pages = frozenset(p for (p,) in conn.execute("SELECT page_index FROM parse_staging_pages WHERE job_id = %s", (lease.job_id,)))
            return Resume(bool(pages), pages)
        if row is not None:
            conn.execute("DELETE FROM parse_staging WHERE job_id = %s", (lease.job_id,))          # cascades to pages and runs
        conn.execute("INSERT INTO parse_staging (job_id, project_id, identity) VALUES (%s, %s, %s)", (lease.job_id, project_id, Jsonb(json.loads(_dumps(identity)))))
        return Resume(False, frozenset())


def stage_page(conn: psycopg.Connection, project_id: uuid.UUID, lease: Lease, page_index: int, status: str, diagnostics: list, evidence: list,
               observations: list, run_key: Optional[str] = None) -> int:
    """Store one FINAL page in its own short transaction; returns its canonical size in bytes. A page already stored must have the same digest."""
    digest, data = page_digest(page_index, status, diagnostics, evidence, observations)
    if len(data) > MAX_PAGE_BYTES:
        raise ValueError("page_too_large")
    with conn.transaction():
        _fence(conn, project_id, lease)
        conn.execute(
            "INSERT INTO parse_staging_pages (job_id, page_index, status, diagnostics, evidence, observations, run_key, page_sha256, page_bytes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (job_id, page_index) DO NOTHING",
            (lease.job_id, page_index, status, Jsonb(diagnostics), Jsonb(evidence), Jsonb(observations), run_key, digest, len(data)))
        stored = conn.execute("SELECT page_sha256 FROM parse_staging_pages WHERE job_id = %s AND page_index = %s", (lease.job_id, page_index)).fetchone()[0]
        if stored != digest:
            raise StagingConflict(f"page {page_index} was staged with other content")
        conn.execute("UPDATE parse_staging SET touched_at = now() WHERE job_id = %s", (lease.job_id,))
    return len(data)


def stage_run(conn: psycopg.Connection, project_id: uuid.UUID, lease: Lease, run_key: str, manifest: dict, capabilities: dict, raw: Iterable[bytes],
              raw_result_refs: Iterable[str] = ()) -> None:
    """Store the small manifest of one batch run and its raw upstream outputs (kept here until the job succeeds). A run stored again keeps the first copy."""
    raw = list(raw)
    with conn.transaction():
        _fence(conn, project_id, lease)
        conn.execute("INSERT INTO parse_staging_runs (job_id, run_key, manifest, capabilities, raw_result_refs, raw) VALUES (%s, %s, %s, %s, %s, %s) "
                     "ON CONFLICT (job_id, run_key) DO NOTHING", (lease.job_id, run_key, Jsonb(manifest), Jsonb(capabilities), Jsonb(list(raw_result_refs)), raw))


def progress(conn: psycopg.Connection, job_id: uuid.UUID) -> Optional[tuple[int, int]]:
    """(pages staged, pages in the document) or None when the job has no staging (never started, finished, or discarded)."""
    with conn.transaction():
        row = conn.execute("SELECT (SELECT count(*) FROM parse_staging_pages p WHERE p.job_id = s.job_id), (s.identity->>'pages_total')::int "
                           "FROM parse_staging s WHERE s.job_id = %s", (job_id,)).fetchone()
    return None if row is None else (int(row[0]), int(row[1]))


def staged_summary(conn: psycopg.Connection, job_id: uuid.UUID) -> tuple[dict, bool]:
    """({page_index: (status, diagnostics)}, whether any staged page has an observation): what a RESUMED parse needs to know about the stored pages."""
    with conn.transaction():          # no implicit transaction may stay open on the caller's connection across a long parse
        pages = {i: (st, d) for i, st, d in conn.execute("SELECT page_index, status, diagnostics FROM parse_staging_pages WHERE job_id = %s", (job_id,))}
        any_obs = conn.execute("SELECT EXISTS (SELECT 1 FROM parse_staging_pages WHERE job_id = %s AND jsonb_array_length(observations) > 0)", (job_id,)).fetchone()[0]
    return pages, bool(any_obs)


def staged_pages(conn: psycopg.Connection, job_id: uuid.UUID) -> frozenset[int]:
    with conn.transaction():
        return frozenset(p for (p,) in conn.execute("SELECT page_index FROM parse_staging_pages WHERE job_id = %s", (job_id,)))


def finish_from_staging(conn: psycopg.Connection, store: LocalArtifactStore, project_id: uuid.UUID, lease: Lease, header: dict, pages_total: int) -> dict:
    """The ``jobs.finish`` hook of a parse job: runs inside the atomic succeeded transition. ``header`` = {manifest, capabilities, result} WITHOUT pages
    (``result`` may name the pages' statuses, see the caller). Returns {"pages", "bytes", "raw_artifacts", "record_sha256"}."""
    _fence(conn, project_id, lease)
    job = conn.execute("SELECT document_version_id, operation_id FROM parse_jobs WHERE project_id = %s AND job_id = %s", (project_id, lease.job_id)).fetchone()
    if job is None:
        raise ValueError("job has no parse_jobs row")
    document_version_id, operation_id = job
    have = conn.execute("SELECT count(*), min(page_index), max(page_index) FROM parse_staging_pages WHERE job_id = %s", (lease.job_id,)).fetchone()
    if have[0] != pages_total or have[1] != 0 or have[2] != pages_total - 1:
        raise StagingIncomplete(f"{have[0]} of {pages_total} pages are staged")
    for key in ("manifest", "capabilities", "result"):
        if key not in header:
            raise ValueError("header needs manifest, capabilities and result")
    if "pages" in header["result"]:
        raise ValueError("the header result carries no pages")
    # raw outputs: one pass, commit each as an artifact (content-addressed: identical bytes are one artifact)
    artifact_ids: list[uuid.UUID] = []
    seen: dict[str, uuid.UUID] = {}
    raw_count = 0
    with conn.cursor(name="parse_staging_raw") as runs:          # server side, one run's raw outputs in memory at a time
        runs.itersize = 1
        runs.execute("SELECT raw FROM parse_staging_runs WHERE job_id = %s ORDER BY run_key", (lease.job_id,))
        for (blobs,) in runs:
            for blob in blobs:
                raw_count += 1
                if raw_count > MAX_RAW_ARTIFACTS:
                    raise ValueError("too many raw artifacts")
                sha = hashlib.sha256(bytes(blob)).hexdigest()
                if sha not in seen:
                    seen[sha] = store_and_register(conn, store, project_id, bytes(blob), "application/json")
                    artifact_ids.append(seen[sha])
    for extra in header.get("extra_artifacts", ()):       # e.g. the merged manifest, which is not a batch output
        sha = hashlib.sha256(extra).hexdigest()
        if sha not in seen:
            seen[sha] = store_and_register(conn, store, project_id, extra, "application/json")
            artifact_ids.append(seen[sha])
    if not artifact_ids:
        raise ValueError("a parse result needs at least one raw artifact")
    # the record hash, incrementally, in the canonical form of the old whole-record hash; one page in memory at a time
    statuses = [{"page_index": i, "status": st, "diagnostics": d} for i, st, d in
                conn.execute("SELECT page_index, status, diagnostics FROM parse_staging_pages WHERE job_id = %s ORDER BY page_index", (lease.job_id,))]
    result_with_pages = {**header["result"], "pages": statuses}          # the old whole-record hash covered the result WITH its page statuses
    h = hashlib.sha256()
    h.update(('{"capabilities":' + _dumps(header["capabilities"]) + ',"manifest":' + _dumps(header["manifest"]) + ',"pages":[').encode("utf-8"))
    total_bytes, first = 0, True
    with conn.cursor(name="parse_staging_read") as cur:
        cur.itersize = 1
        cur.execute("SELECT page_index, status, diagnostics, evidence, observations FROM parse_staging_pages WHERE job_id = %s ORDER BY page_index", (lease.job_id,))
        for index, status, diagnostics, evidence, observations in cur:
            _, data = page_digest(index, status, diagnostics, evidence, observations)
            h.update(data if first else b"," + data)
            first = False
            total_bytes += len(data)
    h.update(("],\"result\":" + _dumps(result_with_pages) + "}").encode("utf-8"))
    digest = h.hexdigest()
    existing = conn.execute("SELECT record_sha256 FROM parse_results WHERE job_id = %s", (lease.job_id,)).fetchone()
    if existing is not None:
        if existing[0] != digest:
            raise StagingConflict("a different result is already stored for this job")
        conn.execute("DELETE FROM parse_staging WHERE job_id = %s", (lease.job_id,))
        return {"pages": pages_total, "bytes": total_bytes, "raw_artifacts": len(artifact_ids), "record_sha256": digest}
    conn.execute(
        "INSERT INTO parse_results (job_id, project_id, document_version_id, operation_id, manifest, capabilities, result, record_sha256, raw_artifact_ids) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (lease.job_id, project_id, document_version_id, operation_id, Jsonb(header["manifest"]), Jsonb(header["capabilities"]), Jsonb(header["result"]),
         digest, artifact_ids))
    # the pages: a server-side copy, no page goes through Python
    conn.execute(
        "INSERT INTO parse_page_results (job_id, project_id, document_version_id, page_index, status, diagnostics, evidence, observations) "
        "SELECT job_id, %s, %s, page_index, status, diagnostics, evidence, observations FROM parse_staging_pages WHERE job_id = %s ORDER BY page_index",
        (project_id, document_version_id, lease.job_id))
    conn.execute("DELETE FROM parse_staging WHERE job_id = %s", (lease.job_id,))                  # cascades to pages and runs
    return {"pages": pages_total, "bytes": total_bytes, "raw_artifacts": len(artifact_ids), "record_sha256": digest}


def discard_staging(conn: psycopg.Connection, job_id: uuid.UUID) -> int:
    """Delete a job's staging (failed, cancelled, exhausted). Returns the number of pages removed."""
    with conn.transaction():
        n = conn.execute("SELECT count(*) FROM parse_staging_pages WHERE job_id = %s", (job_id,)).fetchone()[0]
        conn.execute("DELETE FROM parse_staging WHERE job_id = %s", (job_id,))
    return int(n)


def sweep_orphans(conn: psycopg.Connection, older_than_hours: float = ORPHAN_HOURS) -> int:
    """Delete the staging of jobs that are terminal (a crash between the transition and the delete) or untouched for ``older_than_hours``
    and not running. Returns the number of staging areas removed. Never touches a job that is running and recently active."""
    with conn.transaction():
        rows = conn.execute(
            "DELETE FROM parse_staging s USING jobs j WHERE j.project_id = s.project_id AND j.id = s.job_id AND "
            "(j.status::text IN ('succeeded', 'failed', 'cancelled') OR (j.status::text <> 'running' AND s.touched_at < now() - make_interval(hours => %s)) "
            "OR s.touched_at < now() - make_interval(hours => %s * 4)) RETURNING s.job_id",
            (older_than_hours, older_than_hours)).fetchall()
    return len(rows)
