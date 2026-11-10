"""Render-based ink cross-check on real PDFium renders (gap G8 (b), Refs #7)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from lab_parser.unstructured_runner import INK_GRID, MIN_INK_FRACTION, _parse_ink
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


def annotated_pdf() -> bytes:
    """A blank page with one Square annotation whose appearance stream is a big black rectangle."""
    ap = b"0 g 0 0 200 200 re f"
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Annots [4 0 R] >>",
            4: b"<< /Type /Annot /Subtype /Square /Rect [100 100 300 300] /F 4 /AP << /N 5 0 R >> >>",
            5: b"<< /Type /XObject /Subtype /Form /BBox [0 0 200 200] /Length %d >>\nstream\n" % len(ap) + ap + b"\nendstream"}
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % offsets[n] for n in range(1, 6))
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


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
        self.assertGreater(check.fraction(0, (0, 0, 1, 0.2)), 0)         # the tails of the lines, top of the cropped page
        self.assertEqual(check.fraction(0, (0, 0.5, 1, 1)), 0)
        upside_down = has_ink_for(one_page_pdf(body_ops(), "/Rotate 180"))[1]
        self.assertTrue(upside_down(0, (0.05, 0.86, 0.95, 0.95)))        # the text moved to the bottom
        self.assertFalse(upside_down(0, TEXT_BOX))

    def test_a_page_that_is_too_large_or_cannot_be_rendered_has_no_map(self):
        huge = one_page_pdf(body_ops()).replace(b"/MediaBox [0 0 612 792]", b"/MediaBox [0 0 6000 6000]")
        self.assertIsNone(has_ink_for(huge)[0][0])
        self.assertFalse(has_ink_for(huge)[1](0, (0, 0, 1, 1)))      # no map: never "has ink"

    def test_annotations_and_form_fields_are_not_drawn(self):
        maps, check = has_ink_for(annotated_pdf())
        self.assertIsNotNone(maps[0])
        self.assertFalse(check(0, (0, 0, 1, 1)))     # the black annotation square would be ink if it were drawn

    def test_the_rendered_size_is_reported_for_the_frame_comparison(self):
        maps, _ = has_ink_for(one_page_pdf(body_ops()))
        self.assertEqual(maps[0]["size"], [612.0, 792.0])
        maps, _ = has_ink_for(one_page_pdf(body_ops(), "/CropBox [50 50 612 792]"))
        self.assertEqual(maps[0]["size"], [562.0, 742.0])
        maps, _ = has_ink_for(one_page_pdf(body_ops(), "/Rotate 90"))
        self.assertEqual(maps[0]["size"], [792.0, 612.0])

    def test_measured_ink_fractions_on_real_text_lines_stay_well_above_the_threshold(self):
        _, check = has_ink_for(one_page_pdf(body_ops()))
        one_line = check.fraction(0, (0.113, 0.09, 0.95, 0.115))      # roughly the first 14 pt line
        two_lines = check.fraction(0, (0.113, 0.07, 0.95, 0.14))
        print("ink fraction one line %.2f, two lines %.2f (threshold %.2f)" % (one_line, two_lines, MIN_INK_FRACTION))
        self.assertGreater(two_lines, MIN_INK_FRACTION)
        self.assertEqual(MIN_INK_FRACTION, 0.2)

    def test_the_cost_of_a_page_is_small(self):
        import time
        started = time.monotonic()
        has_ink_for(one_page_pdf(body_ops()))
        self.assertLess(time.monotonic() - started, 5)


class GridTests(unittest.TestCase):
    def test_a_cell_needs_at_least_min_cell_pixels_dark_pixels(self):
        width = height = ink.GRID                      # one pixel per cell at this size: use a 4x larger page
        width = height = ink.GRID * 4                  # 4x4 pixels per cell
        for dark, expect in ((ink.MIN_CELL_PIXELS - 1, False), (ink.MIN_CELL_PIXELS, True)):
            buffer = bytearray(b"\xff" * (width * height))
            for k in range(dark):                      # pixels of cell (row 0, column 0)
                buffer[(k // 4) * width + (k % 4)] = 0
            rows = ink.grid_from_gray(bytes(buffer), width, height, width)
            self.assertEqual(rows[0][-1] in "13579bdf", expect, (dark, rows[0]))
        self.assertEqual(ink.MIN_CELL_PIXELS, 3)

    def test_near_white_pixels_are_not_ink_and_slightly_darker_ones_are(self):
        width = height = ink.GRID * 4
        for value, expect in ((ink.INK_BELOW, False), (ink.INK_BELOW - 1, True)):
            buffer = bytes([value]) * (width * height)
            rows = ink.grid_from_gray(buffer, width, height, width)
            self.assertEqual(any(int(r, 16) for r in rows), expect, value)
        self.assertEqual(ink.INK_BELOW, 245)


class ParseInkTests(unittest.TestCase):
    def maps(self, *entries):
        return json.dumps({"grid": INK_GRID, "pages": list(entries)}).encode()

    @staticmethod
    def entry(rows, size=(612, 792)):
        return {"size": list(size), "rows": rows}

    def test_boxes_are_rounded_outward_and_clamped_and_the_fraction_is_over_the_cells_under_the_box(self):
        rows = ["0" * 32] * INK_GRID
        rows[10] = format(1 << 20, "032x")                 # one cell: row 10, column 20
        check = _parse_ink(self.maps(self.entry(rows)), [0])
        cell = 1 / INK_GRID
        self.assertEqual(check.fraction(0, (20 * cell, 10 * cell, 21 * cell, 11 * cell)), 1.0)
        self.assertEqual(check.fraction(0, (20.4 * cell, 10.4 * cell, 20.6 * cell, 10.6 * cell)), 1.0)   # inside the cell
        self.assertEqual(check.fraction(0, (19.9 * cell, 9.9 * cell, 20.1 * cell, 10.1 * cell)), 0.25)   # rounded out to 2x2
        self.assertEqual(check.fraction(0, (22 * cell, 10 * cell, 30 * cell, 11 * cell)), 0.0)
        self.assertEqual(check.fraction(0, (-1, -1, 2, 2)), 1 / (INK_GRID * INK_GRID))                    # clamped to the page
        self.assertEqual(check.fraction(1, (0, 0, 1, 1)), 0.0)                                            # unknown page
        self.assertEqual(check.fraction(0, (0.5, 0.5, 0.5, 0.5)), 0.0)                                    # empty box

    def test_the_fraction_threshold_is_tested_on_both_sides(self):
        # A box of 10 columns x 10 rows = 100 cells; ink in n of them.
        for n, expect in ((int(100 * MIN_INK_FRACTION) - 1, False), (int(100 * MIN_INK_FRACTION), True)):
            rows = ["0" * 32] * INK_GRID
            for k in range(n):
                r, c = 20 + k // 10, 30 + k % 10
                rows[r] = format(int(rows[r], 16) | (1 << c), "032x")
            check = _parse_ink(self.maps(self.entry(rows)), [0])
            cell = 1 / INK_GRID
            self.assertEqual(check(0, (30 * cell, 20 * cell, 40 * cell, 30 * cell)), expect, n)

    def test_one_rule_through_a_box_is_not_enough(self):
        rows = ["0" * 32] * INK_GRID
        rows[25] = "f" * 32                                 # a full-width horizontal rule: 1 of 10 rows = 10%
        check = _parse_ink(self.maps(self.entry(rows)), [0])
        cell = 1 / INK_GRID
        self.assertFalse(check(0, (30 * cell, 20 * cell, 40 * cell, 30 * cell)))

    def test_malformed_or_missing_maps_give_none_never_ink(self):
        good = ["0" * 32] * INK_GRID
        entry = lambda **kw: {"size": [612, 792], "rows": good, **kw}  # noqa: E731
        bad = [None, b"not json", b"[]", b"{}", json.dumps({"grid": 64, "pages": [entry()]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": []}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [entry(rows=good[:-1])]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [entry(rows=["0" * 31] * INK_GRID)]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [entry(rows=["zz" * 16] * INK_GRID)]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [entry(rows=[5] * INK_GRID)]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [entry(size=[0, 792])]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [entry(size=[612])]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [{"rows": good}]}).encode(),
               json.dumps({"grid": INK_GRID, "pages": [entry()], "extra": 1}).encode()]
        for raw in bad:
            with self.subTest(raw if raw is None else raw[:40]):
                self.assertIsNone(_parse_ink(raw, [0]))


if __name__ == "__main__":
    unittest.main()
