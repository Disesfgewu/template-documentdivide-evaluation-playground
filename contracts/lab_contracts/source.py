"""SourceRegion (SPEC 4.1, 16.1)."""
from enum import Enum
from typing import Annotated, Optional

from pydantic import Field, StringConstraints, model_validator

from .base import Contract, FiniteFloat, ResourceId, Sha256


class RegionKind(str, Enum):
    text = "text"
    table = "table"
    figure = "figure"
    image = "image"
    formula = "formula"
    page = "page"


Coord = Annotated[FiniteFloat, Field(ge=0.0, le=1.0)]
FULL_PAGE = (0.0, 0.0, 1.0, 1.0)


class SourceRegion(Contract):
    """A region on one physical PDF page. Origin top-left after applying page
    rotation, normalized to the visible CropBox."""

    document_id: ResourceId
    document_sha256: Sha256
    page_index: Annotated[int, Field(ge=0)]
    page_label: Optional[Annotated[str, StringConstraints(max_length=64)]] = None
    bbox: tuple[Coord, Coord, Coord, Coord]
    kind: RegionKind
    quote: Optional[str]

    @model_validator(mode="after")
    def _geometry(self):
        x0, y0, x1, y1 = self.bbox
        if not (x0 < x1 and y0 < y1):
            raise ValueError("bbox must satisfy x0 < x1 and y0 < y1 (positive area)")
        if self.kind is RegionKind.page and tuple(self.bbox) != FULL_PAGE:
            raise ValueError("kind=page requires bbox [0,0,1,1]")
        return self

    def check_against_document(self, document_sha256: str, page_count: int) -> list[str]:
        """Semantic checks needing the document: returns reasons (empty = valid).
        Structurally valid regions failing these are kept as invalid_region
        (SPEC 16.3), so this returns reasons instead of raising."""
        reasons = []
        if self.document_sha256 != document_sha256:
            reasons.append("document_sha256_mismatch")
        if page_count < 1:
            raise ValueError("page_count must be >= 1")
        if self.page_index >= page_count:
            reasons.append("page_index_out_of_range")
        return reasons
