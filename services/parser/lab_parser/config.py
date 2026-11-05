"""Environment-driven settings for the LAB parser service (``LAB_PARSER_*``)."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    dev = "dev"
    test = "test"
    prod = "prod"


class ConfigError(ValueError):
    """Settings are missing or invalid. The message never contains values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAB_PARSER_", env_file=None, frozen=True)

    env: Environment
    # Where uploaded PDFs and derived page images/assets are read and written.
    artifact_root: Path

    @field_validator("artifact_root")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError("must be an absolute path")
        return v


def load_settings(**overrides) -> Settings:
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            field = f"LAB_PARSER_{loc.upper()}" if loc else "settings"
            problems.append(f"{field}: {err['msg']}")
        raise ConfigError("invalid LAB parser configuration: " + "; ".join(problems)) from None
