"""Run pinned Unstructured (``fast`` only) in a resource-bounded child and normalize it.

The child is the only place the parser library runs. The frames given to the normalizer
come from the real page geometry (``inspect_geometry``), never from a constant. This is
resource containment, not a filesystem or network sandbox. ``hi_res`` and MinerU are not
supported here (blocked: no model/GPU environment).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Callable

from lab_contracts.parse import ModelRevision, PageMapEntry, ParseManifest, ParseRequest, RenderSettings

from .isolation import InspectionError, ProcessLimits, _run_child, inspect_geometry
from .normalize import ADAPTER_REVISION, DisplayFrame, NormalizedBatch, UnstructuredParser, UnstructuredProfile

__all__ = ["RUNNER_LIMITS", "run_unstructured_fast"]

_CHILD = Path(__file__).with_name("_unstructured_child.py")
_VERSION = "0.27.36"
# Library imports are large; the geometry child's 512 MiB cannot hold them.
RUNNER_LIMITS = ProcessLimits(memory_bytes=4 * 1024 * 1024 * 1024, cpu_seconds=60,
                              wall_seconds=120, output_bytes=16 * 1024 * 1024)


def _read_regular(directory: str, name: str, limit: int) -> bytes:
    try:
        descriptor = os.open(Path(directory, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise InspectionError("invalid_child_result")
            return stream.read(limit + 1)
    except OSError:
        raise InspectionError("invalid_child_result") from None


def run_unstructured_fast(
    data: bytes,
    *,
    python: str | None = None,
    limits: ProcessLimits = RUNNER_LIMITS,
    cancelled: Callable[[], bool] | None = None,
    document_id: str,
    budget_ref: str,
    operation_id: str,
    _child: Path = _CHILD,
) -> NormalizedBatch:
    """Parse every page of ``data``; raise ``InspectionError`` (fixed code) on failure.

    ``python`` must be an interpreter that has ``unstructured==0.27.36`` (a trusted,
    operator-chosen venv); it defaults to the current interpreter.
    """
    if sys.platform != "linux":
        raise InspectionError("unsupported_platform")
    cancelled = cancelled or (lambda: False)
    # L0 geometry first: it validates the PDF and attests each page's display frame.
    geometry = inspect_geometry(data, cancelled=cancelled)
    sha = geometry.sha256
    page_indices = [p.page_index for p in geometry.pages]
    frames = {p.page_index: DisplayFrame(coordinate_frame="rotated_cropbox_top_left",
                                         width=p.width_pt, height=p.height_pt,
                                         transform_ref=f"l0:{sha[:16]}:p{p.page_index}")
              for p in geometry.pages}
    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + limits.wall_seconds
    try:
        with tempfile.TemporaryDirectory(prefix="lab-unstructured-") as directory:
            source = Path(directory, "source.pdf")
            with source.open("xb") as stream:
                os.chmod(source, 0o600)
                stream.write(data)
            _run_child([python or sys.executable, "-I", str(_child), str(limits.memory_bytes),
                        str(limits.cpu_seconds), str(limits.output_bytes)],
                       directory, deadline, cancelled,
                       env={"LANG": "C.UTF-8", "HOME": directory, "TMPDIR": directory})
            raw = _read_regular(directory, "elements.json", limits.output_bytes)
            meta_raw = _read_regular(directory, "meta.json", 4096)
    except InspectionError:
        raise
    except OSError:
        raise InspectionError("inspection_io_failed") from None
    if cancelled():
        raise InspectionError("cancelled")
    try:
        meta = json.loads(meta_raw)
    except ValueError:
        raise InspectionError("invalid_child_result") from None
    if type(meta) is not dict or meta.get("unstructured") != _VERSION or len(raw) > limits.output_bytes:
        raise InspectionError("version_mismatch")
    request = ParseRequest(document_artifact_ref="sha256:" + sha, page_indices=page_indices,
                           profile_revision="unstructured-oss-fast",
                           budget_ref=budget_ref, operation_id=operation_id)
    manifest = ParseManifest(
        parser_name="unstructured", package_or_service_version=_VERSION,
        adapter_revision=ADAPTER_REVISION, output_schema="unstructured.elements-0.27.36",
        model_revisions=[ModelRevision(name="core", revision=_VERSION, checksum=None)],
        requested_profile="unstructured-oss-fast", effective_profile="unstructured-oss-fast",
        fallback_reason=None, ocr_languages=["en"], page_selection=page_indices,
        page_index_map=[PageMapEntry(parser_page=i + 1, page_index=i) for i in page_indices],
        render_settings=RenderSettings(dpi=150, max_megapixels=16.0), postprocess_flags=[],
        endpoint_deployment_id=None, started_at=started, finished_at=datetime.now(timezone.utc),
        cost_usd=0.0, warnings=[], reproducible=True)
    return UnstructuredParser().normalize(
        raw, request, manifest, UnstructuredProfile(strategy="fast"),
        document_id=document_id, document_sha256=sha, frames=frames,
        document_page_count=len(page_indices))
