"""Deterministic R28 candidate subcases of T94/T95/T96/T97; no quality oracle."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lab_parser.context_linker import (
    Anchor, Document, Evidence, Reference, SCHEMA, SourceRegion,
    link_document, load_manifest, serialize_result,
)

SHA = "a" * 64


def document(pages=300):
    return Document("project", "pdf", SHA, "pdf-rev-1", pages, tuple(range(pages)))


def region(page, **kwargs):
    return SourceRegion("pdf", SHA, page, (.1, .2, .8, .9), "text", "raw source", **kwargs)


def anchor(**kwargs):
    data = dict(kind="figure", label="18", chapter="A", entity="server", year="2025", unit="ms", condition="high-load")
    data.update(kwargs)
    return Anchor(**data)


def target(identifier="plot", page=17, key=None):
    return Evidence(identifier, "1", (region(page),), (key or anchor(),))


def reference(identifier="condition", page=246, key=None, relation="qualifies"):
    return Evidence(identifier, "1", (region(page),), (), (Reference(key or anchor(), relation),))


class TestContextLinker(unittest.TestCase):
    def test_distant_pages_and_all_necessary_explicit_context_are_retained(self):
        items = [target(), reference(), reference("definition", 299, relation="explains")]
        result = link_document(document(), items)
        self.assertEqual(len(result.relations), 2)
        self.assertEqual({r.from_evidence[0] for r in result.relations}, {"condition", "definition"})
        self.assertEqual({r.to_evidence for r in result.relations}, {("plot", "1")})
        self.assertEqual({r.validation_status for r in result.relations}, {"candidate"})
        for relation in result.relations:
            self.assertEqual(relation.target_regions, items[0].regions)
            self.assertIn(relation.source_regions[0].page_index, (246, 299))
            self.assertEqual(relation.supporting_regions, ())
            self.assertEqual(relation.refuting_regions, ())
            self.assertIsNone(relation.reviewer_decision)

    def test_overlap_dedup_and_order_independent_identity(self):
        a, b = target(), reference()
        plain = link_document(document(), [a, b])
        overlap = link_document(document(), [b, a, a, b])
        self.assertEqual(plain.context_hash, overlap.context_hash)
        self.assertEqual(plain.relations, overlap.relations)
        self.assertEqual(overlap.duplicate_count, 2)
        self.assertEqual(overlap.evidence_count, 2)

    def test_conflicting_duplicate_identity_and_mixed_revision_reject(self):
        a = target()
        for other in (replace(a, regions=(region(18),)), replace(a, revision="2")):
            with self.subTest(other=other), self.assertRaises(ValueError):
                link_document(document(), [a, other])

    def test_different_scopes_preserve_conflicts_not_merge(self):
        for key in ("chapter", "entity", "year", "unit", "condition"):
            with self.subTest(key=key):
                different = target(key=anchor(**{key: "other"}))
                relation = link_document(document(), [different, reference()]).relations[0]
                self.assertEqual(relation.validation_status, "conflicting")
                self.assertIn(f"mismatch:{key}", relation.candidate_reason)
                self.assertEqual(relation.refuting_regions, ())

    def test_cross_chapter_reference_names_target_scope(self):
        # The text's physical chapter is not guessed from distance or page.
        result = link_document(document(), [target(key=anchor(chapter="appendix")),
                                            reference(key=anchor(chapter="appendix"))])
        self.assertEqual(result.relations[0].validation_status, "candidate")

    def test_ambiguous_identifier_keeps_all_destinations_unresolved(self):
        result = link_document(document(), [target(), target("second", 20), reference()])
        self.assertEqual(len(result.relations), 2)
        for relation in result.relations:
            self.assertEqual(relation.validation_status, "unresolved")
            self.assertIn("ambiguous_target", relation.candidate_reason)

    def test_incompatible_destination_does_not_make_compatible_one_ambiguous(self):
        result = link_document(document(), [target(), target("other-year", 20, anchor(year="2024")), reference()])
        statuses = {r.to_evidence[0]: r.validation_status for r in result.relations}
        self.assertEqual(statuses, {"plot": "candidate", "other-year": "conflicting"})

    def test_unknown_qualifier_never_verifies(self):
        result = link_document(document(), [target(key=anchor(unit=None)), reference()])
        self.assertEqual(result.relations[0].validation_status, "unresolved")
        self.assertIn("unknown:unit", result.relations[0].candidate_reason)

    def test_same_headers_or_text_without_explicit_reference_create_no_relation(self):
        result = link_document(document(), [target(), target("same-header", 18)])
        self.assertEqual(result.relations, ())

    def test_missing_target_and_self_reference_are_explicit(self):
        source = reference()
        result = link_document(document(), [source])
        self.assertEqual(result.relations, ())
        self.assertEqual(result.unmatched_references[0].reference, source.references[0])
        self_ref = replace(target(), references=source.references)
        result = link_document(document(), [self_ref])
        self.assertEqual(len(result.unmatched_references), 1)

    def test_changed_scope_region_document_or_revision_changes_ids(self):
        original = link_document(document(), [target(), reference()])
        variants = [
            (document(), [target(key=anchor(unit="s")), reference()]),
            (document(), [target(page=18), reference()]),
            (document(), [replace(target(), revision="2"), reference()]),
            (replace(document(), document_revision="2"), [target(), reference()]),
            (replace(document(), project_id="other-project"), [target(), reference()]),
        ]
        for doc, items in variants:
            with self.subTest(doc=doc, items=items):
                result = link_document(doc, items)
                self.assertNotEqual(result.context_hash, original.context_hash)
                self.assertNotEqual(result.relations[0].relation_id, original.relations[0].relation_id)
        with patch("lab_parser.context_linker.REVISION", "new-linker"):
            changed = link_document(document(), [target(), reference()])
            self.assertNotEqual(changed.context_hash, original.context_hash)

    def test_region_schema_and_page_document_boundaries(self):
        for changes in ({"bbox": (0, 0, 0, 1)}, {"bbox": (0, 0, True, 1)},
                        {"bbox": (0, 0, float("nan"), 1)}, {"page_index": True},
                        {"bbox": (0, 0, 10**1000, 1)}, {"kind": "page"}, {"document_sha256": "A" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(region(0), **changes)
        for changes in ({"document_id": "other"}, {"document_sha256": "b" * 64}, {"page_index": 300}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                link_document(document(), [replace(target(), regions=(replace(region(0), **changes),))])

    def test_full_document_manifest_rejects_subset_duplicate_and_noninteger_pages(self):
        for pages in ((0, 1), (0, 1, 1), (0, 1, True)):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                replace(document(3), processed_pages=pages)

    def test_frozen_no_mutable_nested_inputs(self):
        item = target()
        with self.assertRaises(FrozenInstanceError):
            item.revision = "2"
        with self.assertRaises(ValueError):
            replace(item, regions=list(item.regions))
        with self.assertRaises(ValueError):
            replace(item, anchors=(anchor(), anchor()))

    def test_bounds_fail_closed_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, "record_limit"):
            link_document(document(), (target() for _ in range(3)), max_records=2)
        with self.assertRaisesRegex(ValueError, "candidate_limit"):
            link_document(document(), [target(), target("b"), reference()], max_candidates=1)
        for limits in ({"max_records": True}, {"max_candidates": 50001}, {"max_records": 0}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                link_document(document(), [], **limits)
        with patch("lab_parser.context_linker.MAX_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "context_size"):
                link_document(document(), [target()])
        repeated_size = len(json.dumps(asdict(target()), ensure_ascii=False, allow_nan=False).encode())
        with patch("lab_parser.context_linker.MAX_BYTES", repeated_size):
            with self.assertRaisesRegex(ValueError, "context_size"):
                link_document(document(), [target(), target()])
        # Input fits but repeated relation-region payload exceeds output bound.
        items = [target(), reference(), reference("b")]
        input_size = sum(len(json.dumps(asdict(i), ensure_ascii=False, allow_nan=False).encode()) for i in items)
        with patch("lab_parser.context_linker.MAX_BYTES", input_size):
            with self.assertRaisesRegex(ValueError, "relation_output_size"):
                link_document(document(), items)

    def test_output_bound_includes_unmatched_references_and_envelope(self):
        # Repeated unmatched references expand their source identity in output.
        refs = tuple(Reference(anchor(label=str(i))) for i in range(32))
        item = Evidence("x" * 128, "r" * 128, (region(0),), (), refs)
        result = link_document(document(), [item])
        actual_size = len(serialize_result(result).encode("utf-8"))
        input_size = len(json.dumps(asdict(item), ensure_ascii=False, allow_nan=False).encode())
        self.assertGreater(actual_size, input_size)
        with patch("lab_parser.context_linker.MAX_BYTES", actual_size - 1):
            with self.assertRaisesRegex(ValueError, "relation_output_size"):
                link_document(document(), [item])
        with patch("lab_parser.context_linker.MAX_BYTES", actual_size):
            self.assertEqual(link_document(document(), [item]), result)

    def test_cli_rejects_special_files_without_blocking_and_accepts_regular_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = folder / "manifest.json"
            payload = {"schema_version": SCHEMA, "document": asdict(document()),
                       "evidence": [asdict(target()), asdict(reference())]}
            path.write_text(json.dumps(payload))
            symlink = folder / "link.json"
            symlink.symlink_to(path)
            fifo = folder / "fifo"
            os.mkfifo(fifo)
            for candidate, expected in ((path, 0), (symlink, 0), (folder, 2),
                                        (fifo, 2), (folder / "absent", 2), (Path("/dev/zero"), 2)):
                with self.subTest(candidate=candidate):
                    completed = subprocess.run([sys.executable, "-m", "lab_parser.context_linker", str(candidate)],
                                               capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual(completed.returncode, expected, completed.stderr)
                    if expected:
                        self.assertEqual(completed.stdout, "")
                        self.assertEqual(json.loads(completed.stderr), {"error": "invalid_context_manifest"})

    def test_versioned_manifest_strict_fields_and_roundtrip(self):
        payload = json.loads(json.dumps({"schema_version": SCHEMA, "document": asdict(document()),
                                        "evidence": [asdict(target()), asdict(reference())]}))
        doc, items = load_manifest(payload)
        self.assertEqual(link_document(doc, items), link_document(document(), [target(), reference()]))
        for location in (payload, payload["document"], payload["evidence"][0], payload["evidence"][0]["anchors"][0]):
            location["unknown"] = "must reject"
            with self.assertRaises(ValueError):
                load_manifest(payload)
            del location["unknown"]

    def test_cli_file_to_relations_and_invalid_json_no_payload_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            payload = {"schema_version": SCHEMA, "document": asdict(document()),
                       "evidence": [asdict(target()), asdict(reference())]}
            path.write_text(json.dumps(payload))
            command = [sys.executable, "-m", "lab_parser.context_linker", str(path)]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["schema_version"], "context-link-result-1")
            self.assertEqual(result["relations"][0]["validation_status"], "candidate")
            for raw in ('{"secret":"private", "secret":"duplicate"}', '{"a":NaN}', '[]', '{', '\ud800'):
                path.write_bytes(raw.encode("utf-8", errors="surrogatepass"))
                failed = subprocess.run(command, capture_output=True, text=True, check=False)
                self.assertEqual(failed.returncode, 2)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(json.loads(failed.stderr), {"error": "invalid_context_manifest"})
            with path.open("wb") as handle:
                handle.truncate(16 * 1024 * 1024 + 1)
            failed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(failed.returncode, 2)


if __name__ == "__main__":
    unittest.main()
