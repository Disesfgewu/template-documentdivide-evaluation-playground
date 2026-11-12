"""General-bucket question generation: the real ``build_calls`` and the ``on_results`` hook (P-D slice 3, Refs #7).

* ``build_calls`` reads the benchmark's PINNED parse (P-A): only pages promoted under the G8 hard rule, only their text
  evidence units, one call per question, pages taken round-robin. Nothing else is sent (PM-DECISION 2026-11-12: no file
  names, ids, diagnostics, other pages or gold).
* ``on_results`` runs inside the job's final transaction. The model's reply is parsed strictly; every contract field the
  model did not write is derived by CODE: language, tags, modality, reasoning type, the source answer (the page's text
  units that were shown), the textual checks ([]), calculation (null). Each question is validated with the existing
  contracts and ``validate_bundle``; an invalid one is dropped and counted, never repaired; duplicates (by
  ``question_hash``) are dropped. Valid ones are written to questions/facts/evidence_sets/evidence_set_members and to
  ``gold_answers`` (semantic, textual, source) together with an audit event that carries counts only.
* Only the general bucket is generated (D5: cross_page, chart_table and confusable need material this parse cannot
  prove; they are not guessed). If fewer than the plan's general target is written, the shortfall is recorded, never
  padded, and the benchmark stays in ``generating``.

KNOWN LIMITATION: a question that can be answered without the page (general knowledge) is not detected yet; the later
independent validation slice is where that is checked. Human review stays 'skipped'."""
from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Callable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from lab_contracts.benchmark import (EvidenceSet, EvidenceUnit, Fact, FactSupportSet, Modality, Question, QuotaBucket,
                                     ReasoningType, ScoringRule, SemanticAnswer, SourceAnswer, TextualAnswer,
                                     target_counts, validate_bundle)
from lab_api.generate_prompts import (DEFAULT_PAGE_CHARS, PROMPT_VERSION, ParsedReply, build_page_call, detect_language,
                                      parse_model_reply, select_pages, shown_pieces)
from lab_api.generate_worker import CallSpec

__all__ = ["GenerationStats", "general_target", "load_clean_pages", "make_build_calls", "make_on_results",
           "question_hash"]

MAX_UNITS_IN_PROMPT = 60


@dataclass
class GenerationStats:
    replies: int = 0
    unparseable: int = 0
    invalid: int = 0
    duplicate: int = 0
    written: int = 0


def question_hash(text: str) -> str:
    """sha256 over the NFKC-normalized, whitespace-collapsed, case-folded question text (a documented definition: the
    contract only asks for 64 hex characters)."""
    norm = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def general_target(page_count: int) -> int:
    """Questions the general bucket must reach: N - 3Q (the other three buckets have Q each at minimum)."""
    n, q = target_counts(page_count)
    return max(0, n - 3 * q)


def load_clean_pages(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID
                     ) -> tuple[int, dict[int, list[EvidenceUnit]], uuid.UUID]:
    """(page_count, {page index: text evidence units in reading order}, document_version_id) for the pages the pin
    promoted. A unit that fails the contract is left out (it was validated at promotion; this is defence in depth)."""
    row = conn.execute(
        "SELECT s.parse_job_id, s.document_version_id, s.provenance, dv.page_count FROM benchmark_parse_sources s "
        "JOIN document_versions dv ON dv.project_id = s.project_id AND dv.id = s.document_version_id "
        "WHERE s.project_id = %s AND s.benchmark_id = %s", (project_id, benchmark_id)).fetchone()
    if row is None:
        raise LookupError("benchmark has no pinned parse")
    job_id, doc_id, provenance, page_count = row
    promoted = [int(p) for p in provenance.get("promoted_pages", [])]
    pages: dict[int, list[EvidenceUnit]] = {}
    for page_index, evidence in conn.execute(
            "SELECT page_index, evidence FROM parse_page_results WHERE project_id = %s AND job_id = %s "
            "AND page_index = ANY(%s) ORDER BY page_index", (project_id, job_id, promoted)).fetchall():
        units = []
        for raw in evidence:
            try:
                unit = EvidenceUnit.model_validate_json(json.dumps(raw))
            except Exception:
                continue
            if unit.normalized_content.strip() and all(r.kind.value == "text" for r in unit.regions):
                units.append(unit)
        if units:
            pages[int(page_index)] = units
    return page_count, pages, doc_id


def call_id(k: int, page: int) -> str:
    return f"g{k:04d}-p{page}"


def parse_call_id(cid: str) -> tuple[int, int]:
    head, _, page = cid.partition("-p")
    return int(head[1:]), int(page)


def make_build_calls(page_chars: int = DEFAULT_PAGE_CHARS) -> Callable:
    def build_calls(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID) -> Sequence[CallSpec]:
        page_count, pages, _ = load_clean_pages(conn, project_id, benchmark_id)
        have = conn.execute("SELECT count(*) FROM questions WHERE benchmark_id = %s", (benchmark_id,)).fetchone()[0]
        needed = max(0, general_target(page_count) - have)
        chosen = select_pages(sorted(pages), needed)
        seen: dict[int, int] = {}
        calls = []
        for k, page in enumerate(chosen):
            variant = seen.get(page, 0)
            seen[page] = variant + 1
            units = [(u.evidence_id, u.normalized_content) for u in pages[page][:MAX_UNITS_IN_PROMPT]]
            calls.append(build_page_call(call_id(k, page), page, units, page_chars, variant))
        return calls
    return build_calls


def _derive(reply: ParsedReply, units: Sequence[EvidenceUnit], ordinal: int):
    qhash = question_hash(reply.question)
    qid = f"q-{qhash[:24]}"
    question = Question(
        question_id=qid, ordinal=ordinal, question_text=reply.question, language=detect_language(reply.question),
        tags=[], quota_bucket=QuotaBucket.general, question_hash=qhash, modality_requirements=[Modality.text],
        reasoning_type=ReasoningType.value_lookup, visual_object_refs=[])
    weight = round(1.0 / len(reply.facts), 6)
    facts = [Fact(fact_id=f"{qid}-f{i + 1}", statement=s, weight=weight, allowed_equivalences=[],
                  scoring_rule=ScoringRule(full_credit="The answer states this fact.", partial_credit=None,
                                           no_credit="The answer does not state this fact or contradicts it."))
             for i, s in enumerate(reply.facts)]
    semantic = SemanticAnswer(question_id=qid, reference_answer=reply.reference_answer, required_facts=facts,
                              conditions=[], calculation=None)
    ids = [u.evidence_id for u in units]
    source = SourceAnswer(
        question_id=qid, evidence_sets=[EvidenceSet(set_id=f"{qid}-s1", evidence_ids=ids)],
        fact_support_sets=[FactSupportSet(fact_id=f.fact_id, support_sets=[ids]) for f in facts],
        distractors=[], validation_refs=[])
    return question, semantic, TextualAnswer(question_id=qid, checks=[]), source


def make_on_results(page_chars: int = DEFAULT_PAGE_CHARS) -> Callable:
    def on_results(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, rows: list[dict]) -> GenerationStats:
        page_count, pages, doc_id = load_clean_pages(conn, project_id, benchmark_id)
        actor = conn.execute("SELECT requested_by FROM generate_jobs WHERE project_id = %s AND benchmark_id = %s "
                             "ORDER BY created_at DESC LIMIT 1", (project_id, benchmark_id)).fetchone()[0]
        # facts/evidence_sets/gold_answers are under gold RLS (0008): the write is made as the job's owner/maintainer
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))
        existing = {h for (h,) in conn.execute("SELECT question_hash FROM questions WHERE benchmark_id = %s", (benchmark_id,))}
        ordinal = conn.execute("SELECT COALESCE(max(ordinal) + 1, 0) FROM questions WHERE benchmark_id = %s",
                               (benchmark_id,)).fetchone()[0]
        stats = GenerationStats()
        for row in rows:
            if row["status"] != "ok":
                continue
            stats.replies += 1
            reply = parse_model_reply(row["text"])
            _, page = parse_call_id(row["call_id"])
            units = pages.get(page)
            if reply is None or not units:
                stats.unparseable += 1
                continue
            # Only the units the model was ACTUALLY shown (same cut as the prompt) can be the question's source.
            shown_ids = {eid for eid, _ in shown_pieces([(u.evidence_id, u.normalized_content) for u in units[:MAX_UNITS_IN_PROMPT]], page_chars)}
            units = [u for u in units[:MAX_UNITS_IN_PROMPT] if u.evidence_id in shown_ids]
            if not units:
                stats.unparseable += 1
                continue
            try:
                question, semantic, textual, source = _derive(reply, units, ordinal)
            except Exception:
                stats.invalid += 1
                continue
            if question.question_hash in existing:
                stats.duplicate += 1
                continue
            problems = validate_bundle([question], [semantic], [textual], [source], {u.evidence_id: u for u in units}, {})
            if problems:
                stats.invalid += 1
                continue
            ids = [u.evidence_id for u in units]
            unit_rows = dict(conn.execute("SELECT evidence_key, id FROM evidence_units WHERE document_version_id = %s "
                                          "AND evidence_key = ANY(%s)", (doc_id, ids)).fetchall())
            if len(unit_rows) != len(ids):          # a unit that is not stored: skip and count, never raise and roll everything back
                stats.invalid += 1
                continue
            _write(conn, project_id, benchmark_id, unit_rows, question, semantic, textual, source)
            existing.add(question.question_hash)
            ordinal += 1
            stats.written += 1
        target = general_target(page_count)
        total = conn.execute("SELECT count(*) FROM questions WHERE benchmark_id = %s", (benchmark_id,)).fetchone()[0]
        op_id = uuid.uuid4()
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) "
                     "VALUES (%s, %s, 'benchmark.questions_generated', %s, 'succeeded', now())", (op_id, project_id, actor))
        resource = conn.execute("SELECT resource_id FROM benchmark_revisions WHERE id = %s", (benchmark_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, reason, outcome, "
            "operation_id) VALUES (%s, %s, %s, 'benchmark.questions_generated', 'benchmark_revision', %s, %s, 'succeeded', %s)",
            (uuid.uuid4(), project_id, actor, resource,
             f"prompt={PROMPT_VERSION} bucket=general replies={stats.replies} written={stats.written} "
             f"unparseable={stats.unparseable} invalid={stats.invalid} duplicate={stats.duplicate} "
             f"total={total} target={target} shortfall={max(0, target - total)}", op_id))
        if total > 0:
            # generating -> validating (PM-DECISION): never frozen, human review stays skipped; the shortfall is in the reason
            conn.execute("SELECT set_config('lab.transition_reason', %s, true)",
                         (f"general questions generated: total={total} target={target} shortfall={max(0, target - total)}; "
                          "validation pending, human review skipped",))
            conn.execute("UPDATE benchmark_revisions SET status = 'validating', row_version = row_version + 1 "
                         "WHERE id = %s AND status = 'generating'", (benchmark_id,))
        return stats
    return on_results


def _write(conn, project_id, benchmark_id, unit_rows: dict, q: Question, sem: SemanticAnswer, tex: TextualAnswer, src: SourceAnswer):
    qrow = uuid.uuid4()
    conn.execute(
        "INSERT INTO questions (id, benchmark_id, question_key, ordinal, question_text, question_hash, quota_bucket, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (qrow, benchmark_id, q.question_id, q.ordinal, q.question_text, q.question_hash, q.quota_bucket.value,
         Jsonb(json.loads(q.model_dump_json()))))
    for f in sem.required_facts:
        conn.execute("INSERT INTO facts (id, benchmark_id, question_id, fact_key, statement, weight, partial_credit_rule) "
                     "VALUES (%s, %s, %s, %s, %s, %s, NULL)",
                     (uuid.uuid4(), benchmark_id, qrow, f.fact_id, f.statement, f.weight))
    for s in src.evidence_sets:
        set_row = uuid.uuid4()
        conn.execute("INSERT INTO evidence_sets (id, benchmark_id, question_id, set_key) VALUES (%s, %s, %s, %s)",
                     (set_row, benchmark_id, qrow, s.set_id))
        for ev in s.evidence_ids:
            conn.execute("INSERT INTO evidence_set_members (evidence_set_id, benchmark_id, evidence_unit_id) VALUES (%s, %s, %s)",
                         (set_row, benchmark_id, unit_rows[ev]))
    for kind, model in (("semantic", sem), ("textual", tex), ("source", src)):
        conn.execute("INSERT INTO gold_answers (id, benchmark_id, question_id, kind, payload) VALUES (%s, %s, %s, %s, %s)",
                     (uuid.uuid4(), benchmark_id, qrow, kind, Jsonb(json.loads(model.model_dump_json()))))
