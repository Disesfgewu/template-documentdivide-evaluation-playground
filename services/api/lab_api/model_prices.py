"""Per-model list prices for the cost estimate, the budget cap and the worker's charge (Issue #197, PM on the judge model).

Until now ONE price pair (``LAB_GEN_PRICE_IN/OUT_MICROS_PER_MTOK``, the owner's configured model) stood for every model, so choosing a stronger
judge made the estimate, the cap and the recorded spend too low. Now a price is looked up by model name, in this order:

1. ``LAB_GEN_MODEL_PRICES`` (owner-editable JSON, e.g. ``{"claude-sonnet-5-5": [2000000, 10000000]}``, micro-USD per million tokens, in and out);
2. the built-in table below: list prices read from the Claude API reference's model table cached 2026-11-03. **UNVERIFIED**: they are not checked
   against the owner's invoice, and Haiku 5.5's price is for prompts up to 100K tokens (0.50 / 2.50 USD beyond; the prompts here are far smaller);
3. the configured pair ``LAB_GEN_PRICE_*``, which is the price of the model the owner configured (claude-haiku-4-5 by default: it is deliberately not in the
   table, so the configured values keep deciding for it exactly as before).
A date suffix on a model name (``claude-haiku-4-5-20251001``) is ignored for the lookup. No price at all for a model is an error, never zero."""
from __future__ import annotations

import json
import os
import re
from typing import Mapping, Optional

BUILTIN_UNVERIFIED = {
    "claude-haiku-5-5": (100_000, 500_000),
    "claude-sonnet-5-5": (2_000_000, 10_000_000),
    "claude-opus-5-5": (4_000_000, 20_000_000),
}
MAX_PRICE = 1_000_000_000
_DATE = re.compile(r"-\d{8}$")


class ModelPriceError(ValueError):
    """The owner's LAB_GEN_MODEL_PRICES is not usable (the message never contains a key)."""


def model_key(name: str) -> str:
    return _DATE.sub("", name.strip())


def overrides(env: Optional[Mapping[str, str]] = None) -> dict[str, tuple[int, int]]:
    raw = (env if env is not None else os.environ).get("LAB_GEN_MODEL_PRICES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        out = {}
        for name, pair in data.items():
            pin, pout = pair
            if not isinstance(name, str) or type(pin) is not int or type(pout) is not int or not 0 <= pin <= MAX_PRICE or not 0 <= pout <= MAX_PRICE:
                raise ValueError
            out[model_key(name)] = (pin, pout)
        return out
    except (ValueError, TypeError, AttributeError):
        raise ModelPriceError('LAB_GEN_MODEL_PRICES must be JSON like {"model-name": [input_micros_per_mtok, output_micros_per_mtok]} with integers 0..1000000000') from None


def price_for(model: str, configured: Optional[tuple[int, int]], env: Optional[Mapping[str, str]] = None) -> tuple[int, int, str]:
    """(input, output, source) in micro-USD per million tokens; source is ``owner_override``, ``builtin_unverified`` or ``configured``."""
    key = model_key(model)
    own = overrides(env)
    if key in own:
        return (*own[key], "owner_override")
    if key in BUILTIN_UNVERIFIED:
        return (*BUILTIN_UNVERIFIED[key], "builtin_unverified")
    if configured is not None:
        return (*configured, "configured")
    raise ModelPriceError(f"no price is known for model {key!r}: set LAB_GEN_PRICE_IN/OUT_MICROS_PER_MTOK or add it to LAB_GEN_MODEL_PRICES")
