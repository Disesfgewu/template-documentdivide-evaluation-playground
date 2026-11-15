package tracebridge

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

const trace = "1234567890abcdef1234567890abcdef"
const span = "1234567890abcdef"
const valid = "00-" + trace + "-" + span + "-01"

type events struct {
	mu   sync.Mutex
	list []Event
}

func (e *events) observe(v Event) { e.mu.Lock(); defer e.mu.Unlock(); e.list = append(e.list, v) }
func (e *events) all() []Event {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]Event(nil), e.list...)
}
func bridge(t *testing.T, e *events, trusted bool) *Bridge {
	t.Helper()
	b, err := New(Config{AcceptRemoteParent: trusted, Observe: e.observe})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestStrictParentProfile(t *testing.T) {
	p, err := Parse(valid)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := p.Header(); got != valid {
		t.Fatal(got)
	}
	for name, input := range map[string]string{
		"empty": "", "long": valid + "-extra", "version": "01" + valid[2:], "uppercase": strings.ToUpper(valid), "zero_trace": "00-" + strings.Repeat("0", 32) + "-" + span + "-01", "zero_span": "00-" + trace + "-" + strings.Repeat("0", 16) + "-01", "flags": "00-" + trace + "-" + span + "-03", "comma": valid + "," + valid, "space": " " + valid, "control": valid[:54] + "\n", "separator": valid[:35] + "_" + valid[36:],
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := Parse(input); !errors.Is(err, ErrInvalidContext) {
				t.Fatal("accepted invalid", name)
			}
		})
	}
	if _, err := (Parent{}).Header(); err == nil {
		t.Fatal("empty parent serialized")
	}
}

func TestRealHTTPBothBoundariesAndLocalLinkage(t *testing.T) {
	var ev events
	b := bridge(t, &ev, true)
	upstream := httptest.NewServer(b.Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, ids, ok := Current(r.Context())
		if !ok || p.TraceID == "" {
			t.Error("missing server context")
		}
		if ids != (Linkage{}) {
			t.Error("remote IDs promoted", ids)
		}
		for _, k := range []string{"traceparent", "tracestate", "baggage"} {
			if r.Header.Get(k) != "" {
				t.Error("propagation header leaked", k)
			}
		}
		w.Header().Set("X-Result", "actual")
		w.WriteHeader(207)
		_, _ = io.WriteString(w, "body")
	})))
	defer upstream.Close()
	ctx, err := WithLinkage(context.Background(), Linkage{OperationID: "op-1", RequestID: "req-1"})
	if err != nil {
		t.Fatal(err)
	}
	req, _ := http.NewRequestWithContext(ctx, "POST", upstream.URL+"/private?token=CANARY", strings.NewReader("CANARY"))
	req.Header.Set("Traceparent", valid)
	req.Header.Set("Baggage", "CANARY")
	req.Header.Set("Tracestate", "CANARY")
	req.Header.Set("Authorization", "CANARY")
	req.Header.Set("X-Lab-Operation-Id", "CANARY")
	original := req.Header.Clone()
	client := &http.Client{Transport: b.Transport(http.DefaultTransport), CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 207 || resp.Header.Get("X-Result") != "actual" || string(data) != "body" {
		t.Fatal("response changed")
	}
	if !reflect.DeepEqual(req.Header, original) {
		t.Fatal("caller headers mutated")
	}
	all := ev.all()
	if len(all) != 4 {
		t.Fatal(all)
	}
	var cs, ss Event
	for _, e := range all {
		if e.Kind == "client" && e.Phase == "started" {
			cs = e
		}
		if e.Kind == "server" && e.Phase == "started" {
			ss = e
		}
		if e.DurationNS < 0 || e.Visibility != "instrumented_boundary_only" || e.OccurredAt.Location() != time.UTC {
			t.Fatal(e)
		}
	}
	if ss.TraceID != cs.TraceID || ss.ParentSpanID != cs.SpanID || ss.SpanID == cs.SpanID || cs.ParentSpanID != "" || cs.TraceID == trace {
		t.Fatal("wrong actual parent chain", cs, ss)
	}
	if cs.OperationID != "op-1" || cs.RequestID != "req-1" || ss.OperationID != "" || ss.Ingress != "accepted" {
		t.Fatal(cs, ss)
	}
	blob, _ := json.Marshal(all)
	if strings.Contains(string(blob), "CANARY") {
		t.Fatal("payload leaked", string(blob))
	}
}

func TestIngressTrustAndInvalidHeadersResetOnlyCorrelation(t *testing.T) {
	cases := []struct {
		name    string
		trusted bool
		headers http.Header
		ingress string
		accept  bool
	}{
		{"accepted", true, http.Header{"Traceparent": {valid}}, "accepted", true},
		{"absent", true, nil, "absent", false},
		{"untrusted", false, http.Header{"Traceparent": {valid}}, "ignored_untrusted", false},
		{"duplicate", true, http.Header{"Traceparent": {valid, valid}}, "rejected", false},
		{"case_duplicate", true, http.Header{"Traceparent": {valid}, "traceparent": {valid}}, "rejected", false},
		{"joined", true, http.Header{"Traceparent": {valid + "," + valid}}, "rejected", false},
		{"malformed", true, http.Header{"Traceparent": {"CANARY"}}, "rejected", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var ev events
			b := bridge(t, &ev, tc.trusted)
			called := false
			req := httptest.NewRequest("GET", "http://example.invalid", nil)
			req.Header = tc.headers
			ids := Linkage{OperationID: "local-op"}
			ctx, _ := WithLinkage(req.Context(), ids)
			req = req.WithContext(ctx)
			rec := httptest.NewRecorder()
			b.Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				called = true
				_, got, ok := Current(r.Context())
				if !ok || got != ids {
					t.Error("local context lost")
				}
				w.WriteHeader(204)
			})).ServeHTTP(rec, req)
			if !called || rec.Code != 204 {
				t.Fatal("tracing altered business request")
			}
			all := ev.all()
			if len(all) != 2 || all[0].Ingress != tc.ingress {
				t.Fatal(all)
			}
			if (all[0].TraceID == trace) != tc.accept {
				t.Fatal(all)
			}
			if tc.accept && all[0].ParentSpanID != span {
				t.Fatal(all)
			}
		})
	}
}

func TestServerPassesOriginalWriterAndPanic(t *testing.T) {
	var ev events
	b := bridge(t, &ev, false)
	w := httptest.NewRecorder()
	marker := &struct{ secret string }{"CANARY"}
	func() {
		defer func() {
			if recover() != marker {
				t.Error("panic changed")
			}
		}()
		b.Handler(http.HandlerFunc(func(got http.ResponseWriter, r *http.Request) {
			if got != w {
				t.Error("writer wrapped")
			}
			if _, ok := got.(http.Flusher); !ok {
				t.Error("flusher lost")
			}
			panic(marker)
		})).ServeHTTP(w, httptest.NewRequest("GET", "/", nil))
	}()
	all := ev.all()
	if len(all) != 2 || all[1].Phase != "handler_aborted" {
		t.Fatal(all)
	}
	raw, _ := json.Marshal(all)
	if strings.Contains(string(raw), "CANARY") {
		t.Fatal("panic leaked")
	}
}

func TestClientPreservesResponseAndBodyUntilCallerReads(t *testing.T) {
	var ev events
	b := bridge(t, &ev, false)
	body := &probeBody{}
	expected := &http.Response{StatusCode: 200, Body: body}
	called := 0
	rt := b.Transport(roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		called++
		if r.Header.Get("Traceparent") == "" {
			t.Error("missing propagation")
		}
		return expected, nil
	}))
	req := &http.Request{Method: "GET", Header: nil}
	resp, err := rt.RoundTrip(req)
	if err != nil || resp != expected || called != 1 || body.read || body.closed {
		t.Fatal("body/response altered")
	}
	all := ev.all()
	if len(all) != 2 || all[1].Phase != "headers_received" {
		t.Fatal(all)
	}
	if req.Header != nil {
		t.Fatal("mutated nil header")
	}
	_ = resp.Body.Close()
	if !body.closed {
		t.Fatal("close not preserved")
	}
}

type probeBody struct{ read, closed bool }

func (b *probeBody) Read(p []byte) (int, error) { b.read = true; return 0, io.EOF }
func (b *probeBody) Close() error               { b.closed = true; return nil }

func TestClientPreservesCancellationAndOpaqueError(t *testing.T) {
	var ev events
	b := bridge(t, &ev, false)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", "http://example.invalid?token=CANARY", nil)
	expected := errors.New("CANARY")
	calls := 0
	_, err := b.Transport(roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Context().Err() != context.Canceled {
			t.Error("context cancellation lost")
		}
		return nil, expected
	})).RoundTrip(req)
	if err != expected || calls != 1 {
		t.Fatal("error/retry changed")
	}
	all := ev.all()
	if len(all) != 2 || all[1].Phase != "transport_error" {
		t.Fatal(all)
	}
	raw, _ := json.Marshal(all)
	if strings.Contains(string(raw), "CANARY") {
		t.Fatal("error leaked")
	}
}

func TestNestedClientGetsLocalServerParent(t *testing.T) {
	var ev events
	b := bridge(t, &ev, true)
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set("Traceparent", valid)
	b.Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		out, _ := http.NewRequestWithContext(r.Context(), "GET", "http://example.invalid", nil)
		_, _ = b.Transport(roundTripperFunc(func(r *http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 204, Body: http.NoBody}, nil
		})).RoundTrip(out)
	})).ServeHTTP(httptest.NewRecorder(), req)
	all := ev.all()
	if len(all) != 4 || all[1].ParentSpanID != all[0].SpanID || all[1].TraceID != trace || all[0].ParentSpanID != span || all[0].SpanID == span {
		t.Fatal(all)
	}
}

func TestObserverPanicDoesNotChangeHTTPAndCountsLoss(t *testing.T) {
	b, err := New(Config{Observe: func(Event) { panic("CANARY") }})
	if err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	b.Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(202) })).ServeHTTP(w, httptest.NewRequest("GET", "/", nil))
	if w.Code != 202 || b.Dropped() != 2 {
		t.Fatal(w.Code, b.Dropped())
	}
	if _, err := New(Config{}); err == nil {
		t.Fatal("nil observer accepted")
	}
}

func TestLocalLinkageValidationAndConcurrentRequests(t *testing.T) {
	for _, id := range []string{"\nsecret", strings.Repeat("a", 129), "x y"} {
		if _, err := WithLinkage(context.Background(), Linkage{OperationID: id}); err == nil {
			t.Fatal("invalid local ID accepted")
		}
	}
	var ev events
	b := bridge(t, &ev, false)
	h := b.Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }))
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Go(func() { h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/", nil)) })
	}
	wg.Wait()
	all := ev.all()
	if len(all) != 80 {
		t.Fatal(len(all))
	}
	seen := map[string]bool{}
	for _, e := range all {
		if e.Phase == "started" {
			if seen[e.SpanID] {
				t.Fatal("duplicate span")
			}
			seen[e.SpanID] = true
		}
	}
}

func TestClientPanicIsAbortedAndRethrown(t *testing.T) {
	var ev events
	b := bridge(t, &ev, false)
	marker := &struct{}{}
	func() {
		defer func() {
			if recover() != marker {
				t.Error("panic changed")
			}
		}()
		req, _ := http.NewRequest("GET", "http://example.invalid", nil)
		_, _ = b.Transport(roundTripperFunc(func(*http.Request) (*http.Response, error) { panic(marker) })).RoundTrip(req)
	}()
	all := ev.all()
	if len(all) != 2 || all[1].Phase != "transport_aborted" || all[1].HTTPStatus != nil {
		t.Fatal(all)
	}
}

func TestUnobservedStatusIsNull(t *testing.T) {
	var ev events
	b := bridge(t, &ev, false)
	b.Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(201) })).ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/", nil))
	for _, e := range ev.all() {
		if e.HTTPStatus != nil {
			t.Fatal("fabricated server status", e)
		}
		raw, _ := json.Marshal(e)
		if !strings.Contains(string(raw), `"http_status":null`) {
			t.Fatal(string(raw))
		}
	}
}
