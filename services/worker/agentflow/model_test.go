package agentflow

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

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
