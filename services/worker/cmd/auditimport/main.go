// auditimport emits a private, metadata-only report from restricted agentlog files.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"

	"github.com/Disesfgewu/template-documentdivide-evaluation-playground/services/worker/internal/auditimport"
)

func readArtifact(path string, optional bool) ([]byte, error) {
	// Nonblocking open prevents a FIFO from hanging before the regular-file check.
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NONBLOCK|syscall.O_NOFOLLOW, 0)
	if err != nil {
		if optional && errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, errors.New("artifact_read_failed")
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return nil, errors.New("artifact_not_regular")
	}
	raw, err := io.ReadAll(io.LimitReader(f, auditimport.MaxArtifactBytes+1))
	if err != nil {
		return nil, errors.New("artifact_read_failed")
	}
	if len(raw) > auditimport.MaxArtifactBytes {
		return nil, errors.New("artifact_size_limit")
	}
	if raw == nil {
		raw = []byte{}
	}
	return raw, nil
}
func canonicalPath(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	if parent, err := filepath.EvalSymlinks(filepath.Dir(absolute)); err == nil {
		absolute = filepath.Join(parent, filepath.Base(absolute))
	}
	return absolute, nil
}
func run(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("auditimport", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var path, out, project, operation, source string
	fs.StringVar(&path, "log", "", "restricted .log path (companions use .journal/.seal)")
	fs.StringVar(&out, "out", "", "new report path; existing files are refused")
	fs.StringVar(&project, "project", "", "trusted project ID")
	fs.StringVar(&operation, "operation", "", "trusted operation ID")
	fs.StringVar(&source, "source", "", "trusted stable source bundle ID")
	if fs.Parse(args) != nil || fs.NArg() != 0 || path == "" || out == "" {
		fmt.Fprintln(stderr, "usage: auditimport -log PATH -out NEW_PATH -project ID -operation ID -source ID")
		return 2
	}
	outputPath, err := canonicalPath(out)
	if err != nil {
		fmt.Fprintln(stderr, "audit import: invalid output path")
		return 2
	}
	for _, sourcePath := range []string{path, path + ".journal", path + ".seal"} {
		sourcePath, err = canonicalPath(sourcePath)
		if err != nil || sourcePath == outputPath {
			fmt.Fprintln(stderr, "audit import: output conflicts with source artifact")
			return 2
		}
	}
	log, err := readArtifact(path, true)
	if err != nil {
		fmt.Fprintln(stderr, "audit import: artifact read failed or limit exceeded")
		return 2
	}
	journal, err := readArtifact(path+".journal", true)
	if err != nil {
		fmt.Fprintln(stderr, "audit import: artifact read failed or limit exceeded")
		return 2
	}
	seal, err := readArtifact(path+".seal", true)
	if err != nil {
		fmt.Fprintln(stderr, "audit import: artifact read failed or limit exceeded")
		return 2
	}
	report, importErr := auditimport.Import(auditimport.Binding{ProjectID: project, OperationID: operation, SourceID: source}, log, journal, seal)
	raw, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		fmt.Fprintln(stderr, "audit import: encoding failed")
		return 2
	}
	raw = append(raw, '\n')
	// O_EXCL refuses symlinks and existing paths, including any raw source file.
	f, err := os.OpenFile(out, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		fmt.Fprintln(stderr, "audit import: report creation failed")
		return 2
	}
	if _, err = f.Write(raw); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil || closeErr != nil {
		os.Remove(out)
		fmt.Fprintln(stderr, "audit import: report write failed")
		return 2
	}
	if importErr != nil {
		fmt.Fprintln(stderr, "audit import:", report.Status)
		return 1
	}
	return 0
}
func main() { os.Exit(run(os.Args[1:], os.Stderr)) }
