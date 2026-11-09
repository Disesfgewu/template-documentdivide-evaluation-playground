package agentflow

import (
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"strings"
	"testing"

	"github.com/Disesfgewu/agentkit/graph"
)

func envelope() graph.State {
	return graph.State{
		"project_id":           "p1",
		"job_id":               "j1",
		"thread_id":            "t1",
		"workflow_revision_id": "wf-rev-1",
		"stage":                "inspect",
		"attempt":              0,
		"budget_ref":           Ref{Kind: "budget", ID: "b1"},
		"resource_refs":        []any{Ref{Kind: "document", ID: "d1"}},
		"state_schema_version": StateSchemaVersion,
	}
}

func rich() graph.State {
	s := envelope()
	s["pages"] = 21
	s["big"] = int64(9007199254740993) // 2^53+1: not representable as float64
	s["score"] = 0.1
	s["whole_float"] = 2.0
	s["none"] = nil
	s["ok"] = true
	s["nested"] = map[string]any{"list": []any{1, 2.5, "x", map[string]any{"deep": Ref{Kind: "artifact", ID: "a9"}}}}
	return s
}

// T64: int/float/nested/refs round-trip with their Go types intact.
func TestStateRoundTripPreservesTypes(t *testing.T) {
	in := rich()
	b, err := EncodeState(in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := DecodeState(b)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(in, out) {
		t.Fatalf("round trip changed state:\n in=%#v\nout=%#v", in, out)
	}
	if _, ok := out["pages"].(int); !ok {
		t.Fatalf("pages decoded as %T", out["pages"])
	}
	if out["big"].(int64) != 9007199254740993 {
		t.Fatalf("int64 lost precision: %v", out["big"])
	}
	if _, ok := out["whole_float"].(float64); !ok {
		t.Fatalf("2.0 decoded as %T", out["whole_float"])
	}
	// The failure this codec prevents: plain JSON turns ints into float64.
	plain, _ := json.Marshal(in)
	var back map[string]any
	_ = json.Unmarshal(plain, &back)
	if _, ok := back["pages"].(float64); !ok {
		t.Fatalf("expected plain JSON to lose the int type, got %T", back["pages"])
	}
}

func TestStateHashStableAndTypeSensitive(t *testing.T) {
	a := graph.State{}
	b := graph.State{}
	for _, k := range []string{"x", "y", "z"} {
		a[k] = k
	}
	for _, k := range []string{"z", "y", "x"} {
		b[k] = k
	}
	ha, _ := StateHash(a)
	hb, _ := StateHash(b)
	if ha != hb || len(ha) != 64 {
		t.Fatalf("hash depends on insertion order: %s %s", ha, hb)
	}
	h1, _ := StateHash(graph.State{"n": 1})
	h2, _ := StateHash(graph.State{"n": 1.0})
	if h1 == h2 {
		t.Fatal("int 1 and float 1.0 must hash differently")
	}
	z1, _ := StateHash(graph.State{"z": 0.0})
	z2, _ := StateHash(graph.State{"z": math.Copysign(0, -1)})
	if z1 != z2 {
		t.Fatal("-0.0 and 0.0 must have one canonical encoding")
	}
}

func TestUnsupportedValuesRejected(t *testing.T) {
	cases := map[string]any{
		"int32":      int32(1),
		"struct":     struct{ A int }{1},
		"func":       func() {},
		"string map": map[string]string{"a": "b"},
		"NaN":        math.NaN(),
		"Inf":        math.Inf(1),
		"empty ref":  Ref{Kind: "document"},
	}
	for name, v := range cases {
		if _, err := EncodeState(graph.State{"v": v}); err == nil {
			t.Errorf("%s: encoded, want error", name)
		}
	}
	_, err := EncodeState(graph.State{"v": []any{int32(1)}})
	if !errors.Is(err, ErrUnsupportedType) || !strings.Contains(err.Error(), "state.v[0]") {
		t.Fatalf("nested unsupported type: %v", err)
	}
}

func TestDecodeRejectsMalformed(t *testing.T) {
	bad := map[string]string{
		"untagged":          `{"m":{"a":1}}`,
		"two tags":          `{"m":{"a":{"s":"x","b":true}}}`,
		"unknown tag":       `{"m":{"a":{"q":1}}}`,
		"non-canonical int": `{"m":{"a":{"i":"01"}}}`,
		"int overflow":      `{"m":{"a":{"i64":"9223372036854775808"}}}`,
		"non-canonical f":   `{"m":{"a":{"f":"1.5"}}}`,
		"trailing data":     `{"m":{}} {}`,
		"not a map":         `{"l":[]}`,
		"bad null":          `{"m":{"a":{"n":false}}}`,
		"ref extra field":   `{"m":{"a":{"r":{"kind":"d","id":"1","x":1}}}}`,
	}
	for name, raw := range bad {
		if _, err := DecodeState([]byte(raw)); err == nil {
			t.Errorf("%s: decoded, want error", name)
		}
	}
}

func TestEnvelopeValidation(t *testing.T) {
	if err := ValidateEnvelope(envelope()); err != nil {
		t.Fatal(err)
	}
	cases := map[string]func(graph.State){
		"missing job_id":   func(s graph.State) { delete(s, "job_id") },
		"attempt float":    func(s graph.State) { s["attempt"] = 1.0 },
		"empty stage":      func(s graph.State) { s["stage"] = "" },
		"budget not ref":   func(s graph.State) { s["budget_ref"] = "b1" },
		"refs not refs":    func(s graph.State) { s["resource_refs"] = []any{"d1"} },
		"future version":   func(s graph.State) { s["state_schema_version"] = StateSchemaVersion + 1 },
		"negative attempt": func(s graph.State) { s["attempt"] = -1 },
	}
	for name, mutate := range cases {
		s := envelope()
		mutate(s)
		if err := ValidateEnvelope(s); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestCheckpointRoundTripAndCodecRevision(t *testing.T) {
	c := graph.Checkpoint{State: rich(), LastNode: "parse", Steps: []graph.Step{
		{Node: "inspect", Patch: graph.State{"pages": 21}},
		{Node: "parse", Route: "ok", Patch: graph.State{"parsed": true}},
	}}
	b, err := EncodeCheckpoint(c)
	if err != nil {
		t.Fatal(err)
	}
	got, err := DecodeCheckpoint(b)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(c, got) {
		t.Fatalf("checkpoint changed:\n%#v\n%#v", c, got)
	}
	other := strings.Replace(string(b), CodecRevision, "lab-state-codec-0", 1)
	if _, err := DecodeCheckpoint([]byte(other)); err == nil || !strings.Contains(err.Error(), "codec") {
		t.Fatalf("other codec revision decoded: %v", err)
	}
}
