"""Convert ``go test -json`` output into a lab-test-report-1 report.

Usage: go_test_report.py --module <dir> --input <go-test-json-file>

Prints the test output (as ``go test -v`` would) and, if LAB_TEST_REPORT_DIR
is set, writes ``<module dir with / as _>_go.json`` there. Keys use the
traceability reference format ``<dir>/<file>_test.go::TestName``; the file is
found by scanning the package's ``*_test.go`` files for ``func TestName(``.
Only top-level tests are recorded (a failing subtest fails its parent in Go).
A test that never ran (build failure, filtered out) is simply absent, which
the traceability verifier treats as not executed. Exit status: 0, or 2 for
unusable input. The go test exit status stays the caller's verdict.
"""
from __future__ import annotations

import json
import os
import re
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


def test_files(pkg_dir: Path) -> dict[str, str]:
    """TestName -> file name, for one package directory."""
    out = {}
    for f in sorted(pkg_dir.glob("*_test.go")):
        for name in GO_TEST_FUNC.findall(f.read_text(encoding="utf-8")):
            out[name] = f.name
    return out


def convert(lines, module_dir: str) -> tuple[dict[str, str], list[str]]:
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
            files[pkg_dir] = test_files(pkg_dir)
        name = files[pkg_dir].get(test)
        if name is None:
            raise ValueError(f"{pkg}.{test}: no func {test} in {pkg_dir}/*_test.go")
        outcomes[f"{(pkg_dir / name).as_posix()}::{test}"] = OUTCOMES[action]
    return outcomes, output


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        module_dir = argv[argv.index("--module") + 1]
        source = argv[argv.index("--input") + 1]
    except (ValueError, IndexError):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    try:
        with open(source, encoding="utf-8") as f:
            outcomes, output = convert(f, module_dir)
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
