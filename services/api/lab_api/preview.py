"""Supervised bounded subprocess runner for PDF page preview rendering (SPEC 8, 19)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import resource
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

class PreviewError(Exception):
    """Normalized preview rendering error mapped to API response contract."""
    def __init__(self, status_code: int, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable

_REPO_ROOT = Path(__file__).resolve().parents[3]

MAX_PIXELS = 16_000_000
MAX_SIDE_PX = 4096
DEFAULT_PREVIEW_TIMEOUT_S = 10.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclasses.dataclass(frozen=True)
class PreviewAsset:
    """Immutable rendered page preview asset."""
    png: bytes
    sha256: str
    width_px: int
    height_px: int
    page_index: int


def _set_preview_rlimits() -> None:
    """Apply POSIX resource ceilings to child renderer process."""
    try:
        # CPU time: 10s soft, 15s hard
        resource.setrlimit(resource.RLIMIT_CPU, (10, 15))
    except (ValueError, OSError):
        pass
    try:
        # Maximum file size: 50MB
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
    except (ValueError, OSError):
        pass
    try:
        # Address space: 1.5GB
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    except (ValueError, OSError):
        pass


def render_preview_in_subprocess(
    pdf_bytes: bytes,
    page_index: int,
    timeout_s: float = DEFAULT_PREVIEW_TIMEOUT_S,
) -> PreviewAsset:
    """Render a PDF page in a supervised, isolated subprocess under POSIX resource bounds."""
    if not isinstance(pdf_bytes, (bytes, bytearray, memoryview)) or len(pdf_bytes) == 0:
        raise PreviewError(500, "storage_corruption", "invalid PDF bytes for preview rendering")
    if page_index < 0:
        raise PreviewError(404, "not_found", f"page_index {page_index} out of range")

    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, "input.pdf")
        output_path = os.path.join(tmp_dir, "output.png")
        manifest_path = os.path.join(tmp_dir, "manifest.json")

        with open(input_path, "wb") as f:
            f.write(pdf_bytes)
        os.chmod(input_path, 0o600)

        cmd = [
            sys.executable,
            "-m",
            "lab_parser.preview_cli",
            "--input",
            input_path,
            "--output",
            output_path,
            "--manifest",
            manifest_path,
            "--page",
            str(page_index),
            "--dpi",
            "150",
        ]

        env = dict(os.environ)
        parser_path = str(_REPO_ROOT / "services" / "parser")
        if "PYTHONPATH" in env and env["PYTHONPATH"]:
            env["PYTHONPATH"] = f"{parser_path}:{env['PYTHONPATH']}"
        else:
            env["PYTHONPATH"] = parser_path

        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
                preexec_fn=_set_preview_rlimits,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PreviewError(
                504,
                "gateway_timeout",
                "page rendering timed out",
                retryable=True,
            ) from exc

        if proc.returncode != 0:
            if proc.returncode == 11:
                raise PreviewError(
                    422,
                    "pixel_limit_exceeded",
                    "page preview exceeds maximum dimension limits (16MP or 4096px)",
                )
            if proc.returncode == 12:
                raise PreviewError(404, "not_found", f"page_index {page_index} out of range")
            raise PreviewError(
                503,
                "service_unavailable",
                "page preview renderer temporarily unavailable",
                retryable=True,
            )

        if not os.path.exists(manifest_path) or not os.path.exists(output_path):
            raise PreviewError(
                503,
                "service_unavailable",
                "page preview renderer failed to produce output",
                retryable=True,
            )

        # Parent validates manifest JSON
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as exc:
            raise PreviewError(
                500,
                "storage_corruption",
                "invalid preview manifest generated",
            ) from exc

        width_px = manifest.get("width_px")
        height_px = manifest.get("height_px")
        manifest_sha256 = manifest.get("sha256")

        if (
            not isinstance(width_px, int)
            or not isinstance(height_px, int)
            or width_px <= 0
            or height_px <= 0
            or width_px > MAX_SIDE_PX
            or height_px > MAX_SIDE_PX
            or (width_px * height_px) > MAX_PIXELS
        ):
            raise PreviewError(
                422,
                "pixel_limit_exceeded",
                "rendered preview exceeds allowed dimensions",
            )

        if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
            raise PreviewError(
                500,
                "storage_corruption",
                "invalid preview manifest checksum",
            )

        # Parent validates output PNG bytes
        with open(output_path, "rb") as f:
            png_bytes = f.read(MAX_PIXELS * 4 + 4096)

        if not png_bytes.startswith(PNG_SIGNATURE):
            raise PreviewError(
                500,
                "storage_corruption",
                "preview output is not a valid PNG",
            )

        actual_sha256 = hashlib.sha256(png_bytes).hexdigest()
        if actual_sha256 != manifest_sha256:
            raise PreviewError(
                500,
                "storage_corruption",
                "preview output checksum mismatch",
            )

        if len(png_bytes) < 24:
            raise PreviewError(
                500,
                "storage_corruption",
                "truncated preview PNG header",
            )

        # Validate IHDR chunk width and height
        png_w, png_h = struct.unpack(">II", png_bytes[16:24])
        if png_w != width_px or png_h != height_px:
            raise PreviewError(
                500,
                "storage_corruption",
                "preview PNG dimensions do not match manifest",
            )

        return PreviewAsset(
            png=png_bytes,
            sha256=actual_sha256,
            width_px=width_px,
            height_px=height_px,
            page_index=page_index,
        )
