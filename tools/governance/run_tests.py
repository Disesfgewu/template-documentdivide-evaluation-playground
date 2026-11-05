"""Run a unittest suite and fail if zero tests ran (no empty-green)."""
import sys
import unittest


def main(argv):
    if len(argv) != 2:
        print("usage: run_tests.py <start_dir>", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(argv[1], top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0:
        print(f"FAIL: no tests discovered in {argv[1]}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
