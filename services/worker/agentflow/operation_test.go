package agentflow

import (
	"context"
	"errors"
	"fmt"
	"math"
	"strings"
	"testing"

	"github.com/Disesfgewu/agentkit/graph"
)

func TestOperationSuccessFirstTry(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return graph.State{"output": "ok"}, 5_000, nil
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-1", nil },
		MaxAttempts:  3,
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("unexpected Node() error: %v", err)
	}

	res, err := node(ctx, graph.State{"input": 1})
	if err != nil {
		t.Fatalf("unexpected node error: %v", err)
	}
	if callCount != 1 {
		t.Fatalf("expected 1 call, got %d", callCount)
	}
	if res["output"] != "ok" {
		t.Fatalf("expected result 'ok', got %v", res["output"])
	}

	key := OpKey{WorkItemID: "work-1", NodeRevision: "rev-1", LogicalInvocationID: "inv-1"}
	op, ok, err := store.Get(ctx, key)
	if err != nil || !ok {
		t.Fatalf("store.Get failed: ok=%v, err=%v", ok, err)
	}
	if op.Status != OpSucceeded {
		t.Fatalf("expected status %v, got %v", OpSucceeded, op.Status)
	}
	if len(op.Attempts) != 1 {
		t.Fatalf("expected 1 attempt, got %d", len(op.Attempts))
	}
	if op.Attempts[0].Outcome != "succeeded" || op.Attempts[0].ChargedMicros != 5_000 {
		t.Fatalf("unexpected attempt: %+v", op.Attempts[0])
	}
	if op.Result["output"] != "ok" {
		t.Fatalf("unexpected op.Result: %+v", op.Result)
	}
}

// SPEC T61 re-run after "kill before checkpoint save"
func TestOperationRerunAfterCrashT61(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return graph.State{"checkpoint": "data"}, 10_000, nil
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-1", nil },
		MaxAttempts:  3,
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("unexpected Node() error: %v", err)
	}

	// First execution succeeds
	res1, err := node(ctx, graph.State{"x": 10})
	if err != nil {
		t.Fatalf("first call failed: %v", err)
	}
	if res1["checkpoint"] != "data" {
		t.Fatalf("unexpected result: %+v", res1)
	}
	if callCount != 1 {
		t.Fatalf("expected callCount 1, got %d", callCount)
	}

	// Re-run simulating graph resume re-running the node with same state
	res2, err := node(ctx, graph.State{"x": 10})
	if err != nil {
		t.Fatalf("re-run failed: %v", err)
	}
	if res2["checkpoint"] != "data" {
		t.Fatalf("re-run unexpected result: %+v", res2)
	}
	if callCount != 1 {
		t.Fatalf("effect called again on re-run: expected 1, got %d", callCount)
	}
}

func TestOperationRetryableErrorTwiceThenSuccess(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0
	var attemptIDs []string

	transientErr := errors.New("transient network failure")
	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		attemptIDs = append(attemptIDs, c.AttemptID)
		if callCount < 3 {
			return nil, 2_000, transientErr
		}
		return graph.State{"success": true}, 2_000, nil
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-retry", nil },
		MaxAttempts:  3,
		Retryable: func(err error) bool {
			return errors.Is(err, transientErr)
		},
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("unexpected Node() error: %v", err)
	}

	res, err := node(ctx, graph.State{})
	if err != nil {
		t.Fatalf("expected node to succeed, got %v", err)
	}
	if res["success"] != true {
		t.Fatalf("unexpected result: %+v", res)
	}
	if callCount != 3 {
		t.Fatalf("expected 3 calls, got %d", callCount)
	}

	if len(attemptIDs) != 3 {
		t.Fatalf("expected 3 attempt IDs, got %d", len(attemptIDs))
	}
	if attemptIDs[0] != "work-1/rev-1/inv-retry#1" {
		t.Fatalf("unexpected attemptID 1: %s", attemptIDs[0])
	}
	if attemptIDs[1] != "work-1/rev-1/inv-retry#2" {
		t.Fatalf("unexpected attemptID 2: %s", attemptIDs[1])
	}
	if attemptIDs[2] != "work-1/rev-1/inv-retry#3" {
		t.Fatalf("unexpected attemptID 3: %s", attemptIDs[2])
	}

	key := OpKey{WorkItemID: "work-1", NodeRevision: "rev-1", LogicalInvocationID: "inv-retry"}
	op, ok, _ := store.Get(ctx, key)
	if !ok || op.Status != OpSucceeded {
		t.Fatalf("unexpected operation: %+v", op)
	}
	if len(op.Attempts) != 3 {
		t.Fatalf("expected 3 attempts in store, got %d", len(op.Attempts))
	}
	if op.Attempts[0].Outcome != "failed" || op.Attempts[1].Outcome != "failed" || op.Attempts[2].Outcome != "succeeded" {
		t.Fatalf("unexpected attempts outcomes: [%s, %s, %s]",
			op.Attempts[0].Outcome, op.Attempts[1].Outcome, op.Attempts[2].Outcome)
	}
}

func TestOperationNonRetryableError(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	fatalErr := errors.New("unrecoverable schema error")
	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return nil, 1_000, fatalErr
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-fatal", nil },
		MaxAttempts:  3,
		Retryable:    func(err error) bool { return false },
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("unexpected Node() error: %v", err)
	}

	// First call fails
	_, err1 := node(ctx, graph.State{})
	if !errors.Is(err1, fatalErr) {
		t.Fatalf("expected fatalErr, got %v", err1)
	}
	if callCount != 1 {
		t.Fatalf("expected 1 call, got %d", callCount)
	}

	key := OpKey{WorkItemID: "work-1", NodeRevision: "rev-1", LogicalInvocationID: "inv-fatal"}
	op, ok, _ := store.Get(ctx, key)
	if !ok || op.Status != OpFailed {
		t.Fatalf("expected status OpFailed, got %+v", op)
	}

	// Second call returns "operation failed earlier" without calling effect
	_, err2 := node(ctx, graph.State{})
	if err2 == nil || !strings.Contains(err2.Error(), "operation failed earlier") {
		t.Fatalf("expected 'operation failed earlier' error, got %v", err2)
	}
	if callCount != 1 {
		t.Fatalf("effect called again on re-run: expected 1 call, got %d", callCount)
	}
}

func TestOperationRetriesExhausted(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	retryErr := errors.New("temporary error")
	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return nil, 1_000, retryErr
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-exhaust", nil },
		MaxAttempts:  2,
		Retryable:    func(err error) bool { return true },
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("unexpected Node() error: %v", err)
	}

	_, errExec := node(ctx, graph.State{})
	if !errors.Is(errExec, retryErr) {
		t.Fatalf("expected retryErr, got %v", errExec)
	}
	if callCount != 2 {
		t.Fatalf("expected 2 calls, got %d", callCount)
	}

	key := OpKey{WorkItemID: "work-1", NodeRevision: "rev-1", LogicalInvocationID: "inv-exhaust"}
	op, ok, _ := store.Get(ctx, key)
	if !ok || op.Status != OpFailed {
		t.Fatalf("expected OpFailed status, got %+v", op)
	}
	if len(op.Attempts) != 2 {
		t.Fatalf("expected 2 attempts, got %d", len(op.Attempts))
	}
}

// SPEC T63 response lost
func TestOperationResponseLostT63(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	ledger, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
	if err != nil {
		t.Fatalf("NewLedger error: %v", err)
	}
	estimate := Money{Micros: 100_000, Currency: "USD", PricingRevision: "2026-01"}

	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return nil, 0, fmt.Errorf("send ok: %w", ErrResponseLost)
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-lost", nil },
		MaxAttempts:  3,
		Retryable:    func(err error) bool { return true },
		Budget:       ledger,
		Estimate:     estimate,
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("unexpected Node() error: %v", err)
	}

	_, errExec := node(ctx, graph.State{})
	if !errors.Is(errExec, ErrOutcomeUnknown) {
		t.Fatalf("expected ErrOutcomeUnknown, got %v", errExec)
	}
	if callCount != 1 {
		t.Fatalf("expected 1 call, got %d", callCount)
	}

	key := OpKey{WorkItemID: "work-1", NodeRevision: "rev-1", LogicalInvocationID: "inv-lost"}
	op, ok, _ := store.Get(ctx, key)
	if !ok || op.Status != OpUnknown {
		t.Fatalf("expected OpUnknown, got %+v", op)
	}
	if len(op.Attempts) != 1 {
		t.Fatalf("expected 1 attempt, got %d", len(op.Attempts))
	}
	if op.Attempts[0].Outcome != "response_lost" || !op.Attempts[0].Unknown {
		t.Fatalf("expected response_lost with Unknown=true, got %+v", op.Attempts[0])
	}

	snap := ledger.Snapshot()
	if snap.Unknown != estimate.Micros || snap.Reserved != estimate.Micros {
		t.Fatalf("expected Unknown and Reserved == %d, got Unknown=%d, Reserved=%d",
			estimate.Micros, snap.Unknown, snap.Reserved)
	}

	// Calling the node again does NOT call the effect and returns ErrOutcomeUnknown
	_, errRerun := node(ctx, graph.State{})
	if !errors.Is(errRerun, ErrOutcomeUnknown) {
		t.Fatalf("expected ErrOutcomeUnknown on re-run, got %v", errRerun)
	}
	if callCount != 1 {
		t.Fatalf("effect called again on re-run: expected 1, got %d", callCount)
	}
}

func TestOperationStartedFoundOnRerun(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	key := OpKey{WorkItemID: "work-1", NodeRevision: "rev-1", LogicalInvocationID: "inv-crash"}
	// Begin the key manually in the store simulating a crash after Begin
	if err := store.Begin(ctx, key); err != nil {
		t.Fatalf("store.Begin error: %v", err)
	}

	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return graph.State{"done": true}, 0, nil
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-crash", nil },
		MaxAttempts:  3,
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("unexpected Node() error: %v", err)
	}

	_, errExec := node(ctx, graph.State{})
	if !errors.Is(errExec, ErrOutcomeUnknown) {
		t.Fatalf("expected ErrOutcomeUnknown, got %v", errExec)
	}
	if callCount != 0 {
		t.Fatalf("effect should not be called, got callCount=%d", callCount)
	}

	op, ok, _ := store.Get(ctx, key)
	if !ok || op.Status != OpUnknown {
		t.Fatalf("expected status OpUnknown, got %+v", op)
	}
	if op.Error != "found started on re-run" {
		t.Fatalf("expected 'found started on re-run', got %q", op.Error)
	}
}

// SPEC T29 cancel
func TestOperationCancelBeforeAndDuringCallT29(t *testing.T) {
	// Part 1: ctx cancelled before the node
	{
		ctx, cancel := context.WithCancel(context.Background())
		cancel() // cancelled before node

		store := NewMemoryOperationStore()
		callCount := 0
		ledger, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
		if err != nil {
			t.Fatalf("NewLedger error: %v", err)
		}
		estimate := Money{Micros: 100_000, Currency: "USD", PricingRevision: "2026-01"}

		effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
			callCount++
			return graph.State{"ok": true}, 0, nil
		}

		runner := &NodeRunner{
			Store:        store,
			WorkItemID:   "work-1",
			NodeRevision: "rev-1",
			InvocationID: func(s graph.State) (string, error) { return "inv-cancel-before", nil },
			MaxAttempts:  3,
			Budget:       ledger,
			Estimate:     estimate,
		}

		node, err := runner.Node(effect)
		if err != nil {
			t.Fatalf("Node() error: %v", err)
		}

		_, errExec := node(ctx, graph.State{})
		if !errors.Is(errExec, ErrCancelled) {
			t.Fatalf("expected ErrCancelled, got %v", errExec)
		}
		if callCount != 0 {
			t.Fatalf("effect called when cancelled before node: callCount=%d", callCount)
		}
		snap := ledger.Snapshot()
		if snap.Reserved != 0 {
			t.Fatalf("budget should be unchanged: Reserved=%d", snap.Reserved)
		}
	}

	// Part 2: Cancel during the call
	{
		ctx, cancel := context.WithCancel(context.Background())
		store := NewMemoryOperationStore()
		callCount := 0
		ledger, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
		if err != nil {
			t.Fatalf("NewLedger error: %v", err)
		}
		estimate := Money{Micros: 100_000, Currency: "USD", PricingRevision: "2026-01"}

		effect := func(innerCtx context.Context, s graph.State, c Call) (graph.State, int64, error) {
			callCount++
			cancel() // cancel during call
			return nil, 0, innerCtx.Err()
		}

		runner := &NodeRunner{
			Store:        store,
			WorkItemID:   "work-1",
			NodeRevision: "rev-1",
			InvocationID: func(s graph.State) (string, error) { return "inv-cancel-during", nil },
			MaxAttempts:  3,
			Retryable:    func(err error) bool { return true }, // even if Retryable returns true
			Budget:       ledger,
			Estimate:     estimate,
		}

		node, err := runner.Node(effect)
		if err != nil {
			t.Fatalf("Node() error: %v", err)
		}

		_, errExec := node(ctx, graph.State{})
		if !errors.Is(errExec, ErrOutcomeUnknown) {
			t.Fatalf("expected ErrOutcomeUnknown, got %v", errExec)
		}
		if callCount != 1 {
			t.Fatalf("expected exactly 1 call (no retry after cancel), got %d", callCount)
		}

		snap := ledger.Snapshot()
		if snap.Unknown != estimate.Micros || snap.Reserved != estimate.Micros {
			t.Fatalf("reservation should be kept as unknown: snap=%+v", snap)
		}

		key := OpKey{WorkItemID: "work-1", NodeRevision: "rev-1", LogicalInvocationID: "inv-cancel-during"}
		op, ok, _ := store.Get(context.Background(), key)
		if !ok || op.Status != OpUnknown {
			t.Fatalf("expected OpUnknown status, got %+v", op)
		}
		if len(op.Attempts) != 1 {
			t.Fatalf("expected 1 attempt, got %d", len(op.Attempts))
		}
		if op.Attempts[0].Outcome != "cancelled" || !op.Attempts[0].Unknown {
			t.Fatalf("expected outcome 'cancelled' and Unknown=true, got %+v", op.Attempts[0])
		}
	}
}

func TestOperationBudgetExceededOnRetry(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	estimate := Money{Micros: 100_000, Currency: "USD", PricingRevision: "2026-01"}
	// Limit allows 2 attempts of Estimate (2 * 100_000 = 200_000)
	ledger, err := NewLedger(Money{Micros: 200_000, Currency: "USD", PricingRevision: "2026-01"})
	if err != nil {
		t.Fatalf("NewLedger error: %v", err)
	}

	retryErr := errors.New("temporary failure")
	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return nil, estimate.Micros, retryErr
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-1",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) { return "inv-budget-retry", nil },
		MaxAttempts:  3,
		Retryable:    func(err error) bool { return true },
		Budget:       ledger,
		Estimate:     estimate,
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("Node() error: %v", err)
	}

	_, errExec := node(ctx, graph.State{})
	if !errors.Is(errExec, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded on 3rd attempt, got %v", errExec)
	}
	if callCount != 2 {
		t.Fatalf("expected effect called exactly 2 times, got %d", callCount)
	}

	snap := ledger.Snapshot()
	if snap.Spent != 2*estimate.Micros {
		t.Fatalf("expected Spent == %d, got %d", 2*estimate.Micros, snap.Spent)
	}
	if snap.Reserved != 0 {
		t.Fatalf("expected Reserved == 0, got %d", snap.Reserved)
	}
}

func TestOperationLoopsDifferentInvocationIDs(t *testing.T) {
	ctx := context.Background()
	store := NewMemoryOperationStore()
	callCount := 0

	effect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		callCount++
		return graph.State{"iter_done": s["iter"]}, 1_000, nil
	}

	runner := &NodeRunner{
		Store:        store,
		WorkItemID:   "work-loop",
		NodeRevision: "rev-1",
		InvocationID: func(s graph.State) (string, error) {
			return fmt.Sprintf("iter-%v", s["iter"]), nil
		},
		MaxAttempts: 3,
	}

	node, err := runner.Node(effect)
	if err != nil {
		t.Fatalf("Node() error: %v", err)
	}

	res1, err := node(ctx, graph.State{"iter": 1})
	if err != nil || res1["iter_done"] != 1 {
		t.Fatalf("iter 1 failed: %v, res: %+v", err, res1)
	}

	res2, err := node(ctx, graph.State{"iter": 2})
	if err != nil || res2["iter_done"] != 2 {
		t.Fatalf("iter 2 failed: %v, res: %+v", err, res2)
	}

	if callCount != 2 {
		t.Fatalf("expected 2 effect calls, got %d", callCount)
	}

	key1 := OpKey{WorkItemID: "work-loop", NodeRevision: "rev-1", LogicalInvocationID: "iter-1"}
	key2 := OpKey{WorkItemID: "work-loop", NodeRevision: "rev-1", LogicalInvocationID: "iter-2"}

	op1, ok1, _ := store.Get(ctx, key1)
	op2, ok2, _ := store.Get(ctx, key2)

	if !ok1 || op1.Status != OpSucceeded {
		t.Fatalf("expected op1 succeeded, got ok=%v, op=%+v", ok1, op1)
	}
	if !ok2 || op2.Status != OpSucceeded {
		t.Fatalf("expected op2 succeeded, got ok=%v, op=%+v", ok2, op2)
	}
}

func TestOperationConfigValidation(t *testing.T) {
	dummyEffect := func(ctx context.Context, s graph.State, c Call) (graph.State, int64, error) {
		return nil, 0, nil
	}
	store := NewMemoryOperationStore()
	invFunc := func(s graph.State) (string, error) { return "inv", nil }

	// nil Effect
	rEffectNil := &NodeRunner{Store: store, WorkItemID: "w", NodeRevision: "r", InvocationID: invFunc, MaxAttempts: 1}
	if _, err := rEffectNil.Node(nil); err == nil {
		t.Fatalf("expected error for nil effect")
	}

	// nil Store
	rNilStore := &NodeRunner{WorkItemID: "w", NodeRevision: "r", InvocationID: invFunc, MaxAttempts: 1}
	if _, err := rNilStore.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for nil Store")
	}

	// empty WorkItemID
	rEmptyWork := &NodeRunner{Store: store, WorkItemID: "", NodeRevision: "r", InvocationID: invFunc, MaxAttempts: 1}
	if _, err := rEmptyWork.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for empty WorkItemID")
	}

	// empty NodeRevision
	rEmptyRev := &NodeRunner{Store: store, WorkItemID: "w", NodeRevision: "", InvocationID: invFunc, MaxAttempts: 1}
	if _, err := rEmptyRev.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for empty NodeRevision")
	}

	// nil InvocationID
	rNilInv := &NodeRunner{Store: store, WorkItemID: "w", NodeRevision: "r", InvocationID: nil, MaxAttempts: 1}
	if _, err := rNilInv.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for nil InvocationID")
	}

	// MaxAttempts 0 rejected
	rZeroAttempts := &NodeRunner{Store: store, WorkItemID: "w", NodeRevision: "r", InvocationID: invFunc, MaxAttempts: 0}
	if _, err := rZeroAttempts.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for MaxAttempts 0")
	}

	// MaxAttempts 4 rejected
	rFourAttempts := &NodeRunner{Store: store, WorkItemID: "w", NodeRevision: "r", InvocationID: invFunc, MaxAttempts: 4}
	if _, err := rFourAttempts.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for MaxAttempts 4")
	}

	// Budget without Estimate rejected (Estimate.Micros == 0)
	ledger, _ := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
	rBudgetNoEst := &NodeRunner{
		Store:        store,
		WorkItemID:   "w",
		NodeRevision: "r",
		InvocationID: invFunc,
		MaxAttempts:  2,
		Budget:       ledger,
		Estimate:     Money{Micros: 0, Currency: "USD", PricingRevision: "2026-01"},
	}
	if _, err := rBudgetNoEst.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for Budget with zero Estimate")
	}

	// Budget with empty Estimate Currency rejected
	rBudgetNoCurr := &NodeRunner{
		Store:        store,
		WorkItemID:   "w",
		NodeRevision: "r",
		InvocationID: invFunc,
		MaxAttempts:  2,
		Budget:       ledger,
		Estimate:     Money{Micros: 100_000, Currency: "", PricingRevision: "2026-01"},
	}
	if _, err := rBudgetNoCurr.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for Budget with empty Estimate Currency")
	}

	// Budget with empty Estimate PricingRevision rejected
	rBudgetNoRev := &NodeRunner{
		Store:        store,
		WorkItemID:   "w",
		NodeRevision: "r",
		InvocationID: invFunc,
		MaxAttempts:  2,
		Budget:       ledger,
		Estimate:     Money{Micros: 100_000, Currency: "USD", PricingRevision: ""},
	}
	if _, err := rBudgetNoRev.Node(dummyEffect); err == nil {
		t.Fatalf("expected error for Budget with empty Estimate PricingRevision")
	}

	// Valid config returns node without error
	rValid := &NodeRunner{
		Store:        store,
		WorkItemID:   "w",
		NodeRevision: "r",
		InvocationID: invFunc,
		MaxAttempts:  2,
		Budget:       ledger,
		Estimate:     Money{Micros: 100_000, Currency: "USD", PricingRevision: "2026-01"},
	}
	if node, err := rValid.Node(dummyEffect); err != nil || node == nil {
		t.Fatalf("expected valid node, got err=%v, node=%v", err, node)
	}
}

// R20/T28/T29: a saved external result is not publishable before accounting.
func TestSettlementFailureCannotPublishOnReplay(t *testing.T) {
	ctx := context.Background()
	money := func(n int64) Money { return Money{Micros: n, Currency: "USD", PricingRevision: "test"} }
	ledger, err := NewLedger(money(math.MaxInt64))
	if err != nil {
		t.Fatal(err)
	}
	if err := ledger.Reserve(ctx, "prior", money(math.MaxInt64-1)); err != nil {
		t.Fatal(err)
	}
	if err := ledger.Settle("prior", money(math.MaxInt64-1)); err != nil {
		t.Fatal(err)
	}
	store := NewMemoryOperationStore()
	runner := &NodeRunner{Store: store, WorkItemID: "w", NodeRevision: "r",
		InvocationID: func(graph.State) (string, error) { return "i", nil },
		MaxAttempts:  1, Budget: ledger, Estimate: money(1)}
	calls := 0
	node, err := runner.Node(func(context.Context, graph.State, Call) (graph.State, int64, error) {
		calls++
		return graph.State{"must_not_publish": true}, 2, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	patch, err := node(ctx, graph.State{})
	if !errors.Is(err, ErrLedgerOverflow) || patch != nil {
		t.Fatalf("first: patch=%v err=%v", patch, err)
	}
	before := ledger.Snapshot()
	patch, err = node(ctx, graph.State{})
	if !errors.Is(err, ErrAccountingPending) || patch != nil {
		t.Fatalf("replay published unsettled result: patch=%v err=%v", patch, err)
	}
	if calls != 1 || ledger.Snapshot() != before {
		t.Fatalf("replay changed effect/accounting: calls=%d snapshot=%+v", calls, ledger.Snapshot())
	}
	if before.Spent != math.MaxInt64-1 || before.Reserved != 1 {
		t.Fatalf("reservation not held: %+v", before)
	}
	op, ok, err := store.Get(ctx, OpKey{WorkItemID: "w", NodeRevision: "r", LogicalInvocationID: "i"})
	if err != nil || !ok || op.Status != OpAccountingPending || op.Result["must_not_publish"] != true || len(op.Attempts) != 1 || op.Attempts[0].ChargedMicros != 2 {
		t.Fatalf("unsettled result/usage not retained for reconciliation: op=%+v err=%v", op, err)
	}
}

type failPublicationStore struct {
	*MemoryOperationStore
	fail bool
}

func (s *failPublicationStore) Finish(ctx context.Context, key OpKey, status OpStatus, result graph.State, text string) error {
	if s.fail && status == OpSucceeded {
		return errors.New("injected final status write failure")
	}
	return s.MemoryOperationStore.Finish(ctx, key, status, result, text)
}

func TestAccountingPublicationBoundaries(t *testing.T) {
	for _, mode := range []string{"settled_replay", "mark_unknown_failure", "publication_write_failure"} {
		t.Run(mode, func(t *testing.T) {
			ctx := context.Background()
			money := func(n int64) Money { return Money{Micros: n, Currency: "USD", PricingRevision: "test"} }
			ledger, err := NewLedger(money(10))
			if err != nil {
				t.Fatal(err)
			}
			store := &failPublicationStore{MemoryOperationStore: NewMemoryOperationStore(), fail: mode == "publication_write_failure"}
			key := OpKey{WorkItemID: "w", NodeRevision: "r", LogicalInvocationID: "i"}
			runner := &NodeRunner{Store: store, WorkItemID: key.WorkItemID, NodeRevision: key.NodeRevision,
				InvocationID: func(graph.State) (string, error) { return key.LogicalInvocationID, nil },
				MaxAttempts:  1, Budget: ledger, Estimate: money(3)}
			calls := 0
			node, err := runner.Node(func(_ context.Context, _ graph.State, call Call) (graph.State, int64, error) {
				calls++
				if mode == "mark_unknown_failure" {
					// Fault: the reservation disappears before MarkUnknown.
					if err := ledger.Release(call.AttemptID); err != nil {
						t.Fatal(err)
					}
					return graph.State{"saved": true}, UsageUnknown, nil
				}
				return graph.State{"saved": true}, 2, nil
			})
			if err != nil {
				t.Fatal(err)
			}
			first, firstErr := node(ctx, graph.State{})
			before := ledger.Snapshot()
			second, secondErr := node(ctx, graph.State{})
			if calls != 1 || ledger.Snapshot() != before {
				t.Fatalf("effect/accounting repeated: calls=%d", calls)
			}
			op, ok, err := store.Get(ctx, key)
			if err != nil || !ok || op.Result["saved"] != true || len(op.Attempts) != 1 {
				t.Fatalf("result/attempt not retained: op=%+v err=%v", op, err)
			}
			if mode == "settled_replay" {
				if firstErr != nil || secondErr != nil || first["saved"] != true || second["saved"] != true || op.Status != OpSucceeded || before.Spent != 2 || before.Reserved != 0 {
					t.Fatalf("successful T61 replay changed: first=%v second=%v op=%+v ledger=%+v", firstErr, secondErr, op, before)
				}
				return
			}
			if !errors.Is(firstErr, ErrAccountingPending) || !errors.Is(secondErr, ErrAccountingPending) || first != nil || second != nil || op.Status != OpAccountingPending {
				t.Fatalf("unconfirmed accounting published: first=%v/%v second=%v/%v op=%+v", first, firstErr, second, secondErr, op)
			}
			if mode == "mark_unknown_failure" {
				if !errors.Is(firstErr, ErrUnknownReservation) || !op.Attempts[0].Unknown {
					t.Fatalf("missing unknown cause: err=%v op=%+v", firstErr, op)
				}
			} else {
				if before.Spent != 2 || before.Reserved != 0 {
					t.Fatalf("settlement lost: %+v", before)
				}
				// Simulate explicit reconciliation after confirming the stored
				// attempt and the already completed settlement. No automatic retry.
				store.fail = false
				if err := store.Finish(ctx, key, OpSucceeded, op.Result, ""); err != nil {
					t.Fatal(err)
				}
				patch, err := node(ctx, graph.State{})
				if err != nil || patch["saved"] != true || calls != 1 || ledger.Snapshot() != before {
					t.Fatalf("reconciled replay: patch=%v err=%v calls=%d", patch, err, calls)
				}
			}
		})
	}
}
