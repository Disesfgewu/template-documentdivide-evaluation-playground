"""Document ingestion into artifacts, resource_versions, document_versions, and pages (SPEC R01, 10.1-10.2)."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Literal
import uuid

import psycopg

from .repo import Change, record_change
from .storage import LocalArtifactStore, store_and_register

VALID_OUTCOMES = ("accept", "flag", "reject")
VALID_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class Inspection:
    """Pure-data PDF inspection result passed into ingest_document."""

    outcome: Literal["accept", "flag", "reject"]
    reason: str | None
    sha256: str
    size_bytes: int
    page_count: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageGeometry:
    """Pure-data per-page geometry in post-rotation display points."""

    page_index: int
    page_label: str | None
    rotation: int
    display_width_pt: float
    display_height_pt: float


class IngestRejected(Exception):
    """Raised when a document inspection outcome is 'reject'."""

    def __init__(self, reason: str | None) -> None:
        self.reason: str | None = reason
        super().__init__(reason or "document rejected")


def _validate_consistency(
    logical_id: str,
    data: bytes,
    inspection: Inspection,
    pages: list[PageGeometry],
) -> None:
    """Validate byte integrity, page counts, indices, rotations, and dimensions before writes."""
    if not isinstance(logical_id, str) or not (1 <= len(logical_id) <= 128):
        raise ValueError("logical_id must be a string of length 1..128")
    if not isinstance(data, bytes):
        raise ValueError("data must be bytes")
    if isinstance(inspection.size_bytes, bool) or not isinstance(inspection.size_bytes, int):
        raise ValueError("inspection.size_bytes must be an int")
    if len(data) != inspection.size_bytes:
        raise ValueError(
            f"data length ({len(data)}) does not match inspection.size_bytes ({inspection.size_bytes})"
        )

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != inspection.sha256:
        raise ValueError(
            f"data sha256 ({actual_sha256}) does not match inspection.sha256 ({inspection.sha256})"
        )

    if (
        inspection.page_count is None
        or isinstance(inspection.page_count, bool)
        or not isinstance(inspection.page_count, int)
        or inspection.page_count < 1
    ):
        raise ValueError(f"inspection.page_count must be an int >= 1, got {inspection.page_count!r}")

    if inspection.page_count != len(pages):
        raise ValueError(
            f"inspection.page_count ({inspection.page_count}) does not match len(pages) ({len(pages)})"
        )

    for expected_idx, page in enumerate(pages):
        if isinstance(page.page_index, bool) or not isinstance(page.page_index, int):
            raise ValueError(f"page_index must be an int, got {page.page_index!r}")
        if page.page_index != expected_idx:
            raise ValueError(
                f"pages must have contiguous page_index 0..{len(pages) - 1}; "
                f"expected {expected_idx}, got {page.page_index}"
            )
        if isinstance(page.rotation, bool) or page.rotation not in VALID_ROTATIONS:
            raise ValueError(
                f"page rotation must be one of {VALID_ROTATIONS}, got {page.rotation!r}"
            )
        if page.page_label is not None and (
            not isinstance(page.page_label, str) or len(page.page_label) > 64
        ):
            raise ValueError("page_label must be None or a str of length <= 64")
        if (
            isinstance(page.display_width_pt, bool)
            or not isinstance(page.display_width_pt, (int, float))
            or not math.isfinite(page.display_width_pt)
            or page.display_width_pt <= 0
        ):
            raise ValueError(
                f"display_width_pt must be a finite number > 0, got {page.display_width_pt!r}"
            )
        if (
            isinstance(page.display_height_pt, bool)
            or not isinstance(page.display_height_pt, (int, float))
            or not math.isfinite(page.display_height_pt)
            or page.display_height_pt <= 0
        ):
            raise ValueError(
                f"display_height_pt must be a finite number > 0, got {page.display_height_pt!r}"
            )


def _record_rejection(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    reason: str | None,
) -> None:
    """Record a failed document.import operation and document.rejected audit event in one transaction."""
    op_id = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            "INSERT INTO operations (id, project_id, kind, actor_id, status) "
            "VALUES (%s, %s, 'document.import', %s, 'failed')",
            (op_id, project_id, actor_id),
        )
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, "
            "reason, outcome, operation_id) "
            "VALUES (%s, %s, %s, 'document.rejected', 'document_version', NULL, %s, 'failed', %s)",
            (uuid.uuid4(), project_id, actor_id, reason, op_id),
        )


def _insert_pages(
    conn: psycopg.Connection,
    doc_version_id: uuid.UUID,
    pages: list[PageGeometry],
) -> None:
    """Insert all page geometry rows for a document_version."""
    for page in pages:
        conn.execute(
            "INSERT INTO pages "
            "(document_version_id, page_index, page_label, rotation, width_pt, height_pt) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                doc_version_id,
                page.page_index,
                page.page_label,
                page.rotation,
                page.display_width_pt,
                page.display_height_pt,
            ),
        )


def ingest_document(
    conn: psycopg.Connection,
    store: LocalArtifactStore,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    logical_id: str,
    data: bytes,
    inspection: Inspection,
    pages: list[PageGeometry],
) -> uuid.UUID:
    """Ingest an inspected PDF into artifact storage and PostgreSQL within a bounded transaction.

    Returns the newly created ``document_versions.id`` UUID. Two concurrent
    imports of the same ``logical_id`` may compute the same next revision; the
    unique (project, type, logical_id, revision) constraint then rejects one of
    them with an error instead of silently overwriting.
    Raises ``IngestRejected`` if ``inspection.outcome == 'reject'`` (after recording
    a failed ``document.import`` operation and ``document.rejected`` audit event),
    or ``ValueError`` if consistency checks fail prior to any writes.
    """
    if inspection.outcome not in VALID_OUTCOMES:
        raise ValueError(f"invalid inspection outcome: {inspection.outcome!r}")

    if inspection.outcome == "reject":
        _record_rejection(
            conn,
            project_id=project_id,
            actor_id=actor_id,
            reason=inspection.reason,
        )
        raise IngestRejected(inspection.reason)

    _validate_consistency(logical_id, data, inspection, pages)

    artifact_id = store_and_register(
        conn,
        store,
        project_id,
        data,
        mime="application/pdf",
        retention="restricted",
    )

    if inspection.page_count is None:  # already checked; explicit so it survives python -O
        raise ValueError("inspection.page_count is required")
    flagged = inspection.outcome == "flag"
    audit_reason: str | None = (
        f"flagged: {inspection.reason}" if flagged else None
    )
    outbox_payload = {
        "page_count": inspection.page_count,
        "flagged": flagged,
        "warnings": list(inspection.warnings),
    }

    doc_version_id = uuid.uuid4()
    with conn.transaction():
        # Serialize revision allocation per (project, logical_id) so two
        # concurrent imports cannot both compute the same next revision.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"document_version:{project_id}:{logical_id}",))
        rev_row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 "
            "FROM resource_versions "
            "WHERE project_id = %s AND resource_type = 'document_version' AND logical_id = %s",
            (project_id, logical_id),
        ).fetchone()
        next_revision = int(rev_row[0]) if rev_row is not None else 1

        resource_id = record_change(
            conn,
            Change(
                project_id=project_id,
                actor_id=actor_id,
                operation_kind="document.import",
                resource_type="document_version",
                logical_id=logical_id,
                revision=next_revision,
                content_hash=inspection.sha256,
                c14n_version="raw-bytes",
                audit_action="document.imported",
                reason=audit_reason,
                outbox_topic="document.imported",
                outbox_payload=outbox_payload,
            ),
        )

        conn.execute(
            "INSERT INTO document_versions "
            "(id, project_id, resource_id, artifact_id, sha256, page_count) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                doc_version_id,
                project_id,
                resource_id,
                artifact_id,
                inspection.sha256,
                inspection.page_count,
            ),
        )

        _insert_pages(conn, doc_version_id, pages)

    return doc_version_id
