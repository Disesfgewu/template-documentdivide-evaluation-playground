"""Consumer for ``document.generate`` jobs (P-D slice 2, Refs #7). See docs/LAB_GEN.md.

``process_generate_job`` claims a queued job with a fenced lease, reads the pinned model/cap/budget, checks the
per-benchmark budget FAIL-CLOSED before anything is spawned, runs the ``lab-gen`` Go child (the only code that talks
to the model) as a bounded subprocess while a heartbeat keeps the lease and watches for cancellation, validates the
child's JSON strictly, and records every call in ``llm_calls`` together with the job's final status in ONE transaction.

Nothing here interprets document text: prompts come from the injected ``build_calls`` (slice 3 supplies the real one)
and the replies are handed, verbatim, to the optional ``on_results`` hook inside the same transaction.

Accounting rules:
* usage known -> the configured price decides the charge; usage unknown -> the worst case is HELD, never zero;
* the child dies, times out, prints garbage, or the lease is lost -> every call of the batch is recorded as
  ``outcome_unknown`` with its worst-case hold (some may have been sent), never as free;
* ``refused`` by lab-gen's own validation (no call was made) -> nothing is recorded and the job fails;
* the API key comes from the caller, goes only into the child's environment, and is in no argument, log or row.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Callable, Optional, Sequence

import psycopg

from lab_db import jobs
from lab_db.generation import record_llm_calls, request_sha256, reserve_batch, worst_case_micros
from lab_api.parse_worker import ParseOutcome, _Heartbeat, _stopped, _unreachable, is_infrastructure

LOG = logging.getLogger("lab.generate_worker")

__all__ = ["CallSpec", "GenModelConfig", "LabGenOutputInvalid", "parse_lab_gen_output", "process_generate_job", "run_lab_gen"]

KEY_ENV = "LAB_GEN_API_KEY"
MAX_STDOUT_BYTES = 8 * 1024 * 1024
_STATUSES = {"ok", "failed", "skipped_budget", "skipped_after_stop", "outcome_unknown"}
_CALL_KEYS = {"id", "status", "reason", "text", "usage_known", "input_tokens", "output_tokens", "charged_micros",
              "held_micros", "transport_attempts", "request_sha256", "text_sha256"}
_OUT_KEYS = {"status", "reason", "calls", "ledger_spent_micros", "ledger_reserved_micros", "ledger_unknown_micros"}


class LabGenOutputInvalid(Exception):
    """The child's output does not satisfy the contract (fixed, value-free message)."""


@dataclass(frozen=True)
class CallSpec:
    id: str
    system: str
    user: str


@dataclass(frozen=True)
class GenModelConfig:
    """Everything about the endpoint that is NOT in the job row. Prices are configured, never assumed."""
    protocol: str
    base_url: str
    allowed_host: str
    input_micros_per_mtok: int
    output_micros_per_mtok: int
    timeout_seconds: int = 60
    max_retries: int = 1
    requests_per_minute: int = 30


GenBuilder = Callable[[psycopg.Connection, uuid.UUID, uuid.UUID], Sequence[CallSpec]]
OnResults = Callable[[psycopg.Connection, uuid.UUID, uuid.UUID, list[dict]], None]


def _int(v, lo=0, hi=2**62) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise LabGenOutputInvalid("bad_number")
    return v


def parse_lab_gen_output(data: bytes, calls: Sequence[CallSpec], *, model: str, max_output_tokens: int,
                         budget_limit: int) -> dict:
    """Strictly validate lab-gen's stdout against what was asked. Returns ``{"status", "reason", "calls": [row...]}``
    where each row is ready for ``record_llm_calls`` (plus ``text`` for the hook). Raises ``LabGenOutputInvalid``."""
    try:
        out = json.loads(data)
    except Exception:
        raise LabGenOutputInvalid("not_json") from None
    if not isinstance(out, dict) or set(out) - _OUT_KEYS or not {"status", "calls"} <= set(out):
        raise LabGenOutputInvalid("bad_shape")
    status = out["status"]
    if status not in ("ok", "partial", "refused"):
        raise LabGenOutputInvalid("bad_status")
    if status == "refused":
        return {"status": "refused", "reason": str(out.get("reason", ""))[:200], "calls": []}
    got = out["calls"]
    if not isinstance(got, list) or [c.get("id") if isinstance(c, dict) else None for c in got] != [c.id for c in calls]:
        raise LabGenOutputInvalid("calls_do_not_match_the_request")
    rows, committed = [], 0
    for spec, c in zip(calls, got):
        if set(c) - _CALL_KEYS or c["status"] not in _STATUSES:
            raise LabGenOutputInvalid("bad_call")
        if c.get("request_sha256") != request_sha256(model, spec.system, spec.user):
            raise LabGenOutputInvalid("request_hash_mismatch")
        known, charged, held = c.get("usage_known"), _int(c.get("charged_micros"), -1), _int(c.get("held_micros"))
        if not isinstance(known, bool):
            raise LabGenOutputInvalid("bad_usage_flag")
        tin, tout = _int(c.get("input_tokens")), _int(c.get("output_tokens"), 0, max_output_tokens)
        text = c.get("text") or ""
        if not isinstance(text, str):
            raise LabGenOutputInvalid("bad_text")
        text_sha = c.get("text_sha256") or None
        if c["status"] == "ok":
            if text_sha != hashlib.sha256(text.encode("utf-8")).hexdigest():
                raise LabGenOutputInvalid("response_hash_mismatch")
        elif text or text_sha:
            raise LabGenOutputInvalid("text_on_a_call_that_is_not_ok")
        if known != (charged >= 0) or (known and held) or (not known and charged != -1):
            raise LabGenOutputInvalid("cost_and_usage_disagree")
        if not known and (held > 0) != (c["status"] in ("ok", "outcome_unknown")):
            raise LabGenOutputInvalid("unknown_cost_must_be_held")
        committed += max(charged, 0) + held
        rows.append({"call_id": spec.id, "status": c["status"], "reason": str(c.get("reason") or "")[:200],
                     "usage_known": known, "input_tokens": tin, "output_tokens": tout, "charged_micros": charged,
                     "held_micros": held, "attempts": _int(c.get("transport_attempts"), 0, 100),
                     "request_sha256": c["request_sha256"], "response_length": len(text.encode("utf-8")),
                     "response_sha256": text_sha, "text": text})
    if committed > budget_limit:
        raise LabGenOutputInvalid("child_exceeded_the_budget")
    return {"status": status, "reason": str(out.get("reason", ""))[:200], "calls": rows}


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)          # the child leads its own session/group: kill everything it spawned
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_lab_gen(cmd: Sequence[str], payload: dict, *, key: Optional[str], timeout: float,
                cancelled: Callable[[], bool] = lambda: False, extra_env: Optional[dict] = None,
                max_stdout: int = MAX_STDOUT_BYTES) -> tuple[Optional[bytes], str]:
    """Run the child with the JSON payload on stdin. Returns ``(stdout, "")`` or ``(None, reason)`` where reason is
    ``timeout`` | ``cancelled`` | ``exit_<code>`` | ``spawn_failed`` | ``output_too_large``. The child gets a minimal
    environment (PATH, the key, test extras) and never the parent's other secrets; it runs in its own process group,
    which is killed on timeout, cancel or oversized output; stdout is read in chunks and never buffered past the cap."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if key:
        env[KEY_ENV] = key
    env.update(extra_env or {})
    try:
        proc = subprocess.Popen(list(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, start_new_session=True)
    except OSError:
        return None, "spawn_failed"
    chunks: list[bytes] = []
    state = {"size": 0, "over": False}

    def pump():
        while True:
            block = proc.stdout.read(65536)
            if not block:
                return
            state["size"] += len(block)
            if state["size"] > max_stdout:
                state["over"] = True
                return
            chunks.append(block)

    def feed():
        try:
            proc.stdin.write(json.dumps(payload).encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass

    threads = [threading.Thread(target=pump, daemon=True), threading.Thread(target=feed, daemon=True)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    reason = ""
    while proc.poll() is None:
        if state["over"]:
            reason = "output_too_large"
        elif cancelled():
            reason = "cancelled"
        elif time.monotonic() > deadline:
            reason = "timeout"
        if reason:
            _kill_group(proc)
            break
        time.sleep(0.05)
    proc.wait()
    for t in threads:
        t.join(timeout=5)
    for stream in (proc.stdout, proc.stdin):
        try:
            stream.close()
        except OSError:
            pass
    if reason:
        return None, reason
    if state["over"]:
        return None, "output_too_large"
    if proc.returncode != 0:
        return None, f"exit_{proc.returncode}"
    return b"".join(chunks), ""


def _unknown_rows(reserved: Sequence[tuple[CallSpec, int]], model: str, reason: str) -> list[dict]:
    """The batch result is lost: settle each reserved call as outcome_unknown, holding exactly what was reserved."""
    return [{"call_id": c.id, "status": "outcome_unknown", "reason": reason, "usage_known": False, "input_tokens": 0,
             "output_tokens": 0, "charged_micros": -1, "attempts": 0, "held_micros": worst,
             "request_sha256": request_sha256(model, c.system, c.user), "response_length": 0, "response_sha256": None}
            for c, worst in reserved]


def _released_rows(reserved: Sequence[tuple[CallSpec, int]], model: str, reason: str) -> list[dict]:
    """No call was made (lab-gen refused before any network use): release the holds, charge nothing."""
    return [{"call_id": c.id, "status": "skipped_after_stop", "reason": reason, "usage_known": False, "input_tokens": 0,
             "output_tokens": 0, "charged_micros": -1, "attempts": 0, "held_micros": 0,
             "request_sha256": request_sha256(model, c.system, c.user), "response_length": 0, "response_sha256": None}
            for c, _ in reserved]


def process_generate_job(conn: psycopg.Connection, project_id: uuid.UUID, job_id: uuid.UUID, *, worker: str,
                         lab_gen_cmd: Sequence[str], cfg: GenModelConfig, build_calls: GenBuilder,
                         connect: Callable[[], psycopg.Connection], api_key: Optional[str],
                         on_results: Optional[OnResults] = None, ttl_seconds: int = 60, child_timeout: float = 300.0,
                         extra_env: Optional[dict] = None) -> ParseOutcome:
    """Claim and run one generation job. Raises ``jobs.LeaseUnavailable`` if it is not claimable."""
    try:
        lease = jobs.acquire(conn, project_id, job_id, worker, ttl_seconds)
    except jobs.LeaseUnavailable:
        try:
            jobs.fail_exhausted(conn, project_id, job_id)
        except Exception as exc:
            if not is_infrastructure(exc):
                raise
        raise
    except Exception as exc:
        if is_infrastructure(exc):
            return _unreachable(job_id)
        raise
    try:
        return _execute(conn, project_id, lease, worker, lab_gen_cmd, cfg, build_calls, connect, api_key, on_results,
                        ttl_seconds, child_timeout, extra_env)
    except jobs.LeaseLost:
        return _stopped(conn, project_id, job_id)
    except Exception as exc:
        if is_infrastructure(exc):
            return _unreachable(job_id)
        try:
            return _fail(conn, project_id, lease, "internal_error")
        except jobs.LeaseLost:
            return _stopped(conn, project_id, job_id)
        except Exception as again:
            if is_infrastructure(again):
                return _unreachable(job_id)
            raise


def _fail(conn, project_id, lease, code) -> ParseOutcome:
    jobs.finish(conn, project_id, lease, "failed", code)
    return ParseOutcome(lease.job_id, "failed", code)


def _execute(conn, project_id, lease, worker, cmd, cfg, build_calls, connect, api_key, on_results, ttl, child_timeout,
             extra_env):
    job_id = lease.job_id
    with conn.transaction():
        row = conn.execute(
            "SELECT g.benchmark_id, g.model, g.max_output_tokens, g.budget_cap_micros, g.currency, g.pricing_revision, "
            "b.status::text, j.attempt FROM generate_jobs g JOIN benchmark_revisions b ON b.project_id = g.project_id "
            "AND b.id = g.benchmark_id JOIN jobs j ON j.project_id = g.project_id AND j.id = g.job_id "
            "WHERE g.project_id = %s AND g.job_id = %s", (project_id, job_id)).fetchone()
    if row is None:
        return _fail(conn, project_id, lease, "generate_job_missing")
    benchmark_id, model, cap_tokens, cap_micros, currency, pricing_rev, bench_status, attempt = row
    if bench_status != "generating":
        return _fail(conn, project_id, lease, "benchmark_not_generating")
    calls = list(build_calls(conn, project_id, benchmark_id))
    if not calls:
        return _fail(conn, project_id, lease, "no_calls")
    worst = {c.id: worst_case_micros(len((c.system + c.user).encode("utf-8")), cap_tokens, cfg.input_micros_per_mtok,
                                     cfg.output_micros_per_mtok) for c in calls}
    # Reserve BEFORE spawning (one transaction, per-benchmark lock): a worker that dies after this point leaves its
    # holds in the ledger, so a retry sees the reduced budget; calls that do not fit are never sent (fail-closed).
    sendable, hold_total = reserve_batch(
        conn, project_id, benchmark_id, lease.job_id, attempt, cap_micros, model, currency, pricing_rev,
        [(c.id, request_sha256(model, c.system, c.user), worst[c.id]) for c in calls])
    if not sendable:
        return _fail(conn, project_id, lease, "budget_exhausted")
    send = [c for c in calls if c.id in set(sendable)]
    reserved = [(c, worst[c.id]) for c in send]
    payload = {
        "model": {"name": model, "protocol": cfg.protocol, "base_url": cfg.base_url, "allowed_host": cfg.allowed_host,
                  "max_output_tokens": cap_tokens, "input_micros_per_mtok": cfg.input_micros_per_mtok,
                  "output_micros_per_mtok": cfg.output_micros_per_mtok, "currency": currency,
                  "pricing_revision": pricing_rev, "timeout_seconds": cfg.timeout_seconds, "max_retries": cfg.max_retries,
                  "requests_per_minute": cfg.requests_per_minute},
        "budget": {"limit_micros": hold_total},
        "calls": [{"id": c.id, "system": c.system, "user": c.user} for c in send]}
    with _Heartbeat(connect, project_id, lease, ttl) as beat:
        stdout, why = run_lab_gen(cmd, payload, key=api_key, timeout=child_timeout,
                                  cancelled=lambda: beat.lost.is_set() or (shutdown is not None and shutdown()),
                                  extra_env=extra_env)
    parsed = None
    if stdout is not None:
        try:
            parsed = parse_lab_gen_output(stdout, send, model=model, max_output_tokens=cap_tokens, budget_limit=hold_total)
        except LabGenOutputInvalid as exc:
            why = f"invalid_output:{exc}"

    def record(c, rows):
        record_llm_calls(c, project_id, benchmark_id, job_id, attempt, model, currency, pricing_rev, rows)

    if parsed is not None and parsed["status"] == "refused":
        # No call was made: release the holds (fenced, together with the job's final status).
        jobs.finish(conn, project_id, lease, "failed", "lab_gen_refused",
                    hook=lambda c: record(c, _released_rows(reserved, model, "lab-gen refused before any call")))
        return ParseOutcome(job_id, "failed", "lab_gen_refused")
    if parsed is None:
        rows = _unknown_rows(reserved, model, "batch result lost")
        if beat.lost.is_set():
            with conn.transaction():          # the holds exist already; this only labels them (same attempt, idempotent)
                record(conn, rows)
            raise jobs.LeaseLost()
        if why == "cancelled" and shutdown is not None and shutdown():
            with conn.transaction():
                record(conn, rows)
            # The worker is stopping: the child was killed and the batch stays held. Give the job back for the next
            # worker (fenced); a user cancel wins (LeaseLost -> acknowledged by the caller).
            if jobs.release_for_shutdown(conn, project_id, lease) == "failed":
                return ParseOutcome(job_id, "failed", "lease_exhausted")
            return ParseOutcome(job_id, "queued", "released: worker shutdown")
        jobs.finish(conn, project_id, lease, "failed", "lab_gen_failed", hook=lambda c: record(c, rows))
        return ParseOutcome(job_id, "failed", "lab_gen_failed")
    rows = parsed["calls"]
    ok = parsed["status"] == "ok"
    plain = [{k: v for k, v in r.items() if k != "text"} for r in rows]

    def hook(c):
        record(c, plain)
        if on_results is not None and ok:
            on_results(c, project_id, benchmark_id, rows)

    try:
        jobs.finish(conn, project_id, lease, "succeeded" if ok else "failed",
                    "generated" if ok else "partial_calls", hook=hook)
    except jobs.LeaseLost:
        with conn.transaction():                                         # the paid result must not be lost
            record(conn, plain)
        raise
    return ParseOutcome(job_id, "succeeded" if ok else "failed", "generated" if ok else "partial_calls")
