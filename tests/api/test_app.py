import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from lab_api import __main__ as cli
from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.readiness import CheckResult, artifact_storage_check, database_check

ENV = {
    "LAB_ENV": "test",
    "LAB_PROVIDER_MODE": "mock",
    # Port 1 on loopback: nothing listens, so the real driver fails fast.
    "LAB_DATABASE_URL": "postgresql://lab:s3cret-pw@127.0.0.1:1/lab",
    "LAB_ARTIFACT_ROOT": "/nonexistent-lab-artifacts",
    "LAB_DB_CONNECT_TIMEOUT_S": "2",
}


def settings(**env):
    with mock.patch.dict(os.environ, {**ENV, **env}, clear=True):
        return load_settings()


class EndpointTests(unittest.TestCase):
    def client(self, checks):
        return TestClient(create_app(settings(), checks=checks))

    def test_healthz_is_liveness_only(self):
        r = self.client({"database": lambda: CheckResult(False, "down")}).get("/healthz")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))

    def test_readyz_all_ok(self):
        r = self.client({"a": lambda: CheckResult(True, "fine")}).get("/readyz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ready")
        self.assertEqual(r.json()["provider_mode"], "mock")

    def test_readyz_one_failure_is_503(self):
        r = self.client({"a": lambda: CheckResult(True, "fine"),
                         "b": lambda: CheckResult(False, "broken")}).get("/readyz")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "not_ready")
        self.assertFalse(r.json()["checks"]["b"]["ok"])

    def test_crashing_check_is_failure_not_pass(self):
        def boom():
            raise RuntimeError("s3cret-pw")
        r = self.client({"a": boom}).get("/readyz")
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("s3cret-pw", r.text)

    def test_no_checks_is_not_ready(self):
        self.assertEqual(self.client({}).get("/readyz").status_code, 503)

    def test_default_checks_with_unreachable_db_and_missing_root(self):
        r = TestClient(create_app(settings())).get("/readyz")
        self.assertEqual(r.status_code, 503)
        body = r.json()
        self.assertFalse(body["checks"]["database"]["ok"])
        self.assertFalse(body["checks"]["artifact_storage"]["ok"])
        self.assertNotIn("s3cret-pw", r.text)
        self.assertNotIn("127.0.0.1", r.text)


class CheckTests(unittest.TestCase):
    def test_unreachable_database_fails(self):
        res = database_check(settings())()
        self.assertFalse(res.ok)
        self.assertIn("connection failed", res.detail)

    def test_artifact_root_writable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(artifact_storage_check(Path(d))().ok)
            self.assertEqual(os.listdir(d), [])

    def test_artifact_root_is_file(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertFalse(artifact_storage_check(Path(f.name))().ok)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_artifact_root_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o500)
            try:
                res = artifact_storage_check(Path(d))()
            finally:
                os.chmod(d, 0o700)
            self.assertFalse(res.ok)


class CliTests(unittest.TestCase):
    def test_check_config(self):
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(cli.main(["check-config"]), 0)
        with mock.patch.dict(os.environ, {**ENV, "LAB_PROVIDER_MODE": "real"}, clear=True):
            self.assertEqual(cli.main(["check-config"]), 1)
        self.assertEqual(cli.main([]), 2)


if __name__ == "__main__":
    unittest.main()
