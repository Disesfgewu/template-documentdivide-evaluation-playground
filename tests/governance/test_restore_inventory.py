"""Adversarial and positive regression tests for restored artifact inventory verification (Refs #14)."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import tools.governance.restore_inventory as ri
from tools.governance.restore_inventory import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_INVENTORY_BYTES,
    DEFAULT_MAX_INVENTORY_ITEMS,
    InventoryItem,
    InventoryVerificationError,
    VerificationReport,
    main,
    parse_inventory,
    verify_restored_inventory,
)


def _make_object(data: bytes) -> tuple[str, str, int]:
    digest = hashlib.sha256(data).hexdigest()
    key = f"sha256/{digest[:2]}/{digest}"
    return key, digest, len(data)


def _write_store_object(root: Path, key: str, data: bytes) -> Path:
    target = root.joinpath(*key.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


class RestoredInventoryVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.store_root = Path(self.tmp_dir.name) / "artifacts"
        self.store_root.mkdir()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_verify_success_single_and_multiple_objects(self) -> None:
        data1 = b"production artifact payload one"
        data2 = b"production artifact payload two"
        key1, dig1, size1 = _make_object(data1)
        key2, dig2, size2 = _make_object(data2)

        _write_store_object(self.store_root, key1, data1)
        _write_store_object(self.store_root, key2, data2)

        # 1. JSON envelope format
        envelope = {
            "format": "lab-artifact-inventory-1",
            "artifacts": [
                {"object_key": key1, "sha256": dig1, "size_bytes": size1, "state": "committed"},
                {"object_key": key2, "sha256": dig2, "size_bytes": size2, "state": "committed"},
            ],
        }
        report = verify_restored_inventory(json.dumps(envelope), self.store_root)
        self.assertIsInstance(report, VerificationReport)
        self.assertEqual(report.verified_count, 2)
        self.assertEqual(report.total_bytes, size1 + size2)
        self.assertEqual(set(report.verified_keys), {key1, key2})

        # 2. JSON array format
        array_data = [
            {"object_key": key1, "size_bytes": size1},
            {"object_key": key2, "size_bytes": size2},
        ]
        report_arr = verify_restored_inventory(json.dumps(array_data), self.store_root)
        self.assertEqual(report_arr.verified_count, 2)

        # 3. JSONL format
        jsonl_data = f'{{"object_key": "{key1}", "size_bytes": {size1}}}\n{{"object_key": "{key2}", "size_bytes": {size2}}}\n'
        report_jsonl = verify_restored_inventory(jsonl_data, self.store_root)
        self.assertEqual(report_jsonl.verified_count, 2)

        # 4. Inventory file on disk
        inv_file = Path(self.tmp_dir.name) / "inventory.jsonl"
        inv_file.write_text(jsonl_data, encoding="utf-8")
        report_file = verify_restored_inventory(inv_file, self.store_root)
        self.assertEqual(report_file.verified_count, 2)

        # 5. Iterable of InventoryItem
        items = [
            InventoryItem(object_key=key1, sha256=dig1, size_bytes=size1),
            InventoryItem(object_key=key2, sha256=dig2, size_bytes=size2),
        ]
        report_iter = verify_restored_inventory(items, self.store_root)
        self.assertEqual(report_iter.verified_count, 2)

    def test_verify_empty_inventory(self) -> None:
        # allow_empty=False (default) fails closed
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([], self.store_root)
        self.assertIn("inventory must not be empty", str(ctx.exception))

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory("[]", self.store_root)
        self.assertIn("inventory must not be empty", str(ctx.exception))

        # allow_empty=True succeeds
        rep = verify_restored_inventory([], self.store_root, allow_empty=True)
        self.assertEqual(rep.verified_count, 0)
        self.assertEqual(rep.total_bytes, 0)

    def test_untrusted_inventory_duplicate_json_keys_rejected(self) -> None:
        key, _, size = _make_object(b"content")
        bad_json = f'{{"object_key": "{key}", "object_key": "{key}", "size_bytes": {size}}}'
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(bad_json, self.store_root)
        self.assertIn("duplicate JSON field", str(ctx.exception))

    def test_untrusted_inventory_duplicate_object_keys_rejected(self) -> None:
        key, dig, size = _make_object(b"duplicate key test")
        inv = [
            {"object_key": key, "sha256": dig, "size_bytes": size},
            {"object_key": key, "sha256": dig, "size_bytes": size},
        ]
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(inv, self.store_root)
        self.assertIn("duplicate object_key", str(ctx.exception))

    def test_untrusted_inventory_malformed_object_keys_rejected(self) -> None:
        _, valid_dig, _ = _make_object(b"test")
        malformed_keys = [
            f"sha256/../../{valid_dig}",
            f"sha256/00/{valid_dig}../extra",
            f"sha256/zz/{valid_dig}",
            f"sha256/00/{valid_dig[:-1]}z",
            f"md5/00/{valid_dig}",
            f"sha256/ff/{valid_dig}",  # prefix does not match digest[:2]
            f"sha256/{valid_dig[:2]}/{valid_dig[:63]}",  # 63 chars
            f"sha256/{valid_dig[:2]}/{valid_dig}0",  # 65 chars
            "plain_filename.pdf",
            f"sha256/{valid_dig[:2]}/{valid_dig}\x00extra",
        ]
        for key in malformed_keys:
            with self.subTest(key=key):
                inv = [{"object_key": key, "size_bytes": 10}]
                with self.assertRaises(InventoryVerificationError):
                    verify_restored_inventory(inv, self.store_root)

    def test_untrusted_inventory_invalid_size_and_state(self) -> None:
        key, dig, size = _make_object(b"test")

        # Negative size
        with self.assertRaises(InventoryVerificationError):
            verify_restored_inventory([{"object_key": key, "size_bytes": -1}], self.store_root)

        # Boolean size
        with self.assertRaises(InventoryVerificationError):
            verify_restored_inventory([{"object_key": key, "size_bytes": True}], self.store_root)

        # Float size
        with self.assertRaises(InventoryVerificationError):
            verify_restored_inventory([{"object_key": key, "size_bytes": 12.3}], self.store_root)

        # Non-committed state
        for state in ("staged", "tombstoned", "deleted", "active"):
            with self.subTest(state=state):
                inv = [{"object_key": key, "size_bytes": size, "state": state}]
                with self.assertRaises(InventoryVerificationError) as ctx:
                    verify_restored_inventory(inv, self.store_root)
                self.assertIn("must be 'committed'", str(ctx.exception))

        # Unrecognized field
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "size_bytes": size, "unexpected_payload": 123}], self.store_root)
        self.assertIn("unrecognized field", str(ctx.exception))

    def test_state_null_explicitly_supplied_is_rejected(self) -> None:
        key, dig, size = _make_object(b"test state null")
        # Python None
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "size_bytes": size, "state": None}], self.store_root)
        self.assertIn("must be 'committed'", str(ctx.exception))

        # JSON null
        json_null = f'[{{"object_key": "{key}", "size_bytes": {size}, "state": null}}]'
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(json_null, self.store_root)
        self.assertIn("must be 'committed'", str(ctx.exception))

    def test_contradictory_aliases_rejected(self) -> None:
        key1, dig1, size1 = _make_object(b"one")
        key2, dig2, size2 = _make_object(b"two")

        # Contradictory key vs object_key
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key1, "key": key2, "size_bytes": 10}], self.store_root)
        self.assertIn("contradictory key aliases", str(ctx.exception))

        # Contradictory size vs size_bytes
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key1, "size_bytes": 10, "size": 20}], self.store_root)
        self.assertIn("contradictory size aliases", str(ctx.exception))

        # Matching aliases succeed
        _write_store_object(self.store_root, key1, b"one")
        rep = verify_restored_inventory([{"object_key": key1, "key": key1, "size_bytes": size1, "size": size1}], self.store_root)
        self.assertEqual(rep.verified_count, 1)

    def test_nonexistent_and_directory_path_input_clean_error(self) -> None:
        nonexistent_path = Path(self.tmp_dir.name) / "does_not_exist.json"
        # Nonexistent Path
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(nonexistent_path, self.store_root)
        self.assertIn("inventory file not found", str(ctx.exception))

        # Nonexistent string path
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(str(nonexistent_path), self.store_root)
        self.assertIn("inventory file not found", str(ctx.exception))

        # Directory Path
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(Path(self.tmp_dir.name), self.store_root)
        self.assertIn("is not a regular file", str(ctx.exception))

    def test_bounded_iteration_infinite_generator(self) -> None:
        key, dig, size = _make_object(b"item")

        def infinite_generator():
            count = 0
            while True:
                count += 1
                yield {"object_key": f"sha256/{count:02x}/{count:064x}", "size_bytes": 10}

        # Must fail closed at max_items without hanging or exhausting memory
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(infinite_generator(), self.store_root, max_items=5)
        self.assertIn("exceeds maximum 5", str(ctx.exception))

    def test_bounds_enforcement(self) -> None:
        key, dig, size = _make_object(b"test bytes")
        _write_store_object(self.store_root, key, b"test bytes")

        # Exceeds max_items
        inv = [{"object_key": f"sha256/{i:02x}/{i:064x}", "size_bytes": 1} for i in range(5)]
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(inv, self.store_root, max_items=3)
        self.assertIn("exceeds maximum 3", str(ctx.exception))

        # Exceeds max_file_bytes
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "size_bytes": size}], self.store_root, max_file_bytes=5)
        self.assertIn("exceeds limit 5", str(ctx.exception))

        # Exceeds max_total_bytes (pre-read check)
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "size_bytes": size}], self.store_root, max_total_bytes=5)
        self.assertIn("exceeds limit 5", str(ctx.exception))

        # Aggregate total exceeding bound across multiple items (pre-read check)
        inv_multi = [
            {"object_key": key, "size_bytes": 6},
            {"object_key": f"sha256/00/{'0'*64}", "size_bytes": 6},
        ]
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(inv_multi, self.store_root, max_total_bytes=10)
        self.assertIn("exceeds limit 10", str(ctx.exception))

        # Exceeds max_inventory_bytes
        large_json = json.dumps([{"object_key": key, "size_bytes": size}])
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory(large_json, self.store_root, max_inventory_bytes=10)
        self.assertIn("exceeds limit 10", str(ctx.exception))

    def test_missing_artifact_fails_closed(self) -> None:
        key, _, size = _make_object(b"nonexistent artifact")
        # Ensure sha256 directory exists
        (self.store_root / "sha256").mkdir()
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "size_bytes": size}], self.store_root)
        self.assertIn("not found", str(ctx.exception))

    def test_hash_mismatch_fails_closed(self) -> None:
        key, dig, size = _make_object(b"authentic payload")
        # Write corrupted content with exact same length
        corrupted_data = b"tampered! payload"
        self.assertEqual(len(corrupted_data), size)
        _write_store_object(self.store_root, key, corrupted_data)

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
        self.assertIn("hash mismatch", str(ctx.exception))

    def test_size_mismatch_fails_closed(self) -> None:
        key, dig, _ = _make_object(b"authentic payload")
        # Write shorter content
        _write_store_object(self.store_root, key, b"short")
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": 100}], self.store_root)
        self.assertIn("size mismatch", str(ctx.exception))

    def test_symlink_in_object_file_fails_closed(self) -> None:
        target_file = Path(self.tmp_dir.name) / "secret.txt"
        target_file.write_bytes(b"secret outside data")

        key, dig, size = _make_object(b"secret outside data")
        obj_path = self.store_root.joinpath(*key.split("/"))
        obj_path.parent.mkdir(parents=True, exist_ok=True)
        # Create symlink pointing to outside file
        obj_path.symlink_to(target_file)

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
        self.assertIn("symlink", str(ctx.exception))

    def test_symlink_in_prefix_directory_fails_closed(self) -> None:
        target_dir = Path(self.tmp_dir.name) / "outside_prefix"
        target_dir.mkdir()
        data = b"payload inside redirected prefix"
        key, dig, size = _make_object(data)
        (target_dir / dig).write_bytes(data)

        sha_dir = self.store_root / "sha256"
        sha_dir.mkdir(parents=True, exist_ok=True)
        prefix_link = sha_dir / dig[:2]
        prefix_link.symlink_to(target_dir, target_is_directory=True)

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
        self.assertIn("symlink", str(ctx.exception))

    def test_symlink_in_sha256_directory_fails_closed(self) -> None:
        target_dir = Path(self.tmp_dir.name) / "outside_sha256"
        target_dir.mkdir()
        data = b"payload inside redirected sha256"
        key, dig, size = _make_object(data)
        _write_store_object(target_dir.parent / "outside_store", key, data)

        sha_link = self.store_root / "sha256"
        sha_link.symlink_to(target_dir, target_is_directory=True)

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
        self.assertIn("symlink", str(ctx.exception))

    def test_symlink_in_store_root_fails_closed(self) -> None:
        real_root = Path(self.tmp_dir.name) / "real_root"
        real_root.mkdir()
        data = b"payload in real root"
        key, dig, size = _make_object(data)
        _write_store_object(real_root, key, data)

        symlink_root = Path(self.tmp_dir.name) / "symlink_root"
        symlink_root.symlink_to(real_root, target_is_directory=True)

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], symlink_root)
        self.assertIn("symlink", str(ctx.exception))

    def test_symlink_in_ancestor_directory_fails_closed(self) -> None:
        # Create /tmp/.../base/link -> /tmp/.../target/artifacts
        base_dir = Path(self.tmp_dir.name) / "base"
        base_dir.mkdir()
        target_dir = Path(self.tmp_dir.name) / "target"
        target_dir.mkdir()
        real_store = target_dir / "artifacts"
        real_store.mkdir()

        data = b"payload with ancestor symlink"
        key, dig, size = _make_object(data)
        _write_store_object(real_store, key, data)

        ancestor_link = base_dir / "link"
        ancestor_link.symlink_to(target_dir, target_is_directory=True)

        escaped_root = ancestor_link / "artifacts"
        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], escaped_root)
        self.assertIn("symlink", str(ctx.exception))

    def test_non_regular_file_fifo_fails_closed(self) -> None:
        # Create a FIFO named pipe at the object location
        data = b"dummy"
        key, dig, size = _make_object(data)
        fifo_path = self.store_root.joinpath(*key.split("/"))
        fifo_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(fifo_path)

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
        self.assertIn("not a regular file", str(ctx.exception))

    def test_non_regular_file_directory_fails_closed(self) -> None:
        data = b"dummy"
        key, dig, size = _make_object(data)
        dir_obj = self.store_root.joinpath(*key.split("/"))
        dir_obj.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(InventoryVerificationError) as ctx:
            verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
        self.assertIn("not a regular file", str(ctx.exception))

    def test_prefix_directory_renamed_race_fails_closed(self) -> None:
        """Regression test for host-reproduced finding:

        Rename opened prefix directory and create empty replacement immediately after
        _open_dir_no_follow returns; verify_restored_inventory must fail closed because
        canonical path reachability/identity verification detects the substitution.
        """
        data = b"race payload"
        key, dig, size = _make_object(data)
        _write_store_object(self.store_root, key, data)

        original_open_dir = ri._open_dir_no_follow
        prefix = dig[:2]
        intercepted = False

        def racing_open_dir(parent_fd: int, name: str, desc: str) -> int:
            nonlocal intercepted
            fd = original_open_dir(parent_fd, name, desc)
            if name == prefix and not intercepted and not desc.startswith("canonical"):
                intercepted = True
                # Move the real prefix directory away and substitute with an empty directory
                real_prefix_path = self.store_root / "sha256" / prefix
                moved_prefix_path = self.store_root / "sha256" / (prefix + "_moved")
                os.rename(real_prefix_path, moved_prefix_path)
                os.mkdir(real_prefix_path)
            return fd

        with mock.patch("tools.governance.restore_inventory._open_dir_no_follow", side_effect=racing_open_dir):
            with self.assertRaises(InventoryVerificationError) as ctx:
                verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
            self.assertTrue(intercepted)
            self.assertTrue(
                "canonical prefix directory identity changed" in str(ctx.exception)
                or "canonical artifact file missing after read" in str(ctx.exception)
            )

    def test_canonical_object_file_deleted_or_swapped_fails_closed(self) -> None:
        """Regression test: object file removed or modified after descriptor open."""
        data = b"swapped file test"
        key, dig, size = _make_object(data)
        obj_path = _write_store_object(self.store_root, key, data)

        original_open = os.open
        intercepted = False

        def racing_open(path, flags, *args, **kwargs):
            nonlocal intercepted
            fd = original_open(path, flags, *args, **kwargs)
            if path == dig and not intercepted:
                intercepted = True
                # Replace file with empty file having different inode
                obj_path.unlink()
                obj_path.write_bytes(b"")
            return fd

        with mock.patch("os.open", side_effect=racing_open):
            with self.assertRaises(InventoryVerificationError):
                verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
            self.assertTrue(intercepted)

    def test_read_only_invariant_no_mutations_or_leftovers(self) -> None:
        data = b"content to verify no side effects"
        key, dig, size = _make_object(data)
        obj_path = _write_store_object(self.store_root, key, data)

        before_stat = obj_path.stat()
        before_entries = list(self.store_root.rglob("*"))

        report = verify_restored_inventory([{"object_key": key, "sha256": dig, "size_bytes": size}], self.store_root)
        self.assertEqual(report.verified_count, 1)

        after_stat = obj_path.stat()
        after_entries = list(self.store_root.rglob("*"))

        # Inode, size, and mtime unchanged
        self.assertEqual(before_stat.st_ino, after_stat.st_ino)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        # Exactly same set of files/directories (no temporary files created or deleted)
        self.assertEqual(before_entries, after_entries)

    def test_cli_operator_usage(self) -> None:
        data = b"operator usage test content"
        key, dig, size = _make_object(data)
        _write_store_object(self.store_root, key, data)

        inv_file = Path(self.tmp_dir.name) / "test_inv.json"
        inv_file.write_text(json.dumps([{"object_key": key, "size_bytes": size}]), encoding="utf-8")

        # 1. Success with flags and --json
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch("sys.stdout", stdout_buf), mock.patch("sys.stderr", stderr_buf):
            ret = main(["--inventory", str(inv_file), "--store-root", str(self.store_root), "--json"])
        self.assertEqual(ret, 0)
        report_data = json.loads(stdout_buf.getvalue())
        self.assertEqual(report_data["status"], "verified")
        self.assertEqual(report_data["verified_count"], 1)

        # 2. Success with positional arguments
        stdout_pos = io.StringIO()
        with mock.patch("sys.stdout", stdout_pos):
            ret_pos = main([str(inv_file), str(self.store_root)])
        self.assertEqual(ret_pos, 0)
        self.assertIn("Verified 1 committed artifact(s)", stdout_pos.getvalue())

        # 3. Missing arguments exits with code 2
        stderr_usage = io.StringIO()
        with mock.patch("sys.stderr", stderr_usage):
            ret_usage = main([])
        self.assertEqual(ret_usage, 2)

        # 4. Verification failure exits with code 1
        corrupt_inv = Path(self.tmp_dir.name) / "bad_inv.json"
        corrupt_inv.write_text(json.dumps([{"object_key": key, "size_bytes": 99999}]), encoding="utf-8")
        stderr_fail = io.StringIO()
        with mock.patch("sys.stderr", stderr_fail):
            ret_fail = main([str(corrupt_inv), str(self.store_root)])
        self.assertEqual(ret_fail, 1)
        self.assertIn("verification failed", stderr_fail.getvalue())


if __name__ == "__main__":
    unittest.main()
