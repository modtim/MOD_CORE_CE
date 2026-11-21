package main

// k8s-reduction-engine W1: worker-side K8s dispatch + stack-observation hooks.
//
// Everything here is inert unless MOD_EXECUTOR_BACKEND=k8s (task dispatch) or
// MOD_STACK_BACKEND=k8s (stack observation). With both unset/docker,
// initK8sBackends does nothing and executeTask never enters runK8sTask.
// W7: even in k8s mode, a task only runs on k8s when the API's per-task gate
// set payload.executor_backend="k8s" (k8s-enabled tenant); else docker.

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/k8sclient"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const (
	k8sPollInterval = 2 * time.Second
	k8sLogTailLines = 200
	k8sCleanupGrace = 30 * time.Second
)

// k8sOutputScanBlockedReason is executor.go's unexported outputScanBlockedReason
// (the refusal reason a pre-store scan verdict carries). The API's /failed path
// already keys on this literal (mesh_loans.py, worker_host_exec_output.py), so
// the string — not a Go symbol — is the cross-package contract.
const k8sOutputScanBlockedReason = "output_scan_blocked"

var (
	// k8sTaskMode is true when the config asks for the K8s executor backend.
	// It stays true even if the backend could not be built, so tasks are
	// failed loudly instead of silently running on docker.
	k8sTaskMode       bool
	k8sTaskBackend    executor.Backend
	k8sTaskBackendErr error
)

// initK8sBackends builds the K8s task backend / stack observer once at start.
func initK8sBackends(cfg *config.Config, exec *executor.Executor, log *slog.Logger) {
	if cfg == nil {
		return
	}
	if cfg.IsK8sExecutorBackend() {
		k8sTaskMode = true
		k8sTaskBackend, k8sTaskBackendErr = executor.NewBackendFromConfig(cfg, exec, log)
		if k8sTaskBackendErr != nil {
			log.Error("K8s executor backend unavailable; node tasks will be FAILED (no docker fallback)",
				"error", k8sTaskBackendErr)
		}
	}
	if cfg.IsK8sStackBackend() {
		ns := executor.K8sStackNamespace()
		client, err := k8sclient.LoadClientset(cfg.KubeconfigPath)
		if err != nil {
			log.Error("K8s stack backend unavailable; stack observation will report no observation (no docker fallback)",
				"error", err)
			executor.UseK8sStackObserver(failingStackObserver{err: err})
			return
		}
		obs := executor.NewK8sStackObserver(client, ns)
		// Real CPU/mem when metrics-server serves metrics.k8s.io; otherwise
		// the observer reports usage as unavailable (W22).
		obs.Metrics = executor.NewK8sStackPodMetricsSource(client)
		// W32: live stack VRAM from dcgm-exporter when MOD_K8S_DCGM_EXPORTER_URL
		// is set; otherwise VRAM stays allocation-only.
		if d := executor.DCGMSourceFromEnv(); d != nil {
			obs.GPU = d
		}
		executor.UseK8sStackObserver(obs)
		scope := ns
		if scope == "" {
			scope = "per-tenant (" + executor.K8sTenantNamespaceSelector + ")"
		}
		log.Info("stack observer selected", "backend", "k8s", "namespace", scope)
	}
}

// shouldRunOnK8s is the per-task routing decision (W7). A task runs as a K8s
// Job only if this worker is in k8s task mode AND the API marked the task's
// tenant as entitled ("k8s"). Any other value — including empty from an older
// API — runs on the worker's docker executor (always built in main, so the
// fallback is real). If the worker is in k8s mode but its backend failed to
// build, an entitled task still goes to runK8sTask and is failed loudly there.
func shouldRunOnK8s(k8sMode bool, taskBackend string) bool {
	return k8sMode && strings.EqualFold(strings.TrimSpace(taskBackend), "k8s")
}

// failingStackObserver makes ObserveStacks return an error ("I could not
// look") rather than silently observing docker when k8s was requested.
type failingStackObserver struct{ err error }

func (f failingStackObserver) ObserveStacks(context.Context) ([]executor.StackObservation, error) {
	return nil, fmt.Errorf("k8s stack observer unavailable: %w", f.err)
}

// runK8sTask runs one node task as a K8s Job: TaskSpec -> Submit ->
// PollUntilDone -> log tail -> reporter Complete/Failed. ctx cancellation,
// the idle watchdog (W16: no log output for container_timeout once the task
// container is Running) or the legacy/absolute wall cap cancels the Job.
func runK8sTask(ctx context.Context, taskLog *slog.Logger, exec *executor.Executor,
	task *transport.Task, creds *transport.Credentials) {
	b := k8sTaskBackend
	if b == nil {
		msg := "executor backend is k8s but the Kubernetes backend is unavailable; refusing to run on docker"
		if k8sTaskBackendErr != nil {
			msg = fmt.Sprintf("%s: %v", msg, k8sTaskBackendErr)
		}
		taskLog.Error(msg)
		reporter.Failed(ctx, task.ID, msg, true, "", false, "")
		return
	}

	spec, err := exec.K8sTaskSpec(task, creds)
	if err != nil {
		reporter.Failed(ctx, task.ID, fmt.Sprintf("k8s task spec: %s", err), true, "", false, "")
		return
	}

	// W16: same liveness model as the docker path — container_timeout is the
	// IDLE window (inactivity-liveness on, the default) with an optional
	// MOD_CONTAINER_ABSOLUTE_MAX_S wall cap; legacy/sandbox = hard wall cap.
	live := executor.K8sTaskLiveness(task.Payload)
	runCtx, cancelRun := context.WithCancel(ctx)
	defer cancelRun()
	if live.HardCap > 0 {
		runCtx, cancelRun = context.WithTimeout(ctx, live.HardCap)
		defer cancelRun()
	}

	// W3: on the real K8s backend, stage S3 inputs/outputs in-pod (stage-in
	// initContainer + stage-out container), mirroring the Docker mc sidecars.
	kb, staged := b.(*executor.K8sBackend)
	var h *executor.Handle
	if staged {
		st, serr := executor.K8sStagingFromTask(task, creds)
		if serr != nil {
			reporter.Failed(ctx, task.ID, fmt.Sprintf("k8s staging spec: %s", serr), true, "", false, "")
			return
		}
		// Fortress 3.5 (968b): under the scan-staging posture the stage-out
		// uploads to the TRANSIENT prefix first (the API consults those bytes
		// before anything durable is written). Off / unset seam ⇒ st untouched,
		// byte-identical to today's direct-to-durable upload.
		if executor.K8sScanStagingActive(exec) {
			st.UploadCmd = executor.K8sStagingUploadCmd(task, exec)
		}
		h, err = kb.SubmitStaged(runCtx, spec, st)
	} else {
		h, err = b.Submit(runCtx, spec)
	}
	if err != nil {
		taskLog.Error("k8s submit failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("k8s submit: %s", err), true, "", false, "")
		return
	}
	taskLog.Info("k8s job submitted", "handle", h.ID, "image", spec.Image,
		"gpu", spec.GPU.Count, "mig", spec.GPU.MIGProfile, "egress", string(spec.Egress))
	// GB.EGR.10b: egress verdict for the result/failure reports. Nil for
	// non-allowlist tasks; "off" mode when the real K8s backend is absent.
	fqdnMode := executor.FQDNModeOff
	if kb != nil {
		fqdnMode = kb.FQDNMode
	}
	eg := executor.K8sEgressReport(spec, fqdnMode)
	started := time.Now()

	// W11: follow the task container's logs live (docker-path parity) —
	// best-effort, never fails the task, never blocks the final report.
	// W16: idle watchdog — clock starts once the task container is Running;
	// every live log line (and, W20, task CPU use) counts as activity.
	var wd *executor.IdleWatchdog
	if live.IdleWindow > 0 && staged {
		wd = &executor.IdleWatchdog{
			Window:  live.IdleWindow,
			Started: func(c context.Context) bool { return kb.TaskContainerStarted(c, h) },
			// W20: task-container CPU (Metrics API) also counts as activity;
			// degrades to log-only if metrics.k8s.io is absent/forbidden.
			// W32: OR task-pod GPU util (dcgm-exporter) when configured.
			Active: kb.TaskActivity(h, taskLog),
		}
	}
	stopStream := startK8sLogStream(runCtx, taskLog, b, h, task.ID, wd.Touch)

	u, perr := executor.PollUntilDoneWithIdle(runCtx, b, h, k8sPollInterval, wd, k8sCleanupGrace, func(u executor.NodeRunUpdate) {
		taskLog.Info("k8s task status", "status", u.Status, "reason", u.Reason, "exit_code", u.ExitCode)
	})
	stopStream()
	var idleErr *executor.IdleKillError
	if errors.As(perr, &idleErr) {
		// Job already cancelled by PollUntilDoneWithIdle. Same message and
		// retryability as docker's inactivity kill.
		taskLog.Error("k8s task killed for inactivity", "handle", h.ID, "reason", idleErr.Reason())
		reporter.Failed(ctx, task.ID, idleErr.Error(), true, k8sLogTail(b, h), false, "", eg)
		return
	}
	if perr != nil {
		// Cancelled (worker shutdown / preempt) or timed out: stop the Job.
		cctx, c := context.WithTimeout(context.Background(), k8sCleanupGrace)
		if cerr := b.Cancel(cctx, h); cerr != nil {
			taskLog.Warn("k8s job cancel failed", "handle", h.ID, "error", cerr)
		}
		c()
		msg := fmt.Sprintf("k8s task aborted: %s", perr)
		if errors.Is(perr, context.DeadlineExceeded) && ctx.Err() == nil {
			msg = fmt.Sprintf("container timed out after %ds", int(live.HardCap/time.Second))
		}
		reporter.Failed(ctx, task.ID, msg, true, k8sLogTail(b, h), false, "", eg)
		return
	}

	logTail := k8sLogTail(b, h)
	if u.Status != executor.NodeRunCompleted {
		taskLog.Error("k8s task failed", "reason", u.Reason, "exit_code", u.ExitCode, "error", u.ErrorMessage)
		reporter.Failed(ctx, task.ID, u.ErrorMessage, u.ShouldRetry, logTail, u.OOMKilled, u.ErrorDetail, eg)
		return
	}
	// Outputs: from stage-out's manifest (same []OutputFile shape as Docker's
	// uploadSidecar/readResultJSON). Non-staged backends report empty (never nil).
	outputs := []transport.OutputFile{}
	if staged {
		cctx, c := context.WithTimeout(ctx, 30*time.Second)
		outs, oerr := k8sCollectStaged(cctx, kb, h, task, creds, exec)
		c()
		if oerr != nil {
			taskLog.Error("k8s output collection failed", "error", oerr)
			// Fortress 3.5 (968b): a scan refusal is a deterministic VERDICT, so
			// it is reported retryable=false (retrying would double the in-pod
			// upload) with the reason as errorDetail — the k8s twin of the
			// Docker path's blocked Result (executor.go :1281).
			if strings.Contains(oerr.Error(), k8sOutputScanBlockedReason) {
				reporter.Failed(ctx, task.ID, fmt.Sprintf("k8s output collection: %s", oerr), false, logTail, false, oerr.Error(), eg)
				return
			}
			reporter.Failed(ctx, task.ID, fmt.Sprintf("k8s output collection: %s", oerr), true, logTail, false, oerr.Error(), eg)
			return
		}
		outputs = outs
	}
	// c11-J3b: token usage the collect above read off the stage-out logs.
	complete := transport.TaskResult{Outputs: outputs, LogTail: logTail, Egress: eg}
	if h != nil {
		complete.UsageTokens = h.UsageTokens
	}
	reporter.Complete(ctx, task.ID, complete)
	taskLog.Info("task completed", "backend", "k8s", "duration", time.Since(started).Round(time.Second))
}

// k8sLogStreamDrain bounds how long the final report waits for the live
// stream to ship its last lines after the Job reached a terminal state.
const k8sLogStreamDrain = 2 * time.Second

// k8sLogSender is the slice of transport.Transport the live K8s log stream
// needs — the same two calls the docker node path's cb.OnLogBatch makes.
type k8sLogSender interface {
	SendLog(ctx context.Context, taskID int, lines []string) error
	SendCheckpoints(ctx context.Context, taskID int, cps []transport.Checkpoint) error
}

// k8sCollectStaged is the completion-path collect, one indirection deep so the
// cmd/worker wiring test can drive runK8sTask without a cluster (the fake
// clientset's GetLogs always answers "fake logs", so no manifest can be fed to
// the real collect from here). The default IS executor.CollectStagedOutputsScanned
// — production behaviour is the scanned collect, unchanged.
var k8sCollectStaged = executor.CollectStagedOutputsScanned

// k8sLogSink is where live K8s task logs go. main wires it with
// setK8sLogSink(tp); while unset, lines still reach the worker's own log.
var k8sLogSink k8sLogSender

func setK8sLogSink(s k8sLogSender) { k8sLogSink = s }

// k8sLogBatchHandler mirrors main.go's node-path cb.OnLogBatch: SendLog to
// the Dashboard's live Logs panel + transport.ExtractCheckpoints progress.
// Errors are logged and swallowed.
func k8sLogBatchHandler(ctx context.Context, taskLog *slog.Logger, sink k8sLogSender, taskID int) func([]string) {
	return func(lines []string) {
		for _, l := range lines {
			taskLog.Info("container", "backend", "k8s", "log", l)
		}
		if sink == nil {
			return
		}
		if err := sink.SendLog(ctx, taskID, lines); err != nil {
			taskLog.Warn("container-log SendLog failed", "error", err, "lines", len(lines))
		}
		if cps := transport.ExtractCheckpoints(lines); len(cps) > 0 {
			if err := sink.SendCheckpoints(ctx, taskID, cps); err != nil {
				taskLog.Warn("checkpoint report failed", "error", err, "count", len(cps))
			}
		}
	}
}

// startK8sLogStream follows the task container's logs in the background.
// The returned stop waits at most k8sLogStreamDrain for the stream to end on
// its own (container terminated ⇒ EOF), then cancels it. Only the real
// K8sBackend streams; other backends get a no-op.
func startK8sLogStream(ctx context.Context, taskLog *slog.Logger, b executor.Backend,
	h *executor.Handle, taskID int, onActivity func()) (stop func()) {
	kb, ok := b.(*executor.K8sBackend)
	if !ok || h == nil {
		return func() {}
	}
	sctx, cancel := context.WithCancel(ctx)
	// Sends use the parent ctx so the last batch still ships while sctx is
	// being cancelled.
	s := kb.NewTaskLogStreamer(h, k8sLogBatchHandler(ctx, taskLog, k8sLogSink, taskID), taskLog)
	s.OnActivity = onActivity // W16 idle watchdog
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.Run(sctx)
	}()
	return func() {
		t := time.NewTimer(k8sLogStreamDrain)
		defer t.Stop()
		select {
		case <-done:
		case <-t.C:
		}
		cancel()
		<-done
	}
}

// k8sLogTail returns the last k8sLogTailLines lines of the task's logs,
// best-effort (own bounded context so it still works after a timeout).
func k8sLogTail(b executor.Backend, h *executor.Handle) string {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	rc, err := b.Logs(ctx, h)
	if err != nil || rc == nil {
		return ""
	}
	defer rc.Close()
	return tailLines(rc, k8sLogTailLines)
}

func tailLines(r io.Reader, n int) string {
	ring := make([]string, 0, n)
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		if len(ring) == n {
			ring = ring[1:]
		}
		ring = append(ring, sc.Text())
	}
	return strings.Join(ring, "\n")
}
