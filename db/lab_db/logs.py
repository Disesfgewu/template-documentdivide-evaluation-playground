"""Structured log events with mandatory redaction (SPEC 10.3, T69).

Every free-text and attribute value passes through ``redact`` before it is
stored: Authorization headers, bearer tokens, API keys, passwords in URLs,
signed-URL query parameters and cookies are masked. The applied policy is
stored with the event (``redaction_policy``)."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]

    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, obj: Any) -> None:
            self.obj = obj

        def __repr__(self) -> str:
            return f"Jsonb({self.obj!r})"

        def __eq__(self, other: Any) -> bool:
            return isinstance(other, Jsonb) and self.obj == other.obj

REDACTION_POLICY = "lab-redact-1"
MASK = "[REDACTED]"
MAX_ATTRIBUTES_BYTES = 16 * 1024  # 16 KiB


class LogConflict(Exception):
    """Raised when re-inserting an existing event_id with conflicting content."""
    pass


# Boundaries use (?<![A-Za-z0-9]) instead of \b: underscores are word
# characters, so \b would miss names such as svc_key= or err_sk-...
_PATTERNS = [
    # Authorization / Proxy-Authorization header values (any scheme)
    (re.compile(r"(?i)\b((?:proxy-)?authorization)\s*[:=]\s*[^\r\n,;]+"), r"\1: " + MASK),
    # Bearer tokens (1 or more characters)
    (re.compile(r"(?i)(?<![A-Za-z0-9])bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + MASK),
    # Cookie / Set-Cookie headers
    (re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*[^\r\n]+"), r"\1: " + MASK),
    # PEM private key blocks (-----BEGIN ... PRIVATE KEY----- to END)
    (re.compile(r"(?is)-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----.*?-----END[ A-Z0-9_-]*PRIVATE KEY-----"), MASK),
    # JWT (three segments of base64url starting with eyJ)
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), MASK),
    # user:password@ in URLs (handles @ and / inside password up to the last @ before host)
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):(?:[^\s\"']*@)+"), r"\1:" + MASK + "@"),
    # credential-like query parameters (signed URLs, keys, tokens)
    (re.compile(r"(?i)([?&](?:x-amz-[a-z-]*|x-goog-[a-z-]*|sig|signature|token|access_token|"
                r"api[_-]?key|apikey|key|password|passwd|secret|client_secret|credential|"
                r"private_key|session|jwt|bearer|se|sp|sv)=)[^&\s#]+"), r"\1" + MASK),
    # key=value / "key": "value" style secrets (1 or more characters)
    (re.compile(r"(?i)(?<![A-Za-z0-9])((?:api[_-]?key|access[_-]?key|client[_-]?secret|key|secret|password|passwd|token|"
                r"credential|private[_-]?key|session|jwt|bearer)\"?\s*[:=]\s*\"?)[^\s\"',;&]+"), r"\1" + MASK),
    # well-known provider key shapes
    (re.compile(r"(?<![A-Za-z0-9])(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,})(?![A-Za-z0-9_-])"), MASK),
]

_SECRET_KEYS = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|client[_-]?secret|"
    r"api[_-]?key|access[_-]?key|token|credential|private[_-]?key|session|jwt|bearer)"
)


def redact(text: str) -> str:
    """Mask sensitive information in a string according to redaction patterns."""
    if not isinstance(text, str):
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_value(value: Any, key: str = "") -> Any:
    """Recursively mask sensitive values based on dictionary keys or pattern matches."""
    if key and _SECRET_KEYS.search(key):
        return MASK
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value


def log_event(
    conn: psycopg.Connection,
    *,
    level: str,
    event_name: str,
    message: str,
    service: str,
    component: str,
    operation_name: str,
    telemetry_origin: str = "lab_observed",
    occurred_at: Optional[datetime] = None,
    event_id: Optional[uuid.UUID] = None,
    attributes: Optional[dict] = None,
    error_message: Optional[str] = None,
    **ids: Any,
) -> uuid.UUID:
    """Insert one log event with mandatory redaction on all free-text fields.

    Re-sending the same event_id with identical content returns event_id idempotently.
    Re-sending the same event_id with conflicting content raises LogConflict.
    If event_name contains secret patterns, raises ValueError (due to format CHECK).
    If serialized attributes exceed 16 KiB, raises ValueError.
    """
    # 1. Event name validation: must not contain secret patterns (DB has format CHECK)
    if not isinstance(event_name, str):
        raise ValueError(f"event_name must be a string, got {type(event_name).__name__}")
    if redact(event_name) != event_name:
        raise ValueError(f"event_name contains secret pattern: {event_name}")

    # 2. Text fields redaction
    redacted_message = redact(message) if isinstance(message, str) else message
    redacted_service = redact(service) if isinstance(service, str) else service
    redacted_component = redact(component) if isinstance(component, str) else component
    redacted_operation_name = redact(operation_name) if isinstance(operation_name, str) else operation_name
    redacted_error_message = redact(error_message) if isinstance(error_message, str) else None

    # 3. Allowed ids and redaction of text fields in ids (status, error_code)
    allowed = {
        "trace_id", "span_id", "parent_span_id", "project_id", "operation_id", "job_id",
        "run_id", "question_id", "status", "duration_ms", "error_code", "usage_known"
    }
    unknown = set(ids) - allowed
    if unknown:
        raise ValueError(f"unknown log fields: {sorted(unknown)}")

    redacted_ids = dict(ids)
    for k in ("status", "error_code"):
        if k in redacted_ids and isinstance(redacted_ids[k], str):
            redacted_ids[k] = redact(redacted_ids[k])

    # 4. Attributes serialization and size check (<= 16 KiB limit)
    attr_dict = attributes if attributes is not None else {}
    if not isinstance(attr_dict, dict):
        raise ValueError(f"attributes must be a dict, got {type(attr_dict).__name__}")
    redacted_attributes = redact_value(attr_dict)
    try:
        serialized_attrs = json.dumps(redacted_attributes, ensure_ascii=False, default=str)
    except Exception as exc:
        raise ValueError(f"failed to serialize attributes: {exc}") from exc

    if len(serialized_attrs.encode("utf-8")) > MAX_ATTRIBUTES_BYTES:
        raise ValueError(
            f"attributes serialized size ({len(serialized_attrs.encode('utf-8'))} bytes) "
            f"exceeds 16 KiB limit ({MAX_ATTRIBUTES_BYTES} bytes)"
        )

    # 5. Insert with ON CONFLICT (event_id) DO NOTHING RETURNING id
    event_id = event_id or uuid.uuid4()
    cols = [
        "event_id", "schema_version", "occurred_at", "level", "event_name", "message",
        "service", "component", "operation_name", "telemetry_origin", "redaction_policy",
        "attributes", "error_message"
    ]
    vals = [
        event_id, 1, occurred_at or datetime.now(timezone.utc), level, event_name,
        redacted_message, redacted_service, redacted_component, redacted_operation_name,
        telemetry_origin, REDACTION_POLICY, Jsonb(redacted_attributes), redacted_error_message
    ]
    for k in sorted(redacted_ids):
        cols.append(k)
        vals.append(redacted_ids[k])

    placeholders = ", ".join(["%s"] * len(cols))
    sql = (
        f"INSERT INTO log_events ({', '.join(cols)}) VALUES ({placeholders}) "
        "ON CONFLICT (event_id) DO NOTHING RETURNING id"
    )
    cur = conn.execute(sql, vals)
    inserted = None
    if cur is not None:
        try:
            inserted = cur.fetchone()
        except Exception:
            inserted = None

    if inserted is None:
        check_cur = conn.execute(
            "SELECT level, event_name, message, service, component, operation_name "
            "FROM log_events WHERE event_id = %s",
            (event_id,),
        )
        existing = check_cur.fetchone() if check_cur is not None else None
        if existing is not None:
            if isinstance(existing, dict):
                existing_vals = (
                    existing.get("level"),
                    existing.get("event_name"),
                    existing.get("message"),
                    existing.get("service"),
                    existing.get("component"),
                    existing.get("operation_name"),
                )
            else:
                existing_vals = tuple(existing)
            expected = (
                level,
                event_name,
                redacted_message,
                redacted_service,
                redacted_component,
                redacted_operation_name,
            )
            if existing_vals != expected:
                raise LogConflict(
                    f"Conflicting log event for event_id {event_id}: "
                    f"existing={existing_vals}, incoming={expected}"
                )

    return event_id
