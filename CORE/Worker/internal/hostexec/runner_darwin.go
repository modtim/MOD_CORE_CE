//go:build darwin

// Package hostexec: macOS native direct runner (opsx worker-host-exec hx.9m).
// A macOS worker installed as a root launchd daemon has the same root-equivalent
// privilege model as a linux systemd worker, so DIRECT mode (no containerized
// helper) is supported here too. Darwin is BSD-derived, so the process-group
// (Setpgid) and credential-switch (syscall.Credential) syscalls runner.go
// already uses for linux work unchanged here — this file duplicates that
// implementation under its own build tag rather than editing runner.go (which
// is linux-only build-tagged and claimed by the linux runner brief).
package hostexec

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"
)

// MaxFrameBytes is the largest chunk of a single stream emitted in one
// Frame; larger writes are split across multiple Frames. Kept in sync with
// runner.go's linux value.
const MaxFrameBytes = 32 * 1024

// MaxTotalOutputBytes caps the combined stdout+stderr bytes emitted across
// the whole run; bytes beyond the cap are silently dropped (the child still
// runs to completion, its output is just no longer relayed). See runner.go's
// doc comment for the full rationale — identical here.
var MaxTotalOutputBytes int64 = 16 * 1024 * 1024

// killGrace is how long Run waits after sending SIGTERM to the process
// group before escalating to SIGKILL.
const killGrace = 5 * time.Second

// Request describes a host-exec invocation. Kept in sync with runner.go's
// linux definition field-for-field.
type Request struct {
	Argv    []string
	Cwd     string
	Env     map[string]string
	RunAs   string
	Timeout time.Duration
	Stdin   []byte
}

// Frame is one chunk of a run's stdout or stderr.
type Frame struct {
	Stream string
	Data   []byte
}

// Run executes req.Argv directly (exec.CommandContext, no shell — argv[0]
// only becomes a shell if the caller put one there), streaming output to
// emit in <=MaxFrameBytes chunks, and returns the process's exit code. Same
// contract as runner.go's linux Run: process-group lifecycle, scrubbed
// output, PEM-aware chunking, RunAs credential switch.
func Run(ctx context.Context, req Request, emit func(Frame)) (int, error) {
	if len(req.Argv) == 0 {
		return -1, fmt.Errorf("hostexec: empty argv")
	}
	if req.Cwd != "" {
		info, err := os.Stat(req.Cwd)
		if err != nil {
			return -1, fmt.Errorf("hostexec: cwd %q: %w", req.Cwd, err)
		}
		if !info.IsDir() {
			return -1, fmt.Errorf("hostexec: cwd %q is not a directory", req.Cwd)
		}
	}

	runCtx := ctx
	if req.Timeout > 0 {
		var cancel context.CancelFunc
		runCtx, cancel = context.WithTimeout(ctx, req.Timeout)
		defer cancel()
	}

	cmd := exec.CommandContext(runCtx, req.Argv[0], req.Argv[1:]...)
	cmd.Dir = req.Cwd
	cmd.Env = buildEnv(req.Env)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if req.RunAs != "" {
		cred, err := credentialForUser(req.RunAs)
		if err != nil {
			return -1, fmt.Errorf("hostexec: resolving RunAs %q: %w", req.RunAs, err)
		}
		cmd.SysProcAttr.Credential = cred
	}

	if len(req.Stdin) > 0 {
		cmd.Stdin = bytes.NewReader(req.Stdin)
	}

	if emit == nil {
		emit = func(Frame) {}
	}
	var total atomic.Int64
	stdoutW := &chunkedWriter{stream: "stdout", emit: emit, total: &total, workspace: req.Cwd}
	stderrW := &chunkedWriter{stream: "stderr", emit: emit, total: &total, workspace: req.Cwd}
	cmd.Stdout = stdoutW
	cmd.Stderr = stderrW

	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
	}

	if err := cmd.Start(); err != nil {
		return -1, fmt.Errorf("hostexec: starting %q: %w", req.Argv[0], err)
	}

	procDone := make(chan struct{})
	go func() {
		select {
		case <-runCtx.Done():
		case <-procDone:
			return
		}
		select {
		case <-procDone:
		case <-time.After(killGrace):
			if cmd.Process != nil {
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			}
		}
	}()

	waitErr := cmd.Wait()
	close(procDone)
	_ = stdoutW.Close()
	_ = stderrW.Close()

	if waitErr == nil {
		return 0, nil
	}
	var exitErr *exec.ExitError
	if errors.As(waitErr, &exitErr) {
		return exitErr.ExitCode(), nil
	}
	return -1, fmt.Errorf("hostexec: running %q: %w", req.Argv[0], waitErr)
}

// buildEnv is PATH/HOME/LANG from the worker's own environment (only if
// set), overlaid with req.Env. Kept in sync with runner.go's linux version.
func buildEnv(extra map[string]string) []string {
	merged := map[string]string{}
	for _, k := range []string{"PATH", "HOME", "LANG"} {
		if v := os.Getenv(k); v != "" {
			merged[k] = v
		}
	}
	for k, v := range extra {
		merged[k] = v
	}
	env := make([]string, 0, len(merged))
	for k, v := range merged {
		env = append(env, k+"="+v)
	}
	return env
}

// credentialForUser resolves a username (root allowed) to the
// syscall.Credential exec needs to drop/switch to it, including the user's
// supplementary groups. Kept in sync with runner.go's linux version.
func credentialForUser(username string) (*syscall.Credential, error) {
	u, err := user.Lookup(username)
	if err != nil {
		return nil, err
	}
	uid, err := strconv.ParseUint(u.Uid, 10, 32)
	if err != nil {
		return nil, fmt.Errorf("parsing uid %q: %w", u.Uid, err)
	}
	gid, err := strconv.ParseUint(u.Gid, 10, 32)
	if err != nil {
		return nil, fmt.Errorf("parsing gid %q: %w", u.Gid, err)
	}

	groupIDs, err := u.GroupIds()
	if err != nil {
		return nil, fmt.Errorf("hostexec: listing groups for %q: %w", username, err)
	}
	groups := make([]uint32, 0, len(groupIDs))
	for _, g := range groupIDs {
		gv, err := strconv.ParseUint(g, 10, 32)
		if err != nil {
			return nil, fmt.Errorf("parsing supplementary gid %q: %w", g, err)
		}
		groups = append(groups, uint32(gv))
	}

	return &syscall.Credential{
		Uid:    uint32(uid),
		Gid:    uint32(gid),
		Groups: groups,
	}, nil
}

// maxPendingLine bounds how many unterminated bytes chunkedWriter holds back
// waiting for a line's closing newline before scrubbing and emitting it
// anyway. See runner.go's linux version for the full rationale.
const maxPendingLine = 8 * 1024

// chunkedWriter splits writes into <=MaxFrameBytes Frames, scrubs every frame
// (hostexec ScrubFrame) before emitting it, and drops bytes once the shared
// total crosses MaxTotalOutputBytes. Kept in sync with runner.go's linux
// version field-for-field and behavior-for-behavior; duplicated here (not
// shared) because runner.go is linux-only build-tagged.
type chunkedWriter struct {
	stream    string
	workspace string
	emit      func(Frame)
	total     *atomic.Int64
	inPEM     bool
	pending   []byte
}

func (w *chunkedWriter) Write(p []byte) (int, error) {
	n := len(p)
	w.pending = append(w.pending, p...)
	var ready []byte
	switch idx := bytes.LastIndexByte(w.pending, '\n'); {
	case idx >= 0:
		ready = w.pending[:idx+1]
		w.pending = append([]byte(nil), w.pending[idx+1:]...)
	case len(w.pending) > maxPendingLine:
		ready = w.pending
		w.pending = nil
	default:
		return n, nil
	}
	w.emitChunks(ready)
	return n, nil
}

// Close flushes whatever partial line is still held in pending once the
// child's output pipe is exhausted and no further Write calls will arrive.
func (w *chunkedWriter) Close() error {
	if len(w.pending) == 0 {
		return nil
	}
	data := w.pending
	w.pending = nil
	w.emitChunks(data)
	return nil
}

// emitChunks splits data into <=MaxFrameBytes frames, scrubs and PEM-tracks
// each one, and emits it subject to the MaxTotalOutputBytes cap.
func (w *chunkedWriter) emitChunks(p []byte) {
	for len(p) > 0 {
		chunk := p
		if len(chunk) > MaxFrameBytes {
			chunk = chunk[:MaxFrameBytes]
		}
		p = p[len(chunk):]
		data := make([]byte, len(chunk))
		copy(data, chunk)

		if w.inPEM {
			tail, closed := pemTail(data)
			if !closed {
				continue
			}
			w.inPEM = false
			data = tail
			if len(data) == 0 {
				continue
			}
		}
		w.inPEM = pemOpenAfter(data)
		data = ScrubFrame(data, w.workspace)
		if len(data) == 0 {
			continue
		}
		before := w.total.Add(int64(len(data))) - int64(len(data))
		if before >= MaxTotalOutputBytes {
			continue
		}
		w.emit(Frame{Stream: w.stream, Data: data})
	}
}

// pemOpenAfter reports whether data ends inside a PEM private-key block. See
// runner.go's linux version for the full rationale; duplicated here because
// runner.go is linux-only build-tagged.
func pemOpenAfter(data []byte) bool {
	if len(data) == 0 {
		return false
	}
	inPEM := false
	for _, ln := range bytes.Split(data, []byte("\n")) {
		s := string(ln)
		if pemBeginPattern.MatchString(s) {
			inPEM = !pemEndPattern.MatchString(s)
		} else if inPEM && pemEndPattern.MatchString(s) {
			inPEM = false
		}
	}
	return inPEM
}

// pemTail returns the bytes of data that follow the END marker closing a PEM
// block that started in an earlier frame, plus whether such an END was found.
func pemTail(data []byte) ([]byte, bool) {
	lines := bytes.Split(data, []byte("\n"))
	for i, ln := range lines {
		if pemEndPattern.MatchString(string(ln)) {
			return bytes.Join(lines[i+1:], []byte("\n")), true
		}
	}
	return data, false
}
