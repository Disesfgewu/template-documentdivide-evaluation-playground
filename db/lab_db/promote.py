"""Promote a stored parse result into question-citable evidence (P-A, Refs #7, Refs #10).

``promote_parse_result`` pins ONE parse job to a benchmark revision (D1) and writes the evidence of the CLEAN pages
of that job into ``evidence_units`` / ``evidence_regions`` (D3: lazily, when a benchmark is built) in one transaction.

G8 HARD RULE (hidden text must never become evidence): a page is promoted only if its parse status is ``ok`` and
none of the diagnostics ``text_not_visible``, ``content_unparseable``, ``region_without_ink`` is present. Pages in
any other state (``needs_review``, ``partial``, ``failed``, no geometry, hidden or unanalysable text, dropped
regions) are NEVER promoted: they are reported as insufficient material with their reason, not repaired.

KNOWN RESIDUAL LIMIT (recorded in every pin's provenance): the protection against invisible text does not catch hidden
text under or among OTHER visible ink (a rule, stripe, fill, image or shape drawn through or over a one-line box can
rescue it), nor Type3 glyph procedures or annotation appearance streams. A page that passes these checks can
therefore still carry hidden text; question writers and reviewers must treat evidence as 'not proven visible'.
Document text is data here: nothing in this module interprets it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import uuid
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

from lab_contracts.benchmark import EvidenceUnit

__all__ = ["PROMOTION_RULE", "PromoteConflict", "PromoteError", "PromoteReport", "RESIDUAL_LIMIT",
           "UNSAFE_DIAGNOSTICS", "promote_parse_result"]

PROMOTION_RULE = "g8-clean-pages-v1"
UNSAFE_DIAGNOSTICS = frozenset({"text_not_visible", "content_unparseable", "region_without_ink"})
RESIDUAL_LIMIT = (
    "Hidden text under or among other visible ink (a rule, stripe, fill, image or shape over or through a one-line "
    "box), Type3 glyph procedures and annotation appearance streams are NOT detected: a promoted page is not proven "
    "free of hidden text.")
PROMOTABLE_BENCHMARK_STATES = ("draft", "parsing", "planning")
MAX_EVIDENCE_PER_PAGE = 5000


class PromoteError(Exception):
    """The parse job or benchmark cannot be promoted (fixed, value-free message)."""


class PromoteConflict(PromoteError):
    """The benchmark already pins a different parse job."""


@dataclass
class PromoteReport:
    benchmark_id: uuid.UUID
    parse_job_id: uuid.UUID
    already_promoted: bool = False
    pages_total: int = 0
    promoted_pages: list[int] = field(default_factory=list)
    skipped_pages: dict[int, str] = field(default_factory=dict)      # page index -> fixed reason
    evidence_units: int = 0


def _skip_reason(status: str, diagnostics: list) -> Optional[str]:
    """``None`` for a promotable page, else the fixed reason it is not."""
    unsafe = sorted(UNSAFE_DIAGNOSTICS.intersection(d for d in diagnostics if isinstance(d, str)))
    if unsafe:
        return "unsafe_text:" + ",".join(unsafe)
    if status != "ok":
        return "status_" + status
    return None


def promote_parse_result(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID,
                         parse_job_id: uuid.UUID, actor_id: uuid.UUID) -> PromoteReport:
    """Pin ``parse_job_id`` to the benchmark revision and promote its clean pages' evidence; idempotent per pin.

    Raises ``PromoteError`` (unknown benchmark/job, wrong document, job not succeeded, benchmark not in a state that
    takes a pin) or ``PromoteConflict`` (a different job is already pinned). Authorization is the caller's job."""
    with conn.transaction():
        bench = conn.execute(
            "SELECT document_version_id, status::text FROM benchmark_revisions WHERE project_id = %s AND id = %s FOR UPDATE",
            (project_id, benchmark_id)).fetchone()
        if bench is None:
            raise PromoteError("benchmark_not_found")
        document_version_id, bench_status = bench
        pinned = conn.execute("SELECT parse_job_id, provenance FROM benchmark_parse_sources WHERE benchmark_id = %s",
                              (benchmark_id,)).fetchone()
        report = PromoteReport(benchmark_id, parse_job_id)
        if pinned is not None:
            if pinned[0] != parse_job_id:
                raise PromoteConflict("benchmark_pins_another_parse_job")
            prov = pinned[1]
            report.already_promoted = True
            report.pages_total = prov.get("pages_total", 0)
            report.promoted_pages = list(prov.get("promoted_pages", []))
            report.skipped_pages = {int(k): v for k, v in prov.get("skipped_pages", {}).items()}
            report.evidence_units = prov.get("evidence_units", 0)
            return report
        if bench_status not in PROMOTABLE_BENCHMARK_STATES:
            raise PromoteError("benchmark_state_takes_no_pin")
        job = conn.execute(
            "SELECT j.status::text, pr.document_version_id FROM jobs j JOIN parse_results pr "
            "ON pr.project_id = j.project_id AND pr.job_id = j.id WHERE j.project_id = %s AND j.id = %s",
            (project_id, parse_job_id)).fetchone()
        if job is None:
            raise PromoteError("parse_result_not_found")
        if job[0] != "succeeded":
            raise PromoteError("parse_job_not_succeeded")
        if job[1] != document_version_id:
            raise PromoteError("parse_job_is_for_another_document")

        pages = conn.execute(
            "SELECT page_index, status, diagnostics, evidence FROM parse_page_results "
            "WHERE project_id = %s AND job_id = %s ORDER BY page_index", (project_id, parse_job_id)).fetchall()
        report.pages_total = len(pages)
        doc_sha = conn.execute("SELECT sha256 FROM document_versions WHERE project_id = %s AND id = %s",
                               (project_id, document_version_id)).fetchone()[0]
        for page_index, status, diagnostics, evidence in pages:
            reason = _skip_reason(status, diagnostics)
            if reason is not None:
                report.skipped_pages[page_index] = reason
                continue
            if len(evidence) > MAX_EVIDENCE_PER_PAGE:
                report.skipped_pages[page_index] = "too_many_evidence_units"
                continue
            try:
                # JSON mode: the contract is strict (tuples, enums), and jsonb hands back lists and strings.
                units = [EvidenceUnit.model_validate_json(json.dumps(e)) for e in evidence]
            except Exception:                                   # a unit that does not satisfy the contract is not promoted
                report.skipped_pages[page_index] = "invalid_evidence"
                continue
            if any(r.page_index != page_index or r.document_sha256 != doc_sha
                   for u in units for r in u.regions):
                report.skipped_pages[page_index] = "evidence_region_outside_its_page_or_document"
                continue
            for unit in units:
                _write_unit(conn, project_id, document_version_id, parse_job_id, page_index, unit)
            report.evidence_units += len(units)
            report.promoted_pages.append(page_index)

        provenance = {
            "rule": PROMOTION_RULE, "residual_limit": RESIDUAL_LIMIT, "unsafe_diagnostics": sorted(UNSAFE_DIAGNOSTICS),
            "pages_total": report.pages_total, "promoted_pages": report.promoted_pages,
            "skipped_pages": {str(k): v for k, v in report.skipped_pages.items()},
            "evidence_units": report.evidence_units,
        }
        conn.execute(
            "INSERT INTO benchmark_parse_sources (benchmark_id, project_id, parse_job_id, document_version_id, rule, "
            "provenance, promoted_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (benchmark_id, project_id, parse_job_id, document_version_id, PROMOTION_RULE, Jsonb(provenance), actor_id))
        return report


def _write_unit(conn, project_id, document_version_id, parse_job_id, page_index, unit: EvidenceUnit) -> None:
    """Insert one evidence unit with its regions and its source row, unless the same unit already exists."""
    existing = conn.execute(
        "SELECT eu.id, eu.raw_content FROM evidence_units eu WHERE eu.document_version_id = %s AND eu.evidence_key = %s",
        (document_version_id, unit.evidence_id)).fetchone()
    if existing is not None:
        # Same id = same raw artifact, page and pointer (the id is a hash of them): it must be the same content.
        if existing[1] != unit.raw_content:
            raise PromoteError("evidence_key_collision_with_different_content")
        return
    unit_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO evidence_units (id, project_id, document_version_id, evidence_key, raw_content, normalized_content, "
        "extractor_revision, ocr_confidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (unit_id, project_id, document_version_id, unit.evidence_id, unit.raw_content, unit.normalized_content,
         unit.extractor_revision, unit.ocr_confidence))
    for ordinal, region in enumerate(unit.regions):
        x0, y0, x1, y1 = region.bbox
        conn.execute(
            "INSERT INTO evidence_regions (evidence_unit_id, ordinal, document_version_id, page_index, x0, y0, x1, y1, kind, quote) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (unit_id, ordinal, document_version_id, region.page_index, x0, y0, x1, y1,
             region.kind.value if hasattr(region.kind, "value") else str(region.kind), region.quote))
    conn.execute(
        "INSERT INTO evidence_unit_sources (evidence_unit_id, project_id, parse_job_id, source_evidence_id, page_index) "
        "VALUES (%s, %s, %s, %s, %s)", (unit_id, project_id, parse_job_id, unit.evidence_id, page_index))
