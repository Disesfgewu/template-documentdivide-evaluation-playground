"""Unit tests for PDF page geometry extraction and normalization."""

from __future__ import annotations

import dataclasses
import io
import json
import unittest

from fixtures import registry_check as rc
from pypdf.generic import NameObject, NumberObject
from pypdf import PdfWriter
from pypdf.generic import RectangleObject

from lab_parser.pages import (
    PageInfo,
    _normalize_rotation,
    extract_pages,
)


class TestNormalizeRotation(unittest.TestCase):
    """Test rotation angle normalization function."""

    def test_normalize_rotation_cases(self) -> None:
        """Verify normalization to {0, 90, 180, 270} and error handling."""
        self.assertEqual(_normalize_rotation(-90), 270)
        self.assertEqual(_normalize_rotation(450), 90)
        with self.assertRaises(ValueError):
            _normalize_rotation(45)

        # Standard angles
        self.assertEqual(_normalize_rotation(0), 0)
        self.assertEqual(_normalize_rotation(90), 90)
        self.assertEqual(_normalize_rotation(180), 180)
        self.assertEqual(_normalize_rotation(270), 270)
        self.assertEqual(_normalize_rotation(360), 0)

        # Negative angles and large multiples
        self.assertEqual(_normalize_rotation(-180), 180)
        self.assertEqual(_normalize_rotation(-270), 90)
        self.assertEqual(_normalize_rotation(-360), 0)
        self.assertEqual(_normalize_rotation(540), 180)
        self.assertEqual(_normalize_rotation(-450), 270)

        # Non-multiples of 90
        with self.assertRaises(ValueError):
            _normalize_rotation(-45)
        with self.assertRaises(ValueError):
            _normalize_rotation(15)
        with self.assertRaises(ValueError):
            _normalize_rotation(91)


class TestExtractPagesFixtureF02(unittest.TestCase):
    """Test page geometry extraction against fixture F02."""

    def test_f02_geometry_and_pages(self) -> None:
        """Extract pages from F02 and assert fields against expected data."""
        entry = next(e for e in rc.load()["fixtures"] if e["id"] == "F02")
        item = entry["items"][0]
        data = rc.item_bytes(item)
        expected = json.loads((rc.ROOT / entry["expected"]).read_text())["items"][item["id"]]["pages"]

        pages = extract_pages(data)

        # Map expected items by page_index
        if isinstance(expected, list):
            expected_by_index = {p["page_index"]: p for p in expected}
        elif isinstance(expected, dict):
            expected_by_index = {int(k): v for k, v in expected.items()}
        else:
            raise AssertionError(f"Unexpected expected format: {type(expected)}")

        # Verify page count is sufficient
        self.assertGreaterEqual(len(pages), len(expected_by_index))

        # Check page_label, rotate, mediabox, cropbox for each expected page
        for idx, exp_page in expected_by_index.items():
            self.assertLess(idx, len(pages))
            page = pages[idx]
            self.assertEqual(page.page_index, exp_page["page_index"])
            self.assertEqual(page.page_label, exp_page["page_label"])
            self.assertEqual(page.rotation, exp_page["rotate"])
            self.assertEqual(
                page.mediabox,
                tuple(float(v) for v in exp_page["mediabox"]),
            )
            self.assertEqual(
                page.cropbox,
                tuple(float(v) for v in exp_page["cropbox"]),
            )

        # Page 0 (index 0) display dimensions = 612x792
        self.assertEqual(pages[0].display_width_pt, 612.0)
        self.assertEqual(pages[0].display_height_pt, 792.0)

        # Page 5 (index 4) display dimensions = 540x648
        self.assertEqual(pages[4].page_index, 4)
        self.assertEqual(pages[4].rotation, 0)
        self.assertEqual(pages[4].display_width_pt, 540.0)
        self.assertEqual(pages[4].display_height_pt, 648.0)

        # Page 6 (index 5) rotate 90 display dimensions = 648x540
        self.assertEqual(pages[5].page_index, 5)
        self.assertEqual(pages[5].rotation, 90)
        self.assertEqual(pages[5].display_width_pt, 648.0)
        self.assertEqual(pages[5].display_height_pt, 540.0)


class TestExtractPagesFixtureF01(unittest.TestCase):
    """Test page geometry extraction against fixture F01."""

    def test_f01_pages_21(self) -> None:
        """Extract pages from F01 F01.pages_21 and verify page indices and labels."""
        entry = next(e for e in rc.load()["fixtures"] if e["id"] == "F01")
        item = next(
            (it for it in entry["items"] if it.get("id") == "F01.pages_21"),
            None,
        )
        if item is None:
            item = next(
                it for it in entry["items"]
                if "pages_21" in str(it.get("id", ""))
                or "pages_21" in str(it.get("path", ""))
                or "pages_21" in str(it.get("name", ""))
            )
        data = rc.item_bytes(item)
        pages = extract_pages(data)

        self.assertEqual(len(pages), 21)
        for i, page in enumerate(pages):
            self.assertEqual(page.page_index, i)
            # label can be None or "1".."21" based on pypdf's actual parsing
            self.assertIn(page.page_label, (None, str(i + 1)))


class TestExtractPagesValidation(unittest.TestCase):
    """Test validation errors and edge cases for extract_pages."""

    def test_no_intersection_raises_value_error(self) -> None:
        """Disjoint MediaBox and CropBox must raise ValueError."""
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        page.cropbox = RectangleObject([300, 300, 400, 400])

        buf = io.BytesIO()
        writer.write(buf)
        with self.assertRaises(ValueError):
            extract_pages(buf.getvalue())

    def test_zero_area_intersection_raises_value_error(self) -> None:
        """MediaBox and CropBox touching only at an edge must raise ValueError."""
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        page.cropbox = RectangleObject([200, 0, 300, 200])

        buf = io.BytesIO()
        writer.write(buf)
        with self.assertRaises(ValueError):
            extract_pages(buf.getvalue())

    def test_invalid_rotation_raises_value_error(self) -> None:
        """Rotation that is not a multiple of 90 degrees must raise ValueError."""
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        # pypdf's page.rotate() refuses non-multiples of 90 itself, so write
        # the raw /Rotate entry to exercise extract_pages' own validation.
        page[NameObject("/Rotate")] = NumberObject(45)

        buf = io.BytesIO()
        writer.write(buf)
        with self.assertRaises(ValueError):
            extract_pages(buf.getvalue())

    def test_page_info_dataclass_frozen(self) -> None:
        """PageInfo instance attributes must be immutable."""
        info = PageInfo(
            page_index=0,
            page_label="1",
            rotation=0,
            mediabox=(0.0, 0.0, 612.0, 792.0),
            cropbox=(0.0, 0.0, 612.0, 792.0),
            visible_box=(0.0, 0.0, 612.0, 792.0),
            display_width_pt=612.0,
            display_height_pt=792.0,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            info.page_index = 1  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
