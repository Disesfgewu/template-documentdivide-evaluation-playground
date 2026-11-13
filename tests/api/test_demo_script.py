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

    def test_the_repo_head_helper_finds_the_newest_migration(self):
        head = bash("repo_head").stdout.strip()
        versions = sorted(p.name[:4] for p in (SCRIPT.parents[1] / "db" / "lab_db" / "schema" / "versions").glob("[0-9]*.py"))
        self.assertEqual(head, versions[-1])


if __name__ == "__main__":
    unittest.main()
