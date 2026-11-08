"""Unit tests for LocalArtifactStore without database dependencies."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_db.storage import (
    LocalArtifactStore, StorageCorruption, StoredObject, StreamTooLarge, _bounded_file_hash,
)


class LocalArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.store = LocalArtifactStore(self.root)

    def test_root_must_be_existing_absolute_directory(self) -> None:
        with self.assertRaises(ValueError):
            LocalArtifactStore(Path("relative/store/path"))

        missing = self.root / "nonexistent"
        with self.assertRaises(ValueError):
            LocalArtifactStore(missing)

        regular_file = self.root / "some_file.bin"
        regular_file.write_bytes(b"x")
        with self.assertRaises(ValueError):
            LocalArtifactStore(regular_file)

    def test_put_open_verify_roundtrip_and_no_temp_leftovers(self) -> None:
        data = b"PDF-1.7 sample content-addressed bytes"
        expected_sha256 = hashlib.sha256(data).hexdigest()

        stored = self.store.put(data)
        self.assertIsInstance(stored, StoredObject)
        self.assertEqual(
            stored,
            StoredObject(
                object_key=f"sha256/{expected_sha256[:2]}/{expected_sha256}",
                object_version="1",
                sha256=expected_sha256,
                size_bytes=len(data),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            stored.object_version = "2"  # type: ignore[misc]

        self.assertEqual(self.store.open(stored.object_key), data)
        self.assertEqual(
            self.store.verify(stored.object_key), (expected_sha256, len(data))
        )

        bucket_dir = self.root / "sha256" / expected_sha256[:2]
        entries = list(bucket_dir.iterdir())
        self.assertEqual(entries, [bucket_dir / expected_sha256])

    def test_put_idempotent_does_not_rewrite_identical_content(self) -> None:
        data = b"idempotent artifact content"
        first = self.store.put(data)

        with patch("lab_db.storage.tempfile.mkstemp") as mock_mkstemp:
            mock_mkstemp.side_effect = AssertionError(
                "mkstemp should not be called when identical target already exists"
            )
            second = self.store.put(data)

        self.assertEqual(first, second)
        self.assertEqual(self.store.open(second.object_key), data)

        target = self.root / first.object_key
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_tampered_file_raises_storage_corruption_on_open_and_put(self) -> None:
        data = b"authentic content"
        stored = self.store.put(data)
        target = self.root / stored.object_key

        target.write_bytes(b"tampered content")

        with self.assertRaises(StorageCorruption):
            self.store.open(stored.object_key)

        with self.assertRaises(StorageCorruption):
            self.store.put(data)

    def test_oversized_existing_object_is_read_only_to_expected_size_plus_one(self) -> None:
        data = b"x"
        stored = self.store.put(data)
        with (self.root / stored.object_key).open("wb") as f:
            f.truncate(32 * 1024 * 1024)
        import lab_db.storage as storage
        original = storage._bounded_file_hash
        observed = []

        def capture(f, expected_size):
            result = original(f, expected_size)
            observed.append(result[1])
            return result

        with patch("lab_db.storage._bounded_file_hash", side_effect=capture):
            with self.assertRaises(StorageCorruption):
                self.store.put(data)
        self.assertEqual(observed, [2])

    def test_verifier_bounds_reads_when_file_grows(self) -> None:
        class GrowingFile(io.BytesIO):
            def read(self, size=-1):
                result = super().read(size)
                pos = self.tell()
                self.seek(0, io.SEEK_END)
                self.write(b"extra")
                self.seek(pos)
                return result

        _, read_size = _bounded_file_hash(GrowingFile(b"a"), 1)
        self.assertEqual(read_size, 2)

    def test_canonical_recheck_bounds_object_growth(self) -> None:
        data = b"x"
        stored = self.store.put(data)
        original = self.store._verify_existing_in_prefix

        def grow_after_first_check(*args):
            original(*args)
            with (self.root / stored.object_key).open("ab") as f:
                f.truncate(32 * 1024 * 1024)

        with patch.object(self.store, "_verify_existing_in_prefix", side_effect=grow_after_first_check):
            with patch("lab_db.storage._bounded_file_hash", wraps=_bounded_file_hash) as bounded:
                with self.assertRaises(StorageCorruption):
                    self.store.put(data)
        self.assertEqual(bounded.call_count, 2)

    def test_path_traversal_and_malformed_keys_raise_value_error(self) -> None:
        digest = "a" * 64
        invalid_keys = [
            "../secret",
            "../../etc/passwd",
            f"sha256/../{digest}",
            f"sha256/aa/../{digest}",
            f"sha256/aa/{digest}/../other",
            f"/sha256/aa/{digest}",
            f"sha256/ab/{digest}",  # prefix does not match digest[:2]
            "sha256/aa/short",
            f"sha256/AA/{'A' * 64}",
            "",
        ]
        for bad_key in invalid_keys:
            with self.subTest(key=bad_key):
                with self.assertRaises(ValueError):
                    self.store.open(bad_key)
                with self.assertRaises(ValueError):
                    self.store.verify(bad_key)

    def test_root_symlink_raises_value_error(self) -> None:
        symlink_root = self.root / "symlink_root"
        real_root = self.root / "real_root"
        real_root.mkdir()
        symlink_root.symlink_to(real_root)
        with self.assertRaises(ValueError):
            LocalArtifactStore(symlink_root)

    def test_root_ancestor_symlink_raises_value_error(self) -> None:
        base = self.root / "ancestor_base"
        base.mkdir()
        outside = self.root / "outside_dir"
        outside.mkdir()
        real_root = outside / "real_root"
        real_root.mkdir()

        link = base / "symlink_component"
        link.symlink_to(outside)

        traversed_root = base / "symlink_component" / "real_root"
        self.assertTrue(traversed_root.exists())
        self.assertTrue(traversed_root.is_dir())
        self.assertFalse(traversed_root.is_symlink())

        with self.assertRaises(ValueError):
            LocalArtifactStore(traversed_root)

    def test_sha256_component_symlink_raises_storage_corruption(self) -> None:
        outside = self.root.parent / "outside_dir"
        outside.mkdir(exist_ok=True)
        sha256_dir = self.root / "sha256"
        sha256_dir.symlink_to(outside)

        data = b"sample artifact bytes"
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest}"

        with self.assertRaises(StorageCorruption):
            self.store.put(data)
        with self.assertRaises(StorageCorruption):
            self.store.open(key)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key)

    def test_prefix_component_symlink_raises_storage_corruption(self) -> None:
        data = b"sample artifact bytes"
        digest = hashlib.sha256(data).hexdigest()
        prefix = digest[:2]
        key = f"sha256/{prefix}/{digest}"

        sha256_dir = self.root / "sha256"
        sha256_dir.mkdir(parents=True)
        outside = self.root.parent / "outside_prefix"
        outside.mkdir(exist_ok=True)
        (sha256_dir / prefix).symlink_to(outside)

        with self.assertRaises(StorageCorruption):
            self.store.put(data)
        with self.assertRaises(StorageCorruption):
            self.store.open(key)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key)

    def test_object_file_symlink_raises_storage_corruption_and_does_not_overwrite(
        self,
    ) -> None:
        data = b"sample artifact bytes"
        digest = hashlib.sha256(data).hexdigest()
        prefix = digest[:2]
        key = f"sha256/{prefix}/{digest}"

        parent = self.root / "sha256" / prefix
        parent.mkdir(parents=True)

        target = parent / digest
        outside_file = self.root.parent / "outside_file.txt"
        outside_content = b"sensitive outside file content"
        outside_file.write_bytes(outside_content)
        target.symlink_to(outside_file)

        with self.assertRaises(StorageCorruption):
            self.store.put(data)
        with self.assertRaises(StorageCorruption):
            self.store.open(key)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key)

        self.assertEqual(outside_file.read_bytes(), outside_content)
        self.assertTrue(target.is_symlink())

    def test_dangling_symlink_raises_storage_corruption(self) -> None:
        data = b"sample artifact bytes"
        digest = hashlib.sha256(data).hexdigest()
        prefix = digest[:2]
        key = f"sha256/{prefix}/{digest}"

        parent = self.root / "sha256" / prefix
        parent.mkdir(parents=True)

        target = parent / digest
        target.symlink_to(self.root.parent / "nonexistent_file.txt")

        with self.assertRaises(StorageCorruption):
            self.store.put(data)
        with self.assertRaises(StorageCorruption):
            self.store.open(key)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key)

    def test_concurrent_writers_identical_bytes(self) -> None:
        import concurrent.futures

        data = b"concurrent multi-writer content addressed bytes"
        expected_sha256 = hashlib.sha256(data).hexdigest()
        key = f"sha256/{expected_sha256[:2]}/{expected_sha256}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(self.store.put, data) for _ in range(16)]
            results = [f.result() for f in futures]

        for res in results:
            self.assertEqual(res.object_key, key)
            self.assertEqual(res.sha256, expected_sha256)
            self.assertEqual(res.size_bytes, len(data))

        self.assertEqual(self.store.open(key), data)
        bucket_dir = self.root / "sha256" / expected_sha256[:2]
        self.assertEqual(list(bucket_dir.iterdir()), [bucket_dir / expected_sha256])

    def test_race_link_file_exists_verifies_existing_bytes_and_succeeds(self) -> None:
        import os

        data = b"racing artifact content"
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest}"

        original_link = os.link

        def racing_link(src, dst, **kwargs):
            parent = self.root / "sha256" / digest[:2]
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / digest
            target.write_bytes(data)
            return original_link(src, dst, **kwargs)

        with patch("lab_db.storage.os.link", side_effect=racing_link):
            stored = self.store.put(data)

        self.assertEqual(stored.sha256, digest)
        self.assertEqual(self.store.open(key), data)

    def test_race_link_file_exists_corrupted_fails_closed_and_does_not_overwrite(
        self,
    ) -> None:
        import os

        data = b"racing artifact content"
        digest = hashlib.sha256(data).hexdigest()
        corrupted_data = b"racing corrupted content"

        original_link = os.link

        def racing_link(src, dst, **kwargs):
            parent = self.root / "sha256" / digest[:2]
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / digest
            target.write_bytes(corrupted_data)
            return original_link(src, dst, **kwargs)

        with patch("lab_db.storage.os.link", side_effect=racing_link):
            with self.assertRaises(StorageCorruption):
                self.store.put(data)

        target = self.root / "sha256" / digest[:2] / digest
        self.assertEqual(target.read_bytes(), corrupted_data)

    def test_race_link_file_exists_symlink_fails_closed_and_does_not_overwrite(
        self,
    ) -> None:
        import os

        data = b"racing artifact content"
        digest = hashlib.sha256(data).hexdigest()
        outside_file = self.root.parent / "outside_target.txt"
        outside_file.write_bytes(b"untouchable")

        original_link = os.link

        def racing_link(src, dst, **kwargs):
            parent = self.root / "sha256" / digest[:2]
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / digest
            target.symlink_to(outside_file)
            return original_link(src, dst, **kwargs)

        with patch("lab_db.storage.os.link", side_effect=racing_link):
            with self.assertRaises(StorageCorruption):
                self.store.put(data)

        self.assertEqual(outside_file.read_bytes(), b"untouchable")
        target = self.root / "sha256" / digest[:2] / digest
        self.assertTrue(target.is_symlink())

    def test_verify_returns_actual_bytes_hash_on_disk(self) -> None:
        data = b"original valid bytes"
        stored = self.store.put(data)
        target = self.root / stored.object_key
        target.write_bytes(b"modified bytes on disk")
        actual_sha256, actual_size = self.store.verify(stored.object_key)
        self.assertEqual(
            actual_sha256, hashlib.sha256(b"modified bytes on disk").hexdigest()
        )
        self.assertEqual(actual_size, len(b"modified bytes on disk"))

    def test_swapped_directory_race_before_temp_creation_does_not_write_outside(
        self,
    ) -> None:
        data = b"swapped dir test payload"
        digest = hashlib.sha256(data).hexdigest()
        prefix = digest[:2]

        prefix_path = self.root / "sha256" / prefix
        prefix_path.mkdir(parents=True, exist_ok=True)

        outside_dir = self.root.parent / "outside_swapped_target"
        outside_dir.mkdir(exist_ok=True)

        original_open = os.open

        def swapped_open(file, flags, *args, **kwargs):
            if isinstance(file, str) and file.startswith(f".tmp-{digest[:8]}-"):
                if prefix_path.exists() and not prefix_path.is_symlink():
                    hidden_path = self.root / "sha256" / f"hidden_{prefix}"
                    prefix_path.rename(hidden_path)
                    prefix_path.symlink_to(outside_dir)
            return original_open(file, flags, *args, **kwargs)

        with patch("lab_db.storage.os.open", side_effect=swapped_open):
            with self.assertRaises(StorageCorruption):
                self.store.put(data)

        self.assertEqual(list(outside_dir.iterdir()), [])

    def test_swapped_directory_race_on_existing_object_fails_closed(self) -> None:
        data = b"existing object swapped dir test payload"
        stored = self.store.put(data)
        digest = stored.sha256
        prefix = digest[:2]

        prefix_path = self.root / "sha256" / prefix
        outside_dir = self.root.parent / "outside_existing_target"
        outside_dir.mkdir(exist_ok=True)

        real_verify_existing = LocalArtifactStore._verify_existing_in_prefix

        def swapped_verify(*args, **kwargs):
            res = real_verify_existing(self.store, *args, **kwargs)
            hidden_path = self.root / "sha256" / f"hidden_exist_{prefix}"
            prefix_path.rename(hidden_path)
            prefix_path.symlink_to(outside_dir)
            return res

        with patch.object(
            self.store,
            "_verify_existing_in_prefix",
            side_effect=swapped_verify,
        ):
            with self.assertRaises(StorageCorruption):
                self.store.put(data)

    def test_directory_identity_mismatch_fails_closed(self) -> None:
        data = b"directory identity mismatch test payload"
        digest = hashlib.sha256(data).hexdigest()
        prefix = digest[:2]

        prefix_path = self.root / "sha256" / prefix
        prefix_path.mkdir(parents=True, exist_ok=True)

        original_open = os.open

        def replace_with_new_dir(file, flags, *args, **kwargs):
            if isinstance(file, str) and file.startswith(f".tmp-{digest[:8]}-"):
                if prefix_path.exists() and not prefix_path.is_symlink():
                    hidden_path = self.root / "sha256" / f"hidden_inode_{prefix}"
                    prefix_path.rename(hidden_path)
                    prefix_path.mkdir()  # New directory has different inode
            return original_open(file, flags, *args, **kwargs)

        with patch("lab_db.storage.os.open", side_effect=replace_with_new_dir):
            with self.assertRaises(StorageCorruption):
                self.store.put(data)

    def test_fifo_object_fails_closed_and_does_not_hang(self) -> None:
        data = b"fifo test payload"
        digest = hashlib.sha256(data).hexdigest()
        prefix = digest[:2]
        key = f"sha256/{prefix}/{digest}"

        parent = self.root / "sha256" / prefix
        parent.mkdir(parents=True, exist_ok=True)
        fifo_path = parent / digest
        os.mkfifo(fifo_path)

        with self.assertRaises(StorageCorruption):
            self.store.open(key)

        with self.assertRaises(StorageCorruption):
            self.store.verify(key)

        with self.assertRaises(StorageCorruption):
            self.store.put(data)

    def test_open_and_verify_bounded_by_trusted_expected_size(self) -> None:
        data = b"trusted expected size bounded read payload"
        stored = self.store.put(data)
        key = stored.object_key
        size = len(data)

        # 1. Matching expected_size succeeds
        self.assertEqual(self.store.open(key, expected_size=size), data)
        self.assertEqual(self.store.verify(key, expected_size=size), (stored.sha256, size))

        # 2. Invalid expected_size (negative or non-int) raises ValueError
        with self.assertRaises(ValueError):
            self.store.open(key, expected_size=-1)
        with self.assertRaises(ValueError):
            self.store.verify(key, expected_size=-5)

        # 3. Mismatched expected_size raises StorageCorruption
        with self.assertRaises(StorageCorruption):
            self.store.open(key, expected_size=size + 10)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key, expected_size=size + 10)

        with self.assertRaises(StorageCorruption):
            self.store.open(key, expected_size=size - 5)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key, expected_size=size - 5)

    def test_corrupt_oversized_file_on_disk_rejected_before_unbounded_read(self) -> None:
        data = b"initial valid payload"
        stored = self.store.put(data)
        key = stored.object_key
        expected_size = len(data)

        # Corrupt the file on disk by appending oversized bytes
        disk_file = self.root / "sha256" / stored.sha256[:2] / stored.sha256
        with open(disk_file, "ab") as f:
            f.write(b"EXTRABYTES" * 100)

        with self.assertRaises(StorageCorruption):
            self.store.open(key, expected_size=expected_size)

        with self.assertRaises(StorageCorruption):
            self.store.verify(key, expected_size=expected_size)

    def test_server_controlled_ceiling_max_bytes(self) -> None:
        data = b"server ceiling max_bytes payload"
        stored = self.store.put(data)
        key = stored.object_key
        size = len(data)

        # 1. Matching or larger max_bytes succeeds
        self.assertEqual(self.store.open(key, max_bytes=size), data)
        self.assertEqual(self.store.open(key, max_bytes=size + 100), data)
        self.assertEqual(self.store.verify(key, max_bytes=size), (stored.sha256, size))

        # 2. max_bytes smaller than object size raises StorageCorruption before unbounded read
        with self.assertRaises(StorageCorruption):
            self.store.open(key, max_bytes=size - 1)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key, max_bytes=size - 1)

        # 3. expected_size exceeding max_bytes raises StorageCorruption
        with self.assertRaises(StorageCorruption):
            self.store.open(key, expected_size=size, max_bytes=size - 1)
        with self.assertRaises(StorageCorruption):
            self.store.verify(key, expected_size=size, max_bytes=size - 1)

    def test_availability_check_artifact_bounded_verify(self) -> None:
        from lab_db.availability import _check_artifact

        data = b"availability check artifact payload"
        stored = self.store.put(data)
        key = stored.object_key
        size = len(data)

        # 1. Valid artifact passes
        reasons_ok = _check_artifact(
            "committed", key, stored.sha256, self.store, expected_size=size
        )
        self.assertEqual(reasons_ok, [])

        # 2. Corrupt/oversized artifact on disk detected via bounded verify
        disk_file = self.root / "sha256" / stored.sha256[:2] / stored.sha256
        with open(disk_file, "ab") as f:
            f.write(b"EXTRABYTES")

        reasons_corrupt = _check_artifact(
            "committed", key, stored.sha256, self.store, expected_size=size
        )
        self.assertIn("artifact_corrupt", reasons_corrupt)


class PutStreamTests(unittest.TestCase):
    """``put_stream``: bounded streaming publication through the one store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.store = LocalArtifactStore(self.root)
        self.src_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.src_dir.cleanup)

    def source(self, data: bytes) -> int:
        path = Path(self.src_dir.name) / f"src-{len(list(Path(self.src_dir.name).iterdir()))}"
        path.write_bytes(data)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.addCleanup(lambda: self._close(fd))
        return fd

    @staticmethod
    def _close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def stages(self) -> list[str]:
        return [n for n in os.listdir(self.root / "sha256") if n.startswith(".stage-")]

    def objects(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in (self.root / "sha256").rglob("*") if p.is_file())

    def fd_count(self) -> int:
        return len(os.listdir("/proc/self/fd"))

    def test_streamed_object_equals_put_and_second_stream_is_reused(self) -> None:
        data = b"%PDF-1.7 streamed bytes" * 1000
        stored = self.store.put_stream(self.source(data), max_bytes=len(data))
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(stored, StoredObject(f"sha256/{digest[:2]}/{digest}", "1", digest, len(data)))
        self.assertFalse(stored.reused_existing)
        self.assertEqual(self.store.open(stored.object_key), data)
        self.assertEqual(self.stages(), [])
        again = self.store.put_stream(self.source(data), max_bytes=len(data))
        self.assertTrue(again.reused_existing)
        self.assertEqual(again, stored)
        self.assertEqual(self.store.put(data), stored)  # one layout, one object
        self.assertEqual(self.objects(), [stored.object_key])

    def test_limit_boundary_exact_and_one_over(self) -> None:
        data = b"a" * 100
        self.assertEqual(self.store.put_stream(self.source(data), max_bytes=100).size_bytes, 100)
        with self.assertRaises(StreamTooLarge):
            self.store.put_stream(self.source(b"b" * 101), max_bytes=100)
        self.assertEqual(self.stages(), [])
        self.assertEqual(len(self.objects()), 1)  # the over-limit source was not published
        self.assertEqual(self.store.put_stream(self.source(b""), max_bytes=0).size_bytes, 0)
        with self.assertRaises(StreamTooLarge):
            self.store.put_stream(self.source(b"x"), max_bytes=0)

    def test_reads_at_most_limit_plus_one_bytes(self) -> None:
        fd = self.source(b"c" * 5000)
        with self.assertRaises(StreamTooLarge):
            self.store.put_stream(fd, max_bytes=10)
        self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 11)

    def test_inspect_sees_staged_bytes_and_can_reject(self) -> None:
        data = b"inspect me"
        seen = []

        def inspect(stage_fd: int, digest: str, size: int) -> None:
            os.lseek(stage_fd, 0, os.SEEK_SET)
            seen.append((os.read(stage_fd, 100), digest, size))
            raise RuntimeError("rejected by caller")

        with self.assertRaisesRegex(RuntimeError, "rejected by caller"):
            self.store.put_stream(self.source(data), max_bytes=100, inspect=inspect)
        self.assertEqual(seen, [(data, hashlib.sha256(data).hexdigest(), len(data))])
        self.assertEqual(self.stages(), [])
        self.assertEqual(self.objects(), [])

    def test_accepting_inspect_publishes(self) -> None:
        stored = self.store.put_stream(self.source(b"ok"), max_bytes=10, inspect=lambda *_: None)
        self.assertEqual(self.objects(), [stored.object_key])

    def test_stage_rewritten_in_place_is_detected_before_publication(self) -> None:
        def inspect(stage_fd: int, digest: str, size: int) -> None:
            os.pwrite(stage_fd, b"Z", 0)  # same size, different content

        with self.assertRaises(StorageCorruption):
            self.store.put_stream(self.source(b"original bytes"), max_bytes=100, inspect=inspect)
        self.assertEqual(self.stages(), [])
        self.assertEqual(self.objects(), [])

    def test_stage_replaced_by_another_inode_is_detected(self) -> None:
        data = b"original bytes"

        def inspect(stage_fd: int, digest: str, size: int) -> None:
            (name,) = self.stages()
            path = self.root / "sha256" / name
            path.unlink()
            path.write_bytes(data)  # identical bytes, different inode
            os.chmod(path, 0o600)

        with self.assertRaises(StorageCorruption):
            self.store.put_stream(self.source(data), max_bytes=100, inspect=inspect)
        self.assertEqual(self.stages(), [])
        self.assertEqual(self.objects(), [])

    def test_stage_with_extra_link_or_wrong_mode_is_detected(self) -> None:
        for tamper in (lambda p: os.link(p, str(p) + ".alias"), lambda p: os.chmod(p, 0o644)):
            with self.subTest(tamper=tamper):
                def inspect(stage_fd: int, digest: str, size: int, tamper=tamper) -> None:
                    (name,) = [n for n in self.stages()]
                    tamper(self.root / "sha256" / name)

                with self.assertRaises(StorageCorruption):
                    self.store.put_stream(self.source(b"bytes " + os.urandom(4)), max_bytes=100, inspect=inspect)
                for leftover in (self.root / "sha256").glob(".stage-*"):
                    leftover.unlink()
                self.assertEqual(self.objects(), [])

    def test_corrupted_existing_destination_is_never_clobbered(self) -> None:
        data = b"genuine bytes"
        stored = self.store.put(data)
        target = self.root / stored.object_key
        target.write_bytes(b"corrupt bytes!")
        with self.assertRaises(StorageCorruption):
            self.store.put_stream(self.source(data), max_bytes=100)
        self.assertEqual(target.read_bytes(), b"corrupt bytes!")
        self.assertEqual(self.stages(), [])

    def test_symlinked_sha256_directory_is_refused(self) -> None:
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.root / "sha256")
        with self.assertRaises(Exception) as ctx:
            self.store.put_stream(self.source(b"x"), max_bytes=10)
        self.assertNotIsInstance(ctx.exception, StreamTooLarge)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_stage_removal_failure_is_reported_and_closes_descriptors(self) -> None:
        real_unlink = os.unlink

        def failing_unlink(path, *args, **kwargs):
            if str(path).startswith(".stage-"):
                raise OSError(13, "denied")
            return real_unlink(path, *args, **kwargs)

        before = self.fd_count()
        for _ in range(10):
            with patch("lab_db.storage.os.unlink", side_effect=failing_unlink):
                with self.assertRaisesRegex(StorageCorruption, "failed to remove staging file"):
                    self.store.put_stream(self.source(b"y" * 10 + os.urandom(4)), max_bytes=100)
            for leftover in (self.root / "sha256").glob(".stage-*"):
                leftover.unlink()
        self.assertLessEqual(self.fd_count(), before + 10)  # only the test's own source descriptors

    def test_descriptors_do_not_grow_across_repeated_failures(self) -> None:
        def reject(*_args) -> None:
            raise RuntimeError("no")

        self.store.put_stream(self.source(b"warm"), max_bytes=10)
        sources = [self.source(b"z" * 8) for _ in range(30)]
        before = self.fd_count()
        for fd in sources:
            with self.assertRaises(RuntimeError):
                self.store.put_stream(fd, max_bytes=100, inspect=reject)
        self.assertEqual(self.fd_count(), before)

    def test_concurrent_duplicate_streams_publish_one_object(self) -> None:
        import threading

        data = b"concurrent bytes " * 4096
        results, errors = [], []
        barrier = threading.Barrier(8)
        sources = [self.source(data) for _ in range(8)]

        def work(fd: int) -> None:
            try:
                barrier.wait()
                results.append(self.store.put_stream(fd, max_bytes=len(data)))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(fd,)) for fd in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({r.object_key for r in results}), 1)
        self.assertEqual(sum(not r.reused_existing for r in results), 1)
        self.assertEqual(self.stages(), [])
        self.assertEqual(len(self.objects()), 1)

    def test_invalid_arguments(self) -> None:
        for fd in (-1, True, "3", None):
            with self.subTest(fd=fd), self.assertRaises(ValueError):
                self.store.put_stream(fd, max_bytes=10)
        fd = self.source(b"x")
        for bad in (-1, True, 1.5, None):
            with self.subTest(max_bytes=bad), self.assertRaises(ValueError):
                self.store.put_stream(fd, max_bytes=bad)


if __name__ == "__main__":
    unittest.main()
