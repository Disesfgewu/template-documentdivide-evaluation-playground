"""Base types shared by every contract (SPEC 16.1)."""
import json
from enum import Enum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# Opaque API resource ID, length 1-128. Never parsed for meaning.
ResourceId = Annotated[str, StringConstraints(min_length=1, max_length=128)]

# SHA-256: 64 lowercase hex characters.
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# Max serialized size of an `attributes` extension object (bytes, UTF-8 JSON).
ATTRIBUTES_MAX_BYTES = 4096


# Native constraint (not a custom validator) so that bounds layered on top,
# e.g. Annotated[FiniteFloat, Field(ge=0)], export as JSON Schema
# minimum/maximum instead of pydantic-internal "ge"/"le" keys.
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def _attributes_size(v: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        # Non-JSON values (sets, objects, NaN/Infinity) arrive via the Python
        # API; JSON input can never contain them.
        raise ValueError("attributes must be JSON-serializable without NaN/Infinity") from None
    size = len(encoded.encode())
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
