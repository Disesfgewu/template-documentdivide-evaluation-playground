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


def _make_fully_passed_doc(evidence_factory=None) -> dict:
    """Document with all 182 mandatory IDs in passed status."""
    doc = _make_minimal_doc()
    factory = evidence_factory or (lambda item_id: [_make_evidence()])
    for section in cre.MANDATORY_SECTIONS:
        for entry in doc[section]:
            entry["status"] = "passed"
            entry["note"] = f"Verified pass for {entry['id']}"
            entry["evidence"] = factory(entry["id"])
    return doc


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
        self.assertEqual(inv.verdict, "RELEASE_BLOCKED")

        # Under release gate, this MUST fail-closed because #14 and release gates are not finished
        rel_errors, rel_inv = cre.check_release_evidence(doc, spec_text=spec_text, require_release_pass=True)
        self.assertTrue(any("mandatory items are not passed" in e for e in rel_errors))
        self.assertTrue(any("items are blocked" in e for e in rel_errors))
        self.assertEqual(rel_inv.verdict, "RELEASE_BLOCKED")

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
        for value in (True, 123, ["passed"], {"status": True}, {"status": 123}, {"status": "failed"}):
            with self.subTest(value=value):
                problems = cre._validate_provenance_fields(_make_evidence(actual=value), "test")
                self.assertTrue(any("provenance 'actual'" in p for p in problems),
                                f"Expected provenance actual error for {value!r}, got: {problems}")

    def test_actual_not_passed_rejected(self):
        doc = _make_minimal_doc()
        doc["tests"][0]["status"] = "passed"
        doc["tests"][0]["note"] = "Test note"
        doc["tests"][0]["evidence"] = [_make_evidence(actual="failed")]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("provenance 'actual' must be passed" in e for e in errors))


class RealGateRestrictionTests(unittest.TestCase):
    def test_missing_positive_real_gate_evidence_rejected(self):
        """Absence of mock flag is not proof; positive attestations are mandatory."""
        for item in ("T85", "T84", "T47", "T21", "T81", "T79"):
            with self.subTest(item=item):
                errors = cre._check_gate_mock_restrictions(item, _make_evidence(), "", item)
                self.assertTrue(errors, f"Expected positive gate requirement violation for {item}")

    def test_positive_gate_declarations(self):
        evidence = _make_evidence(
            parser_mode="real",
            parser_version="pinned-v1",
            parser_evidence="artifact:parser/raw.json",
        )
        self.assertEqual(cre._check_gate_mock_restrictions("T85", evidence, "", "T85"), [])

    def test_real_parser_gate_rejects_mock(self):
        for pid in ["T85", "T88"]:
            doc = _make_minimal_doc()
            target = next(t for t in doc["tests"] if t["id"] == pid)
            target["status"] = "passed"
            target["note"] = "Parser run"
            target["evidence"] = [_make_evidence(
                parser_mode="mock",
                parser_version="v1",
                parser_evidence="artifact:out",
            )]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real Parser Gate]" in e for e in errors),
                            f"Expected gate violation for {pid}")

    def test_real_provider_gate_rejects_mock(self):
        for pid in ["T84"]:
            doc = _make_minimal_doc()
            target = next(t for t in doc["tests"] if t["id"] == pid)
            target["status"] = "passed"
            target["note"] = "Provider run"
            target["evidence"] = [_make_evidence(
                provider_mode="mock",
                provider_version="v1",
                usage_evidence="artifact:ledger",
                human_verified=True,
                human_review_evidence="https://github.com/signoff",
            )]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real Provider Gate]" in e for e in errors),
                            f"Expected gate violation for {pid}")

    def test_real_db_gate_rejects_sqlite_and_mock(self):
        for tid in ["T47", "T83"]:
            doc = _make_minimal_doc()
            target = next(t for t in doc["tests"] if t["id"] == tid)
            target["status"] = "passed"
            target["note"] = "DB test"
            target["evidence"] = [_make_evidence(
                database="sqlite",
                pgvector_version="0.8.6",
                database_evidence="artifact:db",
            )]
            errors, _ = cre.check_release_evidence(doc)
            self.assertTrue(any("[Real DB Gate]" in e for e in errors),
                            f"Expected SQLite rejection for {tid}")

    def test_human_review_gate_rejects_auto_pass_and_bot(self):
        for tid in ["T21", "T100"]:
            with self.subTest(tid=tid):
                doc = _make_minimal_doc()
                target = next(t for t in doc["tests"] if t["id"] == tid)
                target["status"] = "passed"
                target["note"] = "Review check"
                # auto_pass or bot
                target["evidence"] = [_make_evidence(
                    human_verified=True,
                    human_review_evidence="https://github.com/signoff",
                    by="coding-agent",
                )]
                errors, _ = cre.check_release_evidence(doc)
                self.assertTrue(any("[Human Review Gate] human review cannot be signed off by bot/agent" in e for e in errors))

    def test_performance_gate_requires_scale_and_metrics(self):
        doc = _make_minimal_doc()
        target = next(t for t in doc["tests"] if t["id"] == "T81")
        target["status"] = "passed"
        target["note"] = "Scale test"
        target["evidence"] = [_make_evidence(
            performance_evidence="artifact:profile",
            pages=20,  # < 500 pages
        )]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("[Performance Gate] T81 requires integer pages >= 500" in e for e in errors))

    def test_restore_gate_rejects_unverified_restore(self):
        doc = _make_minimal_doc()
        target = next(t for t in doc["tests"] if t["id"] == "T79")
        target["status"] = "passed"
        target["note"] = "Backup restore"
        target["evidence"] = [_make_evidence(
            restore_verified=False,
            restore_evidence="artifact:restore.log",
        )]
        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(any("[Restore Gate]" in e for e in errors))

    def test_multi_gate_item_requires_all_gate_attestations(self):
        """T104 belongs to real_parser, real_provider, and performance gates."""
        # Evidence only has real_parser
        ev = _make_evidence(
            parser_mode="real",
            parser_version="pinned-v1",
            parser_evidence="artifact:parser",
        )
        violations = cre._check_gate_mock_restrictions("T104", ev, "", "T104")
        self.assertTrue(any("Real Provider Gate" in v for v in violations))
        self.assertTrue(any("Performance Gate" in v for v in violations))

        # Complete all 3 gates for T104
        ev_complete = _make_evidence(
            parser_mode="real",
            parser_version="pinned-v1",
            parser_evidence="artifact:parser",
            provider_mode="real",
            provider_version="vlm-v1",
            usage_evidence="artifact:ledger",
            performance_evidence="artifact:perf",
            pages=500,
        )
        self.assertEqual(cre._check_gate_mock_restrictions("T104", ev_complete, "", "T104"), [])


class AdversarialBypassTests(unittest.TestCase):
    def test_adversarial_182_all_passed_structurally_unverified_rejected(self):
        """Host bypass reproduction: 182 passed structurally without verified provenance must fail-closed."""
        doc = _make_fully_passed_doc()
        # In release mode with verify=None (offline / unverified), this MUST fail-closed with RELEASE_BLOCKED
        errors, inv = cre.check_release_evidence(doc, require_release_pass=True, verify=None)
        self.assertTrue(any("release mode requires verified provenance" in e for e in errors))
        self.assertEqual(inv.verdict, "RELEASE_BLOCKED")

    def test_adversarial_release_with_no_strict_gates_rejected(self):
        """Host bypass reproduction: --no-strict-gates cannot weaken release mode."""
        errors, inv = cre.check_release_evidence(_make_minimal_doc(), strict_gates=False, require_release_pass=True)
        self.assertTrue(any("release mode cannot disable strict gates" in e for e in errors))
        # CLI flag combination must exit with code 1
        rc = cre.main(["--release", "--no-strict-gates"])
        self.assertEqual(rc, 1)

    def test_adversarial_unknown_synthetic_source_rejected(self):
        """Host bypass reproduction: synthetic/unknown fixture and source must be rejected."""
        for bad_fix in ("synthetic", "unknown", "mock", "unregistered"):
            with self.subTest(fixture=bad_fix):
                ev = _make_evidence(fixture=bad_fix)
                problems = cre._validate_provenance_fields(ev, "test")
                self.assertTrue(any("unverified/synthetic fixture rejected" in p for p in problems))

        for bad_src in ("synthetic", "unknown", "model", "model_generated"):
            with self.subTest(source=bad_src):
                ev = _make_evidence(source=bad_src)
                problems = cre._validate_provenance_fields(ev, "test")
                self.assertTrue(any("unverified/synthetic source rejected" in p for p in problems))

        ev_syn = _make_evidence(synthetic=True)
        problems = cre._validate_provenance_fields(ev_syn, "test")
        self.assertTrue(any("synthetic flag rejected" in p for p in problems))

    def test_adversarial_mere_filename_or_freetext_trace_rejected(self):
        """Host bypass reproduction: mere filenames and free-text claims cannot be trace provenance."""
        for bad_trace in ("test.log", "run.txt", "ci_tests.log", "all tests passed", "local manual test"):
            with self.subTest(trace=bad_trace):
                ev = _make_evidence(trace=bad_trace, run=None)
                problems = cre._validate_provenance_fields(ev, "test")
                self.assertTrue(any("trace reference must be a verifiable CI run URL" in p for p in problems),
                                f"Expected mere filename rejection for {bad_trace!r}, got: {problems}")

    def test_adversarial_arbitrary_freetext_gate_evidence_rejected(self):
        """Host bypass reproduction: arbitrary non-reference strings like 'not actually a reference' must be rejected."""
        # 1. Host specific reproduction: T85 parser_mode=real, parser_version=1, parser_evidence="not actually a reference"
        bad_evidence = _make_evidence(
            parser_mode="real",
            parser_version="1",
            parser_evidence="not actually a reference",
        )
        violations = cre._check_gate_mock_restrictions("T85", bad_evidence, "", "T85")
        self.assertTrue(any("parser_evidence must be a verifiable URI or artifact reference" in v for v in violations),
                        f"Expected reference rejection for 'not actually a reference', got: {violations}")

        # 2. Free-text strings across other gate proof fields
        cases = [
            ("T84", {"provider_mode": "real", "provider_version": "1.0", "usage_evidence": "not a reference"}),
            ("T47", {"database": "postgresql", "pgvector_version": "0.8.6", "database_evidence": "tested locally"}),
            ("T21", {"human_verified": True, "human_review_evidence": "I looked at it"}),
            ("T81", {"performance_evidence": "fast run", "pages": 500}),
            ("T79", {"restore_verified": True, "restore_evidence": "restore was successful"}),
        ]
        for item_id, fields in cases:
            with self.subTest(item_id=item_id, fields=fields):
                ev = _make_evidence(**fields)
                v = cre._check_gate_mock_restrictions(item_id, ev, "", item_id)
                self.assertTrue(any("must be a verifiable URI or artifact reference" in x for x in v),
                                f"Expected reference error for {fields}, got: {v}")

    def test_adversarial_unrelated_ci_and_unverified_attestations_cannot_pass_release(self):
        """Host finding: Unrelated successful CI + free-text attestations must not produce release PASS."""
        doc = _make_fully_passed_doc(lambda item_id: [_make_evidence(
            parser_mode="real",
            parser_version="1.0",
            parser_evidence="artifact:parser/out.json",
            provider_mode="real",
            provider_version="1.0",
            usage_evidence="artifact:provider/usage.json",
            database="postgresql",
            pgvector_version="0.8.6",
            database_evidence="artifact:db/mig.json",
            human_verified=True,
            human_review_evidence="https://github.com/org/repo/pull/1#issuecomment-1",
            performance_evidence="artifact:perf/run.json",
            pages=500,
            restore_verified=True,
            restore_evidence="artifact:restore/drill.json",
        )])
        # Injected fake verifier simulates that all cited tests succeeded in CI on main
        fake_ci_verifier = lambda ev: []
        errors, inv = cre.check_release_evidence(doc, require_release_pass=True, verify=fake_ci_verifier)
        self.assertTrue(any("independent evidence verification not established" in e for e in errors),
                        f"Expected release pass blocker for unverified real gates, got: {errors}")
        self.assertEqual(inv.verdict, "RELEASE_BLOCKED")

    def test_check_mode_preserves_reporting_without_claiming_machine_verification(self):
        """In check/inventory mode, unverified real gate attestations remain blocked without claiming verification."""
        doc = _make_fully_passed_doc(lambda item_id: [_make_evidence(
            parser_mode="real",
            parser_version="1.0",
            parser_evidence="artifact:parser/out.json",
            provider_mode="real",
            provider_version="1.0",
            usage_evidence="artifact:provider/usage.json",
            database="postgresql",
            pgvector_version="0.8.6",
            database_evidence="artifact:db/mig.json",
            human_verified=True,
            human_review_evidence="https://github.com/org/repo/pull/1#issuecomment-1",
            performance_evidence="artifact:perf/run.json",
            pages=500,
            restore_verified=True,
            restore_evidence="artifact:restore/drill.json",
        )])
        errors, inv = cre.check_release_evidence(doc, require_release_pass=False)
        self.assertEqual(errors, [])
        self.assertEqual(inv.verdict, "RELEASE_BLOCKED")
        self.assertEqual(inv.gate_counts["real_parser"]["passed"], 0)
        self.assertEqual(inv.gate_counts["real_parser"]["declared_passed"], len(cre.REAL_GATES["real_parser"]["ids"]))
        self.assertFalse(inv.gate_counts["real_parser"]["independent_verification_established"])

        report = cre.format_inventory_report(inv, fmt="text")
        self.assertIn("BLOCKED: independent verification not established", report)
        self.assertIn("Verification Boundaries & Known Limitations", report)

    def test_gate_verifier_independent_verification_credits_gate(self):
        """When an explicit independent gate verifier confirms evidence, gate is credited."""
        ev = _make_evidence(
            parser_mode="real",
            parser_version="1.0",
            parser_evidence="artifact:parser/out.json",
        )
        gate_verifiers = {
            "real_parser": lambda item_id, evidence: [],
        }
        doc = _make_minimal_doc()
        target = next(t for t in doc["tests"] if t["id"] == "T85")
        target["status"] = "passed"
        target["note"] = "Parser verified"
        target["evidence"] = [ev]
        errors, inv = cre.check_release_evidence(doc, gate_verifiers=gate_verifiers)
        self.assertEqual(errors, [])
        self.assertEqual(inv.gate_counts["real_parser"]["passed"], 1)
        self.assertTrue(inv.gate_counts["real_parser"]["independent_verification_established"])

        # When gate verifier reports a failure, it must be recorded as a gate violation
        failing_verifiers = {
            "real_parser": lambda item_id, evidence: ["corrupted artifact hash"],
        }
        err_f, inv_f = cre.check_release_evidence(doc, gate_verifiers=failing_verifiers)
        self.assertTrue(any("corrupted artifact hash" in e for e in err_f))
        self.assertTrue(any("corrupted artifact hash" in gv for gv in inv_f.gate_violations))

    def test_inventory_declarations_alone_cannot_claim_release_pass(self):
        doc = _make_fully_passed_doc(lambda item_id: [])
        _, inventory = cre.build_inventory(doc)
        self.assertEqual(inventory.verdict, "RELEASE_NOT_READY")
        self.assertTrue(all(not gate["independent_verification_established"]
                            for gate in inventory.gate_counts.values()))

    def test_gate_callbacks_alone_cannot_claim_release_pass(self):
        doc = _make_fully_passed_doc(lambda item_id: [_make_evidence(
            parser_mode="real", parser_version="1.0", parser_evidence="artifact:parser/out.json",
            provider_mode="real", provider_version="1.0", usage_evidence="artifact:provider/usage.json",
            database="postgresql", pgvector_version="0.8.6", database_evidence="artifact:db/mig.json",
            human_verified=True, human_review_evidence="https://example.org/review",
            performance_evidence="artifact:perf/run.json", pages=500,
            restore_verified=True, restore_evidence="artifact:restore/drill.json",
        )])
        gate_verifiers = {gate: lambda item_id, evidence: [] for gate in cre.REAL_GATES}
        errors, inv = cre.check_release_evidence(doc, gate_verifiers=gate_verifiers)
        self.assertEqual(errors, [])
        self.assertNotEqual(inv.verdict, "RELEASE_PASS")
        _, unchecked = cre.check_release_evidence(
            doc, verify=lambda evidence: [], gate_verifiers=gate_verifiers, strict_gates=False,
        )
        self.assertNotEqual(unchecked.verdict, "RELEASE_PASS")


class DependencyTests(unittest.TestCase):
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

    def test_requirement_passed_fails_if_mapped_tests_unpassed(self):
        doc = _make_minimal_doc()
        # R01 maps to T01, T02, T03, T04
        target_r = next(r for r in doc["requirements"] if r["id"] == "R01")
        target_r["status"] = "passed"
        target_r["note"] = "Import requirements passed"
        target_r["evidence"] = [_make_evidence()]

        errors, _ = cre.check_release_evidence(doc)
        self.assertTrue(
            any("requirements[R01]: status 'passed' rejected because mapped tests" in e for e in errors)
        )


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
