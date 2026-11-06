"""Image-bearing VisionReader boundary; observations remain unverified (#6).

Only the renderer's RGB8/noninterlaced/filter-0 PNG profile is admitted. There
is deliberately no text-client fallback, retry loop or implicit budget grant.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Annotated, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .render import PNG_SIGNATURE, RenderedAsset

Label = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")]
Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class VisionError(ValueError):
    """Value-free diagnostic with optional restricted late-response material."""
    def __init__(self, code: str, restricted_response: bytes | None = None):
        super().__init__(code)
        self.restricted_response = restricted_response


def _fail(code: str) -> None:
    raise VisionError(code)


def _pinned(value: str) -> bool:
    value = value.strip().lower()
    return bool(value) and value not in {
        "unknown", "latest", "main", "master", "head", "unpinned"
    } and not value.endswith(("@latest", "@unknown"))


class _Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class VisionCapability(_Contract):
    adapter_revision: Label
    model_revision: Label
    vision: Literal[True]
    max_images: Annotated[int, Field(ge=1, le=8)]
    max_total_pixels: Annotated[int, Field(ge=1, le=16_000_000)]
    max_output_tokens: Annotated[int, Field(ge=1, le=1_000_000)]


class VisionObservation(_Contract):
    """Model description, never a verified fact or an OCR replacement."""
    image_sha256: Hash
    kind: Literal["visible_text", "visual_description"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=16384, strip_whitespace=True)] = Field(repr=False)
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None
    verification: Literal["unverified"]


class VisionResponse(_Contract):
    operation_id: Label
    model_revision: Label
    prompt_revision: Label
    image_hashes: Annotated[tuple[Hash, ...], Field(min_length=1, max_length=8)]
    observations: Annotated[tuple[VisionObservation, ...], Field(max_length=256)]
    # Unknown is null, not confirmed zero. Cost/reservation live in the caller.
    input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    provider_request_id: Label | None


@dataclass(frozen=True)
class VisionImage:
    artifact_ref: str
    document_id: str
    document_sha256: str
    page_count: int
    png_sha256: str
    width_px: int
    height_px: int
    page_index: int
    region: tuple[float, float, float, float]
    transform_json: str
    png: bytes = field(repr=False)

    @classmethod
    def from_rendered(cls, asset: RenderedAsset, *, artifact_ref: str,
                      document_id: str, document_sha256: str, page_count: int) -> VisionImage:
        """Snapshot mutable renderer metadata; provenance is caller supplied.

        The caller must authorize the committed artifact and its PDF identity.
        Hash/PNG/geometry are checked again immediately before dispatch.
        """
        try:
            transform = json.dumps(asset.transform, allow_nan=False, sort_keys=True,
                                   separators=(",", ":"))
        except (ValueError, TypeError, RecursionError):
            raise VisionError("invalid_transform") from None
        return cls(artifact_ref, document_id, document_sha256, page_count, asset.sha256,
                   asset.width_px, asset.height_px, asset.page_index,
                   tuple(asset.region), transform, bytes(asset.png))


@dataclass(frozen=True)
class VisionCall:
    operation_id: str
    model_revision: str
    prompt_revision: str
    prompt: str = field(repr=False)
    images: tuple[VisionImage, ...] = field(repr=False)
    max_output_tokens: int


class VisionTransport(Protocol):
    """Implementations must really encode/send images and honor timeout_s.

    The boundary cannot interrupt an arbitrary Python callback. In-flight
    cancel/late results are withheld; the caller still reconciles paid usage.
    """
    def __call__(self, call: VisionCall, timeout_s: float) -> bytes: ...


@dataclass(frozen=True)
class VisionResult:
    call: VisionCall = field(repr=False)
    capability: VisionCapability
    response: VisionResponse = field(repr=False)
    # Restricted audit material, never write it to public logs.
    raw_response: bytes = field(repr=False)
    response_sha256: str
    status: Literal["needs_review"] = "needs_review"


def _integer(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _label(value: object) -> bool:
    if type(value) is not str or not value.strip() or value != value.strip() or len(value) > 128:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _hash(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """Validate the renderer's narrow PNG profile, bounded before inflate."""
    if type(png) is not bytes or len(png) > 64 * 1024 * 1024 or not png.startswith(PNG_SIGNATURE):
        _fail("invalid_png")
    offset = len(PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    # The encoder writes exactly IHDR / IDAT / IEND. Reject other variants.
    for expected in (b"IHDR", b"IDAT", b"IEND"):
        if offset + 12 > len(png):
            _fail("invalid_png")
        size, kind = struct.unpack(">I4s", png[offset:offset + 8])
        end = offset + 8 + size
        if kind != expected or end + 4 > len(png):
            _fail("unsupported_png")
        data = png[offset + 8:end]
        crc = struct.unpack(">I", png[end:end + 4])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            _fail("invalid_png")
        chunks.append((kind, data))
        offset = end + 4
    if offset != len(png) or chunks[2][1] or len(chunks[0][1]) != 13:
        _fail("invalid_png")
    width, height, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", chunks[0][1])
    if (depth, color, comp, filt, inter) != (8, 2, 0, 0, 0):
        _fail("unsupported_png")
    if not (1 <= width <= 4096 and 1 <= height <= 4096 and width * height <= 16_000_000):
        _fail("pixel_limit")
    stride = width * 3 + 1
    expected_bytes = stride * height
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(chunks[1][1], expected_bytes + 1)
        if (len(decoded) != expected_bytes or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail):
            _fail("invalid_png")
    except zlib.error:
        raise VisionError("invalid_png") from None
    if any(decoded[row * stride] != 0 for row in range(height)):
        _fail("unsupported_png")
    return width, height


def _validate_call(call: VisionCall, capability: VisionCapability) -> None:
    if not isinstance(call, VisionCall) or type(call.images) is not tuple:
        _fail("invalid_call")
    if not all(_label(v) for v in (call.operation_id, call.model_revision, call.prompt_revision)):
        _fail("invalid_call")
    if not all(_pinned(v) for v in (call.model_revision, call.prompt_revision,
                                   capability.model_revision, capability.adapter_revision)):
        _fail("unpinned_revision")
    if call.model_revision != capability.model_revision:
        _fail("model_mismatch")
    if (type(call.prompt) is not str or not call.prompt.strip()
            or len(call.prompt.encode("utf-8")) > 65536):
        _fail("invalid_prompt")
    if not _integer(call.max_output_tokens, 1, capability.max_output_tokens):
        _fail("output_token_limit")
    if not 1 <= len(call.images) <= capability.max_images:
        _fail("image_limit")
    total_pixels = 0
    total_bytes = 0
    seen: set[str] = set()
    for image in call.images:
        if not isinstance(image, VisionImage):
            _fail("invalid_image")
        if (not all(_label(v) for v in (image.artifact_ref, image.document_id))
                or not _hash(image.document_sha256) or not _hash(image.png_sha256)
                or not _integer(image.page_count, 1, 2**31 - 1)
                or not _integer(image.page_index, 0, image.page_count - 1)):
            _fail("invalid_provenance")
        if image.png_sha256 in seen:
            _fail("duplicate_image")
        seen.add(image.png_sha256)
        if (type(image.region) is not tuple or len(image.region) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in image.region)):
            _fail("invalid_region")
        x0, y0, x1, y1 = image.region
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            _fail("invalid_region")
        if (type(image.transform_json) is not str or len(image.transform_json.encode("utf-8")) > 16384):
            _fail("invalid_transform")
        try:
            transform = json.loads(image.transform_json, parse_constant=lambda _: _fail("invalid_transform"))
        except (ValueError, RecursionError):
            raise VisionError("invalid_transform") from None
        # JSON numeric overflow can produce infinity even without NaN tokens.
        try:
            json.dumps(transform, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise VisionError("invalid_transform") from None
        # Require renderer's mapping fields to agree; preserve its full snapshot.
        if (not isinstance(transform, dict)
                or not _integer(transform.get("page_index"), 0, image.page_count - 1)
                or not _integer(transform.get("width_px"), 1, 4096)
                or not _integer(transform.get("height_px"), 1, 4096)
                or transform.get("page_index") != image.page_index
                or transform.get("width_px") != image.width_px
                or transform.get("height_px") != image.height_px
                or transform.get("region") != list(image.region)):
            _fail("invalid_transform")
        if transform.get("renderer") != "pdfium" or not _label(transform.get("renderer_version")) or not _pinned(transform["renderer_version"]):
            _fail("invalid_transform")
        dpi, scale = transform.get("dpi"), transform.get("scale")
        page_size, crop = transform.get("page_size_pt"), transform.get("crop_pt")
        if (not _integer(dpi, 72, 300) or type(scale) not in (int, float)
                or not math.isclose(scale, dpi / 72, rel_tol=1e-12)
                or type(page_size) is not list or len(page_size) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in page_size)
                or type(crop) is not list or len(crop) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in crop)):
            _fail("invalid_transform")
        page_width, page_height = page_size
        expected_crop = (x0 * page_width, (1-y1) * page_height,
                         (1-x1) * page_width, y0 * page_height)
        if any(not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9) for a, b in zip(crop, expected_crop)):
            _fail("invalid_transform")
        # Match pinned PdfPage.render: round full extent and each crop edge
        # separately. Rounding ideal cropped size rejects valid thin margins.
        expected_width = math.ceil(page_width * scale) - math.ceil(crop[0] * scale) - math.ceil(crop[2] * scale)
        expected_height = math.ceil(page_height * scale) - math.ceil(crop[1] * scale) - math.ceil(crop[3] * scale)
        if (image.width_px, image.height_px) != (expected_width, expected_height):
            _fail("invalid_transform")
        if type(image.png) is not bytes:
            _fail("invalid_png")
        total_bytes += len(image.png)
        if total_bytes > 64 * 1024 * 1024:
            _fail("image_byte_limit")
        if hashlib.sha256(image.png).hexdigest() != image.png_sha256:
            _fail("image_hash_mismatch")
        width, height = _png_dimensions(image.png)
        if (not _integer(image.width_px, 1, 4096) or not _integer(image.height_px, 1, 4096)
                or (width, height) != (image.width_px, image.height_px)):
            _fail("image_dimensions_mismatch")
        total_pixels += width * height
        if total_pixels > capability.max_total_pixels:
            _fail("pixel_limit")


def read_vision(call: VisionCall, *, capability: VisionCapability,
                transport: VisionTransport, admit: Callable[[VisionCall], bool],
                deadline: float, cancelled: Callable[[], bool] = lambda: False,
                clock: Callable[[], float] = time.monotonic,
                max_response_bytes: int = 1024 * 1024) -> VisionResult:
    """Validate, admit and invoke once. No automatic retry or JSON repair.

    Admission must durably reserve before returning True. External result
    storage/settlement and post-dispatch failures remain the caller's duty.
    ``deadline`` uses the same monotonic clock as ``clock``.
    """
    if (not isinstance(capability, VisionCapability) or capability.vision is not True
            or not callable(transport) or not callable(admit)):
        _fail("vision_capability_required")
    # Revalidate even an unsafe pydantic model_construct/model_copy input.
    try:
        capability = VisionCapability.model_validate(capability.model_dump())
    except ValidationError:
        raise VisionError("invalid_capability") from None
    if (not _finite(deadline)
            or not _integer(max_response_bytes, 1, 16 * 1024 * 1024)):
        _fail("invalid_limits")

    def guard() -> float:
        if cancelled():
            _fail("cancelled")
        remaining = deadline - clock()
        if not _finite(remaining) or remaining <= 0:
            _fail("deadline_exceeded")
        return remaining

    guard()
    try:
        _validate_call(call, capability)
    except VisionError:
        raise
    except (ValueError, TypeError, OverflowError):
        raise VisionError("invalid_call") from None
    guard()
    try:
        allowed = admit(call)
    except Exception:
        raise VisionError("admission_failed") from None
    if allowed is not True:
        _fail("admission_denied")
    remaining = guard()
    try:
        raw = transport(call, remaining)
    except Exception:
        raise VisionError("transport_failed_outcome_unknown") from None
    try:
        guard()
    except VisionError as exc:
        bounded = raw if type(raw) is bytes and len(raw) <= max_response_bytes else None
        raise VisionError(str(exc), bounded) from None
    if type(raw) is not bytes or len(raw) > max_response_bytes:
        _fail("invalid_response_size")
    try:
        response = _decode_response(raw, call)
        guard()
    except VisionError as exc:
        raise VisionError(str(exc), raw) from None
    return VisionResult(call, capability, response, raw, hashlib.sha256(raw).hexdigest())


def _decode_response(raw: bytes, call: VisionCall) -> VisionResponse:
    # Reject duplicate JSON keys and nonstandard NaN rather than silently pick.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("invalid_response")
            result[key] = value
        return result
    try:
        json.loads(raw, object_pairs_hook=pairs,
                             parse_constant=lambda _: _fail("invalid_response"))
        response = VisionResponse.model_validate_json(raw)
    except (ValueError, TypeError, RecursionError):
        raise VisionError("invalid_response") from None
    hashes = tuple(image.png_sha256 for image in call.images)
    if (response.operation_id != call.operation_id or response.model_revision != call.model_revision
            or response.prompt_revision != call.prompt_revision or response.image_hashes != hashes
            or any(o.image_sha256 not in hashes for o in response.observations)):
        _fail("response_binding_mismatch")
    if response.output_tokens is not None and response.output_tokens > call.max_output_tokens:
        _fail("response_token_limit")
    return response
