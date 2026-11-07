"""Environment-driven settings for the LAB API.

Every setting is read from ``LAB_*`` environment variables. There are no
defaults for the settings that decide *where* data goes or *which* provider
answers, so a misconfigured process fails at startup instead of silently
using a fallback.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

# Messages raised by our own validators. They are fixed strings that never
# interpolate input, so they are the only validator messages ever surfaced.
_MSG_SCHEME = "must be a postgresql:// URL"
_MSG_HOST_DB = "must include host and database name"
_MSG_MALFORMED_URL = "is not a well-formed URL"
_MSG_ABSOLUTE = "must be an absolute path"
_MSG_REAL = (
    "provider_mode=real is not available: no real provider adapter "
    "is implemented yet (T84 pending)"
)
_MSG_PROD_MOCK = "env=prod must not run with provider_mode=mock"
_MSG_BOOLEAN = "must be a boolean"
_SAFE_VALUE_ERRORS = frozenset(
    {
        _MSG_SCHEME,
        _MSG_HOST_DB,
        _MSG_MALFORMED_URL,
        _MSG_ABSOLUTE,
        _MSG_REAL,
        _MSG_PROD_MOCK,
        _MSG_BOOLEAN,
    }
)

# Fixed, value-free text for pydantic's built-in error types. Pydantic's own
# ``msg`` may embed the input (or fragments of it), so it is never used.
_SAFE_TYPE_MESSAGES = {
    "missing": "is required",
    "enum": "is not an allowed value",
    "greater_than": "is out of range",
    "less_than_equal": "is out of range",
    "float_parsing": "must be a number",
    "float_type": "must be a number",
    "finite_number": "must be a finite number",
    "bool_parsing": "must be a boolean",
    "bool_type": "must be a boolean",
    "path_type": "must be a path",
    "string_type": "must be a string",
}


class Environment(str, Enum):
    dev = "dev"
    test = "test"
    prod = "prod"


class ProviderMode(str, Enum):
    # Deterministic local fakes; never presented as real model output.
    mock = "mock"
    # Real external providers. No real adapter exists yet (T84 pending), so
    # this mode is refused rather than silently falling back to mocks.
    real = "real"


class ConfigError(ValueError):
    """Settings are missing or invalid. The message never contains values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAB_", env_file=None, frozen=True)

    env: Environment
    provider_mode: ProviderMode
    database_url: SecretStr
    artifact_root: Path
    db_connect_timeout_s: float = Field(default=3.0, gt=0, le=30)
    trust_remote_traceparent: bool = False

    @field_validator("trust_remote_traceparent", mode="before")
    @classmethod
    def _strict_bool(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            norm = v.strip().lower()
            if norm in ("true", "1"):
                return True
            if norm in ("false", "0"):
                return False
        raise ValueError("must be a boolean")

    @field_validator("database_url")
    @classmethod
    def _postgres_only(cls, v: SecretStr) -> SecretStr:
        # urllib's errors (e.g. NFKC netloc, bad IPv6, bad port) quote the
        # netloc, which carries the password; replace them with a fixed text.
        try:
            parts = urlsplit(v.get_secret_value())
            scheme, hostname, path = parts.scheme, parts.hostname, parts.path
            parts.port  # validates the port; raises ValueError quoting it
        except Exception:
            raise ValueError(_MSG_MALFORMED_URL) from None
        if scheme not in ("postgresql", "postgres"):
            # SQLite or other stores must not stand in for PostgreSQL (SPEC §16).
            raise ValueError(_MSG_SCHEME)
        if not hostname or not path.strip("/"):
            raise ValueError(_MSG_HOST_DB)
        return v

    @field_validator("artifact_root")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(_MSG_ABSOLUTE)
        return v

    @model_validator(mode="after")
    def _provider_policy(self) -> "Settings":
        if self.provider_mode is ProviderMode.real:
            raise ValueError(_MSG_REAL)
        if self.env is Environment.prod and self.provider_mode is ProviderMode.mock:
            raise ValueError(_MSG_PROD_MOCK)
        return self


def load_settings(**overrides) -> Settings:
    """Load settings, converting validation errors to a value-free ConfigError."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = [f"{_field_name(err)}: {_safe_message(err)}" for err in exc.errors()]
        raise ConfigError("invalid LAB configuration: " + "; ".join(problems)) from None
    except SettingsError:
        # Raised while decoding env sources; its text may quote the raw value.
        raise ConfigError("invalid LAB configuration: an environment value could not be decoded") from None


def _field_name(err) -> str:
    loc = ".".join(str(p) for p in err["loc"])
    return f"LAB_{loc.upper()}" if loc else "settings"


def _safe_message(err) -> str:
    """Return a fixed message for ``err``; never pydantic's input-bearing ``msg``."""
    if err["type"] == "value_error":
        text = str(err.get("ctx", {}).get("error", ""))
        # pydantic prefixes "Value error, " in msg but not in ctx["error"].
        return text if text in _SAFE_VALUE_ERRORS else "is invalid"
    return _SAFE_TYPE_MESSAGES.get(err["type"], "is invalid")
