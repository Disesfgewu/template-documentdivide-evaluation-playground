// Package tracebridge correlates explicitly instrumented HTTP boundaries. It is
// diagnostic metadata, never authentication, authorization, or durable audit.
package tracebridge

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

var ErrInvalidContext = errors.New("invalid trace context")

// Parent is the bounded version-00 subset of W3C trace context supported here.
// Flags are limited to 00/01; unsupported versions/flags are rejected.
type Parent struct {
	TraceID, SpanID string
	Sampled         bool
}

func Parse(value string) (Parent, error) {
	if len(value) != 55 || value[:3] != "00-" || value[35] != '-' || value[52] != '-' || (value[53:] != "00" && value[53:] != "01") {
		return Parent{}, ErrInvalidContext
	}
	p := Parent{TraceID: value[3:35], SpanID: value[36:52], Sampled: value[53:] == "01"}
	if !validHex(p.TraceID, 32) || !validHex(p.SpanID, 16) {
		return Parent{}, ErrInvalidContext
	}
	return p, nil
}
func validHex(s string, n int) bool {
	if len(s) != n || s == strings.Repeat("0", n) {
		return false
	}
	for _, c := range s {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}
func (p Parent) Header() (string, error) {
	if !validHex(p.TraceID, 32) || !validHex(p.SpanID, 16) {
		return "", ErrInvalidContext
	}
	flags := "00"
	if p.Sampled {
		flags = "01"
	}
	return "00-" + p.TraceID + "-" + p.SpanID + "-" + flags, nil
}

type contextKey struct{}
type localContext struct {
	Parent  Parent
	Linkage Linkage
}

// Linkage contains local opaque identifiers, supplied only after local access
// checks. No HTTP header is mapped to these fields. They confer no authority.
type Linkage struct{ OperationID, RequestID string }

func validID(s string) bool {
	if len(s) > 128 {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("._:-", c)) {
			return false
		}
	}
	return true
}
func WithLinkage(ctx context.Context, ids Linkage) (context.Context, error) {
	if !validID(ids.OperationID) || !validID(ids.RequestID) {
		return nil, ErrInvalidContext
	}
	v, _ := ctx.Value(contextKey{}).(localContext)
	v.Linkage = ids
	return context.WithValue(ctx, contextKey{}, v), nil
}

// Current returns correlation metadata only, never a security principal.
func Current(ctx context.Context) (Parent, Linkage, bool) {
	v, ok := ctx.Value(contextKey{}).(localContext)
	_, err := v.Parent.Header()
	return v.Parent, v.Linkage, ok && err == nil
}

// Event intentionally excludes URLs, headers, bodies and error text. Phase
// started/handler_returned/handler_aborted/headers_received/transport_error
// and transport_aborted describes only the boundary observed, never provider or business completion.
type Event struct {
	SchemaVersion string    `json:"schema_version"`
	TraceID       string    `json:"trace_id"`
	SpanID        string    `json:"span_id"`
	ParentSpanID  string    `json:"parent_span_id"`
	OperationID   string    `json:"operation_id"`
	RequestID     string    `json:"request_id"`
	Kind          string    `json:"kind"`
	Phase         string    `json:"phase"`
	Ingress       string    `json:"ingress"`
	Visibility    string    `json:"visibility"`
	OccurredAt    time.Time `json:"occurred_at"`
	DurationNS    int64     `json:"duration_ns"`
	HTTPStatus    *int      `json:"http_status"`
}

// Config must explicitly opt in to accepting remote parent correlation on a
// deployment-authenticated internal ingress. The flag performs no authentication.
type Config struct {
	AcceptRemoteParent bool
	Observe            func(Event)
}
type Bridge struct {
	config  Config
	dropped atomic.Uint64
}

func New(c Config) (*Bridge, error) {
	if c.Observe == nil {
		return nil, errors.New("trace observer required")
	}
	return &Bridge{config: c}, nil
}

// Dropped counts observer panics. A callback must be concurrent-safe and bounded;
// persistent buffering, sink errors and reconciliation belong to the collector.
func (b *Bridge) Dropped() uint64 { return b.dropped.Load() }
func (b *Bridge) emit(e Event) {
	defer func() {
		if recover() != nil {
			b.dropped.Add(1)
		}
	}()
	b.config.Observe(e)
}
func randomID(n int) (string, error) {
	data := make([]byte, n)
	if _, err := rand.Read(data); err != nil {
		return "", errors.New("trace entropy unavailable")
	}
	s := hex.EncodeToString(data)
	if s == strings.Repeat("0", n*2) {
		return "", errors.New("trace entropy invalid")
	}
	return s, nil
}
func child(parent Parent) (Parent, error) {
	trace := parent.TraceID
	if trace == "" {
		var err error
		trace, err = randomID(16)
		if err != nil {
			return Parent{}, err
		}
	}
	span, err := randomID(8)
	return Parent{TraceID: trace, SpanID: span, Sampled: parent.Sampled}, err
}
func event(p Parent, ids Linkage, parent, kind, phase, ingress string, start time.Time, status int) Event {
	var measuredStatus *int
	if status != 0 {
		measuredStatus = &status
	}
	return Event{SchemaVersion: "trace-bridge-1", TraceID: p.TraceID, SpanID: p.SpanID, ParentSpanID: parent, OperationID: ids.OperationID, RequestID: ids.RequestID, Kind: kind, Phase: phase, Ingress: ingress, Visibility: "instrumented_boundary_only", OccurredAt: time.Now().UTC(), DurationNS: time.Since(start).Nanoseconds(), HTTPStatus: measuredStatus}
}
func clearPropagation(h http.Header) {
	for k := range h {
		if strings.EqualFold(k, "traceparent") || strings.EqualFold(k, "tracestate") || strings.EqualFold(k, "baggage") {
			delete(h, k)
		}
	}
}
func incoming(h http.Header) (Parent, string, error) {
	value := ""
	found := false
	for k, v := range h {
		if strings.EqualFold(k, "traceparent") {
			if found || len(v) != 1 {
				return Parent{}, "rejected", ErrInvalidContext
			}
			found = true
			value = v[0]
		}
	}
	if !found {
		return Parent{}, "absent", nil
	}
	p, err := Parse(value)
	if err != nil {
		return Parent{}, "rejected", err
	}
	return p, "accepted", nil
}

// Handler preserves the original ResponseWriter and all its optional interfaces.
// It therefore does not inspect status/body or claim response delivery completion.
func (b *Bridge) Handler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		parent := Parent{}
		ingress := "ignored_untrusted"
		if b.config.AcceptRemoteParent {
			var err error
			parent, ingress, err = incoming(r.Header)
			if err != nil {
				parent = Parent{}
			}
		}
		p, err := child(parent)
		if err != nil {
			http.Error(w, "trace unavailable", http.StatusServiceUnavailable)
			return
		}
		// Local correlation may be installed by outer trusted middleware; transport
		// headers do not influence it and an old local parent is not remote authority.
		local, _ := r.Context().Value(contextKey{}).(localContext)
		local.Parent = p
		ctx := context.WithValue(r.Context(), contextKey{}, local)
		copy := r.Clone(ctx)
		clearPropagation(copy.Header)
		b.emit(event(p, local.Linkage, parent.SpanID, "server", "started", ingress, start, 0))
		returned := false
		defer func() {
			phase := "handler_aborted"
			if returned {
				phase = "handler_returned"
			}
			b.emit(event(p, local.Linkage, parent.SpanID, "server", phase, ingress, start, 0))
		}()
		next.ServeHTTP(w, copy)
		returned = true
	})
}

// Transport makes one RoundTrip, clones request headers, replaces propagation,
// and records response-header arrival only. Response/body and errors are returned
// untouched, so streaming, upgrades, body cancellation and ownership stay intact.
// The caller must configure the base transport's destination policy and retries;
// http.Client redirect policy is outside RoundTripper and must be set separately.
func (b *Bridge) Transport(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	return roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		start := time.Now()
		local, _ := r.Context().Value(contextKey{}).(localContext)
		parent := local.Parent
		if _, err := parent.Header(); err != nil {
			parent = Parent{}
		}
		p, err := child(parent)
		if err != nil {
			return nil, err
		}
		local.Parent = p
		copy := r.Clone(context.WithValue(r.Context(), contextKey{}, local))
		clearPropagation(copy.Header)
		if copy.Header == nil {
			copy.Header = make(http.Header)
		}
		header, _ := p.Header()
		copy.Header.Set("Traceparent", header)
		b.emit(event(p, local.Linkage, parent.SpanID, "client", "started", "local", start, 0))
		returned := false
		defer func() {
			if !returned {
				b.emit(event(p, local.Linkage, parent.SpanID, "client", "transport_aborted", "local", start, 0))
			}
		}()
		resp, callErr := base.RoundTrip(copy)
		returned = true
		phase := "headers_received"
		status := 0
		if callErr != nil {
			phase = "transport_error"
		}
		if resp != nil {
			status = resp.StatusCode
		}
		b.emit(event(p, local.Linkage, parent.SpanID, "client", phase, "local", start, status))
		return resp, callErr
	})
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
