"""Go test outcomes reach the shared report, and traceability uses them
(PM-REVIEW on #32 at b1d0e37: Go evidence could never be accepted)."""
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tools.governance import check_traceability as ct
from tools.governance import go_test_report as gr

GO_MOD = "module example.com/labgo\n\ngo 1.21\n"
GO_TEST = '''package labgo

import "testing"

func TestPasses(t *testing.T) {}

func TestSkips(t *testing.T) { t.Skip("prerequisite missing") }

func TestSubFails(t *testing.T) {
	t.Run("ok", func(t *testing.T) {})
	t.Run("bad", func(t *testing.T) { t.Fatal("x") })
}
'''


class GoModule:
    """A throwaway Go module inside the repo root (paths stay repo-relative)."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(dir=ct.ROOT, prefix="gomod_")
        self.rel = Path(self.dir).relative_to(ct.ROOT).as_posix()
        Path(self.dir, "go.mod").write_text(GO_MOD, encoding="utf-8")
        Path(self.dir, "lab_test.go").write_text(GO_TEST, encoding="utf-8")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir)


def events(pkg, *pairs):
    return [json.dumps({"Action": a, "Package": pkg, "Test": t}) for t, a in pairs]


class ConvertTests(unittest.TestCase):
    def test_outcomes_keyed_by_file_and_top_level_test(self):
        with GoModule() as m:
            lines = events("example.com/labgo", ("TestPasses", "pass"), ("TestSkips", "skip"),
                           ("TestSubFails/ok", "pass"), ("TestSubFails/bad", "fail"), ("TestSubFails", "fail"))
            lines += [json.dumps({"Action": "output", "Package": "example.com/labgo", "Output": "=== RUN x\n"}),
                      "# example.com/labgo [build output, not JSON]"]
            outcomes, output = gr.convert(lines, m.rel)
        f = f"{m.rel}/lab_test.go"
        self.assertEqual(outcomes, {f"{f}::TestPasses": "passed", f"{f}::TestSkips": "skipped",
                                    f"{f}::TestSubFails": "failed"})
        self.assertIn("=== RUN x", output)
        self.assertIn("# example.com/labgo [build output, not JSON]", output)

    def test_unmappable_events_are_errors_not_guesses(self):
        with GoModule() as m:
            with self.assertRaisesRegex(ValueError, "outside module"):
                gr.convert(events("other.com/x", ("TestPasses", "pass")), m.rel)
            with self.assertRaisesRegex(ValueError, "no func TestGone"):
                gr.convert(events("example.com/labgo", ("TestGone", "pass")), m.rel)

    def test_real_go_runner_feeds_traceability(self):
        """Real `go test -json` -> report -> verifier: a passed Go test is
        accepted; skipped, failed and never-run Go tests are rejected."""
        if not shutil.which("go"):
            self.fail("go is required for the governance suite (CI installs it); not skipped silently")
        with GoModule() as m, tempfile.TemporaryDirectory() as out:
            env = dict(os.environ, GOTOOLCHAIN="local", GOFLAGS="-mod=mod")
            run = subprocess.run(["go", "test", "-count=1", "-json", "./..."], cwd=m.dir, env=env,
                                 capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 1, run.stderr)  # TestSubFails fails
            src = Path(out, "go.json")
            src.write_text(run.stdout, encoding="utf-8")
            with mock.patch.dict(os.environ, {"LAB_TEST_REPORT_DIR": out}), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(gr.main(["--module", m.rel, "--input", str(src)]), 0)
            report = json.loads(Path(out, m.rel.replace("/", "_") + "_go.json").read_text(encoding="utf-8"))
            f = f"{m.rel}/lab_test.go"
            self.assertEqual(report["format"], "lab-test-report-1")
            self.assertEqual(report["tests"], {f"{f}::TestPasses": "passed", f"{f}::TestSkips": "skipped",
                                               f"{f}::TestSubFails": "failed"})

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("go.json", json.dumps(report))
            outcomes = ct._run_outcomes(
                lambda p: {"artifacts": [{"id": 1, "name": "test-reports", "expired": False}]},
                lambda p: buf.getvalue(), "7")
            self.assertEqual(ct._execution_problems(outcomes, "7", f"{f}::TestPasses"), [])
            for ref, msg in ((f"{f}::TestSkips", "was not passed"), (f"{f}::TestSubFails", "was not passed"),
                             (f"{f}::TestNeverRan", "did not execute"), (f, "was not passed")):
                with self.subTest(ref=ref):
                    got = ct._execution_problems(outcomes, "7", ref)
                    self.assertTrue(got and msg in got[0], got)

    def test_worker_suite_emits_the_go_report(self):
        script = (Path(ct.ROOT) / "scripts" / "check.sh").read_text(encoding="utf-8")
        self.assertIn("go test -count=1 -json ./...", script)
        self.assertIn("python3 -m tools.governance.go_test_report --module services/worker", script)


if __name__ == "__main__":
    unittest.main()
