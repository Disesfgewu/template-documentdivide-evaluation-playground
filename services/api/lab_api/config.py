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
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @field_validator("database_url")
    @classmethod
    def _postgres_only(cls, v: SecretStr) -> SecretStr:
        parts = urlsplit(v.get_secret_value())
        if parts.scheme not in ("postgresql", "postgres"):
            # SQLite or other stores must not stand in for PostgreSQL (SPEC §16).
            raise ValueError("must be a postgresql:// URL")
        if not parts.hostname or not parts.path.strip("/"):
            raise ValueError("must include host and database name")
        return v

    @field_validator("artifact_root")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError("must be an absolute path")
        return v

    @model_validator(mode="after")
    def _provider_policy(self) -> "Settings":
        if self.provider_mode is ProviderMode.real:
            raise ValueError(
                "provider_mode=real is not available: no real provider adapter "
                "is implemented yet (T84 pending)"
            )
        if self.env is Environment.prod and self.provider_mode is ProviderMode.mock:
            raise ValueError("env=prod must not run with provider_mode=mock")
        return self


def load_settings(**overrides) -> Settings:
    """Load settings, converting validation errors to a value-free ConfigError."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "settings"
            field = f"LAB_{loc.upper()}" if loc != "settings" else loc
            problems.append(f"{field}: {err['msg']}")
        raise ConfigError("invalid LAB configuration: " + "; ".join(problems)) from None
