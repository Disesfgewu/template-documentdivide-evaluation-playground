"""Bounded HTTP/ASGI trace-context correlation for the LAB API.

This module implements the API-side correlation boundary required by SPEC §10.3:
- Explicit trusted ingress (``trust_remote_traceparent``, default ``False``).
- Strict bounded version-00 W3C ``traceparent`` parsing (55 ASCII chars,
  lowercase hex, nonzero 32-hex trace_id and 16-hex span_id, flags ``00``/``01``).
- Fresh cryptographic IDs when remote input is untrusted, absent, or malformed,
  without rejecting legitimate requests or disclosing user inputs.
- Payload-free request state and response correlation headers (``traceparent``
  and ``X-Request-ID``) that never echo untrusted header values.

Trace identifiers are diagnostic metadata only, never authentication,
authorization, or durable audit storage.
"""
from __future__ import annotations

import contextvars
import secrets
from dataclasses import dataclass
from typing import Any, Callable

TRACE_SCHEMA_VERSION = "api-trace-context-1"
REQUEST_ID_HEADER = "X-Request-ID"
TRACEPARENT_HEADER = "traceparent"

_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16
_LOWER_HEX = frozenset("0123456789abcdef")
_PROPAGATION_HEADERS = frozenset({b"traceparent", b"tracestate", b"baggage"})


class TraceContextError(ValueError):
    """Raised when a trace context or parent header violates the strict profile."""


@dataclass(frozen=True)
class TraceParent:
    """Bounded version-00 subset of W3C traceparent."""

    trace_id: str
    span_id: str
    sampled: bool

    def __post_init__(self) -> None:
        if not _is_valid_hex(self.trace_id, 32) or not _is_valid_hex(self.span_id, 16):
            raise TraceContextError("invalid traceparent identifiers")
        if not isinstance(self.sampled, bool):
            raise TraceContextError("sampled flag must be a bool")

    def to_header(self) -> str:
        flags = "01" if self.sampled else "00"
        return f"00-{self.trace_id}-{self.span_id}-{flags}"


@dataclass(frozen=True)
class TraceContext:
    """Payload-free per-request correlation context."""

    schema_version: str
    request_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    sampled: bool
    ingress: str

    @property
    def traceparent(self) -> str:
        return TraceParent(
            trace_id=self.trace_id,
            span_id=self.span_id,
            sampled=self.sampled,
        ).to_header()


_CURRENT_TRACE_CONTEXT: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "lab_api_trace_context",
    default=None,
)


def current_trace_context() -> TraceContext | None:
    """Return the active request's TraceContext, if inside an instrumented request."""
    return _CURRENT_TRACE_CONTEXT.get()


def request_trace_context(request: Any) -> TraceContext | None:
    """Read the TraceContext attached to a FastAPI/Starlette Request or ASGI scope."""
    state = getattr(request, "state", None)
    if state is not None:
        ctx = getattr(state, "trace_context", None)
        if isinstance(ctx, TraceContext):
            return ctx
    if isinstance(request, dict):
        scope_state = request.get("state")
        if isinstance(scope_state, dict):
            ctx = scope_state.get("trace_context")
            if isinstance(ctx, TraceContext):
                return ctx
    return current_trace_context()


class _OwnedTraceContext:
    """Internal marker wrapper so nested TraceContextMiddleware detects only its own context."""

    __slots__ = ("ctx",)

    def __init__(self, ctx: TraceContext) -> None:
        self.ctx = ctx


def _scope_owned_trace_context(scope: dict[str, Any]) -> TraceContext | None:
    """Read TraceContext strictly from an outer TraceContextMiddleware on this scope."""
    scope_state = scope.get("state")
    if isinstance(scope_state, dict):
        owned = scope_state.get("_lab_trace_context_owned")
        if isinstance(owned, _OwnedTraceContext) and isinstance(owned.ctx, TraceContext):
            return owned.ctx
    return None


def _is_valid_hex(value: str, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    if value == ("0" * length):
        return False
    return all(ch in _LOWER_HEX for ch in value)


def parse_traceparent(value: str) -> TraceParent:
    """Parse a strict bounded version-00 W3C traceparent header value.

    Accepts only exact 55-character lowercase ``00-<32hex>-<16hex>-<00|01>``
    with nonzero trace_id and span_id. Leading/trailing whitespace, uppercase,
    extra fields, or unsupported versions/flags raise ``TraceContextError``
    without echoing the input value.
    """
    if not isinstance(value, str) or len(value) != 55:
        raise TraceContextError("invalid traceparent length")
    if value[:3] != "00-" or value[35] != "-" or value[52] != "-":
        raise TraceContextError("invalid traceparent format")
    flags = value[53:55]
    if flags not in ("00", "01"):
        raise TraceContextError("unsupported traceparent flags")
    trace_id = value[3:35]
    span_id = value[36:52]
    if not _is_valid_hex(trace_id, 32) or not _is_valid_hex(span_id, 16):
        raise TraceContextError("invalid traceparent hex identifiers")
    return TraceParent(trace_id=trace_id, span_id=span_id, sampled=(flags == "01"))


def _new_random_hex(nbytes: int) -> str:
    zero = "0" * (nbytes * 2)
    for _ in range(4):
        candidate = secrets.token_hex(nbytes)
        if candidate != zero:
            return candidate
    raise RuntimeError("trace entropy unavailable")


def new_request_id() -> str:
    """Generate a fresh cryptographic 32-character lowercase hex request ID."""
    return f"req_{_new_random_hex(16)}"


def new_trace_id() -> str:
    """Generate a fresh cryptographic 32-character lowercase hex trace ID."""
    return _new_random_hex(16)


def new_span_id() -> str:
    """Generate a fresh cryptographic 16-character lowercase hex span ID."""
    return _new_random_hex(8)


def _extract_incoming_parent(raw_headers: list[tuple[bytes, bytes]] | Any) -> tuple[TraceParent | None, str]:
    found_value: bytes | None = None
    if not isinstance(raw_headers, (list, tuple)):
        return None, "rejected"
    for item in raw_headers:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        raw_name, raw_val = item
        if not isinstance(raw_name, (bytes, bytearray)) or not isinstance(raw_val, (bytes, bytearray)):
            continue
        if bytes(raw_name).lower() == b"traceparent":
            if found_value is not None:
                # Duplicate traceparent headers are ambiguous and rejected.
                return None, "rejected"
            found_value = bytes(raw_val)

    if found_value is None:
        return None, "absent"
    if len(found_value) != 55:
        return None, "rejected"
    try:
        decoded = found_value.decode("ascii")
        return parse_traceparent(decoded), "accepted"
    except (UnicodeDecodeError, TraceContextError):
        return None, "rejected"


def resolve_request_trace_context(
    raw_headers: list[tuple[bytes, bytes]] | Any,
    *,
    trust_remote_traceparent: bool = False,
) -> TraceContext:
    """Create a fresh request TraceContext from ASGI headers and trust policy.

    - ``request_id`` and ``span_id`` are always freshly generated locally.
    - When ``trust_remote_traceparent`` is ``False`` (default), any incoming
      ``traceparent`` is ignored (``ingress="ignored_untrusted"`` if present,
      ``"absent"`` if absent) and a fresh ``trace_id`` is generated.
    - When ``trust_remote_traceparent`` is ``True``, a single valid version-00
      ``traceparent`` links ``trace_id``, ``parent_span_id``, and ``sampled``
      (``ingress="accepted"``); a missing or malformed header never rejects the
      HTTP request and instead starts a fresh ``trace_id`` with ``ingress`` set
      to ``"absent"`` or ``"rejected"``.
    """
    request_id = new_request_id()
    span_id = new_span_id()

    if not trust_remote_traceparent:
        has_remote = False
        if isinstance(raw_headers, (list, tuple)):
            for item in raw_headers:
                if (
                    isinstance(item, (list, tuple))
                    and len(item) == 2
                    and isinstance(item[0], (bytes, bytearray))
                    and bytes(item[0]).lower() == b"traceparent"
                ):
                    has_remote = True
                    break
        return TraceContext(
            schema_version=TRACE_SCHEMA_VERSION,
            request_id=request_id,
            trace_id=new_trace_id(),
            span_id=span_id,
            parent_span_id=None,
            sampled=False,
            ingress="ignored_untrusted" if has_remote else "absent",
        )

    parent, ingress = _extract_incoming_parent(raw_headers)
    if parent is None:
        return TraceContext(
            schema_version=TRACE_SCHEMA_VERSION,
            request_id=request_id,
            trace_id=new_trace_id(),
            span_id=span_id,
            parent_span_id=None,
            sampled=False,
            ingress=ingress,
        )

    return TraceContext(
        schema_version=TRACE_SCHEMA_VERSION,
        request_id=request_id,
        trace_id=parent.trace_id,
        span_id=span_id,
        parent_span_id=parent.span_id,
        sampled=parent.sampled,
        ingress="accepted",
    )


def _sanitize_request_headers(
    raw_headers: list[tuple[bytes, bytes]] | Any,
    trace_ctx: TraceContext,
) -> list[tuple[bytes, bytes]]:
    cleaned: list[tuple[bytes, bytes]] = []
    if isinstance(raw_headers, (list, tuple)):
        for item in raw_headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            name, val = item
            if not isinstance(name, (bytes, bytearray)) or not isinstance(val, (bytes, bytearray)):
                continue
            lower = bytes(name).lower()
            if lower in _PROPAGATION_HEADERS or lower == b"x-request-id":
                continue
            cleaned.append((bytes(name), bytes(val)))
    cleaned.append((b"traceparent", trace_ctx.traceparent.encode("ascii")))
    cleaned.append((b"x-request-id", trace_ctx.request_id.encode("ascii")))
    return cleaned


def _with_response_correlation_headers(
    raw_headers: list[tuple[bytes, bytes]] | Any,
    trace_ctx: TraceContext,
) -> list[tuple[bytes, bytes]]:
    filtered: list[tuple[bytes, bytes]] = []
    if isinstance(raw_headers, (list, tuple)):
        for item in raw_headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            name, val = item
            if not isinstance(name, (bytes, bytearray)) or not isinstance(val, (bytes, bytearray)):
                continue
            lower = bytes(name).lower()
            if lower in (b"traceparent", b"x-request-id"):
                continue
            filtered.append((bytes(name), bytes(val)))
    filtered.append((b"x-request-id", trace_ctx.request_id.encode("ascii")))
    filtered.append((b"traceparent", trace_ctx.traceparent.encode("ascii")))
    return filtered


class TraceContextMiddleware:
    """Pure ASGI middleware establishing bounded request trace correlation."""

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        trust_remote_traceparent: bool = False,
    ) -> None:
        self.app = app
        self.trust_remote_traceparent = bool(trust_remote_traceparent)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Any],
        send: Callable[..., Any],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Re-use already-established request TraceContext if an outer
        # TraceContextMiddleware (such as FastAPI's outermost build_middleware_stack
        # wrapper) already initialized this scope.
        existing_ctx = _scope_owned_trace_context(scope)
        if existing_ctx is not None:
            trace_ctx = existing_ctx
            child_scope = scope
        else:
            raw_headers = scope.get("headers", ())
            trace_ctx = resolve_request_trace_context(
                raw_headers,
                trust_remote_traceparent=self.trust_remote_traceparent,
            )

            # Copy scope and state so caller-owned dictionaries are never mutated.
            caller_state = scope.get("state")
            child_state = dict(caller_state) if isinstance(caller_state, dict) else {}
            child_state["_lab_trace_context_owned"] = _OwnedTraceContext(trace_ctx)
            child_state["trace_context"] = trace_ctx
            child_state["request_id"] = trace_ctx.request_id
            child_state["trace_id"] = trace_ctx.trace_id
            child_state["span_id"] = trace_ctx.span_id
            child_state["parent_span_id"] = trace_ctx.parent_span_id

            child_scope = dict(scope)
            child_scope["state"] = child_state
            child_scope["headers"] = _sanitize_request_headers(raw_headers, trace_ctx)

        async def send_with_correlation(message: dict[str, Any]) -> None:
            if isinstance(message, dict) and message.get("type") == "http.response.start":
                msg_copy = dict(message)
                msg_copy["headers"] = _with_response_correlation_headers(
                    message.get("headers", ()),
                    trace_ctx,
                )
                await send(msg_copy)
                return
            await send(message)

        token = _CURRENT_TRACE_CONTEXT.set(trace_ctx)
        try:
            await self.app(child_scope, receive, send_with_correlation)
        finally:
            _CURRENT_TRACE_CONTEXT.reset(token)
