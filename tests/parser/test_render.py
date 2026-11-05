"""Unit tests for L0 PDF rendering and pure-Python PNG encoding (SPEC 19.2 L0, 19.3.8)."""

from __future__ import annotations

import json
import struct
import unittest
from unittest import mock
import zlib

from fixtures import registry_check as rc
from lab_parser.render import (
    PNG_SIGNATURE,
    RenderedAsset,
    encode_png_rgb,
    render_region,
)


def _decode_minimal_png_rgb(png: bytes) -> tuple[int, int, bytes, bytes]:
    """Minimal PNG reader verifying signature, IHDR, CRCs, and decompressed scanlines.

    Returns:
        Tuple of (width, height, raw_decompressed_scanlines, packed_rgb_bytes).
    """
    if not png.startswith(PNG_SIGNATURE):
        raise ValueError("invalid PNG signature")

    offset = len(PNG_SIGNATURE)
    width = height = None
    idat_parts: list[bytes] = []
    seen_iend = False

    while offset < len(png):
        if offset + 8 > len(png):
            raise ValueError("truncated PNG chunk header")
        length, chunk_type = struct.unpack(">I4s", png[offset : offset + 8])
        offset += 8
        if offset + length + 4 > len(png):
            raise ValueError("truncated PNG chunk body")
        chunk_data = png[offset : offset + length]
        offset += length
        (expected_crc,) = struct.unpack(">I", png[offset : offset + 4])
        offset += 4

        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError(f"CRC mismatch in chunk {chunk_type!r}")

        if chunk_type == b"IHDR":
            w, h, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (bit_depth, color_type, comp, filt, interlace) != (8, 2, 0, 0, 0):
                raise ValueError(
                    f"unexpected IHDR format: {(bit_depth, color_type, comp, filt, interlace)}"
                )
            width, height = w, h
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            seen_iend = True
            break

    if width is None or height is None or not seen_iend:
        raise ValueError("missing IHDR or IEND chunk")

    raw = zlib.decompress(b"".join(idat_parts))
    expected_raw_len = height * (1 + width * 3)
    if len(raw) != expected_raw_len:
        raise ValueError(
            f"decompressed length mismatch: expected {expected_raw_len}, got {len(raw)}"
        )

    rgb = bytearray(width * height * 3)
    row_stride = 1 + width * 3
    for y in range(height):
        row_start = y * row_stride
        filter_byte = raw[row_start]
        if filter_byte != 0:
            raise ValueError(f"unsupported filter byte {filter_byte} at row {y}")
        dst_start = y * width * 3
        rgb[dst_start : dst_start + width * 3] = raw[
            row_start + 1 : row_start + row_stride
        ]

    return width, height, raw, bytes(rgb)


class TestRender(unittest.TestCase):
    """Tests for encode_png_rgb and render_region."""

    @classmethod
    def setUpClass(cls) -> None:
        entry = next(e for e in rc.load()["fixtures"] if e["id"] == "F02")
        item = entry["items"][0]
        cls.f02_data: bytes = rc.item_bytes(item)
        cls.f02_pages: list[dict] = json.loads(
            (rc.ROOT / entry["expected"]).read_text(encoding="utf-8")
        )["items"][item["id"]]["pages"]

    def test_encode_png_rgb_2x1_roundtrip(self) -> None:
        """2x1 RGB image encodes to valid PNG and decodes back identically."""
        src_rgb = bytes([255, 0, 128, 10, 20, 30])
        png = encode_png_rgb(2, 1, src_rgb)

        self.assertTrue(png.startswith(PNG_SIGNATURE))
        width, height, raw, decoded_rgb = _decode_minimal_png_rgb(png)
        self.assertEqual((width, height), (2, 1))
        self.assertEqual(len(raw), 1 * (1 + 2 * 3))
        self.assertEqual(raw[0], 0)
        self.assertEqual(decoded_rgb, src_rgb)

    def test_f02_page0_marker_l_and_full_page_dimensions(self) -> None:
        """F02 page 0 MARK-0-L crop is >80% light gray (~217+-6) and full page matches 150 DPI size."""
        page0 = self.f02_pages[0]
        marker = next(m for m in page0["markers"] if m["text"] == "MARK-0-L")
        bbox = tuple(marker["bbox"])

        asset = render_region(self.f02_data, 0, bbox, dpi=72)
        self.assertIsInstance(asset, RenderedAsset)
        self.assertEqual(asset.dpi, 72)
        self.assertEqual(asset.page_index, 0)
        self.assertEqual(asset.region, bbox)

        w, h, raw, rgb = _decode_minimal_png_rgb(asset.png)
        self.assertEqual((w, h), (asset.width_px, asset.height_px))
        self.assertEqual(len(raw), h * (1 + w * 3))

        total_pixels = w * h
        self.assertGreater(total_pixels, 0)
        gray_pixels = 0
        for i in range(0, len(rgb), 3):
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if abs(r - 217) <= 6 and abs(g - 217) <= 6 and abs(b - 217) <= 6:
                gray_pixels += 1

        gray_ratio = gray_pixels / total_pixels
        self.assertGreater(
            gray_ratio,
            0.8,
            f"expected light-gray ratio > 0.8 for MARK-0-L, got {gray_ratio:.4f}",
        )

        # Full page render at default 150 DPI
        full = render_region(self.f02_data, 0, (0.0, 0.0, 1.0, 1.0), dpi=150)
        expected_w = round(612 * 150 / 72)
        expected_h = round(792 * 150 / 72)
        self.assertLessEqual(abs(full.width_px - expected_w), 1)
        self.assertLessEqual(abs(full.height_px - expected_h), 1)
        self.assertEqual(full.transform["renderer"], "pdfium")
        self.assertTrue(full.transform["renderer_version"])
        self.assertEqual(full.transform["dpi"], 150)
        self.assertEqual(full.transform["page_size_pt"], (612.0, 792.0))
        self.assertEqual(full.transform["crop_pt"], (0.0, 0.0, 0.0, 0.0))

    def test_reproducible_sha256(self) -> None:
        """Repeated renders with identical inputs produce identical PNG bytes and sha256."""
        a1 = render_region(self.f02_data, 0, (0.1, 0.1, 0.5, 0.4), dpi=150)
        a2 = render_region(self.f02_data, 0, (0.1, 0.1, 0.5, 0.4), dpi=150)
        self.assertEqual(a1.sha256, a2.sha256)
        self.assertEqual(a1.png, a2.png)
        self.assertEqual(a1.transform, a2.transform)

    def test_validation_errors_and_pixel_limit(self) -> None:
        """Invalid DPI, invalid regions, and oversized pixel dimensions raise ValueError."""
        # Invalid DPI
        for bad_dpi in (71, 400, 0, -100):
            with self.subTest(bad_dpi=bad_dpi):
                with self.assertRaises(ValueError):
                    render_region(self.f02_data, 0, (0, 0, 1, 1), dpi=bad_dpi)

        # Invalid regions
        bad_regions = [
            (-0.01, 0.0, 1.0, 1.0),
            (0.0, 0.0, 1.01, 1.0),
            (0.5, 0.0, 0.5, 1.0),
            (0.6, 0.0, 0.4, 1.0),
            (0.0, 0.7, 1.0, 0.3),
            (0.0, 0.5, 1.0, 0.5),
            (0.0, -0.1, 1.0, 1.0),
            (0.0, 0.0, 1.0, 1.2),
        ]
        for reg in bad_regions:
            with self.subTest(region=reg):
                with self.assertRaises(ValueError):
                    render_region(self.f02_data, 0, reg, dpi=150)

        # Pre-render pixel_limit check (longest side > 4096 or total > 16_000_000 without calling page.render)
        fake_page = mock.MagicMock()
        fake_page.get_size.return_value = (3000.0, 3000.0)
        fake_doc = mock.MagicMock()
        fake_doc.__len__.return_value = 1
        fake_doc.__getitem__.return_value = fake_page

        with mock.patch("lab_parser.render.pypdfium2.PdfDocument", return_value=fake_doc):
            with self.assertRaisesRegex(ValueError, "pixel_limit"):
                render_region(b"%PDF-fake", 0, (0.0, 0.0, 1.0, 1.0), dpi=300)
            fake_page.render.assert_not_called()

        # Also test longest side > 4096 when total pixels <= 16_000_000
        fake_page.get_size.return_value = (1200.0, 200.0)  # At 300 DPI: 5000 x 833 px
        with mock.patch("lab_parser.render.pypdfium2.PdfDocument", return_value=fake_doc):
            with self.assertRaisesRegex(ValueError, "pixel_limit"):
                render_region(b"%PDF-fake", 0, (0.0, 0.0, 1.0, 1.0), dpi=300)
            fake_page.render.assert_not_called()


if __name__ == "__main__":
    unittest.main()
