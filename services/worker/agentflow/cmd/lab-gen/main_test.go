package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestTheBinaryRefusesALoopbackEndpointFromJSON(t *testing.T) {
	// Loopback is a test seam of the package, never reachable from the input: a JSON document cannot enable it.
	in := `{"model":{"name":"m","protocol":"openai_chat","base_url":"http://127.0.0.1:9","allowed_host":"127.0.0.1",
	"max_output_tokens":500,"input_micros_per_mtok":1,"output_micros_per_mtok":1,"currency":"USD","pricing_revision":"r",
	"timeout_seconds":5,"max_retries":1,"requests_per_minute":60},"budget":{"limit_micros":1000},
	"calls":[{"id":"a","user":"hi"}]}`
	var stdout, stderr bytes.Buffer
	if code := run(strings.NewReader(in), &stdout, &stderr, t.TempDir()); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	var out map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &out); err != nil || out["status"] != "refused" {
		t.Fatalf("%v %s", err, stdout.String())
	}
}

func TestUnknownFieldsAndGarbageAreRejectedWithExitTwo(t *testing.T) {
	for _, in := range []string{`not json`, `{"model":{},"budget":{},"calls":[],"allow_loopback":true}`} {
		var stdout, stderr bytes.Buffer
		if code := run(strings.NewReader(in), &stdout, &stderr, ""); code != 2 || stdout.Len() != 0 {
			t.Fatalf("%q: exit %d stdout %q", in, code, stdout.String())
		}
	}
}
