import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from lab_parser import __main__ as cli
from lab_parser import adapters
from lab_parser.config import ConfigError, load_settings
from lab_parser.main import create_app


def settings(root):
    env = {"LAB_PARSER_ENV": "test", "LAB_PARSER_ARTIFACT_ROOT": str(root)}
    with mock.patch.dict(os.environ, env, clear=True):
        return load_settings()


class ConfigTests(unittest.TestCase):
    def test_required_and_absolute(self):
        for env in ({}, {"LAB_PARSER_ENV": "dev"}, {"LAB_PARSER_ARTIFACT_ROOT": "/x"},
                    {"LAB_PARSER_ENV": "dev", "LAB_PARSER_ARTIFACT_ROOT": "rel/path"},
                    {"LAB_PARSER_ENV": "staging", "LAB_PARSER_ARTIFACT_ROOT": "/x"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ConfigError):
                    load_settings()

    def test_cli(self):
        env = {"LAB_PARSER_ENV": "dev", "LAB_PARSER_ARTIFACT_ROOT": "/x"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(cli.main(["check-config"]), 0)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.main(["check-config"]), 1)
        self.assertEqual(cli.main([]), 2)


class EndpointTests(unittest.TestCase):
    def test_health_and_ready(self):
        with tempfile.TemporaryDirectory() as d:
            c = TestClient(create_app(settings(d)))
            self.assertEqual(c.get("/healthz").json(), {"status": "ok"})
            r = c.get("/readyz")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(os.listdir(d), [])

    def test_not_ready_without_storage(self):
        c = TestClient(create_app(settings("/nonexistent-lab-parser-root")))
        self.assertEqual(c.get("/healthz").status_code, 200)
        r = c.get("/readyz")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "not_ready")

    def test_no_parse_endpoint_yet(self):
        with tempfile.TemporaryDirectory() as d:
            c = TestClient(create_app(settings(d)))
            self.assertEqual(c.post("/v1/parse").status_code, 404)

    def test_capabilities_never_claim_ready_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            body = TestClient(create_app(settings(d))).get("/v1/capabilities").json()
        names = [p["name"] for p in body["parsers"]]
        self.assertEqual(names, ["unstructured", "mineru"])
        for p in body["parsers"]:
            self.assertEqual(p["adapter"], "not_implemented")


class AdapterStatusTests(unittest.TestCase):
    def test_missing_backend(self):
        b = adapters.Backend("x", "lab_definitely_missing_module", "lab-missing")
        self.assertEqual(adapters.backend_status(b)["installed"], False)

    def test_installed_backend_reports_real_version(self):
        b = adapters.Backend("fastapi", "fastapi", "fastapi")
        s = adapters.backend_status(b)
        self.assertTrue(s["installed"])
        import fastapi
        self.assertEqual(s["version"], fastapi.__version__)
        self.assertEqual(s["adapter"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
