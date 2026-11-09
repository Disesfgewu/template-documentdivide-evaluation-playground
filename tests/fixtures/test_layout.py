"""F02 layout fixture: expected SPEC 4.1 coordinates (top-left origin, after
/Rotate, normalized to the visible CropBox) are checked against an
independent renderer (PDFium) -- the oracle for #7's T05 bbox round-trip."""
import io
import json
import unittest
from collections import deque

import pypdfium2 as pdfium
from pypdf import PdfReader

from fixtures import registry_check as rc

ENTRY = rc.load()["fixtures"][1]
ITEM = ENTRY["items"][0]
EXPECTED = json.loads((rc.ROOT / ENTRY["expected"]).read_text())["items"][ITEM["id"]]["pages"]
T05_TOLERANCE = 0.005  # SPEC T05


def gray_boxes(page):
    """Bounding boxes of the light-gray marker fills, found by flood fill on
    the rendered bitmap, normalized to the rendered (visible) page."""
    bm = page.render(scale=1.0)
    w, h, stride, n = bm.width, bm.height, bm.stride, bm.n_channels
    buf = bytes(bm.buffer)

    def gray(x, y):
        o = y * stride + x * n
        return all(abs(buf[o + c] - 217) <= 4 for c in range(3))

    seen = bytearray(w * h)
    boxes = []
    for y in range(h):
        for x in range(w):
            if seen[y * w + x] or not gray(x, y):
                continue
            queue, count = deque([(x, y)]), 0
            seen[y * w + x] = 1
            x0 = x1 = x
            y0 = y1 = y
            while queue:
                cx, cy = queue.popleft()
                count += 1
                x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and gray(nx, ny):
                        seen[ny * w + nx] = 1
                        queue.append((nx, ny))
            if count > 200:
                boxes.append([x0 / w, y0 / h, (x1 + 1) / w, (y1 + 1) / h])
    return sorted(boxes)


class F02LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = rc.item_bytes(ITEM)

    def test_rendered_markers_match_expected_coordinates(self):
        pdf = pdfium.PdfDocument(self.data)
        self.assertEqual(len(pdf), len(EXPECTED))
        for i, page in enumerate(pdf):
            want = sorted(m["bbox"] for m in EXPECTED[i]["markers"])
            got = gray_boxes(page)
            with self.subTest(page=i, rotate=EXPECTED[i]["rotate"]):
                self.assertEqual(len(got), len(want))
                for g, wv in zip(got, want):
                    for a, b in zip(g, wv):
                        self.assertLessEqual(abs(a - b), T05_TOLERANCE)

    def test_page_geometry_and_labels(self):
        reader = PdfReader(io.BytesIO(self.data), strict=True)
        self.assertEqual(list(reader.page_labels), [p["page_label"] for p in EXPECTED])
        for page, exp in zip(reader.pages, EXPECTED):
            self.assertEqual(page.rotation, exp["rotate"])
            self.assertEqual([float(v) for v in page.cropbox], [float(v) for v in exp["cropbox"]])
            self.assertEqual([float(v) for v in page.mediabox], [float(v) for v in exp["mediabox"]])
            text = page.extract_text()
            for m in exp["markers"]:
                self.assertIn(m["text"], text)

    def test_all_four_rotations_and_a_cropbox_are_covered(self):
        self.assertEqual({p["rotate"] for p in EXPECTED}, {0, 90, 180, 270})
        self.assertTrue(any(p["cropbox"] != p["mediabox"] for p in EXPECTED))


if __name__ == "__main__":
    unittest.main()
