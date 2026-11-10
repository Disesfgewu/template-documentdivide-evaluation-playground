"""The child's lost-text detector, on real PDFs (Refs #7). The real library is not needed here."""
from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfWriter

from lab_parser._unstructured_child import MIN_LOST_TEXT_CHARS, _pages_with_lost_text
from tests.parser.pdfgen import build_pdf


def text_pdf(pages: list[str]) -> bytes:
    """One text line per page ('' = a blank page); hand built so pypdf can extract it."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = (f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else "").encode("ascii")
        content = len(objs) + 2
        kids.append(len(objs) + 1)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


class LostTextTests(unittest.TestCase):
    def lost(self, pages, elements):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(text_pdf(pages))
            handle.flush()
            return _pages_with_lost_text(handle.name, elements)

    LONG = "This page carries a real sentence of text"

    def element(self, page):
        return {"type": "NarrativeText", "text": "x", "metadata": {"page_number": page}}

    def test_pages_with_text_but_no_element_are_flagged(self):
        self.assertEqual(self.lost([self.LONG, self.LONG, self.LONG], []), [0, 1, 2])

    def test_pages_that_have_an_element_are_not_flagged(self):
        self.assertEqual(self.lost([self.LONG, self.LONG, self.LONG], [self.element(2)]), [0, 2])
        self.assertEqual(self.lost([self.LONG, self.LONG], [self.element(1), self.element(2)]), [])

    def test_blank_pages_and_scraps_of_text_are_not_evidence_of_lost_content(self):
        self.assertEqual(self.lost(["", "7", "Page 12"], []), [])           # blank, a page number, a short label
        self.assertEqual(MIN_LOST_TEXT_CHARS, 20)
        self.assertEqual(self.lost(["x" * 19], []), [])
        self.assertEqual(self.lost(["x" * 20], []), [0])

    def test_junk_elements_do_not_break_it(self):
        self.assertEqual(self.lost([self.LONG], [None, "x", {"metadata": None} if False else {}]), [0])

    def test_a_pdf_without_any_text_layer_is_never_flagged(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(build_pdf([{"mediabox": (0, 0, 612, 792)}] * 3))
            handle.flush()
            self.assertEqual(_pages_with_lost_text(handle.name, []), [])


if __name__ == "__main__":
    unittest.main()
