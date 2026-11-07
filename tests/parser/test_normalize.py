"""Tests for pinned Unstructured 0.27.36 and MinerU 4.1.7 normalization boundaries.

All upstream payloads in these tests are synthetic upstream-shaped fixtures
created exclusively for deterministic contract verification. None represent
claimed real parser inference or verified gold data.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import unittest
import zlib

from lab_contracts.parse import (
    ModelRevision,
    PageMapEntry,
    ParseManifest,
    ParseRequest,
    RenderSettings,
)
from lab_parser.normalize import (
    ADAPTER_REVISION,
    DisplayFrame,
    MAX_RAW_BYTES,
    MinerUParser,
    MinerUProfile,
    UnstructuredParser,
    UnstructuredProfile,
)

SYNTHETIC_DOC_ID = "doc-synthetic-boundary-test"
SYNTHETIC_DOC_SHA256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _make_request(page_indices: list[int], profile_rev: str = "unstructured-oss-fast") -> ParseRequest:
    return ParseRequest(
        document_artifact_ref="sha256:" + "a" * 64,
        page_indices=page_indices,
        profile_revision=profile_rev,
        budget_ref="b-synthetic-01",
        operation_id="op-synthetic-01",
    )


def _make_unstructured_manifest(
    page_indices: list[int],
    *,
    strategy: str = "fast",
    version: str = "0.27.36",
    output_schema: str = "unstructured.elements-0.27.36",
    effective_profile: str | None = None,
    requested_profile: str | None = None,
    fallback_reason: str | None = None,
    endpoint_deployment_id: str | None = None,
    postprocess_flags: list[str] | None = None,
) -> ParseManifest:
    now = datetime.now(timezone.utc)
    eff = effective_profile or f"unstructured-oss-{strategy}"
    req_prof = requested_profile or eff
    return ParseManifest(
        parser_name="unstructured",
        package_or_service_version=version,
        adapter_revision=ADAPTER_REVISION,
        output_schema=output_schema,
        model_revisions=[ModelRevision(name="core", revision=version, checksum=None)],
        requested_profile=req_prof,
        effective_profile=eff,
        fallback_reason=fallback_reason,
        ocr_languages=["en"],
        page_selection=page_indices,
        page_index_map=[PageMapEntry(parser_page=p + 1, page_index=p) for p in page_indices],
        render_settings=RenderSettings(dpi=150, max_megapixels=16.0),
        postprocess_flags=postprocess_flags or [],
        endpoint_deployment_id=endpoint_deployment_id,
        started_at=now,
        finished_at=now,
        cost_usd=0.0,
        warnings=[],
        reproducible=True,
    )


def _make_mineru_manifest(
    page_indices: list[int],
    *,
    tier: str = "standard",
    version: str = "4.1.7",
    output_schema: str = "docvortex.middle-2.0",
    effective_profile: str | None = None,
    requested_profile: str | None = None,
    fallback_reason: str | None = None,
    postprocess_flags: list[str] | None = None,
    page_index_map: list[PageMapEntry] | None = None,
) -> ParseManifest:
    now = datetime.now(timezone.utc)
    eff = effective_profile or f"mineru-{tier}"
    req_prof = requested_profile or eff
    if page_index_map is None:
        page_index_map = [PageMapEntry(parser_page=p, page_index=p) for p in page_indices]
    return ParseManifest(
        parser_name="mineru",
        package_or_service_version=version,
        adapter_revision=ADAPTER_REVISION,
        output_schema=output_schema,
        model_revisions=[ModelRevision(name="mineru-core", revision=version, checksum=None)],
        requested_profile=req_prof,
        effective_profile=eff,
        fallback_reason=fallback_reason,
        ocr_languages=["en"],
        page_selection=page_indices,
        page_index_map=page_index_map,
        render_settings=RenderSettings(dpi=150, max_megapixels=16.0),
        postprocess_flags=postprocess_flags or [],
        endpoint_deployment_id=None,
        started_at=now,
        finished_at=now,
        cost_usd=0.0,
        warnings=[],
        reproducible=True,
    )


def _default_frames(page_indices: list[int]) -> dict[int, DisplayFrame]:
    return {
        p: DisplayFrame(
            coordinate_frame="rotated_cropbox_top_left",
            width=612.0,
            height=792.0,
            transform_ref=f"tr-p{p}",
        )
        for p in page_indices
    }


class TestUnstructuredPositive(unittest.TestCase):
    """Positive contract tests for strict pinned Unstructured OSS 0.27.36 adapter."""

    def setUp(self):
        self.parser = UnstructuredParser()

    def test_fast_strategy_narrative_text(self):
        """Synthetic fast strategy text extraction with PixelSpace coordinates."""
        payload = [
            {
                "type": "NarrativeText",
                "text": "Synthetic Unstructured narrative paragraph.",
                "metadata": {
                    "page_number": 1,
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 612.0,
                        "layout_height": 792.0,
                        "points": [[61.2, 79.2], [550.8, 158.4]],
                    },
                },
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "unstructured-oss-fast")
        manifest = _make_unstructured_manifest([0], strategy="fast")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )

        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.result.pages), 1)
        self.assertEqual(batch.result.pages[0].status, "ok")
        self.assertEqual(batch.result.pages[0].diagnostics, [])
        self.assertEqual(len(batch.evidence), 1)

        ev = batch.evidence[0]
        self.assertEqual(ev.raw_content, "Synthetic Unstructured narrative paragraph.")
        self.assertEqual(ev.normalized_content, "Synthetic Unstructured narrative paragraph.")
        self.assertEqual(ev.extractor_revision, ADAPTER_REVISION)
        self.assertIsNone(ev.ocr_confidence)
        self.assertEqual(len(ev.regions), 1)

        reg = ev.regions[0]
        self.assertEqual(reg.page_index, 0)
        self.assertEqual(reg.kind.value, "text")
        self.assertAlmostEqual(reg.bbox[0], 0.1)
        self.assertAlmostEqual(reg.bbox[1], 0.1)
        self.assertAlmostEqual(reg.bbox[2], 0.9)
        self.assertAlmostEqual(reg.bbox[3], 0.2)

        # Artifact lineage: raw bytes SHA256 preserved
        expected_raw_ref = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertIn(expected_raw_ref, batch.result.raw_result_refs)
        self.assertEqual(batch.artifacts[0].ref, expected_raw_ref)

        # Observations preserve upstream pointer and type
        self.assertEqual(len(batch.observations), 1)
        self.assertEqual(batch.observations[0].source_pointer, "/0")
        self.assertEqual(batch.observations[0].upstream_type, "NarrativeText")
        self.assertEqual(batch.observations[0].evidence_id, ev.evidence_id)

    def test_hi_res_table_and_image(self):
        """Synthetic hi_res strategy table with HTML and image asset reference."""
        table_html = "<table><tr><td>Synthetic Cell</td></tr></table>"
        payload = [
            {
                "type": "Table",
                "text": "Synthetic Table Text",
                "metadata": {
                    "page_number": 1,
                    "text_as_html": table_html,
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 612.0,
                        "layout_height": 792.0,
                        "points": [[100.0, 100.0], [500.0, 300.0]],
                    },
                },
            },
            {
                "type": "Image",
                "text": "",
                "metadata": {
                    "page_number": 1,
                    "image_path": "/tmp/synthetic-figure.png",
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 612.0,
                        "layout_height": 792.0,
                        "points": [[100.0, 350.0], [500.0, 600.0]],
                    },
                },
            },
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "unstructured-oss-hi_res")
        manifest = _make_unstructured_manifest([0], strategy="hi_res")
        frames = _default_frames([0])
        asset_refs = {"/tmp/synthetic-figure.png": "sha256:" + "f" * 64}

        profile = UnstructuredProfile(strategy="hi_res", infer_table_structure=True)
        batch = self.parser.normalize(
            raw, req, manifest, profile,
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1, asset_refs=asset_refs,
        )

        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.evidence), 2)
        # Table region kind
        self.assertEqual(batch.evidence[0].regions[0].kind.value, "table")
        self.assertEqual(batch.observations[0].text_as_html, table_html)
        # Image region kind and resolved asset reference
        self.assertEqual(batch.evidence[1].regions[0].kind.value, "image")
        self.assertEqual(batch.evidence[1].asset_refs, ["sha256:" + "f" * 64])

    def test_chunking_orig_elements_lineage(self):
        """Synthetic CompositeElement unpacking orig_elements without overwriting."""
        child_elements = [
            {
                "type": "Title",
                "text": "Synthetic Section Title",
                "metadata": {
                    "page_number": 1,
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 612.0,
                        "layout_height": 792.0,
                        "points": [[61.2, 50.0], [550.8, 100.0]],
                    },
                },
            },
            {
                "type": "NarrativeText",
                "text": "Synthetic chunked body sentence.",
                "metadata": {
                    "page_number": 1,
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 612.0,
                        "layout_height": 792.0,
                        "points": [[61.2, 110.0], [550.8, 200.0]],
                    },
                },
            },
        ]
        child_json = json.dumps(child_elements).encode("utf-8")
        encoded_orig = base64.b64encode(zlib.compress(child_json)).decode("ascii")

        payload = [
            {
                "type": "CompositeElement",
                "text": "Synthetic Section Title\n\nSynthetic chunked body sentence.",
                "metadata": {
                    "orig_elements": encoded_orig,
                },
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "unstructured-oss-fast")
        manifest = _make_unstructured_manifest([0], strategy="fast")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )

        self.assertIsNone(batch.quarantine_reason)
        # Exactly 2 evidence units from child elements; none from CompositeElement itself
        self.assertEqual(len(batch.evidence), 2)
        self.assertEqual(batch.evidence[0].raw_content, "Synthetic Section Title")
        self.assertEqual(batch.evidence[1].raw_content, "Synthetic chunked body sentence.")

        # Both raw artifact and child orig_elements artifact are preserved
        child_ref = "sha256:" + hashlib.sha256(child_json).hexdigest()
        self.assertIn(child_ref, batch.result.raw_result_refs)
        self.assertTrue(any(a.ref == child_ref for a in batch.artifacts))

        # CompositeElement observation preserved with evidence_id=None
        chunk_obs = [o for o in batch.observations if o.upstream_type == "CompositeElement"]
        self.assertEqual(len(chunk_obs), 1)
        self.assertIsNone(chunk_obs[0].evidence_id)


class TestUnstructuredNegative(unittest.TestCase):
    """Negative and quarantine contract tests for Unstructured adapter."""

    def setUp(self):
        self.parser = UnstructuredParser()

    def test_profile_rejects_table_without_hi_res(self):
        """UnstructuredProfile raises ValueError when infer_table_structure is not hi_res."""
        with self.assertRaises(ValueError):
            UnstructuredProfile(strategy="fast", infer_table_structure=True)
        with self.assertRaises(ValueError):
            UnstructuredProfile(strategy="ocr_only", infer_table_structure=True)

    def test_quarantine_unsupported_version(self):
        """Unpinned or newer version quarantines batch."""
        raw = b"[]"
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0], version="0.28.0")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "unsupported_unstructured_version_or_schema")
        self.assertEqual(batch.result.pages[0].status, "failed")
        self.assertEqual(len(batch.evidence), 0)

    def test_quarantine_strategy_mismatch(self):
        """Profile strategy not matching manifest effective profile quarantines."""
        raw = b"[]"
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0], strategy="fast")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="hi_res"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "effective_strategy_mismatch")

    def test_quarantine_api_or_enrichment(self):
        """API endpoint or postprocessing flags quarantine."""
        raw = b"[]"
        req = _make_request([0])
        frames = _default_frames([0])

        manifest1 = _make_unstructured_manifest([0], endpoint_deployment_id="ep-api-01")
        batch1 = self.parser.normalize(
            raw, req, manifest1, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch1.quarantine_reason, "api_or_enrichment_requires_separate_adapter")

        manifest2 = _make_unstructured_manifest([0], postprocess_flags=["vlm_summarize"])
        batch2 = self.parser.normalize(
            raw, req, manifest2, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch2.quarantine_reason, "api_or_enrichment_requires_separate_adapter")

    def test_strategy_fallback_marks_needs_review(self):
        """Recorded strategy fallback marks pages needs_review without quarantine."""
        payload = [
            {
                "type": "NarrativeText",
                "text": "Synthetic text after fallback.",
                "metadata": {
                    "page_number": 1,
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 612.0,
                        "layout_height": 792.0,
                        "points": [[50.0, 50.0], [200.0, 100.0]],
                    },
                },
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "unstructured-oss-hi_res")
        manifest = _make_unstructured_manifest(
            [0], strategy="fast", requested_profile="unstructured-oss-hi_res",
            effective_profile="unstructured-oss-fast", fallback_reason="hi_res_model_missing",
        )
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(batch.result.pages[0].status, "needs_review")
        self.assertIn("parser_strategy_fallback", batch.result.pages[0].diagnostics)

    def test_missing_or_invalid_geometry_never_fabricates(self):
        """Element lacking valid PixelSpace coordinates is retained as observation but no evidence."""
        payload = [
            {
                "type": "NarrativeText",
                "text": "Synthetic unpositioned text.",
                "metadata": {"page_number": 1},
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0])
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.evidence), 0)
        self.assertEqual(batch.result.pages[0].status, "needs_review")
        self.assertIn("missing_or_invalid_geometry_or_asset_reference", batch.result.pages[0].diagnostics)
        self.assertEqual(len(batch.observations), 1)
        self.assertIsNone(batch.observations[0].evidence_id)

    def test_unattested_layout_geometry_marks_needs_review(self):
        """Coordinates with layout dimensions differing from DisplayFrame are not used."""
        payload = [
            {
                "type": "NarrativeText",
                "text": "Synthetic mismatched layout text.",
                "metadata": {
                    "page_number": 1,
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 1000.0,  # does not match frame 612.0
                        "layout_height": 2000.0,
                        "points": [[10.0, 10.0], [50.0, 50.0]],
                    },
                },
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0])
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.evidence), 0)
        self.assertEqual(batch.result.pages[0].status, "needs_review")
        self.assertIn("unattested_layout_geometry", batch.result.pages[0].diagnostics)

    def test_corrupt_orig_elements_quarantines(self):
        """Corrupt orig_elements in CompositeElement quarantines batch."""
        payload = [
            {
                "type": "CompositeElement",
                "text": "Synthetic bad chunk",
                "metadata": {"orig_elements": "not-valid-base64-!!!"},
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0])
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")


class TestMinerUPositive(unittest.TestCase):
    """Positive contract tests for strict pinned MinerU 4.1.7 Middle JSON 2.0 adapter."""

    def setUp(self):
        self.parser = MinerUParser()

    def test_standard_tier_text_table_and_chart(self):
        """Synthetic DocVortex Middle JSON 2.0 with text, table, and chart blocks."""
        table_html = "<table><tr><td>Middle JSON Cell</td></tr></table>"
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "4.1.7"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "text",
                            "index": 0,
                            "bbox": [0.1, 0.05, 0.9, 0.2],
                            "content": [
                                {"type": "text", "content": "Synthetic MinerU heading and sentence."}
                            ],
                        },
                        {
                            "type": "table",
                            "index": 1,
                            "bbox": [0.1, 0.25, 0.9, 0.55],
                            "content": [
                                {
                                    "type": "table_body",
                                    "index": 1,
                                    "bbox": [0.1, 0.25, 0.9, 0.55],
                                    "content": table_html,
                                }
                            ],
                        },
                        {
                            "type": "chart",
                            "index": 2,
                            "bbox": [0.1, 0.6, 0.9, 0.9],
                            "content": [
                                {
                                    "type": "chart_body",
                                    "index": 2,
                                    "bbox": [0.1, 0.6, 0.9, 0.9],
                                    "content": "Synthetic Chart Alt Text",
                                    "image_path": "/tmp/synthetic-chart.png",
                                }
                            ],
                        },
                    ],
                }
            ],
            "extensions": {
                "mineru": {"tier": "standard"},
                "docvortex_layout": {
                    "version": 1,
                    "pages": [{"page_idx": 0, "width_pt": 612.0, "height_pt": 792.0}],
                },
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])
        asset_refs = {"/tmp/synthetic-chart.png": "sha256:" + "c" * 64}

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1, asset_refs=asset_refs,
        )

        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.result.pages), 1)
        self.assertEqual(batch.result.pages[0].status, "ok")
        self.assertEqual(len(batch.evidence), 3)

        # Text evidence
        self.assertEqual(batch.evidence[0].regions[0].kind.value, "text")
        self.assertEqual(batch.evidence[0].raw_content, "Synthetic MinerU heading and sentence.")

        # Table evidence: table region preserved, cell_geometry declared unsupported
        self.assertEqual(batch.evidence[1].regions[0].kind.value, "table")
        self.assertEqual(batch.observations[1].text_as_html, table_html)
        self.assertEqual(batch.capabilities.cell_geometry.state, "unsupported")

        # Chart evidence: mapped to figure, asset resolved
        self.assertEqual(batch.evidence[2].regions[0].kind.value, "figure")
        self.assertEqual(batch.evidence[2].asset_refs, ["sha256:" + "c" * 64])

    def test_explicit_blank_page_attested(self):
        """Attested blank page with empty blocks list is confirmed ok, not failed."""
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "4.1.7"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [
                {"page_idx": 0, "blocks": []}
            ],
            "extensions": {
                "mineru": {"tier": "flash"},
                "docvortex_layout": {
                    "version": 1,
                    "pages": [{"page_idx": 0, "width_pt": 612.0, "height_pt": 792.0}],
                },
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-flash")
        manifest = _make_mineru_manifest([0], tier="flash")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="flash"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(batch.result.pages[0].status, "ok")
        self.assertEqual(len(batch.evidence), 0)


class TestMinerUNegative(unittest.TestCase):
    """Negative and quarantine contract tests for MinerU adapter."""

    def setUp(self):
        self.parser = MinerUParser()

    def test_quarantine_legacy_schema(self):
        """MinerU legacy schema (e.g. 1.0 or pdf_info) quarantines."""
        payload = {"schema": "mineru.pdf_info", "schema_version": "1.0"}
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "unsupported_mineru_schema")

    def test_quarantine_schema_drift(self):
        """Payload violating docvortex-middle-2.0.json schema quarantines."""
        # Missing required 'metadata' and 'pages'
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "is_full_document": True,
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "invalid_mineru_output_schema")

    def test_quarantine_producer_version_mismatch(self):
        """Producer version not matching pinned 4.1.7 quarantines."""
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "3.1.0"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [],
            "extensions": {"mineru": {"tier": "standard"}},
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "unsupported_mineru_producer_version")

    def test_quarantine_tier_mismatch(self):
        """Extension tier differing from manifest/profile quarantines."""
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "4.1.7"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [],
            "extensions": {"mineru": {"tier": "basic"}},  # basic != standard
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "mineru_tier_mismatch")

    def test_quarantine_fake_full_document(self):
        """Claiming is_full_document=True on subset of pages quarantines."""
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "4.1.7"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [{"page_idx": 0, "blocks": []}],  # doc has 2 pages
            "extensions": {"mineru": {"tier": "standard"}},
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=2,  # document has 2 pages
        )
        self.assertEqual(batch.quarantine_reason, "invalid_full_document_page_selection")

    def test_quarantine_postprocessing_flags(self):
        """Uncontrolled MinerU postprocessing flags quarantine."""
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "4.1.7"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [],
            "extensions": {"mineru": {"tier": "standard"}},
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard", postprocess_flags=["cross_page_table_merge"])
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "uncontrolled_mineru_postprocessing")

    def test_quarantine_block_order_invalid(self):
        """Out-of-order block indices quarantine batch."""
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "4.1.7"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {"type": "text", "index": 1, "content": [{"type": "text", "content": "Second"}]},
                        {"type": "text", "index": 0, "content": [{"type": "text", "content": "First"}]},
                    ],
                }
            ],
            "extensions": {"mineru": {"tier": "standard"}},
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "invalid_mineru_block_order")

    def test_unattested_layout_geometry_does_not_use_bbox(self):
        """Mismatched page layout dimensions in docvortex_layout marks needs_review and omits bbox."""
        payload = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {
                "producer": {"name": "mineru", "version": "4.1.7"},
                "file_suffix": "pdf",
            },
            "is_full_document": True,
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "text",
                            "index": 0,
                            "bbox": [0.1, 0.1, 0.9, 0.9],
                            "content": [{"type": "text", "content": "Synthetic text"}],
                        }
                    ],
                }
            ],
            "extensions": {
                "mineru": {"tier": "standard"},
                "docvortex_layout": {
                    "version": 1,
                    "pages": [{"page_idx": 0, "width_pt": 500.0, "height_pt": 500.0}],  # differs from frame 612x792
                },
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(batch.result.pages[0].status, "needs_review")
        self.assertIn("unattested_layout_geometry", batch.result.pages[0].diagnostics)
        # Bbox was not used, so no evidence unit was created
        self.assertEqual(len(batch.evidence), 0)
        self.assertEqual(len(batch.observations), 1)
        self.assertIsNone(batch.observations[0].evidence_id)


class TestCommonConstraints(unittest.TestCase):
    """Common strict constraints: JSON size limits, duplicate keys, non-finite values."""

    def test_raw_size_limit(self):
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0])
        frames = _default_frames([0])
        oversized = b" " * (MAX_RAW_BYTES + 1)
        parser = UnstructuredParser()
        batch = parser.normalize(
            oversized, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")

    def test_duplicate_json_keys_rejected(self):
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0])
        frames = _default_frames([0])
        dup_json = b'[{"type": "NarrativeText", "type": "Title"}]'
        parser = UnstructuredParser()
        batch = parser.normalize(
            dup_json, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")

    def test_non_finite_json_rejected(self):
        req = _make_request([0])
        manifest = _make_unstructured_manifest([0])
        frames = _default_frames([0])
        nan_json = b'[{"type": "NarrativeText", "val": NaN}]'
        parser = UnstructuredParser()
        batch = parser.normalize(
            nan_json, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")


if __name__ == "__main__":
    unittest.main()
