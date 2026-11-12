"""General-bucket question generation from promoted CLEAN evidence (P-D slice 3, Refs #7, Refs #10).

``build_calls`` makes one call per selected clean text page (round robin, as many as the plan's general bucket asks
for) with ``generate_prompts`` (only that page's evidence text, fenced as data). ``on_results`` runs INSIDE the job's
fenced finish transaction: it parses each reply strictly, derives every other contract field by code, validates the
bundle with the existing contracts and ``validate_bundle``, drops (and counts) invalid replies and duplicates, and
writes questions, facts, evidence sets, members and ``gold_answers`` together with the ``llm_calls`` settlement rows;
a lost lease therefore writes none of it. The benchmark moves ``generating -> validating`` with the counts and any
shortfall below ``q_min`` in the transition reason (never padded). Human review stays ``skipped``.

The model only writes question, reference answer and facts. The source answer is the page's whole promoted text
evidence (one set, no distractors), and a textual ``quote`` check exists only for a fact whose statement appears
verbatim (whitespace and case folded) in a unit of that page. Nothing here proves the question needs the page."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import uuid
from typing import Callable, Optional, Sequence

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from lab_contracts.benchmark import (EvidenceSet, EvidenceUnit, Fact, FactSupportSet, Modality, Question, QuotaBucket,
                                     ReasoningType, ScoringRule, SemanticAnswer, SourceAnswer, TextCheck, TextualAnswer,
                                     validate_bundle)
from lab_contracts.source import RegionKind, SourceRegion
from lab_db.repo import transition_benchmark
from lab_api.generate_prompts import PROMPT_VERSION, build_page_call, detect_language, parse_model_reply, select_pages
from lab_api.generate_worker import CallSpec

__all__ = ["Derived", "GenerationCounts", "PageEvidence", "accept_replies", "build_calls", "derive", "general_target",
           "load_pages", "on_results", "question_hash"]

_CALL_ID = re.compile(r"^g(\d{1,4})-p(\d{1,6})$")
SCORING = ScoringRule(full_credit="the answer states this fact (or an allowed equivalent)", partial_credit=None,
                      no_credit="the fact is missing or contradicted")


@dataclass(frozen=True)
class PageEvidence:
    page_index: int
    units: tuple[EvidenceUnit, ...]          # text units of the page in reading order

    def texts(self) -> list[tuple[str, str]]:
        return [(u.evidence_id, u.normalized_content) for u in self.units]


@dataclass(frozen=True)
class Derived:
    question: Question
    semantic: SemanticAnswer
    textual: TextualAnswer
    source: SourceAnswer
    page_index: int


@dataclass
class GenerationCounts:
    calls: int = 0
    replies: int = 0
    accepted: int = 0
    invalid: int = 0
    duplicate: int = 0
    over_target: int = 0
    problems: dict = field(default_factory=dict)


def general_target(n_target: int, q_min: int) -> int:
    """The material plan's general-bucket minimum (same formula as ``material_plan``)."""
    return max(0, n_target - 3 * q_min)


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def question_hash(text: str) -> str:
    """Duplicate key: sha256 of the whitespace-collapsed, case-folded question text."""
    return hashlib.sha256(_fold(text).encode("utf-8")).hexdigest()


def derive(reply_text: str, *, question_id: str, ordinal: int, page: PageEvidence) -> Optional[Derived]:
    """Parse one reply and derive the four parts; ``None`` when the reply or any derived part is invalid."""
    parsed = parse_model_reply(reply_text)
    if parsed is None or not page.units:
        return None
    ids = [u.evidence_id for u in page.units]
    weights = parsed.weights or tuple(1.0 for _ in parsed.facts)
    try:
        facts = [Fact(fact_id=f"f{i + 1}", statement=s, weight=w, allowed_equivalences=[], scoring_rule=SCORING)
                 for i, (s, w) in enumerate(zip(parsed.facts, weights))]
        checks = []
        for f in facts:
            support = [u.evidence_id for u in page.units if _fold(f.statement) in _fold(u.normalized_content)]
            if support:
                checks.append(TextCheck(check_id=f"t-{f.fact_id}", fact_id=f.fact_id, kind="quote", expected=f.statement,
                                        interval=None, unit=None, normalization_rule=None, absolute_tolerance=None,
                                        relative_tolerance=None, precision=None, supporting_evidence_ids=support))
        question = Question(question_id=question_id, ordinal=ordinal, question_text=parsed.question,
                            language=detect_language(parsed.question), tags=["llm_generated", f"prompt:{PROMPT_VERSION}"],
                            quota_bucket=QuotaBucket.general, question_hash=question_hash(parsed.question),
                            modality_requirements=[Modality.text], reasoning_type=ReasoningType.value_lookup, visual_object_refs=[])
        semantic = SemanticAnswer(question_id=question_id, reference_answer=parsed.reference_answer,
                                  required_facts=facts, conditions=[], calculation=None)
        textual = TextualAnswer(question_id=question_id, checks=checks)
        source = SourceAnswer(question_id=question_id, evidence_sets=[EvidenceSet(set_id="s1", evidence_ids=ids)],
                              fact_support_sets=[FactSupportSet(fact_id=f.fact_id, support_sets=[ids]) for f in facts],
                              distractors=[], validation_refs=[])
    except ValidationError:
        return None
    if validate_bundle([question], [semantic], [textual], [source], {u.evidence_id: u for u in page.units}, {}):
        return None
    return Derived(question, semantic, textual, source, page.page_index)


def accept_replies(replies: Sequence[tuple[str, str]], pages: dict[int, PageEvidence], *, target: int,
                   seen_hashes: set[str], first_ordinal: int,
                   new_id: Callable[[], str] = lambda: str(uuid.uuid4())) -> tuple[list[Derived], GenerationCounts]:
    """``replies`` = (call_id, reply text) of the calls that returned ``ok``. Pure: no database."""
    counts = GenerationCounts(replies=len(replies))
    out: list[Derived] = []
    seen = set(seen_hashes)
    for call_id, text in replies:
        m = _CALL_ID.match(call_id)
        page = pages.get(int(m.group(2))) if m else None
        d = derive(text, question_id=new_id(), ordinal=first_ordinal + len(out), page=page) if page else None
        if d is None:
            counts.invalid += 1
        elif d.question.question_hash in seen:
            counts.duplicate += 1
        elif len(out) >= target:
            counts.over_target += 1
        else:
            seen.add(d.question.question_hash)
            out.append(d)
    counts.accepted = len(out)
    return out, counts


# ---- database -----------------------------------------------------------------------------------------------------

def load_pages(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID) -> dict[int, PageEvidence]:
    """The pinned parse's promoted text evidence, per page. Only units whose every region is a ``text`` region on the
    unit's own page (single-page text) are used; reading order = first region top, then left."""
    rows = conn.execute(
        "SELECT eu.id, eu.raw_content, eu.normalized_content, eu.extractor_revision, eu.ocr_confidence, s.page_index, "
        "er.ordinal, er.page_index, er.x0, er.y0, er.x1, er.y1, er.kind, er.quote, dv.id, dv.sha256 "
        "FROM benchmark_parse_sources b "
        "JOIN evidence_unit_sources s ON s.project_id = b.project_id AND s.parse_job_id = b.parse_job_id "
        "JOIN evidence_units eu ON eu.id = s.evidence_unit_id AND eu.document_version_id = b.document_version_id "
        "JOIN evidence_regions er ON er.evidence_unit_id = eu.id "
        "JOIN document_versions dv ON dv.id = b.document_version_id "
        "WHERE b.project_id = %s AND b.benchmark_id = %s ORDER BY eu.id, er.ordinal",
        (project_id, benchmark_id)).fetchall()
    units: dict = {}
    for (uid, raw, norm, rev, ocr, page, _o, rpage, x0, y0, x1, y1, kind, quote, doc, sha) in rows:
        u = units.setdefault(uid, {"raw": raw, "norm": norm, "rev": rev, "ocr": ocr, "page": page, "regions": []})
        u["regions"].append((rpage, kind, (str(doc), sha, (float(x0), float(y0), float(x1), float(y1)), quote)))
    pages: dict[int, list] = {}
    for uid, u in units.items():
        if not u["norm"].strip() or any(k != "text" or rp != u["page"] for rp, k, _ in u["regions"]):
            continue
        try:
            regions = [SourceRegion(document_id=d, document_sha256=h, page_index=rp, bbox=b, kind=RegionKind(k), quote=q)
                       for rp, k, (d, h, b, q) in u["regions"]]
            unit = EvidenceUnit(evidence_id=str(uid), regions=regions, raw_content=u["raw"],
                                normalized_content=u["norm"], extractor_revision=u["rev"], ocr_confidence=u["ocr"],
                                asset_refs=[])
        except ValidationError:
            continue                              # never send or cite evidence that does not satisfy the contract
        first = regions[0].bbox
        pages.setdefault(u["page"], []).append(((first[1], first[0], str(uid)), unit))
    return {p: PageEvidence(p, tuple(unit for _, unit in sorted(items, key=lambda t: t[0])))
            for p, items in sorted(pages.items())}


def _targets(conn, project_id, benchmark_id) -> tuple[int, int]:
    n_target, q_min = conn.execute("SELECT n_target, q_min FROM benchmark_revisions WHERE project_id = %s AND id = %s",
                                   (project_id, benchmark_id)).fetchone()
    return general_target(n_target, q_min), q_min


def build_calls(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID) -> list[CallSpec]:
    """One call per selected page, as many as the general bucket's target; call id ``g<n>-p<page>``."""
    with conn.transaction():
        target, _ = _targets(conn, project_id, benchmark_id)
        pages = load_pages(conn, project_id, benchmark_id)
    return [build_page_call(f"g{i}-p{p}", p, pages[p].texts()) for i, p in enumerate(select_pages(list(pages), target))]


_Q_SQL = ("INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, "
          "payload) VALUES (%s, %s, %s, %s, %s, %s, 'general', %s)")


def _write(conn, project_id, benchmark_id, d: Derived) -> None:
    q = d.question
    qid = uuid.UUID(q.question_id)
    conn.execute(_Q_SQL, (qid, benchmark_id, f"q-{q.question_id}", q.ordinal, q.question_text, q.question_hash,
                          Jsonb(q.model_dump(mode="json"))))
    for f in d.semantic.required_facts:
        conn.execute("INSERT INTO facts (id, benchmark_id, question_id, fact_key, statement, weight, partial_credit_rule) "
                     "VALUES (%s, %s, %s, %s, %s, %s, NULL)", (uuid.uuid4(), benchmark_id, qid, f.fact_id, f.statement, f.weight))
    for s in d.source.evidence_sets:
        set_id = uuid.uuid4()
        conn.execute("INSERT INTO evidence_sets (id, benchmark_id, question_id, set_key) VALUES (%s, %s, %s, %s)",
                     (set_id, benchmark_id, qid, s.set_id))
        for e in s.evidence_ids:
            conn.execute("INSERT INTO evidence_set_members (evidence_set_id, benchmark_id, evidence_unit_id) "
                         "VALUES (%s, %s, %s)", (set_id, benchmark_id, uuid.UUID(e)))
    for kind, part in (("semantic", d.semantic), ("textual", d.textual), ("source", d.source)):
        conn.execute("INSERT INTO gold_answers (project_id, benchmark_id, question_id, kind, payload) VALUES (%s, %s, %s, %s, %s)",
                     (project_id, benchmark_id, qid, kind, Jsonb(part.model_dump(mode="json"))))


def on_results(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, rows: list[dict]) -> None:
    """The ``process_generate_job`` hook (inside the fenced finish transaction). Raises to roll everything back."""
    actor = conn.execute(
        "SELECT g.requested_by FROM generate_jobs g JOIN jobs j ON j.project_id = g.project_id AND j.id = g.job_id "
        "WHERE g.project_id = %s AND g.benchmark_id = %s AND j.status = 'running'", (project_id, benchmark_id)).fetchall()
    if len(actor) != 1:
        raise RuntimeError("no single running generate job for the benchmark")
    actor = actor[0][0]
    # Gold RLS (0008/0015) decides by lab.actor_id: write as the owner/maintainer who queued the job.
    conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))
    status, version = conn.execute("SELECT status::text, row_version FROM benchmark_revisions WHERE project_id = %s "
                                   "AND id = %s FOR UPDATE", (project_id, benchmark_id)).fetchone()
    if status != "generating":
        raise RuntimeError("benchmark is not generating")
    target, q_min = _targets(conn, project_id, benchmark_id)
    pages = load_pages(conn, project_id, benchmark_id)
    existing = conn.execute("SELECT question_hash, ordinal FROM questions WHERE benchmark_id = %s", (benchmark_id,)).fetchall()
    first = max((o for _, o in existing), default=-1) + 1
    replies = [(r["call_id"], r.get("text") or "") for r in rows if r["status"] == "ok"]
    accepted, counts = accept_replies(replies, pages, target=max(0, target - len(existing)),
                                      seen_hashes={h for h, _ in existing}, first_ordinal=first)
    counts.calls = len(rows)
    for d in accepted:
        _write(conn, project_id, benchmark_id, d)
    total = len(existing) + counts.accepted
    shortfall = (f"shortfall: {total} general question(s) < q_min {q_min} (not padded)" if total < q_min
                 else f"general target {target}: {total} written" + (f", {target - total} short" if total < target else ""))
    reason = (f"generated {counts.accepted} general question(s) from {counts.calls} call(s) "
              f"(prompt {PROMPT_VERSION}); dropped invalid {counts.invalid}, duplicate {counts.duplicate}, "
              f"over target {counts.over_target}; {shortfall}; human review skipped")
    transition_benchmark(conn, project_id, benchmark_id, version, "validating", reason, actor)
