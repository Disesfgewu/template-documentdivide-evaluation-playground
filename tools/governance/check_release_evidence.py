"""SPEC v0.1 fail-closed bounded release evidence inventory and checker (Issue #15).

Validates all mandatory SPEC IDs (R01–R30, A01–A48, T01–T104) and enforces
explicit statuses (passed, blocked, skipped, partial, not_started).
For release pass, every mandatory item must be passed with verified 6-part
provenance (commit, schema, fixture, profile, actual, trace).
Mock evidence is strictly prohibited for real gates:
  - Real Parser Gate (T85-T88, T104, A43, A48, R26, R30)
  - Real Provider Gate (T84, T104, A42, A48, R08, R25, R30)
  - Real DB/pgvector Gate (T47, T49, T50, T83, A21, A22, A42, R13, R17, R30)
  - Human Review Gate (T21, T84, T100, A44, A46, R06, R25)
  - Performance & Scale Gate (T81, T82, T104, A01, A48, R24, R25, R30)
  - Backup & Disposable Restore Gate (T79, T80, A29, A30, R24)

Parent prerequisite Issue #14 and release gates must never be fabricated.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import yaml

from tools.governance import check_traceability as ct

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "docs" / "document-seperater-playground_SPEC_v0.1.md"
TRACE_PATH = ROOT / "docs" / "traceability.yaml"

MANDATORY_REQUIREMENTS: Tuple[str, ...] = tuple(f"R{i:02d}" for i in range(1, 31))
MANDATORY_ACCEPTANCES: Tuple[str, ...] = tuple(f"A{i:02d}" for i in range(1, 49))
MANDATORY_TESTS: Tuple[str, ...] = tuple(f"T{i:02d}" if i < 100 else f"T{i}" for i in range(1, 105))

MANDATORY_SECTIONS: Dict[str, Tuple[str, ...]] = {
    "requirements": MANDATORY_REQUIREMENTS,
    "acceptance": MANDATORY_ACCEPTANCES,
    "tests": MANDATORY_TESTS,
}

ALL_MANDATORY_IDS: Set[str] = set(MANDATORY_REQUIREMENTS) | set(MANDATORY_ACCEPTANCES) | set(MANDATORY_TESTS)

STATUSES: Set[str] = {"not_started", "partial", "blocked", "skipped", "passed"}

ACCEPTANCE_TEST_MAP: Dict[str, List[str]] = {
    "A01": ["T14", "T81"],
    "A02": ["T14", "T16"],
    "A03": ["T16"],
    "A04": ["T30"],
    "A05": ["T10", "T37"],
    "A06": ["T17", "T37"],
    "A07": ["T11", "T41"],
    "A08": ["T08", "T09", "T39"],
    "A09": ["T35", "T41"],
    "A10": ["T36", "T40"],
    "A11": ["T22"],
    "A12": ["T22", "T30"],
    "A13": ["T44"],
    "A14": ["T07", "T24"],
    "A15": ["T34", "T48"],
    "A16": ["T43"],
    "A17": ["T43"],
    "A18": ["T70"],
    "A19": ["T35", "T36"],
    "A20": ["T23", "T74"],
    "A21": ["T51", "T52"],
    "A22": ["T49"],
    "A23": ["T63"],
    "A24": ["T65"],
    "A25": ["T23", "T71"],
    "A26": ["T68", "T78"],
    "A27": ["T67"],
    "A28": ["T69", "T70"],
    "A29": ["T79"],
    "A30": ["T80"],
    "A31": ["T80"],
    "A32": ["T22", "T28"],
    "A33": ["T61", "T64"],
    "A34": ["T62"],
    "A35": ["T58", "T60"],
    "A36": ["T53"],
    "A37": ["T54", "T55"],
    "A38": ["T56", "T57"],
    "A39": ["T19", "T25"],
    "A40": ["T66", "T67"],
    "A41": ["T41"],
    "A42": ["T83", "T84"],
    "A43": ["T85", "T86", "T87", "T88", "T104"],
    "A44": ["T89", "T90", "T91", "T92", "T93", "T100"],
    "A45": ["T94", "T95", "T96", "T97"],
    "A46": ["T98", "T99", "T100"],
    "A47": ["T102", "T103"],
    "A48": ["T101", "T102", "T104"],
}

REQUIREMENT_TEST_MAP: Dict[str, List[str]] = {
    "R01": ["T01", "T02", "T03", "T04"],
    "R02": ["T05", "T06", "T07", "T08"],
    "R03": ["T08", "T09", "T10", "T11"],
    "R04": ["T12", "T13", "T14"],
    "R05": ["T14", "T15", "T16", "T17"],
    "R06": ["T18", "T19", "T20", "T21"],
    "R07": ["T22", "T23", "T24"],
    "R08": ["T19", "T25", "T26", "T27", "T28", "T29"],
    "R09": ["T30", "T31", "T32", "T33", "T34"],
    "R10": ["T33", "T34", "T35", "T36"],
    "R11": ["T37", "T38", "T39", "T40", "T41", "T42", "T43", "T44"],
    "R12": ["T43", "T44", "T45", "T46"],
    "R13": ["T47", "T48", "T49", "T50"],
    "R14": ["T51", "T52"],
    "R15": ["T53", "T54", "T55", "T56", "T57"],
    "R16": ["T58", "T59", "T60"],
    "R17": ["T61", "T62", "T63", "T64"],
    "R18": ["T65", "T66", "T67", "T68"],
    "R19": ["T49", "T69", "T70", "T71", "T72"],
    "R20": ["T27", "T28", "T29", "T63"],
    "R21": ["T73", "T74", "T75", "T76"],
    "R22": ["T31", "T48", "T70", "T77"],
    "R23": ["T74", "T78"],
    "R24": ["T50", "T79", "T80", "T81", "T82"],
    "R25": ["T81", "T82", "T83", "T84"],
    "R26": ["T85", "T86", "T87", "T88", "T101", "T104"],
    "R27": ["T89", "T90", "T91", "T92", "T93", "T100"],
    "R28": ["T94", "T95", "T96", "T97"],
    "R29": ["T98", "T99", "T100", "T103"],
    "R30": ["T101", "T102", "T103", "T104"],
}

REAL_GATES: Dict[str, Dict[str, Any]] = {
    "real_parser": {
        "name": "Real Parser Gate",
        "ids": {"T85", "T86", "T87", "T88", "T104", "A43", "A48", "R26", "R30"},
        "description": "Requires real pinned Unstructured / MinerU parser execution, not mock or unpinned parser.",
    },
    "real_provider": {
        "name": "Real Provider Gate",
        "ids": {"T84", "T104", "A42", "A48", "R08", "R25", "R30"},
        "description": "Requires real chat/embedding/vision provider calls with tracked usage, not mock provider mode.",
    },
    "real_db": {
        "name": "Real PostgreSQL/pgvector Gate",
        "ids": {"T47", "T49", "T50", "T83", "A21", "A22", "A42", "R13", "R17", "R30"},
        "description": "Requires real PostgreSQL with pgvector extension, migrations, and transactional integrity, not SQLite/mock.",
    },
    "human_review": {
        "name": "Human Review & Calibration Gate",
        "ids": {"T21", "T84", "T100", "A44", "A46", "R06", "R25"},
        "description": "Requires human review/signoff for ambiguous questions, OCR conflicts, and judge calibration.",
    },
    "performance": {
        "name": "Performance & Scale Gate",
        "ids": {"T81", "T82", "T104", "A01", "A48", "R24", "R25", "R30"},
        "description": "Requires 500/600-page execution and recorded resource profiles (RSS, VRAM, latency), not synthetic stubs.",
    },
    "restore": {
        "name": "Backup & Disposable Restore Gate",
        "ids": {"T79", "T80", "A29", "A30", "R24"},
        "description": "Requires verified disposable restore of DB and artifact bundle with measured RTO/RPO.",
    },
}

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class EvidenceInventory:
    total_mandatory: int
    status_counts: Dict[str, int]
    gate_counts: Dict[str, Dict[str, int]]
    provenance_passed_count: int
    items_by_status: Dict[str, List[str]]
    blocked_items: List[Dict[str, Any]]
    skipped_items: List[Dict[str, Any]]
    missing_items: List[str]
    unpassed_mandatory: List[str]
    gate_violations: List[str]
    verdict: str  # "RELEASE_PASS", "RELEASE_BLOCKED", "RELEASE_NOT_READY"
    summary: str


def _validate_commit_sha(commit: Any) -> Optional[str]:
    s = str(commit or "").strip().lower()
    if not SHA_RE.fullmatch(s):
        return f"commit must be 40-hex SHA, got {commit!r}"
    if len(set(s)) <= 1:
        return f"fabricated commit SHA rejected: {commit!r}"
    return None


def _validate_provenance_fields(ev: Dict[str, Any], where: str) -> List[str]:
    """Validate 6-part provenance: commit, schema, fixture, profile, actual, trace."""
    problems = []

    # 1. Commit
    commit = ev.get("commit")
    sha_err = _validate_commit_sha(commit)
    if sha_err:
        problems.append(f"{where}: provenance commit error: {sha_err}")

    # Branch
    branch = ev.get("branch")
    if branch != "main":
        problems.append(f"{where}: release evidence must run on main branch, got {branch!r}")

    # Scope
    scope = ev.get("scope")
    if scope == "contract":
        problems.append(f"{where}: scope 'contract' only supports partial, cannot back passed release evidence")

    # 2. Schema
    schema = ev.get("schema") or ev.get("schema_version") or ev.get("schema_revision")
    if not schema or not isinstance(schema, str) or not schema.strip():
        problems.append(f"{where}: missing provenance 'schema' (schema version/contract revision)")

    # 3. Fixture
    fixture = ev.get("fixture") or ev.get("fixtures") or ev.get("fixture_hash") or ev.get("fixture_hashes")
    if not fixture:
        problems.append(f"{where}: missing provenance 'fixture' (fixture reference or hash)")

    # 4. Profile
    profile = ev.get("profile") or ev.get("environment") or ev.get("env")
    if not profile:
        problems.append(f"{where}: missing provenance 'profile' (environment, profile or seed)")

    # 5. Actual
    actual = ev.get("actual")
    if actual is None or (isinstance(actual, str) and not actual.strip()):
        problems.append(f"{where}: missing provenance 'actual' (test execution outcome)")
    elif isinstance(actual, str) and actual.strip().lower() != "passed":
        problems.append(f"{where}: provenance 'actual' must be passed, got {actual!r}")
    elif isinstance(actual, dict):
        status = actual.get("status")
        if not isinstance(status, str) or status.strip().lower() != "passed":
            problems.append(f"{where}: provenance 'actual' status must be passed, got {status!r}")
    elif not isinstance(actual, str):
        problems.append(f"{where}: provenance 'actual' must be a passed string or status mapping")

    # 6. Trace
    trace = ev.get("trace") or ev.get("run") or ev.get("trace_id") or ev.get("artifact_ref")
    if not trace or not isinstance(trace, str) or not trace.strip():
        problems.append(f"{where}: missing provenance 'trace' (run URL, trace ID, or artifact ref)")
    elif ev.get("run") and not ct.RUN_URL.fullmatch(str(ev.get("run"))):
        problems.append(f"{where}: run must be a CI run URL of this repository")

    # Tests discoverability
    tests = ev.get("tests")
    if not tests or not isinstance(tests, list):
        problems.append(f"{where}: missing discoverable tests list")
    else:
        for t in tests:
            if not isinstance(t, str) or not t.strip():
                problems.append(f"{where}: test entry must be non-empty string")
                continue
            path = t.split("::", 1)[0].split(" ", 1)[0]
            if "*" in path:
                problems.append(f"{where}: glob paths not allowed for passed evidence: {t}")
            elif not (ROOT / path).is_file():
                problems.append(f"{where}: test file does not exist: {path}")
            elif not ct._discoverable(path):
                problems.append(f"{where}: not a runner-discoverable test: {path}")
            elif "::" in t:
                prob = ct._named_test_problem(path, t)
                if prob:
                    problems.append(f"{where}: {prob}")

    return problems


def _check_gate_mock_restrictions(item_id: str, ev: Dict[str, Any], note: str, where: str) -> List[str]:
    """Reject mock/stub evidence for any designated real gate."""
    violations = []
    note_lower = (note or "").lower()

    # Positive declarations and a traceable evidence reference are required; absence
    # of a mock flag is not proof that the corresponding real gate was exercised.
    requirements = {
        "real_parser": (("parser_mode", "real"), ("parser_version", None), ("parser_evidence", None)),
        "real_provider": (("provider_mode", "real"), ("provider_version", None), ("usage_evidence", None)),
        "real_db": (("database", "postgresql"), ("pgvector_version", None), ("database_evidence", None)),
        "human_review": (("human_verified", True), ("human_review_evidence", None)),
        "performance": (("performance_evidence", None),),
        "restore": (("restore_verified", True), ("restore_evidence", None)),
    }
    for gate, fields in requirements.items():
        if item_id not in REAL_GATES[gate]["ids"]:
            continue
        for name, expected in fields:
            value = ev.get(name)
            valid = (isinstance(value, str) and bool(value.strip()) if expected is None
                     else type(value) is type(expected) and value == expected)
            if not valid:
                violations.append(f"{where}: [{REAL_GATES[gate]['name']}] missing or invalid {name} for {item_id}")
    if item_id in {"T81", "T104"}:
        pages = ev.get("pages")
        if type(pages) is not int or pages < 500:
            violations.append(f"{where}: [Performance Gate] {item_id} requires >= 500 pages")

    # Real Parser Gate
    if item_id in REAL_GATES["real_parser"]["ids"]:
        if ev.get("mock") is True or ev.get("mock_parser") is True or ev.get("is_mock") is True:
            violations.append(f"{where}: [Real Parser Gate] mock parser is strictly prohibited for {item_id}")
        if str(ev.get("parser_mode", "")).lower() == "mock" or str(ev.get("parser", "")).lower() == "mock":
            violations.append(f"{where}: [Real Parser Gate] parser_mode 'mock' rejected for {item_id}")
        if "mock parser" in note_lower:
            violations.append(f"{where}: [Real Parser Gate] note indicates mock parser for {item_id}")

    # Real Provider Gate
    if item_id in REAL_GATES["real_provider"]["ids"]:
        if ev.get("mock") is True or ev.get("is_mock") is True or ev.get("mock_provider") is True:
            violations.append(f"{where}: [Real Provider Gate] mock provider is strictly prohibited for {item_id}")
        if str(ev.get("provider_mode", "")).lower() == "mock":
            violations.append(f"{where}: [Real Provider Gate] provider_mode 'mock' rejected for {item_id}")
        if "mock provider" in note_lower:
            violations.append(f"{where}: [Real Provider Gate] note indicates mock provider for {item_id}")

    # Real DB / pgvector Gate
    if item_id in REAL_GATES["real_db"]["ids"]:
        db = str(ev.get("database", "")).lower()
        if ev.get("mock_db") is True or db in {"sqlite", "mock", "in_memory"}:
            violations.append(f"{where}: [Real DB Gate] SQLite/mock DB rejected for {item_id}; real PostgreSQL/pgvector required")
        if "sqlite" in note_lower and "not sqlite" not in note_lower:
            violations.append(f"{where}: [Real DB Gate] note indicates SQLite usage for {item_id}")

    # Human Review Gate
    if item_id in REAL_GATES["human_review"]["ids"]:
        if ev.get("human_verified") is False or ev.get("human") is False or ev.get("auto_pass") is True:
            violations.append(f"{where}: [Human Review Gate] automated bypass or false human signoff rejected for {item_id}")

    # Performance Gate
    if item_id in REAL_GATES["performance"]["ids"]:
        pages = ev.get("pages")
        if isinstance(pages, int) and pages < 500 and item_id in {"T81", "T104"}:
            violations.append(f"{where}: [Performance Gate] {item_id} requires >= 500 pages, got {pages}")

    # Restore Gate
    if item_id in REAL_GATES["restore"]["ids"]:
        if ev.get("restore_verified") is False or ev.get("mock_restore") is True:
            violations.append(f"{where}: [Restore Gate] mock/unverified restore rejected for {item_id}")

    return violations


def build_inventory(doc: Dict[str, Any], spec_text: Optional[str] = None) -> Tuple[List[str], EvidenceInventory]:
    """Extract inventory and validate mandatory SPEC coverage and structure."""
    errors: List[str] = []
    if not isinstance(doc, dict):
        return ["traceability document is not a mapping"], EvidenceInventory(
            total_mandatory=len(ALL_MANDATORY_IDS),
            status_counts={},
            gate_counts={},
            provenance_passed_count=0,
            items_by_status={},
            blocked_items=[],
            skipped_items=[],
            missing_items=sorted(ALL_MANDATORY_IDS),
            unpassed_mandatory=sorted(ALL_MANDATORY_IDS),
            gate_violations=[],
            verdict="RELEASE_FAILED",
            summary="Invalid document format",
        )

    # Validate against SPEC IDs if spec_text provided
    spec_parsed_ids = ct.spec_ids(spec_text) if spec_text else None

    status_counts: Dict[str, int] = {s: 0 for s in STATUSES}
    status_counts["missing"] = 0
    items_by_status: Dict[str, List[str]] = {s: [] for s in STATUSES}
    items_by_status["missing"] = []
    blocked_items: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    all_entries: Dict[str, Dict[str, Any]] = {}

    for section, prefix_tuple in MANDATORY_SECTIONS.items():
        entries = doc.get(section)
        if not isinstance(entries, list):
            errors.append(f"{section}: missing list")
            continue
        for e in entries:
            if not isinstance(e, dict):
                errors.append(f"{section}: entry is not a mapping")
                continue
            item_id = e.get("id")
            if not isinstance(item_id, str):
                errors.append(f"{section}: entry without string id")
                continue
            if item_id in seen_ids:
                errors.append(f"{section}[{item_id}]: duplicate ID")
            seen_ids.add(item_id)
            all_entries[item_id] = e

            status = e.get("status")
            if status not in STATUSES:
                errors.append(f"{section}[{item_id}]: bad status {status!r}")
                status = "missing"
            status_counts[status] = status_counts.get(status, 0) + 1
            items_by_status[status].append(item_id)

            if status == "blocked":
                blocked_items.append({
                    "id": item_id,
                    "title": e.get("title"),
                    "owners": e.get("owners"),
                    "note": e.get("note"),
                })
            elif status == "skipped":
                skipped_items.append({
                    "id": item_id,
                    "title": e.get("title"),
                    "owners": e.get("owners"),
                    "note": e.get("note"),
                })

    missing_mandatory = sorted(ALL_MANDATORY_IDS - seen_ids)
    if missing_mandatory:
        errors.append(f"missing mandatory SPEC IDs: {missing_mandatory}")
        status_counts["missing"] += len(missing_mandatory)
        items_by_status["missing"].extend(missing_mandatory)

    extra_ids = sorted(seen_ids - ALL_MANDATORY_IDS)
    if extra_ids:
        errors.append(f"unrecognized non-SPEC IDs: {extra_ids}")

    if spec_parsed_ids:
        for prefix_key, prefix_char in ct.SECTIONS.items():
            expected = spec_parsed_ids.get(prefix_char, set())
            found = {i for i in seen_ids if i.startswith(prefix_char)}
            if expected != found:
                missing_from_spec = sorted(expected - found)
                extra_from_spec = sorted(found - expected)
                if missing_from_spec:
                    errors.append(f"{prefix_key}: missing from parsed SPEC: {missing_from_spec}")
                if extra_from_spec:
                    errors.append(f"{prefix_key}: extra vs parsed SPEC: {extra_from_spec}")

    # Gate statistics
    gate_counts: Dict[str, Dict[str, int]] = {}
    for gate_key, gate_info in REAL_GATES.items():
        gate_ids = gate_info["ids"]
        passed_count = sum(1 for i in gate_ids if i in all_entries and all_entries[i].get("status") == "passed")
        gate_counts[gate_key] = {
            "total": len(gate_ids),
            "passed": passed_count,
            "unpassed": len(gate_ids) - passed_count,
        }

    unpassed = sorted(ALL_MANDATORY_IDS - set(items_by_status.get("passed", [])))

    if missing_mandatory or extra_ids:
        verdict = "RELEASE_FAILED"
    elif blocked_items:
        verdict = "RELEASE_BLOCKED"
    elif unpassed:
        verdict = "RELEASE_NOT_READY"
    else:
        verdict = "RELEASE_PASS"

    summary_parts = [
        f"{status_counts.get('passed', 0)} passed",
        f"{status_counts.get('blocked', 0)} blocked",
        f"{status_counts.get('partial', 0)} partial",
        f"{status_counts.get('not_started', 0)} not_started",
        f"{status_counts.get('skipped', 0)} skipped",
        f"{status_counts.get('missing', 0)} missing",
    ]
    summary = f"Mandatory SPEC items: {len(ALL_MANDATORY_IDS)} total ({', '.join(summary_parts)}). Verdict: {verdict}"

    inv = EvidenceInventory(
        total_mandatory=len(ALL_MANDATORY_IDS),
        status_counts=status_counts,
        gate_counts=gate_counts,
        provenance_passed_count=status_counts.get("passed", 0),
        items_by_status=items_by_status,
        blocked_items=blocked_items,
        skipped_items=skipped_items,
        missing_items=missing_mandatory,
        unpassed_mandatory=unpassed,
        gate_violations=[],
        verdict=verdict,
        summary=summary,
    )
    return errors, inv


def check_release_evidence(
    doc: Dict[str, Any],
    spec_text: Optional[str] = None,
    verify: Optional[Callable[[Dict[str, Any]], List[str]]] = None,
    strict_gates: bool = True,
    require_release_pass: bool = False,
) -> Tuple[List[str], EvidenceInventory]:
    """Validate release evidence completeness, provenance, gate restrictions, and dependencies."""
    structural_errors, inventory = build_inventory(doc, spec_text)
    errors: List[str] = list(structural_errors)
    gate_violations: List[str] = []

    all_entries: Dict[str, Dict[str, Any]] = {}
    for section in MANDATORY_SECTIONS:
        for e in doc.get(section, []) if isinstance(doc.get(section), list) else []:
            if isinstance(e, dict) and "id" in e:
                all_entries[e["id"]] = e

    if require_release_pass and verify is None:
        errors.append("release mode requires external provenance verification")
    if require_release_pass and not strict_gates:
        errors.append("release mode cannot disable strict gates")
        strict_gates = True

    # 1. Dependency checks: Acceptance items require their mapped tests to pass
    for a_id, mapped_tests in ACCEPTANCE_TEST_MAP.items():
        a_entry = all_entries.get(a_id)
        if not a_entry:
            continue
        if a_entry.get("status") == "passed":
            unpassed_deps = [
                t for t in mapped_tests
                if t not in all_entries or all_entries[t].get("status") != "passed"
            ]
            if unpassed_deps:
                errors.append(
                    f"acceptance[{a_id}]: status 'passed' rejected because mapped tests {unpassed_deps} are not passed"
                )

    # 2. Detailed check of each item's evidence
    for section in MANDATORY_SECTIONS:
        entries = doc.get(section, [])
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            item_id = e.get("id")
            if not item_id:
                continue
            where = f"{section}[{item_id}]"
            status = e.get("status")
            note = e.get("note") or ""
            evidence = e.get("evidence")

            if not isinstance(evidence, list):
                errors.append(f"{where}: evidence must be a list")
                continue

            # Non-not_started items require note and evidence
            if status in {"partial", "blocked", "skipped", "passed"}:
                if not evidence:
                    errors.append(f"{where}: {status} requires evidence")
                if not note:
                    errors.append(f"{where}: {status} requires a note naming what is (not) covered")

            # Blocked items must explain blocker
            if status == "blocked" and "blocked" not in note.lower() and "block" not in note.lower():
                errors.append(f"{where}: blocked entry must explain blocker in note")

            # Skipped items are explicitly tracked and always forbidden from release pass
            if status == "skipped":
                if not note:
                    errors.append(f"{where}: skipped entry requires explicit reason in note")

            # Check each evidence item
            for k, ev in enumerate(evidence):
                ev_where = f"{where}.evidence[{k}]"
                if not isinstance(ev, dict):
                    errors.append(f"{ev_where}: must be a mapping")
                    continue

                # If status is passed, check strict 6-part provenance
                if status == "passed":
                    prov_errs = _validate_provenance_fields(ev, ev_where)
                    errors.extend(prov_errs)

                    # Strict Gate check: no mock on real gates
                    if strict_gates:
                        gv = _check_gate_mock_restrictions(item_id, ev, note, ev_where)
                        gate_violations.extend(gv)
                        errors.extend(gv)

                    # External verifier (GitHub API) if provided
                    if verify is not None:
                        ver_errs = verify(ev)
                        errors.extend(f"{ev_where}: {msg}" for msg in ver_errs)

    inventory.gate_violations = gate_violations

    # Release mode requires 100% passed and ZERO blockers/unpassed items
    if require_release_pass:
        if inventory.unpassed_mandatory:
            errors.append(
                f"RELEASE_PASS failed: {len(inventory.unpassed_mandatory)} mandatory items are not passed "
                f"(first 10: {inventory.unpassed_mandatory[:10]})"
            )
        if inventory.blocked_items:
            blocked_ids = [b['id'] for b in inventory.blocked_items]
            errors.append(f"RELEASE_PASS failed: {len(blocked_ids)} items are blocked: {blocked_ids}")
        if inventory.skipped_items:
            skipped_ids = [s['id'] for s in inventory.skipped_items]
            errors.append(f"RELEASE_PASS failed: {len(skipped_ids)} items are skipped: {skipped_ids}")
        if gate_violations:
            errors.append(f"RELEASE_PASS failed: {len(gate_violations)} real gate mock violations detected")

    if errors:
        inventory.verdict = "RELEASE_BLOCKED" if inventory.blocked_items else "RELEASE_FAILED"
    elif inventory.blocked_items:
        inventory.verdict = "RELEASE_BLOCKED"
    elif inventory.unpassed_mandatory:
        inventory.verdict = "RELEASE_NOT_READY"
    else:
        inventory.verdict = "RELEASE_PASS"

    inventory.summary = re.sub(r"Verdict: \w+$", f"Verdict: {inventory.verdict}", inventory.summary)
    return errors, inventory


def format_inventory_report(inv: EvidenceInventory, fmt: str = "text") -> str:
    """Format inventory as human-readable text or JSON."""
    if fmt == "json":
        return json.dumps(asdict(inv), indent=2, ensure_ascii=False)

    lines = [
        "=" * 80,
        "LAB SPEC v0.1 Release Evidence Inventory & Gate Report",
        "=" * 80,
        f"Verdict: {inv.verdict}",
        f"Summary: {inv.summary}",
        "-" * 80,
        "Status Breakdown:",
    ]
    for status, count in sorted(inv.status_counts.items()):
        pct = (count / inv.total_mandatory * 100) if inv.total_mandatory else 0
        lines.append(f"  {status:12s}: {count:3d} ({pct:5.1f}%)")

    lines.extend([
        "-" * 80,
        "Real Gate Status (Mock Strictly Prohibited):",
    ])
    for gate_key, counts in sorted(inv.gate_counts.items()):
        gate_info = REAL_GATES.get(gate_key, {})
        name = gate_info.get("name", gate_key)
        passed = counts.get("passed", 0)
        total = counts.get("total", 0)
        status_str = "PASSED" if passed == total else f"BLOCKED ({total - passed} remaining)"
        lines.append(f"  {name:38s}: {passed}/{total} [{status_str}]")

    if inv.blocked_items:
        lines.extend([
            "-" * 80,
            f"Blocked Items ({len(inv.blocked_items)}):",
        ])
        for b in inv.blocked_items:
            lines.append(f"  - {b['id']} (owners: {b.get('owners')}): {b.get('title')}")
            if b.get("note"):
                lines.append(f"    Note: {b.get('note')}")

    if inv.skipped_items:
        lines.extend([
            "-" * 80,
            f"Skipped Items ({len(inv.skipped_items)}):",
        ])
        for s in inv.skipped_items:
            lines.append(f"  - {s['id']}: {s.get('title')}")
            if s.get("note"):
                lines.append(f"    Note: {s.get('note')}")

    if inv.gate_violations:
        lines.extend([
            "-" * 80,
            f"Gate Violations ({len(inv.gate_violations)}):",
        ])
        for gv in inv.gate_violations:
            lines.append(f"  ! {gv}")

    lines.extend([
        "=" * 80,
    ])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--traceability", type=Path, default=TRACE_PATH, help="Path to traceability.yaml")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH, help="Path to SPEC markdown")
    parser.add_argument("--evidence", type=Path, default=None, help="Path to candidate release evidence file (optional)")
    parser.add_argument("--inventory", action="store_true", help="Print full inventory report")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--release", action="store_true", help="Enforce full release pass (fail-closed if any unpassed)")
    parser.add_argument("--check", action="store_true", help="Check document validity and report status")
    parser.add_argument("--verify-provenance", action="store_true", help="Verify main commit & CI runs on GitHub")
    parser.add_argument("--no-strict-gates", action="store_true", help="Disable strict mock gate prohibition")

    args = parser.parse_args(argv)

    doc_path = args.evidence if args.evidence else args.traceability
    if not doc_path.is_file():
        print(f"check_release_evidence: file not found: {doc_path}", file=sys.stderr)
        return 1

    try:
        doc = yaml.safe_load(doc_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"check_release_evidence: failed to load YAML from {doc_path}: {exc}", file=sys.stderr)
        return 1

    spec_text = args.spec.read_text(encoding="utf-8") if args.spec.is_file() else None

    verify = ct.github_verifier() if args.verify_provenance or args.release else None
    if args.release and args.no_strict_gates:
        print("check_release_evidence: --release cannot disable strict gates", file=sys.stderr)
        return 1
    strict_gates = not args.no_strict_gates

    errors, inventory = check_release_evidence(
        doc=doc,
        spec_text=spec_text,
        verify=verify,
        strict_gates=strict_gates,
        require_release_pass=args.release,
    )

    if args.inventory:
        report = format_inventory_report(inventory, fmt=args.format)
        print(report)
    elif not errors:
        print(f"release_evidence ok: {inventory.summary}")

    if errors:
        print("\ncheck_release_evidence errors:", file=sys.stderr)
        for err in errors:
            print(f"  ERROR: {err}", file=sys.stderr)
        return 1

    if args.release and inventory.verdict != "RELEASE_PASS":
        print(f"check_release_evidence: release criteria not met (verdict: {inventory.verdict})", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
