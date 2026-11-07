"""Bounded ASGI application/pdf request-body reader (SPEC R01, 16.4, 16.6).

Reads and enforces boundaries for streaming PDF request bodies on ASGI/FastAPI
routes:
- Preserves exact raw bytes for downstream validation (e.g. inspect_pdf).
- Enforces byte limits across streamed ASGI chunks without loading excess bytes.
- Validates Content-Type strictly (application/pdf).
- Validates Content-Length (missing, malformed, duplicate, misleading).
- Detects premature client disconnects.
- Avoids leaking header values, body content, or filesystem paths in error messages.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

__all__ = [
    "DEFAULT_MAX_PDF_BYTES",
    "PdfRequestBodyError",
    "PdfRequestBodyReader",
    "read_pdf_body",
    "read_pdf_request_body",
]

# SPEC 16.6 default maximum PDF size: 512 MiB
DEFAULT_MAX_PDF_BYTES = 512 * 1024 * 1024

_CONTENT_TYPE_PDF = b"application/pdf"

# Fixed, sanitized error messages (never interpolate headers, paths, or body).
_MSG_INVALID_SCOPE = "invalid ASGI scope"
_MSG_EXPECTED_HTTP = "expected HTTP ASGI scope"
_MSG_INVALID_RECEIVE = "receive callable must be provided"
_MSG_INVALID_CONTENT_TYPE = "Content-Type must be application/pdf"
_MSG_INVALID_CONTENT_LENGTH = "invalid Content-Length header"
_MSG_FILE_TOO_LARGE = "request body exceeds size limit"
_MSG_CONTENT_LENGTH_MISMATCH = "request body size does not match Content-Length header"
_MSG_CLIENT_DISCONNECT = "client disconnected before request body was fully received"
_MSG_UNEXPECTED_MESSAGE = "unexpected ASGI message type"
_MSG_INVALID_MESSAGE_FORMAT = "invalid ASGI message format"
_MSG_INVALID_MESSAGE_BODY = "invalid ASGI message body"


class PdfRequestBodyError(HTTPException, ValueError):
    """Sanitized boundary error for PDF request-body intake.

    Inherits from both HTTPException (for FastAPI/Starlette handling) and
    ValueError. Error messages and repr never echo raw body content, paths,
    or header values.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def to_error_body(self, request_id: str = "req-1") -> dict[str, Any]:
        """Format an ErrorResponse contract compliant payload (SPEC 16.4)."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": False,
                "request_id": request_id,
                "details": {},
                "retry_after": None,
            }
        }

    def to_response(self, request_id: str = "req-1") -> JSONResponse:
        """Create a Starlette JSONResponse with the contract error structure."""
        return JSONResponse(
            status_code=self.status_code,
            content=self.to_error_body(request_id),
        )

    async def send_response(
        self,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        request_id: str = "req-1",
    ) -> None:
        """Send raw ASGI HTTP response messages for this error."""
        body = json.dumps(self.to_error_body(request_id)).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )

    def __str__(self) -> str:
        return f"{self.status_code}: {self.code}: {self.message}"

    def __repr__(self) -> str:
        return f"PdfRequestBodyError(status_code={self.status_code!r}, code={self.code!r}, message={self.message!r})"


async def read_pdf_request_body(
    scope: Mapping[str, Any] | Any,
    receive: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
) -> bytes:
    """Read and validate raw application/pdf request bytes from ASGI messages.

    Parameters
    ----------
    scope : Mapping[str, Any] or starlette.requests.Request
        ASGI scope mapping, or a Request object providing ``scope`` and ``receive``.
    receive : Callable[[], Awaitable[Mapping[str, Any]]], optional
        ASGI receive callable returning HTTP messages.
    max_bytes : int, optional
        Maximum allowable byte count. Defaults to 512 MiB (SPEC 16.6).

    Returns
    -------
    bytes
        Unaltered raw PDF bytes suitable for ``inspect_pdf``.

    Raises
    ------
    PdfRequestBodyError
        415 invalid_content_type if Content-Type is missing, duplicate, or not application/pdf.
        400 invalid_content_length if Content-Length is malformed, duplicate, or mismatched.
        413 file_too_large if declared or streamed bytes exceed max_bytes.
        400 client_disconnect if client disconnects prematurely.
        400 invalid_request on non-HTTP scope or malformed ASGI messages.
    ValueError
        If max_bytes is not a positive integer or receive is missing/not callable.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")

    # Support Starlette / FastAPI Request instances directly
    if hasattr(scope, "scope") and hasattr(scope, "receive") and receive is None:
        receive = scope.receive
        scope = scope.scope

    if receive is None or not callable(receive):
        raise ValueError(_MSG_INVALID_RECEIVE)

    if not isinstance(scope, Mapping):
        raise PdfRequestBodyError(
            status_code=400,
            code="invalid_request",
            message=_MSG_INVALID_SCOPE,
        )

    if scope.get("type") != "http":
        raise PdfRequestBodyError(
            status_code=400,
            code="invalid_request",
            message=_MSG_EXPECTED_HTTP,
        )

    # 1. Parse and validate headers (bounded, safe)
    content_types: list[bytes] = []
    content_lengths: list[bytes] = []

    raw_headers = scope.get("headers") or ()
    for item in raw_headers:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        raw_name, raw_value = item
        if not isinstance(raw_name, (bytes, str)) or not isinstance(raw_value, (bytes, str)):
            continue
        name = raw_name.lower() if isinstance(raw_name, bytes) else raw_name.encode("latin-1").lower()
        val = raw_value if isinstance(raw_value, bytes) else raw_value.encode("latin-1")
        if name == b"content-type":
            content_types.append(val)
        elif name == b"content-length":
            content_lengths.append(val)

    # Content-Type: strictly application/pdf (allowing optional parameters like charset)
    if not content_types or len(content_types) > 1:
        raise PdfRequestBodyError(
            status_code=415,
            code="invalid_content_type",
            message=_MSG_INVALID_CONTENT_TYPE,
        )

    media_type = content_types[0].split(b";")[0].strip().lower()
    if media_type != _CONTENT_TYPE_PDF:
        raise PdfRequestBodyError(
            status_code=415,
            code="invalid_content_type",
            message=_MSG_INVALID_CONTENT_TYPE,
        )

    # Content-Length: optional (chunked streaming), but if present must be a single non-negative integer
    expected_length: int | None = None
    if len(content_lengths) > 1:
        raise PdfRequestBodyError(
            status_code=400,
            code="invalid_content_length",
            message=_MSG_INVALID_CONTENT_LENGTH,
        )

    if len(content_lengths) == 1:
        raw_cl = content_lengths[0].strip()
        # Non-empty ASCII digits only (no signs, floats, spaces, or commas)
        if not raw_cl or not raw_cl.isdigit():
            raise PdfRequestBodyError(
                status_code=400,
                code="invalid_content_length",
                message=_MSG_INVALID_CONTENT_LENGTH,
            )
        try:
            expected_length = int(raw_cl.decode("ascii"))
        except Exception:
            raise PdfRequestBodyError(
                status_code=400,
                code="invalid_content_length",
                message=_MSG_INVALID_CONTENT_LENGTH,
            )

        if expected_length > max_bytes:
            raise PdfRequestBodyError(
                status_code=413,
                code="file_too_large",
                message=_MSG_FILE_TOO_LARGE,
            )

    # 2. Stream ASGI request body messages
    chunks: list[bytes] = []
    total_bytes = 0

    while True:
        message = await receive()
        if not isinstance(message, Mapping):
            raise PdfRequestBodyError(
                status_code=400,
                code="invalid_request",
                message=_MSG_INVALID_MESSAGE_FORMAT,
            )

        msg_type = message.get("type")
        if msg_type == "http.disconnect":
            raise PdfRequestBodyError(
                status_code=400,
                code="client_disconnect",
                message=_MSG_CLIENT_DISCONNECT,
            )

        if msg_type != "http.request":
            raise PdfRequestBodyError(
                status_code=400,
                code="invalid_request",
                message=_MSG_UNEXPECTED_MESSAGE,
            )

        body = message.get("body", b"")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise PdfRequestBodyError(
                status_code=400,
                code="invalid_request",
                message=_MSG_INVALID_MESSAGE_BODY,
            )

        chunk = bytes(body) if not isinstance(body, bytes) else body
        if chunk:
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise PdfRequestBodyError(
                    status_code=413,
                    code="file_too_large",
                    message=_MSG_FILE_TOO_LARGE,
                )
            if expected_length is not None and total_bytes > expected_length:
                raise PdfRequestBodyError(
                    status_code=400,
                    code="invalid_content_length",
                    message=_MSG_CONTENT_LENGTH_MISMATCH,
                )
            chunks.append(chunk)

        more_body = bool(message.get("more_body", False))
        if not more_body:
            break

    # If Content-Length was declared, ensure received body matches exactly
    if expected_length is not None and total_bytes != expected_length:
        raise PdfRequestBodyError(
            status_code=400,
            code="invalid_content_length",
            message=_MSG_CONTENT_LENGTH_MISMATCH,
        )

    return b"".join(chunks)


read_pdf_body = read_pdf_request_body


class PdfRequestBodyReader:
    """Reusable bounded ASGI reader for application/pdf request bodies."""

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_PDF_BYTES) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.max_bytes = max_bytes

    async def read(
        self,
        scope: Mapping[str, Any] | Any,
        receive: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    ) -> bytes:
        return await read_pdf_request_body(scope, receive, max_bytes=self.max_bytes)

    async def __call__(
        self,
        scope: Mapping[str, Any] | Any,
        receive: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    ) -> bytes:
        return await self.read(scope, receive)
