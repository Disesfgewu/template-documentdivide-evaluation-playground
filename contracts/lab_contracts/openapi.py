"""OpenAPI 3.1 document for the method-service protocol (SPEC 7.2), generated
from the contract models so the two can never drift. LAB's own external API
(SPEC 8) gets its document when those endpoints exist (#5+)."""
from __future__ import annotations

from pydantic.json_schema import models_json_schema

from .api import ErrorResponse
from .method import (PROTOCOL_VERSION, AsyncAccepted, Capabilities, JobStatus, PrepareRequest,
                     QueryRequest, QueryResponse)

REF = "#/components/schemas/{model}"
MODELS = [Capabilities, PrepareRequest, AsyncAccepted, JobStatus, QueryRequest, QueryResponse, ErrorResponse]

# (method, path) exactly as listed in SPEC 7.2.
ENDPOINTS = [("get", "/v1/capabilities"), ("post", "/v1/prepare"), ("get", "/v1/jobs/{job_id}"),
             ("post", "/v1/query"), ("post", "/v1/jobs/{job_id}/cancel")]


def _ref(model) -> dict:
    return {"$ref": REF.format(model=model.__name__)}


def _json(model, description: str) -> dict:
    return {"description": description, "content": {"application/json": {"schema": _ref(model)}}}


def _errors(*codes: int) -> dict:
    text = {400: "Body is not strict UTF-8 JSON", 404: "Unknown job", 409: "Illegal state transition",
            413: "Body too large", 422: "Schema or semantic error"}
    return {str(c): _json(ErrorResponse, text[c]) for c in codes}


def build() -> dict:
    _, defs = models_json_schema([(m, "validation") for m in MODELS], ref_template=REF)
    job_id = [{"name": "job_id", "in": "path", "required": True,
               "schema": {"type": "string", "minLength": 1, "maxLength": 128}}]
    paths = {
        "/v1/capabilities": {"get": {
            "operationId": "getCapabilities",
            "responses": {"200": _json(Capabilities, "Protocol version and capabilities")}}},
        "/v1/prepare": {"post": {
            "operationId": "prepare",
            "requestBody": {"required": True, "content": {"application/json": {"schema": _ref(PrepareRequest)}}},
            "responses": {"202": _json(AsyncAccepted, "Index build accepted"), **_errors(400, 413, 422)}}},
        "/v1/jobs/{job_id}": {"get": {
            "operationId": "getJob", "parameters": job_id,
            "responses": {"200": _json(JobStatus, "Lookup succeeded (the job itself may have failed)"),
                          **_errors(404)}}},
        "/v1/query": {"post": {
            "operationId": "query",
            "requestBody": {"required": True, "content": {"application/json": {"schema": _ref(QueryRequest)}}},
            "responses": {"200": _json(QueryResponse, "Synchronous answer"),
                          "202": _json(AsyncAccepted, "Asynchronous query accepted"),
                          **_errors(400, 413, 422)}}},
        "/v1/jobs/{job_id}/cancel": {"post": {
            "operationId": "cancelJob", "parameters": job_id,
            "responses": {"202": _json(JobStatus, "Cancellation requested; poll the job for its final status"),
                          **_errors(404, 409)}}},
    }
    return {"openapi": "3.1.0",
            "info": {"title": "LAB method service protocol", "version": PROTOCOL_VERSION,
                     "description": "Implemented by participant methods; called by LAB (SPEC 7.2, 16.3)."},
            "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
            "paths": paths,
            "components": {"schemas": defs["$defs"]}}
