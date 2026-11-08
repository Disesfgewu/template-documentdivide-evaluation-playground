"""Convert ``go test -json`` output into a lab-test-report-1 report.

Usage: go_test_report.py --module <dir> --input <go-test-json-file> [--tags <t1,t2>]

Prints the test output (as ``go test -v`` would) and, if LAB_TEST_REPORT_DIR
is set, writes ``<module dir with / as _>_go.json`` there. Keys use the
traceability reference format ``<dir>/<file>_test.go::TestName``. The file is
found by scanning only the test files ``go list`` reports as active for the
package (TestGoFiles + XTestGoFiles) under the same build configuration: the
inherited environment (GOOS, GOARCH, GOFLAGS, CGO_ENABLED) plus ``--tags``,
which must match the tags given to ``go test``. Files excluded by build
constraints are never credited, and a name defined in more than one active
file is an error, never a guess (PM-REVIEW on #32 at b08a0d7).
Only top-level tests are recorded (a failing subtest fails its parent in Go).
A test that never ran (build failure, filtered out) is simply absent, which
the traceability verifier treats as not executed. Exit status: 0, or 2 for
unusable input. The go test exit status stays the caller's verdict.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tools.governance.run_tests import REPORT_FORMAT

GO_TEST_FUNC = re.compile(r"^func (Test[A-Za-z0-9_]*)\(\w+ \*testing\.T\)", re.M)
OUTCOMES = {"pass": "passed", "fail": "failed", "skip": "skipped"}


def module_path(module_dir: Path) -> str:
    m = re.search(r"^module\s+(\S+)", (module_dir / "go.mod").read_text(encoding="utf-8"), re.M)
    if not m:
        raise ValueError(f"{module_dir}/go.mod has no module line")
    return m.group(1)


def active_test_files(pkg_dir: Path, tags: str = "") -> list[str]:
    """Test file names go would compile for this package under the current
    build configuration. Any go list failure is an error (fail closed)."""
    go = shutil.which("go")
    if go is None:
        raise ValueError("go is required to resolve the active test files")
    cmd = [go, "list"] + ([f"-tags={tags}"] if tags else []) + [
        "-f", "{{range .TestGoFiles}}{{.}}\n{{end}}{{range .XTestGoFiles}}{{.}}\n{{end}}", "."]
    try:
        run = subprocess.run(cmd, cwd=pkg_dir, capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, GOTOOLCHAIN=os.environ.get("GOTOOLCHAIN", "local")))
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"go list timed out in {pkg_dir}") from exc
    if run.returncode != 0:
        raise ValueError(f"go list failed in {pkg_dir}: {run.stderr.strip()}")
    return [line for line in run.stdout.splitlines() if line]


def test_files(pkg_dir: Path, active: list[str]) -> dict[str, str]:
    """TestName -> file name over the active test files of one package."""
    out: dict[str, str] = {}
    for name_ in sorted(active):
        f = pkg_dir / name_
        for name in GO_TEST_FUNC.findall(f.read_text(encoding="utf-8")):
            if name in out:
                raise ValueError(f"{name} is defined in both {out[name]} and {f.name} in {pkg_dir}; "
                                 "attribution is ambiguous")
            out[name] = f.name
    return out


def convert(lines, module_dir: str, tags: str = "", active_files=active_test_files) -> tuple[dict[str, str], list[str]]:
    """Return (outcomes, output lines) from go test -json event lines."""
    mod = Path(module_dir)
    prefix = module_path(mod)
    outcomes, output, files = {}, [], {}
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            output.append(raw)  # build errors etc. are printed, never recorded
            continue
        if ev.get("Action") == "output":
            output.append(ev.get("Output", "").rstrip("\n"))
            continue
        test, action, pkg = ev.get("Test"), ev.get("Action"), ev.get("Package", "")
        if not test or "/" in test or action not in OUTCOMES:
            continue
        if pkg != prefix and not pkg.startswith(prefix + "/"):
            raise ValueError(f"package {pkg} is outside module {prefix}")
        rel = pkg[len(prefix):].lstrip("/")
        pkg_dir = mod / rel if rel else mod
        if pkg_dir not in files:
            files[pkg_dir] = test_files(pkg_dir, active_files(pkg_dir, tags))
        name = files[pkg_dir].get(test)
        if name is None:
            raise ValueError(f"{pkg}.{test}: no func {test} in the active test files of {pkg_dir}")
        outcomes[f"{(pkg_dir / name).as_posix()}::{test}"] = OUTCOMES[action]
    return outcomes, output


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        module_dir = argv[argv.index("--module") + 1]
        source = argv[argv.index("--input") + 1]
        tags = argv[argv.index("--tags") + 1] if "--tags" in argv else ""
    except (ValueError, IndexError):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    try:
        with open(source, encoding="utf-8") as f:
            outcomes, output = convert(f, module_dir, tags)
    except (OSError, ValueError) as exc:
        print(f"go_test_report: {exc}", file=sys.stderr)
        return 2
    for line in output:
        print(line)
    report_dir = os.environ.get("LAB_TEST_REPORT_DIR")
    if report_dir:
        path = Path(report_dir) / (module_dir.strip("/").replace("/", "_") + "_go.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": REPORT_FORMAT, "start_dir": module_dir, "runner": "go test -json",
                                    "tests": dict(sorted(outcomes.items()))}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
