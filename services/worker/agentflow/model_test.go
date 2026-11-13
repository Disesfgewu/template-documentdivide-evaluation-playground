package agentflow

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Disesfgewu/agentkit/graph"
	"github.com/Disesfgewu/agentkit/model"
)

func goodConfig(name, url string) model.Config {
	return model.Config{Name: name, Protocol: "openai_chat", BaseURL: url, Model: name + "-model",
		MaxRetries: 1, TimeoutSeconds: 30, RequestsPerMinute: 6000}
}

func TestModelConfigMustExpressTheProfile(t *testing.T) {
	if err := ValidateModelConfig(goodConfig("a", "http://x")); err != nil {
		t.Fatal(err)
	}
	cases := map[string]func(*model.Config){
		"max_retries 0 (agentKit: 5 retries)": func(c *model.Config) { c.MaxRetries = 0 },
		"max_retries 3 (4 attempts)":          func(c *model.Config) { c.MaxRetries = 3 },
		"negative max_retries":                func(c *model.Config) { c.MaxRetries = -1 },
		"default timeout":                     func(c *model.Config) { c.TimeoutSeconds = 0 },
		"no rate limit":                       func(c *model.Config) { c.RequestsPerMinute = 0 },
		"no name":                             func(c *model.Config) { c.Name = "" },
		"embedding":                           func(c *model.Config) { c.Kind = model.KindEmbedding },
	}
	for name, mutate := range cases {
		c := goodConfig("a", "http://x")
		mutate(&c)
		if err := ValidateModelConfig(c); !errors.Is(err, ErrModelConfig) {
			t.Errorf("%s: %v", name, err)
		}
	}
	dup := model.Router{Entries: []model.Entry{{Config: goodConfig("a", "http://x")}, {Config: goodConfig("a", "http://y")}}}
	if err := ValidateRouter(dup); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("duplicate names: %v", err)
	}
}

// provider is a local OpenAI-compatible endpoint that counts requests.
type provider struct {
	hits    atomic.Int32
	handler func(n int32, w http.ResponseWriter, r *http.Request)
}

func (p *provider) start(t *testing.T) string {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.handler(p.hits.Add(1), w, r)
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

func answer(text string, usage bool) func(int32, http.ResponseWriter, *http.Request) {
	return func(_ int32, w http.ResponseWriter, _ *http.Request) {
		u := ""
		if usage {
			u = `,"usage":{"prompt_tokens":1000,"completion_tokens":500}`
		}
		fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]%s}`, text, u)
	}
}

func status(code int) func(int32, http.ResponseWriter, *http.Request) {
	return func(_ int32, w http.ResponseWriter, _ *http.Request) { w.WriteHeader(code) }
}

type chatEnv struct {
	ops    *MemoryOperationStore
	budget *Ledger
	node   graph.Node
}

func newChat(t *testing.T, client model.Client, pin string) chatEnv {
	t.Helper()
	ops := NewMemoryOperationStore()
	budget, _ := NewLedger(usd(10_000_000))
	r := &NodeRunner{Store: ops, WorkItemID: "q1", NodeRevision: "generate@1", MaxAttempts: 1,
		InvocationID: func(graph.State) (string, error) { return "inv", nil }, Budget: budget, Estimate: usd(1_000_000)}
	call := ChatCall{Client: client, Pin: pin, Price: Price{InputMicrosPerMTok: 3_000_000, OutputMicrosPerMTok: 15_000_000},
		Build: func(graph.State) (model.Request, error) {
			return model.Request{Messages: []model.Message{{Role: "user", Content: "q"}}, MaxOutputTokens: 256}, nil
		},
		Apply: func(_ graph.State, resp model.Response) (graph.State, error) {
			return graph.State{"answer": resp.Text}, nil
		}}
	node, err := call.Node(r)
	if err != nil {
		t.Fatal(err)
	}
	return chatEnv{ops: ops, budget: budget, node: node}
}

func routerFor(t *testing.T, url string, names ...string) model.Router {
	t.Helper()
	var r model.Router
	for _, n := range names {
		c := goodConfig(n, url)
		hc, err := model.New(c)
		if err != nil {
			t.Fatal(err)
		}
		r.Entries = append(r.Entries, model.Entry{Config: c, Client: hc})
	}
	if err := ValidateRouter(r); err != nil {
		t.Fatal(err)
	}
	return r
}

func (e chatEnv) op(t *testing.T) Operation {
	op, ok, err := e.ops.Get(context.Background(), OpKey{"q1", "generate@1", "inv"})
	if err != nil || !ok {
		t.Fatalf("operation missing: %v", err)
	}
	return op
}

func TestChatSuccessChargesActualUsage(t *testing.T) {
	ctx := audited(t)
	p := &provider{handler: answer("42", true)}
	env := newChat(t, routerFor(t, p.start(t), "primary"), "primary")
	patch, err := env.node(ctx, graph.State{})
	if err != nil || patch["answer"] != "42" {
		t.Fatalf("patch=%v err=%v", patch, err)
	}
	call := patch["llm_call"].(map[string]any)
	if call["provider"] != "primary" || call["model"] != "primary-model" || call["transport_attempts"] != 1 || call["usage_known"] != true {
		t.Fatalf("llm_call %v", call)
	}
	if _, err := EncodeState(patch); err != nil {
		t.Fatalf("patch must be checkpointable: %v", err)
	}
	// 1000 in * 3 + 500 out * 15 (micro-USD per token) = 10500
	if s := env.budget.Snapshot(); s.Spent != 10_500 || s.Reserved != 0 {
		t.Fatalf("budget %+v", s)
	}
	if p.hits.Load() != 1 {
		t.Fatalf("hits %d", p.hits.Load())
	}
}

// T27: retryable statuses are retried by agentKit only, up to
// MaxRetries+1 attempts; the runner adds none. Non-retryable is sent once.
func TestTransportRetriesHaveOneOwner(t *testing.T) {
	ctx := audited(t)
	cases := []struct {
		code int
		hits int32
	}{{503, 2}, {429, 2}, {401, 1}, {400, 1}}
	for _, c := range cases {
		t.Run(fmt.Sprint(c.code), func(t *testing.T) {
			p := &provider{handler: status(c.code)}
			env := newChat(t, routerFor(t, p.start(t), "primary"), "primary")
			_, err := env.node(ctx, graph.State{})
			var pe *ProviderStatusError
			if !errors.As(err, &pe) || pe.Status != c.code {
				t.Fatalf("err %v", err)
			}
			if got := p.hits.Load(); got != c.hits {
				t.Fatalf("HTTP attempts %d, want %d", got, c.hits)
			}
			op := env.op(t)
			if op.Status != OpFailed || len(op.Attempts) != 1 {
				t.Fatalf("op %+v", op)
			}
			if s := env.budget.Snapshot(); s.Spent != 0 || s.Reserved != 0 {
				t.Fatalf("budget %+v", s)
			}
		})
	}
}

// T29: a billed answer without usage keeps its reservation as unknown.
func TestUnknownUsageIsNotChargedAsZero(t *testing.T) {
	ctx := audited(t)
	p := &provider{handler: answer("ok", false)}
	env := newChat(t, routerFor(t, p.start(t), "primary"), "primary")
	if _, err := env.node(ctx, graph.State{}); err != nil {
		t.Fatal(err)
	}
	op := env.op(t)
	if op.Status != OpSucceeded || !op.Attempts[0].Unknown {
		t.Fatalf("op %+v", op)
	}
	if s := env.budget.Snapshot(); s.Spent != 0 || s.Reserved != 1_000_000 || s.Unknown != 1_000_000 {
		t.Fatalf("budget %+v", s)
	}
}

// T29/T63: cancelled while the provider holds the request: outcome_unknown,
// reservation held, and the provider sees exactly one request.
func TestCancelWhileProviderHoldsRequest(t *testing.T) {
	ctx := audited(t)
	received := make(chan struct{})
	release := make(chan struct{})
	p := &provider{handler: func(_ int32, w http.ResponseWriter, r *http.Request) {
		close(received)
		select {
		case <-r.Context().Done():
		case <-release:
		}
	}}
	env := newChat(t, routerFor(t, p.start(t), "primary"), "primary")
	defer close(release)
	cctx, cancel := context.WithCancel(ctx)
	go func() { <-received; cancel() }()
	_, err := env.node(cctx, graph.State{})
	if !errors.Is(err, ErrOutcomeUnknown) {
		t.Fatalf("err %v", err)
	}
	if op := env.op(t); op.Status != OpUnknown || op.Attempts[0].Outcome != "cancelled" {
		t.Fatalf("op %+v", op)
	}
	if s := env.budget.Snapshot(); s.Unknown != 1_000_000 {
		t.Fatalf("budget %+v", s)
	}
	if p.hits.Load() != 1 {
		t.Fatalf("hits %d", p.hits.Load())
	}
	// Re-running the node does not send again.
	if _, err := env.node(ctx, graph.State{}); !errors.Is(err, ErrOutcomeUnknown) || p.hits.Load() != 1 {
		t.Fatalf("re-run: %v hits=%d", err, p.hits.Load())
	}
}

// T26: the pin selects one Router entry with no failover, and a response
// from another model is rejected.
func TestPinIsHonouredAndSwapsAreRejected(t *testing.T) {
	ctx := audited(t)
	down := &provider{handler: status(503)}
	up := &provider{handler: answer("from-b", true)}
	r := routerFor(t, down.start(t), "a")
	r.Entries = append(r.Entries, routerFor(t, up.start(t), "b").Entries...)
	env := newChat(t, r, "a")
	if _, err := env.node(ctx, graph.State{}); err == nil {
		t.Fatal("pinned to a failing entry: must fail, not fail over to b")
	}
	if up.hits.Load() != 0 {
		t.Fatalf("failed over to b: %d hits", up.hits.Load())
	}
	env = newChat(t, r, "b")
	patch, err := env.node(ctx, graph.State{})
	if err != nil || patch["llm_call"].(map[string]any)["provider"] != "b" {
		t.Fatalf("pin b: %v %v", patch, err)
	}
	liar := fakeClient{resp: model.Response{Text: "x", Provider: "other", Model: "m", UsageKnown: true, InputTokens: 1}}
	env = newChat(t, liar, "b")
	if _, err := env.node(ctx, graph.State{}); !errors.Is(err, ErrModelSwapped) {
		t.Fatalf("swap: %v", err)
	}
}

type fakeClient struct {
	resp model.Response
	err  error
}

func (f fakeClient) Chat(context.Context, model.Request) (model.Response, error) {
	return f.resp, f.err
}

func TestChatCallGuards(t *testing.T) {
	call := ChatCall{Client: fakeClient{}, Pin: "a", Build: func(graph.State) (model.Request, error) {
		return model.Request{Messages: []model.Message{{Role: "user", Content: "q"}}}, nil
	}, Apply: func(graph.State, model.Response) (graph.State, error) { return nil, nil }}
	ops := NewMemoryOperationStore()
	inv := func(graph.State) (string, error) { return "i", nil }
	if _, err := call.Node(&NodeRunner{Store: ops, WorkItemID: "w", NodeRevision: "n", InvocationID: inv, MaxAttempts: 2}); err == nil {
		t.Fatal("runner retries on top of agentKit retries must be refused")
	}
	if _, err := call.Node(&NodeRunner{Store: ops, WorkItemID: "w", NodeRevision: "n", InvocationID: inv, MaxAttempts: 1,
		Retryable: func(error) bool { return true }}); err == nil {
		t.Fatal("Retryable on a model call must be refused")
	}
	node, err := call.Node(&NodeRunner{Store: ops, WorkItemID: "w", NodeRevision: "n", InvocationID: inv, MaxAttempts: 1})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := node(context.Background(), graph.State{}); err == nil || !strings.Contains(err.Error(), "max_output_tokens") {
		t.Fatalf("missing max_output_tokens: %v", err)
	}
	// A transport failure without status is outcome_unknown, never a clean failure.
	call.Build = func(graph.State) (model.Request, error) {
		return model.Request{Messages: []model.Message{{Role: "user", Content: "q"}}, MaxOutputTokens: 8}, nil
	}
	call.Client = fakeClient{err: errors.New(`Post "http://x/chat/completions": context deadline exceeded (Client.Timeout exceeded)`)}
	ops2 := NewMemoryOperationStore()
	node, _ = call.Node(&NodeRunner{Store: ops2, WorkItemID: "w", NodeRevision: "n", InvocationID: inv, MaxAttempts: 1})
	if _, err := node(context.Background(), graph.State{}); !errors.Is(err, ErrOutcomeUnknown) {
		t.Fatalf("timeout: %v", err)
	}
}

// R20/T28: products, sums and the ceiling operation must stay exact.
func TestPriceCostBoundaries(t *testing.T) {
	cases := []struct {
		name    string
		price   Price
		in, out int
		want    int64
		wantErr error
	}{
		{"zero", Price{}, 0, 0, 0, nil},
		{"fraction rounds up", Price{1, 0}, 1, 0, 1, nil},
		{"exact micro", Price{1, 0}, 1_000_000, 0, 1, nil},
		{"sum then round once", Price{1, 1}, 1, 1, 1, nil},
		{"multiplication exceeds int64", Price{math.MaxInt64, 0}, 2, 0, 18_446_744_073_710, nil},
		{"sum exceeds int64", Price{math.MaxInt64, math.MaxInt64}, 1, 1, 18_446_744_073_710, nil},
		{"rounding addition exceeds int64", Price{math.MaxInt64, 0}, 1, 0, 9_223_372_036_855, nil},
		{"exact maximum cost", Price{math.MaxInt64, 0}, 1_000_000, 0, math.MaxInt64, nil},
		{"round up to maximum", Price{math.MaxInt64 - 1, 1}, 1_000_000, 1, math.MaxInt64, nil},
		{"one fraction above maximum", Price{math.MaxInt64, 1}, 1_000_000, 1, 0, ErrCostOverflow},
		{"above maximum", Price{math.MaxInt64, 0}, 1_000_001, 0, 0, ErrCostOverflow},
		{"negative input", Price{1, 1}, -1, 0, 0, ErrInvalidUsage},
		{"negative output", Price{1, 1}, 0, -1, 0, ErrInvalidUsage},
		{"negative input price", Price{-1, 1}, 0, 0, 0, ErrInvalidUsage},
		{"negative output price", Price{1, -1}, 0, 0, 0, ErrInvalidUsage},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := tc.price.Cost(tc.in, tc.out)
			if got != tc.want || !errors.Is(err, tc.wantErr) {
				t.Fatalf("Cost(%d,%d) = %d, %v; want %d, %v", tc.in, tc.out, got, err, tc.want, tc.wantErr)
			}
		})
	}
}

// R20/T29: known token counts can still have an unrepresentable cost.
// No result is published, no charge is fabricated, and no call is repeated.
func TestUnsettleableUsageHoldsReservation(t *testing.T) {
	cases := []struct {
		name    string
		in, out int
		wantErr error
	}{
		{"cost overflow", 1_000_001, 0, ErrCostOverflow},
		{"negative input", -1, 0, ErrInvalidUsage},
		{"negative output", 0, -1, ErrInvalidUsage},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ops := NewMemoryOperationStore()
			budget, _ := NewLedger(usd(1_000_000))
			r := &NodeRunner{Store: ops, WorkItemID: "q1", NodeRevision: "generate@1", MaxAttempts: 1,
				InvocationID: func(graph.State) (string, error) { return "inv", nil }, Budget: budget, Estimate: usd(1_000_000)}
			calls, applied := 0, false
			call := ChatCall{Client: clientFunc(func(context.Context, model.Request) (model.Response, error) {
				calls++
				return model.Response{Provider: "primary", UsageKnown: true, InputTokens: tc.in, OutputTokens: tc.out}, nil
			}), Pin: "primary", Price: Price{InputMicrosPerMTok: math.MaxInt64, OutputMicrosPerMTok: math.MaxInt64},
				Build: func(graph.State) (model.Request, error) { return model.Request{MaxOutputTokens: 1}, nil },
				Apply: func(graph.State, model.Response) (graph.State, error) {
					applied = true
					return graph.State{"answer": "must not publish"}, nil
				}}
			node, err := call.Node(r)
			if err != nil {
				t.Fatal(err)
			}
			patch, err := node(context.Background(), graph.State{})
			if !errors.Is(err, tc.wantErr) || patch != nil || applied {
				t.Fatalf("patch=%v applied=%v err=%v", patch, applied, err)
			}
			env := chatEnv{ops: ops, budget: budget, node: node}
			op := env.op(t)
			if op.Status != OpFailed || len(op.Attempts) != 1 || !op.Attempts[0].Unknown || op.Attempts[0].ChargedMicros != 0 || !strings.Contains(op.Error, tc.wantErr.Error()) {
				t.Fatalf("operation %+v", op)
			}
			before := budget.Snapshot()
			if before.Spent != 0 || before.Reserved != 1_000_000 || before.Unknown != 1_000_000 {
				t.Fatalf("budget %+v", before)
			}
			if err := budget.Release("q1/generate@1/inv#1"); err == nil {
				t.Fatal("unreconciled reservation released")
			}
			if _, err := node(context.Background(), graph.State{}); err == nil || calls != 1 || budget.Snapshot() != before {
				t.Fatalf("re-run err=%v calls=%d budget=%+v", err, calls, budget.Snapshot())
			}
		})
	}
}

type clientFunc func(context.Context, model.Request) (model.Response, error)

func (f clientFunc) Chat(ctx context.Context, req model.Request) (model.Response, error) {
	return f(ctx, req)
}

// R20/T27: invalid integer extremes must not wrap into valid retry or
// timeout settings. No request should be constructed from these configs.
func TestModelConfigIntegerBoundaries(t *testing.T) {
	maxTimeoutSeconds := int64(math.MaxInt64) / int64(time.Second)
	for _, retries := range []int{1, MaxTransportAttempts - 1} {
		c := goodConfig("boundary", "http://unused")
		c.MaxRetries = retries
		if err := ValidateModelConfig(c); err != nil {
			t.Fatalf("valid retries %d: %v", retries, err)
		}
	}
	for _, retries := range []int{0, -1, math.MinInt, MaxTransportAttempts, math.MaxInt} {
		c := goodConfig("boundary", "http://unused")
		c.MaxRetries = retries
		if err := ValidateModelConfig(c); !errors.Is(err, ErrModelConfig) {
			t.Errorf("invalid retries %d accepted: %v", retries, err)
		}
		if err := ValidateRouter(model.Router{Entries: []model.Entry{{Config: c}}}); !errors.Is(err, ErrModelConfig) {
			t.Errorf("router accepted invalid retries %d: %v", retries, err)
		}
	}
	// On 32-bit hosts every positive int number of seconds fits Duration.
	if int64(math.MaxInt) <= maxTimeoutSeconds {
		return
	}
	for _, seconds := range []int64{1, maxTimeoutSeconds} {
		c := goodConfig("boundary", "http://unused")
		c.TimeoutSeconds = int(seconds)
		if err := ValidateModelConfig(c); err != nil {
			t.Errorf("valid timeout %d: %v", seconds, err)
		}
	}
	for _, seconds := range []int64{maxTimeoutSeconds + 1, int64(math.MaxInt)} {
		c := goodConfig("boundary", "http://unused")
		c.TimeoutSeconds = int(seconds)
		if err := ValidateModelConfig(c); !errors.Is(err, ErrModelConfig) {
			t.Errorf("unrepresentable timeout %d accepted: %v", seconds, err)
		}
		if err := ValidateRouter(model.Router{Entries: []model.Entry{{Config: c}}}); !errors.Is(err, ErrModelConfig) {
			t.Errorf("router accepted unrepresentable timeout %d: %v", seconds, err)
		}
	}
}

// #174: a header value the HTTP client refuses (a CR from a Windows .env) fails BEFORE any byte is sent: a plain failure with
// the reservation released, never an unknown outcome that keeps the hold.
func TestARequestRejectedBeforeTransportIsAFailureNotAnUnknownOutcome(t *testing.T) {
	ctx := audited(t)
	p := &provider{handler: answer("42", true)}
	c := goodConfig("primary", p.start(t))
	c.APIKeyEnv = "LAB_TEST_BAD_KEY"
	t.Setenv("LAB_TEST_BAD_KEY", "sk-test-key\r")
	hc, err := model.New(c)
	if err != nil {
		t.Fatal(err)
	}
	env := newChat(t, model.Router{Entries: []model.Entry{{Config: c, Client: hc}}}, "primary")
	_, err = env.node(ctx, graph.State{})
	if !errors.Is(err, ErrNotSent) || errors.Is(err, ErrOutcomeUnknown) || errors.Is(err, ErrResponseLost) {
		t.Fatalf("err %v", err)
	}
	if strings.Contains(err.Error(), "sk-test-key") {
		t.Fatalf("the key leaked into the error: %v", err)
	}
	if op := env.op(t); op.Status != OpFailed {
		t.Fatalf("op %+v", op)
	}
	if s := env.budget.Snapshot(); s.Reserved != 0 || s.Unknown != 0 || s.Spent != 0 {
		t.Fatalf("the reservation must be released: %+v", s)
	}
	if p.hits.Load() != 0 {
		t.Fatalf("hits %d", p.hits.Load())
	}
}
