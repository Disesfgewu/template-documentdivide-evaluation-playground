// Command lab-worker is the LAB workflow worker.
//
// This is the #3 skeleton: settings validation and a liveness endpoint.
// The agentKit graph, PostgresGraphStore and job execution are delivered by
// later Issues (#6); nothing here claims to run workflows yet.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Disesfgewu/template-documentdivide-evaluation-playground/services/worker/internal/config"
	"github.com/Disesfgewu/template-documentdivide-evaluation-playground/services/worker/internal/health"
)

const usage = "usage: lab-worker check-config|serve"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Getenv, os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, getenv func(string) string, stdout, stderr io.Writer) int {
	if len(args) != 1 || (args[0] != "check-config" && args[0] != "serve") {
		fmt.Fprintln(stderr, usage)
		return 2
	}
	cfg, err := config.Load(getenv)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	if args[0] == "check-config" {
		fmt.Fprintf(stdout, "config ok: env=%s provider_mode=%s\n", cfg.Env, cfg.ProviderMode)
		return 0
	}
	ln, err := net.Listen("tcp", cfg.HTTPAddr)
	if err != nil {
		fmt.Fprintln(stderr, "listen:", err)
		return 1
	}
	if err := serve(ctx, ln, stdout); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

// serve runs the health server on ln until ctx is cancelled, then shuts
// down gracefully.
func serve(ctx context.Context, ln net.Listener, stdout io.Writer) error {
	srv := &http.Server{Handler: health.Handler(), ReadHeaderTimeout: 5 * time.Second}
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	fmt.Fprintf(stdout, "lab-worker listening on %s\n", ln.Addr())

	select {
	case err := <-errc:
		return fmt.Errorf("server: %w", err)
	case <-ctx.Done():
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		return fmt.Errorf("shutdown: %w", err)
	}
	if err := <-errc; !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("server: %w", err)
	}
	return nil
}
