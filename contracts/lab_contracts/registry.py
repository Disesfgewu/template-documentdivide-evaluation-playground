"""Exported schema registry: name -> model. Adding a contract = add it here."""
from .api import ErrorResponse, Page, PageRequest
from .benchmark import (BenchmarkManifest, EvidenceUnit, Question, SemanticAnswer, SourceAnswer,
                        TextualAnswer, ValidationRecord)
from .parse import CapabilityManifest, ParseManifest, ParseRequest, ParseResult
from .score import MetricResult
from .source import SourceRegion

MODELS = {
    "SourceRegion": SourceRegion,
    "ErrorResponse": ErrorResponse,
    "PageRequest": PageRequest,
    "SourceRegionPage": Page[SourceRegion],
    "Question": Question,
    "SemanticAnswer": SemanticAnswer,
    "TextualAnswer": TextualAnswer,
    "SourceAnswer": SourceAnswer,
    "EvidenceUnit": EvidenceUnit,
    "ValidationRecord": ValidationRecord,
    "BenchmarkManifest": BenchmarkManifest,
    "CapabilityManifest": CapabilityManifest,
    "ParseManifest": ParseManifest,
    "ParseRequest": ParseRequest,
    "ParseResult": ParseResult,
    "MetricResult": MetricResult,
}
