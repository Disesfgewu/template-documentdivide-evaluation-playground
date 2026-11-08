"""Supervised bounded subprocess runner for PDF page preview rendering (SPEC 8, 19)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

MAX_PIXELS = 16_000_000
MAX_SIDE_PX = 4096
DEFAULT_PREVIEW_TIMEOUT_S = 10.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_MANIFEST_BYTES = 8192
MAX_PNG_BYTES = MAX_PIXELS * 4 + 65536


class PreviewError(Exception):
    """Normalized preview rendering error mapped to API response contract."""
    def __init__(self, status_code: int, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclasses.dataclass(frozen=True)
class PreviewAsset:
    """Immutable rendered page preview asset."""
    png: bytes
    sha256: str
    width_px: int
    height_px: int
    page_index: int


def render_preview_in_subprocess(
    pdf_bytes: bytes,
    page_index: int,
    timeout_s: float = DEFAULT_PREVIEW_TIMEOUT_S,
) -> PreviewAsset:
    """Render a PDF page in a supervised, isolated subprocess without unsafe preexec_fn."""
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

        # Native parser receives no database, signing or provider credentials.
        env = {"PYTHONPATH": str(_REPO_ROOT / "services" / "parser"), "LANG": "C.UTF-8"}

        try:
            # DEVNULL prevents unbounded buffer exhaustion in parent from untrusted output
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
                env=env,
                cwd=tmp_dir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PreviewError(
                504,
                "gateway_timeout",
                "page rendering timed out",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise PreviewError(
                503,
                "service_unavailable",
                f"failed to launch preview renderer process ({type(exc).__name__})",
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

        # Parent validates manifest file presence and absence of symlinks
        if not os.path.exists(manifest_path) or not os.path.exists(output_path):
            raise PreviewError(
                503,
                "service_unavailable",
                "page preview renderer failed to produce output",
                retryable=True,
            )

        # Read manifest safely: regular file, no symlink, bounded bytes
        try:
            m_fd = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            try:
                m_st = os.fstat(m_fd)
                if not stat.S_ISREG(m_st.st_mode) or m_st.st_size > MAX_MANIFEST_BYTES:
                    raise PreviewError(500, "storage_corruption", "invalid preview manifest size")
                with os.fdopen(m_fd, "rb", closefd=False) as mf:
                    manifest_raw = mf.read(MAX_MANIFEST_BYTES + 1)
            finally:
                os.close(m_fd)

            if len(manifest_raw) > MAX_MANIFEST_BYTES:
                raise PreviewError(500, "storage_corruption", "manifest exceeds maximum size")

            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise PreviewError(500, "storage_corruption", "invalid preview manifest generated") from exc

        if not isinstance(manifest, dict):
            raise PreviewError(500, "storage_corruption", "preview manifest must be a JSON object")

        width_px = manifest.get("width_px")
        height_px = manifest.get("height_px")
        manifest_sha256 = manifest.get("sha256")
        manifest_page = manifest.get("page_index")
        manifest_dpi = manifest.get("dpi")

        if type(manifest_page) is not int or type(manifest_dpi) is not int or manifest_page != page_index or manifest_dpi != 150:
            raise PreviewError(500, "storage_corruption", "preview manifest metadata mismatch")

        # Reject booleans or non-integers for dimensions
        if (
            isinstance(width_px, bool)
            or isinstance(height_px, bool)
            or not isinstance(width_px, int)
            or not isinstance(height_px, int)
            or width_px <= 0
            or height_px <= 0
        ):
            raise PreviewError(422, "pixel_limit_exceeded", "rendered preview has invalid dimensions")

        if width_px > MAX_SIDE_PX or height_px > MAX_SIDE_PX or (width_px * height_px) > MAX_PIXELS:
            raise PreviewError(422, "pixel_limit_exceeded", "rendered preview exceeds maximum dimension limits")

        # Validate SHA-256 strictly: 64 lowercase hex characters
        if (
            not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
            or not all(c in "0123456789abcdef" for c in manifest_sha256)
        ):
            raise PreviewError(500, "storage_corruption", "invalid preview manifest checksum")

        # Read output PNG safely: regular file, no symlink, bounded bytes
        try:
            p_fd = os.open(output_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            try:
                p_st = os.fstat(p_fd)
                if not stat.S_ISREG(p_st.st_mode) or p_st.st_size > MAX_PNG_BYTES:
                    raise PreviewError(500, "storage_corruption", "rendered preview output size out of bounds")
                with os.fdopen(p_fd, "rb", closefd=False) as pf:
                    png_bytes = pf.read(MAX_PNG_BYTES + 1)
            finally:
                os.close(p_fd)

            if len(png_bytes) > MAX_PNG_BYTES:
                raise PreviewError(500, "storage_corruption", "rendered preview exceeds maximum file bounds")
        except OSError as exc:
            raise PreviewError(500, "storage_corruption", "failed to read rendered preview output") from exc

        if not png_bytes.startswith(PNG_SIGNATURE):
            raise PreviewError(500, "storage_corruption", "preview output is not a valid PNG")

        # Validate IHDR chunk structure and dimensions (SPEC 19.3.8)
        # 8 bytes signature + 4 bytes length + 4 bytes "IHDR" + 13 bytes data + 4 bytes CRC = 33 bytes min
        if len(png_bytes) < 33:
            raise PreviewError(500, "storage_corruption", "truncated preview PNG structure")

        ihdr_len = struct.unpack(">I", png_bytes[8:12])[0]
        ihdr_type = png_bytes[12:16]
        if ihdr_len != 13 or ihdr_type != b"IHDR":
            raise PreviewError(500, "storage_corruption", "preview output missing valid IHDR chunk")

        png_w, png_h = struct.unpack(">II", png_bytes[16:24])
        if png_w != width_px or png_h != height_px:
            raise PreviewError(500, "storage_corruption", "preview PNG dimensions do not match manifest")

        actual_sha256 = hashlib.sha256(png_bytes).hexdigest()
        if actual_sha256 != manifest_sha256:
            raise PreviewError(500, "storage_corruption", "preview output checksum mismatch")

        _validate_png(png_bytes, width_px, height_px)

        return PreviewAsset(
            png=png_bytes,
            sha256=actual_sha256,
            width_px=width_px,
            height_px=height_px,
            page_index=page_index,
        )


def _validate_png(data: bytes, width: int, height: int) -> None:
    """Validate the fixed RGB8 noninterlaced renderer format and bounded pixels."""
    def invalid():
        raise PreviewError(500, "storage_corruption", "invalid rendered PNG structure")

    pos = 8
    compressed = bytearray()
    types = []
    while pos < len(data):
        if pos + 12 > len(data):
            invalid()
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        end = pos + 12 + size
        if end > len(data):
            invalid()
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + size]
        crc = struct.unpack(">I", data[pos + 8 + size:end])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            invalid()
        if kind == b"IHDR":
            if types or payload != struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0):
                invalid()
        elif kind == b"IDAT":
            if not types or types[-1] not in (b"IHDR", b"IDAT"):
                invalid()
            compressed.extend(payload)
        elif kind == b"IEND":
            if size or end != len(data) or not compressed:
                invalid()
        else:
            invalid()
        types.append(kind)
        pos = end
    if not types or types[-1] != b"IEND":
        invalid()
    expected = height * (1 + width * 3)
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected + 1)
        if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            invalid()
        if any(raw[row * (1 + width * 3)] != 0 for row in range(height)):
            invalid()
    except zlib.error:
        invalid()
