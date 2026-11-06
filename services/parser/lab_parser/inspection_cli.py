"""Bounded local PDF inspection; no service configuration or persistent state."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from dataclasses import asdict
import io
import json
import math
import os
import stat

from .importer import Limits, inspect_pdf
from .pages import extract_pages

FORMAT = "lab-pdf-inspection-1"


class InvocationError(Exception):
    """An invocation error whose original text must not be published."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvocationError()


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError()
    return number


def _emit(report: dict) -> None:
    print(json.dumps({"format": FORMAT, **report}, allow_nan=False, sort_keys=True))


def _error(reason: str) -> int:
    _emit({"outcome": "error", "reason": reason})
    return 2


def _read_regular(path: str, max_bytes: int) -> bytes:
    # O_NONBLOCK prevents opening an attacker-supplied FIFO from hanging before
    # fstat can reject it. The object checked is the same descriptor we read.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InvocationError()
        stream = os.fdopen(fd, "rb")
        fd = None  # ownership transferred to stream
        with stream:
            return stream.read(max_bytes + 1)
    finally:
        if fd is not None:
            os.close(fd)


def inspect(argv: list[str]) -> int:
    parser = _Parser(prog="python -m lab_parser inspect", description="Inspect a local regular PDF without creating a document or job.")
    parser.add_argument("path")
    parser.add_argument("--max-bytes", type=_positive, default=Limits().max_bytes)
    parser.add_argument("--max-pages", type=_positive, default=Limits().max_pages)
    try:
        args = parser.parse_args(argv)
        limits = Limits(max_bytes=args.max_bytes, max_pages=args.max_pages)
    except (InvocationError, ValueError, OverflowError):
        return _error("invalid_arguments")
    try:
        data = _read_regular(args.path, limits.max_bytes)
    except InvocationError:
        return _error("not_regular_file")
    except (OSError, ValueError, OverflowError):
        return _error("input_unavailable")

    if len(data) > limits.max_bytes:
        # Only a prefix was read. Never mislabel its hash/length as the file's.
        _emit({"outcome": "reject", "reason": "file_too_large", "complete_read": False,
               "bytes_read": len(data), "size_bytes": None, "sha256": None,
               "page_count": None, "pages": [], "warnings_count": 0})
        return 1

    # pypdf diagnostics can contain arbitrary PDF strings. Publish stable reason
    # codes and a count only, never unfiltered warnings or credential-like paths.
    with redirect_stderr(io.StringIO()):
        result = inspect_pdf(data, limits)
        report = {"outcome": result.outcome, "reason": result.reason,
                  "complete_read": True, "bytes_read": len(data),
                  "size_bytes": result.size_bytes, "sha256": result.sha256,
                  "page_count": result.page_count, "warnings_count": len(result.warnings), "pages": []}
        if result.outcome == "accept":
            try:
                pages = [asdict(page) for page in extract_pages(data)]
                for page in pages:
                    for key in ("mediabox", "cropbox", "visible_box"):
                        if not all(math.isfinite(v) for v in page[key]):
                            raise ValueError()
                    if not all(math.isfinite(page[key]) and page[key] > 0
                               for key in ("display_width_pt", "display_height_pt")):
                        raise ValueError()
                report["pages"] = pages
            except Exception:
                report.update(outcome="flag", reason="invalid_page_geometry")
    _emit(report)
    return 0 if report["outcome"] == "accept" else 1
