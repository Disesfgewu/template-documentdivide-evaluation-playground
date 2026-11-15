package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestCLIIncompletePrivateReportAndNoOverwrite(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "missing.log")
	out := filepath.Join(dir, "report.json")
	args := []string{"-log", path, "-out", out, "-project", "project", "-operation", "operation", "-source", "source"}
	var stderr bytes.Buffer
	if rc := run(args, &stderr); rc != 1 {
		t.Fatal("incomplete exit", rc)
	}
	info, e := os.Stat(out)
	if e != nil || info.Mode().Perm() != 0600 {
		t.Fatal("report mode", e)
	}
	before, _ := os.ReadFile(out)
	if !bytes.Contains(before, []byte(`"status": "incomplete"`)) {
		t.Fatal("missing status")
	}
	stderr.Reset()
	if rc := run(args, &stderr); rc != 2 {
		t.Fatal("overwritten", rc)
	}
	after, _ := os.ReadFile(out)
	if !bytes.Equal(before, after) {
		t.Fatal("existing report changed")
	}
}
func TestCLIRefusesSymlinkAndHidesPaths(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "NEVER_EMIT_SECRET")
	os.WriteFile(target, []byte("original"), 0600)
	link := filepath.Join(dir, "link")
	if e := os.Symlink(target, link); e != nil {
		t.Fatal(e)
	}
	var stderr bytes.Buffer
	if run([]string{"-log", filepath.Join(dir, "missing"), "-out", link, "-project", "p", "-operation", "o", "-source", "s"}, &stderr) != 2 {
		t.Fatal("symlink accepted")
	}
	raw, _ := os.ReadFile(target)
	if string(raw) != "original" || strings.Contains(stderr.String(), dir) {
		t.Fatal("raw overwritten or path leaked")
	}
}
func TestReadLimitAndUsage(t *testing.T) {
	var stderr bytes.Buffer
	if run([]string{"-unknown=NEVER_EMIT_SECRET"}, &stderr) != 2 || strings.Contains(stderr.String(), "NEVER_EMIT_SECRET") {
		t.Fatal("flag leak")
	}
	dir := t.TempDir()
	if _, e := readArtifact(dir, false); e == nil {
		t.Fatal("directory read accepted")
	}
}

func TestFIFORejectedWithoutBlocking(t *testing.T) {
	path := filepath.Join(t.TempDir(), "blocked.log")
	if err := syscall.Mkfifo(path, 0600); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := readArtifact(path, false); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("FIFO accepted")
		}
	case <-time.After(time.Second):
		// Unblock the opener so a failing regression does not leave a leaked reader.
		go func() {
			f, err := os.OpenFile(path, os.O_WRONLY|syscall.O_NONBLOCK, 0)
			if err == nil {
				f.Close()
			}
		}()
		t.Fatal("FIFO blocked before file type validation")
	}
}

func TestMissingSourcePathsCannotBecomeReports(t *testing.T) {
	for _, suffix := range []string{"", ".journal", ".seal"} {
		t.Run("artifact"+suffix, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "missing.log")
			out := path + suffix
			var stderr bytes.Buffer
			if rc := run([]string{"-log", path, "-out", out, "-project", "p", "-operation", "o", "-source", "s"}, &stderr); rc != 2 {
				t.Fatalf("source poisoned: %d", rc)
			}
			if _, err := os.Stat(out); !os.IsNotExist(err) {
				t.Fatal("source path created")
			}
		})
	}
}
