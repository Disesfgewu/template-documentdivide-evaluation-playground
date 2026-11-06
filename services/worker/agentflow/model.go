package agentflow

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"time"

	"github.com/Disesfgewu/agentkit/graph"
	"github.com/Disesfgewu/agentkit/model"
)

// UsageUnknown is the charged value an Effect returns when the provider
// reported no usage: the reservation is kept as outcome_unknown, never
// settled as zero (SPEC T29 "未知 usage 非零值冒充").
const UsageUnknown int64 = -1

// MaxTransportAttempts is the SPEC 12.3 bound per logical call.
const MaxTransportAttempts = 3

var (
	ErrInvalidUsage  = errors.New("token counts and prices must not be negative")
	ErrCostOverflow  = errors.New("priced usage cannot be represented in int64 micro-units")
	ErrModelSwapped  = errors.New("model pin not honoured: response came from another model")
	ErrModelConfig   = errors.New("model config cannot express the LAB profile")
	statusErrPattern = regexp.MustCompile(`HTTP (\d{3}) after \d+ retries`)
)

// ProviderStatusError is a provider answer with an HTTP error status after
// agentKit's own retries: a response arrived, so the outcome is known.
type ProviderStatusError struct {
	Status int
	Err    error
}

func (e *ProviderStatusError) Error() string {
	return fmt.Sprintf("provider HTTP %d: %v", e.Status, e.Err)
}
func (e *ProviderStatusError) Unwrap() error { return e.Err }

// ValidateModelConfig refuses agentKit settings that do not express the LAB
// profile explicitly (SPEC 12.3: align agentKit retry/default timeout, or
// refuse to start). The retry owner for transport is agentKit's HTTPClient:
//
//   - MaxRetries must be 1..2 (attempts = MaxRetries+1 <= 3). At ce49a71,
//     MaxRetries 0 means 5 retries (6 attempts), so 0 is rejected.
//   - TimeoutSeconds must be set and fit time.Duration in seconds
//     (0 silently means 3 minutes).
//   - RequestsPerMinute must be set (shared provider quota).
func ValidateModelConfig(c model.Config) error {
	var problems []string
	if c.Name == "" {
		problems = append(problems, "name is required (it is the pin)")
	}
	if c.Kind == model.KindEmbedding {
		problems = append(problems, "embedding models are not chat models")
	}
	if c.MaxRetries < 1 || c.MaxRetries > MaxTransportAttempts-1 {
		problems = append(problems, fmt.Sprintf("max_retries %d: must be 1..%d (0 means 5 retries in agentKit)", c.MaxRetries, MaxTransportAttempts-1))
	}
	if c.TimeoutSeconds <= 0 || int64(c.TimeoutSeconds) > math.MaxInt64/int64(time.Second) {
		problems = append(problems, "timeout_seconds must be positive and fit time.Duration")
	}
	if c.RequestsPerMinute <= 0 {
		problems = append(problems, "requests_per_minute must be set")
	}
	if len(problems) > 0 {
		return fmt.Errorf("%w: %s: %v", ErrModelConfig, c.Name, problems)
	}
	return nil
}

// ValidateRouter checks every chat entry and that names are unique, since a
// pinned name shared by two entries would still fail over between them.
func ValidateRouter(r model.Router) error {
	seen := map[string]bool{}
	for _, e := range r.Entries {
		if e.Config.Kind == model.KindEmbedding {
			continue
		}
		if seen[e.Config.Name] {
			return fmt.Errorf("%w: duplicate entry name %q", ErrModelConfig, e.Config.Name)
		}
		seen[e.Config.Name] = true
		if err := ValidateModelConfig(e.Config); err != nil {
			return err
		}
	}
	return nil
}

// Price is the pricing of one model in micro-currency per million tokens.
type Price struct {
	InputMicrosPerMTok  int64
	OutputMicrosPerMTok int64
}

// Cost rounds up once after summing both token charges. Wide integer
// intermediates avoid overflow even when the final micro-unit cost fits.
func (p Price) Cost(in, out int) (int64, error) {
	if in < 0 || out < 0 || p.InputMicrosPerMTok < 0 || p.OutputMicrosPerMTok < 0 {
		return 0, ErrInvalidUsage
	}
	var input, output, total big.Int
	input.Mul(big.NewInt(int64(in)), big.NewInt(p.InputMicrosPerMTok))
	output.Mul(big.NewInt(int64(out)), big.NewInt(p.OutputMicrosPerMTok))
	total.Add(&input, &output)
	total.Add(&total, big.NewInt(999_999))
	total.Quo(&total, big.NewInt(1_000_000))
	if !total.IsInt64() {
		return 0, ErrCostOverflow
	}
	return total.Int64(), nil
}

// ChatCall turns one pinned agentKit chat call into a NodeRunner Effect.
type ChatCall struct {
	Client model.Client // a model.Router, model.RemoteClient or HTTPClient
	Pin    string       // Router entry name; required, disables failover
	Price  Price
	Build  func(graph.State) (model.Request, error)
	Apply  func(graph.State, model.Response) (graph.State, error)
}

// Node wraps the call for a runner. agentKit owns transport retries, so the
// runner must not retry model calls (no multiplied retry layers, R20).
func (c ChatCall) Node(r *NodeRunner) (graph.Node, error) {
	if c.Client == nil || c.Pin == "" || c.Build == nil || c.Apply == nil {
		return nil, errors.New("chat call needs client, pin, build and apply")
	}
	if c.Price.InputMicrosPerMTok < 0 || c.Price.OutputMicrosPerMTok < 0 {
		return nil, errors.New("price must not be negative")
	}
	if r.MaxAttempts != 1 || r.Retryable != nil {
		return nil, errors.New("model calls: transport retries belong to agentKit (max_retries); the runner must use MaxAttempts 1 and no Retryable")
	}
	return r.Node(c.effect)
}

func (c ChatCall) effect(ctx context.Context, s graph.State, _ Call) (graph.State, int64, error) {
	req, err := c.Build(s)
	if err != nil {
		return nil, 0, err
	}
	if req.Model != "" && req.Model != c.Pin {
		return nil, 0, fmt.Errorf("request pins %q, call is pinned to %q", req.Model, c.Pin)
	}
	req.Model = c.Pin
	if req.MaxOutputTokens <= 0 {
		// agentKit would silently use 1024 (SPEC: max_output_tokens is required).
		return nil, 0, errors.New("max_output_tokens is required")
	}
	resp, err := c.Client.Chat(ctx, req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, 0, err // in flight when cancelled: the runner records outcome_unknown
		}
		if m := statusErrPattern.FindStringSubmatch(err.Error()); m != nil {
			code, _ := strconv.Atoi(m[1])
			return nil, 0, &ProviderStatusError{Status: code, Err: err}
		}
		// Transport failure after the request may have been sent (timeout,
		// reset): the provider may have billed it, so the outcome is unknown.
		return nil, 0, fmt.Errorf("%w: %v", ErrResponseLost, err)
	}
	charged := UsageUnknown
	if resp.UsageKnown {
		charged, err = c.Price.Cost(resp.InputTokens, resp.OutputTokens)
		if err != nil {
			// The response arrived, but cannot be settled safely. The runner
			// records the failure and holds the reservation for reconciliation.
			return nil, UsageUnknown, fmt.Errorf("price reported usage: %w", err)
		}
	}
	if resp.Provider != c.Pin {
		return nil, charged, fmt.Errorf("%w: pinned %q, got %q (%s)", ErrModelSwapped, c.Pin, resp.Provider, resp.Model)
	}
	patch, err := c.Apply(s, resp)
	if err != nil {
		return nil, charged, err
	}
	if patch == nil {
		patch = graph.State{}
	}
	patch["llm_call"] = map[string]any{
		"provider": resp.Provider, "model": resp.Model, "transport_attempts": resp.Attempts,
		"usage_known": resp.UsageKnown, "input_tokens": resp.InputTokens, "output_tokens": resp.OutputTokens,
	}
	return patch, charged, nil
}
