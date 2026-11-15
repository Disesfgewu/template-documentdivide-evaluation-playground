package agentflow

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/Disesfgewu/agentkit/graph"
)

type OpStatus string

const (
	OpAccountingPending OpStatus = "accounting_pending"
	OpStarted           OpStatus = "started"
	OpSucceeded         OpStatus = "succeeded"
	OpFailed            OpStatus = "failed"
	OpUnknown           OpStatus = "outcome_unknown"
)

var (
	ErrAccountingPending = errors.New("accounting_pending: saved result requires reconciliation before publication")
	ErrOutcomeUnknown    = errors.New("outcome_unknown: external call may have happened; reconcile before continuing")
	ErrNotSent           = errors.New("request was never sent") // the HTTP client refused the request before any byte left (invalid header, URL ...): nothing was billed, the reservation is released
	ErrResponseLost      = errors.New("response lost")          // an Effect returns an error wrapping this when the request was sent but no response/result arrived
)

type OpKey struct {
	WorkItemID          string
	NodeRevision        string
	LogicalInvocationID string
}

type Attempt struct {
	N             int
	Outcome       string /* "succeeded","failed","response_lost","cancelled" */
	Error         string
	ChargedMicros int64
	Unknown       bool
}

type Operation struct {
	Key      OpKey
	Status   OpStatus
	Attempts []Attempt
	Result   graph.State
	Error    string
}

type OperationStore interface {
	Get(ctx context.Context, key OpKey) (Operation, bool, error)
	Begin(ctx context.Context, key OpKey) error                                                       // create status started with no attempts; error if it already exists
	AddAttempt(ctx context.Context, key OpKey, a Attempt) error                                       // append
	Finish(ctx context.Context, key OpKey, status OpStatus, result graph.State, errText string) error // status accounting_pending|succeeded|failed|outcome_unknown
}

type MemoryOperationStore struct {
	mu  sync.Mutex
	ops map[OpKey]Operation
}

func NewMemoryOperationStore() *MemoryOperationStore {
	return &MemoryOperationStore{
		ops: make(map[OpKey]Operation),
	}
}

func (m *MemoryOperationStore) Get(ctx context.Context, key OpKey) (Operation, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	op, exists := m.ops[key]
	if !exists {
		return Operation{}, false, nil
	}
	if op.Attempts != nil {
		attemptsCopy := make([]Attempt, len(op.Attempts))
		copy(attemptsCopy, op.Attempts)
		op.Attempts = attemptsCopy
	}
	return op, true, nil
}

func (m *MemoryOperationStore) Begin(ctx context.Context, key OpKey) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if _, exists := m.ops[key]; exists {
		return fmt.Errorf("operation already exists: %+v", key)
	}
	m.ops[key] = Operation{
		Key:      key,
		Status:   OpStarted,
		Attempts: nil,
	}
	return nil
}

func (m *MemoryOperationStore) AddAttempt(ctx context.Context, key OpKey, a Attempt) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	op, exists := m.ops[key]
	if !exists {
		return fmt.Errorf("operation not found: %+v", key)
	}
	op.Attempts = append(op.Attempts, a)
	m.ops[key] = op
	return nil
}

func (m *MemoryOperationStore) Finish(ctx context.Context, key OpKey, status OpStatus, result graph.State, errText string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	op, exists := m.ops[key]
	if !exists {
		return fmt.Errorf("operation not found: %+v", key)
	}
	op.Status = status
	op.Result = result
	op.Error = errText
	m.ops[key] = op
	return nil
}

type Call struct {
	Attempt   int
	AttemptID string
}

type Effect func(ctx context.Context, s graph.State, c Call) (patch graph.State, charged int64, err error)

type NodeRunner struct {
	Store        OperationStore
	WorkItemID   string
	NodeRevision string
	InvocationID func(graph.State) (string, error) // logical invocation id, e.g. read from state; loops use a new id per iteration
	MaxAttempts  int                               // 1..3
	Retryable    func(error) bool                  // nil = nothing is retryable
	Budget       *Ledger                           // optional; if set, every attempt reserves Estimate before calling the effect
	Estimate     Money                             // required if Budget != nil
}

func (r *NodeRunner) Node(effect Effect) (graph.Node, error) {
	if effect == nil {
		return nil, errors.New("effect must not be nil")
	}
	if r.Store == nil {
		return nil, errors.New("store must not be nil")
	}
	if r.WorkItemID == "" {
		return nil, errors.New("work item id must not be empty")
	}
	if r.NodeRevision == "" {
		return nil, errors.New("node revision must not be empty")
	}
	if r.InvocationID == nil {
		return nil, errors.New("invocation id func must not be nil")
	}
	if r.MaxAttempts < 1 || r.MaxAttempts > 3 {
		return nil, fmt.Errorf("max attempts must be 1..3: got %d", r.MaxAttempts)
	}
	if r.Budget != nil {
		if r.Estimate.Micros <= 0 {
			return nil, fmt.Errorf("estimate micros must be > 0: got %d", r.Estimate.Micros)
		}
		if r.Estimate.Currency == "" {
			return nil, errors.New("estimate currency must not be empty")
		}
		if r.Estimate.PricingRevision == "" {
			return nil, errors.New("estimate pricing revision must not be empty")
		}
	}

	node := func(ctx context.Context, state graph.State) (graph.State, error) {
		// 1. key from WorkItemID, NodeRevision, InvocationID(state) (error -> return it).
		invID, err := r.InvocationID(state)
		if err != nil {
			return nil, err
		}
		key := OpKey{
			WorkItemID:          r.WorkItemID,
			NodeRevision:        r.NodeRevision,
			LogicalInvocationID: invID,
		}

		// 2. Get:
		// succeeded -> return op.Result (effect NOT called).
		// accounting_pending -> block until explicit reconciliation; never rerun the effect.
		// failed -> return fmt.Errorf("operation failed earlier: %s", op.Error).
		// outcome_unknown -> ErrOutcomeUnknown.
		// started -> Finish(..., OpUnknown, nil, "found started on re-run") then return ErrOutcomeUnknown.
		op, exists, err := r.Store.Get(ctx, key)
		if err != nil {
			return nil, err
		}
		if exists {
			switch op.Status {
			case OpSucceeded:
				return op.Result, nil
			case OpAccountingPending:
				return nil, ErrAccountingPending
			case OpFailed:
				return nil, fmt.Errorf("operation failed earlier: %s", op.Error)
			case OpUnknown:
				return nil, ErrOutcomeUnknown
			case OpStarted:
				// The previous executor died between Begin and Finish: the
				// external call may have happened, so it is never re-run.
				if err := r.Store.Finish(context.WithoutCancel(ctx), key, OpUnknown, nil, "found started on re-run"); err != nil {
					return nil, errors.Join(ErrOutcomeUnknown, err)
				}
				return nil, ErrOutcomeUnknown
			default:
				return nil, fmt.Errorf("unknown operation status: %s", op.Status)
			}
		}

		// 3. absent -> Begin, persisted before the first external call.
		if err := r.Store.Begin(ctx, key); err != nil {
			return nil, err
		}
		// Bookkeeping after Begin must survive cancellation/timeout of the
		// call itself, or an outcome_unknown would be lost with it.
		bk := context.WithoutCancel(ctx)

		// 4. for n := 1..MaxAttempts:
		for n := 1; n <= r.MaxAttempts; n++ {
			// - if ctx.Err() != nil: Finish failed with "cancelled before attempt n" ...
			//   BUT only if no attempt was in flight; return an error wrapping ErrCancelled.
			if ctx.Err() != nil {
				errMsg := fmt.Sprintf("cancelled before attempt %d", n)
				return nil, errors.Join(fmt.Errorf("%w: %s", ErrCancelled, errMsg),
					r.Store.Finish(bk, key, OpFailed, nil, errMsg))
			}

			attemptID := fmt.Sprintf("%s/%s/%s#%d", key.WorkItemID, key.NodeRevision, key.LogicalInvocationID, n)
			call := Call{
				Attempt:   n,
				AttemptID: attemptID,
			}

			// - if Budget: Reserve(ctx, AttemptID, Estimate); on error: Finish failed with the error text, return error (wrap the budget error so errors.Is works).
			if r.Budget != nil {
				if err := r.Budget.Reserve(ctx, attemptID, r.Estimate); err != nil {
					return nil, errors.Join(fmt.Errorf("budget reservation failed: %w", err),
						r.Store.Finish(bk, key, OpFailed, nil, err.Error()))
				}
			}

			// - call effect.
			patch, charged, effectErr := effect(ctx, state, call)

			// - success: save attempt/result as accounting_pending; settle/hold unknown; only then Finish succeeded and return patch.
			if effectErr == nil {
				// Save the external result before accounting, but do not mark
				// it publishable until settlement/unknown bookkeeping succeeds.
				// A crash in that gap blocks for reconciliation, never a resend.
				usageUnknown := charged == UsageUnknown
				if usageUnknown {
					charged = 0
				}
				attempt := Attempt{N: n, Outcome: "succeeded", ChargedMicros: charged, Unknown: usageUnknown}
				if err := r.Store.AddAttempt(bk, key, attempt); err != nil {
					return nil, err
				}
				if r.Budget != nil {
					if err := r.Store.Finish(bk, key, OpAccountingPending, patch, "accounting not confirmed"); err != nil {
						return nil, err
					}
					var err error
					if usageUnknown {
						// Billed but usage not reported: hold the reservation
						// for reconciliation, never settle as zero.
						err = r.Budget.MarkUnknown(attemptID)
					} else {
						err = r.Budget.Settle(attemptID, r.money(charged))
					}
					if err != nil {
						return nil, errors.Join(ErrAccountingPending, fmt.Errorf("result saved, settlement failed: %w", err))
					}
				}
				if err := r.Store.Finish(bk, key, OpSucceeded, patch, ""); err != nil {
					if r.Budget != nil {
						return nil, errors.Join(ErrAccountingPending, err)
					}
					return nil, err
				}
				return patch, nil
			}

			// - err wraps ErrResponseLost, or ctx is done after the call returned an error (cancel/timeout during a sent call):
			//   Budget.MarkUnknown(AttemptID) (do NOT settle/release); AddAttempt{Outcome "response_lost" or "cancelled", Unknown true};
			//   Finish outcome_unknown; return ErrOutcomeUnknown wrapped with the cause (errors.Is must work for ErrOutcomeUnknown).
			if errors.Is(effectErr, ErrResponseLost) || ctx.Err() != nil {
				var bookErr error
				if r.Budget != nil {
					bookErr = r.Budget.MarkUnknown(attemptID)
				}
				outcome := "response_lost"
				if !errors.Is(effectErr, ErrResponseLost) && ctx.Err() != nil {
					outcome = "cancelled"
				}
				attempt := Attempt{
					N:             n,
					Outcome:       outcome,
					Error:         effectErr.Error(),
					ChargedMicros: charged,
					Unknown:       true,
				}
				bookErr = errors.Join(bookErr, r.Store.AddAttempt(bk, key, attempt),
					r.Store.Finish(bk, key, OpUnknown, nil, effectErr.Error()))
				return nil, errors.Join(fmt.Errorf("%w: %w", ErrOutcomeUnknown, effectErr), bookErr)
			}

			// - other error: Budget.Settle(AttemptID, charged) (charged may be 0); AddAttempt{failed, err text, charged};
			//   if Retryable(err) && n < MaxAttempts -> continue; else Finish failed and return the error.
			var bookErr error
			usageUnknown := charged == UsageUnknown
			if usageUnknown {
				charged = 0
			}
			if r.Budget != nil {
				if usageUnknown {
					bookErr = r.Budget.MarkUnknown(attemptID)
				} else {
					bookErr = r.Budget.Settle(attemptID, r.money(charged))
				}
			}
			attempt := Attempt{N: n, Outcome: "failed", Error: effectErr.Error(), ChargedMicros: charged, Unknown: usageUnknown}
			bookErr = errors.Join(bookErr, r.Store.AddAttempt(bk, key, attempt))
			if bookErr != nil {
				// Unrecorded attempts must not be retried blindly.
				return nil, errors.Join(effectErr, bookErr, r.Store.Finish(bk, key, OpFailed, nil, effectErr.Error()))
			}

			if r.Retryable != nil && r.Retryable(effectErr) && n < r.MaxAttempts {
				continue
			}

			return nil, errors.Join(effectErr, r.Store.Finish(bk, key, OpFailed, nil, effectErr.Error()))
		}

		return nil, errors.New("unreachable: max attempts loop finished")
	}

	return node, nil
}

func (r *NodeRunner) money(micros int64) Money {
	return Money{Micros: micros, Currency: r.Estimate.Currency, PricingRevision: r.Estimate.PricingRevision}
}
