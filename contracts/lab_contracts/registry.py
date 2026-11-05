"""Exported schema registry: name -> model. Adding a contract = add it here."""
from .api import ErrorResponse, Page, PageRequest
from .source import SourceRegion

MODELS = {
    "SourceRegion": SourceRegion,
    "ErrorResponse": ErrorResponse,
    "PageRequest": PageRequest,
    "SourceRegionPage": Page[SourceRegion],
}
