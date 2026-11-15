"""Uniform operational logging for all lab services (Issue #191).

Format:
  <ISO-8601 UTC> <service> <context_id> [<MODE>] FUNCTION:{<Function(params)>} <status> [{Return: <ret>}]

Where:
  MODE is CALL, RETURN, OP, ALARM, ERROR.
  Level via LAB_APP_LOG=off|stages|full (default off in tests, full in dev compose).
  Append to logs/app.log (or LAB_APP_LOG_FILE) with cross-process safe file locking and size rotation.
"""
from __future__ import annotations

import asyncio
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


def bound_value(val: Any, max_str_chars: int = 300, max_items: int = 8, depth: int = 0) -> str:
    """Format and bound a value with mandatory secret redaction."""
    try:
        if depth > 4:
            return "..."
        if val is None:
            return "None"
        if isinstance(val, (int, float, bool)):
            return str(val)
        if isinstance(val, (bytes, bytearray)):
            h = hashlib.sha256(val).hexdigest()[:16]
            return f"<{len(val)} bytes sha256:{h}>"
        if isinstance(val, str):
            clean = redact(val)
            if len(clean) > max_str_chars:
                h = hashlib.sha256(val.encode("utf-8", errors="replace")).hexdigest()[:16]
                prefix = clean[:120]
                return f"<{len(clean)} chars sha256:{h}>{prefix}"
            return repr(clean)
        if isinstance(val, dict):
            if len(val) > max_items:
                keys = list(val.keys())[:max_items]
                return f"<dict len={len(val)} keys={[str(k) for k in keys]}>"
            items = []
            for k, v in val.items():
                k_str = str(k)
                if any(sec in k_str.lower() for sec in ("key", "token", "secret", "password", "auth", "url")):
                    items.append(f"{k!r}: '[REDACTED]'")
                else:
                    items.append(f"{k!r}: {bound_value(v, max_str_chars, max_items, depth + 1)}")
            return "{" + ", ".join(items) + "}"
        if isinstance(val, (list, tuple, set)):
            v_list = list(val)
            if len(v_list) > max_items:
                first = bound_value(v_list[0], max_str_chars, max_items, depth + 1)
                return f"<{type(val).__name__} len={len(v_list)} [{first}, ...]>"
            inner = ", ".join(bound_value(item, max_str_chars, max_items, depth + 1) for item in v_list)
            if isinstance(val, tuple):
                return f"({inner},)" if len(v_list) == 1 else f"({inner})"
            if isinstance(val, set):
                return f"{{{inner}}}"
            return f"[{inner}]"
        # generic object fallback
        clean_repr = redact(repr(val))
        if len(clean_repr) > max_str_chars:
            h = hashlib.sha256(clean_repr.encode("utf-8", errors="replace")).hexdigest()[:16]
            return f"<{len(clean_repr)} chars sha256:{h}>{clean_repr[:120]}"
        return clean_repr
    except Exception:
        global _LOG_FAILURES
        _LOG_FAILURES += 1
        return "<unserializable>"


def _rotate_file(path: str) -> None:
    for i in range(BACKUP_COUNT - 1, 0, -1):
        src = f"{path}.{i}"
        dst = f"{path}.{i+1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    if os.path.exists(path):
        try:
            os.replace(path, f"{path}.1")
        except OSError:
            pass


def _write_log_line(line: str) -> None:
    global _LOG_FAILURES
    try:
        path = get_log_path()
        dir_name = os.path.dirname(os.path.abspath(path))
        os.makedirs(dir_name, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                encoded = (line + "\n").encode("utf-8")
                if size + len(encoded) > MAX_BYTES:
                    f.flush()
                    _rotate_file(path)
                    with open(path, "a", encoding="utf-8") as f2:
                        f2.write(line + "\n")
                        f2.flush()
                else:
                    f.write(line + "\n")
                    f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
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
    lvl = get_log_level()
    if lvl == "off":
        return
    if lvl == "stages" and not is_stage and stage is None and mode not in ("ALARM", "ERROR"):
        return

    # Redact parameters
    bound_params = []
    if stage is not None and "stage" not in params:
        bound_params.append(f"stage={stage!r}")
    for k, v in params.items():
        if any(sec in k.lower() for sec in ("key", "token", "secret", "password", "auth", "url", "header")):
            bound_params.append(f"{k}='[REDACTED]'")
        else:
            bound_params.append(f"{k}={bound_value(v)}")
    args_str = ", ".join(bound_params)

    ret_str = bound_value(return_val) if mode == "RETURN" and return_repr_needed(return_val) else (
        bound_value(return_val) if mode == "RETURN" else None
    )
    line = format_log_line(mode, func_name, args_str, status, ret_str)
    _write_log_line(line)


def return_repr_needed(val: Any) -> bool:
    return True


def log_op(op_name: str, status: str = "ok", stage: str | None = None, **details) -> None:
    log_event("OP", op_name, status, stage=stage, **details)


def log_alarm(func_name: str, message: str, stage: str | None = None, **details) -> None:
    log_event("ALARM", func_name, message, stage=stage, **details)


def log_error(func_name: str, exc_type: str, reason_code: str, stage: str | None = None, **details) -> None:
    log_event("ERROR", func_name, f"{exc_type}: {reason_code}", stage=stage, **details)


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
                log_event("CALL", name, "started", stage=stage, is_stage=is_stage, **params)
                try:
                    res = await fn(*args, **kwargs)
                    log_event("RETURN", name, "ok", return_val=res, stage=stage, is_stage=is_stage, **params)
                    return res
                except Exception as exc:
                    code = getattr(exc, "code", getattr(exc, "reason", type(exc).__name__))
                    log_event("ERROR", name, f"{type(exc).__name__}: {code}", stage=stage, is_stage=is_stage, **params)
                    raise
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            lvl = get_log_level()
            if lvl == "off":
                return fn(*args, **kwargs)
            params = _extract_params(args, kwargs)
            log_event("CALL", name, "started", stage=stage, is_stage=is_stage, **params)
            try:
                res = fn(*args, **kwargs)
                log_event("RETURN", name, "ok", return_val=res, stage=stage, is_stage=is_stage, **params)
                return res
            except Exception as exc:
                code = getattr(exc, "code", getattr(exc, "reason", type(exc).__name__))
                log_event("ERROR", name, f"{type(exc).__name__}: {code}", stage=stage, is_stage=is_stage, **params)
                raise
        return sync_wrapper

    return decorator
