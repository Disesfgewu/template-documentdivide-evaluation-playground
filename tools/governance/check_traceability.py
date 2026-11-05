"""Check docs/traceability.yaml against the SPEC's R/T/A tables.

Rules: every SPEC ID appears exactly once and nothing else does; status is
one of not_started | partial | blocked | passed; anything but not_started
needs evidence and a note; passed additionally needs main-branch evidence
with a commit SHA and CI run; owners are Issue numbers 2-14, and an empty
owner list must be explicitly marked unassigned.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "document-seperater-playground_SPEC_v0.1.md"
TRACE = ROOT / "docs" / "traceability.yaml"
STATUSES = {"not_started", "partial", "blocked", "passed"}
SECTIONS = {"requirements": "R", "tests": "T", "acceptance": "A"}


def spec_ids(text: str) -> dict[str, set[str]]:
    out = {p: set() for p in SECTIONS.values()}
    for line in text.splitlines():
        m = re.match(r"^\| ([RTA])(\d{2,3})(?: [A-Z/]+)? \|", line)
        if m:
            out[m.group(1)].add(m.group(1) + m.group(2))
    return out


def check(doc, ids: dict[str, set[str]]) -> list[str]:
    errors = []
    if not isinstance(doc, dict):
        return ["traceability file is not a mapping"]
    for section, prefix in SECTIONS.items():
        entries = doc.get(section)
        if not isinstance(entries, list):
            errors.append(f"{section}: missing list")
            continue
        seen = set()
        for e in entries:
            i = e.get("id") if isinstance(e, dict) else None
            where = f"{section}[{i}]"
            if not isinstance(i, str):
                errors.append(f"{section}: entry without id")
                continue
            if i in seen:
                errors.append(f"{where}: duplicate")
            seen.add(i)
            status = e.get("status")
            if status not in STATUSES:
                errors.append(f"{where}: bad status {status!r}")
            owners = e.get("owners")
            if not isinstance(owners, list) or any(
                    isinstance(o, bool) or not isinstance(o, int) or not 2 <= o <= 14 for o in owners):
                errors.append(f"{where}: owners must be Issue numbers 2-14")
            elif not owners and "unassigned" not in (e.get("note") or ""):
                errors.append(f"{where}: empty owners must be marked unassigned in note")
            evidence = e.get("evidence")
            if not isinstance(evidence, list):
                errors.append(f"{where}: evidence must be a list")
                continue
            if status in {"partial", "blocked", "passed"}:
                if not evidence:
                    errors.append(f"{where}: {status} requires evidence")
                if not e.get("note"):
                    errors.append(f"{where}: {status} requires a note naming what is (not) covered")
            if status == "passed":
                ok = [ev for ev in evidence if isinstance(ev, dict) and ev.get("branch") == "main"
                      and re.fullmatch(r"[0-9a-f]{40}", str(ev.get("commit", "")))
                      and str(ev.get("run", "")).startswith("https://github.com/")]
                if not ok:
                    errors.append(f"{where}: passed requires main evidence with commit SHA and CI run")
        missing = sorted(ids[prefix] - seen)
        extra = sorted(seen - ids[prefix])
        if missing:
            errors.append(f"{section}: missing SPEC ids {missing}")
        if extra:
            errors.append(f"{section}: ids not in SPEC {extra}")
    return errors


def main() -> int:
    errors = check(yaml.safe_load(TRACE.read_text(encoding="utf-8")), spec_ids(SPEC.read_text(encoding="utf-8")))
    for err in errors:
        print(f"traceability: {err}", file=sys.stderr)
    counts = {}
    if not errors:
        doc = yaml.safe_load(TRACE.read_text(encoding="utf-8"))
        for section in SECTIONS:
            for e in doc[section]:
                counts[e["status"]] = counts.get(e["status"], 0) + 1
        print(f"traceability ok: {counts}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
