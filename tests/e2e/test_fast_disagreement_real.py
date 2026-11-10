"""What REAL Unstructured fast returns where pypdf and pdfminer may disagree (Refs #7, measurement only).

The PM review of #127 asked for real fast output on rotated, CropBox and watermark pages before any single
page is treated as fatal. Each case is a one-page SYNTHETIC PDF run through the real ``run_unstructured_fast``;
the report records, per case, the number of elements, pypdf's non-whitespace character count, the page status
and diagnostics, and whether the page was flagged suspect. It asserts that the control page (plain text) is
parsed with elements and evidence and not flagged, so a broken measurement fails, and (gap G8) that pages with
invisible text (render mode 3 or 7, alone or next to visible text) are needs_review/text_not_visible with no
evidence, and (G8 (b)) that white text on a white page has no evidence (region_without_ink); the other rows are
data, not policy.
Not covered: Type3 fonts and odd encodings (not built here). Runs only with ``LAB_E2E_REAL_UNSTRUCTURED=1``.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_parser import unstructured_runner
from lab_parser.isolation import InspectionError
from lab_parser.unstructured_runner import LOST_TEXT_DIAGNOSTIC, run_unstructured_fast

ENABLED = os.environ.get("LAB_E2E_REAL_UNSTRUCTURED") == "1"
BODY = ["This is an ordinary sentence of synthetic body text for the measurement.",
        "A second line follows so that the page has more than one text line."]


def one_page_pdf(ops: str, extra: str = "") -> bytes:
    stream = ops.encode("ascii")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] {extra} /Contents 4 0 R "
            f"/Resources << /Font << /F1 5 0 R >> >> >>".encode(),
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
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


def body_ops(prefix: str = "") -> str:
    return "\n".join(["BT", prefix, "/F1 14 Tf", "72 700 Td", "18 TL"] + [f"({t}) Tj T*" for t in BODY] + ["ET"])


CASES = {
    "control_plain": one_page_pdf(body_ops()),
    "rotate_90": one_page_pdf(body_ops(), "/Rotate 90"),
    "rotate_180": one_page_pdf(body_ops(), "/Rotate 180"),
    "rotate_270": one_page_pdf(body_ops(), "/Rotate 270"),
    "cropbox_clips_all_text": one_page_pdf(body_ops(), "/CropBox [400 0 612 792]"),
    "cropbox_clips_half": one_page_pdf(body_ops(), "/CropBox [200 0 612 792]"),
    "invisible_text_render_mode_3": one_page_pdf(body_ops("3 Tr")),
    "clip_only_render_mode_7": one_page_pdf(body_ops("7 Tr")),
    "visible_body_plus_invisible_run": one_page_pdf(
        body_ops() + "\nBT 3 Tr /F1 14 Tf 72 300 Td (An invisible run on a page that also has visible text.) Tj ET"),
    "white_text_on_white_page": one_page_pdf(body_ops("1 g")),
    "text_clipped_to_a_10pt_square": one_page_pdf("q 0 0 10 10 re W n\n" + body_ops() + "\nQ"),
    "text_far_outside_the_page": one_page_pdf(
        "BT /F1 14 Tf 3000 700 Td (This text is far outside the page box so nobody sees it.) Tj ET"),
    "visible_body_plus_white_run": one_page_pdf(
        body_ops() + "\nBT 1 g /F1 14 Tf 72 300 Td (A white run on a page that also has visible text.) Tj ET"),
    "short_words_and_spaces_line": one_page_pdf(
        "BT /F1 14 Tf 72 700 Td (I am a we do it so go on up to me as if an ox) Tj ET"),
    "single_short_word": one_page_pdf("BT /F1 14 Tf 72 700 Td (Hi there) Tj ET"),
    "wide_character_spacing_line": one_page_pdf(
        "BT /F1 14 Tf 12 Tc 72 700 Td (Wide spaced text for the ink fraction) Tj ET"),
    "grey_diagonal_watermark_only": one_page_pdf(
        "BT 0.9 g /F1 60 Tf 0.7071 0.7071 -0.7071 0.7071 150 200 Tm (SYNTHETIC WATERMARK TEXT FOR THE MEASUREMENT) Tj ET"),
    "watermark_under_body": one_page_pdf(
        "q BT 0.9 g /F1 60 Tf 0.7071 0.7071 -0.7071 0.7071 150 200 Tm (SYNTHETIC WATERMARK TEXT FOR THE MEASUREMENT) Tj ET Q\n"
        + body_ops()),
}


class _SpyInk:
    """Wraps the runner's ink check and records the ink fraction of every evidence region it is asked about."""

    def __init__(self, inner, log):
        self._inner, self._log, self.sizes = inner, log, inner.sizes

    def fraction(self, page_index, bbox):
        return self._inner.fraction(page_index, bbox)

    def __call__(self, page_index, bbox):
        fraction = self._inner.fraction(page_index, bbox)
        self._log.append(round(fraction, 3))
        return self._inner(page_index, bbox)


def pypdf_chars(data: bytes) -> int:
    from pypdf import PdfReader
    text = PdfReader(io.BytesIO(data), strict=True).pages[0].extract_text() or ""
    return len("".join(text.split()))


@unittest.skipUnless(ENABLED, "needs LAB_E2E_REAL_UNSTRUCTURED=1 (CI job e2e-parse)")
class FastDisagreementMeasurement(unittest.TestCase):
    def test_measure_real_fast_output_on_pages_where_the_extractors_may_disagree(self):
        rows = {}
        for name, data in CASES.items():
            row = {"pypdf_nonspace_chars": pypdf_chars(data)}
            fractions: list[float] = []
            original = unstructured_runner._parse_ink
            unstructured_runner._parse_ink = lambda raw, pages, _o=original, _f=fractions: (
                None if _o(raw, pages) is None else _SpyInk(_o(raw, pages), _f))
            try:
                batch = run_unstructured_fast(data, document_id="doc-measure", budget_ref="b", operation_id=f"op-{name}")
                page = batch.result.pages[0]
                row.update(outcome="parsed", elements=len(batch.observations), evidence=len(batch.evidence),
                           status=page.status, diagnostics=list(page.diagnostics),
                           suspect=LOST_TEXT_DIAGNOSTIC in page.diagnostics)
            except InspectionError as exc:
                row.update(outcome="error", code=exc.code)
            finally:
                unstructured_runner._parse_ink = original
            row["ink_fractions"] = fractions     # one per evidence region that was checked (min ink fraction to keep: 0.2)
            rows[name] = row
        report = {"synthetic": True, "cases": rows}
        print("FAST_DISAGREEMENT_REPORT " + json.dumps(report, sort_keys=True))
        path = os.environ.get("LAB_E2E_DISAGREEMENT_REPORT")
        if path:
            Path(path).write_text(json.dumps(report, indent=2, sort_keys=True))
        control = rows["control_plain"]
        self.assertEqual(control.get("outcome"), "parsed", control)
        self.assertGreater(control["elements"], 0, control)
        self.assertFalse(control["suspect"], control)
        self.assertEqual(control["evidence"], 1, control)
        # PM-DECISION on gap G8: a page with text the reader cannot see is quarantined as a whole.
        for name in ("invisible_text_render_mode_3", "clip_only_render_mode_7", "visible_body_plus_invisible_run"):
            row = rows[name]
            self.assertEqual((row.get("outcome"), row.get("status"), row.get("evidence")), ("parsed", "needs_review", 0), (name, row))
            self.assertIn("text_not_visible", row["diagnostics"], (name, row))
        # Gap G8 (b): evidence needs rendered ink under its region. White text on a white page has none.
        white = rows["white_text_on_white_page"]
        self.assertEqual((white.get("outcome"), white.get("evidence")), ("parsed", 0), white)
        self.assertIn("region_without_ink", white["diagnostics"], white)


if __name__ == "__main__":
    unittest.main()
