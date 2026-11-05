"""LAB parser service factory. Run: ``uvicorn --factory lab_parser.main:create_app`` (from services/parser)."""
from __future__ import annotations

import os
import tempfile

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .adapters import capabilities
from .config import Settings, load_settings

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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
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

    return app
