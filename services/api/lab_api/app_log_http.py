"""Observe actual ASGI completion, including streamed bodies and trailers."""
import asyncio
import time
import uuid
from .app_log import ContextScope, log_event


class AppLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        status = None
        body_complete = False
        trailers_expected = False
        trailers_complete = False
        disconnected = False
        request_id = "req-" + uuid.uuid4().hex
        started = time.monotonic()

        async def observe_receive():
            nonlocal disconnected
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnected = True
            return message

        async def observe_send(message):
            nonlocal status, body_complete, trailers_expected, trailers_complete
            # A failed send is not a completed response. Preserve the original exception.
            await send(message)
            kind = message.get("type")
            if kind == "http.response.start":
                status = message.get("status")
                trailers_expected = message.get("trailers") is True
            elif kind == "http.response.body" and not message.get("more_body", False):
                body_complete = True
            elif kind == "http.response.trailers" and not message.get("more_trailers", False):
                trailers_complete = True

        def event(mode, reason):
            # the route template is only known once the router has matched: the CALL line has the method only
            extra = {} if mode == "CALL" else {"route": getattr(scope.get("route"), "path", "unmatched")}
            log_event(mode, "api_request", reason, stage="api", is_stage=True,
                      method=scope.get("method"), **extra,
                      return_val={"status_code": status, "duration_ms": round((time.monotonic() - started) * 1000)})

        with ContextScope(service="api", request_id=request_id, stage="api"):
            event("CALL", "started")
            try:
                await self.app(scope, observe_receive, observe_send)
            except BaseException as exc:
                code = "request_cancelled" if isinstance(exc, asyncio.CancelledError) else "request_failed"
                event("ERROR", f"{type(exc).__name__}: {code}")
                raise
            if body_complete and (not trailers_expected or trailers_complete):
                event("RETURN", "ok")
            else:
                event("ERROR", "request_disconnected" if disconnected else "incomplete_response")
