"""Bounded structured-output execution; injected transport, no provider retry.

Raw inputs, outputs and validated values are restricted data. They are excluded
from repr; callers must persist them in access-controlled artifacts, not logs.
"""
from dataclasses import dataclass, field
import hashlib
import math
import time
from typing import Callable, Generic, Literal, TypeVar

from pydantic import ValidationError

from .base import Contract
from .method import StrictJSONError, strict_loads

T = TypeVar("T", bound=Contract)
MAX_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 64


def _positive_int(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"invalid {name}")


def _revision(value: str) -> None:
    if (type(value) is not str or not value.strip() or len(value) > 128
            or value.strip().lower() in {"unknown", "latest"}):
        raise ValueError("explicit revision required")


@dataclass(frozen=True)
class StructuredRequest:
    operation_id: str
    model_revision: str
    prompt_revision: str
    schema_revision: str
    input_text: str = field(repr=False)
    max_output_tokens: int
    max_input_bytes: int = MAX_BYTES
    max_output_bytes: int = MAX_BYTES
    allow_repair: bool = True

    def __post_init__(self) -> None:
        for value in (self.operation_id, self.model_revision,
                      self.prompt_revision, self.schema_revision):
            _revision(value)
        _positive_int(self.max_output_tokens, 2**31 - 1, "max_output_tokens")
        _positive_int(self.max_input_bytes, MAX_BYTES, "max_input_bytes")
        _positive_int(self.max_output_bytes, MAX_BYTES, "max_output_bytes")
        if type(self.allow_repair) is not bool:
            raise ValueError("invalid allow_repair")
        if type(self.input_text) is not str or not self.input_text.strip():
            raise ValueError("nonempty input text required")
        if len(self.input_text) > self.max_input_bytes:
            raise ValueError("input too large")
        try:
            size = len(self.input_text.encode("utf-8"))
        except UnicodeError:
            raise ValueError("invalid input encoding") from None
        if size > self.max_input_bytes:
            raise ValueError("input too large")


@dataclass(frozen=True)
class Validation(Generic[T]):
    value: T | None = field(repr=False)
    code: str | None


def _contract(model: type[T]) -> None:
    if (not isinstance(model, type) or not issubclass(model, Contract)
            or model.model_config.get("extra") != "forbid"):
        raise ValueError("model must be a LAB Contract class forbidding extras")


def _bounded_depth(text: str) -> bool:
    # Scan before decoding a nested JSON tree; braces inside strings do not
    # count. JSON syntax itself is validated by strict_loads afterwards.
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif char in "]}":
            depth -= 1
    return True


def validate_output(raw: bytes, model: type[T], *, max_bytes: int = MAX_BYTES) -> Validation[T]:
    """Strict UTF-8/JSON and contract validation with payload-free error codes.

    No markdown extraction, key repair, coercion or model-generated code runs.
    Unknown fields follow the merged Contract's extra=forbid policy. Schema
    validity is structural, not evidence verification or permission to freeze.
    """
    _positive_int(max_bytes, MAX_BYTES, "max_bytes")
    _contract(model)
    if type(raw) is not bytes:
        return Validation(None, "invalid_output_type")
    if len(raw) > max_bytes:
        return Validation(None, "output_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return Validation(None, "invalid_encoding")
    if not _bounded_depth(text):
        return Validation(None, "invalid_json")
    try:
        strict_loads(text)
    except StrictJSONError:
        return Validation(None, "invalid_json")
    try:
        value = model.model_validate_json(raw, strict=True)
    except ValidationError:
        return Validation(None, "schema_invalid")
    except Exception:
        return Validation(None, "validator_failed")
    return Validation(value, None)


@dataclass(frozen=True)
class Invocation:
    request: StructuredRequest
    attempt_number: int
    # Repair material is untrusted DATA, deliberately separate from input_text.
    repair_output: bytes | None = field(default=None, repr=False)
    repair_code: str | None = None


@dataclass(frozen=True)
class Attempt:
    number: int
    code: str | None
    output_size: int | None
    output_sha256: str | None
    raw_output: bytes | None = field(repr=False)


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    status: Literal["succeeded", "needs_review", "blocked", "failed", "deadline_exceeded", "cancelled"]
    code: str | None
    attempts: tuple[Attempt, ...]
    value: T | None = field(default=None, repr=False)


def execute_structured(
    request: StructuredRequest,
    model: type[T],
    *,
    transport: Callable[[Invocation], bytes],
    admit: Callable[[Invocation], bool],
    release_unsent: Callable[[Invocation], None],
    cancelled: Callable[[], bool],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> StructuredResult[T]:
    """Generate once; optionally repair JSON/schema once with fresh admission.

    Admission must atomically check cancellation/deadline and reserve the full
    next call's cost. Only exact True permits dispatch; failures deny it. The
    release_unsent must idempotently release an admitted but unsent call; a
    release failure is reported as blocked and requires reconciliation. The
    transport must enforce its own network/CPU/response limits and account for
    each actual attempt; this synchronous boundary cannot interrupt it. Never
    configure transport retries underneath this executor. A returned result
    after deadline is retained for restricted audit but cannot be published.
    """
    if not isinstance(request, StructuredRequest):
        raise ValueError("StructuredRequest required")
    _contract(model)
    if not callable(release_unsent):
        raise ValueError("release_unsent callback required")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("finite monotonic deadline required")
    attempts: list[Attempt] = []
    previous: bytes | None = None
    previous_code: str | None = None

    def result(status, code, value=None):
        return StructuredResult(status, code, tuple(attempts), value)

    def cancellation_result():
        try:
            requested = cancelled()
        except Exception:
            return result("blocked", "cancellation_check_failed")
        if type(requested) is not bool:
            return result("blocked", "cancellation_check_failed")
        if requested:
            return result("cancelled", "cancelled")
        return None

    for number in range(1, 3 if request.allow_repair else 2):
        stopped = cancellation_result()
        if stopped is not None:
            return stopped
        if clock() >= deadline:
            return result("deadline_exceeded", "deadline_exceeded")
        if previous is not None and (len(request.input_text.encode("utf-8"))
                                     + len(previous) > request.max_input_bytes):
            return result("needs_review", "repair_input_too_large")
        invocation = Invocation(request, number, previous, previous_code)
        try:
            allowed = admit(invocation)
        except Exception:
            return result("blocked", "admission_failed")
        if allowed is not True:
            return result("blocked", "admission_denied")
        # A successful admission owns a reservation until transport is invoked.
        # Release it on every pre-dispatch exit, including callback failure.
        try:
            stopped = cancellation_result()
            if stopped is None and clock() >= deadline:
                stopped = result("deadline_exceeded", "deadline_exceeded")
        except Exception:
            stopped = result("blocked", "clock_check_failed")
        if stopped is not None:
            try:
                release_unsent(invocation)
            except Exception:
                return result("blocked", "reservation_release_failed")
            return stopped
        try:
            raw = transport(invocation)
        except Exception:
            attempts.append(Attempt(number, "transport_failed", None, None, None))
            return result("failed", "transport_failed")
        validation = validate_output(raw, model, max_bytes=request.max_output_bytes)
        retain = type(raw) is bytes and len(raw) <= request.max_output_bytes
        attempts.append(Attempt(
            number, validation.code,
            len(raw) if type(raw) is bytes else None,
            hashlib.sha256(raw).hexdigest() if retain else None,
            raw if retain else None,
        ))
        stopped = cancellation_result()
        if stopped is not None:
            return stopped
        if clock() >= deadline:
            return result("deadline_exceeded", "late_output")
        if validation.code is None:
            return result("succeeded", None, validation.value)
        # Only JSON/schema errors are repairable. Encoding/type/size failures
        # cannot be safely supplied as bounded UTF-8 repair material.
        if validation.code not in {"invalid_json", "schema_invalid"}:
            return result("needs_review", validation.code)
        previous, previous_code = raw, validation.code
    return result("needs_review", previous_code)
