package agentflow

// Real-provider chat smoke test (SPEC A42, T81-T84; Refs #7). It needs credentials that are not in
// this repository, so without them it records "blocked" and skips: a skipped smoke is never a pass.
// With them it makes ONE pinned chat call through ChatCall/NodeRunner/Ledger (the same path the
// worker graph uses) and writes a report of version, usage, cost and attempts.
//
//	LAB_LLM_SMOKE_BASE_URL   provider base URL (https://...)           required
//	LAB_LLM_SMOKE_MODEL      model name                                required
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
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
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
	TextLength     int     `json:"text_length"`
	TextSHA256     string  `json:"text_sha256,omitempty"`
	LedgerSpent    int64   `json:"ledger_spent_micros"`
	LedgerReserved int64   `json:"ledger_reserved_micros"`
}

type smokeEnv struct {
	BaseURL, Model, Protocol string
	Price                    Price
}

// loadSmokeEnv returns the configuration or the reason the smoke is blocked.
func loadSmokeEnv(get func(string) string) (smokeEnv, string) {
	e := smokeEnv{BaseURL: get("LAB_LLM_SMOKE_BASE_URL"), Model: get("LAB_LLM_SMOKE_MODEL"),
		Protocol: get("LAB_LLM_SMOKE_PROTOCOL")}
	if e.Protocol == "" {
		e.Protocol = "openai_chat"
	}
	var missing []string
	for _, k := range []string{"LAB_LLM_SMOKE_BASE_URL", "LAB_LLM_SMOKE_MODEL", smokeKeyEnv} {
		if get(k) == "" {
			missing = append(missing, k)
		}
	}
	in, errIn := strconv.ParseInt(get("LAB_LLM_SMOKE_PRICE_IN_MICROS_PER_MTOK"), 10, 64)
	out, errOut := strconv.ParseInt(get("LAB_LLM_SMOKE_PRICE_OUT_MICROS_PER_MTOK"), 10, 64)
	if errIn != nil || errOut != nil || in < 0 || out < 0 {
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

// runSmoke makes the single pinned call. It never returns the key, prompt or reply text.
func runSmoke(ctx context.Context, e smokeEnv) (rep SmokeReport) {
	rep = SmokeReport{Status: "failed", Protocol: e.Protocol, Model: e.Model, ChargedMicros: UsageUnknown}
	u, err := url.Parse(e.BaseURL)
	if err != nil || u.Host == "" {
		rep.Reason = "invalid base url"
		return rep
	}
	rep.Host = u.Hostname()
	rep.Real = !isLoopback(u.Hostname())
	cfg := model.Config{Name: "smoke", Protocol: e.Protocol, BaseURL: e.BaseURL, Model: e.Model, APIKeyEnv: smokeKeyEnv,
		MaxRetries: 1, TimeoutSeconds: 60, RequestsPerMinute: 30}
	if err := ValidateModelConfig(cfg); err != nil {
		rep.Reason = "model config: " + err.Error()
		return rep
	}
	client, err := model.NewContext(ctx, cfg)
	if err != nil {
		rep.Reason = "client: " + err.Error()
		return rep
	}
	budget, _ := NewLedger(usd(1_000_000))
	runner := &NodeRunner{Store: NewMemoryOperationStore(), WorkItemID: "smoke", NodeRevision: "smoke@1", MaxAttempts: 1,
		InvocationID: func(graph.State) (string, error) { return "inv", nil }, Budget: budget, Estimate: usd(100_000)}
	var seen model.Response
	call := ChatCall{Client: model.Router{Entries: []model.Entry{{Config: cfg, Client: client}}}, Pin: cfg.Name, Price: e.Price,
		Build: func(graph.State) (model.Request, error) {
			return model.Request{Messages: []model.Message{{Role: "user", Content: "Reply with the single word: OK."}},
				MaxOutputTokens: 16}, nil
		},
		Apply: func(_ graph.State, resp model.Response) (graph.State, error) {
			seen = resp
			return graph.State{"len": len(resp.Text)}, nil
		}}
	node, err := call.Node(runner)
	if err != nil {
		rep.Reason = "node: " + err.Error()
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
	rep.TextLength = len(seen.Text)
	sum := sha256.Sum256([]byte(seen.Text))
	rep.TextSHA256 = hex.EncodeToString(sum[:])
	if rep.UsageKnown {
		rep.ChargedMicros, _ = e.Price.Cost(rep.InputTokens, rep.OutputTokens)
	}
	switch {
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

func containsAny(s string, needles ...string) bool {
	for _, n := range needles {
		if strings.Contains(s, n) {
			return true
		}
	}
	return false
}
