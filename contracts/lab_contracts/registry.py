"""Exported schema registry: name -> model. Adding a contract = add it here."""
from .api import ErrorResponse, Page, PageRequest
from .method import (AsyncAccepted, Capabilities, JobStatus, PrepareRequest, PrepareResult,
                     QueryRequest, QueryResponse, SubmissionLine)
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
}
