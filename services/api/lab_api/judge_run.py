"""Judge ONE finished answering-method run (PM-DECISION GO on Issue #7, Refs #12).

A job of phase ``judge`` (migration 0019) makes one lab-gen call per ANSWERED submission of the run: the gold facts of the
question, the submission's answer and claims, and the text of the hits those claims cite (``method_judge``). It writes a NEW
evaluation for the same run (never touching old score rows); the rule-based metrics (recall, complete, localization, textual)
are copied unchanged from the run's latest evaluation, and the four semantic metrics come from the judge:

* answered + valid verdict: correctness, faithfulness, citation_precision, citation_completeness, DERIVED IN CODE from the verdict
  (fact weights from the gold); the rows carry the judge model, prompt revision, prompt sha256 and the limitation text;
* model abstention: correctness by the existing abstention rule (scored 0, reason ``abstained``), the other three not_applicable;
  no call is made for it (no spend);
* method failure (budget, provider, contract, unknown outcome): ``error`` with the failure's code, as in the answering run;
* a judge failure (budget, provider, unknown outcome, unparseable or mismatching verdict, oversized answer): ``error`` with a
  fixed reason, never a guessed value.
The evaluation is ``provisional`` and the run keeps its own status."""
from __future__ import annotations

import hashlib
import json
import uuid
from fractions import Fraction
from typing import Callable, Sequence

import psycopg

from lab_contracts.method import QueryResponse
from lab_db.jobs import ACTIVE_JOB_STATUSES
from lab_db.repo import NotFound, PermissionDenied, authorize
from lab_db.storage import LocalArtifactStore
from lab_api import baseline_eval as be
from lab_api.generate_worker import CallSpec
from lab_api.method_judge import (JUDGE_GRADER_SUFFIX, JUDGE_LIMITATION, JUDGE_PROMPT_REVISION, JUDGE_PROMPT_SHA256, MAX_JUDGED_CLAIMS,
                                  build_judge_call, check_verdict, parse_judge_reply)
from lab_scoring import rules

ACTION = "register_method_and_submit"
RULE_METRICS = ("recall_at_k", "complete_at_k", "localization_precision", "textual_consistency")
JUDGED_METRICS = be.PENDING_METRICS
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
JUDGE_LIMITS = [
    "The judge is the same provider/model family that generated the questions and is uncalibrated against human labels: every "
    "judged score is provisional.",
    "One call per answered submission, at most 500 output tokens; it sees the gold facts, the answer, its claims and the cited text only.",
    "Scores are derived in code from the verdict; an unparseable or mismatching verdict is an error for that question, never a guess.",
    "Human review is skipped; this evaluation is never published.",
]


class JudgeRunError(Exception):
    """The judge run cannot start (fixed, value-free message)."""


def start_judge_run(conn: psycopg.Connection, project_id: uuid.UUID, run_id: uuid.UUID, actor_id: uuid.UUID, *, model: str,
                    budget_cap_micros: int, currency: str = "USD", pricing_revision: str = "configured-prices",
                    max_output_tokens: int = 500) -> dict:
    """Queue the judge job for a finished answering run. Authorized for ``register_method_and_submit`` (owner/maintainer)."""
    from lab_db.generation import MAX_OUTPUT_TOKENS, benchmark_budget
    if not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS or budget_cap_micros <= 0:
        raise JudgeRunError("max_output_tokens_or_budget_out_of_range")
    decision = authorize(conn, project_id, actor_id, ACTION)
    if decision.condition is not None:
        raise PermissionDenied(f"{decision.role} may not judge a run on this benchmark")
    job_id, op_id = uuid.uuid4(), uuid.uuid4()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"judge_run:{project_id}:{run_id}",))
        run = conn.execute("SELECT r.benchmark_id, r.status::text, b.status::text FROM runs r JOIN benchmark_revisions b "
                           "ON b.id = r.benchmark_id WHERE r.id = %s AND r.project_id = %s FOR SHARE OF r", (run_id, project_id)).fetchone()
        if run is None:
            raise NotFound("run")
        benchmark_id, run_status, bench_status = run
        if bench_status != "frozen":
            raise JudgeRunError("benchmark_is_not_frozen")
        if run_status not in ("completed", "completed_with_errors"):
            raise JudgeRunError("the_run_is_not_finished")
        if conn.execute("SELECT 1 FROM generate_jobs WHERE project_id = %s AND run_id = %s AND phase = 'answer'",
                        (project_id, run_id)).fetchone() is None:
            raise JudgeRunError("only_an_answering_method_run_can_be_judged")
        active = conn.execute(
            "SELECT 1 FROM generate_jobs g JOIN jobs j ON j.project_id = g.project_id AND j.id = g.job_id "
            "WHERE g.project_id = %s AND g.benchmark_id = %s AND g.phase IN ('judge', 'answer') AND j.status::text = ANY(%s) LIMIT 1",
            (project_id, benchmark_id, list(ACTIVE_JOB_STATUSES))).fetchone()
        if active is not None:
            raise JudgeRunError("a_method_or_judge_run_is_already_active_for_this_benchmark")
        answered = conn.execute("SELECT count(*) FROM submissions WHERE run_id = %s AND status = 'accepted' AND answer_status = 'answered' "
                                "AND failure_reason IS NULL", (run_id,)).fetchone()[0]
        if answered == 0:
            raise JudgeRunError("the_run_has_no_answered_submission_to_judge")
        used = benchmark_budget(conn, project_id, benchmark_id, 0)
        total_cap = used.spent + used.held + budget_cap_micros          # the request is THIS judge run's budget
        conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) "
                     "VALUES (%s, %s, 'method.run', %s, 'succeeded', now())", (op_id, project_id, actor_id))
        conn.execute("INSERT INTO jobs (id, project_id, kind, stage, max_attempts, retryable) VALUES (%s, %s, 'document.generate', 'queued', 3, true)",
                     (job_id, project_id))
        conn.execute(
            "INSERT INTO generate_jobs (job_id, project_id, benchmark_id, operation_id, requested_by, model, max_output_tokens, "
            "budget_cap_micros, currency, pricing_revision, phase, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'judge', %s)",
            (job_id, project_id, benchmark_id, op_id, actor_id, model, max_output_tokens, total_cap, currency, pricing_revision, run_id))
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, outcome, operation_id) "
            "VALUES (%s, %s, %s, 'run.requested', 'run', %s, 'succeeded', %s)", (uuid.uuid4(), project_id, actor_id, run_id, op_id))
    return {"run_id": str(run_id), "job_id": str(job_id), "status": "queued", "model": model, "max_output_tokens": max_output_tokens,
            "budget_cap_micros": budget_cap_micros, "benchmark_cap_micros": total_cap, "answered_submissions": answered,
            "judge_prompt_revision": JUDGE_PROMPT_REVISION, "judge_prompt_sha256": JUDGE_PROMPT_SHA256, "limits": JUDGE_LIMITS}


def _store_score(conn, evaluation_id, run_id, benchmark_id, qid, submission_id, r, judged: bool, model: str) -> None:
    conn.execute(
        "INSERT INTO question_scores (evaluation_id, run_id, benchmark_id, question_id, submission_id, metric_name, status, "
        "raw_value, numerator, denominator, reason_codes, judge_model, judge_prompt_revision, judge_prompt_sha256, judge_limitation) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (evaluation_id, run_id, benchmark_id, qid, submission_id, r.metric, r.status, r.raw_value, r.numerator, r.denominator,
         list(r.reason_codes), model if judged else None, JUDGE_PROMPT_REVISION if judged else None,
         JUDGE_PROMPT_SHA256 if judged else None, JUDGE_LIMITATION if judged else None))


def make_judge_hooks(store: LocalArtifactStore, run_id: uuid.UUID) -> tuple[Callable, Callable]:
    """(build_calls, on_results) for one judge job. Both run as the job's requester (an owner or maintainer): the gold is read here."""

    def load(conn, benchmark_id) -> list[dict]:
        """Every question of the benchmark with its submission of this run, in order. Deterministic and read-only."""
        out = []
        subs = conn.execute(
            "SELECT q.id, q.question_key, q.ordinal, s.id, s.answer_status, s.failure_reason, s.payload_hash, a.object_key, a.size_bytes "
            "FROM questions q LEFT JOIN submissions s ON s.question_id = q.id AND s.run_id = %s AND s.status = 'accepted' "
            "LEFT JOIN artifacts a ON a.id = s.payload_artifact_id WHERE q.benchmark_id = %s ORDER BY q.ordinal",
            (run_id, benchmark_id)).fetchall()
        for qid, qkey, ordinal, sid, status, failure, phash, object_key, size in subs:
            item = {"qid": qid, "qkey": qkey, "ordinal": ordinal, "sid": sid, "status": status, "failure": failure,
                    "call_id": f"j{ordinal:04d}", "facts": None, "weights": None, "response": None, "claims": None}
            gold = conn.execute("SELECT payload FROM gold_answers WHERE question_id = %s AND kind = 'semantic'", (qid,)).fetchone()
            facts = (gold[0].get("required_facts") if gold else None) or []
            if facts and all(isinstance(f, dict) and isinstance(f.get("statement"), str) and f.get("weight") is not None for f in facts):
                item["facts"] = [f["statement"] for f in facts]
                item["weights"] = [be._fraction(f["weight"]) for f in facts]
            if sid is not None and status == "answered" and failure is None:
                data = store.open(object_key, expected_size=size, max_bytes=MAX_PAYLOAD_BYTES)
                if hashlib.sha256(data).hexdigest() != phash:
                    raise be.BaselineError("a_submission_payload_does_not_match_its_hash")
                resp = QueryResponse.model_validate_json(data)
                cited: dict[str, list[str]] = {}
                for cit in resp.citations:
                    cited.setdefault(cit.claim_id, []).append(cit.hit_id)
                item["response"] = resp
                item["claims"] = [(c.claim_id, c.text, list(dict.fromkeys(cited.get(c.claim_id, [])))) for c in resp.answer.claims]
            out.append(item)
        return out

    def judgeable(it) -> bool:
        return it["response"] is not None and it["facts"] is not None and len(it["claims"]) <= MAX_JUDGED_CLAIMS

    def build_calls(conn, project_id, benchmark_id) -> Sequence[CallSpec]:
        calls = []
        for it in load(conn, benchmark_id):
            if judgeable(it):
                hits = {h.hit_id: h.content for h in it["response"].retrieved_hits}
                calls.append(build_judge_call(it["call_id"], it["facts"], it["response"].answer.text, it["claims"], hits))
        return calls

    def derive(it, verdict, grader) -> list:
        qkey = it["qkey"]
        score = {label: Fraction(1) if v == "stated" else Fraction(0) for label, v in verdict.facts}
        facts = [(w, score[f"f{i}"]) for i, w in enumerate(it["weights"], start=1)]
        supported = {cid: (ok, set(hids)) for cid, ok, hids in verdict.claims}
        citations = [(cit.claim_id, frozenset({cit.hit_id}), cit.hit_id in supported[cit.claim_id][1]) for cit in it["response"].citations]
        fully = sum(1 for ok, hids in supported.values() if ok and hids)
        return [rules.correctness(qkey, facts, False, grader),
                rules.faithfulness(qkey, len(supported), sum(1 for ok, _ in supported.values() if ok), grader),
                rules.citation_precision(qkey, citations, True, grader),
                rules.citation_completeness(qkey, len(supported), fully, grader)]

    def on_results(conn, project_id, benchmark_id, rows, job_id) -> dict:
        row = conn.execute("SELECT model, requested_by FROM generate_jobs WHERE job_id = %s", (job_id,)).fetchone()
        model, actor = row
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor),))        # gold is read as the requester
        by_call = {r["call_id"]: r for r in rows}
        items = load(conn, benchmark_id)
        prev = conn.execute("SELECT id, grader_revision FROM evaluations WHERE run_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                            (run_id,)).fetchone()
        if prev is None:
            raise be.BaselineError("the_run_has_no_evaluation_to_extend")
        grader = prev[1].split("+")[0] + JUDGE_GRADER_SUFFIX
        evaluation_id = uuid.uuid4()
        conn.execute("INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) VALUES (%s, %s, %s, %s, 'provisional')",
                     (evaluation_id, run_id, benchmark_id, grader))
        conn.execute(
            "INSERT INTO question_scores (evaluation_id, run_id, benchmark_id, question_id, submission_id, metric_name, status, "
            "raw_value, numerator, denominator, reason_codes) SELECT %s, run_id, benchmark_id, question_id, submission_id, metric_name, "
            "status, raw_value, numerator, denominator, reason_codes FROM question_scores WHERE evaluation_id = %s AND metric_name = ANY(%s)",
            (evaluation_id, prev[0], list(RULE_METRICS)))
        counts: dict[str, int] = {}
        for it in items:
            qkey = it["qkey"]
            judged = False
            if it["sid"] is None:
                continue                                    # no submission for this question in the run: nothing to judge
            if it["failure"] is not None:                   # the answering METHOD failed: error with its code
                results = [rules._error(qkey, m, grader, [it["failure"]]) for m in JUDGED_METRICS]
            elif it["status"] == "abstained":               # the model abstained: no call, no spend
                if it["facts"] is None:
                    results = [rules._error(qkey, m, grader, ["gold_facts_missing"]) for m in JUDGED_METRICS]
                else:
                    results = [rules.correctness(qkey, [(w, Fraction(0)) for w in it["weights"]], False, grader, abstained=True)]
                    results += [rules._na(qkey, m, grader, ["abstained"]) for m in JUDGED_METRICS[1:]]
            else:
                judged = True
                code, results = None, None
                call = by_call.get(it["call_id"])
                if it["facts"] is None:
                    code, judged = "gold_facts_missing", False
                elif len(it["claims"]) > MAX_JUDGED_CLAIMS:
                    code = "judge_input_too_large"
                elif call is None or call["status"] in ("outcome_unknown",):
                    code = "outcome_unknown"
                elif call["status"] in ("skipped_budget", "skipped_after_stop"):
                    code = "budget_exhausted"
                elif call["status"] == "failed":
                    code = "provider_error"
                elif call["status"] != "ok":
                    code = "outcome_unknown"
                else:
                    obj = parse_judge_reply(call.get("text", ""))
                    verdict, why = check_verdict(obj, len(it["facts"]), it["claims"]) if obj is not None else (None, "invalid_judge_reply")
                    if verdict is None:
                        code = why
                    else:
                        results = derive(it, verdict, grader)
                if results is None:
                    results = [rules._error(qkey, m, grader, [code]) for m in JUDGED_METRICS]
                    counts[code] = counts.get(code, 0) + 1
            for r in results:
                _store_score(conn, evaluation_id, run_id, benchmark_id, it["qid"], it["sid"], r, judged, model)
        return {"evaluation_id": str(evaluation_id), "failures": counts}

    return build_calls, on_results
