package executor

// k8s-reduction-engine W16: inactivity-liveness (idle kill) for K8s task Jobs.
//
// Mirrors the docker node path (executor.go runContainerWithCreds, the
// "INACTIVITY-based liveness" loop + Run()'s outer-cap block):
//
//   - The idle window is the node's container_timeout (payload.ContainerTimeout
//     seconds), defaulting to 30m when 0 — docker never disables the idle kill
//     via a 0 timeout, so neither do we.
//   - The whole model is gated by MOD_INACTIVITY_LIVENESS (!= "0") and is off
//     for wizard_test_run sandbox runs; in those cases docker applies a hard
//     wall-clock cap of the same timeout (SandboxTimeoutS for sandbox runs),
//     and so does K8sTaskLiveness.
//   - With inactivity on there is no wall-clock cap unless
//     MOD_CONTAINER_ABSOLUTE_MAX_S is set.
//   - The failure the API sees is identical to docker's:
//     "run container: container killed: no progress (no logs, CPU/GPU, or
//     output growth) for <window>", reported retryable.
//
// Activity on K8s = a log line from the `task` container (the streamer's
// OnActivity hook) OR, W20, `task`-container CPU use above k8sCPUActiveMilli
// per the Metrics API (metrics.k8s.io PodMetrics; see k8s_metrics.go). That
// mirrors docker's CPU signal. There is no standard per-pod GPU-util API and
// no /data probe into a pod, so a GPU-only job that is silent AND ~0 CPU for
// a whole window is still killed — its feeding process normally burns some
// CPU; for truly silent jobs set MOD_INACTIVITY_LIVENESS=0 (hard wall-clock
// cap) or a larger container_timeout / MOD_CONTAINER_ABSOLUTE_MAX_S. If the
// Metrics API is missing or forbidden the watchdog degrades to log-only. The
// idle clock does not start until the task container is Running (stage-in
// and image pull don't count).

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// k8sIdleProbeEvery matches the docker path's 15s liveness probe.
const k8sIdleProbeEvery = 15 * time.Second

// k8sDefaultContainerTimeout matches Run()'s default when container_timeout=0.
const k8sDefaultContainerTimeout = 30 * time.Minute

// K8sLiveness is the liveness policy for one K8s task.
type K8sLiveness struct {
	// IdleWindow > 0 enables the inactivity watchdog.
	IdleWindow time.Duration
	// HardCap > 0 is a wall-clock ceiling on the whole run (0 = none).
	HardCap time.Duration
}

// K8sTaskLiveness derives the liveness policy exactly like the docker path.
func K8sTaskLiveness(p transport.TaskPayload) K8sLiveness {
	timeout := time.Duration(p.ContainerTimeout) * time.Second
	if timeout == 0 {
		timeout = k8sDefaultContainerTimeout
	}
	if p.Purpose == "wizard_test_run" && p.SandboxTimeoutS > 0 {
		timeout = time.Duration(p.SandboxTimeoutS) * time.Second
	}
	if !_inactivityLivenessEnabled() || p.Purpose == "wizard_test_run" {
		return K8sLiveness{HardCap: timeout} // legacy / sandbox hard cap
	}
	l := K8sLiveness{IdleWindow: timeout}
	if v := os.Getenv("MOD_CONTAINER_ABSOLUTE_MAX_S"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			l.HardCap = time.Duration(n) * time.Second
		}
	}
	return l
}

// IdleKillError is returned when the watchdog killed the task for making no
// progress. Error() is the exact string the docker path reports.
type IdleKillError struct{ Window time.Duration }

func (e *IdleKillError) Reason() string {
	return fmt.Sprintf("no progress (no logs, CPU/GPU, or output growth) for %s", e.Window.Round(time.Second))
}

func (e *IdleKillError) Error() string {
	return fmt.Sprintf("run container: container killed: %s", e.Reason())
}

// IdleWatchdog tracks task-container activity and reports when it has been
// idle longer than Window. The clock only starts once the container is
// Running (Started probe true, or the first Touch).
type IdleWatchdog struct {
	Window time.Duration
	// Started reports whether the task container is Running (or already
	// terminated). Polled each tick until true. nil = only Touch starts it.
	Started func(ctx context.Context) bool
	Now     func() time.Time // nil = time.Now
	Tick    time.Duration    // nil/0 = k8sIdleProbeEvery
	// Active (W20) is a non-log activity probe (task-container CPU busy),
	// consulted once started and only when no log line arrived within the
	// last Tick. true = progress. nil = log-only.
	Active func(ctx context.Context) bool

	mu      sync.Mutex
	started bool
	last    time.Time
}

func (w *IdleWatchdog) now() time.Time {
	if w.Now != nil {
		return w.Now()
	}
	return time.Now()
}

// Touch records activity (a log line). It also starts the clock: output
// implies the container is running. Safe for concurrent use.
func (w *IdleWatchdog) Touch() {
	if w == nil {
		return
	}
	t := w.now()
	w.mu.Lock()
	w.started = true
	w.last = t
	w.mu.Unlock()
}

// check advances the watchdog at time now; true = idle limit exceeded.
func (w *IdleWatchdog) check(ctx context.Context, now time.Time) bool {
	if w.Window <= 0 {
		return false
	}
	w.mu.Lock()
	started := w.started
	w.mu.Unlock()
	if !started {
		if w.Started == nil || !w.Started(ctx) {
			return false
		}
		w.mu.Lock()
		if !w.started { // a concurrent Touch may have beaten us
			w.started = true
			w.last = now
		}
		w.mu.Unlock()
		return false
	}
	w.mu.Lock()
	idle := now.Sub(w.last)
	w.mu.Unlock()
	if w.Active != nil && idle >= w.tick() && w.Active(ctx) {
		w.mu.Lock()
		if now.After(w.last) {
			w.last = now
		}
		w.mu.Unlock()
		return false
	}
	return idle > w.Window
}

func (w *IdleWatchdog) tick() time.Duration {
	if w.Tick > 0 {
		return w.Tick
	}
	return k8sIdleProbeEvery
}

// Run blocks until ctx is done (returns nil) or the task has been idle
// longer than Window (returns *IdleKillError). Window <= 0 disables it.
func (w *IdleWatchdog) Run(ctx context.Context) error {
	if w == nil || w.Window <= 0 {
		<-ctx.Done()
		return nil
	}
	t := time.NewTicker(w.tick())
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			if w.check(ctx, w.now()) {
				return &IdleKillError{Window: w.Window}
			}
		}
	}
}

// PollUntilDoneWithIdle is PollUntilDone under an idle watchdog. If wd fires
// first, the Job is cancelled via b.Cancel (bounded by cancelGrace) and the
// *IdleKillError is returned. wd == nil or a 0 window = plain PollUntilDone.
func PollUntilDoneWithIdle(ctx context.Context, b Backend, h *Handle, interval time.Duration,
	wd *IdleWatchdog, cancelGrace time.Duration, onUpdate func(NodeRunUpdate)) (NodeRunUpdate, error) {
	if wd == nil || wd.Window <= 0 {
		return PollUntilDone(ctx, b, h, interval, onUpdate)
	}
	pctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	wdDone := make(chan struct{})
	go func() {
		defer close(wdDone)
		if err := wd.Run(pctx); err != nil {
			cancel(err)
		}
	}()
	u, err := PollUntilDone(pctx, b, h, interval, onUpdate)
	cancel(nil)
	<-wdDone
	var ik *IdleKillError
	if err != nil && errors.As(context.Cause(pctx), &ik) {
		cctx, c := context.WithTimeout(context.Background(), cancelGrace)
		_ = b.Cancel(cctx, h)
		c()
		return u, ik
	}
	return u, err
}

// TaskContainerStarted reports whether the Job's `task` container is Running
// or already Terminated (i.e. past stage-in and image pull).
func (b *K8sBackend) TaskContainerStarted(ctx context.Context, h *Handle) bool {
	_, pod, err := b.taskPod(ctx, h)
	if err != nil || pod == nil {
		return false
	}
	cs := taskContainerStatus(pod)
	return cs != nil && (cs.State.Running != nil || cs.State.Terminated != nil)
}
