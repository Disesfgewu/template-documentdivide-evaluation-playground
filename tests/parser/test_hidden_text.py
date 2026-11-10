"""Hidden-text detector on real PDFs built by hand and read by real pypdf (PM-DECISION on gap G8, Refs #7)."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import time
import unittest

from pypdf import PdfReader

_PATH = Path(__file__).resolve().parents[2] / "services" / "parser" / "lab_parser" / "_hidden_text.py"
_spec = importlib.util.spec_from_file_location("_hidden_text_under_test", _PATH)
hidden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hidden)


def make_pdf(content: str, forms: dict[str, str] | None = None, page_extra: str = "") -> bytes:
    """One page with ``content``; ``forms`` maps XObject names to Form XObject content streams."""
    forms = forms or {}
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    xobj = ""
    for n, (name, body) in enumerate(forms.items()):
        num = 6 + n
        data = body.encode("ascii")
        objs[num] = (f"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> "
                     f"/XObject << {xobj_ref(forms, num)} >> >> /Length {len(data)} >>\nstream\n").encode() + data + b"\nendstream"
        xobj += f"/{name} {num} 0 R "
    stream = content.encode("ascii")
    objs[3] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] {page_extra} /Contents 4 0 R "
               f"/Resources << /Font << /F1 5 0 R >> /XObject << {xobj} >> >> >>").encode()
    objs[4] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    size = max(objs) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for n in range(1, size):
        out += b"%010d 00000 n \n" % offsets[n]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def xobj_ref(forms, own):
    """Forms may invoke each other by name (for the nesting test): every form sees all the others."""
    return " ".join(f"/{name} {6 + n} 0 R" for n, name in enumerate(forms) if 6 + n != own)


def scan(content: str, forms=None):
    reader = PdfReader(io.BytesIO(make_pdf(content, forms)), strict=True)
    return hidden.scan_document(reader)


SHOW = "(hello hidden) Tj"


class HiddenTextTests(unittest.TestCase):
    def test_a_normal_page_is_clean(self):
        self.assertEqual(scan(f"BT /F1 12 Tf 72 700 Td {SHOW} ET"), [])

    def test_render_mode_3_and_7_hide_text(self):
        for mode in (3, 7):
            with self.subTest(mode):
                self.assertEqual(scan(f"BT /F1 12 Tf {mode} Tr 72 700 Td {SHOW} ET"), [[0, "text_not_visible"]])

    def test_visible_modes_are_clean_and_a_mode_with_no_text_shown_is_harmless(self):
        for mode in (0, 1, 2, 4, 5, 6):
            with self.subTest(mode):
                self.assertEqual(scan(f"BT /F1 12 Tf {mode} Tr 72 700 Td {SHOW} ET"), [])
        self.assertEqual(scan("BT 3 Tr ET"), [])

    def test_a_page_with_visible_body_and_one_invisible_run_is_quarantined_as_a_whole(self):
        content = (f"BT /F1 12 Tf 72 700 Td {SHOW} ET\n"
                   f"BT 3 Tr 72 600 Td (invisible run) Tj ET\n"
                   f"BT 0 Tr 72 500 Td {SHOW} ET")
        self.assertEqual(scan(content), [[0, "text_not_visible"]])

    def test_the_rendering_mode_survives_bt_et_and_is_restored_by_q(self):
        # Set before BT, still in force after ET -> a later BT shows hidden text.
        self.assertEqual(scan(f"3 Tr BT /F1 12 Tf ET BT 72 700 Td {SHOW} ET"), [[0, "text_not_visible"]])
        self.assertEqual(scan(f"BT 3 Tr ET BT /F1 12 Tf 72 700 Td {SHOW} ET"), [[0, "text_not_visible"]])
        # q ... Tr 3 ... Q restores the visible mode, so text after Q is visible.
        self.assertEqual(scan(f"q BT 3 Tr ET Q BT /F1 12 Tf 72 700 Td {SHOW} ET"), [])
        # ... and text shown before Q, inside q, is hidden.
        self.assertEqual(scan(f"q 3 Tr BT /F1 12 Tf 72 700 Td {SHOW} ET Q"), [[0, "text_not_visible"]])

    def test_all_show_operators_count(self):
        for show in ("(x) Tj", "[(x) 10 (y)] TJ", "(x) '", "1 2 (x) \""):
            with self.subTest(show):
                self.assertEqual(scan(f"BT /F1 12 Tf 3 Tr 72 700 Td {show} ET"), [[0, "text_not_visible"]])

    def test_a_form_xobject_is_walked_and_inherits_the_mode_of_its_caller(self):
        forms = {"Fm1": f"BT /F1 12 Tf 72 700 Td {SHOW} ET"}
        self.assertEqual(scan("/Fm1 Do", forms), [])
        self.assertEqual(scan("3 Tr /Fm1 Do", forms), [[0, "text_not_visible"]])
        forms = {"Fm1": f"BT /F1 12 Tf 3 Tr 72 700 Td {SHOW} ET"}
        self.assertEqual(scan("/Fm1 Do", forms), [[0, "text_not_visible"]])
        # a mode set inside a form does not leak into the page
        forms = {"Fm1": "BT 3 Tr ET"}
        self.assertEqual(scan(f"/Fm1 Do BT /F1 12 Tf 72 700 Td {SHOW} ET", forms), [])

    def test_nested_forms_are_followed_to_a_bounded_depth(self):
        forms = {"A": "/B Do", "B": "/C Do", "C": f"BT /F1 12 Tf 3 Tr {SHOW} ET"}
        self.assertEqual(scan("/A Do", forms), [[0, "text_not_visible"]])
        deep = {f"F{i}": f"/F{i + 1} Do" for i in range(hidden.MAX_DEPTH + 2)}
        deep[f"F{hidden.MAX_DEPTH + 2}"] = f"BT /F1 12 Tf 3 Tr {SHOW} ET"
        self.assertEqual(scan("/F0 Do", deep), [[0, "content_unparseable"]])   # too deep: fail closed

    def test_a_self_referencing_form_cannot_loop_forever(self):
        self.assertEqual(scan("/Loop Do", {"Loop": "/Loop Do"})[0][1], "content_unparseable")

    def test_a_missing_xobject_or_a_garbled_operator_fails_closed(self):
        self.assertEqual(scan("/Nope Do"), [[0, "content_unparseable"]])
        self.assertEqual(scan("BT /F1 12 Tf (unterminated Tj ET"), [[0, "content_unparseable"]])

    def test_work_is_bounded_on_an_adversarial_content_stream(self):
        content = "q Q " * (hidden.MAX_OPERATORS // 2 + 10)
        started = time.monotonic()
        self.assertEqual(scan(content), [[0, "content_unparseable"]])
        self.assertLess(time.monotonic() - started, 30)

    def test_the_child_entry_point_loads_the_detector_by_path_and_reports_pages(self):
        import tempfile
        from lab_parser._unstructured_child import _hidden_text_pages
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(make_pdf(f"BT /F1 12 Tf 3 Tr 72 700 Td {SHOW} ET"))
            handle.flush()
            self.assertEqual(_hidden_text_pages(handle.name), [[0, "text_not_visible"]])

    def test_a_page_without_contents_is_clean(self):
        reader = PdfReader(io.BytesIO(make_pdf("")), strict=True)
        self.assertEqual(hidden.scan_document(reader), [])


if __name__ == "__main__":
    unittest.main()
