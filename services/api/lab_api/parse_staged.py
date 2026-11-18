"""The parse worker's staged write path (Issue #205, PM-DECISION GO on #204): the sinks that store pages and runs as they become final, the identity that
decides resume vs restart, and the header of the final result built from what is STAGED (never from a whole document held in memory).

This module and ``parse_worker`` are the only places that touch the staging tables."""
from __future__ import annotations

from dataclasses import asdict
import json
from datetime import timezone
import threading
import uuid
from typing import Callable

import psycopg

from lab_contracts.parse import PageMapEntry, PageStatus, ParseManifest, ParseResult
from lab_db import parse_staging as ps
from lab_db.jobs import Lease
from lab_parser.batch_parse import DEFAULT_LIMITS
from lab_parser.normalize import ADAPTER_REVISION, Artifact
from .app_log import log_op, log_error


MAX_HEADER_REFS_BYTES = 8 * 1024 * 1024


def identity_of(doc_sha256: str, profile: str, operation_id, pages_total: int) -> dict:
    """What must be EXACTLY equal for staged pages of an earlier attempt to be reused (anything else: discard and restart)."""
    return {"document_sha256": doc_sha256, "profile": profile, "adapter_revision": ADAPTER_REVISION, "batch_size": DEFAULT_LIMITS.batch_size,
            "operation_id": str(operation_id), "pages_total": int(pages_total)}


class StagingSinks:
    """``page_sink`` / ``run_sink`` for ``parse_in_batches``: each write is its own short transaction on ONE dedicated connection (batches run in
    threads, so the connection is used under a lock). A failure is logged at ERROR with the real exception class and propagates."""

    def __init__(self, connect: Callable[[], psycopg.Connection], project_id: uuid.UUID, lease: Lease):
        self._connect, self._project_id, self._lease = connect, project_id, lease
        self._lock = threading.Lock()
        self._conn = None

    def _db(self) -> psycopg.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def page(self, page_index: int, status: str, diagnostics: list, evidence: list, observations: list, run_key=None) -> None:
        ev = [e.model_dump(mode="json") for e in evidence]
        ob = [{**asdict(o), "upstream_image_refs": list(o.upstream_image_refs)} for o in observations]
        try:
            with self._lock:
                size = ps.stage_page(self._db(), self._project_id, self._lease, page_index, status, list(diagnostics), ev, ob, run_key)
        except Exception as exc:  # noqa: BLE001 - logged with the real class, then the job fails with a specific reason
            self._failed("stage_page", exc, page_index=page_index)
            raise
        log_op("page_staged", stage="parse", page_index=page_index, status=status, units=len(ev), observations=len(ob), bytes=size, run_key=run_key)

    def run(self, run_key: str, batch) -> None:
        try:
            with self._lock:
                ps.stage_run(self._db(), self._project_id, self._lease, run_key, batch.manifest.model_dump(mode="json"), batch.capabilities.model_dump(mode="json"),
                             [a.content for a in batch.artifacts], list(batch.result.raw_result_refs))
        except Exception as exc:  # noqa: BLE001
            self._failed("stage_run", exc, run_key=run_key)
            raise
        log_op("run_staged", stage="parse", run_key=run_key, raw_artifacts=len(batch.artifacts), bytes=sum(len(a.content) for a in batch.artifacts))

    def _failed(self, what: str, exc: Exception, **where) -> None:
        diag = getattr(exc, "diag", None)
        log_error(what, type(exc).__name__, "page_write_failed", stage="parse", job_id=str(self._lease.job_id), sqlstate=getattr(exc, "sqlstate", None),
                  constraint=getattr(diag, "constraint_name", None), table=getattr(diag, "table_name", None), **where)
        self._conn = None            # a broken connection is not reused

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None


def build_header(conn: psycopg.Connection, job_id: uuid.UUID, operation_id, pages_total: int) -> dict:
    """The final result's header (manifest, capabilities, result WITHOUT pages) from the STAGED runs and pages, merged exactly as the old in-memory
    merge did (``batch_parse._merge``): the manifest of the run that produced page 0, its page selection and map widened to the whole document, the
    earliest start and latest finish of all runs, the raw refs of the runs in order of first use, the evidence ids in page order."""
    with conn.transaction():          # no implicit transaction may stay open on the caller's connection
        runs = {k: (ParseManifest.model_validate_json(json.dumps(m)), c, [], refs)
                for k, m, c, refs in conn.execute("SELECT run_key, manifest, capabilities, raw_result_refs FROM parse_staging_runs WHERE job_id = %s", (job_id,))}
        pages = conn.execute("SELECT page_index, status, diagnostics, run_key, (SELECT coalesce(jsonb_agg(e->>'evidence_id'), '[]'::jsonb) FROM jsonb_array_elements(evidence) e) "
                             "FROM parse_staging_pages WHERE job_id = %s ORDER BY page_index", (job_id,)).fetchall()
    order: list[str] = []                                           # runs by first appearance in page order
    for _, _, _, key, _ in pages:
        if key is not None and key in runs and key not in order:
            order.append(key)
    if not order:
        raise ps.StagingIncomplete("no staged run produced any page")
    manifests = [runs[k][0] for k in order]
    first = runs[order[0]]
    manifest = first[0].model_copy(update={
        "page_selection": list(range(pages_total)),
        "page_index_map": [PageMapEntry(parser_page=i + 1, page_index=i) for i in range(pages_total)],
        "started_at": min(m.started_at for m in manifests).astimezone(timezone.utc),
        "finished_at": max(m.finished_at for m in manifests).astimezone(timezone.utc),
        "warnings": [], "fallback_reason": None})
    raw_refs: list[str] = []
    for k in order:
        raw_refs.extend(r for r in runs[k][3] if r not in raw_refs)
    merged_manifest_artifact = Artifact(manifest.model_dump_json().encode())
    evidence_ids = [e for _, _, _, _, ids in pages for e in ids]
    # The header's list of every evidence id grows with the document (13.2 MB at 2000 pages of the measured density, against the 16 MiB column cap, and
    # nothing reads it: the ids are in the page rows). Beyond MAX_HEADER_REFS_BYTES it is left out and the header says how many there were.
    omit = sum(len(e) + 4 for e in evidence_ids) > MAX_HEADER_REFS_BYTES
    result = ParseResult(operation_id=str(operation_id), manifest_ref=merged_manifest_artifact.ref, raw_result_refs=raw_refs,
                         normalized_evidence_refs=[] if omit else evidence_ids, pages=[PageStatus(page_index=i, status=st, diagnostics=d) for i, st, d, _, _ in pages])
    body = result.model_dump(mode="json")
    body.pop("pages")
    if omit:
        body["evidence_refs_omitted"] = len(evidence_ids)
    return {"manifest": manifest.model_dump(mode="json"), "capabilities": first[1], "result": body, "extra_artifacts": (merged_manifest_artifact.content,)}
