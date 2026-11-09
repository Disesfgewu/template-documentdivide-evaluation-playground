"""Unit tests for L0 PDF rendering and PNG encoding (SPEC 4.1 coordinates, T05, 19.2 L0, 19.3.8 limits)."""

from __future__ import annotations

import hashlib
import types
import unittest

from lab_parser.geometry import to_region_space
from lab_parser import render as r
from lab_parser.render import encode_png_rgb, render_region
from tests.parser.pdfgen import build_pdf, decode_png_rgb, mean_luma


class TestRender(unittest.TestCase):
    def test_encode_png_rgb(self) -> None:
        rgb_data = bytes([255, 0, 0, 0, 0, 255])
        png = encode_png_rgb(2, 1, rgb_data)
        w, h, decoded = decode_png_rgb(png)

        self.assertEqual((w, h), (2, 1))
        self.assertEqual(decoded, rgb_data)
        self.assertTrue(png.startswith(r.PNG_SIGNATURE))
        self.assertEqual(png, encode_png_rgb(2, 1, rgb_data))

        with self.assertRaises(ValueError):
            encode_png_rgb(2, 1, bytes([255, 0, 0]))

        with self.assertRaises(ValueError):
            encode_png_rgb(0, 1, b"")
        with self.assertRaises(ValueError):
            encode_png_rgb(True, 1, rgb_data)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            encode_png_rgb(1, 1, "abc")  # type: ignore[arg-type]

    def test_full_page_size_and_metadata(self) -> None:
        data = build_pdf([{"mediabox": (0, 0, 144, 72)}])

        asset72 = render_region(data, 0, dpi=72)
        self.assertEqual(asset72.width_px, 144)
        self.assertEqual(asset72.height_px, 72)
        self.assertEqual(asset72.sha256, hashlib.sha256(asset72.png).hexdigest())
        self.assertEqual(asset72.transform["renderer"], "pdfium")
        self.assertEqual(asset72.transform["dpi"], 72)
        self.assertEqual(asset72.transform["region"], (0.0, 0.0, 1.0, 1.0))
        self.assertEqual(asset72.transform["width_px"], 144)
        self.assertEqual(asset72.transform["height_px"], 72)

        asset150 = render_region(data, 0, dpi=150)
        self.assertEqual(asset150.width_px, 300)
        self.assertEqual(asset150.height_px, 150)
        self.assertEqual(asset150.transform["dpi"], 150)
        self.assertEqual(asset150.transform["width_px"], 300)
        self.assertEqual(asset150.transform["height_px"], 150)

    def test_determinism(self) -> None:
        data = build_pdf([{"mediabox": (0, 0, 144, 72)}])
        asset_a = render_region(data, 0, dpi=150)
        asset_b = render_region(data, 0, dpi=150)
        self.assertEqual(asset_a.png, asset_b.png)
        self.assertEqual(asset_a.sha256, asset_b.sha256)

    def test_rotated_coordinate_agreement(self) -> None:
        setups = [
            (
                "A",
                {"mediabox": (0, 0, 200, 100)},
                (0.0, 0.0, 200.0, 100.0),
                (20.0, 10.0, 60.0, 30.0),
                (120.0, 60.0, 180.0, 90.0),
            ),
            (
                "B",
                {"mediabox": (0, 0, 300, 200), "cropbox": (50, 50, 250, 150)},
                (50.0, 50.0, 250.0, 150.0),
                (70.0, 60.0, 110.0, 80.0),
                (170.0, 110.0, 230.0, 140.0),
            ),
        ]

        for setup_name, page_base, visible, r_box, q_probe in setups:
            for rot in (0, 90, 180, 270):
                with self.subTest(setup=setup_name, rot=rot):
                    p_dict = dict(page_base)
                    p_dict["rects"] = [r_box]
                    p_dict["rotate"] = rot
                    data = build_pdf([p_dict])

                    region = to_region_space(r_box, visible, rot)
                    asset_r = render_region(data, 0, region=region, dpi=144)
                    w_r, h_r, rgb_r = decode_png_rgb(asset_r.png)
                    inner_box = (2, 2, w_r - 2, h_r - 2)
                    self.assertLess(mean_luma(w_r, h_r, rgb_r, inner_box), 40.0)

                    probe = to_region_space(q_probe, visible, rot)
                    asset_q = render_region(data, 0, region=probe, dpi=144)
                    w_q, h_q, rgb_q = decode_png_rgb(asset_q.png)
                    whole_box = (0, 0, w_q, h_q)
                    self.assertGreater(mean_luma(w_q, h_q, rgb_q, whole_box), 240.0)

                    asset_full = render_region(data, 0, dpi=144)
                    w, h, rgb_full = decode_png_rgb(asset_full.png)
                    w_pt = visible[2] - visible[0]
                    h_pt = visible[3] - visible[1]
                    if rot in (90, 270):
                        expected_size = (int(h_pt * 2), int(w_pt * 2))
                    else:
                        expected_size = (int(w_pt * 2), int(h_pt * 2))
                    self.assertEqual((w, h), expected_size)

                    pixel_box = (
                        int(region[0] * w) + 2,
                        int(region[1] * h) + 2,
                        int(region[2] * w) - 2,
                        int(region[3] * h) - 2,
                    )
                    self.assertLess(mean_luma(w, h, rgb_full, pixel_box), 40.0)

    def test_validation(self) -> None:
        data = build_pdf([{"mediabox": (0, 0, 100, 100)}])

        for invalid_dpi in (71, 301, True, 150.0):
            with self.subTest(dpi=invalid_dpi):
                with self.assertRaises(ValueError):
                    render_region(data, 0, dpi=invalid_dpi)  # type: ignore[arg-type]

        invalid_regions = [
            (0.5, 0.0, 0.4, 1.0),
            (0.0, 0.0, 1.0, 1.1),
            (0.0, 0.0, 1.0),
            (0.0, 0.0, float("nan"), 1.0),
        ]
        for inv_reg in invalid_regions:
            with self.subTest(region=inv_reg):
                with self.assertRaises(ValueError):
                    render_region(data, 0, region=inv_reg)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            render_region(data, 1)
        with self.assertRaises(ValueError):
            render_region(data, -1)
        with self.assertRaises(ValueError):
            render_region(data, True)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            render_region(b"", 0)

    def test_pixel_limit(self) -> None:
        data = build_pdf([{"mediabox": (0, 0, 2000, 2000)}])

        with self.assertRaises(ValueError) as ctx:
            render_region(data, 0, dpi=300)
        self.assertEqual(str(ctx.exception), "pixel_limit")

        asset_small = render_region(data, 0, region=(0.0, 0.0, 0.1, 0.1), dpi=300)
        self.assertEqual(asset_small.width_px, 833)

    def test_extract_rgb_from_bitmap(self) -> None:
        bm3 = types.SimpleNamespace(
            width=2,
            height=1,
            stride=8,
            n_channels=3,
            buffer=bytes([1, 2, 3, 4, 5, 6, 0, 0]),
        )
        self.assertEqual(r._extract_rgb_from_bitmap(bm3), bytes([3, 2, 1, 6, 5, 4]))

        bm4 = types.SimpleNamespace(
            width=1,
            height=2,
            stride=4,
            n_channels=4,
            buffer=bytes([10, 20, 30, 255, 40, 50, 60, 255]),
        )
        self.assertEqual(
            r._extract_rgb_from_bitmap(bm4),
            bytes([30, 20, 10, 60, 50, 40]),
        )

        bm1 = types.SimpleNamespace(
            width=2,
            height=1,
            stride=2,
            n_channels=1,
            buffer=bytes([7, 9]),
        )
        self.assertEqual(r._extract_rgb_from_bitmap(bm1), bytes([7, 7, 7, 9, 9, 9]))

        bm2 = types.SimpleNamespace(
            width=1,
            height=1,
            stride=2,
            n_channels=2,
            buffer=bytes([1, 2]),
        )
        with self.assertRaises(ValueError):
            r._extract_rgb_from_bitmap(bm2)

        bm_short = types.SimpleNamespace(
            width=2,
            height=1,
            stride=8,
            n_channels=3,
            buffer=bytes([1, 2, 3, 4, 5, 6]),
        )
        with self.assertRaises(ValueError):
            r._extract_rgb_from_bitmap(bm_short)


if __name__ == "__main__":
    unittest.main()
