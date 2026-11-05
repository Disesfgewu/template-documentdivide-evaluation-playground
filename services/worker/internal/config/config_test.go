package config

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

const secretURL = "postgresql://lab:s3cret-pw@db.internal:5432/lab"

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func valid() map[string]string {
	return map[string]string{
		"LAB_ENV":           "dev",
		"LAB_PROVIDER_MODE": "mock",
		"LAB_DATABASE_URL":  secretURL,
	}
}

func TestLoadValid(t *testing.T) {
	c, err := Load(env(valid()))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.Env != EnvDev || c.ProviderMode != ProviderMock || c.HTTPAddr != DefaultHTTPAddr {
		t.Fatalf("unexpected config: %v", c)
	}
	if c.DatabaseURL() != secretURL {
		t.Fatalf("DatabaseURL not preserved")
	}
}

func TestLoadCustomHTTPAddr(t *testing.T) {
	m := valid()
	m["LAB_WORKER_HTTP_ADDR"] = "0.0.0.0:9090"
	c, err := Load(env(m))
	if err != nil || c.HTTPAddr != "0.0.0.0:9090" {
		t.Fatalf("got %v, %v", c, err)
	}
}

func TestLoadRejects(t *testing.T) {
	cases := []struct {
		name, key, value, want string
	}{
		{"missing env", "LAB_ENV", "", "LAB_ENV: required"},
		{"bad env", "LAB_ENV", "staging", "LAB_ENV: must be one of"},
		{"missing provider", "LAB_PROVIDER_MODE", "", "LAB_PROVIDER_MODE: required"},
		{"bad provider", "LAB_PROVIDER_MODE", "fake", "LAB_PROVIDER_MODE: must be one of"},
		{"real provider refused", "LAB_PROVIDER_MODE", "real", "T84 pending"},
		{"prod with mock", "LAB_ENV", "prod", "env=prod must not run with provider_mode=mock"},
		{"missing db", "LAB_DATABASE_URL", "", "LAB_DATABASE_URL: required"},
		{"sqlite db", "LAB_DATABASE_URL", "sqlite:///tmp/lab.db", "must be a postgresql:// URL"},
		{"db without name", "LAB_DATABASE_URL", "postgresql://lab:pw@db.internal:5432/", "must include host and database name"},
		{"db without host", "LAB_DATABASE_URL", "postgresql:///lab", "must include host and database name"},
		{"unparsable db", "LAB_DATABASE_URL", "postgresql://lab:pw@[::1/lab", "must be a valid URL"},
		{"bad http addr", "LAB_WORKER_HTTP_ADDR", "8081", "LAB_WORKER_HTTP_ADDR: must be host:port"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := valid()
			m[tc.key] = tc.value
			_, err := Load(env(m))
			var cfgErr *Error
			if !errors.As(err, &cfgErr) {
				t.Fatalf("want *Error, got %v", err)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not contain %q", err, tc.want)
			}
			if strings.Contains(err.Error(), "pw@") || strings.Contains(err.Error(), "s3cret") {
				t.Fatalf("error leaks credentials: %q", err)
			}
		})
	}
}

func TestLoadReportsAllProblems(t *testing.T) {
	_, err := Load(env(map[string]string{}))
	var cfgErr *Error
	if !errors.As(err, &cfgErr) || len(cfgErr.Problems) != 3 {
		t.Fatalf("want 3 problems, got %v", err)
	}
}

func TestConfigNeverPrintsDatabaseURL(t *testing.T) {
	c, err := Load(env(valid()))
	if err != nil {
		t.Fatal(err)
	}
	for _, verb := range []string{"%v", "%+v", "%#v", "%s"} {
		if out := fmt.Sprintf(verb, c); strings.Contains(out, "s3cret") {
			t.Fatalf("%s leaks credentials: %q", verb, out)
		}
	}
}
