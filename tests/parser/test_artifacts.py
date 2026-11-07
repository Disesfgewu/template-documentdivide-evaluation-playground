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
    STAGE_PREFIX,
    ingest_pdf_artifact,
)
from lab_parser.importer import Limits
from tests.parser.pdfgen import build_pdf


class TestPdfArtifactIngestion(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "src"
        self.source_dir.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir(mode=0o700)

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
        return list(self.artifact_root.glob(f"{STAGE_PREFIX}*"))

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
            ingest_pdf_artifact(src, root, **kwargs)
        self.assertEqual(ctx.exception.code, expected_code)
        self.assertEqual(str(ctx.exception), expected_code)
        if self.artifact_root.is_dir():
            self.assertEqual(self._stage_files(), [])
        return ctx.exception

    def test_valid_pdf_published_content_addressed_and_immutable_modes(self) -> None:
        res = ingest_pdf_artifact(self.source, self.artifact_root)
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
        self.assertEqual(
            stat.S_IMODE((self.artifact_root / "sha256").stat().st_mode), 0o700
        )
        self.assertEqual(
            stat.S_IMODE(
                (self.artifact_root / "sha256" / expected_sha[:2]).stat().st_mode
            ),
            0o700,
        )
        self.assertEqual(self._stage_files(), [])
        # Original source file is untouched
        self.assertEqual(self.source.read_bytes(), self.pdf_1p)

    def test_duplicate_and_different_pdf_publication_without_clobber(self) -> None:
        first = ingest_pdf_artifact(self.source, self.artifact_root)
        first_ino = first.artifact_path.stat().st_ino

        # Re-ingesting identical PDF reuses existing inode without clobbering
        second = ingest_pdf_artifact(self.source, self.artifact_root)
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
        other = ingest_pdf_artifact(source2, self.artifact_root)
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
                res = ingest_pdf_artifact(src, self.artifact_root)
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
            self.artifact_root,
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
        self.assertEqual(err.inspection.page_count, 2)
        self.assertFalse((self.artifact_root / "sha256").exists())

        ok = ingest_pdf_artifact(
            source_2p,
            self.artifact_root,
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

        self.assertFalse((self.artifact_root / "sha256").exists())
        self.assertEqual(self._stage_files(), [])

    def test_flagged_xref_repaired_rejected_by_default_and_optional_gate(self) -> None:
        from fixtures import registry_check as rc

        doc = rc.load()
        f05 = next(e for e in doc["fixtures"] if e["id"] == "F05")
        bad_xref_item = next(
            it for it in f05["items"] if it["id"] == "F05.bad_xref"
        )
        repaired_bytes = rc.item_bytes(bad_xref_item)
        self.source.write_bytes(repaired_bytes)

        # Default allow_flagged=False rejects xref_repaired and leaves no artifact
        err = self._assert_fail("xref_repaired")
        self.assertIsNotNone(err.inspection)
        self.assertEqual(err.inspection.outcome, "flag")
        self.assertEqual(err.inspection.reason, "xref_repaired")
        self.assertFalse((self.artifact_root / "sha256").exists())

        # Explicit allow_flagged=True publishes the original raw bytes and preserves warnings
        res = ingest_pdf_artifact(
            self.source, self.artifact_root, allow_flagged=True
        )
        self.assertEqual(res.outcome, "flag")
        self.assertEqual(res.reason, "xref_repaired")
        self.assertEqual(res.page_count, 2)
        self.assertGreater(len(res.warnings), 0)
        self.assertEqual(res.artifact_path.read_bytes(), repaired_bytes)

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

    def test_invalid_paths_roots_and_options_rejected(self) -> None:
        # Relative artifact_root
        self._assert_fail("invalid_artifact_root", artifact_root=Path("relative/root"))

        # Missing artifact_root
        self._assert_fail(
            "artifact_root_unavailable",
            artifact_root=self.root / "nonexistent_root",
        )

        # Symlink artifact_root
        root_link = self.root / "root_link"
        root_link.symlink_to(self.artifact_root)
        self._assert_fail("invalid_artifact_root", artifact_root=root_link)

        # Regular file as artifact_root
        self._assert_fail("invalid_artifact_root", artifact_root=self.source)

        # Embedded NUL in paths
        self._assert_fail("invalid_path", source=str(self.source) + "\0evil")
        self._assert_fail(
            "invalid_artifact_root",
            artifact_root=str(self.artifact_root) + "\0evil",
        )

        # Invalid limits or allow_flagged types
        self._assert_fail("invalid_limits", limits="not-a-limits")  # type: ignore[arg-type]
        self._assert_fail("invalid_options", allow_flagged="yes")  # type: ignore[arg-type]

    def test_stage_tampering_detected_and_aborted_before_publication(self) -> None:
        orig_inspect = artifacts._inspect_staged_fd

        # Tamper 1: mutate staged file bytes in-place during inspection
        def mutate_in_place(stage_fd: int, **kwargs):
            res = orig_inspect(stage_fd, **kwargs)
            os.lseek(stage_fd, 0, os.SEEK_SET)
            os.write(stage_fd, b"%PDF-1.7\ntampered")
            return res

        with patch.object(
            artifacts, "_inspect_staged_fd", side_effect=mutate_in_place
        ):
            self._assert_fail("stage_tampered")
        self.assertFalse((self.artifact_root / "sha256").exists())

        # Tamper 2: replace staged path with a different inode before publication
        def swap_stage_inode(stage_fd: int, **kwargs):
            res = orig_inspect(stage_fd, **kwargs)
            for stg in self._stage_files():
                stg.unlink()
                stg.write_bytes(self.pdf_1p)
            return res

        with patch.object(
            artifacts, "_inspect_staged_fd", side_effect=swap_stage_inode
        ):
            self._assert_fail("stage_tampered")
        self.assertFalse((self.artifact_root / "sha256").exists())
        self.assertEqual(self._stage_files(), [])

        # Tamper 3: hard-link the staged file before publication (st_nlink > 1)
        hardlink_path = self.root / "stage_hardlink.pdf"

        def hardlink_stage(stage_fd: int, **kwargs):
            res = orig_inspect(stage_fd, **kwargs)
            for stg in self._stage_files():
                os.link(stg, hardlink_path)
            return res

        with patch.object(
            artifacts, "_inspect_staged_fd", side_effect=hardlink_stage
        ):
            self._assert_fail("stage_tampered")
        hardlink_path.unlink(missing_ok=True)
        self.assertEqual(self._stage_files(), [])

    def test_publication_failure_and_cleanup_error_handling(self) -> None:
        # Simulated renameat2 unsupported failure cleans up stage
        with patch.object(
            artifacts,
            "_rename_noreplace",
            side_effect=PdfArtifactError("publication_unsupported"),
        ):
            self._assert_fail("publication_unsupported")

        # Simulated post-publication directory fsync failure reports durability unknown
        # while keeping the published immutable artifact intact
        orig_fsync_dir = artifacts._fsync_dir_fd
        with patch.object(
            artifacts,
            "_fsync_dir_fd",
            side_effect=PdfArtifactError("publication_durability_unknown"),
        ):
            with self.assertRaises(PdfArtifactError) as ctx:
                ingest_pdf_artifact(self.source, self.artifact_root)
            self.assertEqual(ctx.exception.code, "publication_durability_unknown")
        expected_sha = hashlib.sha256(self.pdf_1p).hexdigest()
        published_path = (
            self.artifact_root / "sha256" / expected_sha[:2] / expected_sha
        )
        self.assertTrue(published_path.is_file())
        self.assertEqual(published_path.read_bytes(), self.pdf_1p)
        self.assertEqual(self._stage_files(), [])
        _ = orig_fsync_dir

        # Simulated unlink failure during cleanup surfaces sanitized cleanup_failed
        self.source.write_bytes(b"not a pdf")
        with patch.object(
            artifacts.os,
            "unlink",
            side_effect=OSError(errno.EIO, "secret-disk-canary"),
        ):
            with self.assertRaises(PdfArtifactError) as ctx:
                ingest_pdf_artifact(self.source, self.artifact_root)
            self.assertEqual(ctx.exception.code, "cleanup_failed")
            self.assertNotIn("secret-disk-canary", str(ctx.exception))
        for stg in self._stage_files():
            stg.unlink()

    def test_stage_permission_failure_removes_stage_and_closes_descriptors(self) -> None:
        before = len(os.listdir("/proc/self/fd"))
        with patch.object(artifacts.os, "fchmod", side_effect=OSError(errno.EIO, "secret")):
            for _ in range(5):
                with self.assertRaises(PdfArtifactError) as ctx:
                    ingest_pdf_artifact(self.source, self.artifact_root)
                self.assertEqual(ctx.exception.code, "artifact_root_unavailable")
                self.assertNotIn("secret", str(ctx.exception))
                self.assertEqual(self._stage_files(), [])
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_cleanup_failure_closes_open_file_descriptors(self) -> None:
        self.source.write_bytes(b"not a pdf")
        before = len(os.listdir("/proc/self/fd"))
        with patch.object(artifacts.os, "unlink", side_effect=OSError(errno.EIO, "secret")):
            for _ in range(5):
                with self.assertRaises(PdfArtifactError) as ctx:
                    ingest_pdf_artifact(self.source, self.artifact_root)
                self.assertEqual(ctx.exception.code, "cleanup_failed")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        for stage in self._stage_files():
            stage.unlink()


if __name__ == "__main__":
    unittest.main()
