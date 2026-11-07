"""Tests for SPEC v0.1 release evidence inventory and checker (Issue #15)."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import yaml

from tools.governance import check_release_evidence as cre
from tools.governance import check_traceability as ct

ROOT = Path(__file__).resolve().parents[2]
TRACE_FILE = ROOT / "docs" / "traceability.yaml"
SPEC_FILE = ROOT / "docs" / "document-seperater-playground_SPEC_v0.1.md"

VALID_COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
VALID_RUN_URL = "https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/42146572035"
HERE = "tests/governance/test_check_release_evidence.py"


def _make_evidence(**kw) -> dict:
    ev = {
        "branch": "main",
        "commit": VALID_COMMIT,
        "schema": "v1",
        "fixture": "F01",
        "profile": "ci-linux-x86_64",
        "actual": "passed",
        "trace": VALID_RUN_URL,
        "run": VALID_RUN_URL,
        "tests": [HERE],
    }
    ev.update(kw)
    return ev


def _make_minimal_doc() -> dict:
    """Document with all 182 mandatory IDs in not_started status."""
    return {
        "requirements": [{"id": r, "title": "req", "owners": [15], "release_owner": 15,
                          "status": "not_started", "evidence": [], "note": None}
                         for r in cre.MANDATORY_REQUIREMENTS],
        "acceptance": [{"id": a, "title": "acc", "owners": [15], "release_owner": 15,
                        "status": "not_started", "evidence": [], "note": None}
                       for a in cre.MANDATORY_ACCEPTANCES],
        "tests": [{"id": t, "title": "test", "owners": [15], "release_owner": 15,
                   "status": "not_started", "evidence": [], "note": None}
                  for t in cre.MANDATORY_TESTS],
    }


class ReleaseEvidenceInventoryTests(unittest.TestCase):
    def test_mandatory_constants(self):
        self.assertEqual(len(cre.MANDATORY_REQUIREMENTS), 30)
        self.assertEqual(len(cre.MANDATORY_ACCEPTANCES), 48)
        self.assertEqual(len(cre.MANDATORY_TESTS), 104)
        self.assertEqual(len(cre.ALL_MANDATORY_IDS), 182)

    def test_current_repo_traceability(self):
        """Current repository state must be honestly reported without pretending release is complete."""
        self.assertTrue(TRACE_FILE.is_file())
        doc = yaml.safe_load(TRACE_FILE.read_text(encoding="utf-8"))
        spec_text = SPEC_FILE.read_text(encoding="utf-8")

        errors, inv = cre.check_release_evidence(doc, spec_text=spec_text, require_release_pass=False)
        self.assertEqual(errors, [])
        self.assertEqual(inv.total_mandatory, 182)
        self.assertEqual(inv.status_counts["missing"], 0)
        self.assertEqual(inv.status_counts["blocked"], 1)
        self.assertEqual(inv.status_counts["partial"], 7)
        self.assertEqual(inv.status_counts["not_started"], 174)
        self.assertEqual(inv.status_counts["passed"], 0)
        self.assertIn("T83", [b["id"] for b in inv.blocked_items])
        self.assertIn(inv.verdict, {"RELEASE_BLOCKED", "RELEASE_NOT_READY"})

        # Under release gate, this MUST fail-closed because #14 and release gates are not finished
        rel_errors, _ = cre.check_release_evidence(doc, spec_text=spec_text, require_release_pass=True)
        self.assertTrue(any("mandatory items are not passed" in e for e in rel_errors))
        self.assertTrue(any("items are blocked" in e for e in rel_errors))

    def test_missing_mandatory_id_rejected(self):
        doc = _make_minimal_doc()
        doc["requirements"] = [r for r in doc["requirements"] if r["id"] != "R01"]
        errors, inv = cre.check_release_evidence(doc)
        self.assertTrue(any("missing mandatory SPEC IDs: ['R01']" in e for e in errors))
        self.assertIn("R01", inv.missing_items)
        self.assertEqual(inv.verdict, "RELEASE_FAILED")

    def test_extra_non_spec_id_rejected(self):
        doc = _make_minimal_doc()
        doc["tests"].append({"id": "T999", "title": "fake", "status": "not_started", "evidence": []})
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("unrecognized non-SPEC IDs: ['T999']" in e for e in errors))

    def test_skipped_item_tracked_and_blocks_release(self):
        doc = _make_minimal_doc()
        doc["tests"][0]["status"] = "skipped"
        doc["tests"][0]["evidence"] = [{"branch": "main", "note": "offline"}]
        doc["tests"][0]["note"] = "Hardware unavailable"
        errors, inv = cre.check_release_evidence(doc, require_release_pass=False)
        self.assertEqual(errors, [])
        self.assertEqual(len(inv.skipped_items), 1)
        self.assertEqual(inv.skipped_items[0]["id"], "T01")

        # Under release mode, skipped item must prevent release pass
        rel_errors, _ = cre.check_release_evidence(doc, require_release_pass=True)
        self.assertTrue(any("items are skipped: ['T01']" in e for e in rel_errors))


class ProvenanceValidationTests(unittest.TestCase):
    def test_valid_passed_evidence(self):
        doc = _make_minimal_doc()
        doc["tests"][0]["status"] = "passed"
        doc["tests"][0]["note"] = "Full main CI evidence verified"
        doc["tests"][0]["evidence"] = [_make_evidence()]
        errors, _ = cre.check_release_evidence(doc)
        self.assertEqual(errors, [])

    def test_commit_sha_validation(self):
        # Fake commit with identical chars
        doc = _make_minimal_doc()
        doc["tests"][0]["status"] = "passed"
        doc["tests"][0]["note"] = "Test note"
        doc["tests"][0]["evidence"] = [_make_evidence(commit="0" * 40)]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("fabricated commit SHA rejected" in e for e in errors))

        # Malformed commit length
        doc["tests"][0]["evidence"] = [_make_evidence(commit="shortsha")]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("commit must be 40-hex SHA" in e for e in errors))

    def test_non_main_branch_rejected_for_passed(self):
        doc = _make_minimal_doc()
        doc["tests"][0]["status"] = "passed"
        doc["tests"][0]["note"] = "Test note"
        doc["tests"][0]["evidence"] = [_make_evidence(branch="feature-x")]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("must run on main branch" in e for e in errors))

    def test_contract_scope_cannot_back_passed_release(self):
        doc = _make_minimal_doc()
        doc["tests"][0]["status"] = "passed"
        doc["tests"][0]["note"] = "Contract only"
        doc["tests"][0]["evidence"] = [_make_evidence(scope="contract")]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("scope 'contract' only supports partial" in e for e in errors))

    def test_missing_provenance_schema_fixture_profile_actual_trace(self):
        for field_name in ["schema", "fixture", "profile", "actual", "trace"]:
            doc = _make_minimal_doc()
            doc["tests"][0]["status"] = "passed"
            doc["tests"][0]["note"] = "Test note"
            ev = _make_evidence()
            ev[field_name] = ""
            if field_name == "trace":
                ev["run"] = None
            doc["tests"][0]["evidence"] = [ev]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any(f"missing provenance '{field_name}'" in e for e in errors),
                            f"Expected error for missing {field_name}, got: {errors}")

    def test_non_string_actual_rejected(self):
        for value in (True, 123, {"status": True}):
            with self.subTest(value=value):
                self.assertTrue(cre._validate_provenance_fields(_make_evidence(actual=value), "test"))

    def test_actual_not_passed_rejected(self):
        doc = _make_minimal_doc()
        doc["tests"][0]["status"] = "passed"
        doc["tests"][0]["note"] = "Test note"
        doc["tests"][0]["evidence"] = [_make_evidence(actual="failed")]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("provenance 'actual' must be passed" in e for e in errors))


class RealGateRestrictionTests(unittest.TestCase):
    def test_missing_positive_real_gate_evidence_rejected(self):
        for item in ("T85", "T84", "T47", "T21", "T81", "T79"):
            with self.subTest(item=item):
                errors = cre._check_gate_mock_restrictions(item, _make_evidence(), "", item)
                self.assertTrue(errors, item)

    def test_release_cannot_disable_gate_checks(self):
        errors, _ = cre.check_release_evidence(_make_minimal_doc(), strict_gates=False,
                                                require_release_pass=True)
        self.assertIn("release mode cannot disable strict gates", errors)
        self.assertEqual(cre.main(["--release", "--no-strict-gates"]), 1)

    def test_positive_gate_declarations(self):
        evidence = _make_evidence(parser_mode="real", parser_version="pinned-v1",
                                  parser_evidence="artifact:parser")
        self.assertEqual(cre._check_gate_mock_restrictions("T85", evidence, "", "T85"), [])


    def test_real_parser_gate_rejects_mock(self):
        for pid in ["T85", "T88", "T104"]:
            doc = _make_minimal_doc()
            target = next(t for t in doc["tests"] if t["id"] == pid)
            target["status"] = "passed"
            target["note"] = "Parser run"
            target["evidence"] = [_make_evidence(mock=True)]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real Parser Gate] mock parser is strictly prohibited" in e for e in errors),
                            f"Expected gate violation for {pid}")

            # parser_mode mock
            target["evidence"] = [_make_evidence(parser_mode="mock")]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real Parser Gate] parser_mode 'mock' rejected" in e for e in errors))

    def test_real_provider_gate_rejects_mock(self):
        for pid in ["T84", "T104"]:
            doc = _make_minimal_doc()
            target = next(t for t in doc["tests"] if t["id"] == pid)
            target["status"] = "passed"
            target["note"] = "Provider run"
            target["evidence"] = [_make_evidence(provider_mode="mock")]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real Provider Gate] provider_mode 'mock' rejected" in e for e in errors),
                            f"Expected gate violation for {pid}")

            target["evidence"] = [_make_evidence(mock=True)]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real Provider Gate] mock provider is strictly prohibited" in e for e in errors))

    def test_real_db_gate_rejects_sqlite_and_mock(self):
        for tid in ["T47", "T83"]:
            doc = _make_minimal_doc()
            target = next(t for t in doc["tests"] if t["id"] == tid)
            target["status"] = "passed"
            target["note"] = "DB test"
            target["evidence"] = [_make_evidence(database="sqlite")]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real DB Gate] SQLite/mock DB rejected" in e for e in errors),
                            f"Expected SQLite rejection for {tid}")

    def test_human_review_gate_rejects_auto_pass(self):
        for tid in ["T21", "T100"]:
            doc = _make_minimal_doc()
            target = next(t for t in doc["tests"] if t["id"] == tid)
            target["status"] = "passed"
            target["note"] = "Review check"
            target["evidence"] = [_make_evidence(human=False)]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Human Review Gate] automated bypass" in e for e in errors))

    def test_performance_gate_requires_scale_and_metrics(self):
        doc = _make_minimal_doc()
        target = next(t for t in doc["tests"] if t["id"] == "T81")
        target["status"] = "passed"
        target["note"] = "Scale test"
        target["evidence"] = [_make_evidence(pages=20)]  # < 500 pages
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("[Performance Gate] T81 requires >= 500 pages" in e for e in errors))

    def test_restore_gate_rejects_unverified_restore(self):
        doc = _make_minimal_doc()
        target = next(t for t in doc["tests"] if t["id"] == "T79")
        target["status"] = "passed"
        target["note"] = "Backup restore"
        target["evidence"] = [_make_evidence(restore_verified=False)]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("[Restore Gate] mock/unverified restore rejected" in e for e in errors))


class AcceptanceDependencyTests(unittest.TestCase):
    def test_acceptance_passed_fails_if_mapped_tests_unpassed(self):
        doc = _make_minimal_doc()
        # A43 maps to T85, T86, T87, T88, T104
        target_a = next(a for a in doc["acceptance"] if a["id"] == "A43")
        target_a["status"] = "passed"
        target_a["note"] = "Dual parser verified"
        target_a["evidence"] = [_make_evidence()]

        # T85, T88, etc. are still not_started
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(
            any("acceptance[A43]: status 'passed' rejected because mapped tests" in e for e in errors)
        )

    def test_acceptance_passed_succeeds_when_all_mapped_tests_passed(self):
        doc = _make_minimal_doc()
        # A03 maps only to T16
        target_t16 = next(t for t in doc["tests"] if t["id"] == "T16")
        target_t16["status"] = "passed"
        target_t16["note"] = "T16 passed on main"
        target_t16["evidence"] = [_make_evidence()]

        target_a03 = next(a for a in doc["acceptance"] if a["id"] == "A03")
        target_a03["status"] = "passed"
        target_a03["note"] = "A03 verified with T16"
        target_a03["evidence"] = [_make_evidence()]

        errors, _ = cre.check_release_evidence(doc)
        self.assertEqual(errors, [])


class CliAndFormattingTests(unittest.TestCase):
    def test_format_inventory_report_text(self):
        _, inv = cre.build_inventory(_make_minimal_doc())
        text = cre.format_inventory_report(inv, fmt="text")
        self.assertIn("LAB SPEC v0.1 Release Evidence Inventory", text)
        self.assertIn("Real Parser Gate", text)
        self.assertIn("182 total", text)

    def test_format_inventory_report_json(self):
        _, inv = cre.build_inventory(_make_minimal_doc())
        raw_json = cre.format_inventory_report(inv, fmt="json")
        data = json.loads(raw_json)
        self.assertEqual(data["total_mandatory"], 182)
        self.assertIn("gate_counts", data)
        self.assertIn("status_counts", data)

    def test_cli_check_mode(self):
        # --check on repo traceability file exits 0
        rc = cre.main(["--check"])
        self.assertEqual(rc, 0)

    def test_cli_release_mode_fails_closed(self):
        # --release on repo traceability file exits 1
        rc = cre.main(["--release"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
