"""ASGI integration and adversarial leakage tests for LAB API request logging."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unittest
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from lab_api.config import Environment, ProviderMode, load_settings
from lab_api.main import create_app
from lab_api.readiness import CheckResult
from lab_api.request_logging import (
    ALLOWED_ERROR_TYPES,
    ALLOWED_EVENT_NAMES,
    ALLOWED_HTTP_METHODS,
    ALLOWED_LEVELS,
    ALLOWED_ROUTES,
    COMPONENT_NAME,
    ERROR_CODE_CANCELLED,
    ERROR_CODE_HTTP_5XX,
    ERROR_CODE_INCOMPLETE,
    ERROR_CODE_INVALID_STATUS,
    ERROR_CODE_TIMEOUT,
    ERROR_CODE_UNHANDLED,
    ERROR_TYPE_BASE_EXCEPTION,
    ERROR_TYPE_CANCELLED,
    ERROR_TYPE_EXCEPTION,
    ERROR_TYPE_KEY,
    ERROR_TYPE_OS,
    ERROR_TYPE_RUNTIME,
    ERROR_TYPE_TIMEOUT,
    ERROR_TYPE_TYPE,
    ERROR_TYPE_VALUE,
    EVENT_COMPLETED,
    EVENT_FAILED,
    INVALID_HTTP_METHOD,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARN,
    LOGGER_NAME,
    OUTCOME_ABORTED,
    OUTCOME_CLIENT_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_OK,
    OUTCOME_SERVER_ERROR,
    REQUEST_LOG_FIELDS,
    ROUTE_HEALTHZ,
    ROUTE_READYZ,
    ROUTE_UNMATCHED,
    SCHEMA_VERSION,
    SERVICE_NAME,
    TELEMETRY_ORIGIN,
    RequestLogEvent,
    RequestLoggingMiddleware,
    classify_exception,
    classify_route,
    sanitize_http_method,
    sanitize_http_status,
)

DB_SECRET = "s3cret-db-password-canary"
DB_HOST = "internal-db-canary.local"
ENV = {
    "LAB_ENV": "test",
    "LAB_PROVIDER_MODE": "mock",
    "LAB_DATABASE_URL": f"postgresql://lab:{DB_SECRET}@{DB_HOST}:5432/lab",
    "LAB_ARTIFACT_ROOT": "/nonexistent-lab-artifacts-canary",
    "LAB_DB_CONNECT_TIMEOUT_S": "2",
}

HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def make_settings(**overrides: str):
    with mock.patch.dict(os.environ, {**ENV, **overrides}, clear=True):
        return load_settings()


async def run_raw_asgi(
    app: Any,
    scope: dict[str, Any],
    receive_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    messages = list(receive_messages or [{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    return sent


class RequestLoggingIntegrationTests(unittest.TestCase):
    def assert_valid_event_structure(self, event: RequestLogEvent) -> dict[str, Any]:
        payload = event.to_dict()
        self.assertEqual(tuple(payload.keys()), REQUEST_LOG_FIELDS)
        parsed = json.loads(event.to_json())
        self.assertEqual(parsed, payload)
        self.assertRegex(event.event_id, HEX32_RE)
        self.assertRegex(event.request_id, HEX32_RE)
        self.assertNotEqual(event.event_id, event.request_id)
        self.assertEqual(event.schema_version, SCHEMA_VERSION)
        self.assertRegex(event.occurred_at, ISO_UTC_RE)
        self.assertRegex(event.observed_at, ISO_UTC_RE)
        self.assertIn(event.level, ALLOWED_LEVELS)
        self.assertIn(event.event_name, ALLOWED_EVENT_NAMES)
        self.assertEqual(event.service, SERVICE_NAME)
        self.assertEqual(event.component, COMPONENT_NAME)
        self.assertIn(event.route, ALLOWED_ROUTES)
        self.assertIsInstance(event.duration_ms, float)
        self.assertGreaterEqual(event.duration_ms, 0.0)
        self.assertEqual(event.telemetry_origin, TELEMETRY_ORIGIN)
        if event.error_type is not None:
            self.assertIn(event.error_type, ALLOWED_ERROR_TYPES)
        return payload

    def test_healthz_emits_bounded_info_event_and_preserves_response(self):
        events: list[RequestLogEvent] = []
        app = create_app(
            make_settings(),
            checks={"database": lambda: CheckResult(False, "down")},
            log_sink=events.append,
        )
        client = TestClient(app)
        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assert_valid_event_structure(ev)
        self.assertEqual(ev.level, LEVEL_INFO)
        self.assertEqual(ev.event_name, EVENT_COMPLETED)
        self.assertEqual(ev.env, "test")
        self.assertEqual(ev.provider_mode, "mock")
        self.assertEqual(ev.http_method, "GET")
        self.assertEqual(ev.route, ROUTE_HEALTHZ)
        self.assertEqual(ev.operation_name, "http.healthz")
        self.assertEqual(ev.status, OUTCOME_OK)
        self.assertEqual(ev.http_status, 200)
        self.assertIsNone(ev.error_code)
        self.assertIsNone(ev.error_type)

    def test_readyz_200_and_503_emit_distinct_bounded_events(self):
        events: list[RequestLogEvent] = []
        ok_app = create_app(
            make_settings(),
            checks={"db": lambda: CheckResult(True, "ok")},
            log_sink=events.append,
        )
        r_ok = TestClient(ok_app).get("/readyz")
        self.assertEqual(r_ok.status_code, 200)

        def crashing_check() -> CheckResult:
            raise RuntimeError("check-secret-canary-999")

        fail_app = create_app(
            make_settings(),
            checks={"db": crashing_check},
            log_sink=events.append,
        )
        r_fail = TestClient(fail_app).get("/readyz")
        self.assertEqual(r_fail.status_code, 503)
        self.assertNotIn("check-secret-canary-999", r_fail.text)

        self.assertEqual(len(events), 2)
        ev_ok, ev_fail = events
        self.assert_valid_event_structure(ev_ok)
        self.assertEqual(ev_ok.route, ROUTE_READYZ)
        self.assertEqual(ev_ok.operation_name, "http.readyz")
        self.assertEqual(ev_ok.level, LEVEL_INFO)
        self.assertEqual(ev_ok.event_name, EVENT_COMPLETED)
        self.assertEqual(ev_ok.status, OUTCOME_OK)
        self.assertEqual(ev_ok.http_status, 200)
        self.assertIsNone(ev_ok.error_code)

        self.assert_valid_event_structure(ev_fail)
        self.assertEqual(ev_fail.route, ROUTE_READYZ)
        self.assertEqual(ev_fail.operation_name, "http.readyz")
        self.assertEqual(ev_fail.level, LEVEL_ERROR)
        self.assertEqual(ev_fail.event_name, EVENT_FAILED)
        self.assertEqual(ev_fail.status, OUTCOME_SERVER_ERROR)
        self.assertEqual(ev_fail.http_status, 503)
        self.assertEqual(ev_fail.error_code, ERROR_CODE_HTTP_5XX)
        self.assertIsNone(ev_fail.error_type)
        self.assertNotIn("check-secret-canary-999", ev_fail.to_json())

    def test_unmatched_route_and_405_method_not_allowed(self):
        events: list[RequestLogEvent] = []
        app = create_app(
            make_settings(),
            checks={"db": lambda: CheckResult(True, "ok")},
            log_sink=events.append,
        )
        client = TestClient(app)

        r_404 = client.get("/v1/projects/secret-proj-id/runs?token=top-secret")
        self.assertEqual(r_404.status_code, 404)

        r_405 = client.post("/healthz", content=b'{"api_key":"sk-live-secret"}')
        self.assertEqual(r_405.status_code, 405)

        self.assertEqual(len(events), 2)
        ev_404, ev_405 = events
        self.assert_valid_event_structure(ev_404)
        self.assertEqual(ev_404.route, ROUTE_UNMATCHED)
        self.assertEqual(ev_404.operation_name, "http.unmatched")
        self.assertEqual(ev_404.level, LEVEL_WARN)
        self.assertEqual(ev_404.event_name, EVENT_COMPLETED)
        self.assertEqual(ev_404.status, OUTCOME_CLIENT_ERROR)
        self.assertEqual(ev_404.http_status, 404)
        self.assertIsNone(ev_404.error_code)

        self.assert_valid_event_structure(ev_405)
        self.assertEqual(ev_405.http_method, "POST")
        self.assertEqual(ev_405.route, ROUTE_HEALTHZ)
        self.assertEqual(ev_405.level, LEVEL_WARN)
        self.assertEqual(ev_405.status, OUTCOME_CLIENT_ERROR)
        self.assertEqual(ev_405.http_status, 405)

    def test_default_log_sink_writes_json_to_named_logger(self):
        app = create_app(
            make_settings(),
            checks={"db": lambda: CheckResult(True, "ok")},
        )
        client = TestClient(app)
        with self.assertLogs(LOGGER_NAME, level="INFO") as cm:
            resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(cm.records), 1)
        record = cm.records[0]
        self.assertEqual(record.levelno, logging.INFO)
        data = json.loads(record.getMessage())
        self.assertEqual(tuple(sorted(data.keys())), tuple(sorted(REQUEST_LOG_FIELDS)))
        self.assertEqual(data["route"], ROUTE_HEALTHZ)
        self.assertEqual(data["http_status"], 200)


class AdversarialLeakageAndBoundaryTests(unittest.TestCase):
    def test_adversarial_request_never_leaks_paths_queries_headers_cookies_or_body(self):
        events: list[RequestLogEvent] = []
        app = create_app(
            make_settings(),
            checks={"db": lambda: CheckResult(False, "down")},
            log_sink=events.append,
        )
        client = TestClient(app)

        canaries = (
            DB_SECRET,
            DB_HOST,
            "nonexistent-lab-artifacts-canary",
            "Bearer-super-secret-jwt-token",
            "sk-adversarial-api-key-999999",
            "session-cookie-secret-val",
            "url-query-token-canary",
            "raw-path-secret-segment",
            "body-password-canary",
            "x-request-id-spoof-canary",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "client-host-canary.internal",
        )

        headers = {
            "Authorization": "Bearer Bearer-super-secret-jwt-token",
            "X-API-Key": "sk-adversarial-api-key-999999",
            "Cookie": "lab_session=session-cookie-secret-val",
            "X-Request-ID": "x-request-id-spoof-canary",
            "Traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        }
        client.get(
            "/healthz?access_token=url-query-token-canary&password=body-password-canary",
            headers=headers,
        )
        client.post(
            "/v1/documents/raw-path-secret-segment?key=url-query-token-canary",
            headers=headers,
            content=b'{"secret":"body-password-canary"}',
        )
        client.get(
            "/readyz?db=postgresql://u:url-query-token-canary@host/db",
            headers=headers,
        )

        self.assertEqual(len(events), 3)
        for ev in events:
            serialized = ev.to_json()
            for canary in canaries:
                self.assertNotIn(canary, serialized)
            # Remote X-Request-ID must never override the locally generated hex request_id.
            self.assertRegex(ev.request_id, HEX32_RE)

    def test_untrusted_http_methods_are_sanitized_to_invalid_method(self):
        for valid in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"):
            self.assertEqual(sanitize_http_method(valid), valid)
            self.assertIn(valid, ALLOWED_HTTP_METHODS)

        adversarial_methods = (
            "get",
            "GET ",
            " GET",
            "GET\r\nX-Injected: pwn",
            "POST\x00SECRET",
            "CONNECT",
            "TRACE",
            "PROPFIND",
            "Bearer-secret-token",
            "",
            "A" * 64,
            None,
            123,
            ["GET"],
            {"method": "GET"},
        )
        for bad in adversarial_methods:
            with self.subTest(method=repr(bad)):
                self.assertEqual(sanitize_http_method(bad), INVALID_HTTP_METHOD)

        events: list[RequestLogEvent] = []

        async def ok_asgi(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = RequestLoggingMiddleware(
            ok_asgi,
            env=Environment.test,
            provider_mode=ProviderMode.mock,
            sink=events.append,
        )
        scope = {
            "type": "http",
            "method": "GET\r\nAuthorization: Bearer secret-method-canary",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"token=secret-method-canary",
            "headers": [],
        }
        asyncio.run(run_raw_asgi(mw, scope))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].http_method, INVALID_HTTP_METHOD)
        self.assertNotIn("secret-method-canary", events[0].to_json())

    def test_untrusted_status_values_are_bounded_and_bool_is_rejected(self):
        self.assertEqual(sanitize_http_status(200), (200, True))
        self.assertEqual(sanitize_http_status(100), (100, True))
        self.assertEqual(sanitize_http_status(599), (599, True))

        for invalid in (True, False, None, 0, 99, 600, -200, 200.0, "200", "500 secret", [200], {"status": 200}):
            with self.subTest(status=repr(invalid)):
                self.assertEqual(sanitize_http_status(invalid), (None, False))

        events: list[RequestLogEvent] = []
        for bad_status in (True, "200-secret-canary", 999, 70):
            async def bad_status_app(scope, receive, send, st=bad_status):
                await send({"type": "http.response.start", "status": st, "headers": []})
                await send({"type": "http.response.body", "body": b""})

            mw = RequestLoggingMiddleware(
                bad_status_app,
                env=Environment.test,
                provider_mode=ProviderMode.mock,
                sink=events.append,
            )
            asyncio.run(run_raw_asgi(mw, {"type": "http", "method": "GET", "path": "/healthz"}))

        self.assertEqual(len(events), 4)
        for ev in events:
            self.assertIsNone(ev.http_status)
            self.assertEqual(ev.level, LEVEL_ERROR)
            self.assertEqual(ev.event_name, EVENT_FAILED)
            self.assertEqual(ev.status, OUTCOME_SERVER_ERROR)
            self.assertEqual(ev.error_code, ERROR_CODE_INVALID_STATUS)
            self.assertIsNone(ev.error_type)
            self.assertNotIn("200-secret-canary", ev.to_json())

    def test_incomplete_response_without_start_message_is_recorded_as_aborted(self):
        events: list[RequestLogEvent] = []

        async def silent_app(scope, receive, send):
            return

        mw = RequestLoggingMiddleware(
            silent_app,
            env=Environment.test,
            provider_mode=ProviderMode.mock,
            sink=events.append,
        )
        asyncio.run(run_raw_asgi(mw, {"type": "http", "method": "GET", "path": "/healthz"}))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsNone(ev.http_status)
        self.assertEqual(ev.level, LEVEL_ERROR)
        self.assertEqual(ev.event_name, EVENT_FAILED)
        self.assertEqual(ev.status, OUTCOME_ABORTED)
        self.assertEqual(ev.error_code, ERROR_CODE_INCOMPLETE)
        self.assertIsNone(ev.error_type)

    def test_exceptions_are_distinguished_reraised_and_never_leak_messages_or_custom_class_names(self):
        class CustomSecretException(Exception):
            pass

        class CustomSecretBaseException(BaseException):
            pass

        cases: list[tuple[BaseException, str, str, str]] = [
            (
                asyncio.CancelledError("cancel-secret-canary"),
                ERROR_CODE_CANCELLED,
                ERROR_TYPE_CANCELLED,
                OUTCOME_ABORTED,
            ),
            (
                TimeoutError("timeout-secret-canary"),
                ERROR_CODE_TIMEOUT,
                ERROR_TYPE_TIMEOUT,
                OUTCOME_EXCEPTION,
            ),
            (
                ValueError("value-secret-canary"),
                ERROR_CODE_UNHANDLED,
                ERROR_TYPE_VALUE,
                OUTCOME_EXCEPTION,
            ),
            (
                TypeError("type-secret-canary"),
                ERROR_CODE_UNHANDLED,
                ERROR_TYPE_TYPE,
                OUTCOME_EXCEPTION,
            ),
            (
                KeyError("key-secret-canary"),
                ERROR_CODE_UNHANDLED,
                ERROR_TYPE_KEY,
                OUTCOME_EXCEPTION,
            ),
            (
                RuntimeError("runtime-secret-canary"),
                ERROR_CODE_UNHANDLED,
                ERROR_TYPE_RUNTIME,
                OUTCOME_EXCEPTION,
            ),
            (
                OSError("os-secret-canary"),
                ERROR_CODE_UNHANDLED,
                ERROR_TYPE_OS,
                OUTCOME_EXCEPTION,
            ),
            (
                CustomSecretException("custom-exc-secret-canary"),
                ERROR_CODE_UNHANDLED,
                ERROR_TYPE_EXCEPTION,
                OUTCOME_EXCEPTION,
            ),
            (
                CustomSecretBaseException("custom-base-secret-canary"),
                ERROR_CODE_UNHANDLED,
                ERROR_TYPE_BASE_EXCEPTION,
                OUTCOME_EXCEPTION,
            ),
        ]

        for exc_instance, expected_code, expected_type, expected_status in cases:
            with self.subTest(exc=type(exc_instance).__name__):
                self.assertEqual(classify_exception(exc_instance), (expected_code, expected_type))
                events: list[RequestLogEvent] = []

                async def raising_app(scope, receive, send, to_raise=exc_instance):
                    raise to_raise

                mw = RequestLoggingMiddleware(
                    raising_app,
                    env=Environment.test,
                    provider_mode=ProviderMode.mock,
                    sink=events.append,
                )
                with self.assertRaises(type(exc_instance)) as cm:
                    asyncio.run(
                        run_raw_asgi(
                            mw,
                            {
                                "type": "http",
                                "method": "GET",
                                "path": "/readyz",
                                "query_string": b"secret=query-canary",
                            },
                        )
                    )
                self.assertIs(cm.exception, exc_instance)
                self.assertEqual(len(events), 1)
                ev = events[0]
                self.assertEqual(ev.level, LEVEL_ERROR)
                self.assertEqual(ev.event_name, EVENT_FAILED)
                self.assertEqual(ev.status, expected_status)
                self.assertEqual(ev.error_code, expected_code)
                self.assertEqual(ev.error_type, expected_type)
                self.assertIsNone(ev.http_status)
                serialized = ev.to_json()
                self.assertNotIn("secret-canary", serialized)
                self.assertNotIn("CustomSecret", serialized)
                self.assertNotIn("query-canary", serialized)

    def test_exception_after_response_start_records_exception_and_valid_http_status(self):
        events: list[RequestLogEvent] = []

        async def mid_stream_boom(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RuntimeError("mid-stream-secret-canary")

        mw = RequestLoggingMiddleware(
            mid_stream_boom,
            env=Environment.test,
            provider_mode=ProviderMode.mock,
            sink=events.append,
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(run_raw_asgi(mw, {"type": "http", "method": "GET", "path": "/healthz"}))

        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.level, LEVEL_ERROR)
        self.assertEqual(ev.event_name, EVENT_FAILED)
        self.assertEqual(ev.status, OUTCOME_EXCEPTION)
        self.assertEqual(ev.http_status, 200)
        self.assertEqual(ev.error_code, ERROR_CODE_UNHANDLED)
        self.assertEqual(ev.error_type, ERROR_TYPE_RUNTIME)
        self.assertNotIn("mid-stream-secret-canary", ev.to_json())

    def test_sink_failure_increments_dropped_count_and_preserves_response_and_exceptions(self):
        def broken_sink(event: RequestLogEvent) -> None:
            raise RuntimeError("sink-failure-secret")

        async def ok_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"status":"ok"}'})

        mw = RequestLoggingMiddleware(
            ok_app,
            env=Environment.test,
            provider_mode=ProviderMode.mock,
            sink=broken_sink,
        )
        sent = asyncio.run(run_raw_asgi(mw, {"type": "http", "method": "GET", "path": "/healthz"}))
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(mw.dropped_count, 1)

        async def failing_app(scope, receive, send):
            raise ValueError("app-failure-canary")

        mw_fail = RequestLoggingMiddleware(
            failing_app,
            env=Environment.test,
            provider_mode=ProviderMode.mock,
            sink=broken_sink,
        )
        with self.assertRaises(ValueError) as cm:
            asyncio.run(run_raw_asgi(mw_fail, {"type": "http", "method": "GET", "path": "/healthz"}))
        self.assertIn("app-failure-canary", str(cm.exception))
        self.assertEqual(mw_fail.dropped_count, 1)

    def test_non_http_lifespan_scope_passes_through_without_emitting_request_log(self):
        events: list[RequestLogEvent] = []
        called = False

        async def lifespan_app(scope, receive, send):
            nonlocal called
            called = True
            self.assertEqual(scope["type"], "lifespan")

        mw = RequestLoggingMiddleware(
            lifespan_app,
            env=Environment.test,
            provider_mode=ProviderMode.mock,
            sink=events.append,
        )
        asyncio.run(run_raw_asgi(mw, {"type": "lifespan"}))
        self.assertTrue(called)
        self.assertEqual(events, [])

    def test_caller_scope_state_is_not_mutated_and_request_id_is_exposed_downstream(self):
        events: list[RequestLogEvent] = []
        seen_request_id: str | None = None

        async def inspecting_app(scope, receive, send):
            nonlocal seen_request_id
            seen_request_id = scope["state"]["request_id"]
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = RequestLoggingMiddleware(
            inspecting_app,
            env=Environment.test,
            provider_mode=ProviderMode.mock,
            sink=events.append,
        )
        original_state = {"existing": "value"}
        scope = {"type": "http", "method": "GET", "path": "/healthz", "state": original_state}
        asyncio.run(run_raw_asgi(mw, scope))
        self.assertEqual(original_state, {"existing": "value"})
        self.assertIsNotNone(seen_request_id)
        self.assertEqual(events[0].request_id, seen_request_id)

    def test_route_classification_rejects_unallowlisted_route_templates(self):
        FakeRoute = type("FakeRoute", (), {"path": "/v1/projects/{project_id}/secret"})
        scope = {"type": "http", "path": "/v1/projects/123/secret", "route": FakeRoute()}
        self.assertEqual(classify_route(scope), ROUTE_UNMATCHED)

        GoodRoute = type("GoodRoute", (), {"path": "/readyz"})
        self.assertEqual(classify_route({"type": "http", "route": GoodRoute()}), ROUTE_READYZ)

    def test_middleware_constructor_rejects_non_enum_settings(self):
        async def dummy(scope, receive, send):
            return

        with self.assertRaises(TypeError):
            RequestLoggingMiddleware(dummy, env="test", provider_mode=ProviderMode.mock)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            RequestLoggingMiddleware(dummy, env=Environment.test, provider_mode="mock")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
