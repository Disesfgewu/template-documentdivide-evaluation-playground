"""Private transfer and minimal participant projection, synthetic contract data."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lab_contracts import transfer as tr
from lab_contracts.method import canonical_json, strict_loads

ROOT = Path(__file__).resolve().parents[2]
RAW = json.loads((ROOT / "contracts/examples/bundles/minimal.json").read_text())


def source():
    return {"metadata": {"benchmark_id": "bench_1", "revision_id": "rev_1",
                         "document_id": "doc_001", "document_sha256": "a" * 64,
                         "page_count": 500},
            "files": {name: copy.deepcopy(RAW[name]) for name in tr.MODELS},
            "source_manifest": None}


def archive(obj=None):
    return tr.export_archive(canonical_json(source() if obj is None else obj))


def rehash(obj):
    for name, records in obj["files"].items():
        body = canonical_json(records)
        obj["inventory"][name] = {"sha256": hashlib.sha256(body).hexdigest(),
                                   "bytes": len(body), "records": len(records)}
    return canonical_json(obj)


class TransferTests(unittest.TestCase):
    def test_roundtrip_preserves_all_source_records_and_unicode(self):
        src = source()
        blob = archive(src)
        restored = tr.import_archive(blob)
        self.assertEqual(restored["metadata"], src["metadata"])
        for name in tr.MODELS:
            key = tr.KEYS[name]
            expected = sorted(src["files"][name], key=lambda r: r[key])
            self.assertEqual(canonical_json(restored["files"][name]), canonical_json(expected))
        self.assertEqual(tr.export_archive(canonical_json(restored)), blob)
        self.assertIn("正式測試".encode(), blob)
        self.assertIsNone(restored["source_manifest"])
        self.assertNotIn(b'"frozen_at"', blob)

    def test_exact_decimal_tokens_survive_without_model_float_rewrite(self):
        from decimal import Decimal
        src = source()
        check = src["files"]["textual_answers"][0]["checks"][0]
        check["expected"] = Decimal("9007199254740993.0")
        check["absolute_tolerance"] = Decimal("0.000000000000000000000123456789")
        unit = src["files"]["evidence"][0]
        unit["regions"][0]["bbox"][0] = Decimal("0.100000000000000000000001")
        output = tr.import_archive(archive(src))
        checks = next(t["checks"] for t in output["files"]["textual_answers"]
                      if t["question_id"] == src["files"]["textual_answers"][0]["question_id"])
        self.assertEqual(checks[0]["expected"], Decimal("9007199254740993"))
        self.assertEqual(checks[0]["absolute_tolerance"], check["absolute_tolerance"])
        evidence = next(e for e in output["files"]["evidence"] if e["evidence_id"] == unit["evidence_id"])
        self.assertEqual(evidence["regions"][0]["bbox"][0], Decimal("0.100000000000000000000001"))

    def test_exact_source_numeric_bounds_cannot_round_into_valid_range(self):
        from decimal import Decimal
        above_one = Decimal("1.00000000000000000000000000001")
        for target in ("bbox", "fullpage", "ocr", "validation", "interval"):
            src = source()
            if target == "bbox":
                src["files"]["evidence"][0]["regions"][0]["bbox"][2] = above_one
            elif target == "fullpage":
                region = src["files"]["evidence"][0]["regions"][0]
                region.update(kind="page", bbox=[0, 0, Decimal("0.99999999999999999999999999999"), 1])
            elif target == "ocr":
                src["files"]["evidence"][0]["ocr_confidence"] = above_one
            elif target == "validation":
                src["files"]["validations"][0]["confidence"] = above_one
            else:
                check = src["files"]["textual_answers"][0]["checks"][0]
                check.update(expected=None, interval=[above_one, 1], absolute_tolerance=None,
                             relative_tolerance=None, precision=None)
            with self.subTest(target=target), self.assertRaisesRegex(tr.TransferError, "numeric_bounds_invalid"):
                archive(src)
            # Rehashing invalid data cannot bypass the exact checks at import.
            obj = strict_loads(archive())
            obj["files"] = src["files"]
            with self.assertRaisesRegex(tr.TransferError, "numeric_bounds_invalid"):
                tr.import_archive(rehash(obj))
        src = source()
        below_one = Decimal("0.99999999999999999999999999999")
        src["files"]["evidence"][0]["ocr_confidence"] = below_one
        self.assertIn(b"0.99999999999999999999999999999", archive(src))

    def test_file_order_is_deterministic_but_record_contents_not_rewritten(self):
        src = source()
        before = copy.deepcopy(src)
        first = archive(src)
        for records in src["files"].values():
            records.reverse()
        self.assertEqual(first, archive(src))
        self.assertEqual(src["files"]["questions"], list(reversed(before["files"]["questions"])))

    def test_inventory_each_file_hash_bytes_and_count_are_verified(self):
        for name in tr.MODELS:
            for field, bad in (("sha256", "0" * 64), ("bytes", 0), ("records", 0),
                               ("records", True)):
                with self.subTest(name=name, field=field):
                    obj = strict_loads(archive())
                    obj["inventory"][name][field] = bad
                    with self.assertRaisesRegex(tr.TransferError, "integrity_mismatch"):
                        tr.import_archive(canonical_json(obj))

    def test_changed_payload_rejected_without_updated_hash(self):
        obj = strict_loads(archive())
        obj["files"]["evidence"][0]["raw_content"] = "tampered private marker"
        with self.assertRaisesRegex(tr.TransferError, "integrity_mismatch"):
            tr.import_archive(canonical_json(obj))

    def test_unknown_missing_and_duplicate_fields_rejected(self):
        for location in ((), ("metadata",), ("files",), ("inventory",), ("inventory", "questions")):
            obj = strict_loads(archive())
            node = obj
            for key in location:
                node = node[key]
            node["unexpected"] = "secret-marker"
            with self.subTest(location=location), self.assertRaises(tr.TransferError):
                tr.import_archive(canonical_json(obj))
        blob = archive().replace(b'"format":', b'"format":"duplicate","format":', 1)
        with self.assertRaisesRegex(tr.TransferError, "invalid_json"):
            tr.import_archive(blob)
        src = source()
        del src["files"]["source_answers"]
        with self.assertRaises(tr.TransferError):
            archive(src)

    def test_duplicate_ids_and_orphan_cross_part_rejected_even_with_valid_hash(self):
        for name in tr.MODELS:
            src = source()
            src["files"][name].append(copy.deepcopy(src["files"][name][0]))
            with self.subTest(name=name), self.assertRaisesRegex(tr.TransferError, "duplicate_record_id"):
                archive(src)
        obj = strict_loads(archive())
        obj["files"]["semantic_answers"][0]["question_id"] = "unknown-q"
        with self.assertRaisesRegex(tr.TransferError, "bundle_invalid"):
            tr.import_archive(rehash(obj))

    def test_source_document_identity_and_page_boundary_enforced(self):
        for key, bad in (("document_id", "other"), ("document_sha256", "b" * 64),
                         ("page_index", 500)):
            src = source()
            for unit in src["files"]["evidence"]:
                for region in unit["regions"]:
                    region[key] = bad
            with self.subTest(key=key), self.assertRaisesRegex(tr.TransferError, "source_identity_mismatch"):
                archive(src)
        src = source()
        src["files"]["evidence"][0]["regions"][0]["page_index"] = 499
        archive(src)  # P-1 legal; original zero-index pages also preserved.

    def test_structurally_invalid_records_rejected_after_integrity_pass(self):
        obj = strict_loads(archive())
        obj["files"]["questions"][0]["ordinal"] = "1"
        with self.assertRaisesRegex(tr.TransferError, "schema_invalid"):
            tr.import_archive(rehash(obj))
        src = source()
        src["files"]["questions"][0]["ordinal"] = 0
        with self.assertRaisesRegex(tr.TransferError, "bundle_invalid"):
            archive(src)

    def test_minimal_public_projection_allowlist_and_ordinal_order(self):
        obj = strict_loads(archive())
        obj["files"]["semantic_answers"][0]["reference_answer"] = "PRIVATE-GOLD-MARKER"
        obj["files"]["evidence"][0]["raw_content"] = "PRIVATE-EVIDENCE-MARKER"
        obj["files"]["validations"][0]["reason"] = "PRIVATE-VERIFIER-MARKER"
        projection = tr.participant_questions(rehash(obj))
        rows = strict_loads(projection)
        self.assertEqual([q["question_id"] for q in rows], ["q_001", "q_002", "q_003"])
        self.assertTrue(all(set(q) == {"question_id", "question_text", "language"} for q in rows))
        for secret in (b"PRIVATE-", b"evidence_id", b"visual_object", b"sha256", b"quota", b"inventory"):
            self.assertNotIn(secret, projection)
        with self.assertRaises(tr.TransferError):
            tr.participant_questions(archive().replace(b'"sha256":', b'"broken":', 1))

    def test_import_returns_detached_snapshots_and_revalidation_catches_mutation(self):
        body = archive()
        first = tr.import_archive(body)
        first["files"]["source_answers"][0]["evidence_sets"][0]["evidence_ids"] = ["not-present"]
        self.assertNotEqual(first, tr.import_archive(body))
        with self.assertRaisesRegex(tr.TransferError, "bundle_invalid"):
            tr.export_archive(canonical_json(first))

    def test_size_records_depth_and_nodes_bounded(self):
        raw = canonical_json(source())
        with patch.object(tr, "MAX_BYTES", len(raw) - 1):
            with self.assertRaisesRegex(tr.TransferError, "payload_too_large"):
                tr.export_archive(raw)
        with patch.object(tr, "MAX_RECORDS", 1):
            with self.assertRaisesRegex(tr.TransferError, "record_limit"):
                archive()
        with self.assertRaisesRegex(tr.TransferError, "structure_limit"):
            tr.import_archive(b"[" * 65 + b"0" + b"]" * 65)
        with patch.object(tr, "MAX_NODES", 20):
            with self.assertRaisesRegex(tr.TransferError, "structure_limit"):
                tr.import_archive(b"[" + b"0," * 100 + b"0]")
        # Brackets in quoted source text do not count as nesting.
        src = source()
        src["files"]["questions"][0]["question_text"] = "[" * 100
        archive(src)

    def test_invalid_encoding_nonfinite_and_duplicate_nested_keys(self):
        for bad in (b"\xff", b"\xef\xbb\xbf{}", b'{"x":NaN}', b'{"x":1e999}',
                    b'{"x":{"a":1,"a":2}}', b'{"x":"\\ud800"}'):
            with self.subTest(bad=bad), self.assertRaises(tr.TransferError):
                tr.import_archive(bad)
        for bad in (None, "{}", bytearray(b"{}")):
            with self.assertRaisesRegex(tr.TransferError, "bytes_required"):
                tr.import_archive(bad)

    def test_fractional_integer_tokens_rejected_before_canonical_hashing(self):
        for location, field, bad in (("metadata", "page_count", 500.0),
                                     ("questions", "ordinal", 1.0),
                                     ("evidence", "page_index", 17.0)):
            src = source()
            if location == "metadata":
                src[location][field] = bad
            elif location == "questions":
                src["files"][location][0][field] = bad
            else:
                src["files"][location][0]["regions"][0][field] = bad
            # stdlib dumps retains 500.0 / 1.0; canonical hashing would erase it.
            with self.subTest(location=location), self.assertRaisesRegex(tr.TransferError, "schema_invalid"):
                tr.export_archive(json.dumps(src).encode())
        body = archive()
        for old, new in ((b'"page_count":500', b'"page_count":500.0'),
                         (b'"page_count":500', b'"page_count":500e0'),
                         (b'"ordinal":0', b'"ordinal":0.0')):
            with self.assertRaisesRegex(tr.TransferError, "schema_invalid"):
                tr.import_archive(body.replace(old, new))

    def test_huge_strict_integer_canonical_type_drift_is_rejected(self):
        src = source()
        src["files"]["questions"][0]["ordinal"] = 10 ** 21
        with self.assertRaisesRegex(tr.TransferError, "schema_invalid"):
            tr.export_archive(json.dumps(src).encode())
        src["files"]["questions"][0]["ordinal"] = 100000
        body = tr.export_archive(json.dumps(src).encode())
        tr.import_archive(body)

    def test_unknown_version_and_canonicalization_fail_closed(self):
        for field in ("format", "canonicalization"):
            obj = strict_loads(archive())
            obj[field] = "unknown"
            with self.assertRaisesRegex(tr.TransferError, "unsupported_format"):
                tr.import_archive(canonical_json(obj))

    def test_optional_frozen_manifest_preserved_and_checked_not_generated(self):
        src = source()
        hashes = {name: hashlib.sha256(canonical_json(sorted(src["files"][name],
                     key=lambda r: r["question_id"]))).hexdigest() for name in list(tr.MODELS)[:4]}
        manifest = {"schema_version": "v1", **src["metadata"], "file_hashes": hashes,
                    "n_target": 500, "n_valid": 3, "q_min": 50,
                    "quota_counts": {"general": 1, "cross_page": 1, "chart_table": 0, "confusable": 1},
                    "compliance": "partial", "exceptions": ["Synthetic incomplete contract fixture"],
                    "generator_config_version": "g1", "validator_config_version": "v1",
                    "selection_rule_version": "s1", "frozen_at": "2026-11-05T09:00:00+08:00"}
        src["source_manifest"] = manifest
        self.assertEqual(tr.import_archive(archive(src))["source_manifest"], manifest)
        for field, bad, reason in (("revision_id", "different", "manifest_identity_mismatch"),
                                   ("n_valid", 4, "schema_invalid"),
                                   ("file_hashes", {**hashes, "questions": "0" * 64}, "source_manifest_hash_mismatch")):
            changed = copy.deepcopy(src)
            changed["source_manifest"][field] = bad
            with self.subTest(field=field), self.assertRaisesRegex(tr.TransferError, reason):
                archive(changed)

    def test_cli_actual_roundtrip_private_modes_exclusive_outputs_and_sanitized_errors(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp, package, restored, public = (td / name for name in ("source.json", "private.json", "restored.json", "questions.json"))
            inp.write_bytes(canonical_json(source()))
            def run(*args):
                env = {**os.environ, "PYTHONPATH": str(ROOT / "contracts")}
                return subprocess.run([sys.executable, "-m", "lab_contracts.transfer", *map(str, args)],
                                      env=env, capture_output=True, timeout=10)
            for cmd, src, dest in (("export", inp, package), ("import", package, restored), ("project", package, public)):
                result = run(cmd, src, "--output", dest)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(run("check", package).returncode, 0)
            self.assertEqual(tr.export_archive(restored.read_bytes()), package.read_bytes())
            original = public.read_bytes()
            result = run("project", package, "--output", public)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(public.read_bytes(), original)
            link = td / "link"
            link.symlink_to(package)
            self.assertEqual(run("check", link).returncode, 2)
            fifo = td / "fifo"
            os.mkfifo(fifo)
            self.assertEqual(run("check", fifo).returncode, 2)
            inp.write_text('{"secret-marker":"\\ud800"}')
            result = run("export", inp, "--output", td / "bad")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(b"secret-marker", result.stderr)
            self.assertFalse((td / "bad").exists())
