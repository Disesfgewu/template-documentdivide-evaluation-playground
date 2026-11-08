"""Focused tests for bounded runtime smoke helper functions and report safety (Refs #3)."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from deploy import runtime_smoke


class SmokeHelperTests(unittest.TestCase):
    def test_bounded_http_read_rejects_oversized_payload(self) -> None:
        """Bounded HTTP client must refuse payloads exceeding MAX_HTTP_BYTES without unbounded read."""
        oversized = b"x" * (runtime_smoke.MAX_HTTP_BYTES + 10)
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/octet-stream"}
        mock_resp.status = 200
        mock_resp.read = MagicMock(return_value=oversized)

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value = mock_resp
            with self.assertRaises(ValueError) as ctx:
                runtime_smoke._http_get("http://127.0.0.1:8000/test")
            self.assertIn("exceeded 1MiB limit", str(ctx.exception))

    def test_failure_report_written_with_safe_redacted_fields(self) -> None:
        """Exercise real failure/finally path, not a hand-built expected report."""
        from contextlib import redirect_stderr
        from types import SimpleNamespace
        canary = "synthetic-password-canary"
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with patch.dict("os.environ", {"LAB_TEST_DATABASE_URL": "postgresql://u:" + canary + "@localhost/db"}), \
                 patch.object(runtime_smoke.shutil, "which", return_value="/usr/bin/docker"), \
                 patch.object(runtime_smoke.psycopg, "connect", side_effect=RuntimeError(canary)), \
                 patch.object(runtime_smoke, "_run_docker", return_value=SimpleNamespace(stdout=b"", returncode=0)), \
                 redirect_stderr(stderr):
                code = runtime_smoke.main(["--report-dir", tmp])
            self.assertEqual(code, 1)
            report = Path(tmp) / "runtime_smoke_report.json"
            data = json.loads(report.read_text())
            self.assertEqual(data["status"], "FAILED")
            self.assertEqual(data["completed_assertions"], [])
            self.assertEqual(data["error_class"], "RuntimeError")
            self.assertTrue(data["cleanup_status"]["succeeded"])
            self.assertNotIn(canary, report.read_text() + stderr.getvalue())
            self.assertTrue((Path(tmp) / "runtime_smoke_report.md").is_file())

    def test_docker_timeout_is_bounded_and_value_free(self) -> None:
        with patch.object(runtime_smoke.subprocess, "run", side_effect=runtime_smoke.subprocess.TimeoutExpired(["docker", "secret-canary"], 7)) as run:
            with self.assertRaises(TimeoutError) as error:
                runtime_smoke._run_docker(["run", "secret-canary"], timeout_s=7)
            self.assertEqual(run.call_args.kwargs["timeout"], 7)
            self.assertNotIn("secret-canary", str(error.exception))

    def test_run_docker_error_masks_command_arguments(self) -> None:
        """_run_docker raises RuntimeError without printing raw command arguments or credentials."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = runtime_smoke.subprocess.CalledProcessError(
                returncode=127,
                cmd=["docker", "run", "-e", "SECRET=sensitive-token"],
            )
            with self.assertRaises(RuntimeError) as ctx:
                runtime_smoke._run_docker(["run", "-e", "SECRET=sensitive-token"])
            self.assertNotIn("sensitive-token", str(ctx.exception))
            self.assertEqual(str(ctx.exception), "docker command failed with exit code 127")


if __name__ == "__main__":
    unittest.main()
