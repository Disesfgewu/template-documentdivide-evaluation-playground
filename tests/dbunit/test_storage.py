"""Unit tests for LocalArtifactStore without database dependencies."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_db.storage import LocalArtifactStore, StorageCorruption, StoredObject


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


if __name__ == "__main__":
    unittest.main()
