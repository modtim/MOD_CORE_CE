package executor

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// GSEC FX.17 / R-069 — worker tunables (platform_settings registry keys,
// read via internal/tunables; no per-box env). Both default to the SAFE
// posture: tenant code is refused, and no uid drop happens.
const (
	// nativeAllowUnsandboxedTenantCodeKey explicitly allows tenant tasks to
	// run as unsandboxed host processes on the native backend. Default false.
	nativeAllowUnsandboxedTenantCodeKey = "worker.native_allow_unsandboxed_tenant_code"
	// nativeDropUIDKey sets the non-root uid native processes drop to when
	// the worker runs as root. Default 0 = disabled (no drop).
	nativeDropUIDKey = "worker.native_drop_uid"
)

// ErrNativeTenantRefused is the sentinel the tenant gate refuses with.
// dispatchNativeTask matches it (errors.Is) to fail the task without
// pointless retry; tests match it to distinguish policy refusals from
// process-start failures.
var ErrNativeTenantRefused = errors.New("native backend refuses tenant code without sandbox; enable container backend or set worker tunable native_allow_unsandboxed_tenant_code")

// NativeBackend implements Backend by running tasks as native host processes.
// It is designed for Windows/macOS native workers where customer-installed apps
// (like Blender) run natively rather than in containers.
type NativeBackend struct {
	ScratchRoot string
	mu          sync.Mutex
	runs        map[string]*nativeRun
	// Stager moves task bytes between S3 and the host scratch dir for
	// imageless (native) tasks (GP.5). nil = plain GP.2 process runner with
	// no staging (behavior unchanged).
	Stager NativeStager
	// tunables is the process-wide snapshot of API-pushed worker tunables
	// (GSEC FX.17 / R-069 tenant gate + uid drop). nil = nothing pushed,
	// every reader falls through to its built-in default.
	tunables *tunables.Store
}

// SetTunables attaches the process-wide tunables snapshot (main() wires the
// same store the executor uses). Safe to call before or after heartbeats;
// readers consult it lazily.
func (n *NativeBackend) SetTunables(t *tunables.Store) { n.tunables = t }

// tenantCodeGate is the GSEC FX.17 / R-069 policy: tenant tasks
// (TenantID > 0) are refused on the native backend — they would run as
// unconfined host processes — unless the worker tunable
// worker.native_allow_unsandboxed_tenant_code is explicitly true. Platform/
// system tasks (TenantID == 0) pass unconditionally. Fail-closed: a nil
// store, an absent key, or an unparseable value all mean "not allowed".
func (n *NativeBackend) tenantCodeGate(spec *TaskSpec) error {
	// MAC.1.s12: the decision itself lives in native_confine_policy.go (untagged
	// so the darwin/linux policy is unit-testable on any runner). Same sentinel,
	// same tunable, same fail-closed defaults — the darwin spelling just wraps
	// ErrNativeTenantRefused with the macOS reason, so errors.Is still matches.
	return nativeTenantCodeGate(nativeOSName(), spec, n.tunables)
}

// nativeDropUID returns the uid allowed native processes drop to: the
// tunable value, but only on Linux when the worker itself runs as root
// (a non-root worker cannot chown/chdrop, and the process is already
// unprivileged; on Windows Getuid is always 0 and os.Chown is
// unsupported, so the whole path is Linux-only). 0 = no drop.
func (n *NativeBackend) nativeDropUID() uint32 {
	// MAC.1.s12: the decision lives in native_confine_policy.go so the darwin
	// branch (dedicated less-privileged user, worker.native_darwin_uid) is
	// testable on a linux runner. Linux keeps its existing worker.native_drop_uid
	// reading; every other OS has no uid concept here. 0 = run as the worker.
	return nativeDropUIDForOS(nativeOSName(), n.tunables, uint32(os.Getuid()))
}

// nativeRun tracks a submitted task for NativeBackend.
type nativeRun struct {
	mu        sync.Mutex
	status    TaskStatus
	handle    *nativeHandle
	cancelled bool
	// task and creds are the transport.Task / Credentials this run was
	// submitted for (set by SubmitStaged). Cancel uses them to recover a
	// loaned run's partial outputs via the PartialCollector (Gap B): the
	// Backend.Cancel signature carries only a Handle, so the run itself has
	// to hold the task identity. Both nil for a plain Submit (no staging),
	// in which case Cancel has nothing loan-related to do.
	task  *transport.Task
	creds *transport.Credentials
}

// nativeKillGrace is how long Cancel waits after SIGTERM before SIGKILL.
const nativeKillGrace = 2 * time.Second

// loanPartialCollectTimeout bounds the best-effort partial-output recovery a
// loaned Cancel performs (Gap B). Generous enough for a real S3 upload of a
// node's in-flight outputs, hard-capped so a wedged collection can never
// stall the kill-and-release path that frees the slot.
const loanPartialCollectTimeout = 60 * time.Second

// nativeHostEnv is the host environment a native task inherits. Everything
// else (notably the worker's own credentials) is withheld; the task's inputs
// arrive through spec.Env / spec.NativeEnv like any other backend.
var nativeHostEnv = []string{
	"PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
	"SystemRoot", "SYSTEMROOT", "windir", "ComSpec", "PATHEXT", "USERPROFILE",
	"APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
	"CommonProgramFiles", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
}

// NewNativeBackend creates a NativeBackend that uses scratchRoot for per-task
// working directories.
func NewNativeBackend(scratchRoot string) *NativeBackend {
	return &NativeBackend{
		ScratchRoot: scratchRoot,
		runs:        make(map[string]*nativeRun),
	}
}

var _ Backend = (*NativeBackend)(nil)

// Submit creates the task's scratch directory and starts the native process.
func (n *NativeBackend) Submit(ctx context.Context, spec *TaskSpec) (*Handle, error) {
	if spec == nil {
		return nil, fmt.Errorf("executor: NativeBackend.Submit: nil TaskSpec")
	}

	taskID, err := taskIDFromSpec(spec)
	if err != nil {
		return nil, fmt.Errorf("executor: NativeBackend.Submit: %w", err)
	}

	// GSEC FX.17 / R-069: tenant code is refused on the native backend
	// unless the worker tunable explicitly allows it. Fail-closed; no
	// scratch dir is created for a refused task.
	if err := n.tenantCodeGate(spec); err != nil {
		return nil, fmt.Errorf("executor: NativeBackend.Submit: %w", err)
	}

	scratch := filepath.Join(n.ScratchRoot, taskID)
	inputDir := filepath.Join(scratch, "input")
	outputDir := filepath.Join(scratch, "output")

	if err := os.MkdirAll(inputDir, 0755); err != nil {
		return nil, fmt.Errorf("executor: NativeBackend.Submit: creating input dir: %w", err)
	}
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		return nil, fmt.Errorf("executor: NativeBackend.Submit: creating output dir: %w", err)
	}

	// GSEC FX.17 / R-069: when dropping to a non-root uid, the scratch
	// tree must be owned by that uid or the process cannot write its
	// outputs. Worker is root here (nativeDropUID returned non-zero), so
	// chown cannot legitimately fail; if it does, fail the task loudly.
	dropUID := n.nativeDropUID()
	if dropUID > 0 {
		if err := chownNativeTree(scratch, dropUID); err != nil {
			return nil, fmt.Errorf("executor: NativeBackend.Submit: chowning scratch to uid %d: %w", dropUID, err)
		}
	}

	// NativeCommand + NativeArgs win; otherwise Cmd[0] + Cmd[1:].
	var cmdStr string
	var args []string
	switch {
	case spec.NativeCommand != "":
		cmdStr = spec.NativeCommand
		args = append(args, spec.NativeArgs...)
	case len(spec.Cmd) > 0:
		cmdStr = spec.Cmd[0]
		args = append(args, spec.Cmd[1:]...)
	default:
		return nil, fmt.Errorf("executor: NativeBackend.Submit: no command (NativeCommand and Cmd both empty)")
	}

	// Perform argument substitution
	args = substituteArgs(args, inputDir, outputDir, spec.Env)

	// Build environment
	env := buildEnv(spec)

	n.mu.Lock()
	if _, exists := n.runs[taskID]; exists {
		n.mu.Unlock()
		return nil, fmt.Errorf("executor: NativeBackend.Submit: task %q already submitted", taskID)
	}
	run := &nativeRun{status: TaskStatus{Phase: TaskRunning}}
	n.runs[taskID] = run
	n.mu.Unlock()

	if err := startNative(cmdStr, args, env, scratch, run, dropUID); err != nil {
		n.mu.Lock()
		delete(n.runs, taskID)
		n.mu.Unlock()
		return nil, fmt.Errorf("executor: NativeBackend.Submit: starting process: %w", err)
	}

	return &Handle{
		Backend: "native",
		ID:      taskID,
	}, nil
}

// Poll reports the current state of the task.
func (n *NativeBackend) Poll(_ context.Context, h *Handle) (*TaskStatus, error) {
	if h == nil {
		return nil, fmt.Errorf("executor: NativeBackend.Poll: nil Handle")
	}
	if h.Backend != "native" {
		return nil, fmt.Errorf("executor: NativeBackend.Poll: handle is not native (backend=%q)", h.Backend)
	}

	n.mu.Lock()
	run, ok := n.runs[h.ID]
	n.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("executor: NativeBackend.Poll: unknown task %q", h.ID)
	}

	// The Wait goroutine in startNative is the only writer of status, so
	// Poll never probes the PID (a probe races the reap and could report a
	// finished-OK task as failed).
	run.mu.Lock()
	st := run.status
	run.mu.Unlock()
	return &st, nil
}

// scratchRootPlaceholder replaces any occurrence of the native backend's
// scratch root in a log line — the scratch root is an absolute filesystem
// path that can embed tenant-identifying task IDs.
const scratchRootPlaceholder = "<SCRATCH>"

// redactedTokenPlaceholder replaces the token portion of an
// "Authorization: Bearer <token>"-shaped string that leaked into a log line.
const redactedTokenPlaceholder = "<REDACTED>"

// bearerTokenPattern matches "Bearer <token>" (case-insensitive scheme) so a
// leaked credential never reaches the task-log outbox.
var bearerTokenPattern = regexp.MustCompile(`(?i)\bBearer\s+\S+`)

// ScrubLogLine is the exported entry point for the host-exec stream scrubber
// (internal/hostexec/scrub.go, opsx worker-host-exec hx.8): the host-exec
// helper must redact exactly the same tenant-data shapes the native task-log
// outbox does, so it calls through to scrubLogLine rather than re-deriving the
// rules (one definition, two callers).
func ScrubLogLine(line, scratchRoot string) string { return scrubLogLine(line, scratchRoot) }

// scrubLogLine redacts known tenant-data shapes from a single log line:
// absolute paths under scratchRoot and bearer-token-shaped substrings.
func scrubLogLine(line, scratchRoot string) string {
	out := line
	if scratchRoot != "" {
		out = strings.ReplaceAll(out, scratchRoot, scratchRootPlaceholder)
	}
	out = bearerTokenPattern.ReplaceAllString(out, "Bearer "+redactedTokenPlaceholder)
	return out
}

// scrubbingReadCloser wraps a native task's raw task.log file, redacting
// tenant-data patterns (GSEC sz-native-apps-workers 5.4) line-by-line as the
// task-log outbox streams it, instead of loading the whole file into memory.
type scrubbingReadCloser struct {
	f           *os.File
	scanner     *bufio.Scanner
	scratchRoot string
	buf         bytes.Buffer
	eof         bool
}

func newScrubbingReadCloser(f *os.File, scratchRoot string) *scrubbingReadCloser {
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	return &scrubbingReadCloser{f: f, scanner: scanner, scratchRoot: scratchRoot}
}

func (r *scrubbingReadCloser) Read(p []byte) (int, error) {
	for r.buf.Len() == 0 {
		if r.eof {
			return 0, io.EOF
		}
		if r.scanner.Scan() {
			r.buf.WriteString(scrubLogLine(r.scanner.Text(), r.scratchRoot))
			r.buf.WriteByte('\n')
			continue
		}
		if err := r.scanner.Err(); err != nil {
			return 0, err
		}
		r.eof = true
	}
	return r.buf.Read(p)
}

func (r *scrubbingReadCloser) Close() error {
	return r.f.Close()
}

// Logs returns the combined stdout/stderr log file, scrubbed of tenant-data
// patterns (absolute scratch-root paths, bearer-token-shaped substrings)
// before it is handed to the task-log outbox.
func (n *NativeBackend) Logs(_ context.Context, h *Handle) (io.ReadCloser, error) {
	if h == nil {
		return nil, fmt.Errorf("executor: NativeBackend.Logs: nil Handle")
	}
	if h.Backend != "native" {
		return nil, fmt.Errorf("executor: NativeBackend.Logs: handle is not native (backend=%q)", h.Backend)
	}

	n.mu.Lock()
	_, ok := n.runs[h.ID]
	n.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("executor: NativeBackend.Logs: unknown task %q", h.ID)
	}

	scratch := filepath.Join(n.ScratchRoot, h.ID)
	logPath := filepath.Join(scratch, "task.log")

	f, err := os.Open(logPath)
	if err != nil {
		return nil, fmt.Errorf("executor: NativeBackend.Logs: opening log: %w", err)
	}
	return newScrubbingReadCloser(f, n.ScratchRoot), nil
}

// Cancel kills the entire process group.
//
// GC.MWL.12 Gap B: for a LOANED task (task.Payload.LoanID set, recorded on
// the run by SubmitStaged) Cancel additionally recovers whatever partial
// output the node had written before the kill, via the PartialCollector
// helper (NativeBackend.CollectPartialOutputs → MinIONativeStager), so the
// bytes are retained for the tenant, stamped "partial". This is
// best-effort: it runs on a bounded context, AFTER the process is dead and
// BEFORE anything downstream cleans up the scratch dir, and any error is
// logged and swallowed — it never fails the Cancel and never delays it. A
// non-loan kill (or a plain Submit run with no recorded task) skips it
// entirely, so an ordinary kill behaves exactly as before.
func (n *NativeBackend) Cancel(_ context.Context, h *Handle) error {
	if h == nil {
		return fmt.Errorf("executor: NativeBackend.Cancel: nil Handle")
	}
	if h.Backend != "native" {
		return fmt.Errorf("executor: NativeBackend.Cancel: handle is not native (backend=%q)", h.Backend)
	}

	// FX.123.s1b: this is the run's teardown — dispatchNativeTask defers Cancel on
	// every exit (success, failure, cancel, panic), so the encrypted scratch's
	// Destroy lives here rather than in the staging function's defer. It is
	// installed BEFORE any early return below (including "already finished" and
	// "unknown task"), so a run that settled on its own still loses its key and
	// its dir. Idempotent: a second teardown is a no-op success.
	// (the decrypted input files sit inside the task dir, so Destroy removing
	// the dir removes them too; the key is zeroed before the removal)
	defer func() {
		derr := destroyStagedScratch(filepath.Join(n.ScratchRoot, h.ID, "input"))
		if derr != nil {
			slog.Default().Warn("encrypted scratch teardown failed", "task_id", h.ID, "error", derr)
		}
	}()

	n.mu.Lock()
	run, ok := n.runs[h.ID]
	n.mu.Unlock()
	if !ok {
		return fmt.Errorf("executor: NativeBackend.Cancel: unknown task %q", h.ID)
	}

	run.mu.Lock()
	if run.status.Phase != TaskRunning {
		run.mu.Unlock()
		return nil // Already finished
	}
	run.cancelled = true
	handle := run.handle
	task := run.task
	creds := run.creds
	run.mu.Unlock()

	if err := killProcessGroup(handle); err != nil {
		return fmt.Errorf("executor: NativeBackend.Cancel: %w", err)
	}

	// The process is dead; recover a loaned run's partial outputs before the
	// scratch dir is cleaned up. Never fatal, never blocks the non-loan path.
	n.collectLoanPartialOutputs(h, task, creds)
	return nil
}

// collectLoanPartialOutputs is the Gap B best-effort hook: for a loaned task
// (LoanID != "") whose run is registered, it calls the existing partial-
// collect helper (NativeBackend.CollectPartialOutputs → the Stager's
// PartialCollector) on a bounded context and logs — never returns — any
// error. It is a no-op when the task is nil (a plain Submit with no
// staging), the run has no recorded credentials, the task is not a loan, or
// the Stager does not implement PartialCollector — so a non-loan kill (or a
// no-op stager) returns immediately and is never delayed.
//
// Ordering: this runs from Cancel AFTER killProcessGroup has returned, so
// the run is being torn down. But the startNative Wait goroutine is what
// flips the phase TaskRunning → TaskFailed, and it may not have done so in
// the instant between the kill and our call — while CollectPartialOutputs
// refuses a run still in TaskRunning. So we wait (bounded by the same
// context) for the phase to leave TaskRunning before invoking the helper;
// a killed run settles to TaskFailed, which is exactly what the partial
// path expects. If the phase never settles within the budget we skip rather
// than block the kill/release path.
func (n *NativeBackend) collectLoanPartialOutputs(h *Handle, task *transport.Task, creds *transport.Credentials) {
	if task == nil || h == nil || task.Payload.LoanID == "" || creds == nil {
		return
	}
	if n.Stager == nil {
		return
	}
	if _, ok := n.Stager.(PartialCollector); !ok {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), loanPartialCollectTimeout)
	defer cancel()

	n.mu.Lock()
	run, ok := n.runs[h.ID]
	n.mu.Unlock()
	if !ok {
		return
	}
	if !n.waitPhaseSettled(ctx, run) {
		slog.Default().Warn("loan partial-output recovery skipped: run did not settle to a terminal phase in time",
			"task_id", task.ID, "loan_id", task.Payload.LoanID)
		return
	}
	outs, err := n.CollectPartialOutputs(ctx, h, task, creds, task.Payload.LoanID)
	if err != nil {
		slog.Default().Warn("loan partial-output recovery failed (logged, not fatal)",
			"task_id", task.ID, "loan_id", task.Payload.LoanID, "error", err)
		return
	}
	if len(outs) > 0 {
		slog.Default().Info("loan partial outputs retained",
			"task_id", task.ID, "loan_id", task.Payload.LoanID, "outputs", len(outs))
	}
}

// waitPhaseSettled polls the run's phase (under run.mu) until it is no longer
// TaskRunning or ctx is done, reporting whether it settled. A killed run
// flips to TaskFailed from the startNative Wait goroutine; this lets the
// partial-collect path (which refuses a TaskRunning run) proceed safely.
func (n *NativeBackend) waitPhaseSettled(ctx context.Context, run *nativeRun) bool {
	for {
		run.mu.Lock()
		phase := run.status.Phase
		run.mu.Unlock()
		if phase != TaskRunning {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// taskIDFromSpec extracts the task ID from the spec's Env (MOD_TASK_ID).
func taskIDFromSpec(spec *TaskSpec) (string, error) {
	id, ok := spec.Env["MOD_TASK_ID"]
	if !ok {
		return "", fmt.Errorf("MOD_TASK_ID not set in spec.Env")
	}
	return id, nil
}

// substituteArgs performs {input_dir}, {output_dir}, and {param:NAME} substitution.
func substituteArgs(args []string, inputDir, outputDir string, params map[string]string) []string {
	result := make([]string, len(args))
	for i, arg := range args {
		result[i] = arg
		result[i] = strings.ReplaceAll(result[i], "{input_dir}", inputDir)
		result[i] = strings.ReplaceAll(result[i], "{output_dir}", outputDir)

		// Substitute {param:NAME}
		for {
			start := strings.Index(result[i], "{param:")
			if start == -1 {
				break
			}
			end := strings.Index(result[i][start:], "}")
			if end == -1 {
				break
			}
			end += start

			paramName := result[i][start+7 : end]
			if val, ok := params[paramName]; ok {
				result[i] = result[i][:start] + val + result[i][end+1:]
			} else {
				// Keep the placeholder if param not found
				break
			}
		}
	}
	return result
}

// buildEnv constructs the process environment.
func buildEnv(spec *TaskSpec) []string {
	env := make([]string, 0, len(nativeHostEnv)+len(spec.Env)+len(spec.NativeEnv))
	for _, k := range nativeHostEnv {
		if v, ok := os.LookupEnv(k); ok {
			env = append(env, k+"="+v)
		}
	}
	for k, v := range spec.Env {
		env = append(env, k+"="+v)
	}
	for k, v := range spec.NativeEnv {
		env = append(env, k+"="+v)
	}
	return env
}

// startNative launches the process (no shell) with stdout+stderr going to
// <scratch>/task.log, and owns the run's status until the process exits.
// dropUID is the GSEC FX.17 / R-069 non-root uid the process drops to on
// Linux when the worker is root (0 = no drop; see nativeDropUID).
func startNative(cmdStr string, args, env []string, scratch string, run *nativeRun, dropUID uint32) error {
	logFile, err := os.OpenFile(filepath.Join(scratch, "task.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	if err != nil {
		return fmt.Errorf("creating log file: %w", err)
	}
	// The log file is created after Submit's chown walk; the dropped-uid
	// process still needs to open it by path on reopen paths (tools that
	// tail task.log), so match its ownership to the rest of the scratch.
	if dropUID > 0 {
		if hasCapChown() {
			if err := os.Chown(logFile.Name(), int(dropUID), int(dropUID)); err != nil {
				logNoCapOnce()
				slog.Warn("chowning task log failed; continuing as worker user", "err", err)
			}
		} else {
			logNoCapOnce()
		}
	}
	cmd := exec.Command(cmdStr, args...)
	cmd.Env = env
	cmd.Dir = scratch
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	configureNativeProcess(cmd)
	confineNativeProcess(cmd, dropUID)

	// Hold run.mu across start+attach so Cancel can't see a nil handle.
	run.mu.Lock()
	if err := cmd.Start(); err != nil {
		run.mu.Unlock()
		logFile.Close()
		return err
	}
	h, err := attachNativeProcess(cmd)
	if err != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		run.mu.Unlock()
		logFile.Close()
		return err
	}
	run.handle = h
	run.mu.Unlock()

	go func() {
		defer logFile.Close()
		werr := cmd.Wait()
		releaseNativeHandle(h)
		run.mu.Lock()
		defer run.mu.Unlock()
		var exitErr *exec.ExitError
		switch {
		case run.cancelled:
			run.status = TaskStatus{Phase: TaskFailed, ExitCode: 128, Err: "cancelled"}
		case werr == nil:
			run.status = TaskStatus{Phase: TaskSucceeded, ExitCode: 0}
		case errors.As(werr, &exitErr):
			run.status = TaskStatus{Phase: TaskFailed, ExitCode: exitErr.ExitCode(), Err: werr.Error()}
		default:
			run.status = TaskStatus{Phase: TaskFailed, ExitCode: 1, Err: werr.Error()}
		}
	}()
	return nil
}
