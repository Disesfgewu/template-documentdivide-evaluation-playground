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
        """When an assertion fails, report is written in finally with accurate status and no credentials."""
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            measured = {
                "smoke_id": "test1234",
                "commit_sha": "abc1234",
                "base_image_digest": runtime_smoke.BASE_IMAGE_DIGEST,
                "python_version": "3.12.0",
                "timestamp_start": "2026-11-09T00:00:00Z",
                "timestamp_end": "2026-11-09T00:00:05Z",
                "duration_s": 5.0,
                "status": "FAILED",
                "error_step": "pdf_upload_succeeded",
                "error_class": "AssertionError",
                "completed_assertions": ["database_migrated", "images_built", "containers_started"],
                "measured_data": {"migrations_applied_count": 9},
                "cleanup_status": {"succeeded": True, "failures": []},
            }

            json_path = report_dir / "runtime_smoke_report.json"
            md_path = report_dir / "runtime_smoke_report.md"

            json_path.write_text(json.dumps(measured, indent=2), encoding="utf-8")
            md_lines = [
                "# Runtime Container Smoke Report",
                f"- **Status**: {measured['status']}",
                f"- **Duration**: {measured['duration_s']}s",
                "## Completed Assertions",
            ]
            for a in measured["completed_assertions"]:
                md_lines.append(f"- [x] {a}")
            md_lines.append(f"\n> [!CAUTION]\n> Smoke failed at step `{measured['error_step']}` with `{measured['error_class']}`.")
            md_path.write_text("\n".join(md_lines), encoding="utf-8")

            # Verify report readability and contents
            self.assertTrue(json_path.is_file())
            self.assertTrue(md_path.is_file())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "FAILED")
            self.assertEqual(data["error_class"], "AssertionError")
            self.assertEqual(len(data["completed_assertions"]), 3)

            md_text = md_path.read_text(encoding="utf-8")
            self.assertIn("Status**: FAILED", md_text)
            self.assertIn("database_migrated", md_text)
            self.assertIn("pdf_upload_succeeded", md_text)
            # Ensure no credentials or URLs leaked
            self.assertNotIn("postgresql://", md_text)
            self.assertNotIn("Bearer", md_text)

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
