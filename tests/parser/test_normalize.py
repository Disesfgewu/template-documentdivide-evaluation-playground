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
import unittest.mock
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
    Artifact,
    _Builder,
    DisplayFrame,
    MAX_RAW_BYTES,
    MAX_ARTIFACTS,
    MAX_ELEMENTS,
    MAX_MINERU_BLOCKS,
    MAX_MINERU_PAGES,
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

    def test_evidence_identity_includes_source_document_and_transform_lineage(self):
        """Identical raw output from different documents or transforms produces distinct evidence IDs."""
        payload = [
            {
                "type": "NarrativeText",
                "text": "Synthetic identical text.",
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
        req = _make_request([0], "unstructured-oss-fast")
        manifest = _make_unstructured_manifest([0], strategy="fast")
        frames_a = {0: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=612.0, height=792.0, transform_ref="tr-a")}
        frames_b = {0: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=612.0, height=792.0, transform_ref="tr-b")}

        batch_a = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id="doc-a", document_sha256="a" * 64,
            frames=frames_a, document_page_count=1,
        )
        batch_b = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id="doc-b", document_sha256="b" * 64,
            frames=frames_b, document_page_count=1,
        )
        batch_a_again = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id="doc-a", document_sha256="a" * 64,
            frames=frames_a, document_page_count=1,
        )

        self.assertNotEqual(batch_a.evidence[0].evidence_id, batch_b.evidence[0].evidence_id)
        self.assertEqual(batch_a.evidence[0].evidence_id, batch_a_again.evidence[0].evidence_id)
        self.assertEqual(batch_a.evidence[0].regions[0].document_id, "doc-a")
        self.assertEqual(batch_b.evidence[0].regions[0].document_id, "doc-b")

    def test_colon_delimited_identifier_canonical_encoding_no_collision(self):
        """Preimage structured encoding prevents delimiter collision when identifiers contain colons."""
        payload = [
            {
                "type": "NarrativeText",
                "text": "Synthetic collision test text.",
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

        # Case A: document_id='doc:0', manifest parser_page 1 -> canonical page_index 1, transform_ref='tx'
        manifest_a = _make_unstructured_manifest([1]).model_copy(
            update={"page_index_map": [PageMapEntry(parser_page=1, page_index=1)]}
        )
        frames_a = {1: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=612.0, height=792.0, transform_ref="tx")}
        req_a = _make_request([1])

        # Case B: document_id='doc', manifest parser_page 1 -> canonical page_index 0, transform_ref='1:tx'
        manifest_b = _make_unstructured_manifest([0]).model_copy(
            update={"page_index_map": [PageMapEntry(parser_page=1, page_index=0)]}
        )
        frames_b = {0: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=612.0, height=792.0, transform_ref="1:tx")}
        req_b = _make_request([0])

        batch_a = self.parser.normalize(
            raw, req_a, manifest_a, UnstructuredProfile(strategy="fast"),
            document_id="doc:0", document_sha256="a" * 64,
            frames=frames_a, document_page_count=2,
        )
        batch_b = self.parser.normalize(
            raw, req_b, manifest_b, UnstructuredProfile(strategy="fast"),
            document_id="doc", document_sha256="a" * 64,
            frames=frames_b, document_page_count=2,
        )
        batch_a_rerun = self.parser.normalize(
            raw, req_a, manifest_a, UnstructuredProfile(strategy="fast"),
            document_id="doc:0", document_sha256="a" * 64,
            frames=frames_a, document_page_count=2,
        )

        self.assertEqual(len(batch_a.evidence), 1)
        self.assertEqual(len(batch_b.evidence), 1)
        # Differing SourceRegions must not collide in evidence identity
        self.assertNotEqual(batch_a.evidence[0].evidence_id, batch_b.evidence[0].evidence_id)
        # Same source normalization remains strictly deterministic
        self.assertEqual(batch_a.evidence[0].evidence_id, batch_a_rerun.evidence[0].evidence_id)
        self.assertEqual(batch_a.evidence[0].regions[0].document_id, "doc:0")
        self.assertEqual(batch_b.evidence[0].regions[0].document_id, "doc")
        self.assertEqual(batch_a.evidence[0].regions[0].page_index, 1)
        self.assertEqual(batch_b.evidence[0].regions[0].page_index, 0)


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

    def test_invalid_asset_ref_clears_observation_link_and_marks_needs_review(self):
        """Invalid caller asset_ref (e.g. empty string) produces zero evidence and no dangling observation link."""
        payload = [
            {
                "type": "Image",
                "text": "",
                "metadata": {
                    "page_number": 1,
                    "image_path": "synthetic-image.png",
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": 612.0,
                        "layout_height": 792.0,
                        "points": [[100.0, 100.0], [200.0, 200.0]],
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
            asset_refs={"synthetic-image.png": ""},  # invalid empty asset ref
        )
        self.assertEqual(len(batch.evidence), 0)
        self.assertEqual(len(batch.observations), 1)
        self.assertIsNone(batch.observations[0].evidence_id)
        self.assertEqual(batch.result.pages[0].status, "needs_review")
        self.assertIn("missing_or_invalid_geometry_or_asset_reference", batch.result.pages[0].diagnostics)

    def test_invalid_metadata_types_and_quarantine_clears_child_artifacts(self):
        """Invalid metadata field types quarantine and clear unpacked orig_elements artifacts."""
        child_json = json.dumps([{"type": "Title", "text": "Child", "metadata": {"page_number": 1}}]).encode("utf-8")
        encoded_orig = base64.b64encode(zlib.compress(child_json)).decode("ascii")
        req = _make_request([0], "unstructured-oss-fast")
        manifest = _make_unstructured_manifest([0], strategy="fast")
        frames = _default_frames([0])

        for bad_meta in (
            {"page_number": 1, "text_as_html": 12345},
            {"page_number": 1, "image_path": ["bad"]},
            {"page_number": 1, "image_base64": 999},
        ):
            payload = [
                {"type": "CompositeElement", "text": "Chunk", "metadata": {"orig_elements": encoded_orig}},
                {"type": "Table", "text": "Bad table", "metadata": bad_meta},
            ]
            raw = json.dumps(payload).encode("utf-8")
            batch = self.parser.normalize(
                raw, req, manifest, UnstructuredProfile(strategy="fast"),
                document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
                frames=frames, document_page_count=1,
            )
            self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")
            self.assertEqual(len(batch.evidence), 0)
            self.assertEqual(len(batch.observations), 0)
            # Only raw artifact and manifest artifact remain; unpacked child artifact is cleared
            self.assertEqual(len(batch.result.raw_result_refs), 1)
            self.assertEqual(len(batch.artifacts), 2)

        # Also reject non-string orig_elements (e.g., integer)
        bad_chunk_raw = json.dumps([{"type": "CompositeElement", "text": "c", "metadata": {"orig_elements": 123}}]).encode("utf-8")
        batch_chunk = self.parser.normalize(
            bad_chunk_raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch_chunk.quarantine_reason, "invalid_unstructured_output")

    def test_boolean_layout_dimensions_rejected_as_unattested(self):
        """Boolean layout_width/layout_height are not coerced to 1.0 against DisplayFrame."""
        payload = [
            {
                "type": "NarrativeText",
                "text": "Boolean layout width test.",
                "metadata": {
                    "page_number": 1,
                    "coordinates": {
                        "system": "PixelSpace",
                        "layout_width": True,
                        "layout_height": True,
                        "points": [[0.1, 0.1], [0.9, 0.9]],
                    },
                },
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        req = _make_request([0], "unstructured-oss-fast")
        manifest = _make_unstructured_manifest([0], strategy="fast")
        frames = {0: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=1.0, height=1.0, transform_ref="tr-0")}

        batch = self.parser.normalize(
            raw, req, manifest, UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.evidence), 0)
        self.assertIn("unattested_layout_geometry", batch.result.pages[0].diagnostics)

    def test_oversized_layout_integer_is_unattested(self):
        payload = [{"type": "NarrativeText", "text": "x", "metadata": {
            "page_number": 1, "coordinates": {"system": "PixelSpace",
            "layout_width": 10**400, "layout_height": 792,
            "points": [[0, 0], [1, 1]]}}}]
        batch = self.parser.normalize(
            json.dumps(payload).encode(), _make_request([0], "unstructured-oss-fast"),
            _make_unstructured_manifest([0], strategy="fast"), UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames={0: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=612, height=792, transform_ref="tr-0")},
            document_page_count=1,
        )
        self.assertFalse(batch.evidence)
        self.assertIn("unattested_layout_geometry", batch.result.pages[0].diagnostics)


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

    def test_equation_block_assets_and_nested_hyperlink_text(self):
        """EquationBlock preserves top-level formula image assets and annotations preserve nested hyperlink text."""
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
                            "type": "equation",
                            "index": 0,
                            "bbox": [0.1, 0.1, 0.9, 0.2],
                            "content": "E = mc^2",
                            "image_path": "/tmp/synthetic-eq.png",
                            "image_base64": "aGVsbG8=",
                        },
                        {
                            "type": "table",
                            "index": 1,
                            "bbox": [0.1, 0.3, 0.9, 0.6],
                            "content": [
                                {
                                    "type": "table_caption",
                                    "content": [
                                        {"type": "text", "content": "Table 1 (see "},
                                        {
                                            "type": "hyperlink",
                                            "url": "https://example.com/spec",
                                            "content": [
                                                {"type": "text", "content": "Appendix "},
                                                {"type": "code_inline", "content": "A.1"},
                                            ],
                                        },
                                        {"type": "text", "content": ")"},
                                    ],
                                },
                                {
                                    "type": "table_body",
                                    "index": 1,
                                    "bbox": [0.1, 0.3, 0.9, 0.6],
                                    "content": "<table><tr><td>1</td></tr></table>",
                                },
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
        raw_ref = "sha256:" + hashlib.sha256(raw).hexdigest()
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = _default_frames([0])

        # With only /tmp/synthetic-eq.png mapped, image_base64 triggers unresolved_image_asset diagnostic
        batch_partial = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
            asset_refs={"/tmp/synthetic-eq.png": "sha256:" + "e" * 64},
        )
        self.assertIsNone(batch_partial.quarantine_reason)
        self.assertEqual(len(batch_partial.evidence), 2)
        self.assertEqual(batch_partial.evidence[0].regions[0].kind.value, "formula")
        self.assertEqual(batch_partial.evidence[0].asset_refs, ["sha256:" + "e" * 64])
        self.assertEqual(
            batch_partial.observations[0].upstream_image_refs,
            ("/tmp/synthetic-eq.png", f"{raw_ref}#/pages/0/blocks/0/image_base64"),
        )
        self.assertIn("unresolved_image_asset", batch_partial.result.pages[0].diagnostics)
        self.assertEqual(batch_partial.evidence[1].raw_content, "Table 1 (see Appendix A.1)")
        self.assertEqual(batch_partial.observations[1].raw_content, "Table 1 (see Appendix A.1)")


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

    def test_invalid_asset_ref_clears_observation_link_and_marks_needs_review(self):
        """Invalid caller asset_ref on visual body produces zero evidence and no dangling observation link."""
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
                            "type": "chart",
                            "index": 0,
                            "bbox": [0.1, 0.1, 0.9, 0.9],
                            "content": [
                                {
                                    "type": "chart_body",
                                    "index": 0,
                                    "bbox": [0.1, 0.1, 0.9, 0.9],
                                    "content": "Alt text",
                                    "image_path": "/tmp/synthetic-chart.png",
                                }
                            ],
                        }
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

        batch = self.parser.normalize(
            raw, req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
            asset_refs={"/tmp/synthetic-chart.png": ""},  # invalid empty string
        )
        self.assertEqual(len(batch.evidence), 0)
        self.assertEqual(len(batch.observations), 1)
        self.assertIsNone(batch.observations[0].evidence_id)
        self.assertEqual(batch.result.pages[0].status, "needs_review")
        self.assertIn("missing_or_invalid_geometry_or_asset_reference", batch.result.pages[0].diagnostics)

    def test_quarantine_malformed_extensions_and_non_pdf_suffix(self):
        """Non-PDF file_suffix and malformed/boolean extensions fail closed with quarantine."""
        req = _make_request([0], "mineru-standard")
        manifest = _make_mineru_manifest([0], tier="standard")
        frames = {0: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=1.0, height=1.0, transform_ref="tr-0")}

        # Non-PDF file_suffix must quarantine
        non_pdf = {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "metadata": {"producer": {"name": "mineru", "version": "4.1.7"}, "file_suffix": "docx"},
            "is_full_document": True,
            "pages": [{"page_idx": 0, "blocks": []}],
            "extensions": {"mineru": {"tier": "standard"}},
        }
        batch_docx = self.parser.normalize(
            json.dumps(non_pdf).encode("utf-8"), req, manifest, MinerUProfile(tier="standard"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1,
        )
        self.assertEqual(batch_docx.quarantine_reason, "unsupported_mineru_file_suffix")

        # Malformed extensions.mineru or extensions.docvortex_layout must quarantine without AttributeError
        for bad_ext in (
            {},
            {"mineru": None},
            {"mineru": []},
            {"mineru": {"tier": "standard"}, "docvortex_layout": None},
            {"mineru": {"tier": "standard"}, "docvortex_layout": []},
            {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": True, "pages": []}},
            {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": 1, "pages": "not-a-list"}},
            {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": 1, "pages": [None]}},
            {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": 1, "pages": [{"page_idx": True, "width_pt": 1.0, "height_pt": 1.0}]}},
            {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": 1, "pages": [{"page_idx": 0, "width_pt": True, "height_pt": 1.0}]}},
            {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": 1, "pages": [{"page_idx": 0, "width_pt": 10**400, "height_pt": 1.0}]}},
            {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": 1, "pages": [{"page_idx": 0, "width_pt": -1.0, "height_pt": 1.0}]}},
            {
                "mineru": {"tier": "standard"},
                "docvortex_layout": {
                    "version": 1,
                    "pages": [
                        {"page_idx": 0, "width_pt": 612.0, "height_pt": 792.0},
                        {"page_idx": 0, "width_pt": 612.0, "height_pt": 792.0},
                    ],
                },
            },
        ):
            payload = {
                "schema": "docvortex.middle",
                "schema_version": "2.0",
                "metadata": {"producer": {"name": "mineru", "version": "4.1.7"}, "file_suffix": "pdf"},
                "is_full_document": True,
                "pages": [{"page_idx": 0, "blocks": []}],
                "extensions": bad_ext,
            }
            batch = self.parser.normalize(
                json.dumps(payload).encode("utf-8"), req, manifest, MinerUProfile(tier="standard"),
                document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
                frames=frames, document_page_count=1,
            )
            self.assertEqual(batch.quarantine_reason, "invalid_mineru_output", f"Failed for {bad_ext}")
            self.assertEqual(batch.result.pages[0].status, "failed")


class TestNormalizationCost(unittest.TestCase):
    """Normalization work must stay linear in the number of elements (reproduced: 20000 small
    elements, 4 MB, took 101 s because the raw artifact was re-hashed three times per element
    and every new element scanned all earlier evidence). The tests count operations instead of
    measuring time, so they do not depend on the speed of the machine."""

    @staticmethod
    def _elements(count: int) -> bytes:
        return json.dumps([{"type": "NarrativeText", "text": "a", "metadata": {
            "page_number": 1, "coordinates": {"system": "PixelSpace", "layout_width": 612.0,
            "layout_height": 792.0, "points": [[1, 1], [1, 10], [10, 10], [10, 1]]}}} for _ in range(count)]).encode()

    def _normalize(self, raw: bytes):
        pages = [0]
        return UnstructuredParser().normalize(
            raw, _make_request(pages), _make_unstructured_manifest(pages), UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=_default_frames(pages), document_page_count=1)

    def test_the_raw_artifact_is_hashed_once_not_once_per_element(self):
        raw = self._elements(300)
        real = hashlib.sha256
        large = []

        def counting(data=b"", *args, **kwargs):
            if len(data) >= len(raw):
                large.append(len(data))
            return real(data, *args, **kwargs)

        with unittest.mock.patch("lab_parser.normalize.hashlib.sha256", side_effect=counting):
            batch = self._normalize(raw)
        self.assertEqual(len(batch.evidence), 300)
        self.assertEqual(len(large), 1, f"the whole raw artifact was hashed {len(large)} times")

    def test_artifact_ref_is_computed_at_construction_and_stays_stable(self):
        content = b"x" * 5000
        real = hashlib.sha256
        calls = []
        with unittest.mock.patch("lab_parser.normalize.hashlib.sha256",
                                 side_effect=lambda d=b"", *a, **k: (calls.append(len(d)), real(d, *a, **k))[1]):
            artifact = Artifact(content)
            refs = {artifact.ref for _ in range(50)}
        self.assertEqual(calls, [5000])
        self.assertEqual(refs, {"sha256:" + real(content).hexdigest()})
        self.assertEqual(Artifact(content), artifact)           # equality is still by content
        self.assertEqual(len({Artifact(content), artifact}), 1)  # and so is the hash
        self.assertNotIn("sha256:", repr(artifact))

    def test_adding_an_element_never_scans_the_evidence_collected_so_far(self):
        class NoScan(list):
            def __iter__(self):
                raise AssertionError("evidence was scanned")

        pages = [0]
        builder = _Builder(b"[]", _make_request(pages), _make_unstructured_manifest(pages), SYNTHETIC_DOC_ID,
                           SYNTHETIC_DOC_SHA256, _default_frames(pages), {}, 1)
        builder.evidence = NoScan()
        for i in range(25):
            builder.add(builder.raw, f"/{i}", 0, "NarrativeText", "a", [0.1, 0.1, 0.2, 0.2])
        self.assertEqual(len(builder.evidence), 25)
        self.assertEqual(len({o.evidence_id for o in builder.observations}), 25)

    def test_a_repeated_pointer_is_still_one_evidence_unit(self):
        pages = [0]
        builder = _Builder(b"[]", _make_request(pages), _make_unstructured_manifest(pages), SYNTHETIC_DOC_ID,
                           SYNTHETIC_DOC_SHA256, _default_frames(pages), {}, 1)
        for _ in range(3):
            builder.add(builder.raw, "/0", 0, "NarrativeText", "a", [0.1, 0.1, 0.2, 0.2])
        self.assertEqual(len(builder.evidence), 1)
        self.assertEqual(len(builder.observations), 3)  # every observation is kept, evidence is deduplicated

    def test_quarantine_discards_evidence_and_its_index(self):
        good = json.loads(self._elements(3))
        raw = json.dumps(good + [{"type": "NarrativeText", "text": 5}]).encode()  # a later element is invalid
        batch = self._normalize(raw)
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")
        self.assertEqual((batch.evidence, batch.observations), ((), ()))
        pages = [0]
        builder = _Builder(b"[]", _make_request(pages), _make_unstructured_manifest(pages), SYNTHETIC_DOC_ID,
                           SYNTHETIC_DOC_SHA256, _default_frames(pages), {}, 1)
        builder.add(builder.raw, "/0", 0, "NarrativeText", "a", [0.1, 0.1, 0.2, 0.2])
        builder.quarantine("invalid_unstructured_output")
        builder.add(builder.raw, "/0", 0, "NarrativeText", "a", [0.1, 0.1, 0.2, 0.2])  # same pointer after a reset
        self.assertEqual(len(builder.evidence), 1)


def _chunk(text: str, distinct: int | None):
    """A CompositeElement whose orig_elements holds one element (distinct text per chunk when given)."""
    inner = json.dumps([{"type": "NarrativeText", "text": text if distinct is None else f"{text}{distinct}",
                         "metadata": {"page_number": 1}}]).encode()
    return {"type": "CompositeElement", "text": text,
            "metadata": {"orig_elements": base64.b64encode(zlib.compress(inner)).decode()}}


def _normalize_unstructured(raw: bytes):
    pages = [0]
    return UnstructuredParser().normalize(
        raw, _make_request(pages), _make_unstructured_manifest(pages), UnstructuredProfile(strategy="fast"),
        document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
        frames=_default_frames(pages), document_page_count=1)


class TestChunkPathCostAndCaps(unittest.TestCase):
    """The chunk path (CompositeElement/TableChunk with orig_elements) re-summed every retained
    artifact per chunk (10k chunks 1.7 s, 40k chunks 24 s). Counts and caps are asserted
    structurally so the tests do not depend on machine speed."""

    def test_the_retained_artifacts_are_not_rescanned_per_chunk(self):
        scans = []
        real_init = _Builder.__init__

        class Counting(list):
            def __iter__(self):
                scans.append(1)
                return super().__iter__()

        def init(self, *a, **k):
            real_init(self, *a, **k)
            self.artifacts = Counting(self.artifacts)

        raw = json.dumps([_chunk("t", i) for i in range(200)]).encode()
        with unittest.mock.patch.object(_Builder, "__init__", init):
            batch = _normalize_unstructured(raw)
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.artifacts), 1 + 200 + 1)  # raw, 200 distinct children, manifest
        # finish() reads the list a constant number of times; the chunk loop must not.
        self.assertLessEqual(len(scans), 5, f"artifacts scanned {len(scans)} times for 200 chunks")

    def test_identical_children_are_retained_once_but_every_chunk_is_observed(self):
        raw = json.dumps([_chunk("same", None) for _ in range(20)]).encode()
        batch = _normalize_unstructured(raw)
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.artifacts), 1 + 1 + 1)  # raw, one child, manifest
        self.assertEqual(len([o for o in batch.observations if o.upstream_type == "CompositeElement"]), 20)

    def test_identical_chunks_decode_their_payload_once(self):
        """One large child referenced by many identical chunks: reproduced at ~40 ms per chunk
        (200 chunks 7.9 s) when each chunk was decompressed and parsed before the dedupe."""
        chunk = _chunk("x" * 2000, None)
        raw = json.dumps([chunk] * 200).encode()
        real = zlib.decompressobj
        decodes = []

        def counting(*args, **kwargs):
            decodes.append(1)
            return real(*args, **kwargs)

        with unittest.mock.patch("lab_parser.normalize.zlib.decompressobj", side_effect=counting):
            batch = _normalize_unstructured(raw)
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(decodes), 1, f"payload decoded {len(decodes)} times for 200 identical chunks")
        self.assertEqual(len([o for o in batch.observations if o.upstream_type == "CompositeElement"]), 200)
        self.assertEqual(len(batch.artifacts), 1 + 1 + 1)

    def test_the_element_count_is_capped(self):
        raw = json.dumps([{"type": "NarrativeText", "text": "a", "metadata": {"page_number": 1}}] * 6).encode()
        with unittest.mock.patch("lab_parser.normalize.MAX_ELEMENTS", 5):
            batch = _normalize_unstructured(raw)
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")
        self.assertEqual(len(batch.evidence), 0)
        self.assertGreater(MAX_ELEMENTS, 10_000)

    def test_the_distinct_artifact_count_is_capped(self):
        raw = json.dumps([_chunk("t", i) for i in range(4)]).encode()
        with unittest.mock.patch("lab_parser.normalize.MAX_ARTIFACTS", 3):  # raw + 2 children
            batch = _normalize_unstructured(raw)
        self.assertEqual(batch.quarantine_reason, "invalid_unstructured_output")
        self.assertEqual(len(batch.artifacts), 2)  # raw + manifest only after quarantine
        self.assertGreater(MAX_ARTIFACTS, 1_000)


def _mineru_payload(pages: int, blocks_per_page: int, bbox=(0.1, 0.1, 0.9, 0.9)) -> dict:
    return {
        "schema": "docvortex.middle", "schema_version": "2.0",
        "metadata": {"producer": {"name": "mineru", "version": "4.1.7"}, "file_suffix": "pdf"},
        "is_full_document": False,
        "pages": [{"page_idx": p, "blocks": [
            {"type": "text", "index": i, "bbox": list(bbox),
             "content": [{"type": "text", "content": "x"}]} for i in range(blocks_per_page)]}
            for p in range(pages)],
        "extensions": {"mineru": {"tier": "standard"}, "docvortex_layout": {"version": 1, "pages": [
            {"page_idx": p, "width_pt": 612.0, "height_pt": 792.0} for p in range(pages)]}},
    }


def _normalize_mineru(payload: dict, frames=None):
    pages = [p["page_idx"] for p in payload["pages"]]
    return MinerUParser().normalize(
        json.dumps(payload).encode(), _make_request(pages, "mineru-standard"),
        _make_mineru_manifest(pages, tier="standard"), MinerUProfile(tier="standard"),
        document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
        frames=frames or _default_frames(pages), document_page_count=max(pages) + 1)


class TestMinerUCapsAndFrames(unittest.TestCase):
    def test_block_and_page_caps_apply_before_schema_validation(self):
        validated = []
        with unittest.mock.patch("lab_parser.normalize.MAX_MINERU_BLOCKS", 5), \
                unittest.mock.patch("lab_parser.normalize._DOCVORTEX_VALIDATOR") as validator:
            validator.is_valid.side_effect = lambda payload: validated.append(1) or True
            batch = _normalize_mineru(_mineru_payload(1, 6))
        self.assertEqual(batch.quarantine_reason, "mineru_block_count_limit")
        self.assertEqual(validated, [], "the over-cap payload reached whole-payload schema validation")
        with unittest.mock.patch("lab_parser.normalize.MAX_MINERU_PAGES", 1):
            batch = _normalize_mineru(_mineru_payload(2, 1))
        self.assertEqual(batch.quarantine_reason, "mineru_page_count_limit")
        self.assertGreaterEqual(MAX_MINERU_BLOCKS, 1000)
        self.assertGreaterEqual(MAX_MINERU_PAGES, 600)

    def test_a_payload_at_the_cap_is_accepted(self):
        with unittest.mock.patch("lab_parser.normalize.MAX_MINERU_BLOCKS", 6):
            batch = _normalize_mineru(_mineru_payload(1, 6))
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.evidence), 6)

    def test_point_or_pixel_boxes_are_rejected_not_scaled(self):
        batch = _normalize_mineru(_mineru_payload(1, 1, bbox=(72.0, 72.0, 300.0, 120.0)))
        self.assertIsNone(batch.quarantine_reason)
        self.assertEqual(len(batch.evidence), 0)
        self.assertIsNone(batch.observations[0].evidence_id)
        self.assertEqual(batch.result.pages[0].status, "needs_review")


class TestAttestedDisplayFrameOnly(unittest.TestCase):
    """Rotated pages: conversion divides by the attested display frame and nothing else."""

    @staticmethod
    def _element(layout_w, layout_h):
        return json.dumps([{"type": "NarrativeText", "text": "a", "metadata": {
            "page_number": 1, "coordinates": {"system": "PixelSpace", "layout_width": layout_w,
            "layout_height": layout_h, "points": [[79.2, 61.2], [79.2, 122.4], [396.0, 122.4], [396.0, 61.2]]}}}]).encode()

    def _normalize_with_frame(self, raw, width, height):
        pages = [0]
        frames = {0: DisplayFrame(coordinate_frame="rotated_cropbox_top_left", width=width, height=height,
                                  transform_ref="tr-rot90")}
        return UnstructuredParser().normalize(
            raw, _make_request(pages), _make_unstructured_manifest(pages), UnstructuredProfile(strategy="fast"),
            document_id=SYNTHETIC_DOC_ID, document_sha256=SYNTHETIC_DOC_SHA256,
            frames=frames, document_page_count=1)

    def test_a_rotated_page_uses_the_swapped_display_size(self):
        batch = self._normalize_with_frame(self._element(792.0, 612.0), 792.0, 612.0)
        region = batch.evidence[0].regions[0]
        self.assertEqual([round(v, 4) for v in region.bbox], [0.1, 0.1, 0.5, 0.2])

    def test_unrotated_layout_size_for_a_rotated_frame_is_unattested(self):
        batch = self._normalize_with_frame(self._element(612.0, 792.0), 792.0, 612.0)
        self.assertEqual(len(batch.evidence), 0)
        self.assertIn("unattested_layout_geometry", batch.result.pages[0].diagnostics)


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

    def test_vendored_schemas_and_licenses_match_verified_hashes(self):
        """Vendored upstream schema and license files match exact verified byte counts and SHA256 hashes."""
        from pathlib import Path
        schema_path = Path("services/parser/lab_parser/upstream_schemas/docvortex-middle-2.0.json")
        license_path = Path("services/parser/lab_parser/upstream_schemas/DOCVORTEX-LICENSE.md")

        schema_bytes = schema_path.read_bytes()
        license_bytes = license_path.read_bytes()

        self.assertEqual(len(schema_bytes), 61255)
        self.assertEqual(hashlib.sha256(schema_bytes).hexdigest(),
                         "c13491ba1363943b239bd7bbf41fbce0e2d4b7e03d1e71d3741b0328a7b1b38b")
        self.assertEqual(len(license_bytes), 1079)
        self.assertEqual(hashlib.sha256(license_bytes).hexdigest(),
                         "ff54576f0fa4b4a677eaf63497a546d78502398d6c9dddb0984cfa02e5294d3b")


if __name__ == "__main__":
    unittest.main()
