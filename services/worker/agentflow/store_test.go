package agentflow

import (
	"bytes"
	"context"
	"errors"
	"testing"
	"time"

	"github.com/Disesfgewu/agentkit/agentlog"
	"github.com/Disesfgewu/agentkit/graph"
)

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time          { return c.t }
func (c *fakeClock) advance(d time.Duration) { c.t = c.t.Add(d) }

func audited(t *testing.T) context.Context {
	t.Helper()
	r, err := agentlog.New("agentflow-test", t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(); err != nil {
			t.Fatal(err)
		}
		if n, err := agentlog.Verify(r.Path); err != nil || n == 0 {
			t.Fatalf("audit log: records=%d err=%v", n, err)
		}
	})
	return agentlog.WithRecorder(context.Background(), r)
}

// pipeline: a -> b -> c, each node counts its calls and advances "stage".
type pipeline struct {
	calls map[string]int
	failB int
}

func (p *pipeline) graph(ctx context.Context, t *testing.T) *graph.Graph {
	t.Helper()
	g := graph.NewContext(ctx)
	for _, name := range []string{"a", "b", "c"} {
		name := name
		if err := g.AddNodeContext(ctx, name, func(_ context.Context, s graph.State) (graph.State, error) {
			p.calls[name]++
			if name == "b" && p.failB > 0 {
				p.failB--
				return nil, errors.New("worker killed in b")
			}
			return graph.State{"stage": name, "attempt": s["attempt"].(int) + 1}, nil
		}); err != nil {
			t.Fatal(err)
		}
	}
	g.EdgeContext(ctx, graph.Start, "a")
	g.EdgeContext(ctx, "a", "b")
	g.EdgeContext(ctx, "b", "c")
	g.EdgeContext(ctx, "c", graph.End)
	return g
}

func newDB(t *testing.T) (*MemoryCheckpointDB, *fakeClock) {
	t.Helper()
	clock := &fakeClock{t: time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)}
	db := NewMemoryCheckpointDB(clock.now)
	if err := db.CreateThread("t1", "p1", "wf-rev-1"); err != nil {
		t.Fatal(err)
	}
	return db, clock
}

// T62/A34: after lease expiry and takeover, the old worker's write is
// fenced and the thread has one checkpoint sequence.
func TestTakeoverFencesTheOldWorker(t *testing.T) {
	ctx := audited(t)
	db, clock := newDB(t)
	p := &pipeline{calls: map[string]int{}, failB: 1}

	leaseA, err := db.Acquire("t1", "worker-a")
	if err != nil {
		t.Fatal(err)
	}
	storeA := db.Store(leaseA, "wf-rev-1")
	if _, err := p.graph(ctx, t).RunCheckpointed(ctx, "t1", envelope(), storeA); err == nil {
		t.Fatal("worker a should have crashed in b")
	}
	if _, err := db.Acquire("t1", "worker-b"); !errors.Is(err, ErrLeaseHeld) {
		t.Fatalf("live lease must block takeover: %v", err)
	}

	clock.advance(LeaseTTL + time.Second)
	leaseB, err := db.Acquire("t1", "worker-b")
	if err != nil || leaseB.Token <= leaseA.Token {
		t.Fatalf("takeover: lease=%+v err=%v", leaseB, err)
	}
	res, err := p.graph(ctx, t).RunCheckpointed(ctx, "t1", envelope(), db.Store(leaseB, "wf-rev-1"))
	if err != nil || !res.Finished || res.State["stage"] != "c" {
		t.Fatalf("worker b resume: finished=%v state=%v err=%v", res.Finished, res.State, err)
	}
	if p.calls["a"] != 1 || p.calls["b"] != 2 || p.calls["c"] != 1 {
		t.Fatalf("calls %v: committed node a must not re-run", p.calls)
	}

	// Worker a wakes up and writes a stale checkpoint.
	stale := graph.Checkpoint{State: envelope(), LastNode: "a", Steps: []graph.Step{{Node: "a"}}}
	if err := storeA.Save("t1", stale); !errors.Is(err, ErrFenced) {
		t.Fatalf("stale write: %v", err)
	}
	if _, err := db.Heartbeat(leaseA); !errors.Is(err, ErrFenced) {
		t.Fatalf("stale heartbeat: %v", err)
	}

	recs := db.Records("t1")
	if len(recs) != 4 { // a (worker a), b, c, End (worker b)
		t.Fatalf("records: %d", len(recs))
	}
	for i, r := range recs {
		if r.Sequence != int64(i+1) {
			t.Fatalf("sequence %d at %d", r.Sequence, i)
		}
		want := leaseB.Token
		if i == 0 {
			want = leaseA.Token
		}
		if r.Token != want || r.GraphRevision != "wf-rev-1" || r.Codec != CodecRevision || len(r.StateHash) != 64 {
			t.Fatalf("record %d: %+v", i, r)
		}
	}
	if recs[3].LastNode != graph.End {
		t.Fatalf("last record %q", recs[3].LastNode)
	}
}

func TestExpiredOwnLeaseCannotWriteButHeartbeatKeepsIt(t *testing.T) {
	db, clock := newDB(t)
	l, _ := db.Acquire("t1", "w")
	cp := graph.Checkpoint{State: envelope(), LastNode: "a", Steps: []graph.Step{{Node: "a"}}}
	for i := 0; i < 5; i++ { // heartbeats every 15s keep a 60s lease alive
		clock.advance(HeartbeatInterval)
		var err error
		if l, err = db.Heartbeat(l); err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Store(l, "wf-rev-1").Save("t1", cp); err != nil {
		t.Fatal(err)
	}
	clock.advance(LeaseTTL)
	if err := db.Store(l, "wf-rev-1").Save("t1", cp); !errors.Is(err, ErrFenced) {
		t.Fatalf("expired lease wrote: %v", err)
	}
}

// T60: a worker with another graph revision does not continue from the old
// LastNode, and runs nothing.
func TestGraphRevisionMismatchIsNotResumed(t *testing.T) {
	ctx := audited(t)
	db, _ := newDB(t)
	p := &pipeline{calls: map[string]int{}, failB: 1}
	l, _ := db.Acquire("t1", "w")
	_, _ = p.graph(ctx, t).RunCheckpointed(ctx, "t1", envelope(), db.Store(l, "wf-rev-1"))
	before := p.calls["a"] + p.calls["b"] + p.calls["c"]
	_, err := p.graph(ctx, t).RunCheckpointed(ctx, "t1", envelope(), db.Store(l, "wf-rev-2"))
	if !errors.Is(err, ErrRevisionMismatch) {
		t.Fatalf("revision mismatch: %v", err)
	}
	if after := p.calls["a"] + p.calls["b"] + p.calls["c"]; after != before {
		t.Fatalf("nodes ran under the wrong revision: %v", p.calls)
	}
	if err := db.Store(l, "wf-rev-2").Save("t1", graph.Checkpoint{State: envelope(), LastNode: "a", Steps: []graph.Step{}}); !errors.Is(err, ErrRevisionMismatch) {
		t.Fatalf("save under wrong revision: %v", err)
	}
}

func TestSaveRejectsInvalidStateAndLoadDetectsTampering(t *testing.T) {
	db, _ := newDB(t)
	l, _ := db.Acquire("t1", "w")
	s := db.Store(l, "wf-rev-1")
	bad := envelope()
	bad["attempt"] = 1.0 // what an unchecked JSON round trip would produce
	if err := s.Save("t1", graph.Checkpoint{State: bad, LastNode: "a", Steps: []graph.Step{}}); err == nil {
		t.Fatal("invalid envelope saved")
	}
	if err := s.Save("t1", graph.Checkpoint{State: envelope(), LastNode: "a", Steps: []graph.Step{}}); err != nil {
		t.Fatal(err)
	}
	c, found, err := s.Load("t1")
	if err != nil || !found || c.LastNode != "a" || c.State["attempt"].(int) != 0 {
		t.Fatalf("load: %+v %v %v", c, found, err)
	}
	// Change one stored byte of the state: the hash check catches it.
	row := db.threads["t1"]
	tampered := bytes.Replace(row.records[0].Bytes, []byte(`"inspect"`), []byte(`"Inspect"`), 1)
	if bytes.Equal(tampered, row.records[0].Bytes) {
		t.Fatal("tamper target not found")
	}
	row.records[0].Bytes = tampered
	if _, _, err := s.Load("t1"); err == nil {
		t.Fatal("tampered checkpoint loaded")
	}
	if _, err := db.Acquire("nope", "w"); !errors.Is(err, ErrUnknownThread) {
		t.Fatalf("unknown thread: %v", err)
	}
	if err := db.CreateThread("t1", "p2", "wf-rev-1"); err == nil {
		t.Fatal("thread ids must be globally unique")
	}
}
