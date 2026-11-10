"""Run pinned Unstructured (``fast`` only) in a resource-bounded child and normalize it.

The child is the only place the parser library runs. The frames given to the normalizer
come from the real page geometry (``inspect_geometry``), never from a constant. This is
resource containment, not a filesystem or network sandbox. ``hi_res`` and MinerU are not
supported here (blocked: no model/GPU environment).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Callable, Sequence

from lab_contracts.parse import ModelRevision, PageMapEntry, PageStatus, ParseManifest, ParseRequest, RenderSettings

from .frame_transform import display_transform
from .isolation import Inspection, InspectionError, ProcessLimits, _run_child, inspect_geometry
from .normalize import ADAPTER_REVISION, DisplayFrame, NormalizedBatch, UnstructuredParser, UnstructuredProfile

__all__ = ["HIDDEN_TEXT_DIAGNOSTIC", "LOST_TEXT_DIAGNOSTIC", "UNPARSEABLE_CONTENT_DIAGNOSTIC", "RUNNER_LIMITS", "run_unstructured_fast"]

LOST_TEXT_DIAGNOSTIC = "text_present_but_no_element"
HIDDEN_TEXT_DIAGNOSTIC = "text_not_visible"
UNPARSEABLE_CONTENT_DIAGNOSTIC = "content_unparseable"
UNSAFE_CODES = (HIDDEN_TEXT_DIAGNOSTIC, UNPARSEABLE_CONTENT_DIAGNOSTIC)

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
    geometry: Inspection | None = None,
    page_indices: Sequence[int] | None = None,
    document_id: str,
    budget_ref: str,
    operation_id: str,
    _child: Path = _CHILD,
) -> NormalizedBatch:
    """Parse every page of ``data`` (or only ``page_indices``); raise ``InspectionError`` on failure.

    ``geometry`` may be a previous ``inspect_geometry`` result for exactly these bytes (a batch
    runner inspects once, not once per batch); ``page_indices`` is a strictly ascending list of
    original 0-based pages, parsed from a sub-document built inside the child. The result's page
    statuses, evidence and observations carry original physical indices either way.

    ``python`` must be an interpreter that has ``unstructured==0.27.36`` (a trusted,
    operator-chosen venv); it defaults to the current interpreter.
    """
    if sys.platform != "linux":
        raise InspectionError("unsupported_platform")
    cancelled = cancelled or (lambda: False)
    # L0 geometry first: it validates the PDF and attests each page's display frame.
    if geometry is None:
        geometry = inspect_geometry(data, cancelled=cancelled)
    elif type(geometry) is not Inspection or geometry.sha256 != hashlib.sha256(data).hexdigest():
        raise InspectionError("invalid_input")  # geometry of some other document
    sha = geometry.sha256
    total_pages = len(geometry.pages)
    subset = page_indices is not None
    page_indices = list(range(total_pages)) if page_indices is None else list(page_indices)
    if (not page_indices or any(type(i) is not int or not 0 <= i < total_pages for i in page_indices)
            or any(b <= a for a, b in zip(page_indices, page_indices[1:]))):
        raise InspectionError("invalid_input")
    pages_by_index = {p.page_index: p for p in geometry.pages}
    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + limits.wall_seconds
    try:
        with tempfile.TemporaryDirectory(prefix="lab-unstructured-") as directory:
            source = Path(directory, "source.pdf")
            with source.open("xb") as stream:
                os.chmod(source, 0o600)
                stream.write(data)
            if subset:
                Path(directory, "pages.json").write_bytes(json.dumps(page_indices).encode("ascii"))
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
    lost = meta.get("lost_text_pages", [])
    if type(lost) is not list or any(type(i) is not int or i < 0 for i in lost):
        raise InspectionError("invalid_child_result")
    if any(i >= len(page_indices) for i in lost):
        raise InspectionError("invalid_child_result")
    unsafe_raw = meta.get("unsafe_text_pages", [])
    if (type(unsafe_raw) is not list
            or any(type(e) is not list or len(e) != 2 or type(e[0]) is not int or not 0 <= e[0] < len(page_indices)
                   or e[1] not in UNSAFE_CODES for e in unsafe_raw)):
        raise InspectionError("invalid_child_result")
    unsafe = {page_indices[e[0]]: e[1] for e in unsafe_raw}   # original page index -> diagnostic
    attestable = meta.get("attestable_pages")
    if (type(attestable) is not list or len(attestable) != len(page_indices)
            or any(type(v) is not bool for v in attestable)):
        # The child and the geometry inspector must agree on the page count.
        raise InspectionError("invalid_child_result")
    # Unstructured fast takes coordinates from pdfminer: /Rotate is applied to its layout, /CropBox is not.
    # A page is attested only if the child says its text units are intact (rotation 0 or 180) AND the
    # transform computed from its boxes reproduces the L0 display size. Everything else keeps its text
    # but claims no region (and so no evidence unit).
    geometry_info = meta.get("page_geometry")
    if type(geometry_info) is not list:
        geometry_info = []   # a child that reports no page boxes attests nothing: no frame, text only
    frames = {}
    for k, (page_index, ok) in enumerate(zip(page_indices, attestable)):
        if not ok or page_index in unsafe:
            continue   # a page with hidden or unanalysable text claims no region, so it has no evidence
        page = pages_by_index[page_index]
        transform_ref = f"l0:{sha[:16]}:p{page_index}"
        try:
            info = geometry_info[k]
            ft = display_transform(info["mediabox"], info["cropbox"], info["rotation"])
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if abs(ft.width - page.width_pt) > 0.01 or abs(ft.height - page.height_pt) > 0.01:
            continue  # the child's boxes do not describe the page the inspector measured
        frames[page_index] = DisplayFrame(
            coordinate_frame="rotated_cropbox_top_left", width=page.width_pt, height=page.height_pt,
            transform_ref=transform_ref + ":t", layout_width=ft.layout_width, layout_height=ft.layout_height,
            origin_x=ft.origin_x, origin_y=ft.origin_y)
    request = ParseRequest(document_artifact_ref="sha256:" + sha, page_indices=page_indices,
                           profile_revision="unstructured-oss-fast",
                           budget_ref=budget_ref, operation_id=operation_id)
    manifest = ParseManifest(
        parser_name="unstructured", package_or_service_version=_VERSION,
        adapter_revision=ADAPTER_REVISION, output_schema="unstructured.elements-0.27.36",
        model_revisions=[ModelRevision(name="core", revision=_VERSION, checksum=None)],
        requested_profile="unstructured-oss-fast", effective_profile="unstructured-oss-fast",
        fallback_reason=None, ocr_languages=["en"], page_selection=page_indices,
        page_index_map=[PageMapEntry(parser_page=k + 1, page_index=i) for k, i in enumerate(page_indices)],
        render_settings=RenderSettings(dpi=150, max_megapixels=16.0), postprocess_flags=[],
        endpoint_deployment_id=None, started_at=started, finished_at=datetime.now(timezone.utc),
        cost_usd=0.0, warnings=[], reproducible=True)
    batch = UnstructuredParser().normalize(
        raw, request, manifest, UnstructuredProfile(strategy="fast"),
        document_id=document_id, document_sha256=sha, frames=frames,
        document_page_count=total_pages)
    if unsafe and batch.quarantine_reason is None:
        # The whole page is quarantined (PM-DECISION, gap G8): needs_review with a specific diagnostic, no
        # evidence (no frame above) and no observation, so no hidden run can reach a consumer. The raw
        # output artifact still holds the full element text for audit.
        pages = [PageStatus(page_index=p.page_index, status="needs_review",
                            diagnostics=list(dict.fromkeys([*p.diagnostics, unsafe[p.page_index]])))
                 if p.page_index in unsafe else p for p in batch.result.pages]
        batch = replace(batch, result=batch.result.model_copy(update={"pages": pages}),
                        observations=tuple(o for o in batch.observations if o.page_index not in unsafe))
    if lost and batch.quarantine_reason is None:
        # pypdf sees text on these pages but this run produced no element at all. Reported as information
        # only: whether it is a total loss (e.g. the spaCy model could not be installed) is decided over the
        # whole document by the batch layer, because a single page can legitimately disagree with pdfminer.
        suspect = {page_indices[i] for i in lost}
        pages = [PageStatus(page_index=p.page_index, status="needs_review",
                            diagnostics=list(dict.fromkeys([*p.diagnostics, LOST_TEXT_DIAGNOSTIC])))
                 if p.page_index in suspect else p for p in batch.result.pages]
        batch = replace(batch, result=batch.result.model_copy(update={"pages": pages}))
    return batch
