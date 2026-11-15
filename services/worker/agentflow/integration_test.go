package agentflow

import (
	"context"
	"errors"
	"testing"

	"github.com/Disesfgewu/agentkit/graph"
)

// ctxCheckingStore behaves like a database client: it refuses work on a
// cancelled context. Bookkeeping after an in-flight cancel must still land.
type ctxCheckingStore struct{ *MemoryOperationStore }

func (s ctxCheckingStore) AddAttempt(ctx context.Context, k OpKey, a Attempt) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.MemoryOperationStore.AddAttempt(ctx, k, a)
}

func (s ctxCheckingStore) Finish(ctx context.Context, k OpKey, st OpStatus, r graph.State, e string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.MemoryOperationStore.Finish(ctx, k, st, r, e)
}

func usd(m int64) Money { return Money{Micros: m, Currency: "USD", PricingRevision: "price-1"} }

// T29: cancelled mid-call against a store that rejects cancelled contexts:
// the outcome_unknown and its attempt are still recorded, the reservation
// stays held, and no second attempt is made.
func TestCancelMidCallIsRecordedEvenIfStoreRejectsCancelledContext(t *testing.T) {
	store := ctxCheckingStore{NewMemoryOperationStore()}
	budget, _ := NewLedger(usd(1_000_000))
	r := &NodeRunner{Store: store, WorkItemID: "w", NodeRevision: "n1", MaxAttempts: 3,
		InvocationID: func(graph.State) (string, error) { return "inv-1", nil },
		Retryable:    func(error) bool { return true }, Budget: budget, Estimate: usd(100_000)}
	ctx, cancel := context.WithCancel(context.Background())
	calls := 0
	node, err := r.Node(func(ctx context.Context, _ graph.State, _ Call) (graph.State, int64, error) {
		calls++
		cancel()
		return nil, 0, ctx.Err()
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := node(ctx, graph.State{}); !errors.Is(err, ErrOutcomeUnknown) || !errors.Is(err, context.Canceled) {
		t.Fatalf("err %v", err)
	}
	op, _, _ := store.Get(context.Background(), OpKey{"w", "n1", "inv-1"})
	if calls != 1 || op.Status != OpUnknown || len(op.Attempts) != 1 || op.Attempts[0].Outcome != "cancelled" {
		t.Fatalf("calls=%d op=%+v", calls, op)
	}
	if s := budget.Snapshot(); s.Reserved != 100_000 || s.Unknown != 100_000 || s.Spent != 0 {
		t.Fatalf("budget %+v", s)
	}
}

// failingSaveStore fails the first Save after a given node: the "kill after
// the node's DB commit, before Store.Save" of T61.
type failingSaveStore struct {
	graph.Store
	failAfter string
	failed    bool
}

func (s *failingSaveStore) Save(id string, c graph.Checkpoint) error {
	if c.LastNode == s.failAfter && !s.failed {
		s.failed = true
		return errors.New("worker killed before Store.Save")
	}
	return s.Store.Save(id, c)
}

// T61 on the real agentKit graph with the fenced checkpoint store: the paid
// node's result is reused after the lost checkpoint, its effect runs once,
// usage is charged once, and the checkpoint sequence still advances.
func TestNodeRunnerInGraphReusesOperationAfterLostCheckpoint(t *testing.T) {
	ctx := audited(t)
	db, clock := newDB(t)
	ops := NewMemoryOperationStore()
	budget, _ := NewLedger(usd(1_000_000))
	runner := &NodeRunner{Store: ops, WorkItemID: "t1", NodeRevision: "generate@1", MaxAttempts: 3,
		InvocationID: func(s graph.State) (string, error) { return s["job_id"].(string) + "/generate", nil },
		Budget:       budget, Estimate: usd(200_000)}
	effects := 0
	generate, err := runner.Node(func(_ context.Context, s graph.State, c Call) (graph.State, int64, error) {
		effects++
		return graph.State{"stage": "generated", "question_ref": Ref{Kind: "question", ID: c.AttemptID}}, 150_000, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	build := func() *graph.Graph {
		g := graph.NewContext(ctx)
		if err := g.AddNodeContext(ctx, "generate", generate); err != nil {
			t.Fatal(err)
		}
		if err := g.AddNodeContext(ctx, "finish", func(_ context.Context, s graph.State) (graph.State, error) {
			return graph.State{"stage": "done"}, nil
		}); err != nil {
			t.Fatal(err)
		}
		g.EdgeContext(ctx, graph.Start, "generate")
		g.EdgeContext(ctx, "generate", "finish")
		g.EdgeContext(ctx, "finish", graph.End)
		return g
	}

	l1, _ := db.Acquire("t1", "worker-a")
	killed := &failingSaveStore{Store: db.Store(l1, "wf-rev-1"), failAfter: "generate"}
	if _, err := build().RunCheckpointed(ctx, "t1", envelope(), killed); err == nil {
		t.Fatal("first run should lose its checkpoint")
	}
	if len(db.Records("t1")) != 0 {
		t.Fatal("no checkpoint should exist yet")
	}

	clock.advance(LeaseTTL + 1)
	l2, _ := db.Acquire("t1", "worker-b")
	res, err := build().RunCheckpointed(ctx, "t1", envelope(), db.Store(l2, "wf-rev-1"))
	if err != nil || !res.Finished || res.State["stage"] != "done" {
		t.Fatalf("resume: %v %+v", err, res)
	}
	if effects != 1 {
		t.Fatalf("paid effect ran %d times", effects)
	}
	if got := res.State["question_ref"].(Ref).ID; got != "t1/generate@1/j1/generate#1" {
		t.Fatalf("reused result %q", got)
	}
	if s := budget.Snapshot(); s.Spent != 150_000 || s.Reserved != 0 {
		t.Fatalf("budget %+v", s)
	}
	recs := db.Records("t1")
	if len(recs) != 3 || recs[0].LastNode != "generate" || recs[0].Token != l2.Token {
		t.Fatalf("records %+v", recs)
	}
}
