"""Parent-side behaviour of the Unstructured runner using SYNTHETIC test-double children.

The doubles never run the real library; they only write canned element JSON. Real
execution of pinned unstructured 0.27.36 is covered by the CI-only probe job."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lab_parser.isolation import InspectionError, ProcessLimits
from lab_parser.unstructured_runner import run_unstructured_fast
from tests.parser.pdfgen import build_pdf

ELEMENT = {"type": "NarrativeText", "element_id": "e1", "text": "Synthetic runner text",
           "metadata": {"page_number": 1, "filename": "source.pdf"}}

HEAD = "import json,sys\nopen('meta.json','xb').write(json.dumps({'unstructured': %r}).encode())\n"


def child_source(version="0.27.36", elements=(ELEMENT,), extra=""):
    return (HEAD % version) + f"open('elements.json','xb').write(json.dumps({list(elements)!r}).encode())\n" + extra


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.pdf = build_pdf([{"mediabox": (0, 0, 612, 792)}, {"mediabox": (0, 0, 300, 400), "rotate": 90}])

    def child(self, source: str) -> Path:
        path = Path(self._dir.name, f"child{len(list(Path(self._dir.name).iterdir()))}.py")
        path.write_text(source)
        return path

    def run_it(self, source, **kw):
        return run_unstructured_fast(self.pdf, document_id="doc-synthetic", budget_ref="b",
                                     operation_id="op", _child=self.child(source), **kw)

    def test_frames_come_from_real_page_geometry(self):
        el = dict(ELEMENT, metadata={"page_number": 2, "coordinates": {
            "system": "PixelSpace", "layout_width": 400, "layout_height": 300,
            "points": [[40, 30], [40, 60], [80, 60], [80, 30]]}})
        batch = self.run_it(child_source(elements=(el,)))
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.evidence), 1)
        # Page 2 is rotated 90: its attested display frame is the swapped 400x300.
        region = batch.evidence[0].regions[0]
        self.assertEqual(region.page_index, 1)
        self.assertEqual(region.bbox, (0.1, 0.1, 0.2, 0.2))

    def test_layout_that_disagrees_with_the_real_frame_loses_geometry(self):
        el = dict(ELEMENT, metadata={"page_number": 2, "coordinates": {
            "system": "PixelSpace", "layout_width": 300, "layout_height": 400,
            "points": [[40, 30], [40, 60], [80, 60], [80, 30]]}})
        batch = self.run_it(child_source(elements=(el,)))
        # No geometry is invented: the observation is kept, but no evidence unit is made.
        self.assertEqual(len(batch.evidence), 0)
        self.assertEqual(len(batch.observations), 1)

    def test_every_page_is_selected_and_manifest_is_the_pinned_profile(self):
        batch = self.run_it(child_source())
        self.assertEqual(batch.manifest.page_selection, [0, 1])
        self.assertEqual(batch.manifest.package_or_service_version, "0.27.36")
        self.assertEqual(batch.manifest.effective_profile, "unstructured-oss-fast")
        self.assertEqual(batch.manifest.cost_usd, 0.0)

    def test_wrong_installed_version_is_refused_not_normalized(self):
        with self.assertRaises(InspectionError) as ctx:
            self.run_it(child_source(version="0.18.32"))
        self.assertEqual(ctx.exception.code, "version_mismatch")

    def test_child_failure_is_a_fixed_code(self):
        with self.assertRaises(InspectionError) as ctx:
            self.run_it("import sys\nsys.exit(3)\n")
        self.assertEqual(ctx.exception.code, "child_failed")

    def test_missing_output_is_invalid_child_result(self):
        with self.assertRaises(InspectionError) as ctx:
            self.run_it("open('meta.json','xb').write(b'{}')\n")
        self.assertEqual(ctx.exception.code, "invalid_child_result")

    def test_garbage_elements_are_quarantined(self):
        batch = self.run_it(HEAD % "0.27.36" + "open('elements.json','xb').write(b'not json')\n")
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")
        self.assertEqual(len(batch.evidence), 0)

    def test_wall_deadline_stops_the_child(self):
        limits = ProcessLimits(wall_seconds=0.5)
        with self.assertRaises(InspectionError) as ctx:
            self.run_it("import time\ntime.sleep(30)\n", limits=limits)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_cancel_stops_the_child(self):
        with self.assertRaises(InspectionError) as ctx:
            self.run_it("import time\ntime.sleep(30)\n", cancelled=lambda: True)
        self.assertEqual(ctx.exception.code, "cancelled")

    def test_invalid_pdf_never_reaches_the_parser(self):
        with self.assertRaises(InspectionError):
            run_unstructured_fast(b"%PDF-1.4 not a pdf", document_id="d", budget_ref="b", operation_id="o",
                                  _child=self.child("raise SystemExit('must not run')\n"))


if __name__ == "__main__":
    unittest.main()
