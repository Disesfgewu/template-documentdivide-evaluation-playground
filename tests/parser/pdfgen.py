"""In-memory PDF generator and PNG decoding helpers for parser tests (SPEC 4.1 coordinates, T05, 19.2 L0, 19.3.8 limits)."""

from __future__ import annotations

import io
import struct
from typing import Any
import zlib

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    NameObject,
    NumberObject,
    RectangleObject,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def build_pdf(
    pages: list[dict[str, Any]],
    labels: list[dict[str, Any]] | None = None,
) -> bytes:
    """Build a deterministic PDF byte stream inline with pypdf."""
    writer = PdfWriter()
    for page_dict in pages:
        mediabox = page_dict["mediabox"]
        x0, y0, x1, y1 = mediabox
        page = writer.add_blank_page(width=float(x1 - x0), height=float(y1 - y0))
        page[NameObject("/MediaBox")] = RectangleObject(mediabox)

        if "cropbox" in page_dict and page_dict["cropbox"] is not None:
            page[NameObject("/CropBox")] = RectangleObject(page_dict["cropbox"])

        if "rotate" in page_dict and page_dict["rotate"] is not None:
            page[NameObject("/Rotate")] = NumberObject(page_dict["rotate"])

        content_parts: list[str] = []
        rects = page_dict.get("rects")
        if rects:
            content_parts.append("0 g\n")
            for rx0, ry0, rx1, ry1 in rects:
                content_parts.append(f"{rx0} {ry0} {rx1 - rx0} {ry1 - ry0} re f\n")
        content = "".join(content_parts)

        stream = DecodedStreamObject()
        stream.set_data(content.encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)

    if labels is not None:
        for lbl in labels:
            kwargs: dict[str, Any] = {
                "page_index_from": lbl["from"],
                "page_index_to": lbl["to"],
            }
            if lbl.get("style") is not None:
                kwargs["style"] = lbl["style"]
            if lbl.get("prefix") is not None:
                kwargs["prefix"] = lbl["prefix"]
            if lbl.get("start") is not None:
                kwargs["start"] = lbl["start"]
            writer.set_page_label(**kwargs)

    bio = io.BytesIO()
    writer.write(bio)
    return bio.getvalue()


def decode_png_rgb(png: bytes) -> tuple[int, int, bytes]:
    """Parse PNG chunks, verify CRC, read IHDR (bit depth 8, color type 2), and return (width, height, rgb)."""
    if not png.startswith(PNG_SIGNATURE):
        raise AssertionError("invalid PNG signature")

    offset = len(PNG_SIGNATURE)
    width: int | None = None
    height: int | None = None
    idat_parts: list[bytes] = []

    while offset < len(png):
        if offset + 8 > len(png):
            raise AssertionError("truncated PNG chunk header")
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        chunk_type = png[offset + 4 : offset + 8]
        offset += 8

        if offset + length + 4 > len(png):
            raise AssertionError(f"truncated chunk data for {chunk_type!r}")
        chunk_data = png[offset : offset + length]
        expected_crc = struct.unpack(">I", png[offset + length : offset + length + 4])[0]
        offset += length + 4

        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise AssertionError(
                f"CRC mismatch on chunk {chunk_type!r}: expected {expected_crc:#x}, got {actual_crc:#x}"
            )

        if chunk_type == b"IHDR":
            if len(chunk_data) != 13:
                raise AssertionError(f"invalid IHDR chunk length: {len(chunk_data)}")
            w, h, bit_depth, color_type, _comp, _filt, _inter = struct.unpack(">IIBBBBB", chunk_data)
            if bit_depth != 8 or color_type != 2:
                raise AssertionError(
                    f"require bit depth 8 and color type 2, got bit_depth={bit_depth}, color_type={color_type}"
                )
            width = w
            height = h
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if width is None or height is None:
        raise AssertionError("missing IHDR chunk in PNG")

    decompressed = zlib.decompress(b"".join(idat_parts))
    stride = width * 3 + 1
    expected_len = stride * height
    if len(decompressed) != expected_len:
        raise AssertionError(
            f"decompressed length mismatch: expected {expected_len}, got {len(decompressed)}"
        )

    rgb = bytearray(width * height * 3)
    row_bytes = width * 3
    for y in range(height):
        dst_row = y * stride
        filter_byte = decompressed[dst_row]
        if filter_byte != 0:
            raise AssertionError(f"scanline filter byte at row {y} is {filter_byte}, require 0")
        src_start = dst_row + 1
        dst_start = y * row_bytes
        rgb[dst_start : dst_start + row_bytes] = decompressed[src_start : src_start + row_bytes]

    return width, height, bytes(rgb)


def mean_luma(
    width: int,
    height: int,
    rgb: bytes,
    box_px: tuple[int, int, int, int],
) -> float:
    """Compute mean luma (r+g+b)/3 over integer pixel bounds box_px=(x0,y0,x1,y1) (x1/y1 exclusive)."""
    x0, y0, x1, y1 = int(box_px[0]), int(box_px[1]), int(box_px[2]), int(box_px[3])
    count = (x1 - x0) * (y1 - y0)
    if count <= 0:
        return 0.0
    row_stride = width * 3
    total_val = 0
    for y in range(y0, y1):
        start = y * row_stride + x0 * 3
        end = y * row_stride + x1 * 3
        total_val += sum(rgb[start:end])
    return total_val / (3.0 * count)
