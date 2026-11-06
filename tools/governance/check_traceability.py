"""Check docs/traceability.yaml against the SPEC's R/T/A tables.

Rules: every SPEC ID appears exactly once and nothing else does; status is
one of not_started | partial | blocked | passed; anything but not_started
needs evidence and a note; passed additionally needs main-branch evidence
with a commit SHA and CI run; owners are Issue numbers 2-14, and an empty
owner list must be explicitly marked unassigned.

``passed`` is gated behind a separate provenance check: the offline check
cannot know whether a run/commit is real, so it rejects ``passed`` unless a
verifier confirms each main evidence item (``--verify-provenance`` asks
GitHub that the run belongs to this repository, concluded success, ran on
main at exactly the recorded commit, and that the commit is on main).
Named tests (``file::Name``) must exist for a supported runner (Python
unittest classes/methods, Go ``func TestX``); other files cannot carry names.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "document-seperater-playground_SPEC_v0.1.md"
TRACE = ROOT / "docs" / "traceability.yaml"
STATUSES = {"not_started", "partial", "blocked", "passed"}
SECTIONS = {"requirements": "R", "tests": "T", "acceptance": "A"}


REPO = "Disesfgewu/template-documentdivide-evaluation-playground"
RUN_URL = re.compile(r"https://github\.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/([0-9]+)")


def _test_names(path: Path) -> set[str]:
    """Class and Class.method names defined in a Python test file."""
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
            names.update(f"{node.name}.{f.name}" for f in node.body
                         if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return names


def _go_test_names(path: Path) -> set[str]:
    return set(re.findall(r"^func (Test[A-Za-z0-9_]*)\(\w+ \*testing\.T\)", path.read_text(encoding="utf-8"), re.M))


NAMED_TEST_RUNNERS = {".py": _test_names, "_test.go": _go_test_names}


def _named_test_problem(path: str, t: str) -> str | None:
    name = t.split("::", 1)[1].split(" ", 1)[0]
    for suffix, names in NAMED_TEST_RUNNERS.items():
        if path.endswith(suffix):
            return None if name in names(ROOT / path) else f"test not found: {t}"
    return f"named tests are only supported for Python and Go test files: {t}"


def _evidence_problems(ev) -> list[str]:
    """Each evidence item must be a mapping naming where it came from and
    pointing at tests or a CI run that exist."""
    if not isinstance(ev, dict):
        return ["must be a mapping"]
    out = []
    if not isinstance(ev.get("branch"), str) or not ev["branch"]:
        out.append("branch required")
    tests, run = ev.get("tests"), ev.get("run")
    if not tests and not run:
        out.append("needs tests or a CI run")
    if run is not None and not RUN_URL.fullmatch(str(run)):
        out.append("run must be a CI run URL of this repository")
    if tests is not None:
        if not isinstance(tests, list) or not all(isinstance(t, str) and t for t in tests):
            out.append("tests must be a list of strings")
        else:
            for t in tests:
                path = t.split("::", 1)[0].split(" ", 1)[0]
                if "*" in path:
                    if not list(ROOT.glob(path)):
                        out.append(f"no file matches {path}")
                elif not (ROOT / path).is_file():
                    out.append(f"test file does not exist: {path}")
                elif "::" in t:
                    # A named test must exist: stale evidence (renamed or
                    # removed test) is not evidence.
                    problem = _named_test_problem(path, t)
                    if problem:
                        out.append(problem)
    return out


def spec_ids(text: str) -> dict[str, set[str]]:
    out = {p: set() for p in SECTIONS.values()}
    for line in text.splitlines():
        m = re.match(r"^\| ([RTA])(\d{2,3})(?: [A-Z/]+)? \|", line)
        if m:
            out[m.group(1)].add(m.group(1) + m.group(2))
    return out


def github_verifier(fetch=None):
    """Verifier asking GitHub whether a main evidence item really happened.
    ``fetch(path) -> dict`` defaults to ``gh api``; failures are problems."""
    if fetch is None:
        def fetch(path):
            out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip() or f"gh api {path} failed")
            return json.loads(out.stdout)

    def verify(ev) -> list[str]:
        m = RUN_URL.fullmatch(str(ev.get("run", "")))
        commit = str(ev.get("commit", ""))
        if not m:
            return ["provenance: no run URL"]
        try:
            run = fetch(f"repos/{REPO}/actions/runs/{m.group(1)}")
            problems = []
            if (run.get("repository") or {}).get("full_name") != REPO \
                    or (run.get("head_repository") or {}).get("full_name") != REPO:
                problems.append("provenance: run is not from this repository")
            if run.get("head_branch") != "main":
                problems.append(f"provenance: run branch is {run.get('head_branch')!r}, not main")
            if run.get("head_sha") != commit:
                problems.append("provenance: run head_sha does not match commit")
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                problems.append(f"provenance: run did not succeed ({run.get('status')}/{run.get('conclusion')})")
            cmp = fetch(f"repos/{REPO}/compare/{commit}...main")
            if cmp.get("status") not in {"ahead", "identical"}:
                problems.append("provenance: commit is not on main")
            return problems
        except Exception as exc:  # unreachable run/commit is not evidence
            return [f"provenance: could not verify ({exc})"]

    return verify


def check(doc, ids: dict[str, set[str]], verify=None) -> list[str]:
    """``verify(evidence) -> problems`` confirms provenance of passed evidence;
    without it ``passed`` is rejected (offline evidence is never enough)."""
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
            elif "primary" in e and e["primary"] not in owners:
                errors.append(f"{where}: primary must be one of owners")
            if not isinstance(e.get("title"), str) or not e["title"].strip():
                errors.append(f"{where}: title required")
            if e.get("release_owner") != 15:
                errors.append(f"{where}: release_owner must be 15")
            evidence = e.get("evidence")
            if not isinstance(evidence, list):
                errors.append(f"{where}: evidence must be a list")
                continue
            if status in {"partial", "blocked", "passed"}:
                if not evidence:
                    errors.append(f"{where}: {status} requires evidence")
                if not e.get("note"):
                    errors.append(f"{where}: {status} requires a note naming what is (not) covered")
            for k, ev in enumerate(evidence):
                errors += [f"{where}.evidence[{k}]: {msg}" for msg in _evidence_problems(ev)]
            if status == "passed":
                ok = [ev for ev in evidence if isinstance(ev, dict) and ev.get("branch") == "main"
                      and re.fullmatch(r"[0-9a-f]{40}", str(ev.get("commit", "")))
                      and len(set(str(ev.get("commit")))) > 1
                      and RUN_URL.fullmatch(str(ev.get("run", ""))) and ev.get("tests")]
                if not ok:
                    errors.append(f"{where}: passed requires main evidence with commit SHA, this repo's CI run and tests")
                elif verify is None:
                    errors.append(f"{where}: passed requires verified provenance (run with --verify-provenance)")
                else:
                    for ev in ok:
                        errors += [f"{where}: {msg}" for msg in verify(ev)]
        missing = sorted(ids[prefix] - seen)
        extra = sorted(seen - ids[prefix])
        if missing:
            errors.append(f"{section}: missing SPEC ids {missing}")
        if extra:
            errors.append(f"{section}: ids not in SPEC {extra}")
    return errors


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verify = github_verifier() if "--verify-provenance" in argv else None
    errors = check(yaml.safe_load(TRACE.read_text(encoding="utf-8")), spec_ids(SPEC.read_text(encoding="utf-8")), verify)
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
