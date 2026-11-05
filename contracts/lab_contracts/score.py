"""Per-question metric results (SPEC 9, 16.5). A metric result keeps its
numerator and denominator so every score can be recomputed by hand; pending
and error never masquerade as zero (T43)."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from pydantic import Field, StringConstraints, model_validator

from .base import Contract, FiniteFloat, ResourceId

Metric = Literal["recall_at_k", "complete_at_k", "correctness", "textual_consistency",
                 "localization_precision", "citation_precision", "citation_completeness",
                 "faithfulness"]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Score = Annotated[FiniteFloat, Field(ge=0.0, le=100.0)]
NonNeg = Annotated[FiniteFloat, Field(ge=0.0)]


def display(value: float) -> str:
    """Two decimals for display only (half-up on the shortest repr of the
    float, so 12.345 -> "12.35"); computation keeps full precision."""
    return str(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class MetricResult(Contract):
    question_id: ResourceId
    metric: Metric
    status: Literal["scored", "not_applicable", "pending", "error"]
    raw_value: Optional[Score]
    display_value: Optional[Annotated[str, StringConstraints(pattern=r"^[0-9]{1,3}\.[0-9]{2}$")]]
    numerator: Optional[NonNeg]
    denominator: Optional[NonNeg]
    reason_codes: list[ReasonCode]
    grader_revision: ResourceId
    support_refs: list[ResourceId]

    @model_validator(mode="after")
    def _consistency(self):
        vals = (self.raw_value, self.display_value, self.numerator, self.denominator)
        if self.status == "scored":
            if any(v is None for v in vals):
                raise ValueError("scored requires raw_value, display_value, numerator and denominator")
            if self.denominator == 0:
                raise ValueError("scored requires denominator > 0 (use not_applicable)")
            expected = 100.0 * self.numerator / self.denominator
            if not math.isclose(self.raw_value, expected, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("raw_value must equal 100 * numerator / denominator")
            if self.display_value != display(self.raw_value):
                raise ValueError("display_value must be raw_value rounded to 2 decimals")
        else:
            # N/A, pending and error carry no number: never a substitute zero.
            if any(v is not None for v in (self.raw_value, self.display_value)):
                raise ValueError(f"{self.status} must not carry raw_value/display_value")
            if not self.reason_codes:
                raise ValueError(f"{self.status} requires reason_codes")
        return self
