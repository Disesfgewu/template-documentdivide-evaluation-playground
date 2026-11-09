"""Real synthetic-PDF participant preparation/retrieval, never provider mocks."""
import dataclasses
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
from fixtures.generators import synthetic_pdf, layout_pdf
from lab_contracts.method import QueryRequest, QueryResponse, PrepareResult
from lab_parser.local_methods import MethodConfig, prepare, query
from lab_parser.local_method_cli import read_regular


def pdf(pages):
    with patch.object(synthetic_pdf, "_page_text", side_effect=lambda i: pages[i]):
        return synthetic_pdf.build(len(pages))


def index(data, strategy="page", **kwargs):
    return prepare(data, document_id="doc", expected_sha256=hashlib.sha256(data).hexdigest(),
                   config=MethodConfig(strategy=strategy, **kwargs))


def request(idx, question="revenue", **budget):
    return QueryRequest.model_validate_json(json.dumps({"protocol_version": "1.0", "request_id": "request",
        "run_id": "run", "question_id": "question", "index_id": idx.index_id, "question": question,
        "budget": {"top_k": 10, "max_evidence_tokens": 12000, "max_visual_regions": 8,
                   "timeout_seconds": 180, **budget}}))


class LocalMethodsTests(unittest.TestCase):
    def test_two_strategies_parse_same_raw_pdf_with_distinct_chunks_and_indexes(self):
        data = pdf([["revenue orange revenue apple", "revenue pear"], ["cost blue"]])
        page = index(data)
        window = index(data, "window", window_chars=12)
        self.assertEqual(len(page.chunks), 2)
        self.assertGreater(len(window.chunks), len(page.chunks))
        self.assertNotEqual(page.index_id, window.index_id)
        self.assertNotEqual(page.fingerprint, window.fingerprint)
        for idx in (page, window):
            result = query(idx, request(idx))
            QueryResponse.model_validate_json(result.model_dump_json())
            self.assertEqual(result.check_binding(request(idx)), [])
            self.assertEqual(result.status, "abstained")
            self.assertEqual(result.answer.text, "")
            self.assertEqual(result.telemetry.model_calls, 0)
            self.assertTrue(result.retrieved_hits)
            for hit in result.retrieved_hits:
                region = hit.regions[0]
                self.assertEqual(region.page_index, 0)
                self.assertEqual(region.bbox, (0., 0., 1., 1.))
                self.assertEqual(region.quote, hit.content)
                self.assertEqual(region.document_sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(region.check_against_document(idx.document_sha256, idx.page_count), [])

    def test_fingerprint_deterministic_covers_raw_identity_and_all_config(self):
        data = pdf([["revenue"]])
        first = index(data)
        self.assertEqual(first, index(data))
        changed = index(data, max_chunks=99)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        self.assertNotEqual(first.manifest().method_revision, changed.manifest().method_revision)
        with patch("lab_parser.local_methods.version", return_value="different-parser"):
            different = index(data)
        self.assertNotEqual(first.fingerprint, different.fingerprint)
        self.assertNotEqual(first.manifest().method_revision, different.manifest().method_revision)
        self.assertNotEqual(first.fingerprint, index(pdf([["different revenue"]])).fingerprint)
        other = prepare(data, document_id="other", expected_sha256=first.document_sha256)
        self.assertNotEqual(first.fingerprint, other.fingerprint)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.fingerprint = "modified"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.chunks[0].text = "modified"
        manifest = PrepareResult.model_validate_json(first.manifest().model_dump_json())
        self.assertEqual(manifest.method_revision.method_id, "local-page")
        self.assertEqual(manifest.document_hashes, {"doc": first.document_sha256})

    def test_bad_hash_rejected_before_parsing_and_encryption_rejected(self):
        data = pdf([["revenue"]])
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            prepare(data, document_id="doc", expected_sha256="0" * 64)
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(data)))
        writer.encrypt("secret")
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaisesRegex(ValueError, "encryption"):
            index(stream.getvalue())
        for bad in (b"", b"not pdf", b"%PDF-broken"):
            with self.assertRaises(ValueError):
                index(bad)

    def test_page_text_chunk_and_pdf_limits_fail_without_partial_index(self):
        data = pdf([["revenue one", "revenue two"], ["second page"]])
        for limits, reason in (({"max_pdf_bytes": 1}, "oversized"),
                               ({"max_pages": 1}, "page_limit"),
                               ({"max_page_chars": 1}, "text_limit"),
                               ({"max_total_chars": 1}, "text_limit"),
                               ({"max_chunks": 1}, "chunk_limit")):
            with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, reason):
                index(data, **limits)

    def test_blank_pages_preserved_in_coverage_and_never_fabricated(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        stream = io.BytesIO()
        writer.write(stream)
        idx = index(stream.getvalue())
        self.assertEqual(idx.empty_pages, (0,))
        self.assertEqual(idx.chunks, ())
        result = query(idx, request(idx))
        self.assertEqual(result.retrieved_hits, [])
        self.assertEqual(idx.manifest().stats["empty_native_text_pages"], [0])

    def test_budget_is_ranked_prefix_never_skip_and_backfill(self):
        idx = index(pdf([["revenue " * 20], ["revenue"]]))
        # Both have intersection score=1; original page order breaks the tie.
        result = query(idx, request(idx, max_evidence_tokens=20))
        self.assertEqual(result.retrieved_hits, [])
        self.assertEqual(result.retrieval_trace[0]["stop_reason"], "evidence_budget_prefix_exhausted")
        cost = len(idx.chunks[0].text.encode())
        exact = query(idx, request(idx, max_evidence_tokens=cost))
        self.assertEqual(len(exact.retrieved_hits), 1)
        self.assertEqual(exact.retrieved_hits[0].regions[0].page_index, 0)
        one = query(idx, request(idx, top_k=1))
        self.assertEqual(len(one.retrieved_hits), 1)

    def test_page_regions_consume_visual_budget_even_for_native_text(self):
        idx = index(pdf([["revenue"], ["revenue"]]))
        zero = query(idx, request(idx, max_visual_regions=0))
        self.assertEqual(zero.retrieved_hits, [])
        self.assertEqual(zero.retrieval_trace[0]["stop_reason"], "visual_budget_prefix_exhausted")
        one = query(idx, request(idx, max_visual_regions=1))
        self.assertEqual(len(one.retrieved_hits), 1)
        self.assertEqual(one.retrieved_hits[0].regions[0].page_index, 0)
        two = query(idx, request(idx, max_visual_regions=2))
        self.assertEqual(len(two.retrieved_hits), 2)

    def test_deadline_applies_to_empty_and_unmatched_indexes(self):
        idx = index(pdf([["revenue"]]))
        for question in ("unmatched", "revenue"):
            with patch("lab_parser.local_methods.time.monotonic", side_effect=[0., 2.]):
                with self.assertRaises(TimeoutError):
                    query(idx, request(idx, question, timeout_seconds=1))
        empty = dataclasses.replace(idx, chunks=())
        with patch("lab_parser.local_methods.time.monotonic", side_effect=[0., 2.]):
            with self.assertRaises(TimeoutError):
                query(empty, request(empty, timeout_seconds=1))

    def test_rank_ties_stable_unmatched_abstains_and_binding_required(self):
        idx = index(pdf([["revenue"], ["revenue"], ["profit revenue"]]))
        result = query(idx, request(idx, "profit revenue"))
        self.assertEqual([h.regions[0].page_index for h in result.retrieved_hits], [2, 0, 1])
        self.assertEqual([h.rank for h in result.retrieved_hits], [1, 2, 3])
        self.assertEqual(query(idx, request(idx, "unmatched")).retrieved_hits, [])
        bad = request(idx).model_copy(update={"index_id": "wrong"})
        with self.assertRaisesRegex(ValueError, "index_id_mismatch"):
            query(idx, bad)

    def test_nontrivial_crop_rejected_instead_of_claiming_invisible_text(self):
        data = pdf([["revenue outside crop"]])
        reader = PdfReader(io.BytesIO(data))
        reader.pages[0].cropbox.lower_left = (100, 100)
        reader.pages[0].cropbox.upper_right = (200, 200)
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaisesRegex(ValueError, "cropped_page_not_supported"):
            index(stream.getvalue())

    def test_real_f02_crop_rejected_and_full_media_rotations_retain_pages(self):
        data = layout_pdf.build()
        with self.assertRaisesRegex(ValueError, "cropped_page_not_supported"):
            index(data)
        reader = PdfReader(io.BytesIO(data))
        writer = PdfWriter()
        for page in reader.pages[:4]:
            writer.add_page(page)
        stream = io.BytesIO()
        writer.write(stream)
        idx = index(stream.getvalue())
        self.assertEqual(idx.page_count, 4)
        for page in range(4):
            self.assertTrue(any(c.page_index == page and f"MARK-{page}-L" in c.text for c in idx.chunks))
        response = query(idx, request(idx, "MARK"))
        self.assertEqual([h.regions[0].page_index for h in response.retrieved_hits], [0, 1, 2, 3])

    def test_extraction_warning_rejects_partial_text_and_does_not_disclose_pdf_operand(self):
        reader = PdfReader(io.BytesIO(pdf([["revenue"]])))
        page = reader.pages[0]
        page["/Resources"][NameObject("/XObject")] = DictionaryObject()
        stream = DecodedStreamObject()
        stream.set_data(page.get_contents().get_data() + b"\n/SYNTHETIC-SECRET-CONTENT Do\n")
        page[NameObject("/Contents")] = stream
        writer = PdfWriter()
        writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
        data = output.getvalue()
        logger = logging.getLogger("pypdf._page")
        previous = (logger.disabled, logger.level, list(logger.filters))
        logger.disabled = True
        logger.setLevel(logging.ERROR)
        blocker = logging.Filter()
        blocker.filter = lambda record: False
        logger.addFilter(blocker)
        try:
            with self.assertRaisesRegex(ValueError, "extraction_diagnostics"):
                index(data)
            self.assertTrue(logger.disabled)
            self.assertEqual(logger.level, logging.ERROR)
            self.assertEqual(logger.filters, previous[2] + [blocker])
        finally:
            logger.disabled, logger.level, logger.filters = previous
        old_disable = logging.root.manager.disable
        try:
            logging.disable(logging.WARNING)
            with self.assertRaisesRegex(ValueError, "monitoring_unavailable"):
                index(data)
        finally:
            logging.disable(old_disable)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "bad.pdf"
            source.write_bytes(data)
            result = subprocess.run([sys.executable, "-m", "lab_parser.local_method_cli", "prepare",
                                     "--pdf", str(source), "--sha256", hashlib.sha256(data).hexdigest(),
                                     "--document-id", "doc", "--strategy", "page"],
                                    env={**os.environ, "PYTHONPATH": "services/parser:contracts"},
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(b"SYNTHETIC-SECRET", result.stderr)

    def test_config_strict_bounds(self):
        for kwargs in ({"strategy": "unknown"}, {"window_chars": 0}, {"window_chars": True},
                       {"max_pages": 2001}, {"max_chunks": 10001}, {"max_page_chars": 32001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MethodConfig(**kwargs)

    def test_cli_prepare_query_strict_json_and_bounded_regular_files(self):
        data = pdf([["revenue"]])
        idx = index(data)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            source.write_bytes(data)
            req = root / "request.json"
            req.write_text(request(idx).model_dump_json())
            command = [sys.executable, "-m", "lab_parser.local_method_cli"]
            args = ["--pdf", str(source), "--sha256", idx.document_sha256,
                    "--document-id", "doc", "--strategy", "page"]
            env = {**os.environ, "PYTHONPATH": "services/parser:contracts"}
            prepared = subprocess.run(command + ["prepare"] + args, capture_output=True, env=env, timeout=10)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            self.assertEqual(json.loads(prepared.stdout)["index_id"], idx.index_id)
            queried = subprocess.run(command + ["query"] + args + ["--request", str(req)],
                                     capture_output=True, env=env, timeout=10)
            self.assertEqual(queried.returncode, 0, queried.stderr)
            QueryResponse.model_validate_json(queried.stdout)
            for raw in ('{"request_id":"a","request_id":"b"}', '{"x":NaN}', '{"x":1e999}', '{}'):
                req.write_text(raw)
                bad = subprocess.run(command + ["query"] + args + ["--request", str(req)],
                                     capture_output=True, env=env, timeout=10)
                self.assertEqual(bad.returncode, 2)
                self.assertEqual(bad.stdout, b"")
                self.assertNotIn(str(source).encode(), bad.stderr)
            with self.assertRaises(ValueError):
                read_regular(str(source), 1)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                read_regular(str(fifo), 100)
            with self.assertRaises(ValueError):
                read_regular(str(root), 100)


if __name__ == "__main__":
    unittest.main()
