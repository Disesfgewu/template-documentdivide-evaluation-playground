"""Pinned raw-output adapters. No upstream invocation, file access or network I/O.

The caller retains returned artifacts before publishing any references. Parser
output is L1 data, never verified gold. Missing geometry/assets remain visible.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Literal
import zlib

from jsonschema import Draft202012Validator
from pydantic import Field, ValidationError, model_validator

from lab_contracts.base import Contract
from lab_contracts.benchmark import EvidenceUnit
from lab_contracts.parse import CAPABILITIES, Capability, CapabilityManifest, ParseManifest, ParseRequest, ParseResult, PageStatus
from lab_contracts.source import SourceRegion

MAX_RAW_BYTES = 16 * 1024 * 1024
# Work caps checked before any per-item or whole-payload validation. Without them cost was
# bounded only by MAX_RAW_BYTES (a 16 MB MinerU payload took ~1 min in jsonschema alone).
MAX_ELEMENTS = 200_000   # Unstructured elements (flat plus expanded orig_elements)
MAX_ARTIFACTS = 20_000   # distinct decompressed orig_elements artifacts
MAX_MINERU_PAGES = 2_000
MAX_MINERU_BLOCKS = 20_000
ADAPTER_REVISION = "raw-normalization-1"


_DOCVORTEX_SCHEMA_PATH = Path(__file__).parent / "upstream_schemas" / "docvortex-middle-2.0.json"
_DOCVORTEX_SCHEMA = json.loads(_DOCVORTEX_SCHEMA_PATH.read_text(encoding="utf-8"))
_DOCVORTEX_VALIDATOR = Draft202012Validator(_DOCVORTEX_SCHEMA)


class UnstructuredProfile(Contract):
    version: Literal["0.27.36"] = "0.27.36"
    strategy: Literal["fast", "hi_res", "ocr_only"]
    infer_table_structure: bool = False

    @model_validator(mode="after")
    def _table_strategy(self):
        if self.infer_table_structure and self.strategy != "hi_res":
            raise ValueError("table structure requires OSS hi_res")
        return self


class MinerUProfile(Contract):
    version: Literal["4.1.7"] = "4.1.7"
    docvortex_version: Literal["0.6.13"] = "0.6.13"
    tier: Literal["flash", "basic", "standard", "advanced"]
    # Paid and full-document postprocessing stays disabled in this boundary.
    title_leveling: Literal[False] = False
    cross_page_table_merge: Literal[False] = False


class DisplayFrame(Contract):
    """Attested upstream display frame, from the actual parser invocation.

    Conversion is by this attested frame only: a PixelSpace point is divided by the frame
    width/height after the upstream layout size is checked to equal the frame. This module
    does not compute CropBox or /Rotate itself (``geometry.to_region_space`` is not used
    here), so the caller that builds the frame must derive its size and ``transform_ref``
    from the real page; a rotated page is attested as its swapped (display) size.
    MinerU bboxes are accepted only as already normalized [0, 1] top-left coordinates;
    point or pixel boxes fall outside [0, 1] and are rejected as
    ``geometry_outside_display_frame`` (the item keeps its text, without geometry).

    No default frame: callers must verify CropBox/rotation against L0. Upstream
    normalized/PixelSpace bboxes may only be used if this provenance is known.
    """
    coordinate_frame: Literal["rotated_cropbox_top_left"]
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)
    transform_ref: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True)
class Artifact:
    content: bytes
    # Hashed once: ``ref`` is read for every element, and rehashing a multi-megabyte raw
    # artifact each time made normalization cost grow with elements x artifact size.
    ref: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "ref", "sha256:" + hashlib.sha256(self.content).hexdigest())


@dataclass(frozen=True)
class Observation:
    source_ref: str
    source_pointer: str
    page_index: int | None
    upstream_type: str
    raw_content: str
    text_as_html: str | None
    upstream_image_refs: tuple[str, ...]
    evidence_id: str | None


@dataclass(frozen=True)
class NormalizedBatch:
    manifest: ParseManifest
    capabilities: CapabilityManifest
    result: ParseResult
    artifacts: tuple[Artifact, ...]
    evidence: tuple[EvidenceUnit, ...]
    observations: tuple[Observation, ...]
    quarantine_reason: str | None


def _json(raw: bytes):
    if not isinstance(raw, bytes) or len(raw) > MAX_RAW_BYTES:
        raise ValueError("raw_result_size_limit")

    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate_json_key")
            obj[key] = value
        return obj

    def reject(value):
        raise ValueError("nonfinite_json_number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid_page_index")
    return value


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _inline_text(items):
    texts = []
    for item in items:
        if item.get("type") in {"text", "code_inline", "equation_inline"} and isinstance(item.get("content"), str):
            texts.append(item["content"])
        elif item.get("type") == "hyperlink" and isinstance(item.get("content"), list):
            for sub in item["content"]:
                if sub.get("type") in {"text", "code_inline", "equation_inline"} and isinstance(sub.get("content"), str):
                    texts.append(sub["content"])
    return "".join(texts)


def _bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("missing_or_invalid_geometry")
    if any(not _finite_number(n) for n in value):
        raise ValueError("invalid_geometry_numbers")
    x0, y0, x1, y1 = map(float, value)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError("geometry_outside_display_frame")
    return (x0, y0, x1, y1)


def _kind(upstream_type):
    if upstream_type in {"Table", "TableChunk", "table"}:
        return "table"
    if upstream_type in {"Image", "image"}:
        return "image"
    if upstream_type == "chart":
        return "figure"
    if upstream_type in {"Formula", "equation"}:
        return "formula"
    return "text"


def _capabilities(parser):
    unknown = Capability(state="unknown", limitations=["No real inference/golden acceptance in normalization-only adapter"], fixture_evidence=[])
    unsupported = Capability(state="unsupported", limitations=["No verified cell geometry or interpretation is produced"], fixture_evidence=[])
    values = {name: unknown for name in CAPABILITIES}
    values.update(cell_geometry=unsupported, chart_reading=unsupported,
                  cross_page_table=unsupported, full_document_postprocess=unsupported)
    return CapabilityManifest(parser_name=parser, adapter_revision=ADAPTER_REVISION, **values)


class _Builder:
    def __init__(self, raw, req, manifest, document_id, document_sha256, frames, asset_refs, document_page_count):
        if type(document_page_count) is not int or document_page_count < 1 or any(p >= document_page_count for p in req.page_indices):
            raise ValueError("invalid_document_page_count_or_selection")
        if any(type(p) is not int or not isinstance(f, DisplayFrame) for p, f in frames.items()):
            raise ValueError("invalid_coordinate_frame")
        if manifest.adapter_revision != ADAPTER_REVISION:
            raise ValueError("adapter_revision_mismatch")
        if set(manifest.page_selection) != set(req.page_indices):
            raise ValueError("manifest_request_page_mismatch")
        # Validate identity even if every upstream item later lacks geometry.
        SourceRegion.model_validate_json(json.dumps(dict(document_id=document_id,
            document_sha256=document_sha256, page_index=0, bbox=[0, 0, 1, 1], kind="page", quote=None)))
        self.raw = Artifact(raw)
        self.artifacts = [self.raw]
        self.artifact_bytes = len(raw)  # running total, never re-summed per chunk
        self.artifact_refs = {self.raw.ref}
        self.elements = 0
        self.req, self.manifest = req, manifest
        self.document_id, self.document_sha256 = document_id, document_sha256
        self.frames, self.asset_refs = frames, asset_refs
        self.document_page_count = document_page_count
        self.evidence, self.observations = [], []
        self._evidence_ids: set[str] = set()  # O(1) duplicate check, was a scan of every evidence unit
        self.diagnostics = {p: [] for p in req.page_indices}
        self.seen_pages = set()
        self.reason = None

    def warn(self, page, reason):
        pages = self.diagnostics if page not in self.diagnostics else [page]
        for p in pages:
            if reason not in self.diagnostics[p]:
                self.diagnostics[p].append(reason)

    def add(self, artifact, pointer, page, typ, text, bbox, html=None, images=()):
        eid = None
        if page not in self.diagnostics:
            self.warn(None, "missing_or_unrequested_page")
        else:
            self.seen_pages.add(page)
            try:
                if page not in self.frames:
                    raise ValueError("unattested_coordinate_frame")
                region = _bbox(bbox)
                assets = []
                for image in images:
                    if image not in self.asset_refs:
                        self.warn(page, "unresolved_image_asset")
                    else:
                        ref = self.asset_refs[image]
                        if not isinstance(ref, str) or not ref.strip():
                            raise ValueError("invalid_asset_reference")
                        assets.append(ref)
                frame = self.frames[page]
                # Canonical structured preimage encoding avoids delimiter collision when identifiers contain colons
                identity_payload = [
                    self.document_sha256,
                    self.document_id,
                    page,
                    frame.transform_ref,
                    ADAPTER_REVISION,
                    artifact.ref,
                    pointer,
                ]
                identity_bytes = json.dumps(identity_payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                computed_eid = "e-" + hashlib.sha256(identity_bytes).hexdigest()
                evidence = EvidenceUnit.model_validate_json(json.dumps(dict(
                    evidence_id=computed_eid, regions=[dict(document_id=self.document_id,
                    document_sha256=self.document_sha256, page_index=page,
                    bbox=region, kind=_kind(typ), quote=text if text else None)],
                    raw_content=text, normalized_content=text,
                    extractor_revision=ADAPTER_REVISION, ocr_confidence=None,
                    asset_refs=list(dict.fromkeys(assets)))))
                if computed_eid not in self._evidence_ids:
                    self._evidence_ids.add(computed_eid)
                    self.evidence.append(evidence)
                eid = computed_eid
            except (ValueError, TypeError, ValidationError):
                eid = None
                self.warn(page, "missing_or_invalid_geometry_or_asset_reference")
        self.observations.append(Observation(artifact.ref, pointer, page, typ, text, html, tuple(images), eid))

    def finish(self):
        if self.manifest.fallback_reason:
            self.warn(None, "parser_strategy_fallback")
        for page in self.diagnostics:
            if page not in self.seen_pages:
                self.warn(page, "requested_page_missing_or_blank_unconfirmed")
        pages = [PageStatus(page_index=p,
                 status="failed" if self.reason else "needs_review" if diag else "ok",
                 diagnostics=diag) for p, diag in self.diagnostics.items()]
        manifest_artifact = Artifact(self.manifest.model_dump_json().encode())
        result = ParseResult(operation_id=self.req.operation_id,
            manifest_ref=manifest_artifact.ref, raw_result_refs=list(dict.fromkeys(a.ref for a in self.artifacts)),
            normalized_evidence_refs=[e.evidence_id for e in self.evidence], pages=pages)
        return NormalizedBatch(self.manifest, _capabilities(self.manifest.parser_name), result,
            tuple(self.artifacts + [manifest_artifact]), tuple(self.evidence), tuple(self.observations), self.reason)

    def quarantine(self, reason):
        self.reason = reason
        self.artifacts = [self.raw]
        self.artifact_bytes = len(self.raw.content)
        self.artifact_refs = {self.raw.ref}
        self.evidence.clear()
        self._evidence_ids.clear()
        self.observations.clear()
        self.warn(None, reason)
        return self.finish()


class UnstructuredParser:
    """Consumes OSS element.to_dict JSON, never API enrichment as raw OCR."""
    def normalize(self, raw: bytes, request: ParseRequest, manifest: ParseManifest,
                  profile: UnstructuredProfile, *, document_id: str, document_sha256: str,
                  frames: dict[int, DisplayFrame], document_page_count: int,
                  asset_refs: dict[str, str] | None = None) -> NormalizedBatch:
        b = _Builder(raw, request, manifest, document_id, document_sha256, frames, asset_refs or {}, document_page_count)
        if (manifest.parser_name != "unstructured" or manifest.package_or_service_version != profile.version
                or manifest.output_schema != "unstructured.elements-0.27.36"):
            return b.quarantine("unsupported_unstructured_version_or_schema")
        expected = "unstructured-oss-" + profile.strategy
        if manifest.effective_profile != expected:
            return b.quarantine("effective_strategy_mismatch")
        if manifest.endpoint_deployment_id is not None or manifest.postprocess_flags:
            return b.quarantine("api_or_enrichment_requires_separate_adapter")
        try:
            elements = _json(raw)
            if not isinstance(elements, list):
                raise ValueError("elements_array_required")
            page_map = {e.parser_page: e.page_index for e in manifest.page_index_map}

            def consume(items, artifact, prefix, depth=0):
                if depth > 1 or not isinstance(items, list):
                    raise ValueError("invalid_orig_elements")
                b.elements += len(items)
                if b.elements > MAX_ELEMENTS:
                    raise ValueError("element_count_limit")
                for i, item in enumerate(items):
                    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                        raise ValueError("invalid_element_shape")
                    meta, typ = item.get("metadata", {}), item["type"]
                    if not isinstance(meta, dict) or not isinstance(item.get("text", ""), str):
                        raise ValueError("invalid_element_metadata")
                    html = meta.get("text_as_html")
                    image_path = meta.get("image_path")
                    image_base64 = meta.get("image_base64")
                    if (html is not None and not isinstance(html, str)
                            or image_path is not None and not isinstance(image_path, str)
                            or image_base64 is not None and not isinstance(image_base64, str)):
                        raise ValueError("invalid_element_metadata")
                    pointer = f"{prefix}/{i}"
                    if typ in {"CompositeElement", "TableChunk"}:
                        encoded = meta.get("orig_elements")
                        if encoded is not None and not isinstance(encoded, str):
                            raise ValueError("invalid_element_metadata")
                        if not isinstance(encoded, str):
                            b.warn(None, "chunk_missing_orig_elements")
                            b.observations.append(Observation(artifact.ref, pointer, None, typ,
                                item.get("text", ""), html, (), None))
                            continue
                        compressed = base64.b64decode(encoded, validate=True)
                        decoder = zlib.decompressobj()
                        original = decoder.decompress(compressed, MAX_RAW_BYTES + 1)
                        if len(original) > MAX_RAW_BYTES or not decoder.eof or decoder.unused_data:
                            raise ValueError("orig_elements_decompression_limit_or_corruption")
                        child = Artifact(original)
                        # Identical children are retained once; the byte total is a running
                        # counter so the check stays O(1) per chunk.
                        if child.ref not in b.artifact_refs:
                            if (b.artifact_bytes + len(original) > 2 * MAX_RAW_BYTES
                                    or len(b.artifacts) >= MAX_ARTIFACTS):
                                raise ValueError("total_orig_elements_limit")
                            b.artifact_bytes += len(original)
                            b.artifact_refs.add(child.ref)
                            b.artifacts.append(child)
                        consume(_json(original), child, "", depth + 1)
                        # Derived chunk text never overwrites its original elements.
                        b.observations.append(Observation(artifact.ref, pointer, None, typ,
                            item.get("text", ""), html, (), None))
                        continue
                    parser_page = _integer(meta.get("page_number"))
                    if parser_page == 0:
                        raise ValueError("unstructured_pages_are_one_based")
                    page = page_map.get(parser_page)
                    coords = meta.get("coordinates", {})
                    bbox = None
                    frame = frames.get(page)
                    if isinstance(coords, dict) and frame and coords.get("system") == "PixelSpace":
                        lw, lh = coords.get("layout_width"), coords.get("layout_height")
                        if _finite_number(lw) and _finite_number(lh) and lw == frame.width and lh == frame.height:
                            points = coords.get("points")
                            if isinstance(points, list) and len(points) >= 2:
                                if any(not isinstance(p, list) or len(p) != 2 or any(not _finite_number(v) for v in p) for p in points):
                                    raise ValueError("invalid_coordinates")
                                bbox = [min(p[0] for p in points)/frame.width,
                                        min(p[1] for p in points)/frame.height,
                                        max(p[0] for p in points)/frame.width,
                                        max(p[1] for p in points)/frame.height]
                        else:
                            b.warn(page, "unattested_layout_geometry")
                    images = [image_path] if image_path else []
                    if image_base64:
                        images.append(f"{artifact.ref}#{pointer}/metadata/image_base64")
                    b.add(artifact, pointer, page, typ, item.get("text", ""), bbox,
                          html, images)

            consume(elements, b.raw, "")
        except (ValueError, TypeError, KeyError, RecursionError, zlib.error):
            return b.quarantine("invalid_unstructured_output")
        return b.finish()


class MinerUParser:
    """Consumes pinned Middle JSON, not legacy pdf_info or structured_content."""
    def normalize(self, raw: bytes, request: ParseRequest, manifest: ParseManifest,
                  profile: MinerUProfile, *, document_id: str, document_sha256: str,
                  frames: dict[int, DisplayFrame], document_page_count: int,
                  asset_refs: dict[str, str] | None = None) -> NormalizedBatch:
        b = _Builder(raw, request, manifest, document_id, document_sha256, frames, asset_refs or {}, document_page_count)
        try:
            payload = _json(raw)
            if not isinstance(payload, dict) or payload.get("schema") != "docvortex.middle" or payload.get("schema_version") != "2.0":
                return b.quarantine("unsupported_mineru_schema")
            # Cheap structural counts first: schema validation costs ~0.4 ms per block.
            pages_value = payload.get("pages")
            if isinstance(pages_value, list) and len(pages_value) > MAX_MINERU_PAGES:
                return b.quarantine("mineru_page_count_limit")
            block_total = 0
            for page_value in pages_value if isinstance(pages_value, list) else ():
                blocks_value = page_value.get("blocks") if isinstance(page_value, dict) else None
                block_total += len(blocks_value) if isinstance(blocks_value, list) else 0
            if block_total > MAX_MINERU_BLOCKS:
                return b.quarantine("mineru_block_count_limit")
            if not _DOCVORTEX_VALIDATOR.is_valid(payload):
                return b.quarantine("invalid_mineru_output_schema")
            if (manifest.parser_name != "mineru" or manifest.package_or_service_version != profile.version
                    or manifest.output_schema != "docvortex.middle-2.0"
                    or payload["metadata"].get("producer") != {"name": "mineru", "version": profile.version}):
                return b.quarantine("unsupported_mineru_producer_version")
            if payload["metadata"].get("file_suffix") != "pdf":
                return b.quarantine("unsupported_mineru_file_suffix")
            extensions = payload.get("extensions", {})
            if not isinstance(extensions, dict):
                return b.quarantine("invalid_mineru_output")
            ext = extensions.get("mineru")
            if not isinstance(ext, dict):
                return b.quarantine("invalid_mineru_output")
            if ext.get("tier") != profile.tier or manifest.effective_profile != "mineru-" + profile.tier:
                return b.quarantine("mineru_tier_mismatch")
            if manifest.postprocess_flags:
                return b.quarantine("uncontrolled_mineru_postprocessing")
            dimensions = {}
            if "docvortex_layout" in extensions:
                layout = extensions["docvortex_layout"]
                if not isinstance(layout, dict):
                    return b.quarantine("invalid_mineru_output")
                ver = layout.get("version")
                layout_pages = layout.get("pages", [])
                if ver is not None and type(ver) is not int or not isinstance(layout_pages, list):
                    return b.quarantine("invalid_mineru_output")
                if ver == 1:
                    for lp in layout_pages:
                        if not isinstance(lp, dict):
                            return b.quarantine("invalid_mineru_output")
                        lp_idx = _integer(lp.get("page_idx"))
                        w, h = lp.get("width_pt"), lp.get("height_pt")
                        if not (_finite_number(w) and w > 0 and _finite_number(h) and h > 0) or lp_idx in dimensions:
                            return b.quarantine("invalid_mineru_output")
                        dimensions[lp_idx] = lp
            actual_pages = [p["page_idx"] for p in payload["pages"]]
            if len(actual_pages) != len(set(actual_pages)) or actual_pages != sorted(actual_pages):
                return b.quarantine("invalid_mineru_page_order")
            if any(m.parser_page != m.page_index for m in manifest.page_index_map):
                return b.quarantine("mineru_requires_source_page_indices")
            if actual_pages != request.page_indices:
                return b.quarantine("mineru_page_selection_mismatch")
            if payload["is_full_document"] and (actual_pages != list(range(document_page_count))):
                return b.quarantine("invalid_full_document_page_selection")
            for pi, page in enumerate(payload["pages"]):
                index = page["page_idx"]
                frame = frames.get(index)
                dim = dimensions.get(index, {})
                w, h = dim.get("width_pt"), dim.get("height_pt")
                attested = bool(frame and _finite_number(w) and _finite_number(h) and w == frame.width and h == frame.height)
                b.seen_pages.add(index)  # Explicit blank pages are still covered.
                if not attested:
                    b.warn(index, "unattested_layout_geometry")
                block_indices = [block.get("index") for block in page["blocks"]]
                if any(type(i) is not int for i in block_indices) or block_indices != sorted(set(block_indices)):
                    return b.quarantine("invalid_mineru_block_order")
                for bi, block in enumerate(page["blocks"]):
                    # Preserve visual nested structure as raw, no invented cell boxes.
                    content = block.get("content", "")
                    html, images = None, []
                    if isinstance(content, list):
                        texts = []
                        if block["type"] in {"list", "index"}:
                            b.warn(index, "nested_text_preserved_in_raw_only")
                        if block["type"] in {"image", "chart", "table", "code"}:
                            bodies = [c for c in content if c["type"] in {"image_body", "chart_body", "table_body", "code_body", "algorithm_body"}]
                            if (len(bodies) != 1
                                    or (bodies[0].get("bbox") is not None and bodies[0]["bbox"] != block.get("bbox"))
                                    or (bodies[0].get("index") is not None and bodies[0].get("index") != block.get("index"))):
                                return b.quarantine("invalid_mineru_visual_body")
                        for ci, child in enumerate(content):
                            if child.get("type") in {"text", "code_inline", "equation_inline", "code_body", "chart_body", "image_body"} and isinstance(child.get("content"), str):
                                texts.append(child["content"])
                            elif child.get("type") in {"hyperlink", "algorithm_body", "table_caption", "table_footnote", "image_caption", "image_footnote", "chart_caption", "chart_footnote", "code_caption", "code_footnote"} and isinstance(child.get("content"), list):
                                texts.append(_inline_text(child["content"]))
                            elif child.get("type") == "table_body":
                                html = child.get("content")
                            for key in ("image_path", "image_url"):
                                if child.get(key):
                                    images.append(child[key])
                            if child.get("image_base64"):
                                images.append(f"{b.raw.ref}#/pages/{pi}/blocks/{bi}/content/{ci}/image_base64")
                        text = "".join(texts)
                    else:
                        text = content
                        for key in ("image_path", "image_url"):
                            if block.get(key):
                                images.append(block[key])
                        if block.get("image_base64"):
                            images.append(f"{b.raw.ref}#/pages/{pi}/blocks/{bi}/image_base64")
                    b.add(b.raw, f"/pages/{pi}/blocks/{bi}", index, block["type"], text,
                          block.get("bbox") if attested else None, html, images)
        except (ValueError, TypeError, KeyError, RecursionError):
            return b.quarantine("invalid_mineru_output")
        return b.finish()
