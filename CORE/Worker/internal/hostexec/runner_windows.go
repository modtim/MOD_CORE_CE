//go:build windows

// Package hostexec: Windows native direct runner (opsx worker-host-exec
// hx.9w). A Windows worker installed as a SYSTEM service has the same
// root-equivalent privilege model as a linux systemd worker, so DIRECT mode
// (no containerized helper) is supported here too. Windows has no
// process-group SIGTERM/SIGKILL or uid/gid credential-switch syscalls, so
// this is not a line-for-line port of runner.go: the child is placed in its
// own process group (CREATE_NEW_PROCESS_GROUP) so cancellation can send it a
// graceful CTRL_BREAK_EVENT, and os/exec's own WaitDelay escalates to a hard
// TerminateProcess (via Process.Kill) if the group hasn't exited by then —
// the same "signal, then escalate after a grace period" shape as the linux
// runner's SIGTERM-then-SIGKILL, using the primitives Windows actually has.
// req.RunAs is refused (returns an error) rather than silently dropped:
// Windows user-switch needs CreateProcessWithLogonW token impersonation
// (a plaintext credential or token handle), which this Request shape (a bare
// username string, matching the linux/darwin contract) can't carry — wiring
// that is a follow-up, not something to fake here.
package hostexec

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
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

// killGrace is how long Run waits, after Cancel sends CTRL_BREAK_EVENT, for
// the process to exit before os/exec's WaitDelay force-kills it.
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
// only becomes a shell if the caller put one there, e.g. "cmd.exe" or
// "cmd"), streaming output to emit in <=MaxFrameBytes chunks, and returns
// the process's exit code.
func Run(ctx context.Context, req Request, emit func(Frame)) (int, error) {
	if len(req.Argv) == 0 {
		return -1, fmt.Errorf("hostexec: empty argv")
	}
	if req.RunAs != "" {
		return -1, fmt.Errorf("hostexec: RunAs user-switch is not supported on windows yet (job requested %q)", req.RunAs)
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
	// CREATE_NEW_PROCESS_GROUP lets Cancel below target the whole child
	// process tree with a console control event instead of only the main
	// pid (the closest Windows equivalent of the linux runner's
	// process-group SIGTERM). The job object below (attachJob) is what
	// actually closes the gap: os/exec's Process.Kill escalation only
	// TerminateProcess'es the main pid, so grandchildren and any child
	// that swallows CTRL_BREAK would otherwise survive the timeout as
	// orphans while Run reports the job killed.
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NEW_PROCESS_GROUP}
	// job is the Job Object the child is assigned to once started (0 if
	// assignment failed; then we fall back to CTRL_BREAK + Process.Kill).
	// cancelled records that Cancel ran, i.e. the run ended by
	// timeout/cancellation rather than the child exiting on its own.
	var job windows.Handle
	var cancelled bool

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

	// On ctx cancellation, send the process group a graceful CTRL_BREAK_EVENT
	// instead of os/exec's default Process.Kill(); WaitDelay below is the
	// escalation if the group ignores it.
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		cancelled = true
		return windows.GenerateConsoleCtrlEvent(windows.CTRL_BREAK_EVENT, uint32(cmd.Process.Pid))
	}
	cmd.WaitDelay = killGrace

	if err := cmd.Start(); err != nil {
		return -1, fmt.Errorf("hostexec: starting %q: %w", req.Argv[0], err)
	}

	// Small race: between Start and the AssignProcessToJobObject in attachJob
	// the child may already have spawned grandchildren, which the job won't
	// cover and TerminateJobObject won't reap. CREATE_SUSPENDED would close
	// the window but isn't reachable through os/exec without reimplementing
	// its spawn plumbing, so we accept it — the CTRL_BREAK graceful step still
	// reaches the whole tree via the console, and the window is microseconds.
	if err := attachJob(cmd, &job); err != nil {
		// Log once and keep going: job-object assignment can fail when the
		// worker itself already runs inside a job without breakaway (nested
		// jobs are rejected). Fall back to the pre-992h behaviour
		// (CTRL_BREAK + os/exec's TerminateProcess of the main pid only).
		fmt.Fprintf(os.Stderr, "hostexec: job-object attach for pid %d failed, falling back to main-pid kill: %v\n", cmd.Process.Pid, err)
	}
	// KILL_ON_JOB_CLOSE: closing the job handle at the end of the run reaps
	// the whole child tree; it also cleans up if the worker itself dies (the
	// OS closes outstanding job objects on process exit).
	defer func() {
		if job != 0 {
			_ = windows.CloseHandle(job)
		}
	}()

	waitErr := cmd.Wait()
	_ = stdoutW.Close()
	_ = stderrW.Close()

	// If the run ended by timeout/cancellation, WaitDelay has already fired
	// (os/exec's Process.Kill on the main pid). Escalate to the whole tree:
	// TerminateJobObject reaches every process assigned to the job, so
	// grandchildren and CTRL_BREAK refusers die too.
	if cancelled && job != 0 {
		if err := windows.TerminateJobObject(job, 1); err != nil {
			fmt.Fprintf(os.Stderr, "hostexec: terminating job for pid %d: %v\n", cmd.Process.Pid, err)
		}
	}

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
// set), overlaid with req.Env. Kept in sync with runner.go's linux version
// (HOME is rarely set on Windows, but this mirrors the contract rather than
// substituting USERPROFILE — a job that needs it can set it via req.Env).
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

// attachJob places the started child into a Job Object flagged
// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, the Windows answer to the linux runner's
// Setpgid + Kill(-pid): TerminateJobObject / CloseHandle reach the child's
// whole process tree, not just the main pid. On success *job receives the
// handle (Run closes it in a defer). Failure is returned to the caller, which
// logs once and continues without a job (see the fallback note in Run).
func attachJob(cmd *exec.Cmd, job *windows.Handle) error {
	j, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return fmt.Errorf("creating job object: %w", err)
	}
	// KILL_ON_JOB_CLOSE is set via SetInformationJobObject: CreateJobObject's
	// jobAttr parameter is a SECURITY attribute struct, not the limits one, so
	// the limits have to be applied after creation (same shape as
	// executor/native_proc_windows.go).
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(j, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		_ = windows.CloseHandle(j)
		return fmt.Errorf("configuring job object: %w", err)
	}
	// PROCESS_TERMINATE is what lets us later TerminateJobObject the process;
	// PROCESS_SET_QUOTA mirrors the executor precedent for job accounting.
	proc, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil {
		_ = windows.CloseHandle(j)
		return fmt.Errorf("opening process: %w", err)
	}
	defer func() { _ = windows.CloseHandle(proc) }()
	if err := windows.AssignProcessToJobObject(j, proc); err != nil {
		_ = windows.CloseHandle(j)
		return fmt.Errorf("assigning process to job: %w", err)
	}
	*job = j
	return nil
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
