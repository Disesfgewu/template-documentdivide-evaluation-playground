"""Run ONE local method against a frozen benchmark and score it with the rule-based scorer (PM-DECISION item 3, Refs #10/#12).

The method is the existing local lexical baseline (``lab_parser.local_methods``, raw-PDF page retrieval, participant-side:
it sees the original PDF and the question text only, never system parse output or gold). For every question of the frozen
benchmark this records, through the existing tables, a run, an attempt, an accepted submission (the validated response as an
artifact) and its retrieved hits; then an ``evaluation`` with ``question_scores`` from ``lab_scoring`` (SPEC 16.5):

* ``recall_at_k`` and ``complete_at_k`` from the gold evidence sets, a unit counting as retrieved when the returned regions cover
  each of its gold regions by at least 4/5 (SPEC 9.1);
* ``localization_precision`` of all returned regions against the first gold set's regions (the baseline returns coarse
  full-page regions, so this number is low by construction: that is a true property of the method, not a bug);
* ``textual_consistency``: not applicable (generated questions carry no textual checks);
* ``correctness``, ``citation_precision``, ``citation_completeness`` and ``faithfulness`` need the semantic judge, which has
  not run: they are recorded ``pending`` with reason ``semantic_judge_not_run``, never guessed and never zero.
The evaluation is ``provisional``. No model is called."""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from fractions import Fraction

import psycopg

from lab_contracts.method import QueryBudget, QueryRequest
from lab_contracts.score import MetricResult, ROUNDING_VERSION
from lab_db.repo import Change, NotFound, PermissionDenied, authorize, record_change
from lab_db.storage import LocalArtifactStore, store_and_register
from lab_parser import local_methods
from lab_scoring import rules

GRADER_REVISION = "rule-based-scorer-1"
PENDING_METRICS = ("correctness", "citation_precision", "citation_completeness", "faithfulness")
PENDING_REASON = "semantic_judge_not_run"
BUDGET = {"top_k": 5, "max_evidence_tokens": 200_000, "max_visual_regions": 5, "timeout_seconds": 30}
ACTION = "register_method_and_submit"
METHOD_LOGICAL_ID = "local-lexical-baseline"


class BaselineError(Exception):
    """The baseline cannot run (fixed, value-free message)."""


def _fraction(x: float) -> Fraction:
    return Fraction(Decimal(repr(float(x))))


def _transition(conn, run_id, to_status, reason, finalize=False):
    conn.execute("SELECT set_config('lab.transition_reason', %s, true)", (reason,))
    conn.execute("UPDATE runs SET status = %s, row_version = row_version + 1, "
                 "finalized_at = CASE WHEN %s THEN now() ELSE finalized_at END WHERE id = %s", (to_status, finalize, run_id))


def _pending(question_id: str, metric: str) -> MetricResult:
    return MetricResult.model_validate({
        "question_id": question_id, "metric": metric, "status": "pending", "raw_value": None, "display_value": None,
        "rounding": ROUNDING_VERSION, "numerator": None, "denominator": None, "reason_codes": [PENDING_REASON],
        "grader_revision": GRADER_REVISION, "support_refs": []})


def run_local_baseline(conn: psycopg.Connection, store: LocalArtifactStore, project_id: uuid.UUID,
                       benchmark_id: uuid.UUID, actor_id: uuid.UUID, strategy: str = "page") -> dict:
    """Run and score the baseline. Authorized for ``register_method_and_submit`` (owner/maintainer); the benchmark must be frozen."""
    decision = authorize(conn, project_id, actor_id, ACTION)
    if decision.condition is not None:
        raise PermissionDenied(f"{decision.role} may not run a method on this benchmark")
    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))      # gold RLS (owner/maintainer)
        bench = conn.execute("SELECT status::text, document_version_id FROM benchmark_revisions "
                             "WHERE project_id = %s AND id = %s FOR SHARE", (project_id, benchmark_id)).fetchone()
        if bench is None:
            raise NotFound("benchmark")
        if bench[0] != "frozen":
            raise BaselineError("benchmark_is_not_frozen")
        doc_id = bench[1]
        row = conn.execute(
            "SELECT dv.sha256, a.object_key, a.size_bytes FROM document_versions dv JOIN artifacts a ON a.id = dv.artifact_id "
            "WHERE dv.project_id = %s AND dv.id = %s", (project_id, doc_id)).fetchone()
        sha, object_key, size = row
        data = store.open(object_key, expected_size=size, max_bytes=local_methods.MethodConfig().max_pdf_bytes)
        if hashlib.sha256(data).hexdigest() != sha:
            raise BaselineError("document_bytes_do_not_match")
        index = local_methods.prepare(data, document_id=str(doc_id), expected_sha256=sha,
                                      config=local_methods.MethodConfig(strategy=strategy))

        # the method revision (a resource version) and the run
        config_hash = hashlib.sha256(json.dumps({"method": index.method_revision, "budget": BUDGET}, sort_keys=True).encode()).hexdigest()
        revision = 1 + conn.execute("SELECT count(*) FROM resource_versions WHERE project_id = %s AND resource_type = 'method_revision' "
                                    "AND logical_id = %s", (project_id, METHOD_LOGICAL_ID)).fetchone()[0]
        resource_id = record_change(conn, Change(project_id, actor_id, "method.register", "method_revision", METHOD_LOGICAL_ID,
                                                 revision, config_hash, "lab-c14n-1", "method.registered"))
        method_id, run_id, evaluation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        conn.execute("INSERT INTO method_revisions (id, project_id, resource_id, owner_actor_id, protocol_version, config_hash, "
                     "execution_mode, visibility) VALUES (%s, %s, %s, %s, '1.0', %s, 'jsonl', 'lab_observed')",
                     (method_id, project_id, resource_id, actor_id, config_hash))
        conn.execute("INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile) VALUES (%s, %s, %s, %s, %s)",
                     (run_id, project_id, benchmark_id, method_id, "baseline-top5"))
        _transition(conn, run_id, "preparing", "local baseline: indexing the original PDF")
        _transition(conn, run_id, "running", "local baseline: querying every question")

        questions = conn.execute("SELECT id, question_key, question_text FROM questions WHERE benchmark_id = %s ORDER BY ordinal",
                                 (benchmark_id,)).fetchall()
        conn.execute("INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) VALUES (%s, %s, %s, %s, 'provisional')",
                     (evaluation_id, run_id, benchmark_id, GRADER_REVISION))
        scored = 0
        for qid, qkey, qtext in questions:
            request = QueryRequest(protocol_version="1.0", request_id=f"req-{qkey}", run_id=str(run_id), question_id=qkey,
                                   index_id=index.index_id, question=qtext, budget=QueryBudget(**BUDGET))
            response = local_methods.query(index, request)
            payload = response.model_dump_json().encode("utf-8")
            attempt_id, submission_id = uuid.uuid4(), uuid.uuid4()
            conn.execute("INSERT INTO attempts (id, run_id, benchmark_id, question_id, attempt_number, transport) "
                         "VALUES (%s, %s, %s, %s, 1, 'jsonl')", (attempt_id, run_id, benchmark_id, qid))
            artifact_id = store_and_register(conn, store, project_id, payload, "application/json")
            conn.execute("INSERT INTO submissions (id, run_id, attempt_id, question_id, status, answer_status, payload_hash, "
                         "c14n_version, payload_artifact_id) VALUES (%s, %s, %s, %s, 'accepted', %s, %s, 'lab-c14n-1', %s)",
                         (submission_id, run_id, attempt_id, qid, response.status, hashlib.sha256(payload).hexdigest(), artifact_id))
            for hit in response.retrieved_hits:
                conn.execute("INSERT INTO retrieved_hits (submission_id, hit_key, rank, content) VALUES (%s, %s, %s, %s)",
                             (submission_id, hit.hit_id, hit.rank, hit.content))
            for result in _score_question(conn, benchmark_id, doc_id, qid, qkey, response):
                _store_score(conn, evaluation_id, run_id, benchmark_id, qid, submission_id, result)
                scored += result.status == "scored"
        _transition(conn, run_id, "evaluating", "local baseline: scoring with the rule-based scorer")
        _transition(conn, run_id, "completed", "local baseline scored; semantic metrics are pending (judge not run)", finalize=True)
    return {"run_id": str(run_id), "evaluation_id": str(evaluation_id), "method_revision": index.method_revision,
            "questions": len(questions), "scored_metric_values": scored, "status": "completed",
            "evaluation_status": "provisional", "pending_metrics": list(PENDING_METRICS)}


def _score_question(conn, benchmark_id, doc_id, qid, qkey, response) -> list[MetricResult]:
    source = conn.execute("SELECT payload FROM gold_answers WHERE question_id = %s AND kind = 'source'", (qid,)).fetchone()
    if source is None:
        raise BaselineError("a question has no source gold")
    gold_sets = [set(s["evidence_ids"]) for s in source[0]["evidence_sets"]]
    ids = sorted(set().union(*gold_sets))
    unit_regions: dict[str, list[rules.Region]] = {}
    for key, page, x0, y0, x1, y1 in conn.execute(
            "SELECT eu.evidence_key, er.page_index, er.x0, er.y0, er.x1, er.y1 FROM evidence_units eu "
            "JOIN evidence_regions er ON er.evidence_unit_id = eu.id WHERE eu.document_version_id = %s "
            "AND eu.evidence_key = ANY(%s) ORDER BY eu.evidence_key, er.ordinal", (doc_id, ids)).fetchall():
        unit_regions.setdefault(key, []).append(rules.Region(page, _fraction(x0), _fraction(y0), _fraction(x1), _fraction(y1)))
    returned = [rules.Region(r.page_index, _fraction(r.bbox[0]), _fraction(r.bbox[1]), _fraction(r.bbox[2]), _fraction(r.bbox[3]))
                for h in response.retrieved_hits for r in h.regions]
    matched = {k for k, regs in unit_regions.items() if rules.unit_hit(returned, regs)}
    first_set_regions = [r for k in sorted(gold_sets[0]) for r in unit_regions.get(k, [])]
    out = [rules.recall_at_k(qkey, gold_sets, matched, GRADER_REVISION),
           rules.complete_at_k(qkey, gold_sets, matched, GRADER_REVISION),
           rules.localization_precision(qkey, returned, first_set_regions, GRADER_REVISION),
           rules.textual_consistency(qkey, 0, 0, GRADER_REVISION)]
    out += [_pending(qkey, m) for m in PENDING_METRICS]
    return out


def _store_score(conn, evaluation_id, run_id, benchmark_id, qid, submission_id, r: MetricResult) -> None:
    conn.execute(
        "INSERT INTO question_scores (evaluation_id, run_id, benchmark_id, question_id, submission_id, metric_name, status, "
        "raw_value, numerator, denominator, reason_codes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (evaluation_id, run_id, benchmark_id, qid, submission_id, r.metric, r.status, r.raw_value, r.numerator, r.denominator,
         list(r.reason_codes)))
