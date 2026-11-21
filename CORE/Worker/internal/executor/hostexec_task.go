package executor

// hx.7c — the `host-exec` task_type routing endpoint (opsx worker-host-exec).
//
// Executor.Run dispatches here when the API enqueues an ApiTaskQueue row with
// task_type "host-exec" (services/worker_host_exec_service.py): the payload
// carries {job_id, argv, cwd, env, run_as, timeout_s, mode} — NOT a
// ProcessingImage, so the generic container path would fail deep in image-pull
// exactly like the build task did before sz-build-queue.1.
//
// Output goes BACK the other way: every chunk is POSTed to
// /v1/workers/{worker_id}/exec/{job_id}/output (routers/worker_host_exec_output.py,
// hx.6) as {seq, stream, data_b64, final, exit_code}. Chunk bytes are scrubbed
// line-wise with the SAME scrubLogLine the native backend applies to task logs
// (hx.8) before they leave, and are never logged here.
//
// Two seams are wired by cmd/worker/main.go (hx.7b — the mod-host-exec unix-socket
// relay): hostExecRunner (how a host exec actually happens on THIS install) and
// hostExecSetAuth (the X-Worker-Name / X-Bootstrap-Token headers every other
// worker->API call carries). Both default to "not wired", which fails the task
// loudly rather than running anything unsandboxed or unauthenticated.
//
// internal/hostexec must NOT be imported here: hostexec/scrub.go imports THIS
// package (so the two share one definition of the scrub rules), so importing it
// back would be a package cycle. The runner seam is how main.go hands it over.

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// hostExecEnabledTunableKey is the platform_settings registry key gating the
// whole feature on the worker side (hx.2 registered it, default False). The API
// only ships tunables keys that have a live DB row, so an untouched fleet reads
// the default here: OFF. Fail closed, never open.
const hostExecEnabledTunableKey = "worker.host_exec_enabled"

// hostExecNodeIDPrefix matches the node_id the API stamps on a host-exec row
// (node_id=f"host-exec-{job.id}"). The poll response forwards task_payload
// verbatim and never the row's own task_type column, so a payload that does
// not stamp "task_type" into itself is still recognised by this prefix.
const hostExecNodeIDPrefix = "host-exec-"

// hostExecChunkBytes is the largest scrubbed chunk sent in one output POST.
// Matches hostexec.MaxFrameBytes so the worker and the helper stream the same
// frame size into the same ingest endpoint.
const hostExecChunkBytes = 32 * 1024

// hostExecPostRetries is how many times one output POST is retried with
// exponential backoff before the stream is declared broken. A dropped chunk
// must never look like a silent success: the job's output is the evidence.
const hostExecPostRetries = 3

// hostExecVerdictMaxBytes caps the ingest verdict body (a few JSON keys). A body
// past the cap is refused as a failed post, never decoded from a truncated read.
const hostExecVerdictMaxBytes = 64 << 10

// hostExecBlockedReason is the terminal reason the sink records when the API's
// output scan (958a) rejects a chunk: the ingest endpoint answers 200 with
// blocked=true / blocked_reason="output_scan_blocked" and closes the job failed.
const hostExecBlockedReason = "output_scan_blocked"

// hostExecOutputChunkResponse is the ingest endpoint's verdict on one POST
// (routers/worker_host_exec_output.py OutputChunkResponse). Only the two scan
// keys matter here; both are optional — an API that never sends them decodes as
// "not blocked", so today's behaviour is untouched.
type hostExecOutputChunkResponse struct {
	Accepted      *bool
	Status        string
	Blocked       *bool  `json:"blocked"`
	BlockedReason string `json:"blocked_reason"`
}

// hostExecRespBlocked reports whether one ingest response says the chunk was
// blocked by the API's output scan. A body that fails to parse (an old API
// answering a bare 200) is "not blocked".
func hostExecRespBlocked(raw []byte) bool {
	var verdict hostExecOutputChunkResponse
	if err := json.Unmarshal(raw, &verdict); err != nil {
		return false
	}
	return verdict.Blocked != nil && *verdict.Blocked
}

// hostExecFinalPostBudget bounds the END-of-run posts (the pending-chunk flush
// plus the closing frame). They run on a context detached from the task's
// (hx.11b): a cancelled task must still close its job, so the task ctx cannot
// gate them — but they must not hang worker shutdown either, hence one shared
// deadline well under the 30s shutdown cap.
const hostExecFinalPostBudget = 15 * time.Second

// hostExecAPIPrefix is transport.apiPrefix restated (that const is package-local
// to transport, so this package cannot read it). Every worker->API call lives
// under it.
const hostExecAPIPrefix = "/v1"

type HostExecPayload struct {
	JobID    int               `json:"job_id"`
	Argv     []string          `json:"argv"`
	Cwd      string            `json:"cwd"`
	Env      map[string]string `json:"env"`
	RunAs    string            `json:"run_as"`
	TimeoutS int               `json:"timeout_s"`
	Mode     string            `json:"mode"`
}

// HostExecRequest is the runner seam's view of one host exec — the same shape
// hostexec.Request carries, restated here because this package cannot import
// hostexec (cycle, above).
type HostExecRequest struct {
	Argv    []string
	Cwd     string
	Env     map[string]string
	RunAs   string
	Timeout time.Duration
}

// HostExecFrame is one chunk of a run's stdout or stderr.
type HostExecFrame struct {
	Stream string
	Data   []byte
}

// hostExecRunner is the exec seam (hx.7b). main.go wires it to the mod-host-exec
// unix-socket helper (cmd/host-exec-helper, reached through
// internal/hostexec/protocol.go) on a containerized worker, and to hostexec.Run
// directly on a systemd-binary install. Default: refuse.
var hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
	return -1, fmt.Errorf("host-exec runner not wired (hx.7b relay pending)")
}

// hostExecSetAuth stamps the worker's identity headers on an output POST.
// main.go wires it from cfg.WorkerName / cfg.BootstrapToken, exactly like
// transport.APITransport.setHeaders. nil (the default) means "not wired" and
// the task fails rather than posting unauthenticated.
var hostExecSetAuth func(*http.Request)

// hostExecWorkerID is the numeric WorkerLicense id this worker is enrolled as
// — the output endpoint's path parameter. main.go wires it (hx.7b); empty
// means the relay is not wired.
var hostExecWorkerID = ""

// hostExecPayloadOf is the payload seam. transport.TaskPayload models only the
// container-path keys, so the host-exec keys never survive the poll decode
// today; hx.7b wires this to the raw poll payload once the transport carries
// them. Default nil = "not delivered", which fails the task rather than
// inventing an argv.
var hostExecPayloadOf = func(task *transport.Task) *HostExecPayload { return nil }

// hostExecDisabledMsg is the stable failure string for the off switch.
const hostExecDisabledMsg = "host-exec disabled (worker.host_exec_enabled off)"

// hostExecSandboxMsg is the fail-closed answer for mode "sandboxed" before
// hx.17 wires the sandbox wrapper. A sandboxed job must NEVER run unsandboxed.
const hostExecSandboxMsg = "host-exec sandboxed mode not wired (TODO hx.17-wire): refusing to run unsandboxed"

// hostExecSandboxWrap is the sandbox-wrapper seam (hx.17-wire). cmd/worker/main.go
// wires it to hostexec/sandbox.go's systemd-run wrapper (SandboxArgv): it receives
// the plain HostExecRequest the payload describes and returns the wrapped one
// (unshare/rlimit/cgroup argv in front), or an error. nil = "not wired", which
// keeps mode "sandboxed" refused fail-closed: a sandboxed job must NEVER run
// unsandboxed. This package cannot import hostexec (cycle, above), so the wrapper
// is handed over exactly like the runner seam.
var hostExecSandboxWrap func(req HostExecRequest) (HostExecRequest, error)

// SetHostExecSandboxWrapper wires the sandbox-wrapper seam (hx.17-wire). cmd/worker
// calls it once at startup with a function built on hostexec.SandboxArgv(opts
// SandboxOptions{Workspace, Inaccessible, TimeoutS, Env}); passing nil unwires it
// and mode "sandboxed" goes back to failing closed.
func SetHostExecSandboxWrapper(f func(HostExecRequest) (HostExecRequest, error)) {
	hostExecSandboxWrap = f
}

// hostExecContainerMsg is the fail-closed answer for a containerized worker
// with no helper socket yet (hx.7b).
const hostExecContainerMsg = "host-exec unavailable in container without helper (hx.7b relay pending)"

// hostExecPayloadMissingMsg is the fail-closed answer when the poll never
// delivered the host-exec payload keys.
const hostExecPayloadMissingMsg = "host-exec payload not delivered by poll (hx.7b)"

// hostExecContainerized is the container-detection seam (stacksecrets.go's
// insideContainer reads /proc, which a test cannot steer). hx.7b keeps the
// default; tests force either outcome.
var hostExecContainerized = insideContainer

// hostExecHTTPDo is the HTTP seam (same shape as checkpoint_transfer.go's
// checkpointHTTPDo) so a test can stand an httptest server for the ingest
// endpoint instead of the real API.
var hostExecHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
	client := &http.Client{}
	return client.Do(req)
}

// hostExecOf reports whether a task is a host-exec task.
func hostExecOf(task *transport.Task) bool {
	if task == nil {
		return false
	}
	if task.Payload.TaskType == "host-exec" {
		return true
	}
	return strings.HasPrefix(task.NodeID, hostExecNodeIDPrefix)
}

// hostExecJobIDFromNodeID recovers the job id the API stamped into node_id.
func hostExecJobIDFromNodeID(nodeID string) int {
	if !strings.HasPrefix(nodeID, hostExecNodeIDPrefix) {
		return 0
	}
	n, err := strconv.Atoi(strings.TrimPrefix(nodeID, hostExecNodeIDPrefix))
	if err != nil {
		return 0
	}
	return n
}

// hostExecEnabled is the worker-side gate (see hostExecEnabledTunableKey).
func hostExecEnabled(e *Executor) bool {
	if e.tunables == nil {
		return false
	}
	return e.tunables.Bool(hostExecEnabledTunableKey, "", false)
}

// hostExecScrub redacts one streamed chunk line-wise with the SAME rules the
// native backend applies to task logs (scrubLogLine, hx.8). Chunk boundaries
// are byte boundaries, so a chunk's trailing partial line is scrubbed as its
// own line: every rule is per-token, never per-line-stateful.
func hostExecScrub(data []byte) []byte {
	lines := strings.Split(string(data), "\n")
	for i, l := range lines {
		lines[i] = scrubLogLine(l, "")
	}
	return []byte(strings.Join(lines, "\n"))
}

// HostExecSink batches scrubbed output into <=hostExecChunkBytes chunks and
// POSTs them to the ingest endpoint with strictly increasing seq, so the API's
// per-seq dedup (record_chunk) can never see a gap or a reused slot.
type HostExecSink struct {
	e        *Executor
	log      *slog.Logger
	workerID string
	jobID    int
	seq      int
	pending  map[string][]byte
	failed   bool
	// blocked records that the ingest endpoint answered blocked=true for a
	// chunk (958a's output scan): the API already closed the job failed, so the
	// sink stops streaming, stops the host process through the existing cancel
	// path (cancelRun, the ctx-cancel the runner already turns into SIGTERM/
	// SIGKILL) and reports "output_scan_blocked" as the task's terminal reason.
	// blockedReason is that recorded reason; cancelRun is the stop hook handle
	// handleHostExecTask registers (nil = nothing to stop, e.g. a stub runner).
	blocked bool
	// (fields below)
	blockedReason string
	cancelRun     context.CancelFunc
	mu            sync.Mutex
	ctx           context.Context
}

// emit scrubs and buffers one frame, flushing whole chunks as they fill.
func (s *HostExecSink) emit(stream string, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failed {
		return fmt.Errorf("host-exec output stream already broken")
	}
	if s.blocked {
		// The API's output scan rejected an earlier chunk: the job is closed
		// failed server-side. Stop streaming — no further chunks, no error.
		return nil
	}
	if stream != "stdout" && stream != "stderr" {
		return fmt.Errorf("host-exec: unknown stream %q", stream)
	}
	s.pending[stream] = append(s.pending[stream], hostExecScrub(data)...)
	for len(s.pending[stream]) >= hostExecChunkBytes {
		chunk := s.pending[stream][:hostExecChunkBytes]
		s.pending[stream] = s.pending[stream][hostExecChunkBytes:]
		if err := s.post(s.ctx, stream, chunk, false, nil); err != nil {
			return err
		}
		if s.blocked {
			// The API's scan closed the job: drop whatever is still buffered
			// and stop. emit() keeps answering nil from here on.
			s.pending[stream] = nil
			return nil
		}
	}
	return nil
}

// post sends one chunk (or, when final, the closing frame) with retries.
// Chunk bytes are never logged on any path — only the status code.
//
// ctx is the context THIS post runs on: streaming chunks pass the task ctx
// (a cancelled run stops streaming promptly), the end-of-run flush + closing
// frame pass a detached one (hx.11b) so a cancelled task still closes its job.
// 404/409 on the closing frame means the API already ended the job (cancelled
// server-side): terminal, logged at info, not retried, not a failure.
func (s *HostExecSink) post(ctx context.Context, stream string, data []byte, final bool, exitCode *int) error {
	body := map[string]any{
		"seq":      s.seq,
		"stream":   stream,
		"data_b64": base64.StdEncoding.EncodeToString(data),
	}
	if final {
		body["final"] = true
		if exitCode != nil {
			body["exit_code"] = *exitCode
		}
	}
	raw, err := json.Marshal(body)
	if err != nil {
		s.failed = true
		return err
	}
	base := strings.TrimRight(s.e.apiURL, "/")
	if base == "" {
		s.failed = true
		return fmt.Errorf("host-exec: no apiURL to post output to")
	}
	if !strings.HasSuffix(base, hostExecAPIPrefix) {
		base += hostExecAPIPrefix
	}
	path := fmt.Sprintf("/workers/%s/exec/%d/output", s.workerID, s.jobID)
	var lastErr error
	delay := 200 * time.Millisecond
	for attempt := 0; attempt < hostExecPostRetries; attempt++ {
		if attempt > 0 {
			time.Sleep(delay)
			delay *= 2
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+path, bytes.NewReader(raw))
		if err != nil {
			lastErr = err
			continue
		}
		if hostExecSetAuth != nil {
			hostExecSetAuth(req)
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := hostExecHTTPDo(ctx, req)
		if err != nil {
			lastErr = err
			continue
		}
		// 958a: under enforce the ingest endpoint answers 200 with
		// {"accepted": false, "status": "failed", "blocked": true,
		// "blocked_reason": "output_scan_blocked"} and closes the job. Old APIs
		// that never send those keys decode as "not blocked" (absent -> false),
		// so behaviour is byte-identical when the keys are missing.
		// The verdict body is tiny (a few JSON keys); the ingest endpoint never
		// streams a payload back, so reading it whole is bounded.
		respRaw, _ := io.ReadAll(io.LimitReader(resp.Body, hostExecVerdictMaxBytes+1))
		resp.Body.Close()
		// A verdict past the cap is refused, never decoded from a truncated body
		// (a truncated body would decode as "not blocked").
		if len(respRaw) > hostExecVerdictMaxBytes {
			lastErr = fmt.Errorf("host-exec output post: verdict body over %d bytes", hostExecVerdictMaxBytes)
			continue
		}
		if resp.StatusCode < 400 && hostExecRespBlocked(respRaw) {
			// Terminal: the API already closed the job failed. No retry, no
			// further chunks, and the host process is stopped through the
			// runner's existing cancel/timeout path (cancelRun -> the ctx the
			// runner watches -> SIGTERM group, SIGKILL after killGrace).
			s.blocked = true
			s.blockedReason = hostExecBlockedReason
			if s.cancelRun != nil {
				s.cancelRun()
			}
			s.log.Error("host-exec output blocked by API scan: stopping stream and cancelling run",
				"job_id", s.jobID, "stream", stream, "seq", s.seq, "reason", hostExecBlockedReason)
			return nil
		}
		if final && (resp.StatusCode == http.StatusConflict || resp.StatusCode == http.StatusNotFound) {
			// The job is already gone / already closed server-side (a task cancel
			// can end it before the closing frame arrives): nothing we send can
			// land, and retrying is pointless. Terminal, not a failure.
			s.log.Info("host-exec output post: job already closed server-side",
				"job_id", s.jobID, "stream", stream, "final", final, "http", resp.StatusCode)
			s.seq++
			return nil
		}
		if resp.StatusCode >= 400 {
			lastErr = fmt.Errorf("host-exec output post: http %d", resp.StatusCode)
			continue
		}
		s.seq++
		return nil
	}
	s.failed = true
	return lastErr
}

// flushFinal sends whatever is left buffered on each stream, then the closing
// frame (final=true + exit_code) on stdout. The API closes the job on that
// frame: succeeded iff exit_code == 0, else failed.
func (s *HostExecSink) flushFinal(exitCode int) error {
	// 963: the API's output scan already closed the job failed and the sink
	// stopped streaming + cancelled the run. There is nothing left to close:
	// skip the buffered flush and the closing frame entirely.
	if s.blocked {
		return nil
	}
	// hx.11b: the end-of-run posts run on a context DETACHED from the task's
	// (context.WithoutCancel keeps the ctx values — auth headers, anything a
	// middleware layer hangs on — but not its cancellation). A task cancel /
	// worker shutdown must still close the job, otherwise the API leaves it
	// RUNNING forever. Bounded by hostExecFinalPostBudget so a dead API can
	// never hang shutdown.
	finalCtx, cancelFinal := context.WithTimeout(context.WithoutCancel(s.ctx), hostExecFinalPostBudget)
	defer cancelFinal()
	for _, stream := range []string{"stdout", "stderr"} {
		rest := s.pending[stream]
		if len(rest) == 0 {
			continue
		}
		s.pending[stream] = nil
		if err := s.post(finalCtx, stream, rest, false, nil); err != nil {
			return err
		}
	}
	return s.post(finalCtx, "stdout", nil, true, &exitCode)
}

// hostExecDefaultTimeoutS mirrors the API's worker.host_exec_default_timeout_s
// default (services/worker_host_exec_settings.py): the payload always carries
// timeout_s, so this only covers a payload that omitted it.
const hostExecDefaultTimeoutS = 600

// handleHostExecTask is the routing endpoint Executor.Run dispatches to
// (executor.go, next to the build branch). Same signature as handleBuildTask:
// a non-nil error fails the task (the caller reports it via
// reporter.Failed), never a silent success or a silent drop.
func (e *Executor) handleHostExecTask(ctx context.Context, task *transport.Task, creds *transport.Credentials, started time.Time, cb Callbacks) (*Result, error) {
	taskID := 0
	if task != nil {
		taskID = task.ID
	}
	logger := e.log
	if logger == nil {
		logger = slog.Default()
	}

	if !hostExecEnabled(e) {
		logger.Error("host-exec task refused: feature off",
			"task_id", taskID, "task_type", "host-exec", "tunable", hostExecEnabledTunableKey)
		return nil, fmt.Errorf("%s (task_id=%d)", hostExecDisabledMsg, taskID)
	}

	p := hostExecPayloadOf(task)
	if p == nil {
		logger.Error("host-exec task refused: payload keys not delivered",
			"task_id", taskID, "task_type", "host-exec")
		return nil, fmt.Errorf("%s (task_id=%d)", hostExecPayloadMissingMsg, taskID)
	}

	jobID := p.JobID
	if jobID == 0 {
		jobID = hostExecJobIDFromNodeID(task.NodeID)
	}
	if jobID == 0 {
		return nil, fmt.Errorf("host-exec: no job_id in payload or node_id (task_id=%d)", taskID)
	}

	sandboxed := p.Mode == "sandboxed"
	if !sandboxed && p.Mode != "direct" {
		return nil, fmt.Errorf("host-exec: unknown mode %q (task_id=%d)", p.Mode, taskID)
	}
	// FAIL CLOSED on sandboxed mode until hx.17 wires hostexec/sandbox.go's
	// systemd-run wrapper: a sandboxed job must NEVER run unsandboxed.
	if sandboxed && hostExecSandboxWrap == nil {
		logger.Error("host-exec sandboxed task refused: wrapper not wired",
			"task_id", taskID, "job_id", jobID, "mode", "sandboxed")
		return nil, fmt.Errorf("%s (task_id=%d)", hostExecSandboxMsg, taskID)
	}
	if hostExecContainerized() {
		logger.Error("host-exec direct task refused: worker is containerized, no helper socket",
			"task_id", taskID, "job_id", jobID, "mode", p.Mode)
		return nil, fmt.Errorf("%s (task_id=%d)", hostExecContainerMsg, taskID)
	}
	// Output has nowhere authenticated to go: running the job and losing its
	// output would make the task unverifiable.
	if hostExecWorkerID == "" || hostExecSetAuth == nil {
		return nil, fmt.Errorf("host-exec relay not wired (hx.7b) (task_id=%d)", taskID)
	}
	if len(p.Argv) == 0 {
		return nil, fmt.Errorf("host-exec: empty argv (task_id=%d)", taskID)
	}

	timeoutS := p.TimeoutS
	if timeoutS <= 0 {
		timeoutS = hostExecDefaultTimeoutS
	}
	req := HostExecRequest{
		Argv:    p.Argv,
		Cwd:     p.Cwd,
		Env:     p.Env,
		RunAs:   p.RunAs,
		Timeout: time.Duration(timeoutS) * time.Second,
	}
	if sandboxed {
		logger.Info("host-exec sandboxed task starting",
			"task_id", taskID, "job_id", jobID, "mode", "sandboxed")
		wrapped, wrapErr := hostExecSandboxWrap(req)
		if wrapErr != nil {
			// The wrapper refused: run NOTHING.
			logger.Error("host-exec sandboxed task refused: sandbox wrapper failed",
				"task_id", taskID, "job_id", jobID, "mode", "sandboxed", "error", wrapErr)
			return nil, fmt.Errorf("host-exec sandbox wrapper: %w (task_id=%d, job_id=%d)", wrapErr, taskID, jobID)
		}
		if len(wrapped.Argv) == 0 {
			logger.Error("host-exec sandboxed task refused: wrapper returned empty argv",
				"task_id", taskID, "job_id", jobID, "mode", "sandboxed")
			return nil, fmt.Errorf("host-exec sandbox wrapper returned empty argv (task_id=%d, job_id=%d)", taskID, jobID)
		}
		req = wrapped
	}

	// 963: the sink's stop hook. handleHostExecTask wraps the task ctx in
	// WithCancel and hands the runner the CHILD ctx, so cancelRun() is exactly
	// the path a task cancel / timeout already takes (runner.go: runCtx.Done
	// -> exec's Cancel -> SIGTERM to the whole process group, SIGKILL after
	// killGrace). Nothing new is invented here.
	runnerCtx, cancelRunner := context.WithCancel(ctx)
	defer cancelRunner()
	sink := HostExecSink{
		e:         e,
		log:       logger,
		workerID:  hostExecWorkerID,
		jobID:     jobID,
		pending:   map[string][]byte{},
		ctx:       ctx,
		cancelRun: cancelRunner,
	}
	// hostExecLogTailLines is how many scrubbed lines the task result carries
	// back as log_tail — the same shape the container path reports.
	const hostExecLogTailLines = 200

	var emitErr error
	var tail []string
	exitCode, runErr := hostExecRunner(runnerCtx, req, func(f HostExecFrame) {
		if len(f.Data) == 0 {
			return
		}
		scrubbed := hostExecScrub(f.Data)
		if err := sink.emit(f.Stream, scrubbed); err != nil && emitErr == nil {
			emitErr = err
		}
		for _, line := range strings.Split(string(scrubbed), "\n") {
			if line == "" {
				continue
			}
			if len(tail) >= hostExecLogTailLines {
				tail = tail[1:]
			}
			tail = append(tail, line)
		}
		if cb.OnLogBatch != nil {
			cb.OnLogBatch(strings.Split(string(scrubbed), "\n"))
		}
	})

	// The closing frame carries the exit code even when the run itself failed
	// (a runner error still means "this job ended"): the API's job status must
	// not stay RUNNING on a dead task.
	finalErr := sink.flushFinal(exitCode)
	// 963: the API's output scan blocked a chunk: the job is already closed
	// failed server-side, the run was cancelled through the existing cancel
	// path. The task's terminal reason is the scan's, not the runner's
	// cancellation error — report it first.
	if sink.blocked {
		return &Result{
			LogTail:  strings.Join(tail, "\n"),
			Duration: time.Since(started),
		}, fmt.Errorf("%s (task_id=%d, job_id=%d)", sink.blockedReason, taskID, jobID)
	}
	if runErr != nil {
		logger.Error("host-exec run failed",
			"task_id", taskID, "job_id", jobID, "exit_code", exitCode, "error", runErr)
		return nil, fmt.Errorf("host-exec: %w (task_id=%d, job_id=%d)", runErr, taskID, jobID)
	}
	if emitErr != nil {
		logger.Error("host-exec output stream broken",
			"task_id", taskID, "job_id", jobID, "error", emitErr)
		return nil, fmt.Errorf("host-exec output stream: %w (task_id=%d, job_id=%d)", emitErr, taskID, jobID)
	}
	if finalErr != nil {
		logger.Error("host-exec final output frame failed",
			"task_id", taskID, "job_id", jobID, "error", finalErr)
		return nil, fmt.Errorf("host-exec final output frame: %w (task_id=%d, job_id=%d)", finalErr, taskID, jobID)
	}
	if exitCode != 0 {
		// A non-zero exit is a FAILED task (the container path reports the
		// same way), but the output already streamed, so the job's own status
		// is the record of what ran.
		return &Result{
			LogTail:  strings.Join(tail, "\n"),
			Duration: time.Since(started),
		}, fmt.Errorf("host-exec exited %d (task_id=%d, job_id=%d)", exitCode, taskID, jobID)
	}
	return &Result{
		LogTail:  strings.Join(tail, "\n"),
		Duration: time.Since(started),
	}, nil
}
