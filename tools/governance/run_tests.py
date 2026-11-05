"""Run a unittest suite and fail if zero tests ran (no empty-green).

Usage: run_tests.py <start_dir> [--fail-on-skip]
  --fail-on-skip  treat any skipped test as a failure (for suites whose
                  prerequisites, e.g. a real database, must be present).
"""
import sys
import unittest


def main(argv):
    args = argv[1:]
    fail_on_skip = "--fail-on-skip" in args
    args = [a for a in args if a != "--fail-on-skip"]
    if len(args) != 1:
        print("usage: run_tests.py <start_dir> [--fail-on-skip]", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(args[0], top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
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
