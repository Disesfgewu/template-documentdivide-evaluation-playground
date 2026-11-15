"""Hand the frozen questions to a method developer and score what they send back (Issue #189).

* ``GET .../revisions/{n}/questions/export?format=jsonl|json``: the PARTICIPANT package of a FROZEN benchmark: benchmark and document
  identity, and per active question only ``{question_id, ordinal, question_text, language}``. No gold of any kind (no answers, facts,
  evidence, buckets, verdicts, distractors): the query reads nothing but ``active_questions`` and the benchmark/document rows.
* ``GET /api/v1/submission-format``: the file format a developer returns (description, example lines, JSON schema).
* ``POST .../revisions/{n}/submissions``: upload the developer's JSONL (see ``external_submission``); owner/maintainer, frozen only.
* ``GET  .../revisions/{n}/submissions``: the uploads of this benchmark with their scores' state.
* ``GET /api/v1/runs/{run_id}/submission-details``: per question their answer, their sources (with where the quote was found), the scores
  and the gold side (owner/maintainer: it contains the gold)."""
from __future__ import annotations

import json
import uuid
from typing import Optional

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from .documents import ApiError, _run_blocking, get_authenticated_actor, get_db, get_store, resolve_project_id
from .benchmark_build import _find
from .benchmark_review import BUILD_ACTION, _authorize, _details
from . import external_submission as ext
from lab_contracts.method import QueryResponse
from lab_db.repo import NotFound, PermissionDenied, authorize
from lab_db.storage import LocalArtifactStore

router = APIRouter()
PACKAGE_SCHEMA = "lab-questions-package-1"
MAX_BODY = 24 * 1024 * 1024

FORMAT_EXAMPLE = [
    '{"method_name": "my-rag", "method_version": "0.3", "notes": "bm25 + reranker"}',
    '{"question_id": "q-8f3a1c", "status": "answered", "answer": "The annual fee is 120 EUR, due in May.", '
    '"sources": [{"page": 3, "text": "The annual fee of the Gold plan is 120 EUR and is due in May."}]}',
    '{"question_id": "q-b02d77", "status": "abstained", "reason": "the document does not say"}',
]


def format_doc() -> dict:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "title": "one line of the answers file (not the optional header line)",
        "type": "object", "additionalProperties": False, "required": ["question_id", "status"],
        "properties": {
            "question_id": {"type": "string", "description": "the question_id of the questions file"},
            "status": {"enum": ["answered", "abstained"]},
            "answer": {"type": "string", "minLength": 1, "maxLength": ext.MAX_ANSWER_CHARS, "description": "answered only"},
            "sources": {"type": "array", "maxItems": ext.MAX_SOURCES, "description": "answered only: the passages the answer relied on", "items": {
                "type": "object", "additionalProperties": False, "required": ["page", "text"], "properties": {
                    "page": {"type": "integer", "minimum": 1, "description": "1-based page of the ORIGINAL PDF"},
                    "text": {"type": "string", "maxLength": ext.MAX_SOURCE_CHARS, "description": f"the quoted passage, copied from that page (at least {ext.MIN_SOURCE_CHARS} non-space characters)"}}}},
            "reason": {"type": "string", "maxLength": ext.MAX_REASON_CHARS, "description": "abstained only"}}}
    header = {"type": "object", "additionalProperties": False, "properties": {k: {"type": "string"} for k in sorted(ext.HEADER_KEYS)},
              "description": "optional first line"}
    return {
        "format": ext.SUBMISSION_FORMAT,
        "description": [
            "One JSON object per line (JSONL, UTF-8). Optional first line: method_name, method_version, notes.",
            "One line per question of the questions file: question_id, status (answered or abstained), and for answered an answer and the sources it relied on.",
            "A source is {page, text}: page is 1-based in the original PDF, text is the passage copied from that page. The system looks for the text on that page of its own "
            "parse of the PDF and REPORTS every quote it cannot find (the cited region then counts as the whole page).",
            f"Limits: answer at most {ext.MAX_ANSWER_CHARS} characters, at most {ext.MAX_SOURCES} sources per question, a source text at most {ext.MAX_SOURCE_CHARS} characters.",
            "A question missing from the file is recorded as not_submitted (never guessed). An unknown question_id, a duplicate or a malformed line is listed by line number "
            "and nothing is imported unless the owner accepts importing only the valid lines.",
            "An abstained line has a reason and no answer or sources."],
        "example_lines": FORMAT_EXAMPLE, "line_schema": schema, "header_schema": header}


def _frozen_benchmark(conn: psycopg.Connection, project_id, logical_id, revision):
    benchmark_id, doc_version_id, status, page_count = _find(conn, project_id, logical_id, revision)
    return benchmark_id, doc_version_id, status, page_count


from .app_log import app_traced


@app_traced(stage="export", is_stage=True)
def _questions_package(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> tuple[dict, list]:
    _authorize(conn, project_id, actor_id, owner_only=False)
    benchmark_id, doc_version_id, status, page_count = _frozen_benchmark(conn, project_id, logical_id, revision)
    if status != "frozen":
        raise ApiError(409, "conflict", f"the questions package is only available once the benchmark is frozen (it is {status})")
    sha = conn.execute("SELECT sha256 FROM document_versions WHERE project_id = %s AND id = %s", (project_id, doc_version_id)).fetchone()[0]
    # ONLY these columns of active_questions: no payload (it names the quota bucket), no gold table, nothing that reveals how a question was built
    rows = conn.execute("SELECT question_key, ordinal, question_text, payload -> 'language' FROM active_questions WHERE benchmark_id = %s ORDER BY ordinal",
                        (benchmark_id,)).fetchall()
    questions = [{"question_id": k, "ordinal": int(o), "question_text": t, "language": lang} for k, o, t, lang in rows]
    header = {"schema": PACKAGE_SCHEMA, "logical_id": logical_id, "revision": revision, "benchmark_revision_id": str(benchmark_id),
              "document_id": str(doc_version_id), "document_sha256": sha, "document_page_count": page_count, "questions": len(questions),
              "contains_gold": False, "answers_file_format": ext.SUBMISSION_FORMAT, "answers_file_format_url": "/api/v1/submission-format",
              "instructions": ["Answer every question from the PDF with this sha256. Return one JSONL line per question (see answers_file_format_url): "
                               "the answer and the source passages (page + quoted text) it relied on.",
                               "page is 1-based in the original PDF."]}
    return header, questions


def _upload(conn: psycopg.Connection, store: LocalArtifactStore, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int,
            body: dict) -> dict:
    missing = ApiError(404, "not_found", "benchmark revision not found")
    benchmark_id, _, _, _ = _find(conn, project_id, logical_id, revision)
    try:
        out = ext.import_submission(
            conn, store, project_id, benchmark_id, actor_id, file_bytes=body["file_content"].encode("utf-8"), method_name=body["method_name"],
            method_version=body.get("method_version"), notes=body.get("notes"), accept_partial=bool(body.get("accept_partial", False)))
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    except ext.SubmissionError as exc:
        report = getattr(exc, "report", None)
        raise ApiError(422 if report else 409, "submission_rejected", f"the upload was not imported: {exc}",
                       details={"report": report} if report else None) from None
    return out


def _list(conn: psycopg.Connection, actor_id: uuid.UUID, project_id: uuid.UUID, logical_id: str, revision: int) -> dict:
    _authorize(conn, project_id, actor_id, owner_only=False)
    benchmark_id, _, status, _ = _find(conn, project_id, logical_id, revision)
    rows = conn.execute(
        "SELECT x.run_id, x.method_name, x.method_version, x.notes, x.file_sha256, x.created_at, x.accepted_partial, x.report, r.status::text, "
        "(SELECT e.id FROM evaluations e WHERE e.run_id = x.run_id ORDER BY e.created_at DESC, e.id DESC LIMIT 1), "
        "(SELECT e.grader_revision FROM evaluations e WHERE e.run_id = x.run_id ORDER BY e.created_at DESC, e.id DESC LIMIT 1) "
        "FROM external_submissions x JOIN runs r ON r.id = x.run_id WHERE x.project_id = %s AND x.benchmark_id = %s ORDER BY x.created_at DESC",
        (project_id, benchmark_id)).fetchall()
    items = [{"run_id": str(run), "method_name": name, "method_version": version, "notes": notes, "file_sha256": sha, "uploaded_at": at.isoformat(),
              "imported_only_valid_lines": bool(partial), "run_status": run_status, "evaluation_id": str(ev) if ev else None, "grader_revision": grader,
              "judged": bool(grader and "llm-judge" in grader),
              "counts": {k: report.get(k) for k in ("answered", "abstained", "not_submitted", "problem_count")}, "source_checks": report.get("source_checks")}
             for run, name, version, notes, sha, at, partial, report, run_status, ev, grader in rows]
    return {"benchmark_revision_id": str(benchmark_id), "benchmark_status": status, "items": items}


def _details_for_run(conn: psycopg.Connection, store: LocalArtifactStore, actor_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    run = conn.execute("SELECT r.benchmark_id, b.document_version_id FROM runs r JOIN benchmark_revisions b ON b.id = r.benchmark_id "
                       "WHERE r.id = %s AND r.project_id = %s", (run_id, project_id)).fetchone()
    missing = ApiError(404, "not_found", "run not found")
    try:
        decision = authorize(conn, project_id, actor_id, BUILD_ACTION)
    except NotFound:
        raise missing from None
    except PermissionDenied as exc:
        raise ApiError(403, "permission_denied", str(exc)) from None
    if decision.condition is not None:
        raise ApiError(403, "permission_denied", f"{decision.role} may not read the gold side of a run")
    if run is None:
        raise missing
    benchmark_id, doc_version_id = run
    up = conn.execute("SELECT method_name, method_version, notes, file_sha256, report FROM external_submissions WHERE run_id = %s", (run_id,)).fetchone()
    if up is None:
        raise ApiError(404, "not_found", "this run is not an uploaded developer file")
    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))
        gold = {d["id"]: d for d in _details(conn, benchmark_id, doc_version_id)}
        ev = conn.execute("SELECT id, grader_revision FROM evaluations WHERE run_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (run_id,)).fetchone()
        scores: dict = {}
        if ev:
            for qid, metric, st, raw, reasons in conn.execute("SELECT question_id, metric_name, status, raw_value, reason_codes FROM question_scores "
                                                              "WHERE evaluation_id = %s", (ev[0],)):
                scores.setdefault(str(qid), {})[metric] = {"status": st, "value": raw, "reason_codes": list(reasons)}
        subs = conn.execute(
            "SELECT s.id, s.question_id, s.answer_status, s.failure_reason, a.object_key, a.size_bytes, q.question_key, q.ordinal, q.question_text "
            "FROM submissions s JOIN active_questions q ON q.id = s.question_id JOIN artifacts a ON a.id = s.payload_artifact_id "
            "WHERE s.run_id = %s AND s.status = 'accepted' ORDER BY q.ordinal", (run_id,)).fetchall()
        matches: dict = {}
        for sid, key, page, quality, found, cov in conn.execute(
                "SELECT m.submission_id, m.hit_key, m.cited_page, m.match_quality, m.text_found, m.quote_coverage FROM submission_source_matches m "
                "JOIN submissions s ON s.id = m.submission_id WHERE s.run_id = %s", (run_id,)):
            matches.setdefault(sid, {})[key] = {"page": page, "match_quality": quality, "text_found": found, "quote_coverage": cov}
    items = []
    for sid, qid, status, failure, key, size, qkey, ordinal, qtext in subs:
        resp = QueryResponse.model_validate_json(store.open(key, expected_size=size, max_bytes=ext.MAX_FILE_BYTES))
        g = gold.get(str(qid), {})
        items.append({
            "question_id": qkey, "ordinal": int(ordinal), "question_text": qtext, "status": status, "failure_reason": failure,
            "their_answer": resp.answer.text or None, "their_reason": resp.reason,
            "their_sources": [{"page": matches.get(sid, {}).get(h.hit_id, {}).get("page"), "text": h.content, **{k: v for k, v in matches.get(sid, {}).get(h.hit_id, {}).items() if k != "page"},
                               "region_kind": h.regions[0].kind.value if h.regions else None} for h in resp.retrieved_hits],
            "scores": scores.get(str(qid), {}),
            "gold": {"reference_answer": g.get("reference_answer"), "required_facts": g.get("required_facts"), "evidence_sets": g.get("evidence_sets"),
                     "validation_verdict": g.get("verdict")}})
    return {"run_id": str(run_id), "method_name": up[0], "method_version": up[1], "notes": up[2], "file_sha256": up[3],
            "evaluation": {"id": str(ev[0]), "grader_revision": ev[1]} if ev else None, "upload_report": up[4], "items": items,
            "contains_gold": True}


async def _json_body(request: Request, limit: int) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "payload_too_large", "request body is too large")
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError(413, "payload_too_large", "request body is too large")
    try:
        body = json.loads(raw)
    except Exception:
        raise ApiError(400, "bad_request", "the body must be JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "the body must be a JSON object")
    return body


async def export_questions_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                                   actor_id: uuid.UUID = Depends(get_authenticated_actor)):
    project_id = resolve_project_id(request)
    fmt = request.query_params.get("format", "jsonl")
    if fmt not in ("json", "jsonl"):
        raise ApiError(400, "bad_request", "format must be json or jsonl")
    header, questions = await _run_blocking(request, "meta", _questions_package, conn, actor_id, project_id, logical_id, revision)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in logical_id)[:80] or "benchmark"
    if fmt == "json":
        body, media = json.dumps({**header, "items": questions}, ensure_ascii=False, indent=2), "application/json"
    else:
        body = "\n".join([json.dumps({"type": "benchmark", **header}, ensure_ascii=False)] +
                         [json.dumps({"type": "question", **q}, ensure_ascii=False) for q in questions]) + "\n"
        media = "application/x-ndjson"
    return Response(content=body.encode("utf-8"), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{safe}-r{revision}-questions.{fmt}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


async def format_handler(request: Request, actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    return JSONResponse(format_doc())


async def upload_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                         store: LocalArtifactStore = Depends(get_store), actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    body = await _json_body(request, MAX_BODY)
    if set(body) - {"method_name", "method_version", "notes", "file_content", "accept_partial"} or not isinstance(body.get("file_content"), str) \
            or not isinstance(body.get("method_name"), str) or not isinstance(body.get("accept_partial", False), bool) \
            or any(body.get(k) is not None and not isinstance(body.get(k), str) for k in ("method_version", "notes")):
        raise ApiError(400, "bad_request", "the body must be {method_name, file_content, [method_version], [notes], [accept_partial]}")
    return JSONResponse(status_code=201, content=await _run_blocking(request, "meta", _upload, conn, store, actor_id, project_id, logical_id, revision, body))


async def list_handler(logical_id: str, revision: int, request: Request, conn: psycopg.Connection = Depends(get_db),
                       actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(await _run_blocking(request, "meta", _list, conn, actor_id, project_id, logical_id, revision))


async def details_handler(run_id: uuid.UUID, request: Request, conn: psycopg.Connection = Depends(get_db),
                          store: LocalArtifactStore = Depends(get_store), actor_id: uuid.UUID = Depends(get_authenticated_actor)) -> JSONResponse:
    project_id = resolve_project_id(request)
    return JSONResponse(await _run_blocking(request, "meta", _details_for_run, conn, store, actor_id, project_id, run_id))


router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/questions/export", export_questions_handler, methods=["GET"],
                     name="export_questions_package_v1")
router.add_api_route("/api/v1/submission-format", format_handler, methods=["GET"], name="submission_format_v1")
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/submissions", upload_handler, methods=["POST"],
                     name="upload_submission_v1", status_code=201)
router.add_api_route("/api/v1/benchmarks/{logical_id}/revisions/{revision}/submissions", list_handler, methods=["GET"], name="list_submissions_v1")
router.add_api_route("/api/v1/runs/{run_id}/submission-details", details_handler, methods=["GET"], name="submission_details_v1")
