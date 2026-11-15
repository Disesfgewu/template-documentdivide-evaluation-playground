"""Uniform operational logging for all lab services (Issue #191).

Format:
  <ISO-8601 UTC> <service> <context_id> [<MODE>] FUNCTION:{<Function(params)>} <status> [{Return: <ret>}]

Where:
  MODE is CALL, RETURN, OP, ALARM, ERROR.
  Level via LAB_APP_LOG=off|stages|full (default off in tests, full in dev compose).
  Append to logs/app.log (or LAB_APP_LOG_FILE) with cross-process safe file locking and size rotation.
"""
from __future__ import annotations

import dataclasses
import itertools
import re
import uuid
import contextvars
import fcntl
import functools
import hashlib
import inspect
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from lab_db.logs import redact, redact_value

DEFAULT_LOG_PATH = "logs/app.log"
MAX_BYTES = 20 * 1024 * 1024  # 20 MiB
BACKUP_COUNT = 5

_current_service: contextvars.ContextVar[str] = contextvars.ContextVar("service", default="")
_current_context_id: contextvars.ContextVar[str] = contextvars.ContextVar("context_id", default="-")
_current_stage: contextvars.ContextVar[str] = contextvars.ContextVar("stage", default="")
_LOG_FAILURES = 0


def get_log_failures() -> int:
    return _LOG_FAILURES


def get_log_level() -> str:
    return os.environ.get("LAB_APP_LOG", "off").lower().strip()


def get_log_path() -> str:
    return os.environ.get("LAB_APP_LOG_FILE") or os.environ.get("LAB_APP_LOG_PATH") or DEFAULT_LOG_PATH


def set_context(*, job_id: Any = None, run_id: Any = None, request_id: Any = None,
                service: str | None = None, stage: str | None = None):
    """Set the context for operational logging. Returns a token list or context manager."""
    if service is not None:
        _current_service.set(service)
    if stage is not None:
        _current_stage.set(stage)
    if job_id is not None:
        _current_context_id.set(f"job={job_id}")
    elif run_id is not None:
        _current_context_id.set(f"run={run_id}")
    elif request_id is not None:
        _current_context_id.set(f"request={request_id}")


class ContextScope:
    def __init__(self, **kw):
        self.kw = kw
        self.tokens = []

    def __enter__(self):
        if "service" in self.kw and self.kw["service"] is not None:
            self.tokens.append((_current_service, _current_service.set(self.kw["service"])))
        if "stage" in self.kw and self.kw["stage"] is not None:
            self.tokens.append((_current_stage, _current_stage.set(self.kw["stage"])))
        cid = None
        if self.kw.get("job_id"):
            cid = f"job={self.kw['job_id']}"
        elif self.kw.get("run_id"):
            cid = f"run={self.kw['run_id']}"
        elif self.kw.get("request_id"):
            cid = f"request={self.kw['request_id']}"
        elif self.kw.get("context_id"):
            cid = str(self.kw["context_id"])
        if cid is not None:
            self.tokens.append((_current_context_id, _current_context_id.set(cid)))
        return self

    def __exit__(self, *args):
        for var, token in reversed(self.tokens):
            var.reset(token)


_SENSITIVE = re.compile(r"key|token|secret|password|passwd|auth|url|dsn|cookie|credential|header", re.I)
_RAW = {"text", "system", "user", "prompt", "reply", "stdout", "stderr", "data", "shown_text", "quote", "content", "file_content", "body", "by_page", "response", "payload", "raw", "raw_text", "normalized_text"}
_DSN = re.compile(r"(?i)postgres(?:ql)?(?:\+[a-z0-9]+)?://[^\s\"'<>]+")


def _clean(value: str) -> str:
    return _DSN.sub("[REDACTED]", redact(value))


def _digest(value) -> str:
    data = value.encode("utf-8", errors="replace") if isinstance(value, str) else bytes(value)
    return f"<{len(value)} {'chars' if isinstance(value, str) else 'bytes'} sha256:{hashlib.sha256(data).hexdigest()[:16]}>"


def _field(name, value, depth=0):
    if _SENSITIVE.search(name):
        return "'[REDACTED]'"
    if (name.lower() in _RAW or name.lower().endswith("_text")) and isinstance(value, (str, bytes, bytearray)):
        return _digest(value)
    return bound_value(value, depth=depth)


def bound_value(val: Any, max_str_chars: int = 300, max_items: int = 8, depth: int = 0) -> str:
    """Bound every traversal; never invoke application objects' repr/str methods."""
    try:
        if depth > 4:
            return "..."
        if val is None or type(val) in (int, float, bool):
            return str(val)[:100]
        if isinstance(val, uuid.UUID):
            return str(val)
        if type(val) in (bytes, bytearray):
            return _digest(val)
        if type(val) is str:
            clean = _clean(val)
            return _digest(val) + repr(clean[:120]) if len(clean) > max_str_chars else repr(clean)
        if type(val) is dict:
            items = []
            for k, v in itertools.islice(val.items(), max_items):
                name = k if type(k) is str else "field"
                items.append(f"{_clean(name[:80])!r}: {_field(name, v, depth + 1)}")
            suffix = f", ... len={len(val)}" if len(val) > max_items else ""
            return "{" + ", ".join(items) + suffix + "}"
        if type(val) in (list, tuple, set, frozenset):
            items = [bound_value(v, max_str_chars, max_items, depth + 1) for v in itertools.islice(val, max_items)]
            suffix = f", ... len={len(val)}" if len(val) > max_items else ""
            return "[" + ", ".join(items) + suffix + "]"
        if dataclasses.is_dataclass(val) and not isinstance(val, type):
            fields = {f.name: getattr(val, f.name) for f in itertools.islice(dataclasses.fields(val), max_items)}
            return bound_value(fields, max_str_chars, max_items, depth + 1)
        return "<object>"
    except Exception:
        global _LOG_FAILURES
        _LOG_FAILURES += 1
        return "<unserializable>"


def _write_log_line(line: str) -> None:
    global _LOG_FAILURES
    try:
        path = get_log_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        # The lock inode never rotates. Open the data file only AFTER acquiring it.
        fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            encoded = (line.replace("\n", "\\n").replace("\r", "\\r")[:16384] + "\n").encode("utf-8")
            if os.path.exists(path) and os.path.getsize(path) + len(encoded) > MAX_BYTES:
                for i in range(BACKUP_COUNT - 1, 0, -1):
                    if os.path.exists(f"{path}.{i}"):
                        os.replace(f"{path}.{i}", f"{path}.{i+1}")
                os.replace(path, path + ".1")
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o640)
            with os.fdopen(fd, "ab") as out:
                out.write(encoded)
    except Exception:
        _LOG_FAILURES += 1


def format_log_line(mode: str, func_name: str, args_repr: str, status: str, return_repr: str | None = None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    svc = _current_service.get() or os.environ.get("LAB_SERVICE_NAME", "api")
    cid = _current_context_id.get() or "-"
    ret_part = f" {{Return: {return_repr}}}" if return_repr is not None else ""
    return f"{ts} {svc} {cid} [{mode}] FUNCTION:{{{func_name}({args_repr})}} {status}{ret_part}"


def log_event(mode: str, func_name: str, status: str, return_val: Any = None,
              stage: str | None = None, is_stage: bool = False, **params) -> None:
    global _LOG_FAILURES
    try:
        lvl = get_log_level()
        if lvl not in ("stages", "full"):
            return
        if lvl == "stages" and not is_stage and stage is None and mode not in ("ALARM", "ERROR"):
            return
        parts = [f"stage={stage!r}"] if stage is not None else []
        for k, v in itertools.islice(params.items(), 32):
            parts.append(f"{k}={_field(k, v)}")
        ret = bound_value(return_val) if mode == "RETURN" else None
        line = format_log_line(mode, func_name, ", ".join(parts), _clean(status), ret)
        _write_log_line(_clean(line))
    except Exception:
        _LOG_FAILURES += 1


def log_op(op_name: str, status: str = "ok", stage: str | None = None, **details) -> None:
    log_event("OP", op_name, status, stage=stage, **details)


def log_alarm(func_name: str, message: str, stage: str | None = None, **details) -> None:
    log_event("ALARM", func_name, message, stage=stage, **details)


def log_error(func_name: str, exc_type: str, reason_code: str, stage: str | None = None, **details) -> None:
    log_event("ERROR", func_name, f"{exc_type}: {reason_code}", stage=stage, **details)


def _reason(exc):
    try:
        code = getattr(exc, "code", None)
        return code if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "operation_failed"
    except Exception:
        return "operation_failed"


def app_traced(stage: str | None = None, is_stage: bool = False):
    """Decorator to log entry ([CALL]), exit ([RETURN]), and failure ([ERROR])."""
    def decorator(fn: Callable) -> Callable:
        name = fn.__name__
        sig = None
        try:
            sig = inspect.signature(fn)
        except (ValueError, TypeError):
            pass

        def _extract_params(args, kwargs):
            params = {}
            if sig:
                try:
                    bound = sig.bind_partial(*args, **kwargs)
                    bound.apply_defaults()
                    params = dict(bound.arguments)
                except Exception:
                    params = dict(kwargs)
            else:
                params = dict(kwargs)
            # Remove self / cls
            params.pop("self", None)
            params.pop("cls", None)
            # Do not pass conn/db connections or heavy objects
            for k in list(params.keys()):
                if "conn" in k or k == "request" or k == "store":
                    params.pop(k, None)
            return params

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                lvl = get_log_level()
                if lvl == "off":
                    return await fn(*args, **kwargs)
                params = _extract_params(args, kwargs)
                log_event("CALL", name, "started", stage=stage, is_stage=is_stage, inputs=params)
                try:
                    res = await fn(*args, **kwargs)
                    log_event("RETURN", name, "ok", return_val=res, stage=stage, is_stage=is_stage, inputs=params)
                    return res
                except Exception as exc:
                    code = _reason(exc)
                    log_event("ERROR", name, f"{type(exc).__name__}: {code}", stage=stage, is_stage=is_stage, inputs=params)
                    raise
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            lvl = get_log_level()
            if lvl == "off":
                return fn(*args, **kwargs)
            params = _extract_params(args, kwargs)
            log_event("CALL", name, "started", stage=stage, is_stage=is_stage, inputs=params)
            try:
                with ContextScope(job_id=params.get("job_id"), run_id=params.get("run_id"),
                                  service=("parse-worker" if stage == "parse" else "generate-worker") if name.startswith("process_") else None):
                    res = fn(*args, **kwargs)
                log_event("RETURN", name, "ok", return_val=res, stage=stage, is_stage=is_stage, inputs=params)
                return res
            except Exception as exc:
                code = _reason(exc)
                log_event("ERROR", name, f"{type(exc).__name__}: {code}", stage=stage, is_stage=is_stage, inputs=params)
                raise
        return sync_wrapper

    return decorator
