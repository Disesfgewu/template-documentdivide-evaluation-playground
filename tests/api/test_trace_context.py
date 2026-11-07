from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest import mock

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from lab_api.config import load_settings
from lab_api.main import create_app
from lab_api.readiness import CheckResult
from lab_api.trace_context import (
    REQUEST_ID_HEADER,
    TRACE_SCHEMA_VERSION,
    TRACEPARENT_HEADER,
    TraceContext,
    TraceContextError,
    TraceContextMiddleware,
    TraceParent,
    current_trace_context,
    new_request_id,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    request_trace_context,
    resolve_request_trace_context,
)

ENV = {
    "LAB_ENV": "test",
    "LAB_PROVIDER_MODE": "mock",
    "LAB_DATABASE_URL": "postgresql://lab:s3cret-pw@127.0.0.1:1/lab",
    "LAB_ARTIFACT_ROOT": "/nonexistent-lab-artifacts",
    "LAB_DB_CONNECT_TIMEOUT_S": "2",
}

VALID_TRACE_ID = "1234567890abcdef1234567890abcdef"
VALID_SPAN_ID = "1234567890abcdef"
VALID_TRACEPARENT_SAMPLED = f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01"
VALID_TRACEPARENT_UNSAMPLED = f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-00"

REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def make_settings(**overrides: str):
    with mock.patch.dict(os.environ, {**ENV, **overrides}, clear=True):
        return load_settings()


class StrictTraceparentValidationTests(unittest.TestCase):
    def test_valid_version_00_sampled_and_unsampled(self):
        sampled = parse_traceparent(VALID_TRACEPARENT_SAMPLED)
        self.assertEqual(sampled.trace_id, VALID_TRACE_ID)
        self.assertEqual(sampled.span_id, VALID_SPAN_ID)
        self.assertTrue(sampled.sampled)
        self.assertEqual(sampled.to_header(), VALID_TRACEPARENT_SAMPLED)

        unsampled = parse_traceparent(VALID_TRACEPARENT_UNSAMPLED)
        self.assertEqual(unsampled.trace_id, VALID_TRACE_ID)
        self.assertEqual(unsampled.span_id, VALID_SPAN_ID)
        self.assertFalse(unsampled.sampled)
        self.assertEqual(unsampled.to_header(), VALID_TRACEPARENT_UNSAMPLED)

    def test_malformed_traceparents_rejected_without_echoing_input(self):
        canary = "canary-secret-token-999"
        invalid_cases = {
            "empty": "",
            "non_string": None,
            "too_short": VALID_TRACEPARENT_SAMPLED[:-1],
            "too_long": VALID_TRACEPARENT_SAMPLED + "-extra",
            "oversized": "00-" + ("a" * 4096),
            "unsupported_version_01": f"01-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            "unsupported_version_ff": f"ff-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            "uppercase_trace": f"00-{VALID_TRACE_ID.upper()}-{VALID_SPAN_ID}-01",
            "uppercase_span": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID.upper()}-01",
            "uppercase_flags": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-0A",
            "zero_trace_id": f"00-{'0' * 32}-{VALID_SPAN_ID}-01",
            "zero_span_id": f"00-{VALID_TRACE_ID}-{'0' * 16}-01",
            "unsupported_flag_02": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-02",
            "unsupported_flag_03": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-03",
            "unsupported_flag_ff": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-ff",
            "leading_space": " " + VALID_TRACEPARENT_SAMPLED[:-1],
            "trailing_space": VALID_TRACEPARENT_SAMPLED[:-1] + " ",
            "embedded_newline": VALID_TRACEPARENT_SAMPLED[:53] + "\n1",
            "null_byte": VALID_TRACEPARENT_SAMPLED[:53] + "\x001",
            "wrong_delimiters": f"00_{VALID_TRACE_ID}_{VALID_SPAN_ID}_01",
            "non_hex_chars": f"00-{'g' * 32}-{VALID_SPAN_ID}-01",
            "comma_joined": f"{VALID_TRACEPARENT_SAMPLED},{VALID_TRACEPARENT_UNSAMPLED}",
            "canary_payload": f"00-{canary:<32.32}-{VALID_SPAN_ID}-01",
        }
        for name, raw in invalid_cases.items():
            with self.subTest(case=name):
                with self.assertRaises(TraceContextError) as cm:
                    parse_traceparent(raw)  # type: ignore[arg-type]
                self.assertNotIn(canary, str(cm.exception))
                if isinstance(raw, str) and len(raw) > 4:
                    self.assertNotIn(raw, str(cm.exception))

    def test_traceparent_dataclass_rejects_invalid_direct_construction(self):
        with self.assertRaises(TraceContextError):
            TraceParent(trace_id="0" * 32, span_id=VALID_SPAN_ID, sampled=True)
        with self.assertRaises(TraceContextError):
            TraceParent(trace_id=VALID_TRACE_ID, span_id="0" * 16, sampled=False)
        with self.assertRaises(TraceContextError):
            TraceParent(trace_id=VALID_TRACE_ID, span_id=VALID_SPAN_ID, sampled="yes")  # type: ignore[arg-type]

    def test_cryptographic_id_generators_format_and_uniqueness(self):
        req_ids = {new_request_id() for _ in range(64)}
        trace_ids = {new_trace_id() for _ in range(64)}
        span_ids = {new_span_id() for _ in range(64)}
        self.assertEqual(len(req_ids), 64)
        self.assertEqual(len(trace_ids), 64)
        self.assertEqual(len(span_ids), 64)
        for rid in req_ids:
            self.assertRegex(rid, REQUEST_ID_RE)
        for tid in trace_ids:
            self.assertRegex(tid, TRACE_ID_RE)
            self.assertNotEqual(tid, "0" * 32)
        for sid in span_ids:
            self.assertRegex(sid, SPAN_ID_RE)
            self.assertNotEqual(sid, "0" * 16)


class FastAPITraceContextTests(unittest.TestCase):
    def _build_observed_app(self, *, trust_remote: bool = False, setting_env: dict[str, str] | None = None):
        s = make_settings(**(setting_env or {}))
        app = create_app(
            s,
            checks={"db": lambda: CheckResult(True, "ok")},
            trust_remote_traceparent=trust_remote if setting_env is None else None,
        )
        captured: list[dict[str, Any]] = []

        @app.get("/_test/inspect")
        async def inspect_endpoint(request: Request) -> JSONResponse:
            ctx_from_req = request_trace_context(request)
            ctx_from_var = current_trace_context()
            captured.append(
                {
                    "req_ctx": ctx_from_req,
                    "var_ctx": ctx_from_var,
                    "state_request_id": request.state.request_id,
                    "state_trace_id": request.state.trace_id,
                    "state_span_id": request.state.span_id,
                    "state_parent_span_id": request.state.parent_span_id,
                    "incoming_traceparent": request.headers.get("traceparent"),
                    "incoming_tracestate": request.headers.get("tracestate"),
                    "incoming_baggage": request.headers.get("baggage"),
                    "incoming_x_request_id": request.headers.get("x-request-id"),
                }
            )
            return JSONResponse({"ok": True})

        return app, captured

    def test_default_untrusted_ingress_ignores_valid_remote_traceparent_and_request_id(self):
        app, captured = self._build_observed_app(trust_remote=False)
        client = TestClient(app)
        resp = client.get(
            "/_test/inspect",
            headers={
                "traceparent": VALID_TRACEPARENT_SAMPLED,
                "tracestate": "vendor=canary-state",
                "baggage": "user=canary-baggage",
                "X-Request-ID": "req_client_supplied_canary",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(captured), 1)
        obs = captured[0]
        ctx: TraceContext = obs["req_ctx"]
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx, obs["var_ctx"])
        self.assertEqual(ctx.schema_version, TRACE_SCHEMA_VERSION)
        self.assertEqual(ctx.ingress, "ignored_untrusted")
        self.assertNotEqual(ctx.trace_id, VALID_TRACE_ID)
        self.assertNotEqual(ctx.span_id, VALID_SPAN_ID)
        self.assertIsNone(ctx.parent_span_id)
        self.assertFalse(ctx.sampled)
        self.assertRegex(ctx.request_id, REQUEST_ID_RE)
        self.assertNotEqual(ctx.request_id, "req_client_supplied_canary")

        # Downstream request headers were sanitized to local context only.
        self.assertEqual(obs["incoming_traceparent"], ctx.traceparent)
        self.assertIsNone(obs["incoming_tracestate"])
        self.assertIsNone(obs["incoming_baggage"])
        self.assertEqual(obs["incoming_x_request_id"], ctx.request_id)

        # Response headers carry fresh correlation IDs and never echo untrusted input.
        self.assertEqual(resp.headers.get(REQUEST_ID_HEADER), ctx.request_id)
        self.assertEqual(resp.headers.get(TRACEPARENT_HEADER), ctx.traceparent)
        self.assertIsNone(current_trace_context())

    def test_trusted_ingress_accepts_valid_traceparent_and_creates_child_span(self):
        app, captured = self._build_observed_app(
            setting_env={"LAB_TRUST_REMOTE_TRACEPARENT": "true"},
        )
        client = TestClient(app)
        resp = client.get(
            "/_test/inspect",
            headers={
                "traceparent": VALID_TRACEPARENT_SAMPLED,
                "tracestate": "vendor=untrusted-state",
                "baggage": "secret=untrusted-baggage",
                "X-Request-ID": "req_remote_override_attempt",
            },
        )
        self.assertEqual(resp.status_code, 200)
        obs = captured[0]
        ctx: TraceContext = obs["req_ctx"]
        self.assertEqual(ctx.ingress, "accepted")
        self.assertEqual(ctx.trace_id, VALID_TRACE_ID)
        self.assertEqual(ctx.parent_span_id, VALID_SPAN_ID)
        self.assertNotEqual(ctx.span_id, VALID_SPAN_ID)
        self.assertRegex(ctx.span_id, SPAN_ID_RE)
        self.assertTrue(ctx.sampled)
        self.assertRegex(ctx.request_id, REQUEST_ID_RE)
        self.assertNotEqual(ctx.request_id, "req_remote_override_attempt")

        self.assertEqual(obs["state_request_id"], ctx.request_id)
        self.assertEqual(obs["state_trace_id"], VALID_TRACE_ID)
        self.assertEqual(obs["state_span_id"], ctx.span_id)
        self.assertEqual(obs["state_parent_span_id"], VALID_SPAN_ID)
        self.assertIsNone(obs["incoming_tracestate"])
        self.assertIsNone(obs["incoming_baggage"])

        self.assertEqual(resp.headers.get(REQUEST_ID_HEADER), ctx.request_id)
        self.assertEqual(
            resp.headers.get(TRACEPARENT_HEADER),
            f"00-{VALID_TRACE_ID}-{ctx.span_id}-01",
        )

    def test_trusted_ingress_absent_or_malformed_or_duplicate_starts_fresh_trace_without_rejecting(self):
        app, captured = self._build_observed_app(trust_remote=True)
        client = TestClient(app)

        # 1. Absent header -> ingress="absent"
        r_absent = client.get("/_test/inspect")
        self.assertEqual(r_absent.status_code, 200)
        ctx_absent: TraceContext = captured[-1]["req_ctx"]
        self.assertEqual(ctx_absent.ingress, "absent")
        self.assertIsNone(ctx_absent.parent_span_id)
        self.assertFalse(ctx_absent.sampled)

        # 2. Malformed headers -> ingress="rejected", HTTP 200 preserved, no canary echoed
        canary = "secret-canary-value-777"
        for bad in (
            "invalid",
            f"00-{canary}-01",
            f"00-{'0' * 32}-{VALID_SPAN_ID}-01",
            f"00-{VALID_TRACE_ID}-{'0' * 16}-01",
            f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-02",
            f"01-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            VALID_TRACEPARENT_SAMPLED.upper(),
            f"{VALID_TRACEPARENT_SAMPLED},{VALID_TRACEPARENT_UNSAMPLED}",
        ):
            with self.subTest(bad=bad):
                resp = client.get(
                    "/_test/inspect?secret=" + canary,
                    headers={
                        "traceparent": bad,
                        "Authorization": f"Bearer {canary}",
                        "X-Request-ID": canary,
                    },
                )
                self.assertEqual(resp.status_code, 200)
                ctx: TraceContext = captured[-1]["req_ctx"]
                self.assertEqual(ctx.ingress, "rejected")
                self.assertNotEqual(ctx.trace_id, VALID_TRACE_ID)
                self.assertIsNone(ctx.parent_span_id)
                self.assertFalse(ctx.sampled)
                self.assertNotIn(canary, repr(dataclasses.asdict(ctx)))
                for h_name, h_val in resp.headers.items():
                    self.assertNotIn(canary, f"{h_name}:{h_val}")

    def test_healthz_readyz_and_404_preserve_behavior_and_emit_correlation_headers(self):
        s = make_settings()
        client_ok = TestClient(
            create_app(s, checks={"db": lambda: CheckResult(True, "ready")})
        )
        r_health = client_ok.get("/healthz")
        self.assertEqual(r_health.status_code, 200)
        self.assertEqual(r_health.json(), {"status": "ok"})
        self.assertRegex(r_health.headers.get(REQUEST_ID_HEADER, ""), REQUEST_ID_RE)
        parsed_health_tp = parse_traceparent(r_health.headers.get(TRACEPARENT_HEADER, ""))
        self.assertFalse(parsed_health_tp.sampled)

        r_ready = client_ok.get("/readyz")
        self.assertEqual(r_ready.status_code, 200)
        self.assertEqual(r_ready.json()["status"], "ready")
        self.assertRegex(r_ready.headers.get(REQUEST_ID_HEADER, ""), REQUEST_ID_RE)
        parse_traceparent(r_ready.headers.get(TRACEPARENT_HEADER, ""))

        client_fail = TestClient(
            create_app(s, checks={"db": lambda: CheckResult(False, "down")})
        )
        r_fail = client_fail.get("/readyz")
        self.assertEqual(r_fail.status_code, 503)
        self.assertEqual(r_fail.json()["status"], "not_ready")
        self.assertRegex(r_fail.headers.get(REQUEST_ID_HEADER, ""), REQUEST_ID_RE)
        parse_traceparent(r_fail.headers.get(TRACEPARENT_HEADER, ""))

        r_404 = client_ok.get("/nonexistent-route-canary")
        self.assertEqual(r_404.status_code, 404)
        self.assertRegex(r_404.headers.get(REQUEST_ID_HEADER, ""), REQUEST_ID_RE)
        parse_traceparent(r_404.headers.get(TRACEPARENT_HEADER, ""))
        self.assertNotIn("nonexistent-route-canary", r_404.headers.get(REQUEST_ID_HEADER, ""))
        self.assertNotIn("nonexistent-route-canary", r_404.headers.get(TRACEPARENT_HEADER, ""))

    def test_payload_free_state_never_contains_user_inputs(self):
        app, captured = self._build_observed_app(trust_remote=True)
        client = TestClient(app)
        canary = "SensitiveUserPayload_987654"
        resp = client.get(
            f"/_test/inspect?query={canary}",
            headers={
                "Authorization": f"Bearer {canary}",
                "Cookie": f"session={canary}",
                "X-Custom-Secret": canary,
                "X-Request-ID": canary,
                "traceparent": f"00-{canary}",
            },
        )
        self.assertEqual(resp.status_code, 200)
        ctx: TraceContext = captured[0]["req_ctx"]
        serialized = repr(dataclasses.asdict(ctx))
        self.assertNotIn(canary, serialized)
        self.assertEqual(
            set(dataclasses.asdict(ctx).keys()),
            {
                "schema_version",
                "request_id",
                "trace_id",
                "span_id",
                "parent_span_id",
                "sampled",
                "ingress",
            },
        )


class ASGIBoundaryAndConcurrencyTests(unittest.TestCase):
    def test_duplicate_raw_traceparent_headers_rejected_and_caller_scope_untouched(self):
        observed_in_app: dict[str, Any] = {}

        async def raw_app(scope, receive, send):
            observed_in_app["ctx"] = request_trace_context(scope)
            observed_in_app["var"] = current_trace_context()
            observed_in_app["headers"] = list(scope["headers"])
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"x-request-id", b"downstream-spoofed"),
                        (b"traceparent", b"downstream-spoofed"),
                        (b"x-custom", b"preserved"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        mw = TraceContextMiddleware(raw_app, trust_remote_traceparent=True)
        original_state = {"existing_key": "keep_me"}
        original_headers = [
            (b"traceparent", VALID_TRACEPARENT_SAMPLED.encode("ascii")),
            (b"TraceParent", VALID_TRACEPARENT_UNSAMPLED.encode("ascii")),
            (b"tracestate", b"v=1"),
            (b"baggage", b"k=v"),
            (b"x-request-id", b"client-spoofed"),
        ]
        caller_scope = {
            "type": "http",
            "method": "GET",
            "path": "/raw",
            "headers": list(original_headers),
            "state": original_state,
        }
        sent_messages: list[dict[str, Any]] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent_messages.append(msg)

        asyncio.run(mw(caller_scope, receive, send))

        # Caller scope & state must not be mutated.
        self.assertEqual(caller_scope["state"], {"existing_key": "keep_me"})
        self.assertEqual(caller_scope["headers"], original_headers)
        self.assertIsNone(current_trace_context())

        # Duplicate traceparent must be rejected and replaced with a fresh trace.
        ctx: TraceContext = observed_in_app["ctx"]
        self.assertEqual(ctx, observed_in_app["var"])
        self.assertEqual(ctx.ingress, "rejected")
        self.assertNotEqual(ctx.trace_id, VALID_TRACE_ID)
        self.assertIsNone(ctx.parent_span_id)

        # Response headers must override downstream spoofed correlation headers while keeping X-Custom.
        start_msg = sent_messages[0]
        resp_headers = dict(start_msg["headers"])
        self.assertEqual(resp_headers[b"x-custom"], b"preserved")
        self.assertEqual(resp_headers[b"x-request-id"], ctx.request_id.encode("ascii"))
        self.assertEqual(resp_headers[b"traceparent"], ctx.traceparent.encode("ascii"))

    def test_non_ascii_and_malformed_raw_header_tuples_handled_gracefully(self):
        ctx = resolve_request_trace_context(
            [(b"traceparent", b"00-\xff" + b"a" * 51)],
            trust_remote_traceparent=True,
        )
        self.assertEqual(ctx.ingress, "rejected")
        self.assertIsNone(ctx.parent_span_id)

        ctx_bad_list = resolve_request_trace_context(
            "not-a-header-list",
            trust_remote_traceparent=True,
        )
        self.assertEqual(ctx_bad_list.ingress, "rejected")

    def test_non_http_scope_passes_through_untouched(self):
        called = False

        async def lifespan_app(scope, receive, send):
            nonlocal called
            called = True
            self.assertNotIn("state", scope)
            self.assertIsNone(current_trace_context())

        mw = TraceContextMiddleware(lifespan_app, trust_remote_traceparent=True)
        scope = {"type": "lifespan"}

        async def dummy():
            return {}

        async def dummy_send(_msg):
            pass

        asyncio.run(mw(scope, dummy, dummy_send))
        self.assertTrue(called)
        self.assertEqual(scope, {"type": "lifespan"})

    def test_exception_in_downstream_app_resets_contextvar(self):
        async def failing_app(scope, receive, send):
            self.assertIsNotNone(current_trace_context())
            raise RuntimeError("boom")

        mw = TraceContextMiddleware(failing_app, trust_remote_traceparent=True)

        async def dummy():
            return {}

        async def dummy_send(_msg):
            pass

        with self.assertRaises(RuntimeError):
            asyncio.run(mw({"type": "http", "headers": []}, dummy, dummy_send))
        self.assertIsNone(current_trace_context())

    def test_async_and_threaded_concurrency_isolation(self):
        s = make_settings(LAB_TRUST_REMOTE_TRACEPARENT="true")
        app = create_app(s, checks={"db": lambda: CheckResult(True, "ok")})

        @app.get("/_test/concurrent/{idx}")
        async def concurrent_endpoint(idx: int, request: Request) -> JSONResponse:
            before = current_trace_context()
            req_before = request_trace_context(request)
            await asyncio.sleep(0.005)
            after = current_trace_context()
            req_after = request_trace_context(request)
            if before is None or before != after or before != req_before or before != req_after:
                return JSONResponse({"mismatch": True}, status_code=500)
            return JSONResponse(
                {
                    "idx": idx,
                    "request_id": before.request_id,
                    "trace_id": before.trace_id,
                    "span_id": before.span_id,
                    "parent_span_id": before.parent_span_id,
                    "ingress": before.ingress,
                }
            )

        async def run_async_batch():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                async def call_one(i: int):
                    trace_id = f"{i + 1:032x}"
                    parent_span = f"{i + 1:016x}"
                    headers = {}
                    if i % 3 == 0:
                        headers["traceparent"] = f"00-{trace_id}-{parent_span}-01"
                    elif i % 3 == 1:
                        headers["traceparent"] = f"00-malformed-{i}"
                    resp = await client.get(f"/_test/concurrent/{i}", headers=headers)
                    return i, trace_id, parent_span, resp

                return await asyncio.gather(*(call_one(i) for i in range(36)))

        results = asyncio.run(run_async_batch())
        seen_request_ids: set[str] = set()
        seen_span_ids: set[str] = set()
        for i, expected_trace, expected_parent, resp in results:
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(body["idx"], i)
            self.assertEqual(resp.headers[REQUEST_ID_HEADER], body["request_id"])
            self.assertNotIn(body["request_id"], seen_request_ids)
            self.assertNotIn(body["span_id"], seen_span_ids)
            seen_request_ids.add(body["request_id"])
            seen_span_ids.add(body["span_id"])
            if i % 3 == 0:
                self.assertEqual(body["ingress"], "accepted")
                self.assertEqual(body["trace_id"], expected_trace)
                self.assertEqual(body["parent_span_id"], expected_parent)
                self.assertEqual(
                    resp.headers[TRACEPARENT_HEADER],
                    f"00-{expected_trace}-{body['span_id']}-01",
                )
            elif i % 3 == 1:
                self.assertEqual(body["ingress"], "rejected")
                self.assertNotEqual(body["trace_id"], expected_trace)
                self.assertIsNone(body["parent_span_id"])
            else:
                self.assertEqual(body["ingress"], "absent")
                self.assertIsNone(body["parent_span_id"])

        # Also verify multi-threaded synchronous TestClient isolation.
        def sync_call(i: int):
            with TestClient(app) as tc:
                tid = f"{i + 100:032x}"
                pspan = f"{i + 100:016x}"
                r = tc.get(
                    f"/_test/concurrent/{i}",
                    headers={"traceparent": f"00-{tid}-{pspan}-00"},
                )
                return i, tid, pspan, r.status_code, r.json(), dict(r.headers)

        with ThreadPoolExecutor(max_workers=8) as pool:
            thread_results = list(pool.map(sync_call, range(16)))
        for i, tid, pspan, status, body, headers in thread_results:
            self.assertEqual(status, 200)
            self.assertEqual(body["trace_id"], tid)
            self.assertEqual(body["parent_span_id"], pspan)
            self.assertEqual(headers["x-request-id"], body["request_id"])
            self.assertEqual(headers["traceparent"], f"00-{tid}-{body['span_id']}-00")


if __name__ == "__main__":
    unittest.main()
