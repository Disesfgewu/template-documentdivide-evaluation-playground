package agentflow

// Real-provider chat smoke test (SPEC A42, T81-T84; Refs #7). It needs credentials that are not in
// this repository, so without them it records "blocked" and skips: a skipped smoke is never a pass.
// With them it makes ONE pinned chat call through ChatCall/NodeRunner/Ledger (the same path the
// worker graph uses) and writes a report of version, usage, cost and attempts.
//
//	LAB_LLM_SMOKE_BASE_URL   provider base URL (https://...)           required
//	LAB_LLM_SMOKE_MODEL      model name                                required
//	LAB_LLM_SMOKE_ALLOWED_HOST  the only host the key may reach (must equal the URL host)  required
//	LAB_LLM_SMOKE_API_KEY    the key (read by agentKit from this env)  required, never logged
//	LAB_LLM_SMOKE_PROTOCOL   agentKit protocol, default openai_chat
//	LAB_LLM_SMOKE_PRICE_IN_MICROS_PER_MTOK / ..._OUT_...               required (micro-USD per 1M tokens)
//	LAB_LLM_SMOKE_REPORT     file that receives the JSON report
//
// The report never contains the key, the prompt or the model's text (only its length and sha256).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"crypto/sha256"
	"encoding/hex"

	"github.com/Disesfgewu/agentkit/graph"
	"github.com/Disesfgewu/agentkit/model"
)

const smokeKeyEnv = "LAB_LLM_SMOKE_API_KEY"

// SmokeReport is what a smoke run leaves behind. Status is "passed", "failed" or "blocked".
type SmokeReport struct {
	Status         string  `json:"status"`
	Reason         string  `json:"reason,omitempty"`
	Real           bool    `json:"real"` // false for loopback endpoints (local fakes)
	Host           string  `json:"host,omitempty"`
	Protocol       string  `json:"protocol,omitempty"`
	Model          string  `json:"model,omitempty"`
	UsageKnown     bool    `json:"usage_known"`
	InputTokens    int     `json:"input_tokens"`
	OutputTokens   int     `json:"output_tokens"`
	ChargedMicros  int64   `json:"charged_micros"` // -1 = unknown, never recorded as zero
	Attempts       int     `json:"transport_attempts"`
	WallSeconds    float64 `json:"wall_seconds"`
	ResponseModel  string  `json:"response_model,omitempty"`
	RequestSHA256  string  `json:"request_sha256,omitempty"`
	TextLength     int     `json:"text_length"`
	TextSHA256     string  `json:"text_sha256,omitempty"`
	LedgerSpent    int64   `json:"ledger_spent_micros"`
	LedgerReserved int64   `json:"ledger_reserved_micros"`
}

type smokeEnv struct {
	BaseURL, Model, Protocol string
	AllowedHost              string // the only host the key may be sent to
	Price                    Price
}

// maxPriceMicros is a sanity cap (micro-USD per 1M tokens): 1,000 USD per million tokens.
const maxPriceMicros = 1_000_000_000

// loadSmokeEnv returns the configuration or the reason the smoke is blocked.
func loadSmokeEnv(get func(string) string) (smokeEnv, string) {
	e := smokeEnv{BaseURL: get("LAB_LLM_SMOKE_BASE_URL"), Model: get("LAB_LLM_SMOKE_MODEL"),
		Protocol: get("LAB_LLM_SMOKE_PROTOCOL"), AllowedHost: get("LAB_LLM_SMOKE_ALLOWED_HOST")}
	if e.Protocol == "" {
		e.Protocol = "openai_chat"
	}
	var missing []string
	for _, k := range []string{"LAB_LLM_SMOKE_BASE_URL", "LAB_LLM_SMOKE_MODEL", "LAB_LLM_SMOKE_ALLOWED_HOST", smokeKeyEnv} {
		if get(k) == "" {
			missing = append(missing, k)
		}
	}
	in, errIn := strconv.ParseInt(get("LAB_LLM_SMOKE_PRICE_IN_MICROS_PER_MTOK"), 10, 64)
	out, errOut := strconv.ParseInt(get("LAB_LLM_SMOKE_PRICE_OUT_MICROS_PER_MTOK"), 10, 64)
	if errIn != nil || errOut != nil || in < 0 || out < 0 || in > maxPriceMicros || out > maxPriceMicros {
		missing = append(missing, "LAB_LLM_SMOKE_PRICE_IN/OUT_MICROS_PER_MTOK")
	}
	if len(missing) > 0 {
		return e, fmt.Sprintf("blocked: no credentials or configuration (%v)", missing)
	}
	e.Price = Price{InputMicrosPerMTok: in, OutputMicrosPerMTok: out}
	return e, ""
}

func isLoopback(host string) bool {
	return host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "[::1]"
}

// isRealHost is true only for a public DNS name: not loopback, not an IP literal, not a private,
// link-local or metadata address, not a single-label or internal-looking name.
func isRealHost(host string) bool {
	if host == "" || isLoopback(host) || net.ParseIP(host) != nil {
		return false
	}
	h := strings.ToLower(strings.TrimSuffix(host, "."))
	if !strings.Contains(h, ".") {
		return false
	}
	for _, suffix := range []string{".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".test", ".invalid", ".example"} {
		if strings.HasSuffix(h, suffix) {
			return false
		}
	}
	return true
}

// checkEndpoint refuses a base URL the key must not be sent to. Loopback http endpoints are allowed
// only as local fakes (they are never "real"); everything else needs https, no userinfo, no query or
// fragment, and a host equal to the explicitly allowed one.
func checkEndpoint(e smokeEnv) (host string, real bool, reason string) {
	u, err := url.Parse(e.BaseURL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", false, "invalid base url"
	}
	host = u.Hostname()
	if isLoopback(host) {
		return host, false, "" // a local fake; the report says real=false and the test refuses to pass it
	}
	if u.Scheme != "https" {
		return host, false, "base url must be https"
	}
	if e.AllowedHost == "" || !strings.EqualFold(e.AllowedHost, host) {
		return host, false, "base url host is not the allowed host"
	}
	return host, isRealHost(host), ""
}

// runSmoke makes the single pinned call. It never returns the key, prompt or reply text.
func runSmoke(ctx context.Context, e smokeEnv) (rep SmokeReport) {
	rep = SmokeReport{Status: "failed", Protocol: e.Protocol, Model: e.Model, ChargedMicros: UsageUnknown}
	host, real, reason := checkEndpoint(e)
	rep.Host, rep.Real = host, real
	if reason != "" {
		rep.Reason = reason
		return rep
	}
	cfg := model.Config{Name: "smoke", Protocol: e.Protocol, BaseURL: e.BaseURL, Model: e.Model, APIKeyEnv: smokeKeyEnv,
		MaxRetries: 1, TimeoutSeconds: 60, RequestsPerMinute: 30}
	if err := ValidateModelConfig(cfg); err != nil {
		rep.Reason = "invalid model config"
		return rep
	}
	client, err := model.NewContext(ctx, cfg)
	if err != nil {
		rep.Reason = "client construction failed"
		return rep
	}
	// Never follow a redirect: some protocols send the key in a header Go does not strip on a
	// cross-host redirect. A 30x is returned to agentKit as an error status instead.
	hc := *client.HTTP
	hc.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	client.HTTP = &hc
	budget, _ := NewLedger(usd(1_000_000))
	runner := &NodeRunner{Store: NewMemoryOperationStore(), WorkItemID: "smoke", NodeRevision: "smoke@1", MaxAttempts: 1,
		InvocationID: func(graph.State) (string, error) { return "inv", nil }, Budget: budget, Estimate: usd(100_000)}
	var seen model.Response
	const prompt = "Reply with the single word: OK."
	reqSum := sha256.Sum256([]byte(e.Model + "\x00" + prompt))
	rep.RequestSHA256 = hex.EncodeToString(reqSum[:])
	call := ChatCall{Client: model.Router{Entries: []model.Entry{{Config: cfg, Client: client}}}, Pin: cfg.Name, Price: e.Price,
		Build: func(graph.State) (model.Request, error) {
			return model.Request{Messages: []model.Message{{Role: "user", Content: prompt}},
				MaxOutputTokens: 16}, nil
		},
		Apply: func(_ graph.State, resp model.Response) (graph.State, error) {
			seen = resp
			return graph.State{"len": len(resp.Text)}, nil
		}}
	node, err := call.Node(runner)
	if err != nil {
		rep.Reason = "node construction failed"
		return rep
	}
	began := time.Now()
	patch, err := node(ctx, graph.State{})
	rep.WallSeconds = time.Since(began).Seconds()
	snap := budget.Snapshot()
	rep.LedgerSpent, rep.LedgerReserved = int64(snap.Spent), int64(snap.Reserved)
	if err != nil {
		var pe *ProviderStatusError
		switch {
		case errors.As(err, &pe):
			rep.Reason = fmt.Sprintf("provider status %d", pe.Status)
		case errors.Is(err, ErrResponseLost):
			rep.Reason = "response lost (outcome unknown)"
		default:
			rep.Reason = "call failed"
		}
		return rep
	}
	info, _ := patch["llm_call"].(map[string]any)
	rep.UsageKnown, _ = info["usage_known"].(bool)
	rep.InputTokens, _ = info["input_tokens"].(int)
	rep.OutputTokens, _ = info["output_tokens"].(int)
	rep.Attempts, _ = info["transport_attempts"].(int)
	rep.ResponseModel = seen.Model
	rep.TextLength = len(seen.Text)
	sum := sha256.Sum256([]byte(seen.Text))
	rep.TextSHA256 = hex.EncodeToString(sum[:])
	if rep.UsageKnown {
		rep.ChargedMicros, _ = e.Price.Cost(rep.InputTokens, rep.OutputTokens)
	}
	switch {
	case rep.Real && (!rep.UsageKnown || rep.InputTokens <= 0 || rep.OutputTokens <= 0):
		rep.Reason = "a real run must report usage"
	case rep.TextLength == 0:
		rep.Reason = "empty reply"
	case !rep.UsageKnown && rep.LedgerSpent != 0:
		rep.Reason = "unknown usage was settled as a cost" // never allowed (SPEC: unknown is not zero)
	case rep.UsageKnown && rep.LedgerSpent != rep.ChargedMicros:
		rep.Reason = "ledger does not match the priced usage"
	default:
		rep.Status = "passed"
	}
	return rep
}

func writeSmokeReport(t *testing.T, rep SmokeReport) {
	t.Helper()
	path := os.Getenv("LAB_LLM_SMOKE_REPORT")
	if path == "" {
		return
	}
	data, err := json.MarshalIndent(rep, "", "  ")
	if err == nil {
		err = os.WriteFile(path, data, 0o600)
	}
	if err != nil {
		t.Fatalf("write report: %v", err)
	}
}

// TestRealProviderSmoke is the credentialed smoke. Without credentials it records "blocked".
func TestRealProviderSmoke(t *testing.T) {
	e, blocked := loadSmokeEnv(os.Getenv)
	if blocked != "" {
		writeSmokeReport(t, SmokeReport{Status: "blocked", Reason: blocked, ChargedMicros: UsageUnknown})
		t.Skip(blocked)
	}
	ctx := audited(t)
	rep := runSmoke(ctx, e)
	writeSmokeReport(t, rep)
	if rep.Status != "passed" {
		t.Fatalf("smoke %s: %s", rep.Status, rep.Reason)
	}
	if !rep.Real {
		t.Fatalf("a loopback endpoint is not a real provider")
	}
}

// The harness itself is tested against local fakes (these prove the plumbing, not a real provider).

func fakeEnv(url string, price Price) smokeEnv {
	return smokeEnv{BaseURL: url, Model: "fake-model", Protocol: "openai_chat", Price: price}
}

func TestSmokeHarnessBlockedWithoutCredentials(t *testing.T) {
	_, reason := loadSmokeEnv(func(string) string { return "" })
	if reason == "" || reason[:8] != "blocked:" {
		t.Fatalf("reason %q", reason)
	}
	partial := func(k string) string {
		return map[string]string{"LAB_LLM_SMOKE_BASE_URL": "https://example.invalid", "LAB_LLM_SMOKE_MODEL": "m"}[k]
	}
	if _, reason := loadSmokeEnv(partial); reason == "" {
		t.Fatal("a missing key and price must keep the smoke blocked")
	}
}

func TestSmokeHarnessRecordsUsageCostAndNeverPassesALoopback(t *testing.T) {
	t.Setenv(smokeKeyEnv, "test-key-not-real")
	p := &provider{handler: answer("OK", true)}
	rep := runSmoke(audited(t), fakeEnv(p.start(t), Price{InputMicrosPerMTok: 3_000_000, OutputMicrosPerMTok: 15_000_000}))
	if rep.Status != "passed" || rep.Real || !rep.UsageKnown || rep.InputTokens != 1000 || rep.OutputTokens != 500 {
		t.Fatalf("report %+v", rep)
	}
	if rep.ChargedMicros != 10_500 || rep.LedgerSpent != 10_500 || rep.LedgerReserved != 0 || rep.Attempts != 1 {
		t.Fatalf("cost %+v", rep)
	}
	if rep.TextLength != 2 || len(rep.TextSHA256) != 64 {
		t.Fatalf("text fields %+v", rep)
	}
	data, _ := json.Marshal(rep)
	if s := string(data); containsAny(s, "test-key-not-real", "Reply with") {
		t.Fatalf("report leaks a secret or prompt: %s", s)
	}
}

func TestSmokeHarnessUnknownUsageIsNotZeroCost(t *testing.T) {
	t.Setenv(smokeKeyEnv, "test-key-not-real")
	p := &provider{handler: answer("OK", false)}
	rep := runSmoke(audited(t), fakeEnv(p.start(t), Price{InputMicrosPerMTok: 3_000_000, OutputMicrosPerMTok: 15_000_000}))
	if rep.Status != "passed" || rep.UsageKnown || rep.ChargedMicros != UsageUnknown || rep.LedgerSpent != 0 {
		t.Fatalf("report %+v", rep)
	}
	if rep.LedgerReserved == 0 {
		t.Fatalf("an unknown outcome must keep its reservation, got %+v", rep)
	}
}

func TestSmokeHarnessFailuresAreRecordedNotPassed(t *testing.T) {
	t.Setenv(smokeKeyEnv, "test-key-not-real")
	for name, h := range map[string]func(int32, http.ResponseWriter, *http.Request){
		"401": status(401), "503": status(503), "empty": answer("", true),
	} {
		t.Run(name, func(t *testing.T) {
			p := &provider{handler: h}
			rep := runSmoke(audited(t), fakeEnv(p.start(t), Price{InputMicrosPerMTok: 1, OutputMicrosPerMTok: 1}))
			if rep.Status != "failed" || rep.Reason == "" {
				t.Fatalf("report %+v", rep)
			}
		})
	}
}

func TestSmokeEndpointRules(t *testing.T) {
	cases := []struct {
		name, url, allowed string
		real               bool
		reason             string
	}{
		{"https allowed public host", "https://api.example-llm.com/v1", "api.example-llm.com", true, ""},
		{"http public host", "http://api.example-llm.com/v1", "api.example-llm.com", false, "must be https"},
		{"host not allowed", "https://evil.example-llm.com/v1", "api.example-llm.com", false, "not the allowed host"},
		{"no allowed host", "https://api.example-llm.com", "", false, "not the allowed host"},
		{"userinfo", "https://user:pw@api.example-llm.com", "api.example-llm.com", false, "invalid base url"},
		{"query string", "https://api.example-llm.com/v1?key=x", "api.example-llm.com", false, "invalid base url"},
		{"private ip literal", "https://10.0.0.5/v1", "10.0.0.5", false, ""},
		{"metadata ip", "https://169.254.169.254/v1", "169.254.169.254", false, ""},
		{"single label host", "https://llm/v1", "llm", false, ""},
		{"internal suffix", "https://llm.internal/v1", "llm.internal", false, ""},
		{"loopback http fake", "http://127.0.0.1:9999", "", false, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, real, reason := checkEndpoint(smokeEnv{BaseURL: c.url, AllowedHost: c.allowed})
			if real != c.real || (c.reason == "") != (reason == "") || !strings.Contains(reason, c.reason) {
				t.Fatalf("real=%v reason=%q, want real=%v reason~%q", real, reason, c.real, c.reason)
			}
		})
	}
}

func TestSmokeNeverFollowsARedirectWithTheKey(t *testing.T) {
	t.Setenv(smokeKeyEnv, "test-key-not-real")
	var leaked atomic.Int32
	target := &provider{handler: func(_ int32, w http.ResponseWriter, r *http.Request) {
		leaked.Add(1)
		answer("OK", true)(0, w, r)
	}}
	targetURL := target.start(t)
	redirector := &provider{handler: func(_ int32, w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, targetURL, http.StatusTemporaryRedirect)
	}}
	rep := runSmoke(audited(t), fakeEnv(redirector.start(t), Price{InputMicrosPerMTok: 1, OutputMicrosPerMTok: 1}))
	if leaked.Load() != 0 {
		t.Fatalf("the redirect target received %d request(s): the key could have leaked", leaked.Load())
	}
	if rep.Status != "failed" {
		t.Fatalf("report %+v", rep)
	}
}

func TestSmokeReportHasProvenanceAndFixedReasons(t *testing.T) {
	t.Setenv(smokeKeyEnv, "test-key-not-real")
	p := &provider{handler: answer("OK", true)}
	rep := runSmoke(audited(t), fakeEnv(p.start(t), Price{InputMicrosPerMTok: 1, OutputMicrosPerMTok: 1}))
	if len(rep.RequestSHA256) != 64 || rep.ResponseModel == "" || rep.Host != "127.0.0.1" {
		t.Fatalf("provenance missing: %+v", rep)
	}
}

func TestSmokePriceSanityCaps(t *testing.T) {
	get := func(in, out string) func(string) string {
		return func(k string) string {
			return map[string]string{"LAB_LLM_SMOKE_BASE_URL": "https://a.example-llm.com", "LAB_LLM_SMOKE_MODEL": "m",
				"LAB_LLM_SMOKE_ALLOWED_HOST": "a.example-llm.com", smokeKeyEnv: "k",
				"LAB_LLM_SMOKE_PRICE_IN_MICROS_PER_MTOK": in, "LAB_LLM_SMOKE_PRICE_OUT_MICROS_PER_MTOK": out}[k]
		}
	}
	if _, reason := loadSmokeEnv(get("3000000", "15000000")); reason != "" {
		t.Fatalf("a normal price is blocked: %s", reason)
	}
	if _, reason := loadSmokeEnv(get("999999999999", "1")); reason == "" {
		t.Fatal("an absurd price must be refused")
	}
}

func containsAny(s string, needles ...string) bool {
	for _, n := range needles {
		if strings.Contains(s, n) {
			return true
		}
	}
	return false
}
