package agentkitsmoke

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/Disesfgewu/agentkit/agentlog"
	"github.com/Disesfgewu/agentkit/graph"
)

// audited returns a context carrying an agentKit audit recorder that writes
// into the test's temp dir (agentKit otherwise opens sessions under ./log),
// and checks at cleanup that the hash-chained log verifies.
func audited(t *testing.T) context.Context {
	t.Helper()
	r, err := agentlog.New("worker-smoke", t.TempDir())
	if err != nil {
		t.Fatalf("agentlog.New: %v", err)
	}
	t.Cleanup(func() {
		if err := r.Close(); err != nil {
			t.Fatalf("audit log close: %v", err)
		}
		n, err := agentlog.Verify(r.Path)
		if err != nil || n == 0 {
			t.Fatalf("audit log %s: records=%d err=%v", r.Path, n, err)
		}
	})
	return agentlog.WithRecorder(context.Background(), r)
}

// job is a deterministic local stand-in for a worker job: inspect a
// document, route on its page count, then parse and finalize, or reject.
// Every node invocation is counted so resume can prove no committed node
// ran twice.
type job struct {
	calls     map[string]int
	failParse int // fail this many parse attempts before succeeding
}

func pages(s graph.State) (int, error) {
	// After a JSON round trip numbers are float64; accept both, nothing else.
	switch v := s["pages"].(type) {
	case int:
		return v, nil
	case float64:
		if v != float64(int(v)) {
			return 0, errors.New("pages is not an integer")
		}
		return int(v), nil
	}
	return 0, errors.New("pages missing")
}

func (j *job) build(ctx context.Context, t *testing.T) *graph.Graph {
	t.Helper()
	g := graph.NewContext(ctx)
	g.RetainPatches = true
	node := func(name string, fn func(graph.State) (graph.State, error)) {
		if err := g.AddNodeContext(ctx, name, func(_ context.Context, s graph.State) (graph.State, error) {
			j.calls[name]++
			return fn(s)
		}); err != nil {
			t.Fatalf("AddNode %s: %v", name, err)
		}
	}
	node("inspect", func(s graph.State) (graph.State, error) {
		n, err := pages(s)
		if err != nil {
			return nil, err
		}
		return graph.State{"inspected_pages": n}, nil
	})
	node("parse", func(s graph.State) (graph.State, error) {
		if j.failParse > 0 {
			j.failParse--
			return nil, errors.New("simulated worker crash during parse")
		}
		return graph.State{"parsed": true}, nil
	})
	node("finalize", func(graph.State) (graph.State, error) { return graph.State{"status": "done"}, nil })
	node("reject", func(graph.State) (graph.State, error) { return graph.State{"status": "rejected"}, nil })
	g.EdgeContext(ctx, graph.Start, "inspect")
	g.ConditionalContext(ctx, "inspect", func(s graph.State) (string, error) {
		n, err := pages(s)
		if err != nil {
			return "", err
		}
		if n > 2000 { // SPEC import page limit: over-limit documents are rejected
			return "too_large", nil
		}
		return "ok", nil
	}, map[string]string{"ok": "parse", "too_large": "reject"})
	g.EdgeContext(ctx, "parse", "finalize")
	g.EdgeContext(ctx, "finalize", graph.End)
	g.EdgeContext(ctx, "reject", graph.End)
	// No explicit g.Compile(): at ce49a71 it has no Context variant and opens
	// its own audit session under ./log. Run compiles with the audited ctx.
	return g
}

func newJob() *job { return &job{calls: map[string]int{}} }

func trace(steps []graph.Step) []string {
	out := make([]string, len(steps))
	for i, s := range steps {
		out[i] = s.Node
		if s.Route != "" {
			out[i] += "(" + s.Route + ")"
		}
	}
	return out
}

func TestConditionalRoute(t *testing.T) {
	cases := []struct {
		pages  int
		trace  []string
		status string
	}{
		{21, []string{"inspect", "parse(ok)", "finalize"}, "done"},
		{2001, []string{"inspect", "reject(too_large)"}, "rejected"},
	}
	ctx := audited(t)
	for _, c := range cases {
		j := newJob()
		res, err := j.build(ctx, t).Run(ctx, graph.State{"pages": c.pages})
		if err != nil || !res.Finished {
			t.Fatalf("pages=%d: finished=%v err=%v", c.pages, res.Finished, err)
		}
		if got := trace(res.Steps); !reflect.DeepEqual(got, c.trace) {
			t.Fatalf("pages=%d: trace %v, want %v", c.pages, got, c.trace)
		}
		if res.State["status"] != c.status {
			t.Fatalf("pages=%d: status %v, want %s", c.pages, res.State["status"], c.status)
		}
	}
}

func TestRouterErrorStopsTheRun(t *testing.T) {
	ctx := audited(t)
	j := newJob()
	res, err := j.build(ctx, t).Run(ctx, graph.State{})
	if err == nil || res.Finished || !strings.Contains(err.Error(), "pages missing") {
		t.Fatalf("missing pages: finished=%v err=%v", res.Finished, err)
	}
	if j.calls["parse"]+j.calls["reject"] != 0 {
		t.Fatalf("no node after a failed route may run: %v", j.calls)
	}
}

// jsonStore keeps checkpoints only as JSON bytes, the way a durable store
// (#6) would, so resume works from serialized state rather than shared maps.
type jsonStore struct{ items map[string][]byte }

func (s *jsonStore) Save(id string, c graph.Checkpoint) error {
	b, err := json.Marshal(c)
	if err != nil {
		return err
	}
	s.items[id] = b
	return nil
}

func (s *jsonStore) Load(id string) (graph.Checkpoint, bool, error) {
	b, ok := s.items[id]
	if !ok {
		return graph.Checkpoint{}, false, nil
	}
	var c graph.Checkpoint
	return c, true, json.Unmarshal(b, &c)
}

func TestCheckpointResumeAfterCrash(t *testing.T) {
	stores := map[string]func() graph.Store{
		"memory": func() graph.Store { return graph.NewMemoryStore() },
		"json":   func() graph.Store { return &jsonStore{items: map[string][]byte{}} },
	}
	for name, mk := range stores {
		t.Run(name, func(t *testing.T) {
			ctx := audited(t)
			store := mk()
			j := newJob()
			j.failParse = 1
			_, err := j.build(ctx, t).RunCheckpointed(ctx, "job-1", graph.State{"pages": 21}, store)
			if err == nil || !strings.Contains(err.Error(), "simulated worker crash") {
				t.Fatalf("first run: want crash, got %v", err)
			}
			cp, found, err := store.Load("job-1")
			if err != nil || !found || cp.LastNode != "inspect" {
				t.Fatalf("checkpoint after crash: found=%v last=%q err=%v", found, cp.LastNode, err)
			}

			// "Restart": a fresh graph instance resumes from the stored
			// checkpoint; the committed inspect node must not run again.
			res, err := j.build(ctx, t).RunCheckpointed(ctx, "job-1", graph.State{"pages": 21}, store)
			if err != nil || !res.Finished {
				t.Fatalf("resume: finished=%v err=%v", res.Finished, err)
			}
			want := map[string]int{"inspect": 1, "parse": 2, "finalize": 1}
			if !reflect.DeepEqual(j.calls, want) {
				t.Fatalf("node calls %v, want %v (inspect committed once, parse retried once)", j.calls, want)
			}
			if got := trace(res.Steps); !reflect.DeepEqual(got, []string{"inspect", "parse(ok)", "finalize"}) {
				t.Fatalf("resumed trace %v", got)
			}
			if res.State["status"] != "done" || res.State["parsed"] != true {
				t.Fatalf("final state %v", res.State)
			}

			// A finished thread returns its result without running any node.
			before := j.calls["inspect"] + j.calls["parse"] + j.calls["finalize"]
			res, err = j.build(ctx, t).RunCheckpointed(ctx, "job-1", graph.State{"pages": 21}, store)
			after := j.calls["inspect"] + j.calls["parse"] + j.calls["finalize"]
			if err != nil || !res.Finished || before != after {
				t.Fatalf("finished thread re-ran nodes or failed: err=%v calls=%v", err, j.calls)
			}
		})
	}
}
