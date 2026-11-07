"""Bounded structured HTTP request/error logging foundation for the LAB API.

This module implements an independent ASGI middleware and structured event
boundary (SPEC §10.3, R18, R19, T69) for the existing LAB API routes.

Scope and guarantees:
- Emits one bounded, allowlisted ``RequestLogEvent`` per HTTP ASGI request after
  the downstream ASGI callable returns or raises.
- Ensures ``lab_api.request_log`` has an active ``INFO`` handler via
  ``configure_request_logger()`` when the default sink is used, without mutating
  or suppressing the root logger or external server loggers.
- Route identities are allowlisted fixed templates (``/healthz``, ``/readyz``,
  or ``unmatched``); raw request paths, path parameters, raw_path bytes, and
  query strings are never recorded in ``RequestLogEvent``.
- HTTP methods are restricted to a fixed standard allowlist; non-standard or
  malformed method strings are normalized to ``INVALID_METHOD``.
- HTTP status codes are validated as exact ``int`` values (excluding ``bool``)
  in ``100..599``; missing or invalid status values are recorded as ``None``.
- Headers, cookies, authorization tokens, request/response bodies, client
  network addresses, settings/DB URLs, and raw exception messages or tracebacks
  are never inspected for logging or included in ``RequestLogEvent``.
- Unhandled exceptions and ``BaseException`` subclasses (such as
  ``asyncio.CancelledError``) are classified using a closed allowlist of fixed
  error codes/types and re-raised unchanged so ASGI server and FastAPI error
  semantics are preserved.
- Synchronous observer callbacks are isolated: if an observer raises, the
  failure is counted on ``RequestLoggingMiddleware.dropped_count`` and never
  alters HTTP response behavior or masks application exceptions.

Explicit non-claims and upstream server logging limits:
- This primitive governs only ``RequestLogEvent`` records emitted by
  ``RequestLoggingMiddleware`` (and ``lab_api.request_log``). It does NOT
  control or silence external ASGI server loggers (such as ``uvicorn.access``
  or ``uvicorn.error``) or process-level crash dumps.
- Uvicorn's default access logger logs raw request targets and query strings.
  Recommended dev/prod startup therefore passes ``--no-access-log`` (see
  ``UVICORN_SAFE_CLI_ARGS`` and ``services/api/README.md``):
  ``uvicorn --factory lab_api.main:create_app --no-access-log --port 8000``.
- Because unhandled exceptions are re-raised to preserve standard ASGI/FastAPI
  error handling, upstream server error loggers (e.g. ``uvicorn.error``) may
  still format tracebacks and exception messages when an endpoint raises an
  unhandled exception. Existing LAB API endpoints (``/healthz`` and ``/readyz``
  via ``run_checks``) catch dependency errors internally and return bounded
  status payloads; future business endpoints and global exception handlers
  (tracked in #4, #5, #14) must similarly convert domain failures into bounded
  ``ErrorResponse`` payloads before they escape to the ASGI server, or pair the
  server with a sanitizing log formatter.
- This primitive is in-process diagnostic observation only. It is NOT durable
  PostgreSQL ``log_events`` or ``audit_events`` storage, collector buffering,
  gap detection, deduplication, or log export/filtering (T68), nor does it
  implement distributed trace propagation, authentication, role/ACL enforcement
  (T70), or full security/operational acceptance for Issue #14.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TextIO

from .config import Environment, ProviderMode

LOGGER_NAME = "lab_api.request_log"
SCHEMA_VERSION = "lab.api.request_log.v1"
SERVICE_NAME = "lab-api"
COMPONENT_NAME = "asgi.http"
TELEMETRY_ORIGIN = "lab_observed"
UVICORN_SAFE_CLI_ARGS: tuple[str, ...] = (
    "--factory",
    "lab_api.main:create_app",
    "--no-access-log",
)

# Closed allowlist of standard HTTP methods. Any other value from the ASGI
# scope is replaced with INVALID_METHOD so untrusted method tokens cannot
# inject arbitrary strings or log lines.
ALLOWED_HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"}
)
INVALID_HTTP_METHOD = "INVALID_METHOD"

# Closed allowlist of known route templates on the current LAB API surface,
# plus the fixed fallback token for unmatched or non-business paths.
ROUTE_HEALTHZ = "/healthz"
ROUTE_READYZ = "/readyz"
ROUTE_UNMATCHED = "unmatched"
ALLOWED_ROUTES: frozenset[str] = frozenset({ROUTE_HEALTHZ, ROUTE_READYZ, ROUTE_UNMATCHED})

OPERATION_BY_ROUTE: dict[str, str] = {
    ROUTE_HEALTHZ: "http.healthz",
    ROUTE_READYZ: "http.readyz",
    ROUTE_UNMATCHED: "http.unmatched",
}

EVENT_COMPLETED = "http.request.completed"
EVENT_FAILED = "http.request.failed"
ALLOWED_EVENT_NAMES: frozenset[str] = frozenset({EVENT_COMPLETED, EVENT_FAILED})

LEVEL_INFO = "INFO"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"
ALLOWED_LEVELS: frozenset[str] = frozenset({LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR})

OUTCOME_OK = "ok"
OUTCOME_CLIENT_ERROR = "client_error"
OUTCOME_SERVER_ERROR = "server_error"
OUTCOME_ABORTED = "aborted"
OUTCOME_EXCEPTION = "exception"

ERROR_CODE_HTTP_5XX = "http_5xx"
ERROR_CODE_INCOMPLETE = "incomplete_response"
ERROR_CODE_INVALID_STATUS = "invalid_http_status"
ERROR_CODE_CANCELLED = "request_cancelled"
ERROR_CODE_TIMEOUT = "request_timeout"
ERROR_CODE_UNHANDLED = "unhandled_exception"

ERROR_TYPE_CANCELLED = "CancelledError"
ERROR_TYPE_TIMEOUT = "TimeoutError"
ERROR_TYPE_RUNTIME = "RuntimeError"
ERROR_TYPE_VALUE = "ValueError"
ERROR_TYPE_TYPE = "TypeError"
ERROR_TYPE_KEY = "KeyError"
ERROR_TYPE_INDEX = "IndexError"
ERROR_TYPE_ATTRIBUTE = "AttributeError"
ERROR_TYPE_LOOKUP = "LookupError"
ERROR_TYPE_ASSERTION = "AssertionError"
ERROR_TYPE_ARITHMETIC = "ArithmeticError"
ERROR_TYPE_OS = "OSError"
ERROR_TYPE_EXCEPTION = "Exception"
ERROR_TYPE_BASE_EXCEPTION = "BaseException"

ALLOWED_ERROR_TYPES: frozenset[str] = frozenset(
    {
        ERROR_TYPE_CANCELLED,
        ERROR_TYPE_TIMEOUT,
        ERROR_TYPE_RUNTIME,
        ERROR_TYPE_VALUE,
        ERROR_TYPE_TYPE,
        ERROR_TYPE_KEY,
        ERROR_TYPE_INDEX,
        ERROR_TYPE_ATTRIBUTE,
        ERROR_TYPE_LOOKUP,
        ERROR_TYPE_ASSERTION,
        ERROR_TYPE_ARITHMETIC,
        ERROR_TYPE_OS,
        ERROR_TYPE_EXCEPTION,
        ERROR_TYPE_BASE_EXCEPTION,
    }
)

_HEX_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

REQUEST_LOG_FIELDS: tuple[str, ...] = (
    "event_id",
    "schema_version",
    "occurred_at",
    "observed_at",
    "level",
    "event_name",
    "service",
    "component",
    "env",
    "provider_mode",
    "request_id",
    "operation_name",
    "http_method",
    "route",
    "status",
    "http_status",
    "duration_ms",
    "error_code",
    "error_type",
    "telemetry_origin",
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_hex_id() -> str:
    return uuid.uuid4().hex


def sanitize_http_method(raw_method: Any) -> str:
    """Return an allowlisted uppercase HTTP method token or ``INVALID_METHOD``."""
    if not isinstance(raw_method, str):
        return INVALID_HTTP_METHOD
    if len(raw_method) == 0 or len(raw_method) > 16:
        return INVALID_HTTP_METHOD
    # Only exact uppercase ASCII tokens in ALLOWED_HTTP_METHODS are accepted.
    if raw_method in ALLOWED_HTTP_METHODS:
        return raw_method
    return INVALID_HTTP_METHOD


def sanitize_http_status(raw_status: Any) -> tuple[int | None, bool]:
    """Validate an ASGI response status code.

    Returns ``(status_code, valid)``. Booleans are explicitly rejected because
    ``isinstance(True, int)`` is ``True`` in Python.
    """
    if isinstance(raw_status, bool) or not isinstance(raw_status, int):
        return None, False
    if 100 <= raw_status <= 599:
        return raw_status, True
    return None, False


def classify_route(scope: dict[str, Any]) -> str:
    """Map an ASGI scope to a fixed allowlisted route template.

    Never echoes dynamic path segments, query strings, or unmatched paths.
    """
    route_obj = scope.get("route")
    route_path = getattr(route_obj, "path", None)
    if isinstance(route_path, str) and route_path in ALLOWED_ROUTES and route_path != ROUTE_UNMATCHED:
        return route_path
    raw_path = scope.get("path")
    if isinstance(raw_path, str) and raw_path in (ROUTE_HEALTHZ, ROUTE_READYZ):
        return raw_path
    return ROUTE_UNMATCHED


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map an exception to a fixed ``(error_code, error_type)`` pair.

    Never inspects ``str(exc)``, ``exc.args``, ``__cause__``, or ``__context__``,
    and never emits custom or dynamically constructed class names outside the
    closed allowlist.
    """
    if isinstance(exc, asyncio.CancelledError):
        return ERROR_CODE_CANCELLED, ERROR_TYPE_CANCELLED
    if isinstance(exc, TimeoutError):
        return ERROR_CODE_TIMEOUT, ERROR_TYPE_TIMEOUT
    if isinstance(exc, RuntimeError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_RUNTIME
    if isinstance(exc, ValueError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_VALUE
    if isinstance(exc, TypeError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_TYPE
    if isinstance(exc, KeyError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_KEY
    if isinstance(exc, IndexError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_INDEX
    if isinstance(exc, AttributeError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_ATTRIBUTE
    if isinstance(exc, LookupError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_LOOKUP
    if isinstance(exc, AssertionError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_ASSERTION
    if isinstance(exc, ArithmeticError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_ARITHMETIC
    if isinstance(exc, OSError):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_OS
    if isinstance(exc, Exception):
        return ERROR_CODE_UNHANDLED, ERROR_TYPE_EXCEPTION
    return ERROR_CODE_UNHANDLED, ERROR_TYPE_BASE_EXCEPTION


@dataclass(frozen=True, slots=True)
class RequestLogEvent:
    """Immutable, allowlisted HTTP request log record."""

    event_id: str
    schema_version: str
    occurred_at: str
    observed_at: str
    level: str
    event_name: str
    service: str
    component: str
    env: str
    provider_mode: str
    request_id: str
    operation_name: str
    http_method: str
    route: str
    status: str
    http_status: int | None
    duration_ms: float
    error_code: str | None
    error_type: str | None
    telemetry_origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "level": self.level,
            "event_name": self.event_name,
            "service": self.service,
            "component": self.component,
            "env": self.env,
            "provider_mode": self.provider_mode,
            "request_id": self.request_id,
            "operation_name": self.operation_name,
            "http_method": self.http_method,
            "route": self.route,
            "status": self.status,
            "http_status": self.http_status,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "telemetry_origin": self.telemetry_origin,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)


RequestLogSink = Callable[[RequestLogEvent], None]


class _RequestLogStreamHandler(logging.StreamHandler):
    """Dedicated stream handler initialized for ``lab_api.request_log``."""


def configure_request_logger(stream: TextIO | None = None) -> logging.Logger:
    """Ensure ``lab_api.request_log`` has an active ``INFO`` handler.

    Configures only the dedicated ``lab_api.request_log`` logger so that
    ``INFO``/``WARN``/``ERROR`` JSON request events are emitted to a concrete
    stream sink under default Python and ``uvicorn`` startup (where the root
    logger defaults to ``WARNING`` and has no JSON handler). Never mutates or
    suppresses root or third-party/server loggers (such as ``uvicorn.error`` or
    ``uvicorn.access``). If the caller already attached a non-``NullHandler``
    handler or set a more verbose level on ``lab_api.request_log``, those are
    preserved; propagation remains enabled so ``unittest.TestCase.assertLogs``
    and external root aggregators continue to observe records.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)

    target_stream = stream if stream is not None else sys.stderr
    existing_concrete = [
        h for h in logger.handlers if not isinstance(h, logging.NullHandler)
    ]
    if not existing_concrete:
        handler = _RequestLogStreamHandler(target_stream)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    elif stream is not None:
        for h in existing_concrete:
            if isinstance(h, _RequestLogStreamHandler):
                h.setStream(target_stream)
    return logger


def default_log_sink(event: RequestLogEvent) -> None:
    """Emit the serialized JSON event to the ``lab_api.request_log`` logger."""
    logger = configure_request_logger()
    if event.level == LEVEL_ERROR:
        log_level = logging.ERROR
    elif event.level == LEVEL_WARN:
        log_level = logging.WARNING
    else:
        log_level = logging.INFO
    logger.log(log_level, "%s", event.to_json())


class RequestLoggingMiddleware:
    """ASGI middleware that emits one allowlisted ``RequestLogEvent`` per HTTP request."""

    def __init__(
        self,
        app: Any,
        *,
        env: Environment,
        provider_mode: ProviderMode,
        sink: RequestLogSink | None = None,
    ) -> None:
        if not isinstance(env, Environment):
            raise TypeError("env must be an Environment enum")
        if not isinstance(provider_mode, ProviderMode):
            raise TypeError("provider_mode must be a ProviderMode enum")
        self.app = app
        self._env = env.value
        self._provider_mode = provider_mode.value
        if sink is None:
            configure_request_logger()
            self._sink: RequestLogSink = default_log_sink
        else:
            self._sink = sink
        self._dropped_count = 0
        self._lock = threading.Lock()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    def _emit(self, event: RequestLogEvent) -> None:
        try:
            self._sink(event)
        except Exception:
            with self._lock:
                self._dropped_count += 1
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            with self._lock:
                self._dropped_count += 1

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not isinstance(scope, dict) or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        occurred_at = _utc_timestamp()
        start_ns = time.monotonic_ns()
        request_id = _new_hex_id()
        http_method = sanitize_http_method(scope.get("method"))

        # Expose the locally generated request_id on scope["state"] without
        # mutating the caller's state mapping if other middleware shares it.
        existing_state = scope.get("state")
        if isinstance(existing_state, dict):
            state = dict(existing_state)
        else:
            state = {}
        state["request_id"] = request_id
        child_scope = dict(scope)
        child_scope["state"] = state

        response_started = False
        body_finished = False
        trailers_expected = False
        trailers_finished = False
        raw_status: Any = None

        async def send_wrapper(message: Any) -> None:
            nonlocal response_started, raw_status, body_finished, trailers_expected, trailers_finished
            await send(message)
            if isinstance(message, dict) and message.get("type") == "http.response.start":
                if not response_started:
                    response_started = True
                    raw_status = message.get("status")
                    trailers_expected = message.get("trailers", False) is True
            elif isinstance(message, dict) and message.get("type") == "http.response.body":
                if response_started and message.get("more_body", False) is False:
                    body_finished = True
            elif isinstance(message, dict) and message.get("type") == "http.response.trailers":
                if body_finished and message.get("more_trailers", False) is False:
                    trailers_finished = True

        caught_exc: BaseException | None = None
        try:
            await self.app(child_scope, receive, send_wrapper)
        except BaseException as exc:
            caught_exc = exc
            raise
        finally:
            elapsed_ns = max(0, time.monotonic_ns() - start_ns)
            duration_ms = round(elapsed_ns / 1_000_000.0, 3)
            observed_at = _utc_timestamp()
            route = classify_route(child_scope)
            operation_name = OPERATION_BY_ROUTE[route]
            http_status, status_valid = sanitize_http_status(raw_status)

            if caught_exc is not None:
                error_code, error_type = classify_exception(caught_exc)
                level = LEVEL_ERROR
                event_name = EVENT_FAILED
                status = OUTCOME_ABORTED if error_code == ERROR_CODE_CANCELLED else OUTCOME_EXCEPTION
            elif not response_started or not body_finished or (trailers_expected and not trailers_finished):
                error_code = ERROR_CODE_INCOMPLETE
                error_type = None
                level = LEVEL_ERROR
                event_name = EVENT_FAILED
                status = OUTCOME_ABORTED
            elif not status_valid or http_status is None:
                error_code = ERROR_CODE_INVALID_STATUS
                error_type = None
                level = LEVEL_ERROR
                event_name = EVENT_FAILED
                status = OUTCOME_SERVER_ERROR
                http_status = None
            elif http_status >= 500:
                error_code = ERROR_CODE_HTTP_5XX
                error_type = None
                level = LEVEL_ERROR
                event_name = EVENT_FAILED
                status = OUTCOME_SERVER_ERROR
            elif http_status >= 400:
                error_code = None
                error_type = None
                level = LEVEL_WARN
                event_name = EVENT_COMPLETED
                status = OUTCOME_CLIENT_ERROR
            else:
                error_code = None
                error_type = None
                level = LEVEL_INFO
                event_name = EVENT_COMPLETED
                status = OUTCOME_OK

            event = RequestLogEvent(
                event_id=_new_hex_id(),
                schema_version=SCHEMA_VERSION,
                occurred_at=occurred_at,
                observed_at=observed_at,
                level=level,
                event_name=event_name,
                service=SERVICE_NAME,
                component=COMPONENT_NAME,
                env=self._env,
                provider_mode=self._provider_mode,
                request_id=request_id,
                operation_name=operation_name,
                http_method=http_method,
                route=route,
                status=status,
                http_status=http_status,
                duration_ms=duration_ms,
                error_code=error_code,
                error_type=error_type,
                telemetry_origin=TELEMETRY_ORIGIN,
            )
            self._emit(event)
