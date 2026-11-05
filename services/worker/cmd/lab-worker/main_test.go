package main

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

var validEnv = map[string]string{
	"LAB_ENV":           "dev",
	"LAB_PROVIDER_MODE": "mock",
	"LAB_DATABASE_URL":  "postgresql://lab:s3cret-pw@db.internal:5432/lab",
}

func TestUsage(t *testing.T) {
	for _, args := range [][]string{nil, {"bogus"}, {"serve", "extra"}} {
		var out, errOut bytes.Buffer
		if code := run(context.Background(), args, env(validEnv), &out, &errOut); code != 2 {
			t.Fatalf("args %v: exit %d", args, code)
		}
		if !strings.Contains(errOut.String(), "usage:") {
			t.Fatalf("args %v: stderr %q", args, errOut.String())
		}
	}
}

func TestCheckConfigOK(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run(context.Background(), []string{"check-config"}, env(validEnv), &out, &errOut); code != 0 {
		t.Fatalf("exit %d, stderr %q", code, errOut.String())
	}
	if got := out.String(); got != "config ok: env=dev provider_mode=mock\n" {
		t.Fatalf("stdout %q", got)
	}
}

func TestInvalidConfigFailsWithoutLeaking(t *testing.T) {
	m := map[string]string{"LAB_ENV": "dev", "LAB_PROVIDER_MODE": "real", "LAB_DATABASE_URL": validEnv["LAB_DATABASE_URL"]}
	for _, cmd := range []string{"check-config", "serve"} {
		var out, errOut bytes.Buffer
		if code := run(context.Background(), []string{cmd}, env(m), &out, &errOut); code != 1 {
			t.Fatalf("%s: exit %d", cmd, code)
		}
		if s := errOut.String(); !strings.Contains(s, "T84 pending") || strings.Contains(s, "s3cret") {
			t.Fatalf("%s: stderr %q", cmd, s)
		}
	}
}

func TestServeHealthzAndShutdown(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- serve(ctx, ln, io.Discard) }()

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get("http://" + ln.Addr().String() + "/healthz")
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("serve returned %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("serve did not shut down")
	}
}

func TestServeListenFailure(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	m := map[string]string{}
	for k, v := range validEnv {
		m[k] = v
	}
	m["LAB_WORKER_HTTP_ADDR"] = ln.Addr().String() // already in use
	var out, errOut bytes.Buffer
	if code := run(context.Background(), []string{"serve"}, env(m), &out, &errOut); code != 1 {
		t.Fatalf("exit %d", code)
	}
	if !strings.Contains(errOut.String(), "listen:") {
		t.Fatalf("stderr %q", errOut.String())
	}
}
