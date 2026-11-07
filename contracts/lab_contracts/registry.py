"""Exported schema registry: name -> model. Adding a contract = add it here."""
from .api import ErrorResponse, Page, PageRequest
from .benchmark import (BenchmarkManifest, EvidenceUnit, Question, SemanticAnswer, SourceAnswer,
                        TextualAnswer, ValidationRecord)
from .method import (AsyncAccepted, Capabilities, JobStatus, PrepareRequest, PrepareResult,
                     QueryRequest, QueryResponse, SubmissionLine)
from .parse import CapabilityManifest, ParseManifest, ParseRequest, ParseResult
from .score import MetricResult
from .source import SourceRegion

MODELS = {
    "SourceRegion": SourceRegion,
    "ErrorResponse": ErrorResponse,
    "PageRequest": PageRequest,
    "SourceRegionPage": Page[SourceRegion],
    "Capabilities": Capabilities,
    "PrepareRequest": PrepareRequest,
    "PrepareResult": PrepareResult,
    "QueryRequest": QueryRequest,
    "QueryResponse": QueryResponse,
    "AsyncAccepted": AsyncAccepted,
    "JobStatus": JobStatus,
    "SubmissionLine": SubmissionLine,
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
