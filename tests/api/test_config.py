import os
import unittest
from unittest import mock

from lab_api.config import ConfigError, Environment, ProviderMode, load_settings

GOOD = {
    "LAB_ENV": "dev",
    "LAB_PROVIDER_MODE": "mock",
    "LAB_DATABASE_URL": "postgresql://lab:s3cret-pw@db.local:5432/lab",
    "LAB_ARTIFACT_ROOT": "/var/lib/lab/artifacts",
}


def load(env):
    with mock.patch.dict(os.environ, env, clear=True):
        return load_settings()


class ConfigTests(unittest.TestCase):
    def test_valid_dev_mock(self):
        s = load(GOOD)
        self.assertIs(s.env, Environment.dev)
        self.assertIs(s.provider_mode, ProviderMode.mock)
        self.assertEqual(s.db_connect_timeout_s, 3.0)

    def test_every_required_var_is_required(self):
        for key in GOOD:
            env = {k: v for k, v in GOOD.items() if k != key}
            with self.subTest(missing=key), self.assertRaises(ConfigError) as cm:
                load(env)
            self.assertIn(key, str(cm.exception))

    def test_sqlite_or_other_scheme_rejected(self):
        for url in ("sqlite:///lab.db", "mysql://u:p@h/db", "postgresql://h", "postgresql:///lab"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                load({**GOOD, "LAB_DATABASE_URL": url})

    def test_relative_artifact_root_rejected(self):
        with self.assertRaises(ConfigError):
            load({**GOOD, "LAB_ARTIFACT_ROOT": "artifacts"})

    def test_real_provider_refused_until_adapter_exists(self):
        with self.assertRaises(ConfigError) as cm:
            load({**GOOD, "LAB_PROVIDER_MODE": "real"})
        self.assertIn("T84", str(cm.exception))

    def test_prod_with_mock_provider_refused(self):
        with self.assertRaises(ConfigError):
            load({**GOOD, "LAB_ENV": "prod"})

    def test_unknown_enum_and_bad_timeout_rejected(self):
        for key, val in (("LAB_ENV", "staging"), ("LAB_PROVIDER_MODE", "fake"),
                         ("LAB_DB_CONNECT_TIMEOUT_S", "0"), ("LAB_DB_CONNECT_TIMEOUT_S", "31")):
            with self.subTest(key=key, val=val), self.assertRaises(ConfigError):
                load({**GOOD, key: val})

    def test_errors_and_repr_never_contain_secret(self):
        with self.assertRaises(ConfigError) as cm:
            load({**GOOD, "LAB_DATABASE_URL": "mysql://lab:s3cret-pw@h/db"})
        self.assertNotIn("s3cret-pw", str(cm.exception))
        s = load(GOOD)
        self.assertNotIn("s3cret-pw", repr(s))
        self.assertNotIn("s3cret-pw", str(s.model_dump()))


if __name__ == "__main__":
    unittest.main()
