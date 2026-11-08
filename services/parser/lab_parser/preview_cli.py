"""Internal CLI module for supervised subprocess PDF page preview rendering (SPEC 8, 19)."""
from __future__ import annotations

import argparse
import json
import os
import resource
import stat
import sys
from pathlib import Path


class PreviewCliError(Exception):
    def __init__(self, exit_code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.reason = reason
        self.message = message


def _apply_fail_closed_limits() -> None:
    """Apply strict POSIX resource ceilings before importing renderer or PDFium."""
    # CPU time: 10s soft, 15s hard
    resource.setrlimit(resource.RLIMIT_CPU, (10, 15))
    # File size limit: 50MB
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
    # Address space: 1.5GB
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    # Disable core dumps
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def render_preview_to_files(
    input_path: str | Path,
    output_path: str | Path,
    manifest_path: str | Path,
    page_index: int,
    dpi: int = 150,
) -> None:
    """Render a single PDF page at fixed 150 DPI and write output PNG + manifest."""
    if dpi != 150:
        raise PreviewCliError(2, "invalid_dpi", f"preview rendering supports fixed 150 DPI only, got {dpi}")

    # Read input file safely using regular file descriptor (no symlinks)
    fd = os.open(str(input_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PreviewCliError(2, "invalid_file", "input must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as f:
            pdf_bytes = f.read()
    finally:
        os.close(fd)

    if not pdf_bytes:
        raise PreviewCliError(2, "empty_file", "input file is empty")

    from .render import render_region

    try:
        asset = render_region(
            pdf_bytes,
            page_index=page_index,
            region=(0.0, 0.0, 1.0, 1.0),
            dpi=150,
        )
    except ValueError as exc:
        msg = str(exc)
        if "pixel_limit" in msg:
            raise PreviewCliError(
                11,
                "pixel_limit_exceeded",
                "rendered page dimensions exceed 16MP or 4096px limit",
            )
        if "out of range" in msg:
            raise PreviewCliError(12, "page_out_of_range", msg)
        raise PreviewCliError(13, "render_error", f"render validation error: {msg}")
    except Exception as exc:
        raise PreviewCliError(14, "render_error", f"render failure: {type(exc).__name__}")

    # Write output PNG
    with open(output_path, "wb") as f:
        f.write(asset.png)

    # Write minimal manifest JSON
    manifest = {
        "width_px": asset.width_px,
        "height_px": asset.height_px,
        "sha256": asset.sha256,
        "dpi": asset.dpi,
        "page_index": asset.page_index,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)


def preview_page_cli(argv: list[str]) -> int:
    # Fail-closed resource ceilings applied immediately in child bootstrap
    try:
        _apply_fail_closed_limits()
    except Exception as exc:
        print(f"fatal: failed to set resource limits: {exc}", file=sys.stderr)
        return 3

    parser = argparse.ArgumentParser(description="Render a single PDF page preview")
    parser.add_argument("--input", required=True, help="Input PDF file path")
    parser.add_argument("--output", required=True, help="Output PNG file path")
    parser.add_argument("--manifest", required=True, help="Output manifest JSON path")
    parser.add_argument("--page", required=True, type=int, help="0-based page index")
    parser.add_argument("--dpi", type=int, default=150, help="Resolution in DPI (fixed 150)")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    if args.page < 0:
        return 12

    try:
        render_preview_to_files(
            input_path=args.input,
            output_path=args.output,
            manifest_path=args.manifest,
            page_index=args.page,
            dpi=args.dpi,
        )
        return 0
    except PreviewCliError as exc:
        return exc.exit_code
    except Exception:
        return 13


if __name__ == "__main__":
    sys.exit(preview_page_cli(sys.argv[1:]))
