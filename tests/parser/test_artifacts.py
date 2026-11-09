"""Tests for bounded local PDF artifact ingestion (SPEC R01, 10.2, 16.6, T01-T04, T80)."""

from __future__ import annotations

import errno
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from lab_parser import artifacts
from lab_parser.artifacts import (
    IngestedPdfArtifact,
    OBJECT_VERSION,
    PdfArtifactError,
    ingest_pdf_artifact,
)
from lab_db.storage import LocalArtifactStore
from lab_parser.importer import Limits
from lab_parser.isolation import InspectionError
from tests.parser.pdfgen import build_pdf
from tests.parser.test_isolation import crafted


class TestPdfArtifactIngestion(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "src"
        self.source_dir.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir(mode=0o700)
        self.store = LocalArtifactStore(self.artifact_root)

        self.pdf_1p = build_pdf(
            [{"mediabox": (0, 0, 200, 100), "rects": [(20, 10, 60, 30)]}]
        )
        self.pdf_2p = build_pdf(
            [
                {"mediabox": (0, 0, 200, 100), "rects": [(20, 10, 60, 30)]},
                {"mediabox": (0, 0, 300, 150), "rects": [(40, 20, 80, 60)]},
            ]
        )
        self.source = self.source_dir / "input.pdf"
        self.source.write_bytes(self.pdf_1p)

    def _stage_files(self) -> list[Path]:
        return list((self.artifact_root / "sha256").glob(".stage-*")) if (self.artifact_root / "sha256").is_dir() else []

    def _objects(self) -> list[Path]:
        base = self.artifact_root / "sha256"
        return [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else []

    def _assert_fail(
        self,
        expected_code: str,
        *,
        source: str | Path | None = None,
        artifact_root: str | Path | None = None,
        **kwargs,
    ) -> PdfArtifactError:
        src = self.source if source is None else source
        root = self.artifact_root if artifact_root is None else artifact_root
        with self.assertRaises(PdfArtifactError) as ctx:
            ingest_pdf_artifact(src, self.store if root is self.artifact_root else LocalArtifactStore(Path(root)), **kwargs)
        self.assertEqual(ctx.exception.code, expected_code)
        self.assertEqual(str(ctx.exception), expected_code)
        if self.artifact_root.is_dir():
            self.assertEqual(self._stage_files(), [])
        return ctx.exception

    def test_post_publication_fsync_preserves_explicit_uncertainty(self) -> None:
        digest = hashlib.sha256(self.pdf_1p).hexdigest()
        target = self.artifact_root / "sha256" / digest[:2] / digest
        original = os.fsync

        def fail_after_publication(fd):
            if target.exists():
                raise OSError(errno.EIO, "secret-fsync-path")
            return original(fd)

        with patch("lab_db.storage.os.fsync", side_effect=fail_after_publication):
            self._assert_fail("publication_durability_unknown")
        self.assertEqual(target.read_bytes(), self.pdf_1p)
        self.assertTrue(ingest_pdf_artifact(self.source, self.store).reused_existing)

    def test_cleanup_failure_keeps_pdf_publication_uncertainty(self) -> None:
        digest = hashlib.sha256(self.pdf_1p).hexdigest()
        target = self.artifact_root / "sha256" / digest[:2] / digest
        original_sync, original_unlink = os.fsync, os.unlink

        def fail_sync(fd):
            if target.exists():
                raise OSError(errno.EIO, "secret-sync")
            return original_sync(fd)

        def fail_unlink(path, *args, **kwargs):
            if str(path).startswith(".stage-"):
                raise OSError(errno.EIO, "secret-cleanup")
            return original_unlink(path, *args, **kwargs)

        before = len(os.listdir("/proc/self/fd"))
        with patch("lab_db.storage.os.fsync", side_effect=fail_sync), patch("lab_db.storage.os.unlink", side_effect=fail_unlink):
            with self.assertRaises(PdfArtifactError) as caught:
                ingest_pdf_artifact(self.source, self.store)
        self.assertEqual(caught.exception.code, "publication_durability_unknown")
        self.assertEqual(str(caught.exception), "publication_durability_unknown")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual(target.read_bytes(), self.pdf_1p)
        for path in self._stage_files():
            path.unlink()

    def test_staged_read_failure_closes_descriptors_and_publishes_nothing(self) -> None:
        before = len(os.listdir("/proc/self/fd"))
        real_pread = os.pread
        for failing_call in (1, 2):  # the header read, then the whole-file read for the child
            calls = []

            def pread(fd, n, offset, _calls=calls, _failing=failing_call):
                _calls.append(1)
                if len(_calls) == _failing:
                    raise OSError(errno.EIO, "secret-path")
                return real_pread(fd, n, offset)

            with patch.object(artifacts.os, "pread", side_effect=pread):
                self._assert_fail("artifact_root_unavailable")
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual(self._objects(), [])
        self.assertEqual(self._stage_files(), [])

    def test_valid_pdf_published_content_addressed_and_immutable_modes(self) -> None:
        res = ingest_pdf_artifact(self.source, self.store)
        expected_sha = hashlib.sha256(self.pdf_1p).hexdigest()

        self.assertIsInstance(res, IngestedPdfArtifact)
        self.assertEqual(res.outcome, "accept")
        self.assertIsNone(res.reason)
        self.assertEqual(res.sha256, expected_sha)
        self.assertEqual(res.size_bytes, len(self.pdf_1p))
        self.assertEqual(res.page_count, 1)
        self.assertEqual(res.object_key, f"sha256/{expected_sha[:2]}/{expected_sha}")
        self.assertEqual(res.object_version, OBJECT_VERSION)
        self.assertFalse(res.reused_existing)
        self.assertEqual(res.warnings, ())
        self.assertEqual(
            res.artifact_path,
            self.artifact_root / "sha256" / expected_sha[:2] / expected_sha,
        )
        self.assertTrue(res.artifact_path.is_file())
        self.assertEqual(res.artifact_path.read_bytes(), self.pdf_1p)
        self.assertEqual(stat.S_IMODE(res.artifact_path.stat().st_mode), 0o600)
        # Directory modes are the store's policy (covered by its own tests), not this module's.
        self.assertEqual(self._stage_files(), [])
        # Original source file is untouched
        self.assertEqual(self.source.read_bytes(), self.pdf_1p)

    def test_duplicate_and_different_pdf_publication_without_clobber(self) -> None:
        first = ingest_pdf_artifact(self.source, self.store)
        first_ino = first.artifact_path.stat().st_ino

        # Re-ingesting identical PDF reuses existing inode without clobbering
        second = ingest_pdf_artifact(self.source, self.store)
        self.assertTrue(second.reused_existing)
        self.assertEqual(second.sha256, first.sha256)
        self.assertEqual(second.object_key, first.object_key)
        self.assertEqual(second.page_count, 1)
        self.assertEqual(second.artifact_path.stat().st_ino, first_ino)
        self.assertEqual(second.artifact_path.read_bytes(), self.pdf_1p)
        self.assertEqual(self._stage_files(), [])

        # Ingesting different PDF creates a separate content-addressed object
        source2 = self.source_dir / "second.pdf"
        source2.write_bytes(self.pdf_2p)
        other = ingest_pdf_artifact(source2, self.store)
        self.assertFalse(other.reused_existing)
        self.assertNotEqual(other.sha256, first.sha256)
        self.assertEqual(other.page_count, 2)
        self.assertEqual(other.artifact_path.read_bytes(), self.pdf_2p)
        self.assertEqual(first.artifact_path.read_bytes(), self.pdf_1p)

    def test_concurrent_duplicate_writers_succeed_without_clobber_or_leaked_stages(
        self,
    ) -> None:
        workers = 8
        barrier = threading.Barrier(workers)
        results: list[IngestedPdfArtifact] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            src = self.source_dir / f"concurrent-{idx}.pdf"
            src.write_bytes(self.pdf_2p)
            try:
                barrier.wait(timeout=5)
                res = ingest_pdf_artifact(src, self.store)
                with lock:
                    results.append(res)
            except BaseException as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), workers)
        expected_sha = hashlib.sha256(self.pdf_2p).hexdigest()
        for res in results:
            self.assertEqual(res.sha256, expected_sha)
            self.assertEqual(res.size_bytes, len(self.pdf_2p))
            self.assertEqual(res.page_count, 2)
            self.assertEqual(res.artifact_path.read_bytes(), self.pdf_2p)

        # Exactly one thread won the atomic RENAME_NOREPLACE publication;
        # all others observed EEXIST, verified existing bytes, and cleaned stage.
        newly_published = [r for r in results if not r.reused_existing]
        reused = [r for r in results if r.reused_existing]
        self.assertEqual(len(newly_published), 1)
        self.assertEqual(len(reused), workers - 1)
        self.assertEqual(self._stage_files(), [])

    def test_corrupted_existing_destination_is_never_clobbered_and_fails_closed(
        self,
    ) -> None:
        expected_sha = hashlib.sha256(self.pdf_1p).hexdigest()
        prefix_dir = self.artifact_root / "sha256" / expected_sha[:2]
        prefix_dir.mkdir(parents=True)
        target = prefix_dir / expected_sha

        # Case 1: existing file has wrong bytes
        corrupt_payload = b"corrupted-existing-bytes"
        target.write_bytes(corrupt_payload)
        self._assert_fail("artifact_corrupted")
        self.assertEqual(target.read_bytes(), corrupt_payload)

        # Case 2: existing file has same length as valid PDF but 1 flipped byte
        tampered_same_len = bytearray(self.pdf_1p)
        tampered_same_len[-1] ^= 0xFF
        target.write_bytes(bytes(tampered_same_len))
        self._assert_fail("artifact_corrupted")
        self.assertEqual(target.read_bytes(), bytes(tampered_same_len))

        # Case 3: existing target is a symlink
        target.unlink()
        other = self.root / "outside.pdf"
        other.write_bytes(self.pdf_1p)
        target.symlink_to(other)
        self._assert_fail("artifact_corrupted")
        self.assertTrue(target.is_symlink())

        # Case 4: existing target is a directory
        target.unlink()
        target.mkdir()
        self._assert_fail("artifact_corrupted")
        self.assertTrue(target.is_dir())

    def test_symlink_in_artifact_subdirectories_rejected(self) -> None:
        outside = self.root / "outside_dir"
        outside.mkdir()
        (self.artifact_root / "sha256").symlink_to(outside)
        self._assert_fail("artifact_corrupted")
        self.assertEqual(list(outside.iterdir()), [])

    def test_bounded_streaming_exact_limit_and_limit_plus_one(self) -> None:
        exact_len = len(self.pdf_1p)
        res = ingest_pdf_artifact(
            self.source,
            self.store,
            limits=Limits(max_bytes=exact_len, max_pages=2000),
        )
        self.assertEqual(res.size_bytes, exact_len)

        # Over limit by 1 byte must abort during streaming without invoking pypdf
        with patch.object(
            artifacts,
            "_inspect_staged_fd",
            side_effect=AssertionError("must not inspect over-limit file"),
        ):
            self._assert_fail(
                "file_too_large",
                limits=Limits(max_bytes=exact_len - 1, max_pages=2000),
            )

    def test_page_limit_boundary_and_exceeded_cleaned_up(self) -> None:
        source_2p = self.source_dir / "two.pdf"
        source_2p.write_bytes(self.pdf_2p)

        err = self._assert_fail(
            "page_limit_exceeded",
            source=source_2p,
            limits=Limits(max_bytes=1024 * 1024, max_pages=1),
        )
        self.assertIsNotNone(err.inspection)
        self.assertIsNone(err.inspection.page_count)  # the child reports the limit, not a count
        self.assertEqual(self._objects(), [])

        ok = ingest_pdf_artifact(
            source_2p,
            self.store,
            limits=Limits(max_bytes=1024 * 1024, max_pages=2),
        )
        self.assertEqual(ok.page_count, 2)

    def test_invalid_empty_fake_corrupt_and_encrypted_pdfs_rejected_and_cleaned(
        self,
    ) -> None:
        # Empty file
        self.source.write_bytes(b"")
        self._assert_fail("empty_file")

        # Non-PDF
        self.source.write_bytes(b"not a pdf at all")
        self._assert_fail("not_a_pdf")

        # Leading whitespace >= 1024 bytes before %PDF-
        self.source.write_bytes(b" " * 1024 + self.pdf_1p)
        self._assert_fail("not_a_pdf")

        # Corrupt / truncated PDF with secret canary text inside
        self.source.write_bytes(b"%PDF-1.7\nsecret-canary-payload\n%%EOF")
        err = self._assert_fail("corrupt_pdf")
        self.assertNotIn("secret-canary-payload", str(err))

        # Encrypted PDF (even with empty password)
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("")
        bio = io.BytesIO()
        writer.write(bio)
        self.source.write_bytes(bio.getvalue())
        self._assert_fail("encrypted_pdf")

        self.assertEqual(self._objects(), [])
        self.assertEqual(self._stage_files(), [])

    def test_repaired_xref_is_rejected_with_nothing_published(self) -> None:
        """The isolation child treats any pypdf warning as an invalid PDF, so there is no
        flagged-publish path any more (allow_flagged is gone)."""
        from fixtures import registry_check as rc

        doc = rc.load()
        f05 = next(e for e in doc["fixtures"] if e["id"] == "F05")
        bad_xref_item = next(it for it in f05["items"] if it["id"] == "F05.bad_xref")
        self.source.write_bytes(rc.item_bytes(bad_xref_item))
        err = self._assert_fail("corrupt_pdf")
        self.assertEqual(err.inspection.outcome, "reject")
        self.assertEqual(self._objects(), [])
        self.assertEqual(self._stage_files(), [])
        with self.assertRaises(TypeError):
            ingest_pdf_artifact(self.source, self.store, allow_flagged=True)  # type: ignore[call-arg]

    def test_a_count_that_disagrees_with_kids_is_rejected_by_the_pdfium_cross_check(self) -> None:
        """pypdf follows /Kids (3 pages) while PDFium trusts /Count (1 or 99); the page_count
        stored must be the one both agree on, so a lying /Count publishes nothing."""
        for name, (kids, count, distinct) in {"below": (3, 1, True), "above": (3, 99, True),
                                              "repeated": (3, 1, False)}.items():
            with self.subTest(name=name):
                self.source.write_bytes(crafted(kids, count, distinct))
                self._assert_fail("corrupt_pdf")
                self.assertEqual(self._objects(), [])
        self.source.write_bytes(crafted(3, 3, True))  # the honest document of the same shape
        self.assertEqual(ingest_pdf_artifact(self.source, self.store).page_count, 3)

    def test_inspection_runs_in_the_isolation_child_with_scaled_limits(self) -> None:
        seen = []
        real = artifacts.inspect_geometry

        def spy(data, limits, **kwargs):
            seen.append((len(data), limits))
            return real(data, limits, **kwargs)

        with patch.object(artifacts, "inspect_geometry", side_effect=spy), \
                patch("pypdf.PdfReader", side_effect=AssertionError("parsed in the caller process")):
            res = ingest_pdf_artifact(self.source, self.store)
        self.assertEqual(res.page_count, 1)
        self.assertEqual(len(seen), 1)
        size, limits = seen[0]
        self.assertEqual(size, len(self.pdf_1p))
        self.assertGreaterEqual(limits.memory_bytes, 512 * 1024 * 1024)
        big = artifacts._process_limits(512 * 1024 * 1024, Limits())
        self.assertEqual((big.memory_bytes, big.cpu_seconds), (4 * 512 * 1024 ** 2 + 256 * 1024 ** 2, 69))
        self.assertLessEqual(big.wall_seconds, 180)

    def test_child_failures_map_to_fixed_codes_and_publish_nothing(self) -> None:
        for child_code, expected in (("timeout", "inspection_timeout"), ("memory_limit", "inspection_failed"),
                                     ("cancelled", "inspection_failed"), ("invalid_child_result", "inspection_failed"),
                                     ("encrypted_pdf", "encrypted_pdf"), ("page_limit_exceeded", "page_limit_exceeded")):
            with self.subTest(child_code=child_code):
                with patch.object(artifacts, "inspect_geometry", side_effect=InspectionError(child_code)):
                    self._assert_fail(expected)
                self.assertEqual(self._objects(), [])

    def test_symlink_directory_fifo_and_missing_source_rejected(self) -> None:
        # Missing source
        self._assert_fail("input_unavailable", source=self.source_dir / "missing.pdf")

        # Directory as source
        self._assert_fail("input_not_regular", source=self.source_dir)

        # FIFO as source (O_NONBLOCK prevents hanging)
        fifo = self.source_dir / "pipe.pdf"
        os.mkfifo(fifo)
        self._assert_fail("input_not_regular", source=fifo)

        # Symlink as source
        link = self.source_dir / "symlink.pdf"
        link.symlink_to(self.source)
        self._assert_fail("input_not_regular", source=link)

    def test_invalid_paths_store_and_options_rejected(self) -> None:
        # Embedded NUL in the source path
        self._assert_fail("invalid_path", source=str(self.source) + "\0evil")
        self._assert_fail("invalid_path", source=b"bytes-path")  # type: ignore[arg-type]

        # Invalid limits
        self._assert_fail("invalid_limits", limits="not-a-limits")  # type: ignore[arg-type]

        # Not a store: refused before the source is opened
        for bad in (None, object(), str(self.artifact_root)):
            with self.subTest(store=bad):
                with self.assertRaises(PdfArtifactError) as ctx:
                    ingest_pdf_artifact(self.source, bad)
                self.assertEqual(ctx.exception.code, "invalid_store")
        # Root problems (relative, missing, symlink, regular file) are the store constructor's
        # job and are covered in tests/dbunit/test_storage.py.

    def test_store_failures_map_to_fixed_codes_without_leaking_details(self) -> None:
        class Boom:
            root = self.artifact_root

            def __init__(self, exc: Exception) -> None:
                self.exc = exc

            def put_stream(self, fd, *, max_bytes, inspect):
                raise self.exc

        for exc, code in ((OSError(13, "secret /path/detail"), "artifact_root_unavailable"),
                          (RuntimeError("secret /path/detail"), "artifact_store_failed")):
            with self.subTest(code=code):
                with self.assertRaises(PdfArtifactError) as ctx:
                    ingest_pdf_artifact(self.source, Boom(exc))
                self.assertEqual(str(ctx.exception), code)
                self.assertNotIn("secret", repr(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__)

    def test_source_descriptor_is_closed_on_every_path(self) -> None:
        count = lambda: len(os.listdir("/proc/self/fd"))
        ingest_pdf_artifact(self.source, self.store)
        before = count()
        for _ in range(20):
            ingest_pdf_artifact(self.source, self.store)  # reused object
            with self.assertRaises(PdfArtifactError):
                ingest_pdf_artifact(self.source, self.store, limits=Limits(max_bytes=10, max_pages=5))
        self.assertEqual(count(), before)
