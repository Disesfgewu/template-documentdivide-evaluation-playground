"""Base types shared by every contract (SPEC 16.1)."""
import json
import math
from enum import Enum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

# Opaque API resource ID, length 1-128. Never parsed for meaning.
ResourceId = Annotated[str, StringConstraints(min_length=1, max_length=128)]

# SHA-256: 64 lowercase hex characters.
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# Max serialized size of an `attributes` extension object (bytes, UTF-8 JSON).
ATTRIBUTES_MAX_BYTES = 4096


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("must be a finite number")
    return v


FiniteFloat = Annotated[float, AfterValidator(_finite)]


def _attributes_size(v: dict[str, Any]) -> dict[str, Any]:
    size = len(json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    if size > ATTRIBUTES_MAX_BYTES:
        raise ValueError(f"attributes exceed {ATTRIBUTES_MAX_BYTES} bytes")
    return v


Attributes = Annotated[dict[str, Any], AfterValidator(_attributes_size)]


class Contract(BaseModel):
    """Strict base: unknown top-level fields rejected (-> 422), no type coercion
    (an integer must not arrive as "3" or 3.0)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)



def check_unique(values, what: str) -> None:
    """Raise ValueError naming the first duplicate (enum members shown by value)."""
    seen = set()
    for v in values:
        if v in seen:
            shown = v.value if isinstance(v, Enum) else v
            raise ValueError(f"duplicate {what}: {shown!r}")
        seen.add(v)
