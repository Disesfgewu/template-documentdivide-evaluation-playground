"""scripts/demo.sh key handling (#174): a .env saved with Windows line endings must not become an invalid key, and a bad key is
refused with a message that never shows it. Only the pure functions run here (the script is sourced; no docker)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo.sh"
KEY = "sk-ant-test-0123456789"


def bash(code: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'source "{SCRIPT}"; {code}'], capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})})


@unittest.skipUnless(subprocess.run(["bash", "--version"], capture_output=True).returncode == 0, "bash is required")
class DemoKeyTests(unittest.TestCase):
    def env_file(self, content: bytes) -> str:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".env")
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_a_crlf_env_file_gives_the_clean_key(self):
        for raw in (f"ANTHROPIC_API_KEY={KEY}\r\n", f'ANTHROPIC_API_KEY="{KEY}"\r\n', f"OTHER=1\r\nANTHROPIC_API_KEY={KEY}  \r\n",
                    f"ANTHROPIC_API_KEY={KEY}\n"):
            path = self.env_file(raw.encode())
            out = bash(f'k="$(env_file_key "{path}")"; printf "%s" "$k"; key_is_valid "$k"')
            self.assertEqual((out.returncode, out.stdout), (0, KEY), repr(raw))

    def test_a_missing_key_line_is_empty_not_an_error(self):
        out = bash(f'k="$(env_file_key "{self.env_file(b"OTHER=1\\r\\n")}")"; printf "[%s]" "$k"')
        self.assertEqual((out.returncode, out.stdout), (0, "[]"))

    def test_keys_with_blanks_control_or_non_ascii_characters_are_invalid(self):
        for bad in ("ab cd", "ab\tcd", "ab\x01cd", "ab\rcd", "é", ""):
            out = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; key_is_valid "$K"'], capture_output=True, env={**os.environ, "K": bad})
            self.assertNotEqual(out.returncode, 0, repr(bad))

    def test_generate_up_refuses_a_bad_key_without_showing_it(self):
        out = bash("generate_up || echo code=$?", env={"LAB_GEN_API_KEY": "sk-secret part\x01two"})
        self.assertIn("BLOCKED", out.stderr)
        self.assertIn("code=4", out.stdout)
        self.assertNotIn("secret", out.stdout + out.stderr)

    def run_generate(self, env):
        # docker is replaced by echo: what the script WOULD pass to the demo container is what we check
        code = 'compose=(echo COMPOSE); up() { :; }; generate_up() { return 0; }; generate'
        return bash(code, env=env)

    def test_the_opt_in_exclusion_is_forwarded_to_the_demo_only_when_asked_for(self):
        on = self.run_generate({"DEMO_EXCLUDE_FAILED": "1"})
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertIn("-e DEMO_EXCLUDE_FAILED=1", on.stdout)
        self.assertIn("-e DEMO_GENERATE=1", on.stdout)
        for value in ("", "0", "yes"):                         # only the exact opt-in counts
            off = self.run_generate({"DEMO_EXCLUDE_FAILED": value})
            self.assertEqual(off.returncode, 0, off.stderr)
            self.assertNotIn("DEMO_EXCLUDE_FAILED", off.stdout, value)

    def test_the_repo_head_helper_finds_the_newest_migration(self):
        head = bash("repo_head").stdout.strip()
        versions = sorted(p.name[:4] for p in (SCRIPT.parents[1] / "db" / "lab_db" / "schema" / "versions").glob("[0-9]*.py"))
        self.assertEqual(head, versions[-1])


if __name__ == "__main__":
    unittest.main()


STATUS = ("PROJECTS=1\nDOCUMENTS=2\nBENCHMARKS=3\nFROZEN_BENCHMARKS=1\nQUESTIONS=40\nRUNS=4\nDEVELOPER_UPLOADS=2\nARTIFACTS=9\n"
          "DANGLING_ARTIFACTS={dangling}\nMIGRATION=0023\n")
EMPTY = STATUS.replace("DOCUMENTS=2", "DOCUMENTS=0").replace("BENCHMARKS=3", "BENCHMARKS=0")
# docker is replaced by a function: `exec` (the data status script) prints $FAKE_STATUS, everything else is echoed
FAKE = ('fake() { if [[ " $* " == *" exec "* ]]; then cat >/dev/null; printf "%s" "$FAKE_STATUS"; else echo "COMPOSE $*"; fi; }; '
        'compose=(fake); up() { echo UP; }; run() { echo RUN; }; generate_up() { return 0; }; ')


@unittest.skipUnless(subprocess.run(["bash", "--version"], capture_output=True).returncode == 0, "bash is required")
class DemoDataSafetyTests(unittest.TestCase):
    """Issue #194: stop keeps the data, down needs the wipe flag and says what it destroys, all never wipes existing data."""

    def run_fn(self, code, status=STATUS.format(dangling=0), env=None):
        return bash(FAKE + code, env={"FAKE_STATUS": status, **(env or {})})

    def test_stop_never_removes_volumes(self):
        out = self.run_fn("stop")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("COMPOSE --profile demo --profile tools --profile generate down", out.stdout)
        self.assertNotIn(" -v", out.stdout)
        self.assertIn("KEPT", out.stdout)

    def test_down_without_the_flag_refuses_deletes_nothing_and_says_what_would_be_lost(self):
        out = self.run_fn("down && echo code=0 || echo code=$?")
        self.assertIn("code=5", out.stdout)
        self.assertNotIn("down -v", out.stdout)
        self.assertIn("REFUSED", out.stderr)
        self.assertIn("documents: 2, benchmarks: 3", out.stderr)
        self.assertIn("developer uploads: 2", out.stderr)
        self.assertIn("scripts/demo.sh stop", out.stderr)

    def test_down_refuses_even_when_the_stack_is_not_running_and_still_names_the_risk(self):
        out = self.run_fn("down && echo code=0 || echo code=$?", status="")
        self.assertIn("code=5", out.stdout)
        self.assertNotIn("down -v", out.stdout)
        self.assertIn("would be deleted", out.stderr)

    def test_only_the_exact_flag_or_the_exact_opt_in_wipes(self):
        for call, env in (("down --wipe", {}), ("down", {"DEMO_WIPE": "1"})):
            out = self.run_fn(f"{call} && echo code=0 || echo code=$?", env=env)
            self.assertIn("code=0", out.stdout, call)
            self.assertIn("down -v", out.stdout, call)
            self.assertIn("documents: 2", out.stdout, call)          # the counts are printed before the wipe
        for call, env in (("down --wipe-all", {}), ("down wipe", {}), ("down", {"DEMO_WIPE": "yes"}), ("down", {"DEMO_WIPE": "0"})):
            out = self.run_fn(f"{call} && echo code=0 || echo code=$?", env=env)
            self.assertIn("code=5", out.stdout, (call, env))
            self.assertNotIn("down -v", out.stdout, (call, env))

    def test_all_refuses_to_run_over_existing_data_and_runs_on_an_empty_stack(self):
        refused = self.run_fn("all && echo code=0 || echo code=$?")
        self.assertIn("code=5", refused.stdout)
        self.assertNotIn("UP", refused.stdout)
        self.assertNotIn("down -v", refused.stdout)
        ok = self.run_fn("all && echo code=0 || echo code=$?", status=EMPTY.format(dangling=0))
        self.assertIn("UP", ok.stdout)
        self.assertIn("RUN", ok.stdout)
        self.assertIn("down -v", ok.stdout)           # the demo cleans up after itself, but only because the stack was empty
        forced = self.run_fn("all", env={"DEMO_WIPE": "1"})
        self.assertIn("UP", forced.stdout)

    def test_the_data_check_reports_the_counts_and_fails_loudly_on_missing_files(self):
        good = self.run_fn("data_check && echo code=0 || echo code=$?")
        self.assertIn("code=0", good.stdout)
        self.assertIn("the database and the artifact volume agree", good.stdout)
        bad = self.run_fn("data_check && echo code=0 || echo code=$?", status=STATUS.format(dangling=3))
        self.assertIn("code=6", bad.stdout)
        self.assertIn("WARNING: 3 stored file(s)", bad.stderr)

    def test_start_runs_on_the_existing_data_and_does_not_wipe(self):
        out = self.run_fn("start && echo code=0 || echo code=$?")
        self.assertIn("UP", out.stdout)
        self.assertIn("code=0", out.stdout)
        self.assertNotIn("down", out.stdout.replace("scripts/demo.sh", ""))
        bad = self.run_fn("start && echo code=0 || echo code=$?", status=STATUS.format(dangling=1))
        self.assertIn("code=6", bad.stdout)

    def test_status_when_stopped_says_the_data_is_kept(self):
        out = self.run_fn("status", status="")
        self.assertIn("kept", out.stdout)
