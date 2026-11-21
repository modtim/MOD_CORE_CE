package main

// launch-completion-megaplan GP.17e (task #4 in gp-17-wslc-design-DRAFT.md §5)
// — wslc NOAH session-lifecycle glue.
//
// wslc has NO pause verb (design §2: the enumerated Container APIs are
// Create/Open/Start/GetState/Stop/Delete/Release only), so a live wslc
// session can only ever be HARD-EVICTED, never soft-paused-then-resumed.
// Every wslc-backed task must go through this manager's yield path, which
// Cancels every running wslc task and then terminates the session —
// mirroring preempt.go's unconditional "pinned memory" evict branch.
//
// Lifecycle state machine:
//
//	off --first claimed wslc task--> ensure running --startSession--> running
//	running --OnYield--> Cancel every running task, then stopSession --> off
//	running --last task finishes + idleTimeout (Tick)--> stopSession --> off
//	off --OnYield--> no-op (nothing to evict)
//
// Create is LAZY: the session is started only on the first claimed wslc
// task, never at worker boot, so a Windows box that never receives a wslc
// task never pays the session-start cost.
//
// Hard rule: this code never touches WSL itself. No `wsl --update`, no
// `wsl --shutdown` — session start/stop go exclusively through the injected
// start/stop closures (GP.17b owns what they shell out to).

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// wslcBackendOps is the subset of executor.Backend this manager needs.
// *executor.WslcBackend satisfies it (it implements the whole Backend).
type wslcBackendOps interface {
	Submit(ctx context.Context, spec *executor.TaskSpec) (*executor.Handle, error)
	Poll(ctx context.Context, h *executor.Handle) (*executor.TaskStatus, error)
	Cancel(ctx context.Context, h *executor.Handle) error
	// ClearRegistryLogins (GP.17f) forgets the backend's cached wslc registry
	// logins; called when the session stops, since a fresh session starts
	// with no stored registry credentials.
	ClearRegistryLogins()
}

// wslcSessionManager owns the single live wslc session for this worker.
//
// Injectables (all nil-safe):
//   - StartSession / StopSession: the CLI-level session semantics are NOT yet
//     confirmed by GP.17b (wslcsdk.dll ABI vs wslc.exe flags), so they stay
//     behind closures rather than hardcoded commands.
//     TODO(GP.17b): replace with the confirmed session create/terminate
//     mechanism (wslcsdk.dll WslcTerminateSession or wslc.exe equivalent).
//   - Clock: the idle timer reads now via Clock so tests advance time.
//
// The idle timeout is a plain field with a code default (wslcIdleTimeout);
// there is deliberately NO new env var. If it later needs to be tunable per
// deployment, it belongs in the GS.PSR platform-settings tunables registry
// (worker already consults `tun` in main() — see the GS.PSR.7 block) rather
// than a MOD_* env var.
type wslcSessionManager struct {
	mu           sync.Mutex
	log          *slog.Logger
	backend      wslcBackendOps
	StartSession func() error
	StopSession  func() error
	Clock        func() time.Time

	idleTimeout  time.Duration
	idleSince    time.Time // first moment with zero running tasks (zero = never idle)
	lastStartErr error     // last StartSession failure, for logs/status
	live         bool      // session started successfully, not yet stopped

	// taskMu guards tasks (a map) separately from mu: OnTaskFinished runs
	// inside CancelAll, which already holds mu, and a re-entrant task-
	// mutation under mu would deadlock.
	taskMu    sync.Mutex
	tasks     map[int]*executor.Handle
	preempted map[int]bool // task evicted by NOAH yield (for the [noah-preempted] report)
}

// wslcIdleTimeout — how long a live wslc session is kept after its last
// task finishes before it is terminated. Code default; see type doc for the
// platform-settings note.
const wslcIdleTimeout = 10 * time.Minute

func newWslcSessionManager(log *slog.Logger) *wslcSessionManager {
	return &wslcSessionManager{
		log:         log,
		Clock:       time.Now,
		idleTimeout: wslcIdleTimeout,
		tasks:       make(map[int]*executor.Handle),
		preempted:   make(map[int]bool),
	}
}

// now returns the current time from the injected clock (never time.Now
// directly, so tests can advance it).
func (m *wslcSessionManager) now() time.Time {
	if m.Clock != nil {
		return m.Clock()
	}
	return time.Now()
}

// runningCount is the number of wslc tasks currently tracked as running.
func (m *wslcSessionManager) runningCount() int {
	m.taskMu.Lock()
	defer m.taskMu.Unlock()
	return len(m.tasks)
}

// OnTaskClaimed is called when a wslc task has been claimed. It lazily
// starts the session (no double-start: an already-live or already-starting
// session is reused), resets any idle timer, and — when h is non-nil —
// tracks the task handle for yield-time hard eviction.
//
// h may be nil for the pre-submit claim (the session must exist before the
// container is started, so the manager is consulted first, then the caller
// Submits and re-calls OnTaskClaimed with the real handle to track it).
//
// If the session cannot be started, the error is returned so the caller
// FAILS THE TASK LOUDLY (shouldRetry=false). No retry loop here: the
// manager does not retry on its own — at most one StartSession per claim,
// so the failure count is bounded by the API's requeue policy, never by a
// worker-side retry storm.
func (m *wslcSessionManager) OnTaskClaimed(taskID int, h *executor.Handle) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.idleSince = time.Time{} // a task is about to run: no longer idle
	if h != nil {
		m.taskMu.Lock()
		m.tasks[taskID] = h
		m.taskMu.Unlock()
	}

	// Already live: reuse the session, never double-start. m.mu is held
	// across the StartSession call, so concurrent claims serialize here —
	// the first one starts, the rest see live=true.
	var startErr error
	if !m.live && m.StartSession != nil {
		startErr = m.StartSession()
		if startErr != nil {
			m.lastStartErr = startErr
			m.log.Error("wslc session start failed; failing the task (no retry)",
				"task", taskID, "error", startErr)
		} else {
			m.live = true
			m.lastStartErr = nil
		}
	}
	return startErr
}

// OnTaskFinished is called when a wslc task reaches a terminal state
// (completed, failed, or cancelled). It drops the task from the tracked set
// and (re)starts the idle timer; the session is terminated later by Tick.
func (m *wslcSessionManager) OnTaskFinished(taskID int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.taskMu.Lock()
	delete(m.tasks, taskID)
	delete(m.preempted, taskID)
	empty := len(m.tasks) == 0
	m.taskMu.Unlock()
	if empty {
		m.idleSince = m.now()
	}
}

// wasPreempted reports whether taskID was hard-evicted by a NOAH yield. The
// runWslcTask report path reads this (mirroring preempt.wasPreempted) to tag
// the failure [noah-preempted] and requeue without a retry charge.
func (m *wslcSessionManager) wasPreempted(taskID int) bool {
	m.taskMu.Lock()
	defer m.taskMu.Unlock()
	return m.preempted[taskID]
}

// OnYield is the NOAH yield hook (user came back / preemption). HARD-EVICT:
// Cancel every running wslc task via the backend, then terminate the
// session. With no live session and no tracked tasks this is a no-op.
func (m *wslcSessionManager) OnYield(reason string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Snapshot the running handles so Cancel can be called without holding
	// m.mu (Cancel may block on the wslc process).
	m.taskMu.Lock()
	handles := make([]*executor.Handle, 0, len(m.tasks))
	for _, h := range m.tasks {
		handles = append(handles, h)
	}
	m.taskMu.Unlock()

	for _, h := range handles {
		if m.backend == nil || h == nil {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		err := m.backend.Cancel(ctx, h)
		cancel()
		if err != nil {
			m.log.Warn("wslc hard-evict: cancel failed", "task", h.ID, "error", err, "reason", reason)
		}
	}

	// Mark the evicted tasks preempted (their report path tags
	// [noah-preempted] + requeue without retry charge), then drop them.
	m.taskMu.Lock()
	for id := range m.tasks {
		m.preempted[id] = true
	}
	m.tasks = make(map[int]*executor.Handle)
	m.taskMu.Unlock()
	m.idleSince = time.Time{}
	stopped := false
	if m.live {
		m.live = false
		if m.StopSession != nil {
			if err := m.StopSession(); err != nil {
				m.log.Warn("wslc session stop failed on yield", "error", err, "reason", reason)
			}
			stopped = true
		}
		// GP.17f: a terminated session holds no registry credentials — drop
		// the cached logins so the next session re-authenticates.
		if m.backend != nil {
			m.backend.ClearRegistryLogins()
		}
	}
	if !stopped && m.log != nil {
		m.log.Debug("wslc OnYield: no live session, nothing to evict", "reason", reason)
	}
}

// Tick advances the idle timer. If the session is live, has no running
// tasks, and the idle timeout has elapsed, the session is terminated.
// Call it from a periodic loop (e.g. once a minute) — it is cheap when idle
// state is not met.
func (m *wslcSessionManager) Tick(now time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.live && !m.idleSince.IsZero() && m.runningCount() == 0 &&
		now.Sub(m.idleSince) >= m.idleTimeout {
		m.live = false
		m.idleSince = time.Time{}
		if m.StopSession != nil {
			if err := m.StopSession(); err != nil {
				m.log.Warn("wslc session stop failed on idle timeout", "error", err)
			}
		}
		// GP.17f: idle stop terminates the session — drop cached logins too.
		if m.backend != nil {
			m.backend.ClearRegistryLogins()
		}
	}
}

// Status is a small snapshot for logging / the local UI.
func (m *wslcSessionManager) Status() (live bool, tasks int, idleSince time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.live, m.runningCount(), m.idleSince
}

// ---------------------------------------------------------------------------
// Production wiring (package-level, single per-process service — same shape
// as k8sTaskBackend / nativeBackend / reporter in main.go).
//
// Inert unless the API stamps task.Payload.ExecutorBackend="wslc" AND this
// worker probed wslc available (profile.Wslc). With no wslc capability
// wslcSession is nil and every hook below is a nil-safe no-op, so a worker
// without wslc behaves exactly as before.
// ---------------------------------------------------------------------------

// wslcPollInterval is the wslc task status poll cadence (k8s parity).
const wslcPollInterval = 2 * time.Second

// wslcTickInterval is how often the idle-timeout Tick fires.
const wslcTickInterval = time.Minute

// wslcSession is the live manager for this worker; nil when this worker has
// no wslc capability.
var wslcSession *wslcSessionManager

// wslcTaskBackend is the *executor.WslcBackend dispatch uses; nil when the
// backend could not be built.
var wslcTaskBackend *executor.WslcBackend

// wslcBackendErr is the build error, surfaced in the loud task failure.
var wslcBackendErr error

// initWslcSession builds the wslc backend + session manager once at start.
// It is a no-op unless the capability probe (GP.17c) marked this host as
// able to run wslc — with no capability, wslcSession stays nil and every
// hook (dispatch / onYield / tick) is a nil-safe no-op, so a worker without
// wslc behaves exactly as before.
//
// The session start/stop closures are injectable because the CLI-level
// session semantics are not yet confirmed (GP.17b):
//   - TODO(GP.17b): StartSession — the real session-creation verb (SDK
//     Session.Start or the wslc.exe equivalent). Today wslc's `wslc run`
//     form is one-shot and the session rides along with the first run, so
//     this closure is a no-op that merely records liveness.
//   - TODO(GP.17b): StopSession — the real session-terminate verb
//     (WslcTerminateSession). Today each Cancel already kills its run; this
//     closure is a no-op until a persistent session object exists to end.
func initWslcSession(wslcAvailable bool, scratchRoot string, log *slog.Logger) {
	if !wslcAvailable {
		return
	}
	b := executor.NewWslcBackend("", filepath.Join(scratchRoot, "wslc"))
	wslcTaskBackend = b
	m := newWslcSessionManager(log)
	m.backend = b
	m.StartSession = func() error { return nil } // TODO(GP.17b): real session create
	m.StopSession = func() error { return nil }  // TODO(GP.17b): real WslcTerminateSession
	wslcSession = m
	log.Info("wslc NOAH session lifecycle enabled", "scratch", filepath.Join(scratchRoot, "wslc"))
}

// isWslcTask reports whether the API stamped this task for the wslc backend
// (per-task gate, same pattern as k8s's ExecutorBackend gate).
func isWslcTask(backend string) bool {
	return strings.EqualFold(strings.TrimSpace(backend), "wslc")
}

// wslcSessionTick runs the idle-timeout tick once per interval until ctx is
// done. No-op (and never started) without wslc capability.
func wslcSessionTick(ctx context.Context) {
	if wslcSession == nil {
		return
	}
	t := time.NewTicker(wslcTickInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-t.C:
			wslcSession.Tick(now)
		}
	}
}

// wslcLogTail drains the last wslcLogTailLines of a task's log for the
// failure/complete report (same shape as k8sLogTail).
func wslcLogTail(b *executor.WslcBackend, h *executor.Handle) string {
	if b == nil {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	rc, err := b.Logs(ctx, h)
	if err != nil || rc == nil {
		return ""
	}
	defer rc.Close()
	return tailLines(rc, wslcLogTailLines)
}

// wslcLogTailLines bounds the log included in reports.
const wslcLogTailLines = 200

// runWslcTask runs one task on the wslc backend end to end (Submit → Poll →
// report), mirroring runK8sTask's result path but without K8s-only extras
// (idle watchdog / FQDN egress report / staging): wslc staging + egress
// policy are GP.17f/GP.17g, still in flight. It is inert unless the caller
// has already established wslcSession is non-nil.
func runWslcTask(ctx context.Context, taskLog *slog.Logger, task *transport.Task, creds *transport.Credentials) {
	b := wslcTaskBackend
	if b == nil {
		msg := "executor backend is wslc but the wslc backend is unavailable; refusing to run on docker"
		if wslcBackendErr != nil {
			msg = fmt.Sprintf("%s: %v", msg, wslcBackendErr)
		}
		taskLog.Error(msg)
		reporter.Failed(ctx, task.ID, msg, false, "", false, "")
		return
	}

	spec, err := executor.TaskSpecFromTask(task, creds, executor.TaskSpecOptions{Log: taskLog})
	if err != nil {
		reporter.Failed(ctx, task.ID, fmt.Sprintf("wslc task spec: %s", err), false, "", false, "")
		return
	}

	// Lazy session start BEFORE Submit: the session must exist (or be
	// starting) first. Failure fails the task loudly, no retry (see
	// OnTaskClaimed doc).
	if err := wslcSession.OnTaskClaimed(task.ID, nil); err != nil {
		taskLog.Error("wslc session start failed; failing the task (no retry)", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("wslc session start: %s", err), false, "", false, "")
		return
	}

	h, err := b.Submit(ctx, spec)
	if err != nil {
		taskLog.Error("wslc submit failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("wslc submit: %s", err), false, "", false, "")
		return
	}
	// Track the real handle for yield-time hard eviction.
	if err := wslcSession.OnTaskClaimed(task.ID, h); err != nil {
		// Session already live: cannot fail here; track anyway (best-effort).
		taskLog.Warn("wslc track after submit failed (unexpected)", "error", err)
	}
	taskLog.Info("wslc task submitted", "handle", h.ID, "image", spec.Image)
	started := time.Now()
	defer wslcSession.OnTaskFinished(task.ID)

	u, perr := executor.PollUntilDone(ctx, b, h, wslcPollInterval, func(u executor.NodeRunUpdate) {
		taskLog.Info("wslc task status", "status", u.Status, "reason", u.Reason, "exit_code", u.ExitCode)
	})
	if perr != nil {
		// Cancelled (worker shutdown / NOAH yield) or timed out: stop the run.
		cctx, c := context.WithTimeout(context.Background(), 15*time.Second)
		if cerr := b.Cancel(cctx, h); cerr != nil {
			taskLog.Warn("wslc task cancel failed", "handle", h.ID, "error", cerr)
		}
		c()
		// A NOAH yield already cancelled+reported via the manager's yield
		// path; do not double-report. A plain ctx cancel/timout still
		// reports.
		if !errors.Is(perr, context.Canceled) {
			reporter.Failed(ctx, task.ID, fmt.Sprintf("wslc task aborted: %s", perr), true, wslcLogTail(b, h), false, "", nil)
		}
		return
	}
	logTail := wslcLogTail(b, h)
	if u.Status != executor.NodeRunCompleted {
		errMsg := u.ErrorMessage
		shouldRetry := u.ShouldRetry
		if wslcSession.wasPreempted(task.ID) {
			// NOAH hard-evicted this run: requeue WITHOUT a retry charge,
			// same contract as the docker path's [noah-preempted] tag.
			taskLog.Info("wslc task yielded to returning user — requeue without retry charge")
			errMsg = "[noah-preempted] " + errMsg
			shouldRetry = true
		} else {
			taskLog.Error("wslc task failed", "reason", u.Reason, "exit_code", u.ExitCode, "error", u.ErrorMessage)
		}
		reporter.Failed(ctx, task.ID, errMsg, shouldRetry, logTail, u.OOMKilled, u.ErrorDetail, nil)
		return
	}
	// Outputs: wslc S3 staging is GP.17f (not yet wired); report empty
	// outputs (never nil) until it lands.
	reporter.Complete(ctx, task.ID, transport.TaskResult{Outputs: []transport.OutputFile{}, LogTail: logTail})
	taskLog.Info("wslc task completed", "duration", time.Since(started).Round(time.Second))
}
