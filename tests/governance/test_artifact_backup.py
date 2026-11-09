import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.governance.artifact_backup import BackupError, create_bundle, restore_bundle


def store(root, data):
    digest = hashlib.sha256(data).hexdigest()
    key = f'sha256/{digest[:2]}/{digest}'
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


class ArtifactBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'src'
        self.source.mkdir()
        self.keys = [store(self.source, b'hello'), store(self.source, b'world')]
        self.destination = self.base / 'dst'

    def test_roundtrip_and_deterministic_order(self):
        bundle = create_bundle(self.source, self.keys)
        self.assertEqual(bundle, create_bundle(self.source, self.keys[::-1]))
        self.assertEqual(restore_bundle(bundle, self.destination), 2)
        self.assertEqual(create_bundle(self.destination, self.keys), bundle)

    def test_corrupt_source_and_missing_source(self):
        (self.source / self.keys[0]).write_bytes(b'bad')
        with self.assertRaises(BackupError): create_bundle(self.source, self.keys)
        (self.source / self.keys[0]).unlink()
        with self.assertRaises(BackupError): create_bundle(self.source, self.keys)

    def test_symlinked_object_and_parent_rejected(self):
        target = self.source / self.keys[0]
        target.unlink()
        target.symlink_to(self.source / self.keys[1])
        with self.assertRaises(BackupError): create_bundle(self.source, [self.keys[0]])
        target.unlink()
        prefix = target.parent
        prefix.rmdir()
        prefix.symlink_to(self.source / self.keys[1].rsplit('/', 1)[0], target_is_directory=True)
        with self.assertRaises(BackupError): create_bundle(self.source, [self.keys[0]])

    def test_malformed_corrupt_or_duplicate_bundle_never_creates_destination(self):
        bundle = create_bundle(self.source, self.keys)
        doc = json.loads(bundle)
        doc['objects'][0]['data'] = base64.b64encode(b'wrong').decode()
        candidates = [json.dumps(doc).encode(), b'{"format":1,"format":2}', b'{', bundle.replace(self.keys[0].encode(), b'../escape')]
        for candidate in candidates:
            with self.subTest(candidate=candidate[:40]):
                with self.assertRaises(BackupError): restore_bundle(candidate, self.destination)
                self.assertFalse(self.destination.exists())
        doc = json.loads(bundle)
        doc['objects'].append(doc['objects'][0])
        with self.assertRaises(BackupError): restore_bundle(json.dumps(doc).encode(), self.destination)
        self.assertFalse(self.destination.exists())

    def test_oversized_json_integer_is_rejected_without_destination(self):
        payload = b'{"format":' + b'9' * 5000 + b',"objects":[]}'
        with self.assertRaisesRegex(BackupError, "invalid bundle JSON"):
            restore_bundle(payload, self.destination)
        self.assertFalse(self.destination.exists())
        with self.assertRaisesRegex(BackupError, "duplicate JSON field"):
            restore_bundle(b'{"format":1,"format":2}', self.destination)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_is_never_replaced(self):
        self.destination.mkdir()
        (self.destination / 'keep').write_text('keep')
        with self.assertRaises(BackupError): restore_bundle(create_bundle(self.source, self.keys), self.destination)
        self.assertEqual((self.destination / 'keep').read_text(), 'keep')

    def test_invalid_keys_and_limits(self):
        for keys in ([self.keys[0], self.keys[0]], ['../outside'], [self.keys[0]] * 1001):
            with self.assertRaises(BackupError): create_bundle(self.source, keys)
        with patch('tools.governance.artifact_backup.MAX_TOTAL_BYTES', 4):
            with self.assertRaises(BackupError): create_bundle(self.source, self.keys)
            bundle = create_bundle(self.source, [])
            self.assertEqual(restore_bundle(bundle, self.destination), 0)

    def test_failed_write_removes_destination(self):
        bundle = create_bundle(self.source, self.keys)
        with patch('pathlib.Path.open', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): restore_bundle(bundle, self.destination)
        self.assertFalse(self.destination.exists())
