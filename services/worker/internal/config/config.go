// Package config loads and validates LAB worker settings from LAB_*
// environment variables, using the same rules as the LAB API.
package config

import (
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
)

// Environment is the deployment environment.
type Environment string

// Allowed environments.
const (
	EnvDev  Environment = "dev"
	EnvTest Environment = "test"
	EnvProd Environment = "prod"
)

// ProviderMode selects deterministic mocks or real external providers.
type ProviderMode string

// Allowed provider modes.
const (
	ProviderMock ProviderMode = "mock"
	ProviderReal ProviderMode = "real"
)

// DefaultHTTPAddr is the loopback address for the worker health endpoint.
const DefaultHTTPAddr = "127.0.0.1:8081"

// Config is the validated worker configuration. The database URL is
// unexported so that printing a Config can never reveal credentials.
type Config struct {
	Env          Environment
	ProviderMode ProviderMode
	HTTPAddr     string
	databaseURL  string
}

// DatabaseURL returns the PostgreSQL connection URL.
func (c Config) DatabaseURL() string { return c.databaseURL }

// String never includes the database URL.
func (c Config) String() string {
	return fmt.Sprintf("Config{Env:%s ProviderMode:%s HTTPAddr:%s DatabaseURL:<redacted>}",
		c.Env, c.ProviderMode, c.HTTPAddr)
}

// GoString makes %#v redact the database URL as well.
func (c Config) GoString() string { return c.String() }

// Error lists invalid setting names with reasons. It never contains values.
type Error struct {
	Problems []string
}

func (e *Error) Error() string {
	return "invalid worker settings: " + strings.Join(e.Problems, "; ")
}

// Load reads and validates settings. getenv is usually os.Getenv.
func Load(getenv func(string) string) (Config, error) {
	var problems []string
	add := func(name, reason string) { problems = append(problems, name+": "+reason) }
	var c Config

	switch v := Environment(getenv("LAB_ENV")); v {
	case EnvDev, EnvTest, EnvProd:
		c.Env = v
	case "":
		add("LAB_ENV", "required")
	default:
		add("LAB_ENV", "must be one of dev, test, prod")
	}

	switch v := ProviderMode(getenv("LAB_PROVIDER_MODE")); v {
	case ProviderMock:
		c.ProviderMode = v
	case ProviderReal:
		// No real provider adapter exists yet (T84 pending); refuse instead
		// of silently falling back to mocks.
		add("LAB_PROVIDER_MODE", "real is not available: no real provider adapter is implemented yet (T84 pending)")
	case "":
		add("LAB_PROVIDER_MODE", "required")
	default:
		add("LAB_PROVIDER_MODE", "must be one of mock, real")
	}
	if c.Env == EnvProd && c.ProviderMode == ProviderMock {
		add("LAB_PROVIDER_MODE", "env=prod must not run with provider_mode=mock")
	}

	if raw := getenv("LAB_DATABASE_URL"); raw == "" {
		add("LAB_DATABASE_URL", "required")
	} else if reason := checkPostgresURL(raw); reason != "" {
		add("LAB_DATABASE_URL", reason)
	} else {
		c.databaseURL = raw
	}

	c.HTTPAddr = getenv("LAB_WORKER_HTTP_ADDR")
	if c.HTTPAddr == "" {
		c.HTTPAddr = DefaultHTTPAddr
	} else if _, port, err := net.SplitHostPort(c.HTTPAddr); err != nil || !validPort(port) {
		add("LAB_WORKER_HTTP_ADDR", "must be host:port with port 1-65535")
	}

	if len(problems) > 0 {
		return Config{}, &Error{Problems: problems}
	}
	return c, nil
}

// checkPostgresURL returns a value-free reason when raw is not usable.
// url.Parse errors are not propagated because they quote the input.
func checkPostgresURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "must be a valid URL"
	}
	if p := u.Port(); p != "" && !validPort(p) {
		return "port must be 1-65535"
	}
	if u.Scheme != "postgresql" && u.Scheme != "postgres" {
		// SQLite or other stores must not stand in for PostgreSQL (SPEC §16).
		return "must be a postgresql:// URL"
	}
	if u.Hostname() == "" || strings.Trim(u.Path, "/") == "" {
		return "must include host and database name"
	}
	return ""
}

// validPort reports whether p is a decimal TCP port in 1..65535.
func validPort(p string) bool {
	n, err := strconv.Atoi(p)
	return err == nil && n >= 1 && n <= 65535 && strconv.Itoa(n) == p
}
