"""Environment-driven settings for the LAB parser service (``LAB_PARSER_*``)."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

_MSG_ABSOLUTE = "must be an absolute path"
# Only these fixed validator messages are surfaced; pydantic's own ``msg``
# may embed the input value, so it is never printed (same policy as lab_api).
_SAFE_VALUE_ERRORS = frozenset({_MSG_ABSOLUTE})
_SAFE_TYPE_MESSAGES = {
    "missing": "is required",
    "enum": "is not an allowed value",
    "path_type": "must be a path",
    "string_type": "must be a string",
}


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
            raise ValueError(_MSG_ABSOLUTE)
        return v


def load_settings(**overrides) -> Settings:
    """Load settings, converting validation errors to a value-free ConfigError."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            field = f"LAB_PARSER_{loc.upper()}" if loc else "settings"
            problems.append(f"{field}: {_safe_message(err)}")
        raise ConfigError("invalid LAB parser configuration: " + "; ".join(problems)) from None
    except SettingsError:
        raise ConfigError(
            "invalid LAB parser configuration: an environment value could not be decoded"
        ) from None


def _safe_message(err) -> str:
    if err["type"] == "value_error":
        text = str(err.get("ctx", {}).get("error", ""))
        return text if text in _SAFE_VALUE_ERRORS else "is invalid"
    return _SAFE_TYPE_MESSAGES.get(err["type"], "is invalid")
