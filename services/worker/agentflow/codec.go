// Package agentflow is the LAB worker's integration layer over agentKit's
// graph runtime (issue #6, SPEC 12.2–12.3): a type-preserving state codec,
// fenced checkpoint stores, idempotent node operations and cost reservation.
package agentflow

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"

	"github.com/Disesfgewu/agentkit/graph"
)

// CodecRevision names this encoding. It is stored with every checkpoint;
// a checkpoint written by another revision is not decoded.
const CodecRevision = "lab-state-codec-1"

// Ref is a typed reference to a stored resource (document, artifact, budget
// ...). State carries refs, never the resource bytes.
type Ref struct {
	Kind string
	ID   string
}

var ErrUnsupportedType = errors.New("unsupported state value type")

// Supported value types, and only these, round-trip exactly:
//
//	nil, bool, string, int, int64, float64 (finite), Ref,
//	map[string]any and []any of supported values.
//
// agentKit copies state only at the top level, so nested maps and lists
// must be treated as immutable by nodes (SPEC 12.2); the codec encodes them
// by value.
//
// Each value is a one-key object tagging its type, e.g. {"i":"42"},
// {"f":"0x1.8p+01"}, {"m":{...}}. Integers are decimal strings and floats
// are exact hex, so nothing passes through float64 JSON numbers and a
// resumed int stays an int (SPEC T64). Map keys are sorted (encoding/json),
// so equal states encode to identical bytes.
func encodeValue(v any, path string) (any, error) {
	switch x := v.(type) {
	case nil:
		return map[string]any{"n": true}, nil
	case bool:
		return map[string]any{"b": x}, nil
	case string:
		return map[string]any{"s": x}, nil
	case int:
		return map[string]any{"i": strconv.FormatInt(int64(x), 10)}, nil
	case int64:
		return map[string]any{"i64": strconv.FormatInt(x, 10)}, nil
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return nil, fmt.Errorf("%s: non-finite float %v", path, x)
		}
		if x == 0 && math.Signbit(x) {
			x = 0 // one canonical zero
		}
		return map[string]any{"f": strconv.FormatFloat(x, 'x', -1, 64)}, nil
	case Ref:
		if x.Kind == "" || x.ID == "" {
			return nil, fmt.Errorf("%s: ref needs kind and id", path)
		}
		return map[string]any{"r": map[string]any{"kind": x.Kind, "id": x.ID}}, nil
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			ev, err := encodeValue(e, path+"."+k)
			if err != nil {
				return nil, err
			}
			out[k] = ev
		}
		return map[string]any{"m": out}, nil
	case graph.State:
		return encodeValue(map[string]any(x), path)
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			ev, err := encodeValue(e, fmt.Sprintf("%s[%d]", path, i))
			if err != nil {
				return nil, err
			}
			out[i] = ev
		}
		return map[string]any{"l": out}, nil
	}
	return nil, fmt.Errorf("%w at %s: %T", ErrUnsupportedType, path, v)
}

func decodeValue(raw json.RawMessage, path string) (any, error) {
	var tagged map[string]json.RawMessage
	if err := strictUnmarshal(raw, &tagged); err != nil || len(tagged) != 1 {
		return nil, fmt.Errorf("%s: not a tagged value", path)
	}
	for tag, body := range tagged {
		switch tag {
		case "n":
			var b bool
			if err := strictUnmarshal(body, &b); err != nil || !b {
				return nil, fmt.Errorf("%s: bad null", path)
			}
			return nil, nil
		case "b":
			var b bool
			if err := strictUnmarshal(body, &b); err != nil {
				return nil, fmt.Errorf("%s: bad bool", path)
			}
			return b, nil
		case "s":
			var s string
			if err := strictUnmarshal(body, &s); err != nil {
				return nil, fmt.Errorf("%s: bad string", path)
			}
			return s, nil
		case "i", "i64":
			var s string
			if err := strictUnmarshal(body, &s); err != nil {
				return nil, fmt.Errorf("%s: bad integer", path)
			}
			bits := 64
			if tag == "i" {
				bits = strconv.IntSize
			}
			n, err := strconv.ParseInt(s, 10, bits)
			if err != nil || strconv.FormatInt(n, 10) != s {
				return nil, fmt.Errorf("%s: bad integer %q", path, s)
			}
			if tag == "i" {
				return int(n), nil
			}
			return n, nil
		case "f":
			var s string
			if err := strictUnmarshal(body, &s); err != nil {
				return nil, fmt.Errorf("%s: bad float", path)
			}
			f, err := strconv.ParseFloat(s, 64)
			if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || strconv.FormatFloat(f, 'x', -1, 64) != s {
				return nil, fmt.Errorf("%s: bad float %q", path, s)
			}
			return f, nil
		case "r":
			var r struct {
				Kind string `json:"kind"`
				ID   string `json:"id"`
			}
			if err := strictUnmarshal(body, &r); err != nil || r.Kind == "" || r.ID == "" {
				return nil, fmt.Errorf("%s: bad ref", path)
			}
			return Ref{Kind: r.Kind, ID: r.ID}, nil
		case "m":
			var m map[string]json.RawMessage
			if err := strictUnmarshal(body, &m); err != nil || m == nil {
				return nil, fmt.Errorf("%s: bad map", path)
			}
			out := make(map[string]any, len(m))
			for k, e := range m {
				v, err := decodeValue(e, path+"."+k)
				if err != nil {
					return nil, err
				}
				out[k] = v
			}
			return out, nil
		case "l":
			var l []json.RawMessage
			if err := strictUnmarshal(body, &l); err != nil || l == nil {
				return nil, fmt.Errorf("%s: bad list", path)
			}
			out := make([]any, len(l))
			for i, e := range l {
				v, err := decodeValue(e, fmt.Sprintf("%s[%d]", path, i))
				if err != nil {
					return nil, err
				}
				out[i] = v
			}
			return out, nil
		}
		return nil, fmt.Errorf("%s: unknown tag %q", path, tag)
	}
	panic("unreachable")
}

func strictUnmarshal(b []byte, v any) error {
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return err
	}
	if d.More() {
		return errors.New("trailing data")
	}
	return nil
}

// EncodeState returns the canonical bytes of a state.
func EncodeState(s graph.State) ([]byte, error) {
	if s == nil {
		return nil, errors.New("state is nil")
	}
	v, err := encodeValue(map[string]any(s), "state")
	if err != nil {
		return nil, err
	}
	return json.Marshal(v)
}

// DecodeState is the inverse of EncodeState and rejects anything else.
func DecodeState(b []byte) (graph.State, error) {
	v, err := decodeValue(b, "state")
	if err != nil {
		return nil, err
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, errors.New("state: not a map")
	}
	return graph.State(m), nil
}

// StateHash is the sha256 of the canonical encoding: equal states give equal
// hashes regardless of map order, and a type change (1 vs 1.0) changes it.
func StateHash(s graph.State) (string, error) {
	b, err := EncodeState(s)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

// StateSchemaVersion is the version of the required state envelope below.
const StateSchemaVersion = 1

// RequiredStateKeys and their types (SPEC 12.2: project_id, job_id,
// thread_id, workflow_revision_id, resource refs, stage, attempt counter,
// budget ref, state_schema_version).
var RequiredStateKeys = map[string]string{
	"project_id":           "string",
	"job_id":               "string",
	"thread_id":            "string",
	"workflow_revision_id": "string",
	"stage":                "string",
	"attempt":              "int",
	"budget_ref":           "ref",
	"resource_refs":        "list of ref",
	"state_schema_version": "int",
}

// ValidateEnvelope checks that a state carries the SPEC envelope with the
// right types and this schema version.
func ValidateEnvelope(s graph.State) error {
	var problems []string
	keys := make([]string, 0, len(RequiredStateKeys))
	for k := range RequiredStateKeys {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		v, ok := s[k]
		if !ok {
			problems = append(problems, k+": missing")
			continue
		}
		good := false
		switch RequiredStateKeys[k] {
		case "string":
			x, isStr := v.(string)
			good = isStr && x != ""
		case "int":
			x, isInt := v.(int)
			good = isInt && x >= 0
		case "ref":
			_, good = v.(Ref)
		case "list of ref":
			l, isList := v.([]any)
			good = isList
			for _, e := range l {
				if _, isRef := e.(Ref); !isRef {
					good = false
				}
			}
		}
		if !good {
			problems = append(problems, fmt.Sprintf("%s: want %s, got %T", k, RequiredStateKeys[k], v))
		}
	}
	if v, ok := s["state_schema_version"].(int); ok && v != StateSchemaVersion {
		problems = append(problems, fmt.Sprintf("state_schema_version: %d, this worker supports %d", v, StateSchemaVersion))
	}
	if len(problems) > 0 {
		return fmt.Errorf("invalid state envelope: %v", problems)
	}
	return nil
}

type encodedStep struct {
	Node  string          `json:"node"`
	Route string          `json:"route,omitempty"`
	Patch json.RawMessage `json:"patch,omitempty"`
}

type encodedCheckpoint struct {
	Codec    string          `json:"codec"`
	State    json.RawMessage `json:"state"`
	Steps    []encodedStep   `json:"steps"`
	LastNode string          `json:"last_node"`
}

// EncodeCheckpoint encodes an agentKit checkpoint, including step patches,
// with the state codec.
func EncodeCheckpoint(c graph.Checkpoint) ([]byte, error) {
	st, err := EncodeState(c.State)
	if err != nil {
		return nil, err
	}
	if c.LastNode == "" {
		return nil, errors.New("checkpoint: empty last node")
	}
	e := encodedCheckpoint{Codec: CodecRevision, State: st, Steps: []encodedStep{}, LastNode: c.LastNode}
	for i, s := range c.Steps {
		es := encodedStep{Node: s.Node, Route: s.Route}
		if s.Patch != nil {
			p, err := EncodeState(s.Patch)
			if err != nil {
				return nil, fmt.Errorf("step %d: %w", i, err)
			}
			es.Patch = p
		}
		e.Steps = append(e.Steps, es)
	}
	return json.Marshal(e)
}

// DecodeCheckpoint rejects other codec revisions and malformed input.
func DecodeCheckpoint(b []byte) (graph.Checkpoint, error) {
	var e encodedCheckpoint
	if err := strictUnmarshal(b, &e); err != nil {
		return graph.Checkpoint{}, fmt.Errorf("checkpoint: %w", err)
	}
	if e.Codec != CodecRevision {
		return graph.Checkpoint{}, fmt.Errorf("checkpoint: codec %q, this worker reads %q", e.Codec, CodecRevision)
	}
	if e.LastNode == "" || e.Steps == nil {
		return graph.Checkpoint{}, errors.New("checkpoint: missing last node or steps")
	}
	st, err := DecodeState(e.State)
	if err != nil {
		return graph.Checkpoint{}, err
	}
	c := graph.Checkpoint{State: st, LastNode: e.LastNode, Steps: make([]graph.Step, 0, len(e.Steps))}
	for i, es := range e.Steps {
		s := graph.Step{Node: es.Node, Route: es.Route}
		if es.Patch != nil {
			p, err := DecodeState(es.Patch)
			if err != nil {
				return graph.Checkpoint{}, fmt.Errorf("step %d: %w", i, err)
			}
			s.Patch = p
		}
		c.Steps = append(c.Steps, s)
	}
	return c, nil
}
