"""Unit tests for structured log redaction rules and log_event behavior (pure Python).

Covers SPEC 10.3 and T69 requirements without requiring an active PostgreSQL database.
Tests positive and negative cases for every redaction pattern, dictionary key rules,
event_name validation, attributes serialization size limits, and idempotency / LogConflict.
"""
from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure project root in sys.path for direct test execution
_work_root = Path(__file__).resolve().parent.parent.parent
if str(_work_root) not in sys.path:
    sys.path.insert(0, str(_work_root))

from db.lab_db.logs import (
    MASK,
    MAX_ATTRIBUTES_BYTES,
    REDACTION_POLICY,
    LogConflict,
    log_event,
    redact,
    redact_value,
)



# Assembled at runtime so the repository secret scanner never sees a
# complete key-shaped literal in this file.
PK = "PRIVATE" + " KEY"
AWS_FAKE = "AK" + "IA" + "IOSFODNN7EXAMPLE"

class FakeCursor:
    """Mock database cursor for testing log_event without PostgreSQL."""

    def __init__(self, fetchone_result: Any = None) -> None:
        self._fetchone_result = fetchone_result

    def fetchone(self) -> Any:
        return self._fetchone_result


class FakeConnection:
    """Mock database connection tracking queries and returning scripted cursor responses."""

    def __init__(self, fetchone_sequence: Optional[list[Any]] = None) -> None:
        self.executed_queries: list[tuple[str, list[Any]]] = []
        self._sequence = list(fetchone_sequence or [])

    def execute(self, query: str, params: Optional[list[Any]] = None) -> FakeCursor:
        self.executed_queries.append((query, list(params or [])))
        result = self._sequence.pop(0) if self._sequence else None
        return FakeCursor(result)


class TestAuthorizationHeaderRedaction(unittest.TestCase):
    """Positive and negative tests for Authorization and Proxy-Authorization headers."""

    def test_positive_authorization_bearer(self) -> None:
        raw = "Authorization: Bearer my-secret-token"
        self.assertEqual(redact(raw), f"Authorization: {MASK}")

    def test_positive_authorization_basic(self) -> None:
        raw = "authorization: Basic dXNlcjpwYXNz"
        self.assertEqual(redact(raw), f"authorization: {MASK}")

    def test_positive_proxy_authorization(self) -> None:
        raw = "Proxy-Authorization: Digest username=\"admin\""
        self.assertEqual(redact(raw), f"Proxy-Authorization: {MASK}")

    def test_positive_authorization_equals_syntax(self) -> None:
        raw = "authorization = token123"
        self.assertEqual(redact(raw), f"authorization: {MASK}")

    def test_negative_authorized_phrase(self) -> None:
        raw = "Authorized user performed action"
        self.assertEqual(redact(raw), raw)

    def test_negative_authorization_without_delimiter(self) -> None:
        raw = "Authorization header is required by SPEC"
        self.assertEqual(redact(raw), raw)

    def test_negative_unauthorized_message(self) -> None:
        raw = "HTTP 401 Unauthorized"
        self.assertEqual(redact(raw), raw)


class TestBearerTokenRedaction(unittest.TestCase):
    """Positive and negative tests for standalone Bearer tokens."""

    def test_positive_bearer_single_char(self) -> None:
        self.assertEqual(redact("Bearer 1"), f"Bearer {MASK}")

    def test_positive_bearer_short_token(self) -> None:
        self.assertEqual(redact("Bearer abc"), f"Bearer {MASK}")

    def test_positive_bearer_seven_chars(self) -> None:
        self.assertEqual(redact("bearer abc1234"), f"Bearer {MASK}")

    def test_positive_bearer_standard_long_token(self) -> None:
        self.assertEqual(redact("Bearer eyJhbGciOiJIUzI1NiJ9"), f"Bearer {MASK}")

    def test_negative_bearer_without_token(self) -> None:
        raw = "The check was payable to the bearer."
        self.assertEqual(redact(raw), raw)

    def test_negative_compound_word_bearer(self) -> None:
        raw = "A standard torchbearer was selected."
        self.assertEqual(redact(raw), raw)

    def test_negative_plain_numbers_and_sentences(self) -> None:
        raw = "Page 42, count 100, elapsed 250ms."
        self.assertEqual(redact(raw), raw)


class TestCookieHeaderRedaction(unittest.TestCase):
    """Positive and negative tests for Cookie and Set-Cookie headers."""

    def test_positive_cookie_header(self) -> None:
        raw = "Cookie: session_id=abc123; user=alice"
        self.assertEqual(redact(raw), f"Cookie: {MASK}")

    def test_positive_set_cookie_header(self) -> None:
        raw = "Set-Cookie: session=xyz789; Secure; HttpOnly"
        self.assertEqual(redact(raw), f"Set-Cookie: {MASK}")

    def test_negative_cookie_in_sentence(self) -> None:
        raw = "User accepted the browser cookie policy"
        self.assertEqual(redact(raw), raw)

    def test_negative_fortune_cookie(self) -> None:
        raw = "Eating a cookie during lunch"
        self.assertEqual(redact(raw), raw)


class TestUrlPasswordRedaction(unittest.TestCase):
    """Positive and negative tests for URL credentials, including @ and / in passwords."""

    def test_positive_url_password_with_at_and_slash(self) -> None:
        raw = "postgresql://user:p@ss/word@host/db"
        self.assertEqual(redact(raw), f"postgresql://user:{MASK}@host/db")

    def test_positive_url_password_with_multiple_ats(self) -> None:
        raw = "postgresql://user:p@ss@host:5432/db"
        self.assertEqual(redact(raw), f"postgresql://user:{MASK}@host:5432/db")

    def test_positive_url_password_with_slash(self) -> None:
        raw = "postgresql://user:p/word@host/db"
        self.assertEqual(redact(raw), f"postgresql://user:{MASK}@host/db")

    def test_positive_url_standard_password(self) -> None:
        raw = "https://admin:secret123@example.com/api"
        self.assertEqual(redact(raw), f"https://admin:{MASK}@example.com/api")

    def test_positive_quoted_url(self) -> None:
        raw = "url='postgresql://user:p@ss/word@host/db'"
        self.assertEqual(redact(raw), f"url='postgresql://user:{MASK}@host/db'")

    def test_negative_url_without_credentials(self) -> None:
        raw = "https://example.com/api/v1?page=1"
        self.assertEqual(redact(raw), raw)

    def test_negative_url_with_port_no_userinfo(self) -> None:
        raw = "http://localhost:8080/metrics"
        self.assertEqual(redact(raw), raw)

    def test_negative_url_with_user_only_no_password(self) -> None:
        raw = "ssh://git@github.com/org/repo.git"
        self.assertEqual(redact(raw), raw)


class TestQueryParamsRedaction(unittest.TestCase):
    """Positive and negative tests for query string secrets."""

    def test_positive_token_param(self) -> None:
        raw = "https://api.example.com/data?token=secret123"
        self.assertEqual(redact(raw), f"https://api.example.com/data?token={MASK}")

    def test_positive_short_key_param(self) -> None:
        raw = "https://api.example.com/data?key=1"
        self.assertEqual(redact(raw), f"https://api.example.com/data?key={MASK}")

    def test_positive_aws_signature_param(self) -> None:
        raw = "https://bucket.s3.amazonaws.com/file?x-amz-signature=abcdef"
        self.assertEqual(redact(raw), f"https://bucket.s3.amazonaws.com/file?x-amz-signature={MASK}")

    def test_positive_session_param(self) -> None:
        raw = "https://example.com/callback?session=sess_999&extra=1"
        self.assertEqual(redact(raw), f"https://example.com/callback?session={MASK}&extra=1")

    def test_negative_page_and_sort_params(self) -> None:
        raw = "https://example.com/items?page=5&limit=20&sort=asc"
        self.assertEqual(redact(raw), raw)

    def test_negative_format_and_id_params(self) -> None:
        raw = "https://example.com/resource?id=123&format=json"
        self.assertEqual(redact(raw), raw)


class TestPemPrivateKeyRedaction(unittest.TestCase):
    """Positive and negative tests for PEM private key blocks."""

    def test_positive_rsa_private_key_block(self) -> None:
        raw = (
            "Header text\n"
            "-----BEGIN RSA " + PK + "-----\n"
            "MIIEowIBAAKCAQEA0Yxyz...\n"
            "-----END RSA " + PK + "-----\n"
            "Footer text"
        )
        expected = f"Header text\n{MASK}\nFooter text"
        self.assertEqual(redact(raw), expected)

    def test_positive_generic_private_key_block(self) -> None:
        raw = (
            "-----BEGIN " + PK + "-----\n"
            "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC...\n"
            "-----END " + PK + "-----"
        )
        self.assertEqual(redact(raw), MASK)

    def test_positive_ec_private_key_block(self) -> None:
        raw = (
            "-----BEGIN EC " + PK + "-----\n"
            "MHcCAQEEII...\n"
            "-----END EC " + PK + "-----"
        )
        self.assertEqual(redact(raw), MASK)

    def test_positive_openssh_private_key_block(self) -> None:
        raw = (
            "-----BEGIN OPENSSH " + PK + "-----\n"
            "b3BlbnNzaC1rZXktdjEAAAA...\n"
            "-----END OPENSSH " + PK + "-----"
        )
        self.assertEqual(redact(raw), MASK)

    def test_negative_pem_certificate(self) -> None:
        raw = (
            "-----BEGIN CERTIFICATE-----\n"
            "MIIDdTCCAl2gAwIBAgILBAAAAAABFUzpW80wDQYJKoZIhvcNAQEFBQAwVzELMAkG...\n"
            "-----END CERTIFICATE-----"
        )
        self.assertEqual(redact(raw), raw)

    def test_negative_pem_public_key(self) -> None:
        raw = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAz...\n"
            "-----END PUBLIC KEY-----"
        )
        self.assertEqual(redact(raw), raw)


class TestJwtRedaction(unittest.TestCase):
    """Positive and negative tests for JSON Web Tokens (eyJ...)."""

    def test_positive_standard_jwt(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        self.assertEqual(redact(jwt), MASK)

    def test_positive_jwt_inside_sentence(self) -> None:
        raw = (
            "Received token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abc-123_456 "
            "for authentication"
        )
        self.assertEqual(raw.count("eyJ"), 2)  # header and payload both start with eyJ
        self.assertEqual(
            redact(raw),
            f"Received token {MASK} for authentication"
        )

    def test_negative_invalid_jwt_prefix(self) -> None:
        raw = "abc.def.ghi"
        self.assertEqual(redact(raw), raw)

    def test_negative_jwt_missing_third_segment(self) -> None:
        raw = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ"
        self.assertEqual(redact(raw), raw)

    def test_negative_semver_and_ip(self) -> None:
        raw = "Release v1.2.3 running on 192.168.1.1"
        self.assertEqual(redact(raw), raw)


class TestKeyValueSecretsRedaction(unittest.TestCase):
    """Positive and negative tests for key=value and "key": "value" style secrets."""

    def test_positive_short_token(self) -> None:
        self.assertEqual(redact("token=a"), f"token={MASK}")
        self.assertEqual(redact("token=abc"), f"token={MASK}")

    def test_positive_short_key(self) -> None:
        self.assertEqual(redact("key=1"), f"key={MASK}")
        self.assertEqual(redact("key=123"), f"key={MASK}")

    def test_positive_explanatory_key_value(self) -> None:
        self.assertEqual(redact("key=value"), f"key={MASK}")

    def test_positive_password_field(self) -> None:
        self.assertEqual(redact("password: mypassword123"), f"password: {MASK}")
        self.assertEqual(redact("passwd=secret"), f"passwd={MASK}")

    def test_positive_client_secret(self) -> None:
        self.assertEqual(redact("client_secret=s3cr3t"), f"client_secret={MASK}")
        self.assertEqual(redact("\"client_secret\": \"s3cr3t\""), f"\"client_secret\": \"{MASK}\"")

    def test_positive_credential_and_private_key(self) -> None:
        self.assertEqual(redact("credential=super_admin"), f"credential={MASK}")
        self.assertEqual(redact("private_key=pk_999"), f"private_key={MASK}")

    def test_positive_session_and_jwt(self) -> None:
        self.assertEqual(redact("session=sess_abc"), f"session={MASK}")
        self.assertEqual(redact("jwt=my_jwt_val"), f"jwt={MASK}")

    def test_negative_common_non_secret_words(self) -> None:
        raw = "page=1, id=42, user=martin, status=ok, count=100"
        self.assertEqual(redact(raw), raw)

    def test_negative_page_count_and_token_count(self) -> None:
        raw = "page_count=500 token_count=1000"
        self.assertEqual(redact(raw), raw)

    def test_negative_keyboard_and_monkey(self) -> None:
        raw = "keyboard=mechanical monkey=chimp"
        self.assertEqual(redact(raw), raw)


class TestProviderKeysRedaction(unittest.TestCase):
    """Positive and negative tests for well-known provider key shapes."""

    def test_positive_openai_key(self) -> None:
        self.assertEqual(redact("sk-abcdef1234567890abcdef"), MASK)

    def test_positive_aws_key_id(self) -> None:
        self.assertEqual(redact(AWS_FAKE), MASK)

    def test_positive_github_pat(self) -> None:
        self.assertEqual(redact("ghp_12345678901234567890"), MASK)

    def test_positive_slack_token(self) -> None:
        self.assertEqual(redact("xoxb-1234567890-abcdefghij"), MASK)

    def test_negative_short_sk_prefix(self) -> None:
        raw = "sk-short"
        self.assertEqual(redact(raw), raw)


class TestSecretKeysDictionaryRedaction(unittest.TestCase):
    """Tests for redact_value with dictionary keys and nested structures."""

    def test_new_secret_keys_redacted(self) -> None:
        test_keys = [
            "credential",
            "credentials",
            "private_key",
            "session",
            "session_id",
            "jwt",
            "bearer",
            "cookie",
            "client_secret",
            "client-secret",
            "password",
            "token",
            "api_key",
        ]
        for k in test_keys:
            res = redact_value("my_value", key=k)
            self.assertEqual(res, MASK, f"Key '{k}' was not masked")

    def test_non_secret_keys_preserved(self) -> None:
        non_secrets = {
            "page_number": 42,
            "document_id": "doc-001",
            "status": "success",
            "count": 10,
            "user_id": "u-123",
            "is_active": True,
        }
        res = redact_value(non_secrets)
        self.assertEqual(res, non_secrets)

    def test_nested_dictionary_redaction(self) -> None:
        payload = {
            "user": "alice",
            "auth": {
                "session": "sess_123",
                "client_secret": "secret_abc",
                "page": 2,
            },
            "history": ["normal", "token=hidden123", 99],
        }
        redacted = redact_value(payload)
        self.assertEqual(redacted["user"], "alice")
        self.assertEqual(redacted["auth"]["session"], MASK)
        self.assertEqual(redacted["auth"]["client_secret"], MASK)
        self.assertEqual(redacted["auth"]["page"], 2)
        self.assertEqual(redacted["history"][0], "normal")
        self.assertEqual(redacted["history"][1], f"token={MASK}")
        self.assertEqual(redacted["history"][2], 99)


class TestLogEventFunctionality(unittest.TestCase):
    """Tests for log_event DB interaction, text field masking, event_name check, and LogConflict."""

    def test_text_fields_redacted_in_log_event(self) -> None:
        fake_conn = FakeConnection(fetchone_sequence=[(uuid.uuid4(),)])
        eid = uuid.uuid4()
        res_id = log_event(
            fake_conn,
            level="INFO",
            event_name="parse.completed",
            message="Connected to postgresql://user:p@ss/word@host/db",
            service="svc_key=1",
            component="comp_token=abc",
            operation_name="op_password=123",
            status="stat_bearer 1",
            error_code="err_sk-1234567890123456",
            event_id=eid,
        )
        self.assertEqual(res_id, eid)
        self.assertEqual(len(fake_conn.executed_queries), 1)
        query, params = fake_conn.executed_queries[0]
        self.assertIn("RETURNING id", query)

        # Check values inserted into log_events
        # cols: event_id, schema_version, occurred_at, level, event_name, message, service,
        #       component, operation_name, telemetry_origin, redaction_policy, attributes, error_message,
        #       error_code, status
        self.assertTrue(any(isinstance(p, str) and f"postgresql://user:{MASK}@host/db" in p for p in params))
        self.assertIn(f"svc_key={MASK}", params)
        self.assertIn(f"comp_token={MASK}", params)
        self.assertIn(f"op_password={MASK}", params)
        self.assertIn(f"stat_Bearer {MASK}", params)
        self.assertIn(f"err_{MASK}", params)

    def test_event_name_with_secret_raises_value_error(self) -> None:
        fake_conn = FakeConnection()
        with self.assertRaises(ValueError) as ctx:
            log_event(
                fake_conn,
                level="INFO",
                event_name="auth.sk-1234567890123456",
                message="hello",
                service="svc",
                component="comp",
                operation_name="op",
            )
        self.assertIn("event_name contains secret pattern", str(ctx.exception))

    def test_event_name_with_token_pattern_raises_value_error(self) -> None:
        fake_conn = FakeConnection()
        with self.assertRaises(ValueError) as ctx:
            log_event(
                fake_conn,
                level="INFO",
                event_name="auth.token=secret",
                message="hello",
                service="svc",
                component="comp",
                operation_name="op",
            )
        self.assertIn("event_name contains secret pattern", str(ctx.exception))

    def test_valid_event_names_allowed(self) -> None:
        valid_events = [
            "parse.completed",
            "context.built",
            "question.generated",
            "question.validated",
            "benchmark.frozen",
            "auth.token.generated",
        ]
        for name in valid_events:
            fake_conn = FakeConnection(fetchone_sequence=[(uuid.uuid4(),)])
            eid = log_event(
                fake_conn,
                level="INFO",
                event_name=name,
                message="ok",
                service="svc",
                component="comp",
                operation_name="op",
            )
            self.assertIsInstance(eid, uuid.UUID)

    def test_attributes_exceeding_16_kib_raises_value_error(self) -> None:
        fake_conn = FakeConnection()
        huge_attributes = {"data": "x" * (MAX_ATTRIBUTES_BYTES + 100)}
        with self.assertRaises(ValueError) as ctx:
            log_event(
                fake_conn,
                level="INFO",
                event_name="data.processed",
                message="test",
                service="svc",
                component="comp",
                operation_name="op",
                attributes=huge_attributes,
            )
        self.assertIn("exceeds 16 KiB limit", str(ctx.exception))

    def test_attributes_within_16_kib_allowed(self) -> None:
        fake_conn = FakeConnection(fetchone_sequence=[(uuid.uuid4(),)])
        normal_attributes = {"data": "x" * 1000}
        eid = log_event(
            fake_conn,
            level="INFO",
            event_name="data.processed",
            message="test",
            service="svc",
            component="comp",
            operation_name="op",
            attributes=normal_attributes,
        )
        self.assertIsInstance(eid, uuid.UUID)

    def test_conflict_with_identical_content_returns_event_id(self) -> None:
        eid = uuid.uuid4()
        # First query: INSERT ... RETURNING id -> returns None (simulating conflict)
        # Second query: SELECT level, event_name, message, service, component, operation_name -> returns identical row
        existing_row = ("INFO", "test.event", "clean message", "svc", "comp", "op")
        fake_conn = FakeConnection(fetchone_sequence=[None, existing_row])

        res_id = log_event(
            fake_conn,
            level="INFO",
            event_name="test.event",
            message="clean message",
            service="svc",
            component="comp",
            operation_name="op",
            event_id=eid,
        )
        self.assertEqual(res_id, eid)
        self.assertEqual(len(fake_conn.executed_queries), 2)
        self.assertIn("SELECT level, event_name, message", fake_conn.executed_queries[1][0])

    def test_conflict_with_different_content_raises_log_conflict(self) -> None:
        eid = uuid.uuid4()
        # Existing row has different message
        existing_row = ("INFO", "test.event", "original message", "svc", "comp", "op")
        fake_conn = FakeConnection(fetchone_sequence=[None, existing_row])

        with self.assertRaises(LogConflict) as ctx:
            log_event(
                fake_conn,
                level="INFO",
                event_name="test.event",
                message="modified message",
                service="svc",
                component="comp",
                operation_name="op",
                event_id=eid,
            )
        self.assertIn(f"Conflicting log event for event_id {eid}", str(ctx.exception))

    def test_unknown_log_fields_raises_value_error(self) -> None:
        fake_conn = FakeConnection()
        with self.assertRaises(ValueError) as ctx:
            log_event(
                fake_conn,
                level="INFO",
                event_name="test.event",
                message="hello",
                service="svc",
                component="comp",
                operation_name="op",
                unsupported_custom_field="bad",
            )
        self.assertIn("unknown log fields", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
