"""L0 PDF rendering and pure-Python RGB PNG encoding (SPEC 19.2 L0, 19.3.8)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct
from typing import Any
import zlib

import pypdfium2


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MIN_DPI = 72
MAX_DPI = 300
MAX_PIXELS = 16_000_000
MAX_SIDE_PX = 4096


@dataclass(frozen=True)
class RenderedAsset:
    """Immutable L0 rendered image asset with reproducible transform metadata."""

    png: bytes
    sha256: str
    width_px: int
    height_px: int
    dpi: int
    page_index: int
    region: tuple[float, float, float, float]
    transform: dict[str, Any]


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Pack a single PNG chunk with big-endian length and CRC-32."""
    if len(chunk_type) != 4:
        raise ValueError("chunk_type must be 4 bytes")
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def encode_png_rgb(width: int, height: int, rgb: bytes) -> bytes:
    """Encode raw 8-bit RGB bytes into a deterministic PNG image without Pillow.

    Args:
        width: Image width in pixels (> 0).
        height: Image height in pixels (> 0).
        rgb: Tightly packed RGB byte buffer of length width * height * 3.

    Returns:
        PNG file bytes (color_type=2, bit_depth=8, filter=0 per scanline).
    """
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise ValueError(f"invalid width: {width!r}")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise ValueError(f"invalid height: {height!r}")
    if not isinstance(rgb, (bytes, bytearray, memoryview)):
        raise TypeError("rgb must be a bytes-like object")

    rgb_bytes = bytes(rgb)
    row_bytes = width * 3
    expected_len = row_bytes * height
    if len(rgb_bytes) != expected_len:
        raise ValueError(
            f"rgb buffer length mismatch: expected {expected_len}, got {len(rgb_bytes)}"
        )

    raw_scanlines = bytearray((row_bytes + 1) * height)
    dst_stride = row_bytes + 1
    for y in range(height):
        src_start = y * row_bytes
        dst_start = y * dst_stride
        raw_scanlines[dst_start] = 0  # Filter type 0 (None)
        raw_scanlines[dst_start + 1 : dst_start + dst_stride] = rgb_bytes[
            src_start : src_start + row_bytes
        ]

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_scanlines), level=6)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def _validate_dpi(dpi: int) -> int:
    """Validate DPI is an integer within [72, 300]."""
    if isinstance(dpi, bool) or not isinstance(dpi, int):
        raise ValueError(f"dpi must be an int in [{MIN_DPI}, {MAX_DPI}], got {dpi!r}")
    if dpi < MIN_DPI or dpi > MAX_DPI:
        raise ValueError(f"dpi must be in [{MIN_DPI}, {MAX_DPI}], got {dpi}")
    return dpi


def _validate_region(
    region: tuple[float, float, float, float] | list[float],
) -> tuple[float, float, float, float]:
    """Validate normalized top-left SourceRegion bbox [x0, y0, x1, y1] in [0, 1]."""
    if not isinstance(region, (tuple, list)) or len(region) != 4:
        raise ValueError(f"region must be a 4-element tuple/list, got {region!r}")
    coords: list[float] = []
    for v in region:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"region coordinates must be finite numbers, got {v!r}")
        fv = float(v)
        if not math.isfinite(fv):
            raise ValueError(f"region coordinates must be finite numbers, got {v!r}")
        coords.append(fv)

    x0, y0, x1, y1 = coords
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError(
            f"region must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1, got {coords}"
        )
    return (x0, y0, x1, y1)


def _check_pixel_limits(width_px: int, height_px: int) -> None:
    """Enforce SPEC 19.3.8 pixel limits (<= 16 MP total, <= 4096 px longest side)."""
    if width_px <= 0 or height_px <= 0:
        raise ValueError("rendered region dimensions must be positive")
    if (
        width_px > MAX_SIDE_PX
        or height_px > MAX_SIDE_PX
        or (width_px * height_px) > MAX_PIXELS
    ):
        raise ValueError("pixel_limit")


def _extract_rgb_from_bitmap(bm: Any) -> bytes:
    """Convert pypdfium2 bitmap buffer (BGR, BGRA, or grayscale) to tight RGB bytes."""
    width = int(bm.width)
    height = int(bm.height)
    stride = int(bm.stride)
    n_channels = int(bm.n_channels)
    buf = bytes(bm.buffer)

    if len(buf) < stride * height:
        raise ValueError(
            f"bitmap buffer too small: expected at least {stride * height}, got {len(buf)}"
        )

    out = bytearray(width * height * 3)
    row_out_bytes = width * 3

    if n_channels == 3:
        for y in range(height):
            row = buf[y * stride : y * stride + width * 3]
            dst = y * row_out_bytes
            out[dst : dst + row_out_bytes : 3] = row[2::3]  # R
            out[dst + 1 : dst + row_out_bytes : 3] = row[1::3]  # G
            out[dst + 2 : dst + row_out_bytes : 3] = row[0::3]  # B
    elif n_channels == 4:
        for y in range(height):
            row = buf[y * stride : y * stride + width * 4]
            dst = y * row_out_bytes
            out[dst : dst + row_out_bytes : 3] = row[2::4]  # R
            out[dst + 1 : dst + row_out_bytes : 3] = row[1::4]  # G
            out[dst + 2 : dst + row_out_bytes : 3] = row[0::4]  # B
    elif n_channels == 1:
        for y in range(height):
            row = buf[y * stride : y * stride + width]
            dst = y * row_out_bytes
            out[dst : dst + row_out_bytes : 3] = row
            out[dst + 1 : dst + row_out_bytes : 3] = row
            out[dst + 2 : dst + row_out_bytes : 3] = row
    else:
        raise ValueError(f"unsupported bitmap channel count: {n_channels}")

    return bytes(out)


def _get_renderer_version() -> str:
    """Return a reproducible string representation of the pypdfium2/PDFium version."""
    version_mod = getattr(pypdfium2, "version", None)
    if version_mod is not None:
        info = getattr(version_mod, "PYPDFIUM_INFO", None)
        if info is not None:
            return str(info)
        ver = getattr(version_mod, "V_PYPDFIUM2", None)
        if ver is not None:
            return str(ver)
    return str(getattr(pypdfium2, "__version__", "unknown"))


def render_region(
    data: bytes,
    page_index: int,
    region: tuple[float, float, float, float] | list[float] = (0.0, 0.0, 1.0, 1.0),
    dpi: int = 150,
) -> RenderedAsset:
    """Render a full PDF page or normalized SourceRegion to an L0 PNG asset.

    Args:
        data: Raw PDF bytes.
        page_index: 0-based physical page index.
        region: Normalized (x0, y0, x1, y1) in [0, 1] with top-left origin on the
            rotated visible page.
        dpi: Render resolution in dots per inch (72..300, default 150).

    Returns:
        RenderedAsset with PNG bytes, SHA-256 hex digest, pixel dimensions, and
        reproducible transform metadata.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) == 0:
        raise ValueError("data must be non-empty PDF bytes")
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise ValueError(f"page_index must be a non-negative integer, got {page_index!r}")

    validated_dpi = _validate_dpi(dpi)
    x0, y0, x1, y1 = _validate_region(region)
    norm_region = (x0, y0, x1, y1)

    pdf = pypdfium2.PdfDocument(bytes(data))
    try:
        page_count = len(pdf)
        if page_index >= page_count:
            raise ValueError(
                f"page_index {page_index} out of range for document with {page_count} pages"
            )

        page = pdf[page_index]
        try:
            page_w_pt, page_h_pt = page.get_size()
            page_w_pt = float(page_w_pt)
            page_h_pt = float(page_h_pt)
            if page_w_pt <= 0.0 or page_h_pt <= 0.0:
                raise ValueError(f"invalid page size in pt: {(page_w_pt, page_h_pt)}")

            scale = validated_dpi / 72.0
            region_w_pt = (x1 - x0) * page_w_pt
            region_h_pt = (y1 - y0) * page_h_pt

            est_width_px = max(1, int(round(region_w_pt * scale)))
            est_height_px = max(1, int(round(region_h_pt * scale)))
            _check_pixel_limits(est_width_px, est_height_px)

            left_pt = x0 * page_w_pt
            bottom_pt = (1.0 - y1) * page_h_pt
            right_pt = (1.0 - x1) * page_w_pt
            top_pt = y0 * page_h_pt
            crop_pt = (left_pt, bottom_pt, right_pt, top_pt)

            bm = page.render(scale=scale, crop=crop_pt)
            try:
                width_px = int(bm.width)
                height_px = int(bm.height)
                _check_pixel_limits(width_px, height_px)
                rgb = _extract_rgb_from_bitmap(bm)
            finally:
                close_bm = getattr(bm, "close", None)
                if callable(close_bm):
                    close_bm()
        finally:
            close_page = getattr(page, "close", None)
            if callable(close_page):
                close_page()
    finally:
        close_pdf = getattr(pdf, "close", None)
        if callable(close_pdf):
            close_pdf()

    png_bytes = encode_png_rgb(width_px, height_px, rgb)
    sha256_hex = hashlib.sha256(png_bytes).hexdigest()
    transform: dict[str, Any] = {
        "renderer": "pdfium",
        "renderer_version": _get_renderer_version(),
        "dpi": validated_dpi,
        "scale": scale,
        "page_index": page_index,
        "page_size_pt": (page_w_pt, page_h_pt),
        "region": norm_region,
        "crop_pt": crop_pt,
        "width_px": width_px,
        "height_px": height_px,
    }

    return RenderedAsset(
        png=png_bytes,
        sha256=sha256_hex,
        width_px=width_px,
        height_px=height_px,
        dpi=validated_dpi,
        page_index=page_index,
        region=norm_region,
        transform=transform,
    )
