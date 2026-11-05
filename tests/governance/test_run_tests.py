import contextlib
import io
import os
import tempfile
import textwrap
import unittest

from tools.governance import run_tests

PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
SKIPPING = PASSING + "    @unittest.skip('prereq missing')\n    def test_skip(self):\n        pass\n"


class RunTestsTests(unittest.TestCase):
    def run_dir(self, files, *flags):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            pkg = os.path.join(d, "suite_" + os.path.basename(d))
            os.mkdir(pkg)
            open(os.path.join(pkg, "__init__.py"), "w").close()
            for name, body in files.items():
                with open(os.path.join(pkg, name), "w") as f:
                    f.write(textwrap.dedent(body))
            os.chdir(d)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    return run_tests.main(["run_tests.py", os.path.basename(pkg), *flags])
            finally:
                os.chdir(cwd)

    def test_empty_suite_fails(self):
        self.assertEqual(self.run_dir({}), 1)

    def test_passing_suite(self):
        self.assertEqual(self.run_dir({"test_a.py": PASSING}), 0)

    def test_skip_allowed_by_default(self):
        self.assertEqual(self.run_dir({"test_a.py": SKIPPING}), 0)

    def test_skip_fails_with_flag(self):
        self.assertEqual(self.run_dir({"test_a.py": SKIPPING}, "--fail-on-skip"), 1)

    def test_bad_usage(self):
        self.assertEqual(run_tests.main(["run_tests.py"]), 2)


if __name__ == "__main__":
    unittest.main()
