"""Unit tests for LocalArtifactStore without database dependencies."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
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


if __name__ == "__main__":
    unittest.main()
