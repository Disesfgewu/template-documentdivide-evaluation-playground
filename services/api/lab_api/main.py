"""LAB API application factory.

Run: ``uvicorn --factory lab_api.main:create_app`` (from services/api).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .readiness import Check, default_checks, run_checks

API_VERSION = "0.1.0"


def create_app(settings: Settings | None = None, checks: dict[str, Check] | None = None) -> FastAPI:
    settings = settings or load_settings()
    checks = checks if checks is not None else default_checks(settings)

    app = FastAPI(title="document-seperater-playground LAB API", version=API_VERSION)
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict:
        # Liveness only: the process is up and serving. Dependencies are /readyz.
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        ready, results = run_checks(checks)
        body = {
            "status": "ready" if ready else "not_ready",
            "env": settings.env.value,
            "provider_mode": settings.provider_mode.value,
            "checks": results,
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    return app
