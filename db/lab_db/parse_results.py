"""Persist a parse job's outcome (PM plan item 5, Refs #7).

``persist_parse_result`` is meant to run as the ``hook`` of ``lab_db.jobs.finish``: inside the
transaction that marks the job succeeded, with the job row locked and still ``running`` under the
caller's lease. It writes the raw parser output as committed artifacts, one ``parse_results`` row
and one ``parse_page_results`` row per page. It is idempotent on the job (and so on
``(job_id, operation_id)``): a second call with the same content writes nothing, a second call
with different content raises ``ParseResultConflict``. Content is data: nothing here interprets
document text.
"""
from __future__ import annotations

from dataclasses import dataclass
import uuid

import psycopg
from psycopg.types.json import Jsonb

from .jobs import Lease, LeaseLost
from .storage import LocalArtifactStore, store_and_register

__all__ = ["PageRecord", "ParseResultConflict", "ParseResultRecord", "persist_parse_result"]

MAX_RAW_ARTIFACTS = 20_000


class ParseResultConflict(Exception):
    """A different result is already stored for this job."""


@dataclass(frozen=True)
class PageRecord:
    page_index: int
    status: str
    diagnostics: list
    evidence: list
    observations: list


@dataclass(frozen=True)
class ParseResultRecord:
    manifest: dict
    capabilities: dict
    result: dict
    raw_artifacts: tuple  # bytes of each raw parser output, stored content-addressed
    pages: tuple          # PageRecord, one per page named in result["pages"]


def persist_parse_result(
    conn: psycopg.Connection,
    store: LocalArtifactStore,
    project_id: uuid.UUID,
    lease: Lease,
    record: ParseResultRecord,
) -> None:
    status, token = (conn.execute(
        "SELECT status::text, lease_token FROM jobs WHERE project_id = %s AND id = %s",
        (project_id, lease.job_id)).fetchone() or (None, None))
    if status != "running" or token != lease.token:
        raise LeaseLost(f"job {lease.job_id} is not running under token {lease.token}")
    job = conn.execute("SELECT document_version_id, operation_id FROM parse_jobs WHERE project_id = %s AND job_id = %s",
                       (project_id, lease.job_id)).fetchone()
    if job is None:
        raise ValueError("job has no parse_jobs row")
    document_version_id, operation_id = job
    if not 1 <= len(record.raw_artifacts) <= MAX_RAW_ARTIFACTS:
        raise ValueError("a parse result needs 1..%d raw artifacts" % MAX_RAW_ARTIFACTS)
    if {p.page_index for p in record.pages} != {p["page_index"] for p in record.result.get("pages", [])}:
        raise ValueError("page records must match the pages named in the result")

    existing = conn.execute("SELECT result FROM parse_results WHERE job_id = %s", (lease.job_id,)).fetchone()
    if existing is not None:
        if existing[0] != record.result:
            raise ParseResultConflict(str(lease.job_id))
        return  # idempotent replay: identical content is already stored

    artifact_ids = [store_and_register(conn, store, project_id, data, "application/json")
                    for data in record.raw_artifacts]
    conn.execute(
        "INSERT INTO parse_results (job_id, project_id, document_version_id, operation_id, manifest, capabilities, "
        "result, raw_artifact_ids) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (lease.job_id, project_id, document_version_id, operation_id, Jsonb(record.manifest),
         Jsonb(record.capabilities), Jsonb(record.result), artifact_ids),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO parse_page_results (job_id, project_id, document_version_id, page_index, status, diagnostics, "
            "evidence, observations) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [(lease.job_id, project_id, document_version_id, p.page_index, p.status, Jsonb(p.diagnostics),
              Jsonb(p.evidence), Jsonb(p.observations)) for p in record.pages],
        )
