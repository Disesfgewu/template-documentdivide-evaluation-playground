"""Run a unittest suite and fail if zero tests ran (no empty-green).

Usage: run_tests.py <start_dir> [--fail-on-skip]
  --fail-on-skip  treat any skipped test as a failure (for suites whose
                  prerequisites, e.g. a real database, must be present).

If LAB_TEST_REPORT_DIR is set, a JSON report of every executed test and its
outcome is written there (``<start_dir with / as _>.json``). CI uploads these
reports as the ``test-reports`` artifact; traceability ``passed`` entries are
verified against them (proof of execution, not only of a green run).
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPORT_FORMAT = "lab-test-report-1"


def test_ref(test) -> str:
    """unittest id ``a.b.test_x.Class.method`` -> ``a/b/test_x.py::Class.method``,
    the reference format used by docs/traceability.yaml."""
    parts = test.id().split(".")
    for i in range(len(parts) - 1, 0, -1):
        if Path(*parts[:i]).with_suffix(".py").is_file():
            return f"{'/'.join(parts[:i])}.py::{'.'.join(parts[i:])}"
    return test.id()


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.outcomes = {}

    def _set(self, test, outcome):
        ref = test_ref(test)
        # A failing subtest makes the whole test failed.
        if self.outcomes.get(ref) not in {"failed", "error"}:
            self.outcomes[ref] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self._set(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "skipped")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "failed")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "failed")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.outcomes[test_ref(test)] = "failed"


def write_report(start_dir: str, outcomes: dict, report_dir: str) -> Path:
    path = Path(report_dir) / (start_dir.strip("/").replace("/", "_") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": REPORT_FORMAT, "start_dir": start_dir,
                                "tests": dict(sorted(outcomes.items()))}, indent=1), encoding="utf-8")
    return path


def main(argv):
    args = argv[1:]
    fail_on_skip = "--fail-on-skip" in args
    args = [a for a in args if a != "--fail-on-skip"]
    if len(args) != 1:
        print("usage: run_tests.py <start_dir> [--fail-on-skip]", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(args[0], top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    if os.environ.get("LAB_TEST_REPORT_DIR"):
        write_report(args[0], result.outcomes, os.environ["LAB_TEST_REPORT_DIR"])
    if result.testsRun == 0:
        print(f"FAIL: no tests discovered in {args[0]}", file=sys.stderr)
        return 1
    if fail_on_skip and result.skipped:
        print(f"FAIL: {len(result.skipped)} skipped test(s) in {args[0]}; "
              "skipped is not passed", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
