"""LAB parser service factory. Run: ``uvicorn --factory lab_parser.main:create_app`` (from services/parser)."""
from __future__ import annotations

import os
import tempfile

from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .adapters import capabilities
from .config import Settings, load_settings
from .importer import Limits, inspect_pdf
from .pages import extract_pages

SERVICE_VERSION = "0.1.0"


def artifact_root_status(settings: Settings) -> dict:
    root = settings.artifact_root
    if not root.is_dir():
        return {"ok": False, "detail": "artifact root does not exist or is not a directory"}
    try:
        fd, name = tempfile.mkstemp(prefix=".readyz-", dir=root)
        os.close(fd)
        os.unlink(name)
    except OSError as exc:
        return {"ok": False, "detail": f"artifact root is not writable ({type(exc).__name__})"}
    return {"ok": True, "detail": "writable"}


def create_app(settings: Settings | None = None, limits: Limits | None = None) -> FastAPI:
    settings = settings or load_settings()
    limits = limits or Limits()
    app = FastAPI(title="document-seperater-playground LAB parser", version=SERVICE_VERSION)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        # Ready means "can accept artifacts". Parser backends are reported by
        # /v1/capabilities; none is implemented yet (#8), so parse requests
        # are not offered at all rather than answered with fake output.
        storage = artifact_root_status(settings)
        body = {"status": "ready" if storage["ok"] else "not_ready",
                "checks": {"artifact_storage": storage}}
        return JSONResponse(body, status_code=200 if storage["ok"] else 503)

    @app.get("/v1/capabilities")
    def caps() -> dict:
        return {"service_version": SERVICE_VERSION, "parsers": capabilities()}

    @app.post("/v1/inspect")
    async def inspect(request: Request) -> JSONResponse:
        """Import check + page geometry for one PDF (SPEC R01, 4.1). This is
        not parsing: no text, tables or images are extracted here (#8)."""
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/pdf":
            return JSONResponse({"error": {"code": "unsupported_media_type", "message": "send the PDF bytes as application/pdf",
                                           "retryable": False, "request_id": "-", "details": {}}}, status_code=415)
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limits.max_bytes:
            return _too_large()
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limits.max_bytes:
                return _too_large()
        result = inspect_pdf(bytes(body), limits)
        pages = []
        if result.outcome != "reject":
            pages = [asdict(p) for p in extract_pages(bytes(body))]
        return JSONResponse({"inspection": asdict(result), "pages": pages}, status_code=200)

    def _too_large() -> JSONResponse:
        return JSONResponse({"error": {"code": "payload_too_large", "message": "PDF exceeds the configured size limit",
                                       "retryable": False, "request_id": "-", "details": {"max_bytes": limits.max_bytes}}},
                            status_code=413)

    return app
