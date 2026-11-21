//go:build linux

package hostexec

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// processGone reports whether pid is truly dead: either reaped+unlinked
// (ESRCH) or a zombie (state Z in /proc/<pid>/stat). A killed grandchild
// whose immediate parent (sh) also died in the same group kill can linger
// as a zombie with no one to reap it, where kill(pid, 0) still succeeds —
// mirrors executor.nativeProcessGone (internal/executor/native_backend_unix_test.go).
func processGone(pid int) bool {
	if err := syscall.Kill(pid, 0); errors.Is(err, syscall.ESRCH) {
		return true
	}
	if stat, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat"); err == nil {
		if idx := strings.LastIndexByte(string(stat), ')'); idx >= 0 && len(stat) > idx+2 && stat[idx+2] == 'Z' {
			return true
		}
	}
	return false
}

func TestRun_EchoExitZero(t *testing.T) {
	var mu sync.Mutex
	var out strings.Builder
	code, err := Run(context.Background(), Request{Argv: []string{"echo", "hello"}}, func(f Frame) {
		mu.Lock()
		defer mu.Unlock()
		if f.Stream == "stdout" {
			out.Write(f.Data)
		}
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	if got := out.String(); got != "hello\n" {
		t.Fatalf("stdout = %q, want %q", got, "hello\n")
	}
}

func TestRun_FalseExitOne(t *testing.T) {
	code, err := Run(context.Background(), Request{Argv: []string{"false"}}, nil)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
}

// TestRun_TimeoutKillsProcessGroup is the pgid test: the real target of a
// SIGTERM-on-timeout is the backgrounded `sleep 30`, a *child* of the sh
// that Run starts directly — only killing the whole process group reaches
// it.
func TestRun_TimeoutKillsProcessGroup(t *testing.T) {
	pidFile := filepath.Join(t.TempDir(), "child.pid")
	req := Request{
		Argv:    []string{"sh", "-c", fmt.Sprintf("sleep 30 & echo $! > %s; wait", pidFile)},
		Timeout: 300 * time.Millisecond,
	}

	start := time.Now()
	code, err := Run(context.Background(), req, nil)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if elapsed > 5*time.Second {
		t.Fatalf("Run took %v, want well under the 5s kill grace", elapsed)
	}
	if code == 0 {
		t.Fatalf("exit code = 0, want non-zero (killed by timeout)")
	}

	data, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("reading child pid file: %v", err)
	}
	childPID, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		t.Fatalf("parsing child pid: %v", err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for !processGone(childPID) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !processGone(childPID) {
		t.Fatalf("backgrounded child pid %d still alive after group kill", childPID)
	}
}

func TestRun_OutputCapDropsExcessBytes(t *testing.T) {
	orig := MaxTotalOutputBytes
	MaxTotalOutputBytes = 64
	defer func() { MaxTotalOutputBytes = orig }()

	var mu sync.Mutex
	var total int
	code, err := Run(context.Background(), Request{
		Argv: []string{"sh", "-c", "yes x | head -c 4096"},
	}, func(f Frame) {
		mu.Lock()
		defer mu.Unlock()
		total += len(f.Data)
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	if int64(total) > MaxTotalOutputBytes+MaxFrameBytes {
		t.Fatalf("emitted %d bytes, want roughly capped near %d", total, MaxTotalOutputBytes)
	}
}

// TestRun_ScrubsBearerTokenInEmittedOutput is the hx.8b wire test: a host
// process that prints a credential must never have it reach emit — the frame
// is scrubbed (scrub.go ScrubFrame) before it leaves the runner.
func TestRun_ScrubsBearerTokenInEmittedOutput(t *testing.T) {
	var mu sync.Mutex
	var out strings.Builder
	code, err := Run(context.Background(), Request{
		Argv: []string{"sh", "-c", "echo \"Authorization: Bearer abc.def\""},
	}, func(f Frame) {
		mu.Lock()
		defer mu.Unlock()
		out.Write(f.Data)
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	got := out.String()
	if strings.Contains(got, "abc.def") {
		t.Fatalf("emitted stdout leaks the token: %q", got)
	}
	if !strings.Contains(got, "<REDACTED>") {
		t.Fatalf("emitted stdout = %q, want the token replaced by <REDACTED>", got)
	}
}

// TestRun_PEMSplitAcrossWrites is the cross-frame PEM test: the child writes
// the BEGIN line, pauses, then writes the body + END. ScrubFrame alone is
// stateless, so the second frame (which starts mid-key) would pass through
// untouched; the writer's inPEM state must swallow it.
func TestRun_PEMSplitAcrossWrites(t *testing.T) {
	var mu sync.Mutex
	var out strings.Builder
	// Two separate writes with a sleep between them: the pipe hands each to
	// the chunkedWriter as its own frame.
	prog := "printf '%s\\n' '-----BEGIN PRIVATE KEY-----'; sleep 0.2; " +
		"printf '%s\\n' 'U0VQTVTR0LQVUOVT=' '-----END PRIVATE KEY-----'"
	code, err := Run(context.Background(), Request{
		Argv: []string{"sh", "-c", prog},
	}, func(f Frame) {
		mu.Lock()
		defer mu.Unlock()
		out.Write(f.Data)
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	got := out.String()
	if strings.Contains(got, "U0VQTVTR0") {
		t.Fatalf("PEM key body leaked across the frame boundary: %q", got)
	}
	if !strings.Contains(got, pemKeyPlaceholder) {
		t.Fatalf("emitted stdout = %q, want the single %s marker and nothing else", got, pemKeyPlaceholder)
	}
}

// TestRun_CleanOutputPassesThroughByteIdentical: scrubbing must not touch
// bytes that match no rule (no synthesised newlines, no re-wrapping).
func TestRun_CleanOutputPassesThroughByteIdentical(t *testing.T) {
	var mu sync.Mutex
	var out strings.Builder
	want := "alpha\nbravo charlie\n"
	_, err := Run(context.Background(), Request{
		Argv: []string{"printf", "alpha\\nbravo charlie\\n"},
	}, func(f Frame) {
		mu.Lock()
		defer mu.Unlock()
		out.Write(f.Data)
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if got := out.String(); got != want {
		t.Fatalf("emitted stdout = %q, want byte-identical %q", got, want)
	}
}

// TestRun_CapCountsScrubbedBytes pins the documented cap semantics: MaxTotalOutputBytes
// charges the SCRUBBED bytes that are emitted, so a run that scrubs heavily
// keeps relaying after its raw byte count has crossed the cap. With a 20000
// cap and ~3:1 scrubbing, charging raw bytes would stop near 7000 emitted.
func TestRun_CapCountsScrubbedBytes(t *testing.T) {
	orig := MaxTotalOutputBytes
	MaxTotalOutputBytes = 20000
	defer func() { MaxTotalOutputBytes = orig }()

	var mu sync.Mutex
	var emitted int
	_, err := Run(context.Background(), Request{
		Argv: []string{"sh", "-c",
			"i=0; while [ $i -lt 2700 ]; do echo \"secret=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"; i=$((i+1)); done"},
	}, func(f Frame) {
		mu.Lock()
		defer mu.Unlock()
		emitted += len(f.Data)
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if emitted < 15000 {
		t.Fatalf("emitted %d scrubbed bytes, want the cap charged on scrubbed bytes (near %d)", emitted, MaxTotalOutputBytes)
	}
}

// TestRun_BearerTokenSplitAcrossWrites is the non-PEM cross-frame scrub test:
// ScrubFrame's secret patterns only match a complete value, so a token split
// mid-line across two writes ("Bearer ab" then "cdef") must not leave the
// tail ("cdef") emitted raw once the line completes — chunkedWriter holds the
// unterminated partial line and reassembles it before scrubbing.
func TestRun_BearerTokenSplitAcrossWrites(t *testing.T) {
	var mu sync.Mutex
	var out strings.Builder
	prog := "printf '%s' 'Authorization: Bearer ab'; sleep 0.2; printf '%s\\n' 'cdef'"
	code, err := Run(context.Background(), Request{
		Argv: []string{"sh", "-c", prog},
	}, func(f Frame) {
		mu.Lock()
		defer mu.Unlock()
		out.Write(f.Data)
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	got := out.String()
	if strings.Contains(got, "abcdef") || strings.Contains(got, "cdef") {
		t.Fatalf("token tail leaked across the write boundary: %q", got)
	}
	if !strings.Contains(got, "<REDACTED>") {
		t.Fatalf("emitted stdout = %q, want the reassembled token replaced by <REDACTED>", got)
	}
}

// TestRun_CtxCancelKillsProcessGroup is the ctx-cancellation half of the group
// kill (hx.11): task cancel / worker shutdown cancels the ctx WITHOUT any
// req.Timeout firing, and the whole group — including the backgrounded
// `sleep 30` grandchild — must still die. req.Timeout (60s) is far longer than
// every bound this test enforces, so the only thing that can end the run is
// cancel(): a runner that only honoured req.Timeout would sit in cmd.Wait.
func TestRun_CtxCancelKillsProcessGroup(t *testing.T) {
	pidFile := filepath.Join(t.TempDir(), "child.pid")
	req := Request{
		Argv:    []string{"sh", "-c", fmt.Sprintf("sleep 30 & echo $! > %s; wait", pidFile)},
		Timeout: 60 * time.Second,
	}

	outer, cancel := context.WithCancel(context.Background())
	type runResult struct {
		code int
		err  error
	}
	ran := make(chan runResult, 1)
	go func() {
		code, err := Run(outer, req, nil)
		ran <- runResult{code: code, err: err}
	}()

	// Wait until the shell has forked its background child and recorded its
	// pid: cancelling before that would kill a run that has no grandchild yet.
	var readErr error
	appearDeadline := time.Now().Add(10 * time.Second)
	for {
		if _, readErr = os.ReadFile(pidFile); readErr == nil {
			break
		}
		if time.Now().After(appearDeadline) {
			t.Fatalf("child pid file never appeared: %v", readErr)
		}
		time.Sleep(20 * time.Millisecond)
	}

	cancel()
	start := time.Now()
	// Run must come back on its own after the cancel — a runner that only
	// honoured req.Timeout would still be sitting in cmd.Wait for ~60s.
	got := make(chan runResult, 1)
	select {
	case r := <-ran:
		got <- r
	case <-time.After(5 * time.Second):
		t.Fatal("Run still unreturned 5s after ctx cancel (req.Timeout was 60s)")
	}
	r := <-got
	elapsed := time.Since(start)
	if elapsed > 5*time.Second {
		t.Fatalf("Run took %v after cancel, want well under the 5s kill grace", elapsed)
	}
	if r.err != nil {
		t.Fatalf("Run: %v", r.err)
	}
	if r.code == 0 {
		t.Fatalf("exit code = 0, want non-zero (killed by ctx cancel)")
	}

	data, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("reading child pid file: %v", err)
	}
	// The pid file is written by the backgrounded child's own echo, so it can
	// land while the file is still being truncated-and-filled; retry parse.
	childPID := 0
	for attempt := 0; attempt < 50; attempt++ {
		var perr error
		childPID, perr = strconv.Atoi(strings.TrimSpace(string(data)))
		if perr == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
		if d, e := os.ReadFile(pidFile); e == nil {
			data = d
		}
	}
	if childPID == 0 {
		t.Fatalf("parsing child pid: %v", strconv.ErrSyntax)
	}
	deadline := time.Now().Add(3 * time.Second)
	for !processGone(childPID) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !processGone(childPID) {
		t.Fatalf("backgrounded child pid %d still alive after ctx-cancel group kill", childPID)
	}
}
