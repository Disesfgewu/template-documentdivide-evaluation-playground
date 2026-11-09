"""End to end on real PostgreSQL with the REAL pinned Unstructured fast child (Refs #7).

Upload -> queue -> consumer (the real ``run_unstructured_fast``, no double) -> stored result ->
GET summary and pages. Every input is SYNTHETIC (fixture F01 text PDF from the repository
generator, plus a hand-built mixed-geometry PDF). Runs only when ``LAB_E2E_REAL_UNSTRUCTURED=1``
in an environment that has ``unstructured==0.27.36`` (the CI job ``e2e-parse``); otherwise it is
skipped, never passed. ``LAB_E2E_REPORT`` names a JSON file that receives versions and timings.
"""
from __future__ import annotations

import importlib.metadata as md
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "db", _REPO_ROOT / "services" / "api", _REPO_ROOT / "services" / "parser", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.parse_results import make_persist
from lab_api.parse_worker import process_parse_job
from lab_db import jobs
from lab_db.storage import LocalArtifactStore
from fixtures.generators import synthetic_pdf
from deploy.unstructured_probe import synthetic_pdf as hand_built_pdf

try:
    from tests.db.base import FreshDatabase, ADMIN_URL
except ImportError:
    ADMIN_URL = os.environ.get("LAB_TEST_DATABASE_URL", "")
    FreshDatabase = unittest.TestCase  # type: ignore[misc,assignment]

ENABLED = os.environ.get("LAB_E2E_REAL_UNSTRUCTURED") == "1"
REPORT = {"synthetic": True, "python": sys.version.split()[0]}


def layout_pdf() -> bytes:
    """Two synthetic pages: (1) a two-column article, (2) a ruled 4x3 table. Hand built, no data."""
    def text(x, y, line):
        return f"BT /F1 11 Tf 1 0 0 1 {x} {y} Tm ({line}) Tj ET"

    left = ["LEFTCOL alpha measures the synthetic baseline for the first group of samples.",
            "LEFTCOL bravo continues the argument and refers to the earlier baseline.",
            "LEFTCOL charlie closes the left column with a short synthetic summary."]
    right = ["RIGHTCOL delta opens the right column with an unrelated synthetic remark.",
             "RIGHTCOL echo adds a second sentence that stays inside the right column.",
             "RIGHTCOL foxtrot ends the page with a final synthetic statement here."]
    import textwrap

    def column(x, paragraphs):
        lines = [chunk for paragraph in paragraphs for chunk in textwrap.wrap(paragraph, 38)]
        return [text(x, 740 - 16 * i, chunk) for i, chunk in enumerate(lines)]

    page1 = column(72, left) + column(330, right)
    rows = [("Item", "Quantity", "UnitPrice"), ("Widget", "12", "3.50"), ("Gadget", "7", "10.25"), ("Gizmo", "100", "0.75")]
    xs, ys = [72, 250, 430, 540], [720, 696, 672, 648, 624]
    page2 = [text(72, 760, "Synthetic price table TABLEMARK")]
    for y in ys:
        page2.append(f"72 {y} m 540 {y} l S")
    for x in xs:
        page2.append(f"{x} {ys[0]} m {x} {ys[-1]} l S")
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page2.append(text(xs[c] + 6, ys[r] - 16, cell))
    return raw_pdf(["\n".join(page1), "\n".join(page2)])


def raw_pdf(contents: list[str]) -> bytes:
    objs = [b"", b""]
    kids = []
    font = 3 + 2 * len(contents)
    for i, content in enumerate(contents):
        stream = content.encode("ascii")
        page_id = 3 + 2 * i
        kids.append(page_id)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_id + 1} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
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


def squash(text: str) -> str:
    """pdfminer emits rotated-page text letter by letter; compare without whitespace."""
    return "".join(text.split())


@unittest.skipUnless(ADMIN_URL and ENABLED, "needs LAB_TEST_DATABASE_URL and LAB_E2E_REAL_UNSTRUCTURED=1")
class ParseEndToEndTests(FreshDatabase):
    @classmethod
    def tearDownClass(cls):
        path = os.environ.get("LAB_E2E_REPORT")
        if path:
            Path(path).write_text(json.dumps(REPORT, indent=2, sort_keys=True))
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = {"LAB_ENV": "test", "LAB_PROVIDER_MODE": "mock", "LAB_DATABASE_URL": self.url,
               "LAB_ARTIFACT_ROOT": str(self.root), "LAB_AUTH_SECRET": "top-secret-signing-key-for-tests-only",
               "LAB_DB_CONNECT_TIMEOUT_S": "5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.client = TestClient(create_app(settings=settings, checks={}))
        self.secret = get_signing_secret(settings)
        self.store = LocalArtifactStore(self.root)
        with self.connect() as c:
            self.project, self.owner = self.make_project(c)

    def headers(self, **extra):
        return {"Authorization": f"Bearer {issue_actor_token(self.owner, self.secret)}", **extra}

    def upload_and_queue(self, pdf: bytes):
        res = self.client.post(f"/api/v1/documents?project_id={self.project}&logical_id=e{uuid.uuid4().hex[:6]}",
                               headers=self.headers(**{"Content-Type": "application/pdf"}), content=pdf)
        self.assertEqual(res.status_code, 201, res.text)
        doc = uuid.UUID(res.json()["id"])
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(), json={})
        self.assertEqual(res.status_code, 202, res.text)
        return doc, uuid.UUID(res.json()["job_id"])

    def consume(self, job, **kw):
        with self.connect() as c:
            return process_parse_job(c, self.project, job, store=self.store, persist=make_persist(self.store),
                                     worker="e2e", max_pdf_bytes=64 * 1024 * 1024, connect=self.connect, **kw)

    def page(self, doc, job, index):
        res = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}/pages/{index}", headers=self.headers())
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_the_consumer_defaults_are_the_real_batcher_and_runner(self):
        """The acceptance tests call ``process_parse_job`` without a ``run``; make sure that default is the
        real thing and not a double that a refactor could have swapped in."""
        import inspect
        from lab_api import parse_worker
        from lab_parser.batch_parse import parse_in_batches
        from lab_parser.unstructured_runner import run_unstructured_fast
        self.assertIs(inspect.signature(parse_worker.process_parse_job).parameters["run"].default, parse_in_batches)
        self.assertIs(inspect.signature(parse_in_batches).parameters["run_batch"].default, run_unstructured_fast)
        self.assertEqual(md.version("unstructured"), "0.27.36")

    def test_f01_21_pages_real_parse_to_page_results_and_an_idempotent_rerun(self):
        pdf = synthetic_pdf.build(21)
        doc, job = self.upload_and_queue(pdf)
        began = time.monotonic()
        outcome = self.consume(job)
        wall = time.monotonic() - began
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "parsed"))
        summary = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}", headers=self.headers()).json()
        self.assertEqual((summary["parser"], summary["parser_version"], summary["strategy"]),
                         ("unstructured", "0.27.36", "fast"))
        self.assertEqual([p["page_index"] for p in summary["pages"]], list(range(21)))
        texts = []
        for i in range(21):
            body = self.page(doc, job, i)
            seen = squash(" ".join(o["raw_content"] for o in body["observations"]))
            self.assertIn(squash(f"page {i + 1}"), seen,
                          f"page {i + 1} text missing (status {body['status']}, diagnostics {body['diagnostics']})")
            token = squash(synthetic_pdf._page_text(i)[3])
            self.assertIn(token, seen, f"page {i + 1} token missing")
            self.assertTrue(body["evidence"], f"page {i + 1} has no evidence on a plain page")
            for unit in body["evidence"]:
                x0, y0, x1, y1 = unit["regions"][0]["bbox"]
                self.assertTrue(0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1, unit["regions"][0]["bbox"])
                self.assertEqual(unit["regions"][0]["page_index"], i)
            texts.append(seen)
        # A finished job cannot be claimed again; a new request re-parses with identical text.
        with self.assertRaises(jobs.LeaseUnavailable):
            self.consume(job)
        res = self.client.post(f"/api/v1/documents/{doc}/parse", headers=self.headers(), json={})
        self.assertEqual(res.status_code, 202, res.text)
        job2 = uuid.UUID(res.json()["job_id"])
        self.assertEqual(self.consume(job2).status, "succeeded")
        again = [squash(" ".join(o["raw_content"] for o in self.page(doc, job2, i)["observations"])) for i in range(21)]
        self.assertEqual(again, texts)
        REPORT["f01_21_pages"] = {"unstructured": md.version("unstructured"), "pdfminer.six": md.version("pdfminer.six"),
                                  "pypdf": md.version("pypdf"), "wall_seconds_first_job": round(wall, 2),
                                  "pages": 21, "pdf_bytes": len(pdf)}

    def test_upright_and_crop_box_pages_get_geometry_and_sideways_pages_keep_only_text(self):
        pdf = hand_built_pdf([(["Plain page alpha"], ""), (["Rotated page bravo"], "/Rotate 90"),
                              (["Cropped page charlie"], "/CropBox [50 50 662 842]")])
        doc, job = self.upload_and_queue(pdf)
        self.assertEqual(self.consume(job).status, "succeeded")
        pages = [self.page(doc, job, i) for i in range(3)]
        self.assertTrue(pages[0]["evidence"])
        self.assertTrue(pages[2]["evidence"], "a CropBox page is attested through the translated frame")
        self.assertEqual(pages[1]["evidence"], [], "a sideways (/Rotate 90) page must carry no geometry")
        for index, word in ((1, "bravo"), (2, "charlie")):
            self.assertIn(word, squash(" ".join(o["raw_content"] for o in pages[index]["observations"])))
        REPORT["mixed_pages"] = {"evidence_pages": [i for i, p in enumerate(pages) if p["evidence"]],
                                 "synthetic": True}

    def test_translated_boxes_match_what_pdfium_renders(self):
        """Validation of the CropBox/180 translation against PDFium (PM list item D): the union of the
        evidence boxes must contain the ink PDFium draws, within a tolerance for pdfminer's line-box
        padding. Sideways pages (90/270) must stay without geometry."""
        import numpy as np
        import pypdfium2
        from lab_parser.isolation import inspect_geometry
        from lab_parser.unstructured_runner import run_unstructured_fast
        lines = ["Alpha bravo charlie delta", "Echo foxtrot golf hotel", "India juliet kilo lima"]
        cases = {"plain": "", "crop_offset_same_size": "/CropBox [50 50 662 842]", "crop_smaller": "/CropBox [40 400 500 780]",
                 "rotate_180": "/Rotate 180", "rotate_180_crop": "/Rotate 180 /CropBox [40 400 500 780]",
                 "nonzero_origin": "/MediaBox [50 50 662 842]",
                 "nonzero_origin_180_crop": "/MediaBox [50 50 662 842] /Rotate 180 /CropBox [60 450 560 800]"}
        sideways = {"rotate_90": "/Rotate 90", "rotate_270": "/Rotate 270", "rotate_90_crop": "/Rotate 90 /CropBox [40 400 500 780]"}
        measured = {}
        for name, extra in {**cases, **sideways}.items():
            pdf = hand_built_pdf([(lines, extra)])
            geometry = inspect_geometry(pdf)
            width, height = geometry.pages[0].width_pt, geometry.pages[0].height_pt
            batch = run_unstructured_fast(pdf, geometry=geometry, document_id="d", budget_ref="b", operation_id="op-" + name)
            boxes = [e.regions[0].bbox for e in batch.evidence]
            document = pypdfium2.PdfDocument(pdf)
            page = document[0]
            pixels = np.asarray(page.render(scale=1).to_numpy())
            page.close()
            document.close()
            ink = np.argwhere(pixels[:, :, :3].min(axis=2) < 160)
            ink_box = [float(ink[:, 1].min()), float(ink[:, 0].min()), float(ink[:, 1].max() + 1), float(ink[:, 0].max() + 1)]
            union = None
            if boxes:
                union = [min(b[0] for b in boxes) * width, min(b[1] for b in boxes) * height,
                         max(b[2] for b in boxes) * width, max(b[3] for b in boxes) * height]
            measured[name] = {"display": [width, height], "evidence_union": union, "pdfium_ink": ink_box,
                              "attested": bool(boxes)}
            if name in sideways:
                self.assertEqual(boxes, [], f"{name} must stay without geometry")
                continue
            self.assertIsNotNone(union, f"{name}: no evidence")
            tolerance_x, tolerance_y = 2.0, 3.0   # measured error is 1-2 pt (pdfminer boxes include the font ascent/descent)
            self.assertLessEqual(abs(union[0] - ink_box[0]), tolerance_x, f"{name} left {measured[name]}")
            self.assertLessEqual(abs(union[2] - ink_box[2]), tolerance_x, f"{name} right {measured[name]}")
            self.assertLessEqual(abs(union[1] - ink_box[1]), tolerance_y, f"{name} top {measured[name]}")
            self.assertLessEqual(abs(union[3] - ink_box[3]), tolerance_y, f"{name} bottom {measured[name]}")
        REPORT["bbox_validation_against_pdfium"] = {"synthetic": True, "cases": measured}

    def test_two_columns_and_a_ruled_table_characterization(self):
        """Characterization of what real Unstructured fast extracts from layout it was not built for.
        Assertions are the safe invariants; everything observed goes into the report."""
        doc, job = self.upload_and_queue(layout_pdf())
        self.assertEqual(self.consume(job).status, "succeeded")
        observed = {}
        for index in range(2):
            body = self.page(doc, job, index)
            observed[index] = {
                "status": body["status"],
                "elements": [[o["upstream_type"], o["raw_content"]] for o in body["observations"]],
                "evidence": [[e["raw_content"], e["regions"][0]["bbox"]] for e in body["evidence"]],
            }
            for unit in body["evidence"]:
                x0, y0, x1, y1 = unit["regions"][0]["bbox"]
                self.assertTrue(0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)
        page1 = squash(" ".join(t for _, t in observed[0]["elements"]))
        for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"):
            self.assertIn(word, page1, f"{word} lost from the two-column page")
        page2 = squash(" ".join(t for _, t in observed[1]["elements"]))
        # Exactly these element texts, no more and no fewer (the earlier substring check did not cover "7").
        expected = ["Synthetic price table TABLEMARK", "Item", "Quantity", "UnitPrice", "Widget", "12", "3.50",
                    "Gadget", "7", "10.25", "Gizmo", "100", "0.75"]
        self.assertEqual(sorted(t for _, t in observed[1]["elements"]), sorted(expected))
        # Observed on CI (run 42692463217) and pinned so a change in the library's behaviour is noticed:
        # the two columns stay separate reading units, in left-to-right order, with disjoint boxes.
        left, right = observed[0]["evidence"]
        self.assertIn("LEFTCOL", left[0])
        self.assertNotIn("RIGHTCOL", left[0])
        self.assertIn("RIGHTCOL", right[0])
        self.assertNotIn("LEFTCOL", right[0])
        self.assertLess(left[1][2], right[1][0], "left column box must end before the right column starts")
        # The fast strategy has no table model: cells come out as separate elements, not a Table, and
        # nothing in the result says which cells share a row. (hi_res would; it is blocked here.)
        types = {t for t, _ in observed[1]["elements"]}
        self.assertNotIn("Table", types)
        self.assertGreaterEqual(len(observed[1]["elements"]), 12)
        REPORT["layout_characterization"] = {"synthetic": True, "pages": observed}

    def test_bbox_frames_of_real_unstructured_versus_pdfium_measurement(self):
        """MEASUREMENT for the rotated/CropBox question (PM list item D). No translation is applied and
        nothing here makes a page attestable: it records, per page geometry, where real pdfminer/Unstructured
        puts the text versus where PDFium puts it, so a translation can be derived and validated later."""
        import tempfile as tmp
        import pypdfium2
        from unstructured.partition.pdf import partition_pdf
        cases = {
            "plain": "", "crop_offset_same_size": "/CropBox [50 50 662 842]", "crop_smaller": "/CropBox [100 100 400 500]",
            "rotate_90": "/Rotate 90", "rotate_180": "/Rotate 180", "rotate_270": "/Rotate 270",
            "rotate_90_crop_smaller": "/Rotate 90 /CropBox [100 100 400 500]",
        }
        measured = {}
        for name, extra in cases.items():
            pdf = hand_built_pdf([(["Alpha bravo charlie", "Delta echo foxtrot", "Golf hotel india"], extra)])
            with tmp.NamedTemporaryFile(suffix=".pdf") as handle:
                handle.write(pdf)
                handle.flush()
                elements = partition_pdf(filename=handle.name, strategy="fast")
            boxes, layout = [], None
            for element in elements:
                coords = element.metadata.coordinates
                if coords is None:
                    continue
                layout = [coords.system.width, coords.system.height]
                xs = [pt[0] for pt in coords.points]
                ys = [pt[1] for pt in coords.points]
                boxes.append([min(xs), min(ys), max(xs), max(ys)])
            document = pypdfium2.PdfDocument(pdf)
            page = document[0]
            textpage = page.get_textpage()
            rects = [textpage.get_rect(i) for i in range(textpage.count_rects())]
            measured[name] = {
                "unstructured": {"layout": layout, "element_boxes": boxes,
                                 "union": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                           max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None,
                                 "texts": [e.text for e in elements]},
                "pdfium": {"display_size": list(page.get_size()), "rotation": page.get_rotation(),
                           "mediabox": list(page.get_mediabox()), "cropbox": list(page.get_cropbox()),
                           "text_rects_left_bottom_right_top": [list(r) for r in rects]},
            }
            page.close()
            document.close()
        REPORT["bbox_measurement"] = {"synthetic": True, "cases": measured}
        self.assertEqual(set(measured), set(cases))

    def test_cancel_stops_the_real_parser_and_stores_nothing(self):
        doc, job = self.upload_and_queue(synthetic_pdf.build(150))
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("outcome", self.consume(job)))
        worker.start()
        deadline = time.monotonic() + 60
        with self.connect() as c:
            while time.monotonic() < deadline:
                if c.execute("SELECT status::text FROM jobs WHERE id = %s", (job,)).fetchone()[0] == "running":
                    break
                c.rollback()
                time.sleep(0.1)
            time.sleep(1.0)  # let the child start
            with c.transaction():
                c.execute("SELECT set_config('lab.transition_reason', 'user cancel', true)")
                c.execute("UPDATE jobs SET status = 'cancel_requested', lease_owner = NULL, lease_token = NULL, "
                          "lease_expires_at = NULL, row_version = row_version + 1 WHERE id = %s", (job,))
        began = time.monotonic()
        worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["outcome"].status, "cancelled")
        self.assertLess(time.monotonic() - began, 15)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status::text FROM jobs WHERE id = %s", (job,)).fetchone()[0], "cancelled")
            self.assertEqual(c.execute("SELECT count(*) FROM parse_results WHERE job_id = %s", (job,)).fetchone()[0], 0)
        res = self.client.get(f"/api/v1/documents/{doc}/parse-results/{job}", headers=self.headers())
        self.assertEqual(res.status_code, 404)
        REPORT["cancel"] = {"outcome": "cancelled", "seconds_to_stop": round(time.monotonic() - began, 2)}


if __name__ == "__main__":
    unittest.main()
