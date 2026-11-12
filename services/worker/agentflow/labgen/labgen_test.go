package labgen

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

const secretKey = "sk-test-key-not-real-0123456789"

type fake struct {
	hits    atomic.Int32
	bodies  chan string
	handler func(n int32, w http.ResponseWriter, r *http.Request)
}

func (f *fake) start(t *testing.T) string {
	f.bodies = make(chan string, 64)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		f.bodies <- string(b)
		f.handler(f.hits.Add(1), w, r)
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

func reply(text string, in, out int, usage bool) func(int32, http.ResponseWriter, *http.Request) {
	return func(_ int32, w http.ResponseWriter, _ *http.Request) {
		u := ""
		if usage {
			u = fmt.Sprintf(`,"usage":{"prompt_tokens":%d,"completion_tokens":%d}`, in, out)
		}
		fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]%s}`, text, u)
	}
}

func input(url string, calls ...string) Input {
	in := Input{
		Model: ModelSpec{Name: "fake-model", Protocol: "openai_chat", BaseURL: url, MaxOutputTokens: 500,
			InputMicrosPerMTok: 3_000_000, OutputMicrosPerMTok: 15_000_000, Currency: "USD", PricingRevision: "test-1",
			TimeoutSeconds: 10, MaxRetries: 1, RequestsPerMinute: 6000},
		Budget: BudgetSpec{LimitMicros: 1_000_000},
	}
	for i, c := range calls {
		in.Calls = append(in.Calls, CallSpec{ID: fmt.Sprintf("c%d", i), System: "sys", User: c})
	}
	return in
}

var loop = Options{AllowLoopback: true}

func TestCapAboveFiveHundredIsRefusedBeforeAnyCall(t *testing.T) {
	f := &fake{handler: reply("x", 5, 5, true)}
	url := f.start(t)
	for _, n := range []int{501, 1024, 100000, 0, -1} {
		in := input(url, "q")
		in.Model.MaxOutputTokens = n
		out := Run(context.Background(), in, loop)
		if out.Status != "refused" || len(out.Calls) != 0 {
			t.Fatalf("cap %d: %+v", n, out)
		}
	}
	if f.hits.Load() != 0 {
		t.Fatalf("a refused input made %d call(s)", f.hits.Load())
	}
}

func TestTheRequestCarriesTheCapAndNoMoreThanFiveHundred(t *testing.T) {
	t.Setenv(KeyEnv, secretKey)
	f := &fake{handler: reply("x", 5, 5, true)}
	out := Run(context.Background(), input(f.start(t), "q"), loop)
	if out.Status != "ok" {
		t.Fatalf("%+v", out)
	}
	var body map[string]any
	if err := json.Unmarshal([]byte(<-f.bodies), &body); err != nil {
		t.Fatal(err)
	}
	found := false
	for k, v := range body {
		if strings.Contains(k, "max") && strings.Contains(k, "token") {
			found = true
			if n, _ := v.(float64); n < 1 || n > 500 {
				t.Fatalf("%s = %v", k, v)
			}
		}
	}
	if !found {
		t.Fatalf("no token cap in the request: %v", body)
	}
}

func TestEndpointRules(t *testing.T) {
	cases := []struct{ name, url, allowed, want string }{
		{"http", "http://api.example-llm.com", "api.example-llm.com", "must be https"},
		{"other host", "https://evil.example-llm.com", "api.example-llm.com", "not the allowed host"},
		{"no allowed host", "https://api.example-llm.com", "", "not the allowed host"},
		{"userinfo", "https://u:p@api.example-llm.com", "api.example-llm.com", "invalid base url"},
		{"query", "https://api.example-llm.com/?k=1", "api.example-llm.com", "invalid base url"},
		{"metadata ip", "https://169.254.169.254", "169.254.169.254", "not a public DNS name"},
		{"internal", "https://llm.internal", "llm.internal", "not a public DNS name"},
		{"loopback without the test option", "http://127.0.0.1:9", "", "must be https"},
	}
	for _, c := range cases {
		in := input(c.url, "q")
		in.Model.AllowedHost = c.allowed
		out := Run(context.Background(), in, Options{})
		if out.Status != "refused" || !strings.Contains(out.Reason, c.want) {
			t.Fatalf("%s: %+v", c.name, out)
		}
	}
	in := input("https://api.anthropic.com", "q")
	in.Model.AllowedHost = "api.anthropic.com"
	if _, err := Validate(in, Options{}); err != nil {
		t.Fatalf("the real endpoint shape is refused: %v", err)
	}
}

func TestCostsAreSettledFromKnownUsageAndTheKeyNeverLeaks(t *testing.T) {
	t.Setenv(KeyEnv, secretKey)
	var auth atomic.Value
	f := &fake{handler: func(n int32, w http.ResponseWriter, r *http.Request) {
		auth.Store(r.Header.Get("Authorization"))
		reply("answer", 5, 20, true)(n, w, r)
	}}
	in := input(f.start(t), "first prompt", "second prompt")
	out := Run(context.Background(), in, loop)
	if out.Status != "ok" || len(out.Calls) != 2 {
		t.Fatalf("%+v", out)
	}
	for _, c := range out.Calls {
		if c.Status != "ok" || !c.UsageKnown || c.InputTokens != 5 || c.OutputTokens != 20 || c.ChargedMicros != 315 || c.Text != "answer" {
			t.Fatalf("%+v", c)
		}
	}
	if out.LedgerSpent != 630 || out.LedgerReserved != 0 || out.LedgerUnknown != 0 {
		t.Fatalf("%+v", out)
	}
	data, _ := json.Marshal(out)
	if strings.Contains(string(data), secretKey) || strings.Contains(string(data), "first prompt") {
		t.Fatalf("output leaks the key or a prompt: %s", data)
	}
	if got, _ := auth.Load().(string); !strings.Contains(got, secretKey) {
		t.Fatalf("the key was not sent to the (allowed) endpoint: %q", got)
	}
}

func TestBudgetIsReservedBeforeTheCallAndFailsClosed(t *testing.T) {
	t.Setenv(KeyEnv, secretKey)
	f := &fake{handler: reply("a", 5, 20, true)}
	in := input(f.start(t), "q1", "q2", "q3", "q4")
	// Worst case per call is 3 + 7500 micros; after two settled calls (315 each) a third reservation exceeds 8000.
	in.Budget.LimitMicros = 8000
	out := Run(context.Background(), in, loop)
	got := []string{}
	for _, c := range out.Calls {
		got = append(got, c.Status)
	}
	if strings.Join(got, ",") != "ok,ok,skipped_budget,skipped_after_stop" || out.Status != "partial" {
		t.Fatalf("%v %+v", got, out)
	}
	if f.hits.Load() != 2 {
		t.Fatalf("the provider saw %d calls; a call that does not fit must never be made", f.hits.Load())
	}
	if out.LedgerSpent != 630 {
		t.Fatalf("%+v", out)
	}
}

func TestUnknownUsageIsNeverSettledAsZero(t *testing.T) {
	t.Setenv(KeyEnv, secretKey)
	f := &fake{handler: reply("a", 0, 0, false)}
	out := Run(context.Background(), input(f.start(t), "q"), loop)
	c := out.Calls[0]
	if c.Status != "ok" || c.UsageKnown || c.ChargedMicros != -1 {
		t.Fatalf("%+v", c)
	}
	if out.LedgerSpent != 0 || out.LedgerUnknown == 0 || out.LedgerReserved == 0 {
		t.Fatalf("unknown usage must keep its reservation: %+v", out)
	}
}

func TestProviderErrorsAreRecordedAndNotPassed(t *testing.T) {
	t.Setenv(KeyEnv, secretKey)
	for _, code := range []int{401, 429, 503} {
		f := &fake{handler: func(_ int32, w http.ResponseWriter, _ *http.Request) { w.WriteHeader(code) }}
		out := Run(context.Background(), input(f.start(t), "q"), loop)
		if out.Status != "partial" || out.Calls[0].Status != "failed" || !strings.Contains(out.Calls[0].Reason, fmt.Sprint(code)) {
			t.Fatalf("%d: %+v", code, out)
		}
		if out.LedgerSpent != 0 {
			t.Fatalf("%d: a failed call was charged: %+v", code, out)
		}
	}
}

func TestARedirectIsNeverFollowedWithTheKey(t *testing.T) {
	t.Setenv(KeyEnv, secretKey)
	target := &fake{handler: reply("a", 5, 5, true)}
	targetURL := target.start(t)
	redirector := &fake{handler: func(_ int32, w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, targetURL, http.StatusTemporaryRedirect)
	}}
	out := Run(context.Background(), input(redirector.start(t), "q"), loop)
	if target.hits.Load() != 0 {
		t.Fatalf("the redirect target received %d request(s): the key could have leaked", target.hits.Load())
	}
	if out.Calls[0].Status == "ok" {
		t.Fatalf("%+v", out)
	}
}

func TestInputRules(t *testing.T) {
	f := &fake{handler: reply("a", 5, 5, true)}
	url := f.start(t)
	bad := map[string]func(*Input){
		"no calls":         func(i *Input) { i.Calls = nil },
		"duplicate ids":    func(i *Input) { i.Calls[1].ID = i.Calls[0].ID },
		"empty prompt":     func(i *Input) { i.Calls[0].System, i.Calls[0].User = "", "" },
		"no pricing":       func(i *Input) { i.Model.PricingRevision = "" },
		"no budget":        func(i *Input) { i.Budget.LimitMicros = 0 },
		"zero retries":     func(i *Input) { i.Model.MaxRetries = 0 },
		"no timeout":       func(i *Input) { i.Model.TimeoutSeconds = 0 },
		"absurd price":     func(i *Input) { i.Model.InputMicrosPerMTok = 1 << 40 },
		"unknown protocol": func(i *Input) { i.Model.Protocol = "nope" },
	}
	for name, mutate := range bad {
		in := input(url, "q1", "q2")
		mutate(&in)
		if out := Run(context.Background(), in, loop); out.Status != "refused" {
			t.Fatalf("%s: %+v", name, out)
		}
	}
	if f.hits.Load() != 0 {
		t.Fatalf("%d call(s) were made for refused inputs", f.hits.Load())
	}
}
