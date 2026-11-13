"""LAB API application factory.

Run: ``uvicorn --factory lab_api.main:create_app`` (from services/api).
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .audit import router as audit_router
from .benchmark_baseline import router as benchmark_baseline_router
from .benchmark_build import router as benchmark_build_router
from .benchmark_freeze import router as benchmark_freeze_router
from .benchmark_generate import router as benchmark_generate_router
from .benchmark_method import router as benchmark_method_router
from .benchmarks import router as benchmarks_router
from .config import Settings, load_settings
from .documents import ApiError, DownloadAdmissionMiddleware, router as documents_router
from .openapi import configure_openapi
from .comparisons import router as comparisons_router
from .logs import router as logs_router
from .results import router as results_router
from .runs import router as runs_router
from .jobs import router as jobs_router
from .parse_jobs import router as parse_jobs_router
from .parse_results import router as parse_results_router
from .lineage import router as lineage_router
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
    app.add_middleware(DownloadAdmissionMiddleware)
    app.state.settings = settings
    app.add_middleware(
        TraceContextMiddleware,
        trust_remote_traceparent=trust_traceparent,
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        req_id = request.headers.get("X-Request-Id", "req-1")
        return exc.to_error_response(request_id=req_id)

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

    app.include_router(documents_router)
    app.include_router(audit_router)
    app.include_router(logs_router)
    app.include_router(runs_router)
    app.include_router(results_router)
    app.include_router(comparisons_router)
    app.include_router(jobs_router)
    app.include_router(parse_jobs_router)
    app.include_router(parse_results_router)
    app.include_router(lineage_router)
    app.include_router(benchmarks_router)
    app.include_router(benchmark_build_router)
    app.include_router(benchmark_generate_router)
    app.include_router(benchmark_freeze_router)
    app.include_router(benchmark_baseline_router)
    app.include_router(benchmark_method_router)

    configure_openapi(app)

    return app
