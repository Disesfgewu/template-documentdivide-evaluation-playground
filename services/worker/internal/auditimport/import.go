// Package auditimport verifies pinned agentlog bundles and projects metadata only.
// Raw logs remain restricted artifacts; this package never emits their payloads.
package auditimport

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const ProducerRevision = "ce49a7130b17ccda07c755de96ef44c49d0b719c"
const MaxArtifactBytes = 16 << 20
const MaxSessionBytes = 32 << 20
const MaxLineBytes = 1 << 20
const MaxCalls = 10000

// Binding is supplied by an authorized caller, never inferred from log contents.
type Binding struct {
	ProjectID   string `json:"project_id"`
	OperationID string `json:"operation_id"`
	SourceID    string `json:"source_id"`
}
type Artifact struct {
	Kind       string `json:"kind"`
	Present    bool   `json:"present"`
	SHA256     string `json:"sha256,omitempty"`
	Size       int    `json:"size"`
	Restricted bool   `json:"restricted"`
}
type Event struct {
	EventID      string    `json:"event_id"`
	SpanID       string    `json:"span_id"`
	ParentSpanID *string   `json:"parent_span_id"`
	CallID       uint64    `json:"call_id"`
	ParentCallID uint64    `json:"parent_call_id"`
	Sequence     *uint64   `json:"sequence"`
	Phase        string    `json:"phase"`
	Level        string    `json:"level"`
	Status       string    `json:"status"`
	OccurredAt   time.Time `json:"occurred_at"`
	DurationMS   *float64  `json:"duration_ms"`
	RecordSHA256 string    `json:"record_sha256"`
}
type Report struct {
	SchemaVersion         string     `json:"schema_version"`
	ProducerRevision      string     `json:"producer_revision"`
	VerifierRevision      string     `json:"verifier_revision"`
	Binding               Binding    `json:"binding"`
	Status                string     `json:"status"`
	Artifacts             []Artifact `json:"artifacts"`
	Events                []Event    `json:"events"`
	Diagnostics           []string   `json:"diagnostics"`
	RedactionPolicy       string     `json:"redaction_policy"`
	Reproducibility       string     `json:"reproducibility"`
	ProducerSessionSHA256 *string    `json:"producer_session_sha256"`
}
type begin struct {
	CallID   uint64          `json:"call_id"`
	ParentID uint64          `json:"parent_call_id"`
	Function string          `json:"function"`
	Input    json.RawMessage `json:"input"`
	Time     time.Time       `json:"time"`
}
type closeSeal struct {
	Records     uint64
	LastHash    string
	JournalHash string
	Calls       uint64
}

var opaque = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,128}$`)
var suffix = regexp.MustCompile(`^, token:(\?|[0-9]+), response_time:([^,]+), time:([^,]+), effective_time:([^,]+), session:([A-Za-z0-9_.-]+), seq:([0-9]+), call_id:([0-9]+), parent_call_id:([0-9]+), phase:END, prev_hash:([a-f0-9]*)$`)
var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

func digest(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func stable(b Binding, call uint64, phase string) string {
	p, _ := json.Marshal([]any{b.ProjectID, b.SourceID, call, phase})
	return digest(p)
}

// uniqueJSON rejects duplicate keys at every nesting level, including ignored
// payload fields. Exact pin output is canonical JSON and cannot contain these.
func uniqueJSON(raw []byte) error {
	if !utf8.Valid(raw) {
		return errors.New("invalid UTF-8")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var value func() error
	value = func() error {
		t, e := d.Token()
		if e != nil {
			return e
		}
		delim, ok := t.(json.Delim)
		if !ok {
			return nil
		}
		switch delim {
		case '{':
			seen := map[string]bool{}
			for d.More() {
				key, e := d.Token()
				if e != nil {
					return e
				}
				k, ok := key.(string)
				if !ok || seen[k] {
					return errors.New("duplicate JSON key")
				}
				seen[k] = true
				if e = value(); e != nil {
					return e
				}
			}
			_, e = d.Token()
			return e
		case '[':
			for d.More() {
				if e = value(); e != nil {
					return e
				}
			}
			_, e = d.Token()
			return e
		default:
			return errors.New("unexpected delimiter")
		}
	}
	if e := value(); e != nil {
		return e
	}
	if _, e := d.Token(); e != io.EOF {
		return errors.New("trailing JSON")
	}
	return nil
}
func strictJSON(raw []byte, dest any) error {
	if uniqueJSON(raw) != nil {
		return errors.New("invalid JSON")
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil || fields == nil {
		return errors.New("invalid object")
	}
	var required []string
	switch dest.(type) {
	case *begin:
		required = []string{"call_id", "parent_call_id", "function", "input", "time"}
	case *closeSeal:
		required = []string{"Records", "LastHash", "JournalHash", "Calls"}
	default:
		return errors.New("unsupported schema")
	}
	if len(fields) != len(required) {
		return errors.New("invalid fields")
	}
	for _, k := range required {
		v, ok := fields[k]
		if !ok || (k != "input" && bytes.Equal(v, []byte("null"))) {
			return errors.New("missing field")
		}
	}
	if json.Unmarshal(raw, dest) != nil {
		return errors.New("invalid JSON")
	}
	return nil
}
func lines(raw []byte) ([][]byte, error) {
	if len(raw) > MaxArtifactBytes {
		return nil, errors.New("artifact_size_limit")
	}
	if len(raw) == 0 {
		return nil, nil
	}
	if raw[len(raw)-1] != '\n' {
		return nil, errors.New("truncated_line")
	}
	if bytes.Count(raw, []byte{'\n'}) > MaxCalls {
		return nil, errors.New("record_count_limit")
	}
	result := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(result) > MaxCalls {
		return nil, errors.New("record_count_limit")
	}
	for _, line := range result {
		if len(line) == 0 || len(line) > MaxLineBytes {
			return nil, errors.New("line_size_or_empty")
		}
	}
	return result, nil
}

// Import verifies exact bytes in memory. nil means a missing artifact; a non-nil
// empty slice means an existing empty artifact. It returns a safe report even on
// invalid/incomplete input. Events are withheld for invalid bundles.
func Import(binding Binding, log, journal, seal []byte) (Report, error) {
	r := Report{SchemaVersion: "audit-import-1", ProducerRevision: ProducerRevision, VerifierRevision: "lab-agentlog-1", Binding: binding, Status: "verified", Events: []Event{}, Diagnostics: []string{}, RedactionPolicy: "metadata-only-1", Reproducibility: "partial"}
	fail := func(code string) (Report, error) {
		r.Status = "invalid"
		r.Events = []Event{}
		r.Diagnostics = append(r.Diagnostics, code)
		return r, errors.New(code)
	}
	for _, v := range []string{binding.ProjectID, binding.OperationID, binding.SourceID} {
		if !opaque.MatchString(v) {
			return fail("invalid_binding")
		}
	}
	for i, raw := range [][]byte{log, journal, seal} {
		a := Artifact{Kind: []string{"log", "journal", "seal"}[i], Present: raw != nil, Size: len(raw), Restricted: true}
		if raw != nil {
			a.SHA256 = digest(raw)
		}
		r.Artifacts = append(r.Artifacts, a)
		if len(raw) > MaxArtifactBytes {
			return fail("artifact_size_limit")
		}
		if !utf8.Valid(raw) {
			return fail("invalid_utf8")
		}
	}
	if len(log)+len(journal)+len(seal) > MaxSessionBytes {
		return fail("session_size_limit")
	}
	incomplete := func(code string) { r.Status = "incomplete"; r.Diagnostics = append(r.Diagnostics, code) }
	if log == nil {
		incomplete("missing_log")
	}
	if journal == nil {
		incomplete("missing_journal")
	}
	if seal == nil {
		incomplete("missing_seal")
	}
	jl, err := lines(journal)
	if err != nil {
		return fail("journal_" + err.Error())
	}
	ll, err := lines(log)
	if err != nil {
		return fail("log_" + err.Error())
	}
	begins := map[uint64]begin{}
	for _, line := range jl {
		var b begin
		if strictJSON(line, &b) != nil || b.CallID != uint64(len(begins)+1) || b.ParentID >= b.CallID || b.Time.IsZero() || len(b.Input) == 0 || !json.Valid(b.Input) {
			return fail("invalid_journal_record")
		}
		if b.ParentID != 0 {
			if _, ok := begins[b.ParentID]; !ok {
				return fail("missing_parent")
			}
		}
		begins[b.CallID] = b
		var parent *string
		if b.ParentID != 0 {
			v := stable(binding, b.ParentID, "span")
			parent = &v
		}
		r.Events = append(r.Events, Event{EventID: stable(binding, b.CallID, "begin:"+digest(line)), SpanID: stable(binding, b.CallID, "span"), ParentSpanID: parent, CallID: b.CallID, ParentCallID: b.ParentID, Phase: "begin", Level: "INFO", Status: "started", OccurredAt: b.Time.UTC(), RecordSHA256: digest(line)})
	}
	seen := map[uint64]bool{}
	last := ""
	session := ""
	for i, line := range ll {
		if !bytes.HasPrefix(line, []byte("[DEBUG] ")) {
			return fail("invalid_log_prefix")
		}
		content := string(line[len("[DEBUG] "):])
		at := strings.LastIndex(content, ", hash:")
		if at < 0 {
			return fail("missing_record_hash")
		}
		body, hash := content[:at], content[at+7:]
		if !digestPattern.MatchString(hash) || digest([]byte(body)) != hash {
			return fail("record_hash_mismatch")
		}
		// Locate metadata from the end, then use its call ID to select the exact
		// journal prefix. JSON return data is decoded before metadata is accepted.
		tailAt := strings.LastIndex(body, ", token:")
		if tailAt < 0 {
			return fail("invalid_log_metadata")
		}
		m := suffix.FindStringSubmatch(body[tailAt:])
		if m == nil {
			return fail("invalid_log_metadata")
		}
		seq, e1 := strconv.ParseUint(m[6], 10, 64)
		id, e2 := strconv.ParseUint(m[7], 10, 64)
		parent, e3 := strconv.ParseUint(m[8], 10, 64)
		if e1 != nil || e2 != nil || e3 != nil || seq != uint64(i+1) || m[9] != last {
			return fail("sequence_or_chain_mismatch")
		}
		b, ok := begins[id]
		if !ok || seen[id] || parent != b.ParentID {
			return fail("call_journal_mismatch")
		}
		if session != "" && session != m[5] {
			return fail("session_mismatch")
		}
		session = m[5]
		prefix := "agent:"
		agentEnd := strings.Index(body, ", func:")
		if !strings.HasPrefix(body, prefix) || agentEnd < len(prefix) || !opaque.MatchString(body[len(prefix):agentEnd]) {
			return fail("invalid_agent")
		}
		want := body[:agentEnd] + ", func:" + b.Function + "(input:" + string(b.Input) + "), return:"
		if len(want) > tailAt || !strings.HasPrefix(body, want) {
			return fail("call_journal_mismatch")
		}
		rawReturn := []byte(body[len(want):tailAt])
		var out any
		d := json.NewDecoder(bytes.NewReader(rawReturn))
		d.UseNumber()
		if uniqueJSON(rawReturn) != nil || d.Decode(&out) != nil || d.Decode(new(any)) != io.EOF {
			return fail("invalid_return_json")
		}
		elapsed, err := time.ParseDuration(m[2])
		if err != nil || elapsed < 0 {
			return fail("invalid_duration")
		}
		occurred, err := time.Parse(time.RFC3339Nano, m[3])
		if err != nil {
			return fail("invalid_time")
		}
		if m[4] != "?" {
			if _, err = time.Parse(time.RFC3339Nano, m[4]); err != nil {
				return fail("invalid_effective_time")
			}
		}
		level, status := "INFO", "completed"
		if obj, ok := out.(map[string]any); ok {
			if _, hasError := obj["error"]; hasError {
				level, status = "ERROR", "reported_error"
			}
		}
		ms := float64(elapsed) / float64(time.Millisecond)
		var parentSpan *string
		if parent != 0 {
			v := stable(binding, parent, "span")
			parentSpan = &v
		}
		r.Events = append(r.Events, Event{EventID: stable(binding, id, "end:"+hash), SpanID: stable(binding, id, "span"), ParentSpanID: parentSpan, CallID: id, ParentCallID: parent, Sequence: &seq, Phase: "end", Level: level, Status: status, OccurredAt: occurred.UTC(), DurationMS: &ms, RecordSHA256: hash})
		seen[id] = true
		last = hash
	}
	for id := uint64(1); id <= uint64(len(begins)); id++ {
		b := begins[id]
		if !seen[id] {
			incomplete("missing_end:" + strconv.FormatUint(id, 10))
			var p *string
			if b.ParentID != 0 {
				v := stable(binding, b.ParentID, "span")
				p = &v
			}
			r.Events = append(r.Events, Event{EventID: stable(binding, id, "incomplete"), SpanID: stable(binding, id, "span"), ParentSpanID: p, CallID: id, ParentCallID: b.ParentID, Phase: "reconciliation", Level: "WARN", Status: "incomplete", OccurredAt: b.Time.UTC(), RecordSHA256: ""})
		}
	}
	if seal != nil {
		var s closeSeal
		if strictJSON(seal, &s) != nil {
			return fail("invalid_seal")
		}
		if s.JournalHash != digest(journal) || s.LastHash != last || s.Records != uint64(len(ll)) || s.Calls != uint64(len(jl)) {
			return fail("seal_mismatch")
		}
		if len(seen) != len(begins) {
			incomplete("unclosed_calls")
		}
	}
	if session != "" {
		h := digest([]byte(session))
		r.ProducerSessionSHA256 = &h
	}
	sort.Strings(r.Diagnostics)
	if r.Status != "verified" {
		return r, fmt.Errorf("audit_%s", r.Status)
	}
	return r, nil
}
