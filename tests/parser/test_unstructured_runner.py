"""Parent-side behaviour of the Unstructured runner using SYNTHETIC test-double children.

The doubles never run the real library; they only write canned element JSON (and apply
the same resource limits as the real child through ``apply_limits``). Real execution of
pinned unstructured 0.27.36, including rotated and CropBox pages, is covered by the
CI-only probe job."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_parser import unstructured_runner
from lab_parser.isolation import InspectionError, ProcessLimits
from lab_parser.unstructured_runner import run_unstructured_fast
from tests.parser.pdfgen import build_pdf

ELEMENT = {"type": "NarrativeText", "element_id": "e1", "text": "Synthetic runner text",
           "metadata": {"page_number": 1, "filename": "source.pdf"}}
PIXEL = {"system": "PixelSpace", "points": [[40, 30], [40, 60], [80, 60], [80, 30]]}
LIB = str(Path(unstructured_runner.__file__).parent.parent)


PLAIN = {"rotation": 0, "mediabox": [0, 0, 612, 792], "cropbox": [0, 0, 612, 792]}
SMALL = {"rotation": 0, "mediabox": [0, 0, 300, 400], "cropbox": [0, 0, 300, 400]}


def child_source(version="0.27.36", elements=(ELEMENT,), pages="[True, True]", extra="", geometry=None):
    """A double child. ``geometry`` is what the real child reports per page (default: the two plain
    pages of the standard test PDF); a child that reports none attests nothing."""
    geometry = [PLAIN, SMALL] if geometry is None else geometry
    return ("import json\n"
            f"open('meta.json','xb').write(json.dumps({{'unstructured': {version!r}, 'attestable_pages': {pages}, "
            f"'page_geometry': {geometry!r}}}).encode())\n"
            f"open('elements.json','xb').write(json.dumps({list(elements)!r}).encode())\n" + extra)


def limited_child(body: str) -> str:
    """A double that applies the real child's resource limits, then misbehaves."""
    return (f"import sys\nsys.path.insert(0, {LIB!r})\n"
            "from lab_parser._unstructured_child import apply_limits\n"
            "apply_limits(*map(int, sys.argv[1:]))\n" + body)


class Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        # Page 1: 612x792. Page 2: 300x400 (a plain page; rotation is tested via the child's report).
        self.pdf = build_pdf([{"mediabox": (0, 0, 612, 792)}, {"mediabox": (0, 0, 300, 400)}])

    def child(self, source: str) -> Path:
        path = Path(self._dir.name, f"child{len(list(Path(self._dir.name).iterdir()))}.py")
        path.write_text(source)
        return path

    def run_it(self, source, **kw):
        return run_unstructured_fast(self.pdf, document_id="doc-synthetic", budget_ref="b",
                                     operation_id="op", _child=self.child(source), **kw)

    def element(self, page, layout):
        return dict(ELEMENT, metadata={"page_number": page,
                                       "coordinates": dict(PIXEL, layout_width=layout[0], layout_height=layout[1])})


class RunnerTests(Base):
    def test_frames_come_from_real_page_geometry(self):
        batch = self.run_it(child_source(elements=(self.element(2, (300, 400)),)))
        self.assertIsNone(batch.quarantine_reason)
        region = batch.evidence[0].regions[0]
        self.assertEqual(region.page_index, 1)
        self.assertAlmostEqual(region.bbox[0], 40 / 300)
        self.assertAlmostEqual(region.bbox[3], 60 / 400)

    def test_layout_that_disagrees_with_the_real_frame_loses_geometry(self):
        batch = self.run_it(child_source(elements=(self.element(2, (400, 300)),)))
        self.assertEqual(len(batch.evidence), 0)  # no geometry is invented
        self.assertEqual(len(batch.observations), 1)

    def test_pages_the_child_cannot_attest_keep_text_but_claim_no_region(self):
        # Rotated or CropBox pages: pdfminer ignores both, so even a layout that happens
        # to equal the frame must not become a region.
        batch = self.run_it(child_source(elements=(self.element(1, (612, 792)),), pages="[False, True]"))
        self.assertEqual(len(batch.evidence), 0)
        self.assertEqual(len(batch.observations), 1)
        self.assertEqual(batch.observations[0].raw_content, "Synthetic runner text")

    def test_attestable_pages_must_match_the_geometry_page_count(self):
        for pages in ("[True]", "[True, True, True]", "[1, 1]", "'yes'"):
            with self.subTest(pages), self.assertRaises(InspectionError) as ctx:
                self.run_it(child_source(pages=pages))
            self.assertEqual(ctx.exception.code, "invalid_child_result")

    def test_a_page_subset_keeps_original_indices_and_frames(self):
        from lab_parser.isolation import inspect_geometry
        geometry = inspect_geometry(self.pdf)
        # Subset [1]: the child's own page 1 is original page 1 (300x400 frame, not page 0's 612x792).
        el = self.element(1, (300, 400))
        batch = run_unstructured_fast(self.pdf, document_id="doc-synthetic", budget_ref="b", operation_id="op",
                                      geometry=geometry, page_indices=[1],
                                      _child=self.child(child_source(elements=(el,), pages="[True]", geometry=[SMALL])))
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(batch.manifest.page_selection, [1])
        self.assertEqual([m.page_index for m in batch.manifest.page_index_map], [1])
        self.assertEqual(batch.evidence[0].regions[0].page_index, 1)
        self.assertAlmostEqual(batch.evidence[0].regions[0].bbox[0], 40 / 300)
        self.assertEqual([p.page_index for p in batch.result.pages], [1])

    def test_invalid_subsets_and_foreign_geometry_are_refused(self):
        from lab_parser.isolation import inspect_geometry
        geometry = inspect_geometry(self.pdf)
        other = inspect_geometry(build_pdf([{"mediabox": (0, 0, 100, 100)}]))
        child = self.child(child_source())
        for kwargs in ({"page_indices": []}, {"page_indices": [2]}, {"page_indices": [1, 0]}, {"page_indices": [0, 0]},
                       {"page_indices": [True]}, {"geometry": other}):
            with self.subTest(kwargs), self.assertRaises(InspectionError) as ctx:
                run_unstructured_fast(self.pdf, document_id="d", budget_ref="b", operation_id="o", _child=child,
                                      **{"geometry": geometry, **kwargs})
            self.assertEqual(ctx.exception.code, "invalid_input")

    def crop_child(self, info, *, pages="[True]"):
        el = dict(ELEMENT, metadata={"page_number": 1, "coordinates": {
            "system": "PixelSpace", "layout_width": 612, "layout_height": 792,
            "points": [[72, 60.9], [72, 110.9], [191.8, 110.9], [191.8, 60.9]]}})
        source = child_source(elements=(el,), pages=pages, geometry=info)
        return self.child(source)

    def test_a_crop_box_page_is_attested_with_the_translated_box(self):
        pdf = build_pdf([{"mediabox": (0, 0, 612, 792), "cropbox": (50, 50, 662, 842)}])
        info = [{"rotation": 0, "mediabox": [0, 0, 612, 792], "cropbox": [50, 50, 662, 842]}]
        batch = run_unstructured_fast(pdf, document_id="d", budget_ref="b", operation_id="o", _child=self.crop_child(info))
        self.assertEqual(len(batch.evidence), 1)
        x0, y0, x1, y1 = batch.evidence[0].regions[0].bbox
        # The page shows [50..612] x [top 0..742]: x 72 -> (72-50)/562, y 60.9 -> 60.9/742.
        self.assertAlmostEqual(x0, 22 / 562)
        self.assertAlmostEqual(y0, 60.9 / 742)
        self.assertAlmostEqual(x1, (191.8 - 50) / 562)

    def test_a_child_that_reports_no_page_boxes_attests_nothing(self):
        el = self.element(1, (612, 792))
        for meta_pages in ("[True, True]",):
            source = ("import json\nopen('meta.json','xb').write(json.dumps({'unstructured': '0.27.36', "
                      f"'attestable_pages': {meta_pages}}}).encode())\n"
                      f"open('elements.json','xb').write(json.dumps([{el!r}]).encode())\n")
            batch = self.run_it(source)
            self.assertEqual((len(batch.evidence), len(batch.observations)), (0, 1))
            for junk in ("'x'", "{}", "None"):
                source = source.replace("}).encode())\nopen('elements", f", 'page_geometry': {junk}}}).encode())\nopen('elements", 1)
                self.assertEqual(len(self.run_it(source).evidence), 0)

    def test_a_child_whose_boxes_do_not_match_the_inspected_page_gets_no_frame(self):
        pdf = build_pdf([{"mediabox": (0, 0, 612, 792), "cropbox": (50, 50, 662, 842)}])
        lie = [{"rotation": 0, "mediabox": [0, 0, 612, 792], "cropbox": [0, 0, 612, 792]}]   # says: no crop
        batch = run_unstructured_fast(pdf, document_id="d", budget_ref="b", operation_id="o", _child=self.crop_child(lie))
        self.assertEqual(len(batch.evidence), 0)
        for bad in ([{"rotation": 45, "mediabox": [0, 0, 612, 792], "cropbox": [0, 0, 612, 792]}], [{}], [],
                    [{"rotation": 0, "mediabox": [0, 0, 612], "cropbox": [0, 0, 612, 792]}]):
            with self.subTest(bad):
                batch = run_unstructured_fast(pdf, document_id="d", budget_ref="b", operation_id="o", _child=self.crop_child(bad))
                self.assertEqual(len(batch.evidence), 0)

    def test_rotated_sideways_pages_are_never_attested(self):
        pdf = build_pdf([{"mediabox": (0, 0, 612, 792), "rotate": 90}])
        info = [{"rotation": 90, "mediabox": [0, 0, 612, 792], "cropbox": [0, 0, 612, 792]}]
        el = dict(ELEMENT, metadata={"page_number": 1, "coordinates": {
            "system": "PixelSpace", "layout_width": 792, "layout_height": 612, "points": [[681, 72], [681, 190], [731, 190], [731, 72]]}})
        source = child_source(elements=(el,), pages="[False]", geometry=info)
        batch = run_unstructured_fast(pdf, document_id="d", budget_ref="b", operation_id="o", _child=self.child(source))
        self.assertEqual((len(batch.evidence), len(batch.observations)), (0, 1))

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
        batch = self.run_it("import json\n"
                            "open('meta.json','xb').write(json.dumps({'unstructured': '0.27.36', 'attestable_pages': [True, True]}).encode())\n"
                            "open('elements.json','xb').write(b'not json')\n")
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")
        self.assertEqual(len(batch.evidence), 0)

    def test_wall_deadline_stops_the_child(self):
        with self.assertRaises(InspectionError) as ctx:
            self.run_it("import time\ntime.sleep(30)\n", limits=ProcessLimits(wall_seconds=0.5))
        self.assertEqual(ctx.exception.code, "timeout")

    def test_cancel_stops_the_child(self):
        with self.assertRaises(InspectionError) as ctx:
            self.run_it("import time\ntime.sleep(30)\n", cancelled=lambda: True)
        self.assertEqual(ctx.exception.code, "cancelled")

    def test_invalid_pdf_never_reaches_the_parser(self):
        with self.assertRaises(InspectionError):
            run_unstructured_fast(b"%PDF-1.4 not a pdf", document_id="d", budget_ref="b", operation_id="o",
                                  _child=self.child("raise SystemExit('must not run')\n"))

    def test_an_empty_env_is_not_replaced_by_the_default(self):
        from lab_parser.isolation import _run_child
        import time
        out = Path(self._dir.name, "env.txt")
        script = self.child(f"import os\nopen({str(out)!r},'w').write(repr(sorted(os.environ)))\n")
        _run_child([unstructured_runner.sys.executable, "-I", str(script)], self._dir.name,
                   time.monotonic() + 20, lambda: False, env={})
        self.assertNotIn("LANG", out.read_text())


class ResourceLimitTests(Base):
    """The real child's limits, applied through the shared ``apply_limits``."""
    LIMITS = ProcessLimits(memory_bytes=128 * 1024 * 1024, cpu_seconds=1, wall_seconds=30, output_bytes=1024)

    def expect_child_failed(self, body):
        with self.assertRaises(InspectionError) as ctx:
            self.run_it(limited_child(body), limits=self.LIMITS)
        self.assertEqual(ctx.exception.code, "child_failed")

    def test_allocation_beyond_the_address_space_limit_fails(self):
        self.expect_child_failed("x = bytearray(512 * 1024 * 1024)\n")

    def test_writing_beyond_the_file_size_limit_fails(self):
        self.expect_child_failed("with open('big.bin','wb') as f:\n    f.write(b'x' * 100000)\n    f.flush()\n")

    def test_burning_cpu_beyond_the_limit_fails(self):
        self.expect_child_failed("while True:\n    pass\n")

    def test_working_directory_is_removed_after_a_timeout(self):
        seen = []
        real = tempfile.TemporaryDirectory

        def spy(*a, **k):
            d = real(*a, **k)
            seen.append(d.name)
            return d

        with patch.object(unstructured_runner.tempfile, "TemporaryDirectory", spy):
            with self.assertRaises(InspectionError):
                self.run_it("import time\ntime.sleep(30)\n", limits=ProcessLimits(wall_seconds=0.5))
        self.assertTrue(seen)
        self.assertFalse(any(Path(p).exists() for p in seen))


if __name__ == "__main__":
    unittest.main()
