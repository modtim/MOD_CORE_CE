//go:build linux

// Package hostexec runs a single argv directly on the worker host (no
// container), for opsx worker-host-exec (hx.3). This file is the real,
// linux-only implementation: process-group lifecycle (Setpgid + group
// SIGTERM/SIGKILL) and RunAs user-credential lookup both rely on linux/unix
// syscalls. Every other GOOS gets the stub in runner_other.go.
//
// Not wired into main.go / task dispatch yet (that is hx.7) — this package
// is a standalone primitive today.
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
// Frame; larger writes are split across multiple Frames.
const MaxFrameBytes = 32 * 1024

// MaxTotalOutputBytes caps the combined stdout+stderr bytes emitted across
// the whole run; bytes beyond the cap are silently dropped (the child still
// runs to completion, its output is just no longer relayed). The counter
// charges the SCRUBBED bytes that are actually emitted (see chunkedWriter),
// not the child's raw bytes: a run that leaks a wall of redaction placeholders
// is charged the placeholder bytes, so the cap bounds what reaches the task
// log rather than what the child printed. Not part of Request (the brief
// fixed that struct's shape) — a package var so callers needing a different
// cap can set it, and tests can shrink it.
var MaxTotalOutputBytes int64 = 16 * 1024 * 1024

// killGrace is how long Run waits after sending SIGTERM to the process
// group before escalating to SIGKILL.
const killGrace = 5 * time.Second

// Request describes a host-exec invocation.
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
	Stream string // "stdout" | "stderr"
	Data   []byte
}

// Run executes req.Argv directly (exec.CommandContext, no shell — argv[0]
// only becomes a shell if the caller put one there), streaming output to
// emit in <=MaxFrameBytes chunks, and returns the process's exit code.
//
// The child runs in its own process group (Setpgid). On ctx cancellation or
// req.Timeout elapsing, the whole group is sent SIGTERM; if it hasn't exited
// killGrace later, the whole group is sent SIGKILL.
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
	// Every frame is scrubbed (hx.8 scrub.go) before it reaches emit: the host
	// process writes its stdout/stderr straight into the task-log outbox, so
	// nothing may leave this runner unscrubbed. req.Cwd is the run's workspace
	// (the scratch root ScrubLogLine rewrites).
	stdoutW := &chunkedWriter{stream: "stdout", emit: emit, total: &total, workspace: req.Cwd}
	stderrW := &chunkedWriter{stream: "stderr", emit: emit, total: &total, workspace: req.Cwd}
	cmd.Stdout = stdoutW
	cmd.Stderr = stderrW

	// exec's own ctx-watcher calls Cancel when runCtx is done; make that
	// send SIGTERM to the whole group instead of the default
	// Process.Kill() (main pid only — would leave grandchildren alive).
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
	// cmd.Wait has already drained and joined the stdout/stderr copy
	// goroutines, so no further Write calls can arrive: flush each writer's
	// trailing partial line (if the child's last line had no newline) now,
	// not before.
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
// set), overlaid with req.Env.
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
// supplementary groups (without them the child only has its primary gid —
// e.g. a docker-group member would unexpectedly lose docker access).
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
// anyway. ScrubFrame's secret patterns (Bearer tokens, key=value secrets)
// only match a complete value: a write that splits "Bearer ab" from "cdef"
// across two Write calls would otherwise scrub only the first half and leak
// "cdef" raw once it arrives as its own frame with no "Bearer" keyword to
// trip on. Holding the trailing partial line and prepending it to the next
// write reassembles the line before scrubbing. Bounded so a child that never
// emits a newline (binary output, one giant line) can't grow memory or delay
// emission forever; past the bound the partial line is scrubbed and emitted
// as-is, matching pre-fix behavior.
const maxPendingLine = 8 * 1024

// chunkedWriter splits writes into <=MaxFrameBytes Frames, scrubs every frame
// (hostexec ScrubFrame) before emitting it, and drops bytes once the shared
// total crosses MaxTotalOutputBytes (the child keeps running; it just stops
// being relayed). The total charges the SCRUBBED bytes that are emitted, not
// the child's raw bytes, so the cap bounds what reaches the task log.
//
// ScrubFrame is stateless, so a PEM private-key block a child streams across
// two writes would leak: the frame that carried BEGIN swallowed the rest of
// that frame, but the NEXT frame starts mid-key with no BEGIN to trip. inPEM
// carries the bracket across frames per stream: once a frame ends inside a
// BEGIN..END block, every following frame is replaced by nothing until a
// frame carrying the END marker, whose remainder is scrubbed normally.
// Fail-closed: while inPEM, nothing at all is emitted.
type chunkedWriter struct {
	stream    string
	workspace string // req.Cwd: the run's scratch root ScrubLogLine rewrites
	emit      func(Frame)
	total     *atomic.Int64
	inPEM     bool   // PEM state carried across frames for this stream
	pending   []byte // trailing bytes after the last '\n' seen so far, held back
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
		return n, nil // still only an unterminated partial line; wait for more
	}
	w.emitChunks(ready)
	return n, nil
}

// Close flushes whatever partial line is still held in pending once the
// child's output pipe is exhausted and no further Write calls will arrive.
// Run calls this after cmd.Wait(); without it a final line with no trailing
// newline would sit in pending forever and never reach the task log.
func (w *chunkedWriter) Close() error {
	if len(w.pending) == 0 {
		return nil
	}
	data := w.pending
	w.pending = nil
	w.emitChunks(data)
	return nil
}

// emitChunks splits data (already line-complete, or the final flush) into
// <=MaxFrameBytes frames, scrubs and PEM-tracks each one, and emits it
// subject to the MaxTotalOutputBytes cap.
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
				continue // whole frame is still inside the PEM block
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

// pemOpenAfter reports whether data ends inside a PEM private-key block: it
// walks the same line-level bracket scan ScrubFrame uses and reports the
// state after the last line, so a BEGIN with no END after it (or a BEGIN cut
// across the frame boundary) leaves the following frames swallowed.
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
// block that started in an earlier frame, plus whether such an END was found
// (when not, the caller swallows the whole frame). bytes.Split keeps the
// trailing empty part, so re-joining preserves a frame that ended mid-line.
func pemTail(data []byte) ([]byte, bool) {
	lines := bytes.Split(data, []byte("\n"))
	for i, ln := range lines {
		if pemEndPattern.MatchString(string(ln)) {
			return bytes.Join(lines[i+1:], []byte("\n")), true
		}
	}
	return data, false
}
