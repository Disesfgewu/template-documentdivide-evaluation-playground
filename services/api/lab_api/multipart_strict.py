"""Strict, bounded multipart/form-data parser for PDF upload (SPEC 8, 16.4).

The previous implementation fed the whole body to ``email``'s parser, which
copies the payload several times (parse, decode, slice) and accepted
ambiguous input. This parser takes one already size-bounded body, finds part
boundaries with ``bytes.find`` and slices the PDF once.

Accepted shape, nothing else:

    --BOUNDARY CRLF
    Content-Disposition: form-data; name="file"; filename="x.pdf" CRLF
    Content-Type: application/pdf CRLF
    CRLF
    <bytes> CRLF
    --BOUNDARY CRLF ... --BOUNDARY-- [CRLF]

* no preamble, at most one trailing CRLF after the closing boundary;
* at most ``MAX_PARTS`` parts, each header block at most ``MAX_HEADER_BYTES``;
* no folded or duplicate headers, ASCII/UTF-8 only;
* exactly one PDF part (``file``/``pdf``/``document``, or any part declared
  ``application/pdf``) and at most one ``logical_id`` text part;
* any other part is rejected rather than ignored, so a request cannot carry
  data the server silently discards;
* ``Content-Transfer-Encoding`` other than 7bit/8bit/binary is rejected
  (the old parser decoded base64, which hid the true payload size).
"""
from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from typing import Optional

MAX_PARTS = 4
MAX_HEADER_BYTES = 8192
MAX_HEADERS = 8
MAX_CONTENT_TYPE_CHARS = 512
MAX_LOGICAL_ID_BYTES = 512
PDF_FIELD_NAMES = ("file", "pdf", "document")
IDENTITY_ENCODINGS = ("7bit", "8bit", "binary")
BOUNDARY_CHARS = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'()+_,-./:=?")


class MultipartError(Exception):
    """A malformed or unsupported multipart upload; carries the HTTP mapping."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedUpload:
    pdf: bytes
    logical_id: Optional[str]


def _bad(message: str) -> MultipartError:
    return MultipartError(400, "invalid_request", message)


def boundary_from_content_type(content_type: str) -> bytes:
    if len(content_type) > MAX_CONTENT_TYPE_CHARS:
        raise _bad("Content-Type header too long")
    try:
        content_type.encode("ascii")
    except UnicodeEncodeError:
        raise _bad("invalid characters in Content-Type header") from None
    msg = Message()
    msg["Content-Type"] = content_type
    if msg.get_content_type() != "multipart/form-data":
        raise MultipartError(415, "unsupported_media_type", "Content-Type must be multipart/form-data")
    boundaries = [v for k, v in msg.get_params() if k.lower() == "boundary"]
    if len(boundaries) != 1 or not isinstance(boundaries[0], str):
        raise _bad("multipart Content-Type needs exactly one boundary")
    boundary = boundaries[0]
    if not (1 <= len(boundary) <= 70) or any(c not in BOUNDARY_CHARS for c in boundary):
        raise _bad("invalid multipart boundary")
    return boundary.encode("ascii")


def _parse_headers(block: bytes) -> dict[str, str]:
    try:
        text = block.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad("part headers are not valid UTF-8") from None
    lines = text.split("\r\n")
    if len(lines) > MAX_HEADERS:
        raise _bad("too many part headers")
    headers: dict[str, str] = {}
    for line in lines:
        if not line or line[0] in " \t":
            raise _bad("malformed or folded part header")
        raw_name, sep, value = line.partition(":")
        name = raw_name.lower()
        # RFC 9110 5.1: no whitespace between the field name and the colon.
        if not sep or not name or name != name.strip() or any(c in name for c in " \t"):
            raise _bad("malformed part header")
        if name in headers:
            raise _bad(f"duplicate part header {name}")
        headers[name] = value.strip()
    return headers


def _disposition(value: str) -> tuple[str, Optional[str]]:
    msg = Message()
    msg["Content-Disposition"] = value
    if msg.get_content_disposition() != "form-data":
        raise _bad("part must use Content-Disposition: form-data")
    name = msg.get_param("name", header="content-disposition")
    filename = msg.get_filename()
    if not isinstance(name, str) or not name:
        raise _bad("part has no field name")
    if filename is not None and not isinstance(filename, str):
        raise _bad("invalid part filename")
    return name, filename


def parse_upload(content_type: str, body: "bytes | bytearray", max_pdf_bytes: int) -> ParsedUpload:
    boundary = boundary_from_content_type(content_type)
    dash = b"--" + boundary
    opening = dash + b"\r\n"
    if body[: len(opening)] != opening:
        raise _bad("multipart body must start with the boundary")
    delimiter = b"\r\n" + dash
    view = memoryview(body)
    pos = len(opening)
    parts: list[tuple[dict[str, str], int, int]] = []
    while True:
        header_end = body.find(b"\r\n\r\n", pos, pos + MAX_HEADER_BYTES + 4)
        if header_end < 0:
            raise _bad("malformed or oversized part headers")
        headers = _parse_headers(bytes(view[pos:header_end]))
        start = header_end + 4
        end = body.find(delimiter, start)
        if end < 0:
            raise _bad("multipart body is not terminated")
        parts.append((headers, start, end))
        after = end + len(delimiter)
        tail = bytes(view[after : after + 2])
        if tail == b"--":
            if bytes(view[after + 2 :]) not in (b"", b"\r\n"):
                raise _bad("unexpected data after the closing boundary")
            break
        if tail != b"\r\n":
            raise _bad("malformed boundary line")
        if len(parts) >= MAX_PARTS:
            raise _bad("too many multipart parts")
        pos = after + 2

    pdf_span: Optional[tuple[int, int]] = None
    logical_id: Optional[str] = None
    for headers, start, end in parts:
        if "content-disposition" not in headers:
            raise _bad("part has no Content-Disposition")
        name, filename = _disposition(headers["content-disposition"])
        encoding = headers.get("content-transfer-encoding", "binary").lower()
        if encoding not in IDENTITY_ENCODINGS:
            raise _bad("unsupported Content-Transfer-Encoding")
        part_type = headers.get("content-type", "text/plain").split(";")[0].strip().lower()

        if name == "logical_id" and part_type != "application/pdf":
            if filename is not None or part_type != "text/plain":
                raise _bad("logical_id must be a plain text field")
            if logical_id is not None:
                raise _bad("duplicate logical_id")
            if end - start > MAX_LOGICAL_ID_BYTES:
                raise _bad("logical_id too long")
            try:
                logical_id = bytes(view[start:end]).decode("utf-8").strip()
            except UnicodeDecodeError:
                raise _bad("logical_id is not valid UTF-8") from None
        elif name in PDF_FIELD_NAMES or part_type == "application/pdf":
            is_pdf = part_type == "application/pdf" or (
                part_type == "application/octet-stream" and bool(filename and filename.lower().endswith(".pdf"))
            )
            if not is_pdf:
                raise MultipartError(
                    415, "unsupported_media_type", f"unsupported media type '{part_type}' for PDF upload"
                )
            if pdf_span is not None:
                raise _bad("multiple PDF parts in multipart request")
            if end - start > max_pdf_bytes:
                raise MultipartError(413, "payload_too_large", "PDF file exceeds size limit")
            pdf_span = (start, end)
        else:
            raise _bad("unexpected multipart part")

    if pdf_span is None:
        raise _bad("multipart body does not contain a PDF file")
    return ParsedUpload(pdf=bytes(view[pdf_span[0] : pdf_span[1]]), logical_id=logical_id)
