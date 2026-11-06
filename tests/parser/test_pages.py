"""Unit tests for PDF page geometry extraction and normalization (SPEC 4.1 coordinates, T05, 19.2 L0, 19.3.8 limits)."""

from __future__ import annotations

import dataclasses
import unittest

from pypdf.errors import PdfReadError

from lab_parser.pages import PageInfo, extract_pages
from tests.parser.pdfgen import build_pdf


class TestPages(unittest.TestCase):
    def test_three_pages_geometry_and_frozen(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 612, 792)},
            {"mediabox": (0, 0, 600, 400), "cropbox": (50, 20, 550, 380), "rotate": 90},
            {"mediabox": (0, 0, 300, 200), "rotate": -90},
        ])
        pages = extract_pages(pdf_bytes)

        self.assertEqual(len(pages), 3)
        self.assertEqual([p.page_index for p in pages], [0, 1, 2])
        self.assertEqual([p.rotation for p in pages], [0, 90, 270])

        p0 = pages[0]
        self.assertEqual(p0.display_width_pt, 612.0)
        self.assertEqual(p0.display_height_pt, 792.0)

        p1 = pages[1]
        self.assertEqual(p1.mediabox, (0.0, 0.0, 600.0, 400.0))
        self.assertEqual(p1.cropbox, (50.0, 20.0, 550.0, 380.0))
        self.assertEqual(p1.visible_box, (50.0, 20.0, 550.0, 380.0))
        self.assertEqual(p1.display_width_pt, 360.0)
        self.assertEqual(p1.display_height_pt, 500.0)

        p2 = pages[2]
        self.assertEqual(p2.display_width_pt, 200.0)
        self.assertEqual(p2.display_height_pt, 300.0)

        for p in pages:
            self.assertIsInstance(p, PageInfo)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                p.rotation = 0  # type: ignore[misc]

    def test_cropbox_larger_than_mediabox(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 300, 200), "cropbox": (-50, -50, 250, 150)}
        ])
        pages = extract_pages(pdf_bytes)

        self.assertEqual(len(pages), 1)
        p = pages[0]
        self.assertEqual(p.visible_box, (0.0, 0.0, 250.0, 150.0))
        self.assertEqual(p.display_width_pt, 250.0)
        self.assertEqual(p.display_height_pt, 150.0)

    def test_regression_no_page_labels(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 100, 100)},
            {"mediabox": (0, 0, 100, 100)},
            {"mediabox": (0, 0, 100, 100)},
        ])
        pages = extract_pages(pdf_bytes)

        self.assertEqual(len(pages), 3)
        for p in pages:
            self.assertIsNone(p.page_label)

    def test_page_labels(self) -> None:
        pdf_bytes = build_pdf(
            [
                {"mediabox": (0, 0, 100, 100)},
                {"mediabox": (0, 0, 100, 100)},
                {"mediabox": (0, 0, 100, 100)},
            ],
            labels=[
                {"from": 0, "to": 1, "style": "/r"},
                {"from": 2, "to": 2, "style": "/D", "prefix": "A-", "start": 1},
            ],
        )
        pages = extract_pages(pdf_bytes)

        self.assertEqual([p.page_label for p in pages], ["i", "ii", "A-1"])

    def test_invalid_rotation_45(self) -> None:
        pdf_bytes = build_pdf([
            {"mediabox": (0, 0, 100, 100), "rotate": 45}
        ])
        with self.assertRaises(ValueError):
            extract_pages(pdf_bytes)

    def test_garbage_bytes(self) -> None:
        with self.assertRaises(PdfReadError):
            extract_pages(b"not a pdf")


if __name__ == "__main__":
    unittest.main()
