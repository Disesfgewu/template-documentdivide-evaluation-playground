import contextlib
import io
import os
import unittest
from unittest import mock

from lab_api.__main__ import main as cli_main
from lab_api.config import ConfigError, Environment, ProviderMode, load_settings
from lab_api.main import create_app

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
        self.assertFalse(s.trust_remote_traceparent)

    def test_trust_remote_traceparent_strict_boolean(self):
        for raw, expected in (("true", True), ("TRUE", True), ("1", True), ("false", False), ("0", False)):
            with self.subTest(raw=raw):
                self.assertIs(load({**GOOD, "LAB_TRUST_REMOTE_TRACEPARENT": raw}).trust_remote_traceparent, expected)
        for bad in ("yes", "no", "on", "off", f"invalid-{SECRET}"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as cm:
                load({**GOOD, "LAB_TRUST_REMOTE_TRACEPARENT": bad})
            self.assertEqual(
                str(cm.exception),
                "invalid LAB configuration: LAB_TRUST_REMOTE_TRACEPARENT: must be a boolean",
            )
            self.assertNotIn(SECRET, str(cm.exception))

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


# Synthetic credentials only. Each URL makes urllib raise an error whose text
# quotes the netloc (and hence the password) or the port.
SECRET = "synthetic-secret"
HOST = "db-host-zz"
MALFORMED_URLS = {
    "nfkc_netloc": f"postgresql://lab:{SECRET}@{HOST}\uff0fx/lab",
    "nfkc_netloc_at": f"postgresql://lab:{SECRET}\uff20{HOST}/lab",
    "unclosed_ipv6": f"postgresql://lab:{SECRET}@[::1/lab",
    "ipv6_garbage": f"postgresql://lab:{SECRET}@[{HOST}]/lab",
    "bad_port": f"postgresql://lab:{SECRET}@{HOST}:99999999/lab",
    "non_numeric_port": f"postgresql://lab:{SECRET}@{HOST}:{SECRET}/lab",
}


class MalformedUrlRedactionTests(unittest.TestCase):
    """R19/T69 sub-scenario: no config error path echoes the DB URL."""

    def assert_clean(self, text, url):
        for leaked in (SECRET, HOST, url):
            self.assertNotIn(leaked, text)

    def run_cli(self, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(["check-config"])
        return code, out.getvalue(), err.getvalue()

    def test_config_error_cli_and_app_factory_never_echo_url(self):
        for name, url in MALFORMED_URLS.items():
            env = {**GOOD, "LAB_DATABASE_URL": url}
            with self.subTest(case=name):
                with self.assertRaises(ConfigError) as cm:
                    load(env)
                msg = str(cm.exception)
                self.assert_clean(msg, url)
                self.assertIn("LAB_DATABASE_URL", msg)
                self.assertIsNone(cm.exception.__cause__)
                self.assertTrue(cm.exception.__suppress_context__)

                code, out, err = self.run_cli(env)
                self.assertEqual(code, 1)
                self.assertIn("LAB_DATABASE_URL", err)
                self.assert_clean(out + err, url)

                with mock.patch.dict(os.environ, env, clear=True), \
                        self.assertRaises(ConfigError) as app_cm:
                    create_app()
                self.assert_clean(str(app_cm.exception), url)

    def test_valid_url_still_accepted_by_cli(self):
        code, out, err = self.run_cli(GOOD)
        self.assertEqual(code, 0, err)
        self.assertIn("config ok", out)
        self.assertNotIn("s3cret-pw", out + err)

    def test_missing_and_policy_errors_keep_field_names_without_values(self):
        env = {k: v for k, v in GOOD.items() if k != "LAB_ENV"}
        env["LAB_PROVIDER_MODE"] = f"fake-{SECRET}"
        env["LAB_DB_CONNECT_TIMEOUT_S"] = SECRET
        code, out, err = self.run_cli(env)
        self.assertEqual(code, 1)
        for field in ("LAB_ENV", "LAB_PROVIDER_MODE", "LAB_DB_CONNECT_TIMEOUT_S"):
            self.assertIn(field, err)
        self.assertNotIn(SECRET, out + err)
        self.assertNotIn("s3cret-pw", out + err)

        code, out, err = self.run_cli({**GOOD, "LAB_ENV": "prod"})
        self.assertEqual(code, 1)
        self.assertIn("env=prod must not run with provider_mode=mock", err)
        self.assertNotIn("s3cret-pw", out + err)

    def test_unlisted_validator_text_is_not_surfaced(self):
        # A future validator whose message interpolates input must not leak:
        # only allowlisted fixed messages pass through.
        from lab_api import config as cfg

        err = {"type": "value_error", "loc": ("database_url",),
               "msg": f"Value error, bad url {MALFORMED_URLS['bad_port']}",
               "ctx": {"error": ValueError(f"bad url {MALFORMED_URLS['bad_port']}")}}
        self.assertEqual(cfg._safe_message(err), "is invalid")
        self.assertEqual(cfg._safe_message({"type": "url_parsing", "loc": ("x",), "msg": SECRET}),
                         "is invalid")


if __name__ == "__main__":
    unittest.main()
