"""Linux process resource boundary for trusted PDF geometry inspection.

This is resource containment, not a filesystem or network security sandbox.
Only the fixed companion is executable through the public API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable

_CHILD = Path(__file__).with_name("_isolation_child.py")
_MAX_BYTES = 512 * 1024 * 1024


class InspectionError(Exception):
    """Fixed diagnostic code, never parser, path or document text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProcessLimits:
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 5
    wall_seconds: float = 10
    output_bytes: int = 1024 * 1024
    max_pdf_bytes: int = _MAX_BYTES
    max_pages: int = 2000

    def __post_init__(self):
        for name, low, high in (
            ("memory_bytes", 128 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
            ("cpu_seconds", 1, 120), ("output_bytes", 1024, 16 * 1024 * 1024),
            ("max_pdf_bytes", 1, _MAX_BYTES), ("max_pages", 1, 2000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise InspectionError("invalid_limits")
        if (type(self.wall_seconds) not in (int, float)
                or not math.isfinite(self.wall_seconds) or not 0 < self.wall_seconds <= 180):
            raise InspectionError("invalid_limits")


@dataclass(frozen=True)
class PageGeometry:
    page_index: int
    page_label: str | None
    rotation: int
    width_pt: float
    height_pt: float


@dataclass(frozen=True)
class Inspection:
    schema_version: str
    sha256: str
    size_bytes: int
    pages: tuple[PageGeometry, ...] = field(repr=False)

    @property
    def page_count(self):
        return len(self.pages)


def _stop(process: subprocess.Popen):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run_child(command: list[str], cwd: str, deadline: float,
               cancelled: Callable[[], bool]) -> None:
    """Private trusted-command harness; public entrypoint fixes the command."""
    process = None
    try:
        if cancelled():
            raise InspectionError("cancelled")
        if time.monotonic() >= deadline:
            raise InspectionError("timeout")
        process = subprocess.Popen(
            command, cwd=cwd, env={"LANG": "C.UTF-8"}, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        while True:
            if cancelled():
                raise InspectionError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InspectionError("timeout")
            # Observe exit without reaping: the zombie pins its PID/PGID until
            # _stop kills remaining group members, avoiding a reuse race.
            status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                code = status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
                break
            time.sleep(min(0.02, remaining))
        if cancelled():
            raise InspectionError("cancelled")
        if time.monotonic() >= deadline:
            raise InspectionError("timeout")
        if code != 0:
            # A signal alone cannot establish which resource was exhausted.
            raise InspectionError("child_failed")
    except OSError:
        raise InspectionError("process_unavailable") from None
    finally:
        if process is not None:
            _stop(process)


def _strict_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError
        out[key] = value
    return out


def _validate(raw: bytes, digest: str, size: int, limits: ProcessLimits) -> Inspection:
    try:
        result = json.loads(raw, object_pairs_hook=_strict_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(result) is not dict:
            raise ValueError
        if set(result) == {"error"}:
            if result["error"] in {"invalid_pdf", "encrypted_pdf", "page_limit_exceeded", "memory_limit"}:
                raise InspectionError(result["error"])
            raise ValueError
        if set(result) != {"schema_version", "sha256", "size_bytes", "pages"}:
            raise ValueError
        if (result["schema_version"] != "isolated-geometry-2" or result["sha256"] != digest
                or type(result["size_bytes"]) is not int or result["size_bytes"] != size):
            raise ValueError
        pages = result["pages"]
        if type(pages) is not list or not 1 <= len(pages) <= limits.max_pages:
            raise ValueError
        checked = []
        for i, page in enumerate(pages):
            if type(page) is not dict or set(page) != {"page_index", "page_label", "rotation", "width_pt", "height_pt"}:
                raise ValueError
            label = page["page_label"]
            if label is not None and (type(label) is not str or len(label) > 64):
                raise ValueError
            if type(page["page_index"]) is not int or page["page_index"] != i:
                raise ValueError
            if type(page["rotation"]) is not int or page["rotation"] not in (0, 90, 180, 270):
                raise ValueError
            for name in ("width_pt", "height_pt"):
                if (type(page[name]) not in (int, float) or not math.isfinite(page[name])
                        or page[name] <= 0):
                    raise ValueError
            checked.append(PageGeometry(**page))
        return Inspection("isolated-geometry-2", digest, size, tuple(checked))
    except InspectionError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise InspectionError("invalid_child_result") from None


def inspect_geometry(data: bytes, limits: ProcessLimits = ProcessLimits(), *,
                     cancelled: Callable[[], bool] | None = None) -> Inspection:
    """Inspect actual PDF bytes in a bounded child; failures publish no result.

    ``cancelled`` must be a fast, nonblocking callback. Document bodies and
    parser diagnostics never leave the private temporary directory. Callers
    own admission, authorization, durable job state and scheduler concurrency.
    """
    if sys.platform != "linux":
        raise InspectionError("unsupported_platform")
    if type(limits) is not ProcessLimits or type(data) is not bytes:
        raise InspectionError("invalid_input")
    if not data or len(data) > limits.max_pdf_bytes:
        raise InspectionError("input_size")
    cancelled = cancelled or (lambda: False)
    if cancelled():
        raise InspectionError("cancelled")
    deadline = time.monotonic() + limits.wall_seconds
    digest = hashlib.sha256(data).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="lab-pdf-inspect-") as directory:
            source = Path(directory, "source.pdf")
            with source.open("xb") as stream:
                os.chmod(source, 0o600)
                stream.write(data)
            _run_child([sys.executable, "-I", str(_CHILD), str(limits.memory_bytes),
                        str(limits.cpu_seconds), str(limits.output_bytes), str(limits.max_pages)],
                       directory, deadline, cancelled)
            descriptor = os.open(Path(directory, "result.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limits.output_bytes:
                    raise InspectionError("invalid_child_result")
                raw = stream.read(limits.output_bytes + 1)
            if cancelled():
                raise InspectionError("cancelled")
            if time.monotonic() >= deadline:
                raise InspectionError("timeout")
            result = _validate(raw, digest, len(data), limits)
            if cancelled():
                raise InspectionError("cancelled")
            if time.monotonic() >= deadline:
                raise InspectionError("timeout")
            return result
    except InspectionError:
        raise
    except OSError:
        raise InspectionError("inspection_io_failed") from None
