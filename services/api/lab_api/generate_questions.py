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
* The general, cross_page and confusable buckets are generated; chart_table needs material this parse cannot prove and is not
  guessed (D5). cross_page needs two DIFFERENT clean text pages: with fewer there is no pair and no call. A shortfall of either
  bucket (the plan's general target N-3Q, or Q for cross_page) is recorded in the audit event and the transition reason, never
  padded; the benchmark moves to ``validating`` as soon as at least one question exists, whatever the shortfall.

KNOWN LIMITATION: a question that can be answered without the page (general knowledge) is not detected yet; the later
independent validation slice is where that is checked. Human review stays 'skipped'."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Callable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from lab_contracts.benchmark import (Distractor, EvidenceSet, EvidenceUnit, Fact, FactSupportSet, Modality, Question, QuotaBucket,
                                     ReasoningType, ScoringRule, SemanticAnswer, SourceAnswer, TextualAnswer,
                                     target_counts, validate_bundle)
from lab_api.generate_prompts import (CONFUSABLE_PROMPT_VERSION, CROSS_PAGE_PROMPT_VERSION, DEFAULT_PAGE_CHARS, ConfusableReply, PROMPT_VERSION, CrossReply, ParsedReply,
                                      build_confusable_call, build_cross_page_call, build_page_call, parse_confusable_reply, detect_language, parse_cross_page_reply,
                                      parse_model_reply, select_page_pairs, select_pages, shown_pieces)
from lab_api.generate_worker import CallSpec
from lab_api.validate_questions import code_checks

__all__ = ["GenerationStats", "confusable_target", "cross_page_target", "general_target", "load_clean_pages", "make_build_calls", "make_on_results",
           "question_hash"]

MAX_UNITS_IN_PROMPT = 60


@dataclass
class GenerationStats:
    confusable_written: int = 0
    cross_written: int = 0
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


def cross_page_target(page_count: int) -> int:
    """cross_page questions the plan asks for: Q = ceil(0.10 * N) (the minimum of each special bucket)."""
    return target_counts(page_count)[1]


def confusable_target(page_count: int) -> int:
    """confusable questions the plan asks for: Q, as for cross_page."""
    return target_counts(page_count)[1]


_CROSS_ID = re.compile(r"x(\d+)-p(\d+)-p(\d+)")
_CONF_ID = re.compile(r"c(\d+)-a(\d+)-d(\d+)")


def cross_call_id(k: int, a: int, b: int) -> str:
    return f"x{k:04d}-p{a}-p{b}"


def confusable_call_id(k: int, answer_page: int, distractor_page: int) -> str:
    return f"c{k:04d}-a{answer_page}-d{distractor_page}"


def parse_confusable_id(cid: str):
    m = _CONF_ID.fullmatch(cid)
    return None if m is None else (int(m.group(2)), int(m.group(3)))


def parse_cross_id(cid: str):
    m = _CROSS_ID.fullmatch(cid)
    return None if m is None else (int(m.group(2)), int(m.group(3)))


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
        have = dict(conn.execute("SELECT quota_bucket, count(*) FROM questions WHERE benchmark_id = %s GROUP BY 1",
                                 (benchmark_id,)).fetchall())
        needed = max(0, general_target(page_count) - int(have.get("general", 0)))
        chosen = select_pages(sorted(pages), needed)
        seen: dict[int, int] = {}
        calls = []
        for k, page in enumerate(chosen):
            variant = seen.get(page, 0)
            seen[page] = variant + 1
            units = [(u.evidence_id, u.normalized_content) for u in pages[page][:MAX_UNITS_IN_PROMPT]]
            calls.append(build_page_call(call_id(k, page), page, units, page_chars, variant))
        # cross_page: two DIFFERENT clean text pages per call (no pair, no call, when fewer than two clean pages exist);
        # these come last so that a budget that runs out skips them first.
        needed_cross = max(0, cross_page_target(page_count) - int(have.get("cross_page", 0)))
        pair_seen: dict[tuple[int, int], int] = {}
        for k, (a_page, b_page) in enumerate(select_page_pairs(sorted(pages), needed_cross)):
            variant = pair_seen.get((a_page, b_page), 0)
            pair_seen[(a_page, b_page)] = variant + 1
            pair = [(pg, [(u.evidence_id, u.normalized_content) for u in pages[pg][:MAX_UNITS_IN_PROMPT]]) for pg in (a_page, b_page)]
            calls.append(build_cross_page_call(cross_call_id(k, a_page, b_page), pair, page_chars, variant))
        # confusable: passage A answers, passage B is the similar-looking distractor (two different clean text pages; the
        # roles alternate so both pages serve as the answer page); last of all, so a budget that runs out skips them first.
        needed_conf = max(0, confusable_target(page_count) - int(have.get("confusable", 0)))
        conf_seen: dict[tuple[int, int], int] = {}
        for k, (p1, p2) in enumerate(select_page_pairs(sorted(pages), needed_conf)):
            a_page, d_page = (p1, p2) if k % 2 == 0 else (p2, p1)
            variant = conf_seen.get((a_page, d_page), 0)
            conf_seen[(a_page, d_page)] = variant + 1
            pair = [(pg, [(u.evidence_id, u.normalized_content) for u in pages[pg][:MAX_UNITS_IN_PROMPT]]) for pg in (a_page, d_page)]
            calls.append(build_confusable_call(confusable_call_id(k, a_page, d_page), pair, page_chars, variant))
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


def _derive_cross(reply: CrossReply, shown: dict[str, list[EvidenceUnit]], ordinal: int):
    """Question, answers and source for a cross_page question. ``shown`` = {"A": units of page A actually shown, "B": ...}.
    Each fact's support alternative is the units of the page the model attributed it to, so the contract's own check
    (no single page may support every fact; every evidence set spans >= 2 pages) decides whether it is truly cross-page."""
    qhash = question_hash(reply.question)
    qid = f"q-{qhash[:24]}"
    question = Question(
        question_id=qid, ordinal=ordinal, question_text=reply.question, language=detect_language(reply.question),
        tags=[], quota_bucket=QuotaBucket.cross_page, question_hash=qhash, modality_requirements=[Modality.text],
        reasoning_type=ReasoningType.cross_page_integration, visual_object_refs=[])
    weight = round(1.0 / len(reply.facts), 6)
    facts = [Fact(fact_id=f"{qid}-f{i + 1}", statement=st, weight=weight, allowed_equivalences=[],
                  scoring_rule=ScoringRule(full_credit="The answer states this fact.", partial_credit=None,
                                           no_credit="The answer does not state this fact or contradicts it."))
             for i, (st, _) in enumerate(reply.facts)]
    semantic = SemanticAnswer(question_id=qid, reference_answer=reply.reference_answer, required_facts=facts,
                              conditions=[], calculation=None)
    all_ids = [u.evidence_id for label in ("A", "B") for u in shown[label]]
    source = SourceAnswer(
        question_id=qid, evidence_sets=[EvidenceSet(set_id=f"{qid}-s1", evidence_ids=all_ids)],
        fact_support_sets=[FactSupportSet(fact_id=f.fact_id, support_sets=[[u.evidence_id for u in shown[src]]])
                           for f, (_, src) in zip(facts, reply.facts)],
        distractors=[], validation_refs=[])
    return question, semantic, TextualAnswer(question_id=qid, checks=[]), source


def _derive_confusable(reply: ConfusableReply, answer_units: Sequence[EvidenceUnit], distractor_units: Sequence[EvidenceUnit], ordinal: int):
    """Question, answers and source for a confusable question: the gold is the units of page A that were shown; every shown unit
    of page B is a distractor with the model's reason and disambiguating condition (checked against the text by the caller)."""
    qhash = question_hash(reply.question)
    qid = f"q-{qhash[:24]}"
    question = Question(
        question_id=qid, ordinal=ordinal, question_text=reply.question, language=detect_language(reply.question),
        tags=[], quota_bucket=QuotaBucket.confusable, question_hash=qhash, modality_requirements=[Modality.text],
        reasoning_type=ReasoningType.value_lookup, visual_object_refs=[])
    weight = round(1.0 / len(reply.facts), 6)
    facts = [Fact(fact_id=f"{qid}-f{i + 1}", statement=st, weight=weight, allowed_equivalences=[],
                  scoring_rule=ScoringRule(full_credit="The answer states this fact.", partial_credit=None,
                                           no_credit="The answer does not state this fact or contradicts it."))
             for i, st in enumerate(reply.facts)]
    semantic = SemanticAnswer(question_id=qid, reference_answer=reply.reference_answer, required_facts=facts,
                              conditions=[reply.disambiguating_condition], calculation=None)
    ids = [u.evidence_id for u in answer_units]
    source = SourceAnswer(
        question_id=qid, evidence_sets=[EvidenceSet(set_id=f"{qid}-s1", evidence_ids=ids)],
        fact_support_sets=[FactSupportSet(fact_id=f.fact_id, support_sets=[ids]) for f in facts],
        distractors=[Distractor(evidence_id=u.evidence_id, confusion_reason=reply.confusion_reason,
                                disambiguating_condition=reply.disambiguating_condition) for u in distractor_units],
        validation_refs=[])
    return question, semantic, TextualAnswer(question_id=qid, checks=[]), source


def make_on_results(page_chars: int = DEFAULT_PAGE_CHARS) -> Callable:
    def on_results(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, rows: list[dict],
                   job_id: uuid.UUID) -> GenerationStats:
        page_count, pages, doc_id = load_clean_pages(conn, project_id, benchmark_id)
        actor = conn.execute("SELECT requested_by FROM generate_jobs WHERE job_id = %s", (job_id,)).fetchone()[0]
        # facts/evidence_sets/gold_answers are under gold RLS (0008): the write is made as the job's owner/maintainer
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))
        existing = {h for (h,) in conn.execute("SELECT question_hash FROM questions WHERE benchmark_id = %s", (benchmark_id,))}
        ordinal = conn.execute("SELECT COALESCE(max(ordinal) + 1, 0) FROM questions WHERE benchmark_id = %s",
                               (benchmark_id,)).fetchone()[0]
        stats = GenerationStats()

        def accept(question, semantic, textual, source, units) -> str:
            """Validate and write one derived question: 'written', 'duplicate' or 'invalid' (never raises on bad input)."""
            nonlocal ordinal
            if question.question_hash in existing:
                return "duplicate"
            if validate_bundle([question], [semantic], [textual], [source], {u.evidence_id: u for u in units}, {}):
                return "invalid"
            ids = [u.evidence_id for u in units]
            unit_rows = dict(conn.execute("SELECT evidence_key, id FROM evidence_units WHERE document_version_id = %s "
                                          "AND evidence_key = ANY(%s)", (doc_id, ids)).fetchall())
            if len(unit_rows) != len(ids):          # a unit that is not stored: skip and count, never raise and roll everything back
                return "invalid"
            _write(conn, project_id, benchmark_id, unit_rows, question, semantic, textual, source)
            existing.add(question.question_hash)
            ordinal += 1
            return "written"

        def shown_units(units: Sequence[EvidenceUnit]) -> list[EvidenceUnit]:
            ids = {eid for eid, _ in shown_pieces([(u.evidence_id, u.normalized_content) for u in units[:MAX_UNITS_IN_PROMPT]], page_chars)}
            return [u for u in units[:MAX_UNITS_IN_PROMPT] if u.evidence_id in ids]

        for row in rows:
            if row["status"] != "ok":
                continue
            stats.replies += 1
            conf_pages = parse_confusable_id(row["call_id"])
            cross_pages = parse_cross_id(row["call_id"])
            if conf_pages is not None:
                reply = parse_confusable_reply(row["text"])
                a_units, d_units = shown_units(pages.get(conf_pages[0], [])), shown_units(pages.get(conf_pages[1], []))
                if reply is None or not a_units or not d_units:
                    stats.unparseable += 1
                    continue
                join = lambda us: "\n\n".join(piece for _, piece in shown_pieces([(u.evidence_id, u.normalized_content) for u in us], page_chars))  # noqa: E731
                text_a, text_b = join(a_units), join(d_units)
                grounded = code_checks(text_a, reply.reference_answer, list(reply.facts))["passed"]
                # the condition that tells A from B must come from what the model was shown (the question or passage A) ...
                condition_ok = code_checks(text_a + "\n" + reply.question, "", [reply.disambiguating_condition])["passed"]
                # ... and B must NOT support the same facts, or it is not a distractor but a second source
                b_also_supports = code_checks(text_b, reply.reference_answer, list(reply.facts))["passed"]
                if not grounded or not condition_ok or b_also_supports:
                    stats.invalid += 1
                    continue
                try:
                    derived = _derive_confusable(reply, a_units, d_units, ordinal)
                except Exception:
                    stats.invalid += 1
                    continue
                outcome = accept(*derived, a_units + d_units)
                if outcome == "written":
                    stats.confusable_written += 1
            elif cross_pages is not None:
                reply = parse_cross_page_reply(row["text"])
                a_units, b_units = shown_units(pages.get(cross_pages[0], [])), shown_units(pages.get(cross_pages[1], []))
                if reply is None or not a_units or not b_units:
                    stats.unparseable += 1
                    continue
                shown = {"A": a_units, "B": b_units}
                texts = {label: "\n\n".join(piece for _, piece in shown_pieces([(u.evidence_id, u.normalized_content) for u in us], page_chars))
                         for label, us in shown.items()}
                # The model's claim of WHICH page a fact comes from is checked by code against the text of that page.
                if not all(code_checks(texts[src], "", [st])["passed"] for st, src in reply.facts):
                    stats.invalid += 1
                    continue
                try:
                    derived = _derive_cross(reply, shown, ordinal)
                except Exception:
                    stats.invalid += 1
                    continue
                outcome = accept(*derived, a_units + b_units)
                if outcome == "written":
                    stats.cross_written += 1
            else:
                reply = parse_model_reply(row["text"])
                _, page = parse_call_id(row["call_id"])
                units = pages.get(page)
                if reply is None or not units:
                    stats.unparseable += 1
                    continue
                units = shown_units(units)         # only the units the model was ACTUALLY shown can be the source
                if not units:
                    stats.unparseable += 1
                    continue
                try:
                    derived = _derive(reply, units, ordinal)
                except Exception:
                    stats.invalid += 1
                    continue
                outcome = accept(*derived, units)
                if outcome == "written":
                    stats.written += 1
            if outcome == "duplicate":
                stats.duplicate += 1
            elif outcome == "invalid":
                stats.invalid += 1
        target = general_target(page_count)
        by_bucket = dict(conn.execute("SELECT quota_bucket, count(*) FROM questions WHERE benchmark_id = %s GROUP BY 1",
                                      (benchmark_id,)).fetchall())
        total = int(by_bucket.get("general", 0))
        cross_total, cross_target = int(by_bucket.get("cross_page", 0)), cross_page_target(page_count)
        conf_total, conf_target = int(by_bucket.get("confusable", 0)), confusable_target(page_count)
        all_total = sum(int(v) for v in by_bucket.values())
        op_id = uuid.uuid4()
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) "
                     "VALUES (%s, %s, 'benchmark.questions_generated', %s, 'succeeded', now())", (op_id, project_id, actor))
        resource = conn.execute("SELECT resource_id FROM benchmark_revisions WHERE id = %s", (benchmark_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, reason, outcome, "
            "operation_id) VALUES (%s, %s, %s, 'benchmark.questions_generated', 'benchmark_revision', %s, %s, 'succeeded', %s)",
            (uuid.uuid4(), project_id, actor, resource,
             f"prompt={PROMPT_VERSION}+{CROSS_PAGE_PROMPT_VERSION}+{CONFUSABLE_PROMPT_VERSION} replies={stats.replies} "
             f"written={stats.written} cross_written={stats.cross_written} confusable_written={stats.confusable_written} unparseable={stats.unparseable} invalid={stats.invalid} "
             f"duplicate={stats.duplicate} total={total} target={target} shortfall={max(0, target - total)} "
             f"cross_total={cross_total} cross_target={cross_target} cross_shortfall={max(0, cross_target - cross_total)} "
             f"confusable_total={conf_total} confusable_target={conf_target} confusable_shortfall={max(0, conf_target - conf_total)}", op_id))
        if all_total > 0:
            # generating -> validating (PM-DECISION): never frozen, human review stays skipped; the shortfall is in the reason
            conn.execute("SELECT set_config('lab.transition_reason', %s, true)",
                         (f"questions generated: general={total}/{target} cross_page={cross_total}/{cross_target} confusable={conf_total}/{conf_target}; "
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
    for d in src.distractors:
        conn.execute("INSERT INTO distractors (question_id, benchmark_id, evidence_unit_id, confusion_reason, disambiguating_condition) "
                     "VALUES (%s, %s, %s, %s, %s)", (qrow, benchmark_id, unit_rows[d.evidence_id], d.confusion_reason, d.disambiguating_condition))
    for kind, model in (("semantic", sem), ("textual", tex), ("source", src)):
        conn.execute("INSERT INTO gold_answers (id, benchmark_id, question_id, kind, payload) VALUES (%s, %s, %s, %s, %s)",
                     (uuid.uuid4(), benchmark_id, qrow, kind, Jsonb(json.loads(model.model_dump_json()))))
