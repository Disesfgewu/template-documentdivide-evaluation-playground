"""Tests for PDF user-space <-> LAB SourceRegion geometry conversion (SPEC 4.1, T05)."""

from __future__ import annotations

import io
import json
import unittest

from pypdf import PdfReader

from fixtures import registry_check as rc
from lab_parser.geometry import to_region_space, to_user_space

# Marker positions in PDF user space (x, y, x+w, y+h) with w=200, h=60:
# L = (72, 600, w=200, h=60) -> (72, 600, 272, 660)
# R = (340, 300, w=200, h=60) -> (340, 300, 540, 360)
MARKER_L_BOX = (72.0, 600.0, 272.0, 660.0)
MARKER_R_BOX = (340.0, 300.0, 540.0, 360.0)


def _page_cropbox(page) -> tuple[float, float, float, float]:
    cb = page.cropbox
    return (float(cb[0]), float(cb[1]), float(cb[2]), float(cb[3]))


class TestGeometry(unittest.TestCase):
    def test_f02_fixture_markers_within_tolerance(self) -> None:
        entry = next(e for e in rc.load()["fixtures"] if e["id"] == "F02")
        item = entry["items"][0]
        data = rc.item_bytes(item)
        expected = json.loads((rc.ROOT / entry["expected"]).read_text(encoding="utf-8"))[
            "items"
        ][item["id"]]["pages"]

        reader = PdfReader(io.BytesIO(data))
        self.assertEqual(len(reader.pages), len(expected))

        for page_exp in expected:
            page_index = page_exp["page_index"]
            pdf_page = reader.pages[page_index]
            rotate = int(pdf_page.rotation or 0)
            cropbox = _page_cropbox(pdf_page)

            markers_by_suffix = {}
            for m in page_exp.get("markers", []):
                text = m["text"]
                if text.endswith("-L"):
                    markers_by_suffix["L"] = m["bbox"]
                elif text.endswith("-R"):
                    markers_by_suffix["R"] = m["bbox"]

            if "L" in markers_by_suffix:
                actual_l = to_region_space(MARKER_L_BOX, cropbox, rotate)
                for a, e in zip(actual_l, markers_by_suffix["L"]):
                    self.assertLessEqual(
                        abs(a - e),
                        0.005,
                        f"Page {page_index} L mismatch: actual={actual_l}, expected={markers_by_suffix['L']}",
                    )

            if "R" in markers_by_suffix:
                actual_r = to_region_space(MARKER_R_BOX, cropbox, rotate)
                for a, e in zip(actual_r, markers_by_suffix["R"]):
                    self.assertLessEqual(
                        abs(a - e),
                        0.005,
                        f"Page {page_index} R mismatch: actual={actual_r}, expected={markers_by_suffix['R']}",
                    )

    def test_roundtrip_all_rotations_and_cropboxes(self) -> None:
        cropboxes = [
            (0.0, 0.0, 612.0, 792.0),
            (36.0, 72.0, 576.0, 720.0),
        ]
        boxes = [
            MARKER_L_BOX,
            MARKER_R_BOX,
            (100.5, 150.25, 420.75, 680.125),
        ]
        for cb in cropboxes:
            for rot in (0, 90, 180, 270, -90, 450, -180, 360):
                for box in boxes:
                    region = to_region_space(box, cb, rot)
                    self.assertGreaterEqual(region[0], 0.0)
                    self.assertGreaterEqual(region[1], 0.0)
                    self.assertLessEqual(region[2], 1.0)
                    self.assertLessEqual(region[3], 1.0)
                    self.assertLess(region[0], region[2])
                    self.assertLess(region[1], region[3])

                    recovered = to_user_space(region, cb, rot)
                    for orig, rec in zip(box, recovered):
                        self.assertLess(abs(orig - rec), 1e-9)

                    region_again = to_region_space(recovered, cb, rot)
                    for r1, r2 in zip(region, region_again):
                        self.assertLess(abs(r1 - r2), 1e-9)

    def test_rotate_normalization_equivalents(self) -> None:
        cb = (36.0, 72.0, 576.0, 720.0)
        box = MARKER_L_BOX
        self.assertEqual(to_region_space(box, cb, -90), to_region_space(box, cb, 270))
        self.assertEqual(to_region_space(box, cb, 450), to_region_space(box, cb, 90))
        self.assertEqual(to_region_space(box, cb, -180), to_region_space(box, cb, 180))
        self.assertEqual(to_region_space(box, cb, 360), to_region_space(box, cb, 0))

        reg = to_region_space(box, cb, 270)
        self.assertEqual(to_user_space(reg, cb, -90), to_user_space(reg, cb, 270))

    def test_clipping_to_cropbox(self) -> None:
        cb = (100.0, 100.0, 500.0, 700.0)
        # Partially outside cropbox on left/bottom and right/top
        box = (50.0, 50.0, 300.0, 400.0)
        clipped = (100.0, 100.0, 300.0, 400.0)
        for rot in (0, 90, 180, 270):
            self.assertEqual(
                to_region_space(box, cb, rot),
                to_region_space(clipped, cb, rot),
            )

        # Box completely covering cropbox clips to [0, 0, 1, 1]
        full_box = (0.0, 0.0, 612.0, 792.0)
        for rot in (0, 90, 180, 270):
            self.assertEqual(to_region_space(full_box, cb, rot), (0.0, 0.0, 1.0, 1.0))

    def test_outside_cropbox_raises(self) -> None:
        cb = (100.0, 100.0, 500.0, 700.0)
        outside_boxes = [
            (0.0, 0.0, 90.0, 90.0),      # completely left/below
            (0.0, 200.0, 100.0, 300.0),  # touching left edge only (zero width intersection)
            (200.0, 700.0, 300.0, 792.0),  # touching top edge only (zero height intersection)
            (510.0, 200.0, 600.0, 400.0),  # completely right
        ]
        for box in outside_boxes:
            for rot in (0, 90, 180, 270):
                with self.subTest(box=box, rot=rot):
                    with self.assertRaises(ValueError) as ctx:
                        to_region_space(box, cb, rot)
                    self.assertEqual(str(ctx.exception), "outside_cropbox")

    def test_invalid_rotate_raises(self) -> None:
        cb = (0.0, 0.0, 612.0, 792.0)
        for bad_rot in (45, -45, 1, 135, 271):
            with self.assertRaises(ValueError):
                to_region_space(MARKER_L_BOX, cb, bad_rot)
            with self.assertRaises(ValueError):
                to_user_space((0.1, 0.1, 0.5, 0.5), cb, bad_rot)

    def test_zero_or_negative_area_cropbox_raises(self) -> None:
        bad_cropboxes = [
            (100.0, 100.0, 100.0, 500.0),  # zero width
            (100.0, 500.0, 400.0, 500.0),  # zero height
            (400.0, 100.0, 100.0, 500.0),  # negative width
            (100.0, 500.0, 400.0, 100.0),  # negative height
        ]
        for cb in bad_cropboxes:
            with self.assertRaises(ValueError):
                to_region_space(MARKER_L_BOX, cb, 0)
            with self.assertRaises(ValueError):
                to_user_space((0.1, 0.1, 0.5, 0.5), cb, 0)


if __name__ == "__main__":
    unittest.main()
