"""Structured log events with mandatory redaction (SPEC 10.3, T69).

Every free-text and attribute value passes through ``redact`` before it is
stored: Authorization headers, bearer tokens, API keys, passwords in URLs,
signed-URL query parameters and cookies are masked. The applied policy is
stored with the event (``redaction_policy``)."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

REDACTION_POLICY = "lab-redact-1"
MASK = "[REDACTED]"

_PATTERNS = [
    # Authorization / Proxy-Authorization header values (any scheme)
    (re.compile(r"(?i)\b((?:proxy-)?authorization)\s*[:=]\s*[^\r\n,;]+"), r"\1: " + MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + MASK),
    (re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*[^\r\n]+"), r"\1: " + MASK),
    # user:password@ in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):[^@\s/]+@"), r"\1:" + MASK + "@"),
    # credential-like query parameters (signed URLs, keys, tokens)
    (re.compile(r"(?i)([?&](?:x-amz-[a-z-]*|sig|signature|token|access_token|api_key|apikey|key|"
                r"x-goog-[a-z-]*|se|sp|sv|password|secret)=)[^&\s#]+"), r"\1" + MASK),
    # key=value / "key": "value" style secrets
    (re.compile(r"(?i)\b((?:api[_-]?key|secret|password|passwd|token|access[_-]?key)\"?\s*[:=]\s*\"?)"
                r"[^\s\"',;&]{4,}"), r"\1" + MASK),
    # well-known provider key shapes
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,})\b"), MASK),
]
_SECRET_KEYS = re.compile(r"(?i)(authorization|cookie|password|passwd|secret|api[_-]?key|access[_-]?key|token)")


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_value(value: Any, key: str = "") -> Any:
    if key and _SECRET_KEYS.search(key):
        return MASK
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def log_event(conn: psycopg.Connection, *, level: str, event_name: str, message: str, service: str,
              component: str, operation_name: str, telemetry_origin: str = "lab_observed",
              occurred_at: Optional[datetime] = None, event_id: Optional[uuid.UUID] = None,
              attributes: Optional[dict] = None, error_message: Optional[str] = None, **ids: Any) -> uuid.UUID:
    """Insert one event; re-sending the same event_id is a no-op."""
    event_id = event_id or uuid.uuid4()
    allowed = {"trace_id", "span_id", "parent_span_id", "project_id", "operation_id", "job_id", "run_id",
               "question_id", "status", "duration_ms", "error_code", "usage_known"}
    unknown = set(ids) - allowed
    if unknown:
        raise ValueError(f"unknown log fields: {sorted(unknown)}")
    cols = ["event_id", "schema_version", "occurred_at", "level", "event_name", "message", "service",
            "component", "operation_name", "telemetry_origin", "redaction_policy", "attributes", "error_message"]
    vals = [event_id, 1, occurred_at or datetime.now(timezone.utc), level, event_name, redact(message), service,
            component, operation_name, telemetry_origin, REDACTION_POLICY,
            Jsonb(redact_value(attributes or {})), redact(error_message) if error_message else None]
    for k in sorted(ids):
        cols.append(k)
        vals.append(ids[k])
    placeholders = ", ".join(["%s"] * len(cols))
    conn.execute(f"INSERT INTO log_events ({', '.join(cols)}) VALUES ({placeholders}) "
                 "ON CONFLICT (event_id) DO NOTHING", vals)
    return event_id
