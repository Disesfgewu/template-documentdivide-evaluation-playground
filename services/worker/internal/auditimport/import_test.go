package auditimport

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
)

var binding = Binding{"project-1", "operation-1", "source-1"}
var stamp = time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC)

func mustJSON(v any) []byte {
	b, e := json.Marshal(v)
	if e != nil {
		panic(e)
	}
	return b
}
func fixture(nested bool) (log, journal, seal []byte) {
	b1 := begin{1, 0, "root(input: , return: awkward)", json.RawMessage(`{"secret":"NEVER_EMIT_SECRET", "nested":[1,{"text":", seq:999, call_id:111, token:999"}]}`), stamp}
	// RawMessage serialization compacts input; use the same canonical bytes in log.
	b1.Input = mustJSON(map[string]any{"secret": "NEVER_EMIT_SECRET", "nested": []any{1, map[string]any{"text": ", seq:999, call_id:111, token:999"}}})
	bs := []begin{b1}
	if nested {
		bs = append(bs, begin{2, 1, "child", json.RawMessage(`null`), stamp})
	}
	for _, b := range bs {
		journal = append(journal, append(mustJSON(b), '\n')...)
	}
	order := []int{0}
	if nested {
		order = []int{1, 0}
	}
	prev := ""
	for i, j := range order {
		b := bs[j]
		out := `{"output":{"url":"https://private/?token=NEVER_EMIT_SECRET", "text":", hash:fake, token:999"}}`
		if j == 1 {
			out = `{"error":"NEVER_EMIT_SECRET", "output":null}`
		}
		body := fmt.Sprintf("agent:test, func:%s(input:%s), return:%s, token:?, response_time:1.2ms, time:%s, effective_time:?, session:test_2026, seq:%d, call_id:%d, parent_call_id:%d, phase:END, prev_hash:%s", b.Function, b.Input, out, stamp.Format(time.RFC3339Nano), i+1, b.CallID, b.ParentID, prev)
		prev = digest([]byte(body))
		log = append(log, []byte("[DEBUG] "+body+", hash:"+prev+"\n")...)
	}
	seal = mustJSON(closeSeal{uint64(len(bs)), prev, digest(journal), uint64(len(bs))})
	return
}
func TestNestedProjectionAndReplay(t *testing.T) {
	l, j, s := fixture(true)
	r, e := Import(binding, l, j, s)
	if e != nil || r.Status != "verified" || len(r.Events) != 4 {
		t.Fatalf("unexpected status %s %v", r.Status, e)
	}
	if r.Events[2].CallID != 2 || r.Events[2].ParentSpanID == nil || *r.Events[2].ParentSpanID != r.Events[0].SpanID || r.Events[2].Level != "ERROR" || r.Events[3].Level != "INFO" {
		t.Fatal("lost nested order/result classification")
	}
	encoded := mustJSON(r)
	for _, secret := range []string{"NEVER_EMIT_SECRET", "root(input:", "https://private"} {
		if bytes.Contains(encoded, []byte(secret)) {
			t.Fatal("payload leaked")
		}
	}
	r2, e := Import(binding, l, j, s)
	if e != nil || !bytes.Equal(encoded, mustJSON(r2)) {
		t.Fatal("replay not deterministic")
	}
	other := binding
	other.ProjectID = "other"
	r3, _ := Import(other, l, j, s)
	if r3.Events[0].EventID == r.Events[0].EventID {
		t.Fatal("cross-project event collision")
	}
	if r.Artifacts[0].SHA256 != digest(l) || r.Artifacts[1].SHA256 != digest(j) || !r.Artifacts[2].Restricted {
		t.Fatal("artifact manifest lost")
	}
}
func TestEmptySealedSession(t *testing.T) {
	empty := []byte{}
	s := mustJSON(closeSeal{0, "", digest(empty), 0})
	r, e := Import(binding, empty, empty, s)
	if e != nil || r.Status != "verified" || len(r.Events) != 0 {
		t.Fatal(r.Status, e)
	}
}
func TestIncompleteIsDeterministicAndNotSuccess(t *testing.T) {
	l, j, _ := fixture(true)
	firstEnd := bytes.IndexByte(l, '\n') + 1
	l = l[:firstEnd]
	r, e := Import(binding, l, j, nil)
	if e == nil || r.Status != "incomplete" {
		t.Fatal("incomplete accepted")
	}
	if len(r.Events) != 4 || r.Events[3].Level != "WARN" {
		t.Fatal("missing incomplete warning")
	}
	for range 10 {
		again, _ := Import(binding, l, j, nil)
		if !bytes.Equal(mustJSON(r), mustJSON(again)) {
			t.Fatal("unstable report")
		}
	}
	r, e = Import(binding, nil, j, nil)
	if e == nil || r.Status != "incomplete" {
		t.Fatal("missing artifacts accepted")
	}
}
func TestCorruptionRejected(t *testing.T) {
	l, j, s := fixture(false)
	cases := map[string][3][]byte{
		"tamper":               {bytes.Replace(l, []byte("1.2ms"), []byte("1.3ms"), 1), j, s},
		"corrupt_missing_seal": {bytes.Replace(l, []byte("1.2ms"), []byte("1.3ms"), 1), j, nil},
		"truncated_log":        {l[:len(l)-1], j, s}, "truncated_journal": {l, j[:len(j)-1], s},
		"mismatched_seal":   {l, j, mustJSON(closeSeal{1, strings.Repeat("a", 64), digest(j), 1})},
		"unknown_seal":      {l, j, []byte(`{"Records":1,"LastHash":"","JournalHash":"","Calls":1,"extra":0}`)},
		"duplicate_journal": {l, append(append([]byte{}, j...), j...), s},
		"journal_parent":    {l, bytes.Replace(j, []byte(`"parent_call_id":0`), []byte(`"parent_call_id":1`), 1), s},
		"missing_function":  {l, []byte(`{"call_id":1,"parent_call_id":0,"input":null,"time":"2026-11-07T00:00:00Z"}` + "\n"), s},
		"invalid_utf8":      {l, append([]byte{0xff}, j...), s},
	}
	for name, v := range cases {
		t.Run(name, func(t *testing.T) {
			r, e := Import(binding, v[0], v[1], v[2])
			if e == nil || r.Status != "invalid" || len(r.Events) != 0 {
				t.Fatal("corruption published", r.Status, e)
			}
		})
	}
}
func TestDuplicateAndMissingSealKeys(t *testing.T) {
	empty := []byte{}
	for _, s := range []string{
		fmt.Sprintf(`{"Records":999,"Records":0,"LastHash":"","JournalHash":"%s","Calls":0}`, digest(empty)),
		fmt.Sprintf(`{"JournalHash":"%s"}`, digest(empty)),
		fmt.Sprintf(`{"records":0,"LastHash":"","JournalHash":"%s","Calls":0}`, digest(empty)),
	} {
		r, e := Import(binding, empty, empty, []byte(s))
		if e == nil || r.Status != "invalid" {
			t.Fatal("ambiguous seal accepted")
		}
	}
}
func TestMalformedRehashedBodyCannotPanic(t *testing.T) {
	_, j, _ := fixture(false)
	for _, prefix := range []string{", func:", "x, func:", "agent:test, func:"} {
		body := prefix + ", token:?, response_time:1ms, time:2026-11-07T00:00:00Z, effective_time:?, session:s, seq:1, call_id:1, parent_call_id:0, phase:END, prev_hash:"
		l := []byte("[DEBUG] " + body + ", hash:" + digest([]byte(body)) + "\n")
		r, e := Import(binding, l, j, nil)
		if e == nil || r.Status != "invalid" {
			t.Fatal("malformed record accepted")
		}
	}
}
func TestLimitsAndBinding(t *testing.T) {
	l, j, s := fixture(false)
	for _, raw := range [][]byte{bytes.Repeat([]byte("\n"), MaxCalls+1), bytes.Repeat([]byte("x"), MaxArtifactBytes+1)} {
		r, e := Import(binding, raw, j, s)
		if e == nil || r.Status != "invalid" {
			t.Fatal("limit accepted")
		}
	}
	r, e := Import(Binding{"", "op", "source"}, l, j, s)
	if e == nil || r.Status != "invalid" {
		t.Fatal("empty binding accepted")
	}
}
func TestStrictJSONRejectsNestedDuplicateKeys(t *testing.T) {
	for _, v := range []string{`{"a":{"x":1,"x":2}}`, `{"a":[{"x":1,"x":2}]}`, `{} {}`, `{"a":1,}`} {
		if uniqueJSON([]byte(v)) == nil {
			t.Fatal("invalid JSON accepted")
		}
	}
}

func TestInvalidUTF8WithValidHashCannotVerify(t *testing.T) {
	l, j, _ := fixture(false)
	content := string(l[len("[DEBUG] "):])
	at := strings.LastIndex(content, ", hash:")
	body := content[:at]
	body = strings.Replace(body, "NEVER_EMIT_SECRET", string([]byte{0xff}), 1)
	h := digest([]byte(body))
	bad := []byte("[DEBUG] " + body + ", hash:" + h + "\n")
	seal := mustJSON(closeSeal{1, h, digest(j), 1})
	r, e := Import(binding, bad, j, seal)
	if e == nil || r.Status != "invalid" || len(r.Events) != 0 {
		t.Fatal("invalid UTF8 verified")
	}
}
func TestProducerSessionIdentityAndRecordIdentity(t *testing.T) {
	l, j, s := fixture(false)
	r, e := Import(binding, l, j, s)
	if e != nil || r.ProducerSessionSHA256 == nil || *r.ProducerSessionSHA256 != digest([]byte("test_2026")) {
		t.Fatal("producer identity lost")
	}
	otherBinding := binding
	otherBinding.OperationID = "another-import"
	again, _ := Import(otherBinding, l, j, s)
	if again.Events[0].EventID != r.Events[0].EventID {
		t.Fatal("operation retry changed event identity")
	}
	content := string(l[len("[DEBUG] "):])
	at := strings.LastIndex(content, ", hash:")
	body := strings.Replace(content[:at], "1.2ms", "1.3ms", 1)
	h := digest([]byte(body))
	changed := []byte("[DEBUG] " + body + ", hash:" + h + "\n")
	seal := mustJSON(closeSeal{1, h, digest(j), 1})
	next, e := Import(binding, changed, j, seal)
	if e != nil || next.Events[1].EventID == r.Events[1].EventID || next.Events[1].SpanID != r.Events[1].SpanID {
		t.Fatal("record revision identity wrong")
	}
}
