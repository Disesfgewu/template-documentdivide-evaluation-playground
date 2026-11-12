// Package labgen is the LLM call stage of question generation (P-D slice 1, Refs #7, PM-DECISION 2026-11-12 G-lite).
//
// It assembles THIS project's graph from agentKit objects (model client, graph node, agentlog) and the agentflow
// wrappers (ChatCall, NodeRunner, Ledger, Price): every call goes through ChatCall inside a NodeRunner with a budget
// reservation, so an unknown usage is never settled as zero and a call that would exceed the budget is never made.
//
// Hard rules enforced HERE, before any network call:
//   - max_output_tokens is 1..HardMaxOutputTokens (500) per call, rejected above (owner decision, Issue #7);
//   - https only, host equal to the explicitly allowed host and a public DNS name (loopback http only for the test
//     entry point, never from JSON input), no userinfo/query/fragment, redirects are never followed;
//   - the API key is read from the environment variable KeyEnv by agentKit, and appears nowhere in the output;
//   - the budget is fail-closed: a call whose worst-case cost does not fit is skipped, and so is every later call.
//
// The package interprets no document text: prompts are data supplied by the caller (Python), and the model's reply
// is returned verbatim for the caller to validate with its own contracts.
package labgen

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"

	"github.com/Disesfgewu/agentkit/graph"
	"github.com/Disesfgewu/agentkit/model"

	"github.com/Disesfgewu/template-documentdivide-evaluation-playground/services/worker/agentflow"
)

// HardMaxOutputTokens is the owner's cap per call (Issue #7, D7).
const HardMaxOutputTokens = 500

// KeyEnv is the only place the API key is read from.
const KeyEnv = "LAB_GEN_API_KEY"

const (
	maxCalls        = 200
	maxPromptBytes  = 64 * 1024
	maxPriceMicros  = 1_000_000_000
	maxBudgetMicros = 1_000_000_000_000
	pin             = "gen"
)

// Input is the JSON the Python side sends on stdin.
type Input struct {
	Model  ModelSpec  `json:"model"`
	Budget BudgetSpec `json:"budget"`
	Calls  []CallSpec `json:"calls"`
}

type ModelSpec struct {
	Name                string `json:"name"`
	Protocol            string `json:"protocol"`
	BaseURL             string `json:"base_url"`
	AllowedHost         string `json:"allowed_host"`
	MaxOutputTokens     int    `json:"max_output_tokens"`
	InputMicrosPerMTok  int64  `json:"input_micros_per_mtok"`
	OutputMicrosPerMTok int64  `json:"output_micros_per_mtok"`
	Currency            string `json:"currency"`
	PricingRevision     string `json:"pricing_revision"`
	TimeoutSeconds      int    `json:"timeout_seconds"`
	MaxRetries          int    `json:"max_retries"`
	RequestsPerMinute   int    `json:"requests_per_minute"`
}

type BudgetSpec struct {
	LimitMicros int64 `json:"limit_micros"`
}

type CallSpec struct {
	ID     string `json:"id"`
	System string `json:"system"`
	User   string `json:"user"`
}

// Output is what goes back on stdout. It never contains the key or the prompts.
type Output struct {
	Status         string       `json:"status"` // ok | partial | refused
	Reason         string       `json:"reason,omitempty"`
	Calls          []CallResult `json:"calls"`
	LedgerSpent    int64        `json:"ledger_spent_micros"`
	LedgerReserved int64        `json:"ledger_reserved_micros"`
	LedgerUnknown  int64        `json:"ledger_unknown_micros"`
}

// CallResult statuses: ok | failed | skipped_budget | skipped_after_stop | outcome_unknown.
type CallResult struct {
	ID            string `json:"id"`
	Status        string `json:"status"`
	Reason        string `json:"reason,omitempty"`
	Text          string `json:"text,omitempty"`
	UsageKnown    bool   `json:"usage_known"`
	InputTokens   int    `json:"input_tokens"`
	OutputTokens  int    `json:"output_tokens"`
	ChargedMicros int64  `json:"charged_micros"` // -1 = unknown, never recorded as zero
	Attempts      int    `json:"transport_attempts"`
	RequestSHA256 string `json:"request_sha256"`
	TextSHA256    string `json:"text_sha256,omitempty"`
}

// Options are the test seams; they are not reachable from JSON.
type Options struct {
	AllowLoopback bool // local fake servers only
}

// Validate refuses an input that does not express the safe profile. It makes no network call.
func Validate(in Input, opt Options) (host string, err error) {
	m := in.Model
	switch {
	case m.Name == "" || len(m.Name) > 128:
		return "", errors.New("model.name is required")
	case m.Protocol == "":
		return "", errors.New("model.protocol is required")
	case m.MaxOutputTokens < 1:
		return "", errors.New("model.max_output_tokens is required")
	case m.MaxOutputTokens > HardMaxOutputTokens:
		return "", fmt.Errorf("model.max_output_tokens %d exceeds the hard cap %d", m.MaxOutputTokens, HardMaxOutputTokens)
	case m.InputMicrosPerMTok < 0 || m.OutputMicrosPerMTok < 0 || m.InputMicrosPerMTok > maxPriceMicros || m.OutputMicrosPerMTok > maxPriceMicros:
		return "", errors.New("model prices are out of range")
	case m.Currency == "" || m.PricingRevision == "":
		return "", errors.New("model.currency and model.pricing_revision are required (prices are configured, not assumed)")
	case in.Budget.LimitMicros <= 0 || in.Budget.LimitMicros > maxBudgetMicros:
		return "", errors.New("budget.limit_micros must be positive and bounded")
	case len(in.Calls) == 0 || len(in.Calls) > maxCalls:
		return "", fmt.Errorf("calls must be 1..%d", maxCalls)
	}
	seen := map[string]bool{}
	for _, c := range in.Calls {
		if c.ID == "" || len(c.ID) > 128 || seen[c.ID] {
			return "", errors.New("call ids must be unique and non-empty")
		}
		seen[c.ID] = true
		if len(c.System)+len(c.User) == 0 || len(c.System)+len(c.User) > maxPromptBytes {
			return "", errors.New("a prompt is empty or too large")
		}
	}
	host, reason := checkEndpoint(m.BaseURL, m.AllowedHost, opt.AllowLoopback)
	if reason != "" {
		return "", errors.New(reason)
	}
	cfg := config(m)
	if err := agentflow.ValidateModelConfig(cfg); err != nil {
		return "", err
	}
	return host, nil
}

func config(m ModelSpec) model.Config {
	return model.Config{Name: pin, Protocol: m.Protocol, BaseURL: m.BaseURL, Model: m.Name, APIKeyEnv: KeyEnv,
		MaxRetries: m.MaxRetries, TimeoutSeconds: m.TimeoutSeconds, RequestsPerMinute: m.RequestsPerMinute}
}

func isLoopback(host string) bool {
	return host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "[::1]"
}

func isPublicHost(host string) bool {
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

func checkEndpoint(base, allowed string, allowLoopback bool) (host, reason string) {
	u, err := url.Parse(base)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", "invalid base url"
	}
	host = u.Hostname()
	if allowLoopback && isLoopback(host) {
		return host, ""
	}
	if u.Scheme != "https" {
		return host, "base url must be https"
	}
	if allowed == "" || !strings.EqualFold(allowed, host) {
		return host, "base url host is not the allowed host"
	}
	if !isPublicHost(host) {
		return host, "base url host is not a public DNS name"
	}
	return host, ""
}

func sum(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

// Run makes the calls one after another. Validation errors return an Output with status "refused" and no calls made.
func Run(ctx context.Context, in Input, opt Options) Output {
	if _, err := Validate(in, opt); err != nil {
		return Output{Status: "refused", Reason: err.Error(), Calls: []CallResult{}}
	}
	m := in.Model
	cfg := config(m)
	client, err := model.NewContext(ctx, cfg)
	if err != nil {
		return Output{Status: "refused", Reason: "client construction failed", Calls: []CallResult{}}
	}
	// Never follow a redirect: the key header must not leave the allowed host.
	hc := *client.HTTP
	hc.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	client.HTTP = &hc
	router := model.Router{Entries: []model.Entry{{Config: cfg, Client: client}}}
	price := agentflow.Price{InputMicrosPerMTok: m.InputMicrosPerMTok, OutputMicrosPerMTok: m.OutputMicrosPerMTok}
	money := func(v int64) agentflow.Money {
		return agentflow.Money{Micros: v, Currency: m.Currency, PricingRevision: m.PricingRevision}
	}
	ledger, err := agentflow.NewLedger(money(in.Budget.LimitMicros))
	if err != nil {
		return Output{Status: "refused", Reason: "ledger: " + err.Error(), Calls: []CallResult{}}
	}
	store := agentflow.NewMemoryOperationStore()

	out := Output{Status: "ok", Calls: make([]CallResult, 0, len(in.Calls))}
	stopped := ""
	for _, c := range in.Calls {
		res := CallResult{ID: c.ID, ChargedMicros: agentflow.UsageUnknown, RequestSHA256: sum(m.Name + "\x00" + c.System + "\x00" + c.User)}
		if stopped != "" {
			res.Status, res.Reason = "skipped_after_stop", stopped
			out.Calls = append(out.Calls, res)
			continue
		}
		// Worst case: every prompt byte is at least one token and the reply uses the whole cap.
		worst, err := price.Cost(len(c.System)+len(c.User), m.MaxOutputTokens)
		if err != nil || worst <= 0 {
			worst = 1
		}
		call, text := c, ""
		var patch graph.State
		cc := agentflow.ChatCall{Client: router, Pin: pin, Price: price,
			Build: func(graph.State) (model.Request, error) {
				msgs := []model.Message{}
				if call.System != "" {
					msgs = append(msgs, model.Message{Role: "system", Content: call.System})
				}
				msgs = append(msgs, model.Message{Role: "user", Content: call.User})
				return model.Request{Messages: msgs, MaxOutputTokens: m.MaxOutputTokens}, nil
			},
			Apply: func(_ graph.State, resp model.Response) (graph.State, error) {
				text = resp.Text
				return graph.State{}, nil
			}}
		runner := &agentflow.NodeRunner{Store: store, WorkItemID: "lab-gen", NodeRevision: "lab-gen@1", MaxAttempts: 1,
			InvocationID: func(graph.State) (string, error) { return call.ID, nil }, Budget: ledger, Estimate: money(worst)}
		node, err := cc.Node(runner)
		if err != nil {
			res.Status, res.Reason = "failed", "node construction failed"
			stopped = "an earlier call failed to start"
			out.Calls = append(out.Calls, res)
			continue
		}
		patch, err = node(ctx, graph.State{})
		if err != nil {
			var pe *agentflow.ProviderStatusError
			switch {
			case errors.Is(err, agentflow.ErrBudgetExceeded):
				res.Status, res.Reason = "skipped_budget", "worst-case cost does not fit the remaining budget"
				stopped = "budget exhausted"
			case errors.Is(err, agentflow.ErrCancelled):
				res.Status, res.Reason = "skipped_after_stop", "cancelled"
				stopped = "cancelled"
			case errors.As(err, &pe):
				res.Status, res.Reason = "failed", fmt.Sprintf("provider status %d", pe.Status)
			case errors.Is(err, agentflow.ErrResponseLost), errors.Is(err, agentflow.ErrOutcomeUnknown):
				res.Status, res.Reason = "outcome_unknown", "response lost; the reservation is kept, not settled as zero"
				stopped = "an earlier outcome is unknown"
			default:
				res.Status, res.Reason = "failed", "call failed"
			}
			out.Calls = append(out.Calls, res)
			continue
		}
		info, _ := patch["llm_call"].(map[string]any)
		res.Status, res.Text, res.TextSHA256 = "ok", text, sum(text)
		res.UsageKnown, _ = info["usage_known"].(bool)
		res.InputTokens, _ = info["input_tokens"].(int)
		res.OutputTokens, _ = info["output_tokens"].(int)
		res.Attempts, _ = info["transport_attempts"].(int)
		if res.UsageKnown {
			res.ChargedMicros, _ = price.Cost(res.InputTokens, res.OutputTokens)
		}
		out.Calls = append(out.Calls, res)
	}
	snap := ledger.Snapshot()
	out.LedgerSpent, out.LedgerReserved, out.LedgerUnknown = snap.Spent, snap.Reserved, snap.Unknown
	for _, r := range out.Calls {
		if r.Status != "ok" {
			out.Status = "partial"
			break
		}
	}
	return out
}
