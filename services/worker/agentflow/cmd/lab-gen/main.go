// lab-gen: one bounded batch of LLM calls for question generation (P-D slice 1, Refs #7).
// JSON Input on stdin, JSON Output on stdout, nothing else on stdout; the key is read from LAB_GEN_API_KEY only.
// Exit code 0: output written (it may say refused or partial); 2: unreadable input. The audit log (agentKit hash chain,
// local to the machine) goes to the directory in LAB_GEN_AUDIT_DIR; it holds requests, so it is never published.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/Disesfgewu/agentkit/agentlog"

	"github.com/Disesfgewu/template-documentdivide-evaluation-playground/services/worker/agentflow/labgen"
)

const maxInput = 16 << 20

func main() { os.Exit(run(os.Stdin, os.Stdout, os.Stderr, os.Getenv("LAB_GEN_AUDIT_DIR"))) }

func run(stdin io.Reader, stdout, stderr io.Writer, auditDir string) int {
	data, err := io.ReadAll(io.LimitReader(stdin, maxInput+1))
	if err != nil || len(data) > maxInput {
		fmt.Fprintln(stderr, "lab-gen: input unreadable or too large")
		return 2
	}
	var in labgen.Input
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		fmt.Fprintln(stderr, "lab-gen: input is not valid JSON for this schema")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if auditDir != "" {
		rec, err := agentlog.New("lab-gen", auditDir)
		if err != nil {
			fmt.Fprintln(stderr, "lab-gen: audit log unavailable")
			return 2
		}
		defer rec.Close()
		ctx = agentlog.WithRecorder(ctx, rec)
	}
	out := labgen.Run(ctx, in, labgen.Options{})
	if err := json.NewEncoder(stdout).Encode(out); err != nil {
		return 2
	}
	return 0
}
