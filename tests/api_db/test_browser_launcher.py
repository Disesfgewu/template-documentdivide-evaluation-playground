"""The browser launcher's retry path, driven with STUB browsers (Issue #145 review of #146, Refs #7).

No real browser and no database: a stub executable plays a browser that exits at once, stays silent, or reports
the DevTools line only on its third start. The tests assert the attempt count, the recorded failures (kept even when a
later attempt succeeds), and that no profile directory is left behind."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required (CI installs it for the web build)")
class BrowserLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="launcher-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def stub(self, body: str) -> str:
        path = self.tmp / "stub-browser.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)

    def drive(self, browser, startup_ms=1500, attempts=3):
        profiles = self.tmp / "profiles"
        profiles.mkdir(exist_ok=True)
        res = subprocess.run([NODE, str(HERE / "browser_launcher_driver.mjs")], capture_output=True, text=True, timeout=60,
                             input=json.dumps({"browserPath": browser, "startupMs": startup_ms, "attempts": attempts,
                                               "tmpDir": str(profiles)}))
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1]), profiles

    def test_a_browser_that_exits_at_once_is_retried_then_reported_with_its_stderr_and_exit_code(self):
        result, profiles = self.drive(self.stub('echo "stub: cannot start" >&2\nexit 7\n'))
        self.assertFalse(result["ok"])
        failed = result["failedAttempts"]
        self.assertEqual(len(failed), 3)
        for item in failed:
            self.assertEqual(item["exit"]["code"], 7)
            self.assertIn("cannot start", item["stderr_tail"])
            self.assertIn("exited before reporting", item["reason"])
        self.assertEqual(list(profiles.iterdir()), [])                      # every profile directory was removed

    def test_a_silent_browser_times_out_per_attempt_and_is_killed(self):
        result, profiles = self.drive(self.stub("sleep 30\n"), startup_ms=700, attempts=2)
        self.assertFalse(result["ok"])
        self.assertEqual([f["reason"] for f in result["failedAttempts"]], ["no DevTools line within 700 ms"] * 2)
        self.assertTrue(all(f["elapsed_ms"] >= 700 for f in result["failedAttempts"]))
        self.assertEqual(list(profiles.iterdir()), [])

    def test_a_retry_that_succeeds_still_reports_the_failed_attempts(self):
        counter = self.tmp / "starts"
        browser = self.stub(f'''n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"
if [ "$n" -lt 3 ]; then echo "stub: start $n failed" >&2; exit 9; fi
echo "DevTools listening on ws://127.0.0.1:43210/devtools/browser/abc" >&2
sleep 30
''')
        result, profiles = self.drive(browser)
        self.assertTrue(result["ok"])
        self.assertEqual((result["port"], result["attempts"]), ("43210", 3))
        self.assertEqual(len(result["failedAttempts"]), 2)                  # NOT discarded because attempt 3 worked
        self.assertIn("start 1 failed", result["failedAttempts"][0]["stderr_tail"])
        self.assertEqual(list(profiles.iterdir()), [])                      # the successful attempt was stopped and cleaned too

    def test_every_launch_reports_its_startup_time(self):
        result, _ = self.drive(self.stub('echo "DevTools listening on ws://127.0.0.1:4321/devtools/browser/q" >&2\nsleep 30\n'))
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["startupMs"], int)
        self.assertGreaterEqual(result["startupMs"], 0)
        self.assertLess(result["startupMs"], 1500)

    def test_a_failed_attempts_log_is_a_bounded_tail_with_paths_scrubbed(self):
        # 30 kB of log, then a failure: only the last 4000 characters are kept, absolute paths other than the temp profile are
        # reduced to <path>, the profile of that attempt is shown as <profile>
        browser = self.stub('i=0; while [ $i -lt 600 ]; do echo "[1:1:INFO:chrome/x.cc] loading /home/runner/.config/secret-dir/file$i.so" >&2; i=$((i+1)); done\n'
                            'for a in "$@"; do case "$a" in --user-data-dir=*) echo "profile ${a#--user-data-dir=}/Default" >&2;; esac; done\n'
                            'exit 5\n')
        result, profiles = self.drive(browser, attempts=1)
        self.assertFalse(result["ok"])
        tail = result["failedAttempts"][0]["stderr_tail"]
        self.assertLessEqual(len(tail), 4100)
        self.assertNotIn("/home/runner", tail)
        self.assertNotIn("secret-dir", tail)
        self.assertIn("<path>", tail)
        self.assertIn("<profile>/Default", tail)
        self.assertNotIn(str(profiles), tail)
        self.assertEqual(list(profiles.iterdir()), [])

    def test_the_browser_is_started_with_its_own_logging_enabled(self):
        counter = self.tmp / "argv"
        browser = self.stub(f'echo "$@" > "{counter}"\necho "DevTools listening on ws://127.0.0.1:4322/devtools/browser/q" >&2\nsleep 30\n')
        result, _ = self.drive(browser)
        self.assertTrue(result["ok"])
        argv = counter.read_text()
        self.assertIn("--enable-logging=stderr", argv)
        self.assertIn("--v=1", argv)

    def test_a_split_devtools_line_is_still_matched_and_the_first_attempt_wins(self):
        browser = self.stub('printf "DevTools listening on ws://127.0." >&2\nsleep 0.3\nprintf "0.1:5555/devtools/browser/x\\n" >&2\nsleep 30\n')
        result, _ = self.drive(browser)
        self.assertTrue(result["ok"])
        self.assertEqual((result["port"], result["attempts"], result["failedAttempts"]), ("5555", 1, []))


if __name__ == "__main__":
    unittest.main()
