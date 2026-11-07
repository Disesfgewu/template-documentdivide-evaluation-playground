"""Unit and security boundary tests for trusted authentication (SPEC 16.4, R01)."""
from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from pydantic import SecretStr

from lab_api.auth import (
    AuthError,
    get_signing_secret,
    issue_actor_token,
    verify_actor_token,
)
from lab_api.config import Settings, load_settings

ENV = {
    "LAB_ENV": "test",
    "LAB_PROVIDER_MODE": "mock",
    "LAB_DATABASE_URL": "postgresql://lab:s3cret-pw@127.0.0.1:1/lab",
    "LAB_ARTIFACT_ROOT": "/nonexistent-lab-artifacts",
    "LAB_AUTH_SECRET": "independently-provisioned-secret-key-12345",
}


class AuthSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.settings = load_settings()
        self.secret = get_signing_secret(self.settings)

    def test_auth_secret_never_falls_back_to_database_url(self) -> None:
        env_no_auth = {k: v for k, v in ENV.items() if k != "LAB_AUTH_SECRET"}
        with mock.patch.dict(os.environ, env_no_auth, clear=True):
            settings_no_auth = load_settings()
        with self.assertRaises(AuthError) as ctx:
            get_signing_secret(settings_no_auth)
        self.assertEqual(ctx.exception.code, "auth_not_configured")

    def test_issue_and_verify_valid_token(self) -> None:
        actor_id = uuid.uuid4()
        token = issue_actor_token(actor_id, self.secret)
        self.assertTrue(token.startswith("lab_act_"))
        verified_actor = verify_actor_token(token, self.secret)
        self.assertEqual(verified_actor, actor_id)

    def test_wrong_secret_fails_verification(self) -> None:
        actor_id = uuid.uuid4()
        token = issue_actor_token(actor_id, self.secret)
        self.assertIsNone(verify_actor_token(token, b"wrong-secret-key-67890"))

    def test_tampered_actor_or_signature_fails_verification(self) -> None:
        actor_id = uuid.uuid4()
        token = issue_actor_token(actor_id, self.secret)
        parts = token.split("_")
        # Tamper actor UUID
        tampered_actor = issue_actor_token(uuid.uuid4(), self.secret)
        other_uuid = tampered_actor.split("_")[2]
        bad_token = f"lab_act_{other_uuid}_{parts[3]}"
        self.assertIsNone(verify_actor_token(bad_token, self.secret))
        # Tamper signature
        bad_sig_token = f"lab_act_{parts[2]}_{'0' * 64}"
        self.assertIsNone(verify_actor_token(bad_sig_token, self.secret))

    def test_malformed_non_ascii_signatures_fail_safely(self) -> None:
        actor_id = uuid.uuid4()
        # Non-ASCII unicode signature characters
        unicode_token = f"lab_act_{actor_id}_{'🔥' * 32}"
        self.assertIsNone(verify_actor_token(unicode_token, self.secret))

        # Malformed format strings
        for bad in ("", "random-string", "Bearer xxx", "lab_act_", f"lab_act_{actor_id}", 12345):
            self.assertIsNone(verify_actor_token(bad, self.secret))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
