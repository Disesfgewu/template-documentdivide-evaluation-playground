"""Observe actual ASGI completion, including streamed bodies and trailers."""
import asyncio
import time
import uuid
from .app_log import ContextScope, log_event

REQUEST_ID_HEADER = b"x-lab-request-id"
# Probes that run every few seconds: a success is not logged (it would push real events out of the rotated files, ~57k lines/day);
# only a CHANGE of state is (the first failure, and the first success after a failure).
QUIET_PATHS = frozenset({"/healthz", "/readyz"})
_last_probe_ok: dict[str, bool] = {}


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
            kind = message.get("type")
            if kind == "http.response.start":
                # the id that is in app.log, so the page can show it next to an error and the owner can grep for it
                message = {**message, "headers": [*message.get("headers", []), (REQUEST_ID_HEADER, request_id.encode())]}
            await send(message)
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

        quiet = scope.get("path") in QUIET_PATHS
        with ContextScope(service="api", request_id=request_id, stage="api"):
            if not quiet:
                event("CALL", "started")
            try:
                await self.app(scope, observe_receive, observe_send)
            except BaseException as exc:
                code = "request_cancelled" if isinstance(exc, asyncio.CancelledError) else "request_failed"
                _last_probe_ok[scope.get("path")] = False
                event("ERROR", f"{type(exc).__name__}: {code}")
                raise
            if body_complete and (not trailers_expected or trailers_complete):
                if quiet:
                    ok = isinstance(status, int) and status < 400
                    if _last_probe_ok.get(scope["path"], True) != ok:
                        event("RETURN" if ok else "ERROR", "probe_recovered" if ok else "probe_failing")
                    _last_probe_ok[scope["path"]] = ok
                else:
                    event("RETURN", "ok")
            else:
                event("ERROR", "request_disconnected" if disconnected else "incomplete_response")
