"""A method developer's answers as an uploaded file: strict validation, checking the quoted passages against the system parse, and
scoring (Issue #189; SPEC 9.2 / 16.5 participant flow).

The owner gives the frozen QUESTIONS (no gold) to a developer. The developer returns JSONL, one line per question:
``{"question_id": "...", "status": "answered"|"abstained", "answer": "...", "sources": [{"page": 3, "text": "quoted passage"}],
"reason": "(abstained only)"}`` with an optional first line ``{"method_name": "...", "method_version": "...", "notes": "..."}``.
``page`` is 1-based in the ORIGINAL PDF.

Honesty rules implemented here:
* a malformed line is reported by line number with a FIXED code (never the content), and nothing is imported unless the owner
  accepts the partial import; a question the file does not answer is recorded as ``not_submitted``, never guessed;
* every source becomes a hit. When the quoted text is found on the cited page of the system parse (NFKC, case-folded, whitespace
  ignored) its region is the located unit(s); otherwise the region is the whole page and the match is recorded as NOT found
  (``text_found`` false, with the share of the quote's words that do occur on the page): a fabricated quote stays visible;
* a re-upload is a NEW run; the file is stored as an artifact with its sha256;
* the rule-based scorer is the one the built-in methods use; the judge (separate job) sees the answer and the submitted passages."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata
import uuid
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

from lab_contracts.method import QueryResponse
from lab_contracts.source import RegionKind, SourceRegion
from lab_db.repo import Change, NotFound, PermissionDenied, authorize, record_change
from lab_db.storage import LocalArtifactStore, store_and_register
from lab_api import baseline_eval as be
from lab_api.method_answer import failure_response
from lab_api.validate_questions import content_tokens
from .app_log import app_traced, log_op, log_alarm, set_question

ACTION = "register_method_and_submit"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ANSWER_CHARS = 4000
MAX_SOURCES = 10
MAX_SOURCE_CHARS = 2000
MIN_SOURCE_CHARS = 6                # fewer non-space characters would match almost anywhere
MAX_REASON_CHARS = 2000
MAX_SYSTEM_TEXT = 6000               # the matched system text a judge may see per source (as the answering prompt caps a hit)
MAX_QUESTIONS = 250                 # scored inside one request, like the baseline
MAX_PROBLEMS_LISTED = 200
BUDGET_PROFILE = "offline-file"
SUBMISSION_FORMAT = "lab-submission-1"
Q_KEYS = {"question_id", "status", "answer", "sources", "reason"}
HEADER_KEYS = {"method_name", "method_version", "notes"}
_WS = re.compile(r"\s+")


class SubmissionError(Exception):
    """The upload cannot proceed (fixed, value-free message)."""


def norm_strip(text: str) -> str:
    """NFKC + case-fold with ALL whitespace removed: PDF text extraction and a developer's copy differ mostly in spaces and line breaks."""
    return _WS.sub("", unicodedata.normalize("NFKC", text).casefold())


@dataclass
class Source:
    page: int
    text: str


@dataclass
class Entry:
    line: int
    status: str
    answer: str = ""
    sources: list = field(default_factory=list)
    reason: Optional[str] = None


@dataclass
class Parsed:
    header: Optional[dict]
    entries: dict                    # question key -> Entry (the first valid line of each question)
    problems: list                   # [{"line": n, "code": "..."}], fixed codes only
    lines: int
    ignored: int                     # valid-looking lines that were not imported (duplicates)


def _problem(problems: list, line: int, code: str) -> None:
    log_alarm("validate_line", code, stage="external_submission", line=line)
    if len(problems) < MAX_PROBLEMS_LISTED:
        problems.append({"line": line, "code": code})


@app_traced(stage="external_submission")
def parse_file(text: str, question_keys: set, page_count: int) -> Parsed:
    """Validate the whole file. Never raises on content: every problem is a (line, code) pair."""
    problems: list = []
    entries: dict = {}
    header: Optional[dict] = None
    seen_any = False
    ignored = 0
    n_lines = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        n_lines += 1
        try:
            obj = json.loads(raw)
        except Exception:
            _problem(problems, number, "invalid_json")
            continue
        if not isinstance(obj, dict):
            _problem(problems, number, "not_an_object")
            continue
        if "question_id" not in obj and set(obj) <= HEADER_KEYS and obj:
            if seen_any or header is not None:
                _problem(problems, number, "header_not_first" if seen_any else "duplicate_header")
            elif not all(isinstance(v, str) for v in obj.values()):
                _problem(problems, number, "header_invalid")
            else:
                header = {k: v.strip() for k, v in obj.items()}
            continue
        seen_any = True
        if set(obj) - Q_KEYS:
            _problem(problems, number, "unknown_keys")
            continue
        qid = obj.get("question_id")
        if not isinstance(qid, str) or not qid.strip():
            _problem(problems, number, "bad_question_id")
            continue
        qid = qid.strip()
        if qid not in question_keys:
            _problem(problems, number, "unknown_question")
            continue
        if qid in entries:
            _problem(problems, number, "duplicate_question")
            ignored += 1
            continue
        code = _check_entry(obj, page_count)
        if isinstance(code, str):
            _problem(problems, number, code)
            continue
        code.line = number
        entries[qid] = code
        log_op("validate_line", "valid", stage="external_submission", line=number, question_key=qid)
    return Parsed(header, entries, problems, n_lines, ignored)


@app_traced(stage="external_submission")
def _check_entry(obj: dict, page_count: int):
    status = obj.get("status")
    if status not in ("answered", "abstained"):
        return "bad_status"
    answer, sources, reason = obj.get("answer"), obj.get("sources"), obj.get("reason")
    if status == "abstained":
        if (answer not in (None, "")) or (sources not in (None, [])):
            return "abstained_with_answer_or_sources"
        if not isinstance(reason, str) or not reason.strip():
            return "reason_missing"
        if len(reason) > MAX_REASON_CHARS:
            return "reason_too_long"
        return Entry(0, "abstained", reason=reason.strip())
    if reason not in (None, ""):
        return "reason_on_answered"
    if not isinstance(answer, str) or not answer.strip():
        return "bad_answer"
    if len(answer) > MAX_ANSWER_CHARS:
        return "answer_too_long"
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        return "bad_source"
    if len(sources) > MAX_SOURCES:
        return "too_many_sources"
    out = []
    for s in sources:
        if not isinstance(s, dict) or set(s) != {"page", "text"}:
            return "bad_source"
        page, text = s["page"], s["text"]
        if isinstance(page, bool) or not isinstance(page, int):
            return "bad_page"
        if not 1 <= page <= page_count:
            return "page_out_of_range"
        if not isinstance(text, str):
            return "bad_source_text"
        if len(text) > MAX_SOURCE_CHARS:
            return "source_text_too_long"
        if len(norm_strip(text)) < MIN_SOURCE_CHARS:
            return "source_text_too_short"
        out.append(Source(page, text.strip()))
    return Entry(0, "answered", answer=answer.strip(), sources=out)


# --- locating the quotes on the system parse ----------------------------------------------------------------------------------

@dataclass
class Unit:
    key: str
    text: str                        # normalised, whitespace removed
    raw: str
    regions: list                    # [(page_index, (x0, y0, x1, y1), kind)]


def load_units(conn: psycopg.Connection, document_version_id: uuid.UUID) -> dict:
    """{page_index: [Unit in reading order]} of the evidence units of the document (the pages the parse promoted)."""
    units: dict = {}
    for uid, key, content, page, x0, y0, x1, y1, kind in conn.execute(
            "SELECT eu.id, eu.evidence_key, eu.normalized_content, er.page_index, er.x0, er.y0, er.x1, er.y1, er.kind FROM evidence_units eu "
            "JOIN evidence_regions er ON er.evidence_unit_id = eu.id WHERE eu.document_version_id = %s ORDER BY eu.id, er.ordinal",
            (document_version_id,)).fetchall():
        u = units.setdefault(uid, Unit(key, norm_strip(content), content, []))
        u.regions.append((int(page), (x0, y0, x1, y1), kind))
    by_page: dict = {}
    for u in units.values():
        for page in sorted({r[0] for r in u.regions}):
            by_page.setdefault(page, []).append(u)
    for page, us in by_page.items():           # reading order: top to bottom, then left to right (first region on that page)
        us.sort(key=lambda u: min((r[1][1], r[1][0]) for r in u.regions if r[0] == page))
    return by_page


@dataclass
class Located:
    quality: str                     # exact_in_unit | exact_in_page | not_found | page_without_text
    regions: list                    # [(page_index, bbox, kind)]
    coverage: float                  # share of the quote's content words found on the page
    system_text: Optional[str] = None   # the text of the matched unit(s) in the SYSTEM parse (None when not found): the only text a judge may see

    @property
    def found(self) -> bool:
        return self.quality in ("exact_in_unit", "exact_in_page")


WHOLE_PAGE = ((0.0, 0.0, 1.0, 1.0), "page")


@app_traced(stage="external_submission")
def locate(quote: str, page_index: int, by_page: dict) -> Located:
    loc = _locate_inner(quote, page_index, by_page)
    log_op("quote_located", match_quality=loc.quality, coverage=loc.coverage, page_index=page_index, quote=quote,
           stage="external_submission")
    if not loc.found:
        log_alarm("locate", "quote_not_found", stage="external_submission", page_index=page_index, quote=quote)
    return loc


def _locate_inner(quote: str, page_index: int, by_page: dict) -> Located:
    page_units = by_page.get(page_index, [])
    whole = [(page_index, WHOLE_PAGE[0], WHOLE_PAGE[1])]
    if not page_units:
        return Located("page_without_text", whole, 0.0)
    q = norm_strip(quote)
    for u in page_units:
        if q in u.text:
            return Located("exact_in_unit", [r for r in u.regions if r[0] == page_index][:64], 1.0, u.raw[:MAX_SYSTEM_TEXT])
    joined, spans, pos = "", [], 0
    for u in page_units:
        spans.append((pos, pos + len(u.text), u))
        joined += u.text
        pos += len(u.text)
    at = joined.find(q)
    if at >= 0:
        end = at + len(q)
        over = [u for s, e, u in spans if s < end and e > at]
        hit = [r for u in over for r in u.regions if r[0] == page_index]
        return Located("exact_in_page", hit[:64], 1.0, "\n".join(u.raw for u in over)[:MAX_SYSTEM_TEXT])
    page_words = set(content_tokens(" ".join(u.raw for u in page_units)))
    words = content_tokens(quote)
    coverage = (sum(1 for w in words if w in page_words) / len(words)) if words else 0.0
    return Located("not_found", whole, round(coverage, 3))


# --- the response the scorer and the judge read ----------------------------------------------------------------------------------

def build_response(qkey: str, entry: Entry, doc_id: uuid.UUID, doc_sha: str, by_page: dict) -> tuple[QueryResponse, list]:
    """(contract response, [(hit_key, cited_page, Located)]). Hits are the sources in the order given (s1, s2, ...)."""
    hits, matches, citations = [], [], []
    for n, src in enumerate(entry.sources, start=1):
        loc = locate(src.text, src.page - 1, by_page)
        regions = [SourceRegion(document_id=str(doc_id), document_sha256=doc_sha, page_index=pg, bbox=bbox, kind=RegionKind(kind), quote=None)
                   for pg, bbox, kind in loc.regions]
        hits.append({"hit_id": f"s{n}", "rank": n, "content": src.text, "regions": [r.model_dump(mode="json") for r in regions]})
        matches.append((f"s{n}", src.page, loc))
        citations.append({"claim_id": "c1", "hit_id": f"s{n}", "region_indices": list(range(len(regions)))})
    base = {"protocol_version": "1.0", "request_id": f"offline-{qkey}", "question_id": qkey, "index_id": "offline-file",
            "retrieved_hits": hits, "telemetry": None}
    if entry.status == "abstained":
        base.update({"status": "abstained", "answer": {"text": "", "claims": []}, "reason": entry.reason, "citations": []})
    else:
        base.update({"status": "answered", "answer": {"text": entry.answer, "claims": [{"claim_id": "c1", "text": entry.answer}]},
                     "citations": citations})
    return QueryResponse.model_validate_json(json.dumps(base)), matches


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKC", name).casefold()).strip("-")
    return (s or "method")[:64]


@app_traced(stage="external_submission", is_stage=True)
def import_submission(conn: psycopg.Connection, store: LocalArtifactStore, project_id: uuid.UUID, benchmark_id: uuid.UUID,
                      actor_id: uuid.UUID, *, file_bytes: bytes, method_name: str, method_version: Optional[str] = None,
                      notes: Optional[str] = None, accept_partial: bool = False) -> dict:
    """Validate, and when allowed import and score, one uploaded file. Authorized for ``register_method_and_submit`` (owner/maintainer).
    Raises ``SubmissionError`` (nothing written) when the file has problems and ``accept_partial`` is false: the report is the exception's
    ``report`` attribute."""
    decision = authorize(conn, project_id, actor_id, ACTION)
    if decision.condition is not None:
        raise PermissionDenied(f"{decision.role} may not upload a method's answers")
    if len(file_bytes) > MAX_FILE_BYTES:
        raise SubmissionError("file_too_large")
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubmissionError("file_is_not_utf8") from None
    method_name = method_name.strip()
    if not method_name or len(method_name) > 128:
        raise SubmissionError("method_name_must_be_1_to_128_characters")
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    with conn.transaction():
        conn.execute("SELECT set_config('lab.actor_id', %s, true)", (str(actor_id),))      # gold RLS (owner/maintainer)
        bench = conn.execute("SELECT status::text, document_version_id FROM benchmark_revisions WHERE project_id = %s AND id = %s FOR SHARE",
                             (project_id, benchmark_id)).fetchone()
        if bench is None:
            raise NotFound("benchmark")
        if bench[0] != "frozen":
            raise SubmissionError("benchmark_is_not_frozen")
        doc_id = bench[1]
        row = conn.execute("SELECT dv.sha256, dv.page_count FROM document_versions dv WHERE dv.project_id = %s AND dv.id = %s",
                           (project_id, doc_id)).fetchone()
        if row is None:
            raise NotFound("document")
        doc_sha, page_count = row
        questions = conn.execute("SELECT id, question_key, ordinal FROM active_questions WHERE benchmark_id = %s ORDER BY ordinal",
                                 (benchmark_id,)).fetchall()
        if len(questions) > MAX_QUESTIONS:
            raise SubmissionError(f"benchmark_has_more_than_{MAX_QUESTIONS}_questions_for_one_upload")
        parsed = parse_file(text, {q[1] for q in questions}, page_count)
        missing = [q for q in questions if q[1] not in parsed.entries]
        report = {"format": SUBMISSION_FORMAT, "file_sha256": file_sha, "lines": parsed.lines, "questions_in_benchmark": len(questions),
                  "answered": sum(1 for e in parsed.entries.values() if e.status == "answered"),
                  "abstained": sum(1 for e in parsed.entries.values() if e.status == "abstained"),
                  "not_submitted": len(missing), "problems": parsed.problems, "problem_count": len(parsed.problems),
                  "lines_not_imported": len(parsed.problems), "header": parsed.header}
        if parsed.problems and not accept_partial:
            err = SubmissionError("the_file_has_problems_nothing_was_imported")
            err.report = report
            raise err
        if not parsed.entries:
            err = SubmissionError("no_valid_line_to_import")
            err.report = report
            raise err
        by_page = load_units(conn, doc_id)
        logical = f"external:{slug(method_name)}"
        config_hash = hashlib.sha256(json.dumps({"method": method_name, "version": method_version, "file": file_sha}, sort_keys=True).encode()).hexdigest()
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"method_revision:{project_id}:{logical}",))
        revision = 1 + conn.execute("SELECT count(*) FROM resource_versions WHERE project_id = %s AND resource_type = 'method_revision' "
                                    "AND logical_id = %s", (project_id, logical)).fetchone()[0]
        resource_id = record_change(conn, Change(project_id, actor_id, "method.register", "method_revision", logical, revision, config_hash,
                                                 "lab-c14n-1", "method.registered"))
        method_id, run_id, evaluation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        conn.execute("INSERT INTO method_revisions (id, project_id, resource_id, owner_actor_id, protocol_version, config_hash, execution_mode, "
                     "visibility) VALUES (%s, %s, %s, %s, '1.0', %s, 'offline_file', 'declared_only')",
                     (method_id, project_id, resource_id, actor_id, config_hash))
        conn.execute("INSERT INTO runs (id, project_id, benchmark_id, method_revision_id, budget_profile) VALUES (%s, %s, %s, %s, %s)",
                     (run_id, project_id, benchmark_id, method_id, BUDGET_PROFILE))
        be._transition(conn, run_id, "preparing", "offline file: checking the uploaded answers")
        be._transition(conn, run_id, "running", "offline file: importing the uploaded answers")
        file_artifact = store_and_register(conn, store, project_id, file_bytes, "application/x-ndjson")
        conn.execute("INSERT INTO evaluations (id, run_id, benchmark_id, grader_revision, status) VALUES (%s, %s, %s, %s, 'provisional')",
                     (evaluation_id, run_id, benchmark_id, be.GRADER_REVISION))
        checks = {"total": 0, "found": 0, "not_found": 0, "page_without_text": 0}
        failures: dict = {}
        for qid, qkey, ordinal in questions:
            entry = parsed.entries.get(qkey)
            set_question(qkey, ordinal)
            code, matches = None, []
            if entry is None:
                code = "not_submitted"
                response = failure_response(qkey, f"offline-{qkey}", "offline-file", [], code, {"latency_ms": None, "retrieval_calls": None,
                                            "model_calls": None, "input_tokens": None, "output_tokens": None})
                failures[code] = failures.get(code, 0) + 1
            else:
                response, matches = build_response(qkey, entry, doc_id, doc_sha, by_page)
            payload = response.model_dump_json().encode("utf-8")
            attempt_id, submission_id = uuid.uuid4(), uuid.uuid4()
            conn.execute("INSERT INTO attempts (id, run_id, benchmark_id, question_id, attempt_number, transport) VALUES (%s, %s, %s, %s, 1, 'offline_file')",
                         (attempt_id, run_id, benchmark_id, qid))
            artifact_id = store_and_register(conn, store, project_id, payload, "application/json")
            conn.execute("INSERT INTO submissions (id, run_id, attempt_id, question_id, status, answer_status, payload_hash, c14n_version, "
                         "payload_artifact_id, failure_reason) VALUES (%s, %s, %s, %s, 'accepted', %s, %s, 'lab-c14n-1', %s, %s)",
                         (submission_id, run_id, attempt_id, qid, response.status, hashlib.sha256(payload).hexdigest(), artifact_id, code))
            for hit in response.retrieved_hits:
                conn.execute("INSERT INTO retrieved_hits (submission_id, hit_key, rank, content) VALUES (%s, %s, %s, %s)",
                             (submission_id, hit.hit_id, hit.rank, hit.content))
            for cit in response.citations:
                conn.execute("INSERT INTO citations (submission_id, claim_key, hit_key, region_indices) VALUES (%s, %s, %s, %s)",
                             (submission_id, cit.claim_id, cit.hit_id, list(cit.region_indices)))
            for hit_key, page, loc in matches:
                conn.execute("INSERT INTO submission_source_matches (submission_id, hit_key, cited_page, match_quality, text_found, quote_coverage, "
                             "matched_text) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                             (submission_id, hit_key, page, loc.quality, loc.found, loc.coverage, loc.system_text))
                checks["total"] += 1
                checks["found" if loc.found else loc.quality] += 1
            for result in be._score_question(conn, benchmark_id, doc_id, qid, qkey, response, failure_code=code,
                                          unlocated=frozenset(hk for hk, _, loc in matches if not loc.found)):
                be._store_score(conn, evaluation_id, run_id, benchmark_id, qid, submission_id, result)
        set_question(None)
        report["source_checks"] = checks
        conn.execute("INSERT INTO external_submissions (run_id, project_id, benchmark_id, method_name, method_version, notes, file_sha256, "
                     "file_artifact_id, uploaded_by, accepted_partial, report) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (run_id, project_id, benchmark_id, method_name, method_version, notes, file_sha, file_artifact, actor_id,
                      bool(parsed.problems), Jsonb(report)))
        be._transition(conn, run_id, "evaluating", "offline file: scoring with the rule-based scorer")
        be._transition(conn, run_id, "completed_with_errors" if failures else "completed",
                       "offline file scored; semantic metrics are pending until the judge runs"
                       + (f"; failures: {json.dumps(failures, sort_keys=True)}" if failures else ""), finalize=True)
    return {"run_id": str(run_id), "evaluation_id": str(evaluation_id), "method_name": method_name, "method_revision": revision,
            "status": "completed_with_errors" if failures else "completed", "evaluation_status": "provisional", "report": report,
            "results_url": f"/api/v1/runs/{run_id}/results",
            "limits": ["The quoted passages are checked against the system's own parse of the PDF: a quote that is not found on the cited page is "
                       "reported and its region is the whole page.",
                       "Answer quality (correctness, citation precision/completeness, faithfulness) stays pending until the judge runs; the judge is "
                       "the same provider/model family that generated the questions and is uncalibrated, so its scores are provisional."]}
