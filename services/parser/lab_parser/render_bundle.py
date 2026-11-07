"""Private, no-clobber local L0 PNG bundles; not a verified evidence publisher."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Iterator

from pypdf import PdfReader

from .render import _validate_dpi, _validate_region, render_region

SCHEMA_VERSION = "l0-render-bundle-1"
MAX_BYTES = 512 * 1024 * 1024
MAX_PAGES = 2000


class BundleError(ValueError):
    """Fixed diagnostic code; never includes source paths or parser payloads."""


@contextmanager
def _parser_diagnostics() -> Iterator[list[int]]:
    # This local operator command is single-threaded. Capture counts, not PDF text.
    logger = logging.getLogger("pypdf")
    old_handlers, old_propagate, old_level = logger.handlers[:], logger.propagate, logger.level
    count = [0]

    class Counter(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            count[0] += 1

    logger.handlers = [Counter()]
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    try:
        yield count
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
        logger.setLevel(old_level)


def _read_pdf(path: Path, max_bytes: int) -> bytes:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleError("input_not_regular")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(max_bytes + 1)
    except BundleError:
        raise
    except (OSError, ValueError):
        raise BundleError("input_unavailable") from None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:
        raise BundleError("input_too_large")
    if not data.startswith(b"%PDF-"):
        raise BundleError("invalid_pdf")
    return data


def _geometry(data: bytes, page_index: int) -> tuple[int, list[float]]:
    try:
        with _parser_diagnostics() as diagnostics:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise BundleError("encrypted_pdf")
            page_count = len(reader.pages)
            if not 0 < page_count <= MAX_PAGES:
                raise BundleError("page_limit")
            if page_index >= page_count:
                raise BundleError("page_out_of_range")
            page = reader.pages[page_index]
            rotation = page.get("/Rotate", 0)
            if isinstance(rotation, bool) or not isinstance(rotation, int) or rotation % 90:
                raise BundleError("invalid_geometry")
            media, crop = tuple(map(float, page.mediabox)), tuple(map(float, page.cropbox))
            if not all(math.isfinite(value) for value in (*media, *crop)):
                raise BundleError("invalid_geometry")
            for box in (media, crop):
                if box[0] >= box[2] or box[1] >= box[3]:
                    raise BundleError("invalid_geometry")
            visible = [max(media[0], crop[0]), max(media[1], crop[1]),
                       min(media[2], crop[2]), min(media[3], crop[3])]
            width, height = visible[2] - visible[0], visible[3] - visible[1]
            if width <= 0 or height <= 0:
                raise BundleError("invalid_geometry")
            if rotation % 180:
                width, height = height, width
            if diagnostics[0]:
                raise BundleError("parser_warning")
            return page_count, [width, height]
    except BundleError:
        raise
    except Exception:
        raise BundleError("invalid_pdf") from None


def _publish(stage: Path, destination: Path) -> None:
    """Linux renameat2 RENAME_NOREPLACE: even an empty destination is preserved."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise BundleError("publication_unsupported")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise BundleError("output_exists")
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise BundleError("publication_unsupported")
        raise BundleError("output_unavailable")


def create_bundle(source: str | Path, output_dir: str | Path, *, page_index: int,
                  bbox: tuple[float, float, float, float] | list[float] = (0, 0, 1, 1),
                  dpi: int = 150, max_bytes: int = MAX_BYTES) -> dict:
    """Render one original-PDF region and atomically publish an immutable bundle.

    Linux and an exclusively controlled output parent are required. Limits bound
    input/image sizes, not malicious-parser CPU/RAM or concurrent use of this API.
    """
    if sys.platform != "linux":
        raise BundleError("platform_unsupported")
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise BundleError("invalid_page_index")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_BYTES:
        raise BundleError("invalid_byte_limit")
    try:
        region, resolution = _validate_region(bbox), _validate_dpi(dpi)
    except (ValueError, TypeError):
        raise BundleError("invalid_render_profile") from None
    try:
        source, destination = Path(source), Path(output_dir)
        if b"\0" in os.fsencode(source) or b"\0" in os.fsencode(destination):
            raise BundleError("invalid_path")
    except (TypeError, ValueError):
        raise BundleError("invalid_path") from None
    # Reject all pre-existing entries, including dangling symlinks, before parsing.
    if os.path.lexists(destination):
        raise BundleError("output_exists")
    data = _read_pdf(source, max_bytes)
    page_count, size = _geometry(data, page_index)
    try:
        asset = render_region(data, page_index, region, resolution)
    except Exception as error:
        code = "pixel_limit" if isinstance(error, ValueError) and str(error) == "pixel_limit" else "render_failed"
        raise BundleError(code) from None
    if any(not math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-5)
           for a, b in zip(asset.transform["page_size_pt"], size)):
        raise BundleError("geometry_disagreement")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "unverified_l0",
        "source": {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                   "page_count": page_count, "page_index": page_index, "bbox": list(region)},
        "asset": {"filename": "asset.png", "media_type": "image/png", "sha256": asset.sha256,
                  "size_bytes": len(asset.png), "width_px": asset.width_px, "height_px": asset.height_px,
                  "dpi": asset.dpi, "transform": asset.transform},
    }
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=".l0-stage-", dir=destination.parent))
        for name, payload in (("asset.png", asset.png), ("manifest.json",
                (json.dumps(manifest, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode())):
            fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        directory_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _publish(stage, destination)
        stage = None
        # Published bytes remain available if the final durability barrier fails.
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise BundleError("publication_durability_unknown") from None
    except BundleError:
        raise
    except OSError:
        raise BundleError("output_unavailable") from None
    finally:
        if stage is not None:
            try:
                shutil.rmtree(stage)
            except OSError:
                raise BundleError("cleanup_failed") from None
    return manifest


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BundleError("invalid_arguments")


@contextmanager
def _quiet_native_stderr() -> Iterator[None]:
    # CLI only: include native library diagnostics which Python logging cannot catch.
    saved = os.dup(2)
    try:
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 2)
            yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--page-index", type=int, required=True)
    parser.add_argument("--bbox", type=float, nargs=4, default=(0, 0, 1, 1))
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    try:
        args = parser.parse_args(argv)
        with _quiet_native_stderr():
            result = create_bundle(args.source, args.output_dir, page_index=args.page_index,
                                   bbox=args.bbox, dpi=args.dpi, max_bytes=args.max_bytes)
    except BundleError as error:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "failed", "code": str(error)}))
        return 1
    print(json.dumps({"schema_version": SCHEMA_VERSION, "status": result["status"],
                      "source_sha256": result["source"]["sha256"], "asset_sha256": result["asset"]["sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
