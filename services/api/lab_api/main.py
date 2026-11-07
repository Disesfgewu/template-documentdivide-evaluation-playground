"""LAB API application factory.

Run: ``uvicorn --factory lab_api.main:create_app`` (from services/api).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .readiness import Check, default_checks, run_checks
from .trace_context import TraceContextMiddleware

API_VERSION = "0.1.0"


class _TraceCorrelatedFastAPI(FastAPI):
    """FastAPI subclass that places TraceContextMiddleware outside ServerErrorMiddleware.

    Starlette's default ``build_middleware_stack`` wraps ``ServerErrorMiddleware``
    outside ``user_middleware`` (``add_middleware``), so unhandled endpoint
    exceptions turned into HTTP 500 responses bypass ``add_middleware`` wrappers.
    Wrapping the built stack in ``TraceContextMiddleware`` ensures 500 responses
    and custom 500 exception handlers still receive the request's correlation
    state and emit ``X-Request-ID`` and ``traceparent`` headers while preserving
    ``ServerErrorMiddleware``'s re-raise semantics.
    """

    _trust_remote_traceparent: bool = False

    def build_middleware_stack(self):
        stack = super().build_middleware_stack()
        return TraceContextMiddleware(
            stack,
            trust_remote_traceparent=self._trust_remote_traceparent,
        )


def create_app(
    settings: Settings | None = None,
    checks: dict[str, Check] | None = None,
    *,
    trust_remote_traceparent: bool | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    checks = checks if checks is not None else default_checks(settings)
    if trust_remote_traceparent is not None and type(trust_remote_traceparent) is not bool:
        raise TypeError("trust_remote_traceparent must be a bool or None")
    trust_traceparent = (
        settings.trust_remote_traceparent
        if trust_remote_traceparent is None
        else trust_remote_traceparent
    )

    app = _TraceCorrelatedFastAPI(
        title="document-seperater-playground LAB API",
        version=API_VERSION,
    )
    app._trust_remote_traceparent = trust_traceparent
    app.state.settings = settings
    app.add_middleware(
        TraceContextMiddleware,
        trust_remote_traceparent=trust_traceparent,
    )

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
