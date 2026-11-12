"""Runs of the answering method as generation-style jobs (PM-DECISION on Issue #7, item (a), Refs #12). See docs/LAB_GEN.md.

* ``start_answer_run`` (API side): frozen benchmark only, owner/maintainer; creates the method revision, the run (queued) and an
  ``answer`` job in ``generate_jobs``; refuses a second concurrent method run on the same benchmark (409).
* ``make_answer_hooks`` (worker side): ``build_calls`` is the PARTICIPANT PHASE and runs in a transaction with ``lab.actor_id``
  UNSET, so the gold row level security (0008/0015) denies gold even if this code regressed; it reads only the original PDF
  bytes and the question keys and texts. ``on_results`` records every question (a submission each), then, with the actor set,
  scores the source metrics with the same code as the baseline.
* Failures are never confused with abstentions (PM Q3): a model abstention is ``answer_status = 'abstained'`` with the model's
  reason; budget/provider/contract/unknown-outcome failures carry a fixed ``failure_reason`` and their answer metrics are
  recorded as ``error`` with that code. Retrieval metrics are still scored from the real hits."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Callable, Optional, Sequence

import psycopg

from lab_contracts.method import QueryBudget, QueryRequest
from lab_db.generation import request_sha256
from lab_db.jobs import ACTIVE_JOB_STATUSES
from lab_db.repo import Change, NotFound, PermissionDenied, authorize, record_change
from lab_db.storage import LocalArtifactStore, store_and_register
from lab_parser import local_methods
from lab_api import baseline_eval as be
from lab_api.generate_worker import CallSpec
from lab_api.method_answer import (ANSWER_PROMPT_VERSION, METHOD_LIMITS, answer_response, build_answer_call, failure_response,
                                   parse_answer_reply)

METHOD_LOGICAL_ID = "local-retrieval-answer"
BUDGET = dict(be.BUDGET)
ACTION = "register_method_and_submit"
MAX_QUESTIONS = be.MAX_QUESTIONS


class AnswerRunError(Exception):
    """The method run cannot start (fixed, value-free message)."""


def start_answer_run(conn: psycopg.Connection, project_id: uuid.UUID, benchmark_id: uuid.UUID, actor_id: uuid.UUID, *,
                     model: str, budget_cap_micros: int, currency: str = "USD", pricing_revision: str = "configured-prices",
                     max_output_tokens: int = 500) -> dict:
    """Create the method revision, the run and the answer job. Authorized for ``register_method_and_submit``."""
    from lab_db.generation import MAX_OUTPUT_TOKENS, benchmark_budget
    if not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS or budget_cap_micros <= 0:
        raise AnswerRunError("max_output_tokens_or_budget_out_of_range")
    decision = authorize(conn, project_id, actor_id, ACTION)
    if decision.condition is not None:
        raise PermissionDenied(f"{decision.role} may not run a method on this benchmark")
    run_id, job_id, op_id, method_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"answer_run:{project_id}:{benchmark_id}",))
        # revision numbers are per (project, method): serialize across benchmarks too (as the baseline does)
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"method_revision:{project_id}:{METHOD_LOGICAL_ID}",))
        bench = conn.execute("SELECT status::text FROM benchmark_revisions WHERE project_id = %s AND id = %s FOR SHARE",
                             (project_id, benchmark_id)).fetchone()
        if bench is None:
            raise NotFound("benchmark")
        if bench[0] != "frozen":
            raise AnswerRunError("benchmark_is_not_frozen")
        if conn.execute("SELECT count(*) FROM questions WHERE benchmark_id = %s", (benchmark_id,)).fetchone()[0] > MAX_QUESTIONS:
            raise AnswerRunError(f"benchmark_has_more_than_{MAX_QUESTIONS}_questions")
        active = conn.execute(
            "SELECT 1 FROM generate_jobs g JOIN jobs j ON j.project_id = g.project_id AND j.id = g.job_id "
            "WHERE g.project_id = %s AND g.benchmark_id = %s AND g.phase = 'answer' AND j.status::text = ANY(%s) LIMIT 1",
            (project_id, benchmark_id, list(ACTIVE_JOB_STATUSES))).fetchone()
        if active is not None:
            raise AnswerRunError("a_method_run_is_already_active_for_this_benchmark")
        used = benchmark_budget(conn, project_id, benchmark_id, 0)
        total_cap = used.spent + used.held + budget_cap_micros          # the request is THIS run's budget
        config_hash = hashlib.sha256(json.dumps({"method": "local-retrieval-answer-1", "prompt": ANSWER_PROMPT_VERSION,
                                                 "model": model, "budget": BUDGET}, sort_keys=True).encode()).hexdigest()
        revision = 1 + conn.execute("SELECT count(*) FROM resource_versions WHERE project_id = %s AND resource_type = 'method_revision' "
                                    "AND logical_id = %s", (project_id, METHOD_LOGICAL_ID)).fetchone()[0]
        resource_id = record_change(conn, Change(project_id, actor_id, "method.register", "method_revision", METHOD_LOGICAL_ID,
                                                 revision, config_hash, "lab-c14n-1", "method.registered"))
        conn.execute("INSERT INTO method_revisions (id, project_id, resource_id, owner_actor_id, protocol_version, config_hash, "
                     "execution_mode, visibility) VALUES (%s, %s, %s, %s, '1.0', %s, 'jsonl', 'lab_observed')",
                     (method_id, project_id, resource_id, actor_id, config_hash))
        conn.execute("INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile) VALUES (%s, %s, %s, %s, %s)",
                     (run_id, project_id, benchmark_id, method_id, "answer-top5"))
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) "
                     "VALUES (%s, %s, 'method.run', %s, 'succeeded', now())", (op_id, project_id, actor_id))
        conn.execute("INSERT INTO jobs (id, project_id, kind, stage, max_attempts, retryable) VALUES (%s, %s, 'document.generate', 'queued', 3, true)",
                     (job_id, project_id))
        conn.execute(
            "INSERT INTO generate_jobs (job_id, project_id, benchmark_id, operation_id, requested_by, model, max_output_tokens, "
            "budget_cap_micros, currency, pricing_revision, phase, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'answer', %s)",
            (job_id, project_id, benchmark_id, op_id, actor_id, model, max_output_tokens, total_cap, currency, pricing_revision, run_id))
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, outcome, operation_id) "
            "VALUES (%s, %s, %s, 'run.requested', 'run', %s, 'succeeded', %s)", (uuid.uuid4(), project_id, actor_id, run_id, op_id))
    return {"run_id": str(run_id), "job_id": str(job_id), "method": "local-retrieval-answer-1", "status": "queued",
            "model": model, "max_output_tokens": max_output_tokens, "budget_cap_micros": budget_cap_micros,
            "benchmark_cap_micros": total_cap, "prompt_version": ANSWER_PROMPT_VERSION, "limits": METHOD_LIMITS}


def _transition(conn, run_id, to_status, reason, finalize=False):
    be._transition(conn, run_id, to_status, reason, finalize)


def make_answer_hooks(store: LocalArtifactStore, run_id: uuid.UUID) -> tuple[Callable, Callable]:
    """(build_calls, on_results) for one run. See the module docstring for the isolation rules."""

    def index_and_questions(conn, project_id, benchmark_id):
        # PARTICIPANT inputs only: the original PDF bytes and the question keys/texts. No other table is read.
        doc = conn.execute("SELECT br.document_version_id, dv.sha256, a.object_key, a.size_bytes FROM benchmark_revisions br "
                           "JOIN document_versions dv ON dv.project_id = br.project_id AND dv.id = br.document_version_id "
                           "JOIN artifacts a ON a.id = dv.artifact_id WHERE br.project_id = %s AND br.id = %s",
                           (project_id, benchmark_id)).fetchone()
        if doc is None:
            raise NotFound("document")
        doc_id, sha, object_key, size = doc
        data = store.open(object_key, expected_size=size, max_bytes=local_methods.MethodConfig().max_pdf_bytes)
        if hashlib.sha256(data).hexdigest() != sha:
            raise AnswerRunError("document_bytes_do_not_match")
        index = local_methods.prepare(data, document_id=str(doc_id), expected_sha256=sha, config=local_methods.MethodConfig(strategy="page"))
        questions = conn.execute("SELECT id, question_key, question_text, ordinal FROM questions WHERE benchmark_id = %s ORDER BY ordinal",
                                 (benchmark_id,)).fetchall()
        return doc_id, index, questions

    def retrieve(index, qkey, qtext):
        request = QueryRequest(protocol_version="1.0", request_id=f"req-{qkey}", run_id=str(run_id), question_id=qkey,
                               index_id=index.index_id, question=qtext, budget=QueryBudget(**BUDGET))
        return local_methods.query(index, request)

    def build_calls(conn, project_id, benchmark_id) -> Sequence[CallSpec]:
        conn.execute("SELECT set_config('lab.actor_id', '', true)")        # defence in depth: the gold RLS denies everything here
        status = conn.execute("SELECT status::text FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
        if status == "queued":
            _transition(conn, run_id, "preparing", "answering method: indexing the original PDF")
            _transition(conn, run_id, "running", "answering method: querying every question")
        _, index, questions = index_and_questions(conn, project_id, benchmark_id)
        return [build_answer_call(f"a{ordinal:04d}", qtext, retrieve(index, qkey, qtext).retrieved_hits)
                for _, qkey, qtext, ordinal in questions]

    def on_results(conn, project_id, benchmark_id, rows, job_id) -> dict:
        conn.execute("SELECT set_config('lab.actor_id', '', true)")        # the participant records are written WITHOUT an actor
        model = conn.execute("SELECT model FROM generate_jobs WHERE job_id = %s", (job_id,)).fetchone()[0]
        by_call = {r["call_id"]: r for r in rows}
        doc_id, index, questions = index_and_questions(conn, project_id, benchmark_id)
        evaluation_id = uuid.uuid4()
        participants = []
        for qid, qkey, qtext, ordinal in questions:                                  # 1. participant records, NO actor
            row = by_call.get(f"a{ordinal:04d}")
            retrieved = retrieve(index, qkey, qtext)
            hits = retrieved.retrieved_hits
            code, response = None, None
            sent = row is not None and row["status"] in ("ok", "failed", "outcome_unknown")
            telemetry = {"latency_ms": None, "retrieval_calls": 1, "model_calls": 1 if sent else 0,
                         "input_tokens": row["input_tokens"] if row and row.get("usage_known") else None,
                         "output_tokens": row["output_tokens"] if row and row.get("usage_known") else None}
            expected = build_answer_call(f"a{ordinal:04d}", qtext, hits)
            if row is None:
                code = "outcome_unknown"
            elif row.get("request_sha256") and row["request_sha256"] != request_sha256(model, expected.system, expected.user):
                # The retrieval re-run here does NOT reproduce the prompt that was sent (a nondeterministic method, or a changed
                # document): the answer cannot be mapped to these hits, so it is not scored as an answer.
                code = "outcome_unknown"
            elif row["status"] == "ok":
                reply = parse_answer_reply(row["text"])
                response = answer_response(qkey, f"req-{qkey}", index.index_id, hits, reply, telemetry) if reply else None
                if response is None:
                    code = "invalid_model_reply"
            elif row["status"] in ("skipped_budget", "skipped_after_stop"):
                code = "budget_exhausted"
            elif row["status"] == "failed":
                code = "provider_error"
            else:
                code = "outcome_unknown"
            if response is None:
                response = failure_response(qkey, f"req-{qkey}", index.index_id, hits, code, telemetry)
            payload = response.model_dump_json().encode("utf-8")
            attempt_id, submission_id = uuid.uuid4(), uuid.uuid4()
            conn.execute("INSERT INTO attempts (id, run_id, benchmark_id, question_id, attempt_number, transport) "
                         "VALUES (%s, %s, %s, %s, 1, 'jsonl')", (attempt_id, run_id, benchmark_id, qid))
            artifact_id = store_and_register(conn, store, project_id, payload, "application/json")
            conn.execute("INSERT INTO submissions (id, run_id, attempt_id, question_id, status, answer_status, payload_hash, "
                         "c14n_version, payload_artifact_id, failure_reason) VALUES (%s, %s, %s, %s, 'accepted', %s, %s, 'lab-c14n-1', %s, %s)",
                         (submission_id, run_id, attempt_id, qid, response.status, hashlib.sha256(payload).hexdigest(), artifact_id, code))
            for hit in response.retrieved_hits:
                conn.execute("INSERT INTO retrieved_hits (submission_id, hit_key, rank, content) VALUES (%s, %s, %s, %s)",
                             (submission_id, hit.hit_id, hit.rank, hit.content))
            for cit in response.citations:
                conn.execute("INSERT INTO citations (submission_id, claim_key, hit_key, region_indices) VALUES (%s, %s, %s, %s)",
                             (submission_id, cit.claim_id, cit.hit_id, list(cit.region_indices)))
            participants.append((qid, qkey, submission_id, response, code))
        # 2. scoring: gold is read from here on, as the job's requester (an owner/maintainer)
        actor = conn.execute("SELECT requested_by FROM generate_jobs WHERE job_id = %s", (job_id,)).fetchone()[0]
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))
        conn.execute("INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) VALUES (%s, %s, %s, %s, 'provisional')",
                     (evaluation_id, run_id, benchmark_id, be.GRADER_REVISION))
        failures: dict[str, int] = {}
        for qid, qkey, submission_id, response, code in participants:
            if code:
                failures[code] = failures.get(code, 0) + 1
            for result in be._score_question(conn, benchmark_id, doc_id, qid, qkey, response, failure_code=code):
                be._store_score(conn, evaluation_id, run_id, benchmark_id, qid, submission_id, result)
        _transition(conn, run_id, "evaluating", "answering method: scoring with the rule-based scorer")
        _transition(conn, run_id, "completed_with_errors" if failures else "completed",
                    "answering method scored; semantic metrics are pending (judge not run)"
                    + (f"; failures: {json.dumps(failures, sort_keys=True)}" if failures else ""), finalize=True)
        return {"evaluation_id": str(evaluation_id), "failures": failures}

    return build_calls, on_results
