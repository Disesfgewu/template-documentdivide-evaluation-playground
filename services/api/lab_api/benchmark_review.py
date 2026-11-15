"""Review the generated questions and EXCLUDE some before the freeze (PM-DECISION on Issue #7, Refs #10/#12).

* ``GET  .../revisions/{revision}/questions/review``: owner/maintainer only (it shows validation verdicts, which are gold-protected).
  Every question, including the excluded ones, with its latest automated verdict and reason and its exclusion state.
* ``POST .../revisions/{revision}/questions/{question_id}/exclusion`` with ``{"excluded": true|false, "reason": "..."}``: owner only,
  only while the benchmark is ``validating``. A RECORDED decision, never a delete: question_exclusions is append-only (migration
  0019), the latest row per question wins, and an excluded question is not frozen, not answered and not scored. Restoring is
  another row. The freeze then needs every NON-excluded question to pass and at least one to remain."""
from __future__ import annotations

import json
import uuid

import psycopg
import psycopg.errors
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from typing import Annotated

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, resolve_project_id
from .benchmark_build import MAX_BODY_BYTES, _find
from lab_db.repo import NotFound, PermissionDenied, authorize

router = APIRouter()
BUILD_ACTION = "build_edit_freeze"


class ExclusionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    excluded: bool
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def _authorize(conn: psycopg.Connection, project_id: uuid.UUID, actor_id: uuid.UUID, owner_only: bool) -> str:
    try:
        decision = authorize(conn, project_id, actor_id, BUILD_ACTION)
    except NotFound:
        raise ApiError(404, "not_found", "benchmark revision not found") from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if owner_only and decision.role != "owner":
        raise ApiError(403, "permission_denied", f"{decision.role} may not exclude a question: only the project owner may")
    return decision.role


EXPORT_SCHEMA = "lab-benchmark-export-1"
MAX_EXPORT_QUESTIONS = 1000


def _details(conn: psycopg.Connection, benchmark_id: uuid.UUID, document_version_id: uuid.UUID) -> list[dict]:
    """Every question of the benchmark (excluded ones too) with EVERYTHING the owner needs to check it: the reference answer, the required facts,
    the evidence (pages and text), the distractors, the latest automated verdict with its checks, and the exclusion state. Gold is included, so the
    caller must be owner/maintainer and have set ``lab.actor_id`` (the gold tables are under row level security). No key and no provider payload:
    the stored validation checks are flags and numbers, the model's text is never kept."""
    rows = conn.execute(
        "SELECT q.id, q.question_key, q.ordinal, q.quota_bucket, q.question_text, q.payload, v.verdict, v.reason, v.code_checks, v.llm_checks, "
        "COALESCE(e.kind = 'exclude', false), e.reason FROM questions q "
        "LEFT JOIN LATERAL (SELECT verdict, reason, code_checks, llm_checks FROM question_validations x WHERE x.question_id = q.id "
        "ORDER BY x.created_at DESC, x.id DESC LIMIT 1) v ON true "
        "LEFT JOIN LATERAL (SELECT kind, reason FROM question_exclusions x WHERE x.question_id = q.id ORDER BY x.seq DESC LIMIT 1) e ON true "
        "WHERE q.benchmark_id = %s ORDER BY q.ordinal LIMIT %s", (benchmark_id, MAX_EXPORT_QUESTIONS + 1)).fetchall()
    if len(rows) > MAX_EXPORT_QUESTIONS:
        raise ApiError(409, "conflict", f"the benchmark has more than {MAX_EXPORT_QUESTIONS} questions: too many to preview or export in one request")
    gold: dict[uuid.UUID, dict] = {}
    for qid, kind, payload in conn.execute("SELECT question_id, kind, payload FROM gold_answers WHERE benchmark_id = %s", (benchmark_id,)):
        gold.setdefault(qid, {})[kind] = payload
    wanted: set[str] = set()
    for g in gold.values():
        src = g.get("source") or {}
        wanted.update(eid for st in src.get("evidence_sets", []) for eid in st.get("evidence_ids", []))
        wanted.update(d["evidence_id"] for d in src.get("distractors", []))
    units: dict[str, dict] = {}
    if wanted:
        for key, text, pages in conn.execute(
                "SELECT eu.evidence_key, eu.normalized_content, array_agg(DISTINCT er.page_index ORDER BY er.page_index) FROM evidence_units eu "
                "JOIN evidence_regions er ON er.evidence_unit_id = eu.id WHERE eu.document_version_id = %s AND eu.evidence_key = ANY(%s) "
                "GROUP BY eu.evidence_key, eu.normalized_content", (document_version_id, sorted(wanted))):
            units[key] = {"evidence_id": key, "pages": [int(p) + 1 for p in pages], "text": text}      # pages are shown 1-based, as in a PDF viewer
    out = []
    for qid, key, ordinal, bucket, text, payload, verdict, vreason, code_checks, llm_checks, excluded, ereason in rows:
        g, sem, src = gold.get(qid, {}), (gold.get(qid, {}).get("semantic") or {}), (gold.get(qid, {}).get("source") or {})
        out.append({
            "id": str(qid), "question_key": key, "ordinal": int(ordinal), "bucket": bucket, "quota_bucket": bucket, "question_text": text,
            "language": (payload or {}).get("language"),
            "reference_answer": sem.get("reference_answer"),
            "required_facts": [{"fact_id": f.get("fact_id"), "statement": f.get("statement"), "weight": f.get("weight")} for f in sem.get("required_facts", [])],
            "conditions": sem.get("conditions", []),
            "evidence_sets": [{"set_id": st.get("set_id"), "units": [units.get(eid, {"evidence_id": eid, "pages": [], "text": None}) for eid in st.get("evidence_ids", [])]}
                              for st in src.get("evidence_sets", [])],
            "distractors": [{**units.get(d["evidence_id"], {"evidence_id": d["evidence_id"], "pages": [], "text": None}),
                             "confusion_reason": d.get("confusion_reason"), "disambiguating_condition": d.get("disambiguating_condition")}
                            for d in src.get("distractors", [])],
            "validation": {"verdict": verdict or "unvalidated", "reason": vreason, "code_checks": code_checks, "llm_checks": llm_checks},
            "verdict": verdict or "unvalidated", "verdict_reason": vreason,
            "excluded": bool(excluded), "exclusion_reason": ereason if excluded else None})
    return out


def _review(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> dict:
    _authorize(conn, project_id, actor_id, owner_only=False)
    benchmark_id, document_version_id, status, _ = _find(conn, project_id, logical_id, revision)
    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))     # question_validations is gold-RLS
        items = _details(conn, benchmark_id, document_version_id)
    active = [i for i in items if not i["excluded"]]
    counts = {k: sum(1 for i in active if i["verdict"] == k) for k in ("pass", "fail", "needs_review", "unvalidated")}
    return {"benchmark_revision_id": str(benchmark_id), "status": status, "items": items,
            "total": len(items), "active": len(active), "excluded": len(items) - len(active),
            "validation_of_active": counts, "can_freeze": status == "validating" and bool(active) and counts["pass"] == len(active),
            "note": "verdicts are automated and come from the same provider and model as the generator; an exclusion is recorded with "
                    "its reason and never deletes the question",
            "validation_explained": "the automated validation checks (1) by code that the answer, the facts and the numbers really appear in the text the "
                                    "model was shown, and (2) with a second model call whether the question can be answered from that text alone"}


def _export(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> tuple[dict, list[dict]]:
    """(header, questions) of the whole benchmark: gold included, so owner/maintainer only."""
    _authorize(conn, project_id, actor_id, owner_only=False)
    benchmark_id, document_version_id, status, page_count = _find(conn, project_id, logical_id, revision)
    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
        items = _details(conn, benchmark_id, document_version_id)
        meta = conn.execute(
            "SELECT b.n_target, b.q_min, b.compliance, dv.sha256 FROM benchmark_revisions b JOIN document_versions dv ON dv.id = b.document_version_id "
            "WHERE b.id = %s", (benchmark_id,)).fetchone()
    header = {"schema": EXPORT_SCHEMA, "logical_id": logical_id, "revision": revision, "benchmark_revision_id": str(benchmark_id), "status": status,
              "compliance": meta[2], "n_target": meta[0], "q_min": meta[1], "document_sha256": meta[3], "document_page_count": page_count,
              "questions": len(items), "excluded": sum(1 for i in items if i["excluded"]),
              "contains_gold": True, "human_review": "skipped",
              "notes": ["This file contains the gold (reference answers, facts, evidence): do not give it to participants.",
                        "Validation verdicts are automated (code checks plus a model call by the same provider and model as the generator); they are not independent.",
                        "An excluded question is recorded with its reason and is not frozen, answered or scored."]}
    return header, items


def _exclude(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
             question_id: uuid.UUID, body: ExclusionRequest) -> dict:
    _authorize(conn, project_id, actor_id, owner_only=True)
    event_id, op_id = uuid.uuid4(), uuid.uuid4()
    try:
        with conn.transaction():
            benchmark_id, _, status, _ = _find(conn, project_id, logical_id, revision, lock=True)
            if status != "validating":
                raise ApiError(409, "conflict", f"questions can only be excluded or restored while the benchmark is 'validating' (it is {status})")
            if conn.execute("SELECT 1 FROM questions WHERE benchmark_id = %s AND id = %s", (benchmark_id, question_id)).fetchone() is None:
                raise ApiError(404, "not_found", "question not found")
            last = conn.execute("SELECT kind FROM question_exclusions WHERE question_id = %s ORDER BY seq DESC LIMIT 1", (question_id,)).fetchone()
            now_excluded = last is not None and last[0] == "exclude"
            if now_excluded == body.excluded:
                raise ApiError(409, "conflict", "the question is already " + ("excluded" if now_excluded else "included"))
            kind = "exclude" if body.excluded else "restore"
            conn.execute("INSERT INTO question_exclusions (id, benchmark_id, question_id, kind, reason, actor_id) VALUES (%s, %s, %s, %s, %s, %s)",
                         (event_id, benchmark_id, question_id, kind, body.reason, actor_id))
            conn.execute("INSERT INTO operations (id, project_id, kind, actor_id, status, finished_at) VALUES (%s, %s, 'question.exclusion', %s, 'succeeded', now())",
                         (op_id, project_id, actor_id))
            conn.execute("INSERT INTO audit_events (event_id, project_id, actor_id, action, resource_type, resource_id, reason, outcome, operation_id) "
                         "VALUES (%s, %s, %s, %s, 'question', %s, %s, 'succeeded', %s)",
                         (uuid.uuid4(), project_id, actor_id, f"question.{kind}d", question_id, body.reason[:500], op_id))
            active = conn.execute("SELECT count(*) FROM active_questions WHERE benchmark_id = %s", (benchmark_id,)).fetchone()[0]
    except psycopg.errors.InsufficientPrivilege:
        raise ApiError(403, "permission_denied", "only the project owner may exclude a question") from None
    except psycopg.errors.IntegrityConstraintViolation:
        raise ApiError(409, "conflict", "the benchmark is no longer validating") from None
    return {"question_id": str(question_id), "excluded": body.excluded, "reason": body.reason, "active_questions": int(active),
            "note": "recorded, not deleted; an excluded question is not frozen, answered or scored"}


async def _body(request: Request) -> ExclusionRequest:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        return ExclusionRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "bad_request", 'the body must be {"excluded": true|false, "reason": "<1..500 characters>"}') from None


async def review_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                         actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(status_code=200, content=await _run_blocking(request, "meta", _review, conn, actor_id, project_id, logical_id, revision))


async def exclusion_handler(logical_id: str, revision: int, question_id: uuid.UUID, request: Request,
                            conn: psycopg.Connection = Depends(get_db),
                            actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    body = await _body(request)
    return JSONResponse(status_code=200, content=await _run_blocking(
        request, "meta", _exclude, conn, actor_id, project_id, logical_id, revision, question_id, body))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions/review", review_handler, methods=["GET"],
                     name="review_questions_v1")
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions/{question_id}/exclusion", exclusion_handler,
                     methods=["POST"], name="exclude_question_v1")


async def export_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                         actor_id: uuid.UUID = Depends(get_authenticated_actor)):
    from fastapi.responses import Response
    project_id = resolve_project_id(request)
    fmt = request.query_params.get("format", "json")
    if fmt not in ("json", "jsonl"):
        raise ApiError(400, "bad_request", "format must be json or jsonl")
    header, items = await _run_blocking(request, "meta", _export, conn, actor_id, project_id, logical_id, revision)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in logical_id)[:80] or "benchmark"
    if fmt == "json":
        body = json.dumps({**header, "items": items}, ensure_ascii=False, indent=2)
        media, ext = "application/json", "json"
    else:        # one JSON object per line: the header first, then one question per line
        lines = [json.dumps({"type": "benchmark", **header}, ensure_ascii=False)] + [json.dumps({"type": "question", **i}, ensure_ascii=False) for i in items]
        body = "\n".join(lines) + "\n"
        media, ext = "application/x-ndjson", "jsonl"
    return Response(content=body.encode("utf-8"), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{safe}-r{revision}.{ext}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/export", export_handler, methods=["GET"], name="export_benchmark_v1")
