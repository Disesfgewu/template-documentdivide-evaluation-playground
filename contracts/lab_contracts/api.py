"""API error body and pagination envelope (SPEC 16.4)."""
from typing import Annotated, Any, Generic, Optional, TypeVar

from pydantic import Field, StringConstraints, model_validator

from .base import Contract, ResourceId

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200

ErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class ErrorBody(Contract):
    """Every non-2xx response and every failed job's `error`. Must not carry
    credentials or gold content; details is a bounded, typed-free object."""

    code: ErrorCode
    message: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    retryable: bool
    request_id: ResourceId
    details: dict[str, Any]
    retry_after: Optional[Annotated[int, Field(ge=0)]] = None


class ErrorResponse(Contract):
    error: ErrorBody


T = TypeVar("T")


class PageRequest(Contract):
    page_size: Annotated[int, Field(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT
    cursor: Optional[Annotated[str, StringConstraints(min_length=1, max_length=1024)]] = None


class Page(Contract, Generic[T]):
    """Cursor page. next_cursor is null only on the last page; the cursor is
    bound to the filter/order that produced it (enforced by the API, #5+)."""

    items: list[T]
    page_size: Annotated[int, Field(ge=1, le=PAGE_SIZE_MAX)]
    next_cursor: Optional[Annotated[str, StringConstraints(min_length=1, max_length=1024)]]

    @model_validator(mode="after")
    def _size(self):
        if len(self.items) > self.page_size:
            raise ValueError("items longer than page_size")
        return self
