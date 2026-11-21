package executor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// backendEnvVar selects which Backend the worker uses to run ephemeral node
// tasks. Read once at worker start (k8s-reduction-engine design.md, Seam 1
// "Config / tiering").
const backendEnvVar = "MOD_EXECUTOR_BACKEND"

// SelectBackend returns the Backend this worker should use to run node
// tasks, per MOD_EXECUTOR_BACKEND (default "docker") and KUBECONFIG.
//
// Deprecated: retired by k8s-reduction-engine W1 — the worker selects its
// backend from the loaded config via NewBackendFromConfig. Kept only so any
// out-of-tree caller still compiles; it now delegates there, so a k8s
// request with no usable cluster returns nil (logged) instead of silently
// falling back to docker.
func SelectBackend(exec *Executor, log *slog.Logger) Backend {
	cfg := &config.Config{ExecutorBackend: config.BackendDocker, KubeconfigPath: os.Getenv("KUBECONFIG")}
	if strings.EqualFold(strings.TrimSpace(os.Getenv(backendEnvVar)), string(config.BackendK8s)) {
		cfg.ExecutorBackend = config.BackendK8s
	}
	b, err := NewBackendFromConfig(cfg, exec, log)
	if err != nil {
		return nil
	}
	return b
}

// DockerBackend implements Backend on top of the existing Executor — the
// CURRENT Docker execution path (ContainerCreate/start/poll/collect,
// env injection, netpol via SelectNetworkMode, GPU DeviceRequests, S3
// staging via the download/upload sidecars) is untouched in executor.go;
// this type only adapts that synchronous, single-shot Executor.Run call to
// the async Submit/Poll/Logs/Cancel shape the Backend interface needs so
// K8sBackend (T2) can be dropped in beside it later.
type DockerBackend struct {
	exec *Executor

	mu   sync.Mutex
	runs map[string]*dockerRun
}

// dockerRun tracks one Submit()'d task for later Poll/Logs/Cancel calls.
type dockerRun struct {
	mu          sync.Mutex
	status      TaskStatus
	containerID string   // set once Executor.Run's OnContainerCreated fires
	logLines    []string // recent OnLogBatch output, used if the container is already gone
	cancel      context.CancelFunc
}

const dockerRunLogCap = 4000 // lines kept in-memory per run for Logs() fallback

// nextDockerTaskID hands out synthetic transport.Task IDs for tasks that
// arrive via the Backend interface (as opposed to the API-assigned IDs the
// normal claim/dispatch path in cmd/worker uses). Only used for logging
// inside Executor.Run; never round-tripped anywhere.
var dockerBackendTaskSeq int64

func nextDockerTaskID() int {
	return int(atomic.AddInt64(&dockerBackendTaskSeq, 1))
}

// NewDockerBackend wraps an existing Executor as a Backend.
func NewDockerBackend(exec *Executor) *DockerBackend {
	return &DockerBackend{exec: exec, runs: make(map[string]*dockerRun)}
}

var _ Backend = (*DockerBackend)(nil)

// Submit translates spec into a transport.Task/Credentials pair and starts
// Executor.Run in the background (Run blocks until the task finishes, but
// Submit must not), returning immediately with a Handle for Poll/Logs/Cancel.
//
// The run is intentionally NOT tied to ctx's lifetime: ctx here governs only
// the Submit call itself (matching a K8s Job create call), not the task's
// execution — otherwise a caller whose own context is scoped to an HTTP
// request would kill every in-flight task the moment that request returned.
// Cancel (below) is the only way to stop a submitted run early.
func (d *DockerBackend) Submit(_ context.Context, spec *TaskSpec) (*Handle, error) {
	if spec == nil {
		return nil, errors.New("executor: DockerBackend.Submit: nil TaskSpec")
	}

	id := newInstanceID()
	runCtx, cancel := context.WithCancel(context.Background())
	run := &dockerRun{
		status: TaskStatus{Phase: TaskRunning},
		cancel: cancel,
	}

	d.mu.Lock()
	d.runs[id] = run
	d.mu.Unlock()

	task, creds := taskSpecToTask(spec, id)
	cb := Callbacks{
		OnContainerCreated: func(containerID string) {
			run.mu.Lock()
			run.containerID = containerID
			run.mu.Unlock()
		},
		OnLogBatch: func(lines []string) {
			run.mu.Lock()
			run.logLines = append(run.logLines, lines...)
			if over := len(run.logLines) - dockerRunLogCap; over > 0 {
				run.logLines = run.logLines[over:]
			}
			run.mu.Unlock()
		},
	}

	go func() {
		result, err := d.exec.Run(runCtx, task, creds, cb)
		run.mu.Lock()
		defer run.mu.Unlock()
		if err != nil {
			run.status = TaskStatus{
				Phase:    TaskFailed,
				ExitCode: exitCodeFromRunError(result, err),
				Err:      err.Error(),
			}
			return
		}
		run.status = TaskStatus{Phase: TaskSucceeded, ExitCode: 0}
	}()

	return &Handle{Backend: "docker", ID: id}, nil
}

// Poll reports the run's current state. Never blocks on the task itself.
func (d *DockerBackend) Poll(_ context.Context, h *Handle) (*TaskStatus, error) {
	run, err := d.lookup(h)
	if err != nil {
		return nil, err
	}
	run.mu.Lock()
	defer run.mu.Unlock()
	status := run.status
	return &status, nil
}

// Logs returns the task's combined stdout/stderr. While the processing
// container still exists it re-fetches full logs straight from Docker
// (matching what getContainerLogs already returns elsewhere); once it's
// gone (removed by the existing cleanup path) it falls back to the
// OnLogBatch lines captured during the run.
func (d *DockerBackend) Logs(ctx context.Context, h *Handle) (io.ReadCloser, error) {
	run, err := d.lookup(h)
	if err != nil {
		return nil, err
	}
	run.mu.Lock()
	containerID := run.containerID
	joined := strings.Join(run.logLines, "\n")
	run.mu.Unlock()

	if containerID != "" {
		if full := d.exec.getContainerLogs(ctx, containerID, 0); full != "" {
			return io.NopCloser(strings.NewReader(full)), nil
		}
	}
	return io.NopCloser(strings.NewReader(joined)), nil
}

// Cancel best-effort stops the run: cancels the context Executor.Run is
// running under and, if the processing container has already been created,
// kills it directly via the same killContainer path the existing
// timeout/eviction logic uses.
func (d *DockerBackend) Cancel(_ context.Context, h *Handle) error {
	run, err := d.lookup(h)
	if err != nil {
		return err
	}
	run.mu.Lock()
	containerID := run.containerID
	run.cancel()
	run.mu.Unlock()

	if containerID != "" {
		d.exec.killContainer(containerID, "cancelled via Backend.Cancel")
	}
	return nil
}

func (d *DockerBackend) lookup(h *Handle) (*dockerRun, error) {
	if h == nil {
		return nil, errors.New("executor: DockerBackend: nil Handle")
	}
	d.mu.Lock()
	run, ok := d.runs[h.ID]
	d.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("executor: DockerBackend: unknown task handle %q", h.ID)
	}
	return run, nil
}

// taskSpecToTask adapts a backend-agnostic TaskSpec into the transport.Task
// shape Executor.Run expects, reusing the exact same env-injection/netpol
// machinery (SelectNetworkMode, runContainerWithCreds) untouched.
func taskSpecToTask(spec *TaskSpec, correlationID string) (*transport.Task, *transport.Credentials) {
	env := make(map[string]string, len(spec.Env)+1)
	for k, v := range spec.Env {
		env[k] = v
	}
	if spec.Egress == EgressUserEgress {
		// Same marker SelectNetworkMode already treats as "route to the
		// egress network" for system containers — see netpol.go.
		if _, ok := env["NEEDS_INTERNET"]; !ok {
			env["NEEDS_INTERNET"] = "1"
		}
	}

	payload := transport.TaskPayload{
		ProcessingImage: spec.Image,
		ContainerEnv:    env,
		GPURequired:     spec.GPU.Count > 0,
		MemoryLimitMB:   int(spec.Resources.MemoryMB),
		CPULimit:        spec.Resources.CPUCores,
		ShmSizeMB:       spec.Resources.ShmSizeMB,
	}

	task := &transport.Task{
		ID:           nextDockerTaskID(),
		NodeID:       correlationID,
		AttemptCount: 1,
		MaxAttempts:  1,
		Payload:      payload,
	}
	return task, &transport.Credentials{}
}

// exitCodeFromRunError best-effort recovers a numeric exit code from an
// Executor.Run failure. The exact code is only carried as a typed
// *SidecarExitError for sidecar (download/upload) failures; a processing
// container's own non-zero exit is folded into the error string with no
// typed wrapper, so it falls back to a generic non-zero code (137 when the
// kernel OOM-killed it, per Result.OOMKilled — 1 otherwise).
func exitCodeFromRunError(result *Result, err error) int {
	var sidecarErr *SidecarExitError
	if errors.As(err, &sidecarErr) {
		return sidecarErr.ExitCode
	}
	if result != nil && result.OOMKilled {
		return 137
	}
	return 1
}
