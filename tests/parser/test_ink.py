"""Render-based ink cross-check on real PDFium renders (gap G8 (b), Refs #7)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from lab_parser.unstructured_runner import INK_GRID, _parse_ink
from tests.e2e.test_fast_disagreement_real import body_ops, one_page_pdf

_PATH = Path(__file__).resolve().parents[2] / "services" / "parser" / "lab_parser" / "_ink.py"
_spec = importlib.util.spec_from_file_location("_ink_under_test", _PATH)
ink = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ink)

TEXT_BOX = (0.05, 0.05, 0.95, 0.14)    # the two body lines (PDF y 700..664 of 792) as a fraction from the top
EMPTY_BOX = (0.05, 0.5, 0.95, 0.6)


def has_ink_for(pdf: bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(pdf)
        handle.flush()
        maps = ink.document_ink(handle.name)
    return maps, _parse_ink(json.dumps({"grid": INK_GRID, "pages": maps}).encode(), list(range(len(maps))))


class InkTests(unittest.TestCase):
    def test_visible_text_has_ink_under_its_box_and_not_elsewhere(self):
        _, check = has_ink_for(one_page_pdf(body_ops()))
        self.assertTrue(check(0, TEXT_BOX))
        self.assertFalse(check(0, EMPTY_BOX))

    def test_white_text_on_a_white_page_leaves_no_ink(self):
        _, check = has_ink_for(one_page_pdf(body_ops("1 g")))
        self.assertFalse(check(0, TEXT_BOX))

    def test_off_page_text_and_a_blank_page_leave_no_ink(self):
        off = one_page_pdf("BT /F1 14 Tf 3000 700 Td (This text is far outside the page box) Tj ET")
        self.assertFalse(has_ink_for(off)[1](0, (0, 0, 1, 1)))
        self.assertFalse(has_ink_for(one_page_pdf(""))[1](0, (0, 0, 1, 1)))

    def test_text_clipped_away_leaves_no_ink(self):
        clipped = one_page_pdf("q 0 0 10 10 re W n\n" + body_ops() + "\nQ")
        self.assertFalse(has_ink_for(clipped)[1](0, TEXT_BOX))

    def test_the_cropbox_and_rotation_are_applied_like_the_display_frame(self):
        # Crop to the right half: the body text starts at x=72 (left of the crop), so the cropped page shows
        # only the tail of each line; the whole cropped page is the display frame.
        cropped = one_page_pdf(body_ops(), "/CropBox [300 0 612 792]")
        maps, check = has_ink_for(cropped)
        self.assertIsNotNone(maps[0])
        self.assertTrue(check(0, (0, 0, 1, 1)))
        upside_down = has_ink_for(one_page_pdf(body_ops(), "/Rotate 180"))[1]
        self.assertTrue(upside_down(0, (0.05, 0.86, 0.95, 0.95)))        # the text moved to the bottom
        self.assertFalse(upside_down(0, TEXT_BOX))

    def test_a_page_that_is_too_large_or_cannot_be_rendered_has_no_map(self):
        huge = one_page_pdf(body_ops()).replace(b"/MediaBox [0 0 612 792]", b"/MediaBox [0 0 6000 6000]")
        self.assertIsNone(has_ink_for(huge)[0][0])
        self.assertFalse(has_ink_for(huge)[1](0, (0, 0, 1, 1)))      # no map: never "has ink"

    def test_the_cost_of_a_page_is_small(self):
        import time
        started = time.monotonic()
        has_ink_for(one_page_pdf(body_ops()))
        self.assertLess(time.monotonic() - started, 5)


class ParseInkTests(unittest.TestCase):
    def maps(self, *rows_sets):
        return json.dumps({"grid": INK_GRID, "pages": list(rows_sets)}).encode()

    def test_boxes_are_rounded_outward_and_clamped(self):
        rows = ["0" * 32] * INK_GRID
        rows[10] = format(1 << 20, "032x")                 # one cell: row 10, column 20
        check = _parse_ink(self.maps(rows), [0])
        cell = 1 / INK_GRID
        self.assertTrue(check(0, (20 * cell, 10 * cell, 21 * cell, 11 * cell)))
        self.assertTrue(check(0, (20.4 * cell, 10.4 * cell, 20.6 * cell, 10.6 * cell)))   # inside the cell
        self.assertTrue(check(0, (19.9 * cell, 9.9 * cell, 20.1 * cell, 10.1 * cell)))    # touching it (rounded out)
        self.assertFalse(check(0, (22 * cell, 10 * cell, 30 * cell, 11 * cell)))
        self.assertFalse(check(0, (20 * cell, 12 * cell, 21 * cell, 13 * cell)))
        self.assertTrue(check(0, (-1, -1, 2, 2)))                                          # clamped to the page
        self.assertFalse(check(1, (0, 0, 1, 1)))                                           # unknown page

    def test_malformed_maps_are_refused(self):
        good = ["0" * 32] * INK_GRID
        for raw in (b"not json", b"[]", b"{}", json.dumps({"grid": 64, "pages": [good]}).encode(),
                    json.dumps({"grid": INK_GRID, "pages": []}).encode(),
                    json.dumps({"grid": INK_GRID, "pages": [good[:-1]]}).encode(),
                    json.dumps({"grid": INK_GRID, "pages": [["0" * 31] * INK_GRID]}).encode(),
                    json.dumps({"grid": INK_GRID, "pages": [["zz" * 16] * INK_GRID]}).encode(),
                    json.dumps({"grid": INK_GRID, "pages": [[5] * INK_GRID]}).encode(),
                    json.dumps({"grid": INK_GRID, "pages": [good], "extra": 1}).encode()):
            with self.subTest(raw[:40]):
                from lab_parser.isolation import InspectionError
                with self.assertRaises(InspectionError) as ctx:
                    _parse_ink(raw, [0])
                self.assertEqual(ctx.exception.code, "invalid_child_result")


if __name__ == "__main__":
    unittest.main()
