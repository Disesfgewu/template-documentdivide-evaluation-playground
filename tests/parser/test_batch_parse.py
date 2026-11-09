"""Batch orchestration with SYNTHETIC per-batch runs built by the real normalizer (Refs #7).

The fake ``run_batch`` stands in for the parser child: it returns what the runner returns for the
requested pages, so the planning, retry, merge and cancel logic is the real code under test."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time
import unittest

from lab_contracts.parse import ModelRevision, PageMapEntry, ParseManifest, ParseRequest, RenderSettings
from lab_parser.batch_coverage import BatchLimits
from lab_parser.batch_parse import PartialParseFailure, parse_in_batches
from lab_parser.isolation import InspectionError
from lab_parser.normalize import ADAPTER_REVISION, DisplayFrame, UnstructuredParser, UnstructuredProfile
from tests.parser.pdfgen import build_pdf

PAGES = 21


def element(local_page, text):
    return {"type": "NarrativeText", "element_id": f"e{local_page}", "text": text, "metadata": {
        "page_number": local_page, "coordinates": {"system": "PixelSpace", "layout_width": 595, "layout_height": 842,
                                                   "points": [[61.2, 79.2], [61.2, 158.4], [306, 158.4], [306, 79.2]]}}}


def fake_batch(page_indices, document_id, operation_id, budget_ref, blank=()):
    """What run_unstructured_fast returns for a page subset of a PAGES-page document."""
    now = datetime.now(timezone.utc)
    elements = [element(k + 1, f"text of page {p}") for k, p in enumerate(page_indices) if p not in blank]
    request = ParseRequest(document_artifact_ref="sha256:" + "a" * 64, page_indices=list(page_indices),
                           profile_revision="unstructured-oss-fast", budget_ref=budget_ref, operation_id=operation_id)
    manifest = ParseManifest(
        parser_name="unstructured", package_or_service_version="0.27.36", adapter_revision=ADAPTER_REVISION,
        output_schema="unstructured.elements-0.27.36",
        model_revisions=[ModelRevision(name="core", revision="0.27.36", checksum=None)],
        requested_profile="unstructured-oss-fast", effective_profile="unstructured-oss-fast", fallback_reason=None,
        ocr_languages=["en"], page_selection=list(page_indices),
        page_index_map=[PageMapEntry(parser_page=k + 1, page_index=p) for k, p in enumerate(page_indices)],
        render_settings=RenderSettings(dpi=150, max_megapixels=16.0), postprocess_flags=[], endpoint_deployment_id=None,
        started_at=now, finished_at=now, cost_usd=0.0, warnings=[], reproducible=True)
    frames = {p: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=595.0, height=842.0,
                              transform_ref=f"tr-{p}") for p in page_indices}
    return UnstructuredParser().normalize(
        json.dumps(elements).encode(), request, manifest, UnstructuredProfile(strategy="fast"),
        document_id=document_id, document_sha256="a" * 64, frames=frames, document_page_count=PAGES)


class Recorder:
    def __init__(self, behaviour=None, delay=0.0):
        self.calls, self.behaviour, self.delay = [], behaviour, delay
        self.lock = threading.Lock()
        self.running = self.peak = 0

    def __call__(self, data, *, geometry, page_indices, cancelled, python, document_id, budget_ref, operation_id):
        with self.lock:
            self.calls.append(tuple(page_indices))
            nth = sum(1 for c in self.calls if c == tuple(page_indices))
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
            if self.behaviour:
                got = self.behaviour(tuple(page_indices), nth)
                if got is not None:
                    return got
            return fake_batch(page_indices, document_id, operation_id, budget_ref)
        finally:
            with self.lock:
                self.running -= 1


class BatchParseTests(unittest.TestCase):
    def setUp(self):
        self.pdf = build_pdf([{"mediabox": (0, 0, 595, 842)}] * PAGES)

    def parse(self, run, **kw):
        return parse_in_batches(self.pdf, document_id="doc-batch", budget_ref="b", operation_id="op", run_batch=run, **kw)

    def test_a_21_page_document_is_parsed_in_three_core_batches_and_merged_in_original_order(self):
        run = Recorder()
        merged = self.parse(run)
        self.assertEqual(sorted(run.calls), [tuple(range(0, 10)), tuple(range(10, 20)), (20,)])
        self.assertEqual([p.page_index for p in merged.result.pages], list(range(PAGES)))
        self.assertTrue(all(p.status == "ok" for p in merged.result.pages))
        self.assertEqual(len(merged.evidence), PAGES)
        for unit in merged.evidence:  # original physical indices survive the sub-document numbering
            page = unit.regions[0].page_index
            self.assertEqual(unit.raw_content, f"text of page {page}")
        self.assertEqual(merged.manifest.page_selection, list(range(PAGES)))
        self.assertEqual(merged.result.normalized_evidence_refs, [e.evidence_id for e in merged.evidence])
        self.assertEqual(len(set(merged.result.normalized_evidence_refs)), PAGES)  # no duplicates
        refs = {a.ref for a in merged.artifacts}
        self.assertTrue(set(merged.result.raw_result_refs) <= refs and merged.result.manifest_ref in refs)

    def test_at_most_two_batches_run_at_once(self):
        run = Recorder(delay=0.3)
        self.parse(run)
        self.assertEqual(run.peak, 2)

    def test_overlap_pages_are_never_parsed_twice(self):
        run = Recorder()
        merged = self.parse(run, limits=BatchLimits(batch_size=10, overlap_pages=2, parallel_batches=2))
        flat = [p for call in run.calls for p in call]
        self.assertEqual(sorted(flat), list(range(PAGES)))   # each page exactly once: core pages only
        self.assertEqual(len(merged.evidence), PAGES)

    def test_a_failing_batch_is_retried_page_by_page_and_the_result_is_complete(self):
        def behaviour(pages, nth):
            if pages == tuple(range(10, 20)) and nth == 1:
                raise InspectionError("timeout")
        run = Recorder(behaviour)
        merged = self.parse(run)
        singles = [c for c in run.calls if len(c) == 1 and c[0] in range(10, 20)]
        self.assertEqual(sorted(singles), [(p,) for p in range(10, 20)])
        self.assertEqual(len([c for c in run.calls if c == tuple(range(10, 20))]), 1)  # the batch itself is not re-run
        self.assertTrue(all(p.status == "ok" for p in merged.result.pages))
        self.assertEqual(len(merged.evidence), PAGES)

    def test_pages_that_stay_failed_after_the_retry_budget_publish_nothing(self):
        def behaviour(pages, nth):
            if set(pages) & set(range(10, 20)):
                raise InspectionError("child_failed")
        run = Recorder(behaviour)
        with self.assertRaises(PartialParseFailure) as ctx:
            self.parse(run, retry_rounds=2)
        self.assertEqual(ctx.exception.failed_pages, tuple(range(10, 20)))
        # Bounded work: 3 batches + 10 pages x 2 rounds, never more.
        self.assertEqual(len(run.calls), 3 + 10 * 2)

    def test_every_batch_failing_transiently_is_a_job_level_error_not_page_failures(self):
        def behaviour(pages, nth):
            raise InspectionError("timeout")
        run = Recorder(behaviour)
        with self.assertRaises(InspectionError) as ctx:
            self.parse(run)
        self.assertEqual(ctx.exception.code, "timeout")     # the consumer can requeue this exactly as before
        self.assertEqual(len(run.calls), 3)                  # no per-page retries

    def test_every_batch_failing_transiently_with_different_codes_is_still_a_job_level_error(self):
        codes = {tuple(range(0, 10)): "timeout", tuple(range(10, 20)): "process_unavailable", (20,): "inspection_io_failed"}

        def behaviour(pages, nth):
            raise InspectionError(codes[pages])
        run = Recorder(behaviour)
        with self.assertRaises(InspectionError) as ctx:
            self.parse(run)
        self.assertIn(ctx.exception.code, set(codes.values()))
        self.assertEqual(len(run.calls), 3)

    def test_an_unexpected_exception_stops_batches_that_have_not_started(self):
        started = []

        def behaviour(pages, nth):
            started.append(pages)
            raise RuntimeError("boom")
        run = Recorder(behaviour, delay=0.3)
        with self.assertRaises(RuntimeError):
            parse_in_batches(build_pdf([{"mediabox": (0, 0, 595, 842)}] * 41), document_id="doc-many", budget_ref="b",
                             operation_id="op", run_batch=run, limits=BatchLimits(batch_size=10, overlap_pages=0, parallel_batches=2))
        self.assertLessEqual(len(run.calls), 2 + 1)   # the two running children and at most one more that raced the flag

    def test_systemic_codes_stop_at_once(self):
        for code in ("unsupported_platform", "invalid_input"):
            with self.subTest(code):
                def behaviour(pages, nth, code=code):
                    raise InspectionError(code)
                run = Recorder(behaviour, delay=0.05)
                with self.assertRaises(InspectionError) as ctx:
                    self.parse(run)
                self.assertEqual(ctx.exception.code, code)
                self.assertLessEqual(len(run.calls), 3)

    def test_when_most_pages_failed_the_cause_is_systemic_and_no_page_is_retried(self):
        def behaviour(pages, nth):
            if pages != (20,):
                raise InspectionError("child_failed")
        run = Recorder(behaviour)
        with self.assertRaises(PartialParseFailure) as ctx:
            self.parse(run)
        self.assertEqual(len(ctx.exception.failed_pages), 20)
        self.assertEqual(len(run.calls), 3)                  # 20 of 21 pages failed: no 20 x rounds of retries

    def test_the_job_deadline_stops_the_work_before_more_children_start(self):
        run = Recorder(delay=0.4)
        with self.assertRaises(InspectionError) as ctx:
            self.parse(run, job_deadline_seconds=0.1)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertLess(len(run.calls), 3)

    def test_zero_retry_rounds_fail_straight_away(self):
        def behaviour(pages, nth):
            if pages == (20,):
                raise InspectionError("timeout")
        run = Recorder(behaviour)
        with self.assertRaises(PartialParseFailure) as ctx:
            self.parse(run, retry_rounds=0)
        self.assertEqual(ctx.exception.failed_pages, (20,))
        self.assertEqual(len(run.calls), 3)

    def test_a_quarantined_batch_fails_its_pages_and_is_retried(self):
        def behaviour(pages, nth):
            if pages == tuple(range(0, 10)) and nth == 1:
                request_ops = None
                bad = fake_batch(pages, "doc-batch", "op-x", "b")
                import dataclasses
                return dataclasses.replace(bad, quarantine_reason="invalid_unstructured_output", evidence=(), observations=())
        run = Recorder(behaviour)
        merged = self.parse(run)
        self.assertTrue(all(p.status == "ok" for p in merged.result.pages))

    def test_a_page_the_parser_could_not_confirm_stays_needs_review_and_is_not_a_failure(self):
        run = Recorder(lambda pages, nth: fake_batch(pages, "doc-batch", "op-y", "b", blank={5}) if 5 in pages else None)
        merged = self.parse(run)
        status = {p.page_index: p for p in merged.result.pages}
        self.assertEqual(status[5].status, "needs_review")
        self.assertEqual(len(merged.evidence), PAGES - 1)

    def test_cancel_stops_the_work_and_raises(self):
        flag = threading.Event()

        def behaviour(pages, nth):
            flag.set()
            time.sleep(0.2)
        run = Recorder(behaviour)
        thread_result = {}

        def go():
            try:
                self.parse(run, cancelled=flag.is_set)
            except InspectionError as exc:
                thread_result["code"] = exc.code
        worker = threading.Thread(target=go)
        worker.start()
        worker.join(20)
        self.assertEqual(thread_result.get("code"), "cancelled")
        self.assertLess(len(run.calls), 3 + 1)

    def test_a_single_page_document_is_one_batch(self):
        pdf = build_pdf([{"mediabox": (0, 0, 595, 842)}])
        calls = []

        def one(data, *, geometry, page_indices, cancelled, python, document_id, budget_ref, operation_id):
            calls.append(tuple(page_indices))
            return fake_batch(page_indices, document_id, operation_id, budget_ref)
        merged = parse_in_batches(pdf, document_id="d", budget_ref="b", operation_id="op", run_batch=one)
        self.assertEqual(calls, [(0,)])
        self.assertEqual(len(merged.result.pages), 1)

    def test_invalid_retry_budget_is_rejected(self):
        for bad in (-1, 6, True, 1.5):
            with self.subTest(bad), self.assertRaises(InspectionError):
                self.parse(Recorder(), retry_rounds=bad)


if __name__ == "__main__":
    unittest.main()
