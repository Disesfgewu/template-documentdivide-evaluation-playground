"""Trusted credential verification for the LAB API (SPEC 16.4, R01).

Enforces cryptographic Bearer token verification and rejects caller-asserted
identity (e.g. raw actor UUIDs or X-Actor-Id spoofing headers).
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Optional

from .config import Settings


class AuthError(Exception):
    """Sanitized authentication error matching SPEC 16.4 error contract."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def get_signing_secret(settings: Settings) -> bytes:
    """Retrieve independently provisioned signing secret from application settings.

    Never falls back to database_url or any other application credential.
    """
    if getattr(settings, "auth_secret", None) is not None:
        val = settings.auth_secret.get_secret_value()
        if val:
            return val.encode("utf-8")
    raise AuthError(
        status_code=500,
        code="auth_not_configured",
        message="authentication secret is not configured",
    )


def issue_actor_token(actor_id: uuid.UUID, secret: bytes | str) -> str:
    """Issue a cryptographically signed bearer token for a verified actor."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    actor_str = str(actor_id)
    sig = hmac.new(secret, f"lab-actor:{actor_str}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"lab_act_{actor_str}_{sig}"


def verify_actor_token(token: str, secret: bytes | str) -> Optional[uuid.UUID]:
    """Verify an actor token signature and extract the actor UUID safely.

    Returns None if the token format, structure, length, or signature is invalid.
    Never raises uncaught exceptions on malformed or non-ASCII inputs.
    """
    if not isinstance(token, str) or not token.startswith("lab_act_"):
        return None
    parts = token.split("_")
    if len(parts) != 4 or parts[0] != "lab" or parts[1] != "act":
        return None
    actor_str, sig = parts[2], parts[3]
    try:
        actor_id = uuid.UUID(actor_str)
    except Exception:
        return None

    if len(sig) != 64:
        return None

    try:
        sig_bytes = sig.encode("ascii")
    except (UnicodeEncodeError, ValueError):
        return None

    if isinstance(secret, str):
        secret = secret.encode("utf-8")

    try:
        expected_sig = hmac.new(
            secret, f"lab-actor:{actor_str}".encode("utf-8"), hashlib.sha256
        ).hexdigest().encode("ascii")
    except Exception:
        return None

    if not hmac.compare_digest(sig_bytes, expected_sig):
        return None
    return actor_id
