# HTTP trace bridge foundation

`services/worker/internal/tracebridge` adds both sides of an explicitly
instrumented HTTP boundary. A client span sends its identifier as the remote
parent; the server records that parent and creates its own distinct span. Nested
outgoing calls use the local server span as their parent. Both sides report
`trace-bridge-1` metadata through a caller-supplied observer. This is an
independent #6 foundation, not a durable collector, complete LAB log schema,
authorization system, or proof of provider-internal execution.

## Wiring

```go
bridge, err := tracebridge.New(tracebridge.Config{
    AcceptRemoteParent: true,
    Observe: func(event tracebridge.Event) {
        // Enqueue metadata in a bounded, concurrent-safe diagnostic collector.
        // Persist/authorize/query it in the integration layer.
    },
})
if err != nil { return err }
handler := bridge.Handler(applicationHandler)
client := &http.Client{
    Transport: bridge.Transport(configuredTransport),
    CheckRedirect: func(*http.Request, []*http.Request) error {
        return http.ErrUseLastResponse
    },
}
_ = handler
_ = client
```

The bridge belongs on the same authenticated internal route as the actual server
handler, not only on the client. `AcceptRemoteParent` defaults to false: public or
untrusted ingress starts a fresh trace. Enabling it is an explicit deployment
assertion that the remote source is approved for correlation. The bridge itself
does not authenticate that source. No trace ID is an identity or access grant.
Server and client correlation is actual only where both wrappers are installed;
unmodified agentKit RemoteClient/router and provider internals remain separate,
unobserved segments until their integration is performed and tested.

`WithLinkage` installs local operation/request IDs after local authorization;
`Current` reads correlation metadata. IDs are bounded opaque strings, optionally
empty when no local binding exists. No incoming header supplies those bindings.
The syntax check does not identify credentials disguised as valid IDs: callers
must provide only safe application identifiers. Remote `X-Lab-*` and application
headers are neither read as linkage nor altered; application authorization still
belongs to its own middleware.

## Constrained propagation profile

The supported profile accepts exactly one 55-byte lowercase version `00`
`traceparent`, with nonzero 32-hex trace and 16-hex span IDs, and flags `00` or
`01`. Duplicate headers (including alternate map capitalization), comma-joined
values, whitespace, zero IDs, unsupported versions and flags reject remote
linkage. A trusted malformed parent starts a fresh trace with `ingress=rejected`
and still runs the business handler. Untrusted parent input is ignored with
`ingress=ignored_untrusted`. An absent trusted parent records `absent`.

This deliberately restricted profile is not a complete W3C Trace Context
implementation: reserved flags/new versions and `tracestate` are not supported.
Incoming and outgoing request clones remove `traceparent`, `tracestate` and
`baggage` case-insensitively; outgoing requests receive exactly one local
`traceparent`. Original caller headers/context remain unchanged. No baggage or
remote LAB identity is promoted to authorization. Trace sampling is correlation
metadata only: both sampled and unsampled boundaries emit diagnostic events.

## What each event means

Events contain only generated trace/span IDs, an optional parent, trusted local
operation/request IDs, fixed kind/phase/ingress/visibility labels, UTC event time,
monotonic elapsed nanoseconds and an optional HTTP status. No URL, path, query,
header, body, arbitrary error, or panic text is recorded. Visibility is always
`instrumented_boundary_only`; no event asserts business success or provider
usage. Parent/linkage absence uses empty IDs; unobserved HTTP status is JSON null.

- Client: `started`, then `headers_received` or `transport_error`. The observed
  numeric status is included only when the base transport returns a response.
  RoundTrip duration ends at headers, not response body completion.
- Server: `started`, then `handler_returned`. The original ResponseWriter is
  passed through unchanged, preserving Flusher/Hijacker/Pusher and other optional
  interfaces. Status and body delivery are not measured. A returning handler may
  still have written an error or handed off a streaming/upgraded connection.
- Handler/base-transport panic or Goexit emits `handler_aborted` or
  `transport_aborted` without capturing the panic value, then propagates control
  unchanged. Process termination can leave an unmatched start; the collector
  must reconcile this as incomplete rather than invent completion.

The wrapper performs one base RoundTrip and never reads/closes/replaces the
response body. Base transport errors and responses are returned unchanged.
Request cancellation/deadlines are preserved. The caller configures destination
allowlists, transport timeouts, credentials and redirect policy. Go transport
internal retries and http.Client redirects remain outside this wrapper's
control; a span is not proof of one physical network attempt. The bridge does
not create retries or paid calls itself. Random-ID generation failure returns a
sanitized error (client) or HTTP 503 before invoking the server handler.

## Observer and remaining gates

The observer runs synchronously and must be bounded, concurrent-safe and
nonblocking. Observer panic is contained to preserve HTTP behavior, incrementing
`Dropped()`; it cannot count as persisted telemetry. Nil observer is rejected.
There is no built-in queue, durable buffer, acknowledgment, backpressure, SQL
transaction, failure retry, or deduplication. The caller must monitor dropped
counts and collector queue/sink failures. A blocking observer can delay HTTP;
no hard collector isolation is claimed.

Validation uses real local HTTP client/server traffic plus strict-profile,
header immutability, local-versus-remote linkage, writer/body preservation,
cancellation/error/panic secrecy and concurrent/race cases. These are R18/R19,
deterministic T65/T69 boundary subcases and A24 foundation evidence only.
PostgreSQL persistence/ACLs, durable audit/collector recovery, API/worker/router
wiring, agentKit/provider spans, cross-service trust deployment and complete
T65/A24/release acceptance remain open. No provider, parser, human-quality or
performance acceptance follows from these tests.
