"""Bounded, single-attempt method-service transport (SPEC 7.2, 16.3).

An operator supplies one IP-literal origin: requests cannot change the target,
use environment proxies, resolve arbitrary hostnames, or follow redirects.
The caller owns durable intent, retry, cancellation and scheduler state.
"""
from __future__ import annotations

import http.client
import ipaddress
import math
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from lab_contracts.base import Contract
from lab_contracts.method import (
    AsyncAccepted, Capabilities, JobStatus, PrepareRequest, PrepareResult,
    QueryRequest, QueryResponse, StrictJSONError, strict_loads,
)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class MethodClientError(Exception):
    """Stable sanitized code only; remote content and URLs are never included."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MethodClient:
    def __init__(self, origin: str, *, timeout_seconds: float = 30,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        try:
            if not isinstance(origin, str):
                raise ValueError
            parsed = urlsplit(origin)
            ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
            valid = (parsed.scheme in {"http", "https"} and parsed.username is None
                     and parsed.password is None and parsed.path in {"", "/"}
                     and not parsed.query and not parsed.fragment and port != 0
                     and not any(c.isspace() for c in origin) and "%" not in (parsed.hostname or ""))
        except ValueError:
            valid = False
        if not valid:
            raise MethodClientError("invalid_origin")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= 180 or not math.isfinite(timeout_seconds)):
            raise MethodClientError("invalid_timeout")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise MethodClientError("invalid_response_limit")
        self._scheme, self._host, self._port = parsed.scheme, parsed.hostname, port
        self._timeout = timeout_seconds
        self._limit = max_response_bytes

    def _exchange(self, method: str, path: str, body: Contract | None,
                  responses: dict[int, type[Contract]], *, deadline: float | None = None) -> Contract:
        payload = body.model_dump_json().encode("utf-8") if body is not None else None
        if payload is not None and len(payload) > MAX_RESPONSE_BYTES:
            raise MethodClientError("request_too_large")
        now = time.monotonic()
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                                     or not math.isfinite(deadline)):
            raise MethodClientError("invalid_deadline")
        deadline = min(deadline, now + self._timeout) if deadline is not None else now + self._timeout
        if deadline <= now:
            raise MethodClientError("timeout")
        port = self._port or (443 if self._scheme == "https" else 80)
        conn = http.client.HTTPConnection(self._host, port, timeout=deadline - now)
        expired = threading.Event()
        connected_socket = None

        def expire():
            expired.set()
            try:
                if connected_socket is not None:
                    connected_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = None
        try:
            # No hostname resolution policy is needed: origin is an IP literal.
            # TLS uses the standard certificate verifier, never an insecure context.
            conn.connect()
            connected_socket = conn.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MethodClientError("timeout")
            conn.sock.settimeout(remaining)
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            if self._scheme == "https":
                # Separate TCP from TLS so both share one remaining deadline.
                # Start the watchdog before the handshake, then capture its SSL
                # socket (wrap without handshake first) for shutdown on expiry.
                connected_socket = ssl.create_default_context().wrap_socket(
                    connected_socket, server_hostname=self._host,
                    do_handshake_on_connect=False)
                conn.sock = connected_socket
                connected_socket.settimeout(max(0.001, deadline - time.monotonic()))
                connected_socket.do_handshake()
            conn.request(method, path, body=payload,
                         headers={"Accept": "application/json", "Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status not in responses:
                raise MethodClientError("unexpected_http_status")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise MethodClientError("invalid_content_type")
            # read(limit+1) remains bounded for chunked and absent Content-Length.
            raw = response.read(self._limit + 1)
            if expired.is_set() or time.monotonic() >= deadline:
                raise MethodClientError("timeout")
            if len(raw) > self._limit:
                raise MethodClientError("response_too_large")
            if response.length is not None and response.length > 0:
                # read(amt) does not raise IncompleteRead for a short body.
                raise MethodClientError("incomplete_response")
            try:
                strict_loads(raw.decode("utf-8"))
                result = responses[response.status].model_validate_json(raw)
                if expired.is_set() or time.monotonic() >= deadline:
                    raise MethodClientError("timeout")
                return result
            except (UnicodeDecodeError, StrictJSONError, ValidationError, ValueError):
                raise MethodClientError("invalid_response") from None
        except MethodClientError:
            raise
        except (socket.timeout, TimeoutError):
            raise MethodClientError("timeout") from None
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise MethodClientError("timeout" if expired.is_set() else "transport_error") from None
        finally:
            if timer is not None:
                timer.cancel()
            conn.close()

    def capabilities(self) -> Capabilities:
        return self._exchange("GET", "/v1/capabilities", None, {200: Capabilities})

    def prepare(self, request: PrepareRequest) -> AsyncAccepted:
        return self._exchange("POST", "/v1/prepare", request, {202: AsyncAccepted})

    def query(self, request: QueryRequest, *, deadline: float) -> QueryResponse | AsyncAccepted:
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            raise MethodClientError("invalid_deadline")
        result = self._exchange("POST", "/v1/query", request,
                                {200: QueryResponse, 202: AsyncAccepted},
                                deadline=min(deadline, time.monotonic() + request.budget.timeout_seconds))
        if isinstance(result, QueryResponse) and result.check_binding(request):
            raise MethodClientError("response_binding_mismatch")
        return result

    @staticmethod
    def _job_path(job_id: str) -> str:
        if not isinstance(job_id, str) or not 1 <= len(job_id) <= 128 or job_id in {".", ".."}:
            raise MethodClientError("invalid_job_id")
        # Opaque IDs may contain path punctuation: encode every delimiter.
        try:
            return "/v1/jobs/" + quote(job_id, safe="")
        except UnicodeError:
            raise MethodClientError("invalid_job_id") from None

    def _job(self, method: str, job_id: str, request: PrepareRequest | QueryRequest,
             cancel: bool, deadline: float) -> JobStatus:
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            raise MethodClientError("invalid_deadline")
        path = self._job_path(job_id) + ("/cancel" if cancel else "")
        result = self._exchange(method, path, None, {202 if cancel else 200: JobStatus}, deadline=deadline)
        kind = "prepare" if isinstance(request, PrepareRequest) else "query"
        if result.job_id != job_id or result.kind != kind:
            raise MethodClientError("response_binding_mismatch")
        if result.status == "succeeded":
            terminal = result.result
            if isinstance(terminal, PrepareResult):
                reasons = terminal.check_against_request(request)
            else:
                reasons = terminal.check_binding(request)
            if reasons:
                raise MethodClientError("response_binding_mismatch")
        return result

    def poll(self, job_id: str, request: PrepareRequest | QueryRequest, *, deadline: float) -> JobStatus:
        """Exactly one GET; the durable scheduler owns bounded polling cadence."""
        return self._job("GET", job_id, request, False, deadline)

    def cancel(self, job_id: str, request: PrepareRequest | QueryRequest, *, deadline: float) -> JobStatus:
        """One cancellation request; its response does not imply cancellation finished."""
        return self._job("POST", job_id, request, True, deadline)
