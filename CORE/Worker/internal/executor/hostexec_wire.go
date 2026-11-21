package executor

// hx.7e — the host-exec wiring surface (opsx worker-host-exec).
//
// hostexec_task.go's four seams (hostExecRunner / hostExecSetAuth / hostExecWorkerID
// / hostExecPayloadOf) default to "not wired" and FAIL a host-exec task loudly
// rather than running anything unsandboxed or unauthenticated. This file is the
// one place that hands them over: cmd/worker/main.go builds a HostExecWiring
// (wireHostExec, below) and calls ConfigureHostExec once at boot.
//
// This package must NOT import internal/hostexec: hostexec/scrub.go imports THIS
// package (one shared definition of the scrub rules), so importing it back would
// be a package cycle. The runner seam is how main.go hands hostexec over instead.
//
// The payload seam is wired HERE (not in main.go) because hx.7d already taught
// transport.TaskPayload to decode the flat host-exec keys (job_id/argv/cwd/env/
// run_as/timeout_s/mode, top level — worker_host_exec_service.py enqueues them
// with no "host_exec" wrapper), so the mapping payload->HostExecPayload is a
// pure transport<->executor concern with no hostexec import involved.

import (
	"context"
	"net/http"
	"strconv"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// HostExecWiring is everything main.go hands to the executor seams. Every field
// is optional: a nil/empty field leaves its fail-closed default in place, so a
// half-wired install still refuses host-exec tasks instead of running them
// unauthenticated or with an invented argv.
type HostExecWiring struct {
	Runner   func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error)
	SetAuth  func(*http.Request)
	WorkerID string
	// ViaHelper reports that Runner IS the mod-host-exec unix-socket relay —
	// cmd/worker sets this when it chose hostExecRelayRunner over the native
	// (systemd-binary) runner. In that case the Runner is itself the
	// container-safe path, so hostExecContainerized's "no helper socket"
	// refusal (hostexec_task.go's own container guard, aimed at a container
	// with NO runner at all) must stand down. It is ignored when Runner is
	// nil: a half-wired install with ViaHelper set but no Runner still fails
	// closed.
	ViaHelper bool
}

// ConfigureHostExec sets the executor's host-exec seams from w. nil/empty fields
// leave the fail-closed default (hostexec_task.go's own initial values) untouched.
func ConfigureHostExec(w HostExecWiring) {
	if w.Runner != nil {
		hostExecRunner = w.Runner
	}
	if w.SetAuth != nil {
		hostExecSetAuth = w.SetAuth
	}
	if w.WorkerID != "" {
		hostExecWorkerID = w.WorkerID
	}
	if w.ViaHelper && w.Runner != nil {
		// The runner IS the helper relay: a containerized worker running
		// through it is exactly the supported path, not the "no helper
		// socket" case hostExecContainerized guards against. Native
		// (non-helper) mode in a container is untouched here and keeps
		// insideContainer's real verdict, so it still refuses.
		hostExecContainerized = func() bool { return false }
	}
	hostExecPayloadOf = hostExecPayloadFromTask
}

// hostExecPayloadFromTask maps a task's flat HostExec* payload keys (hx.7d) onto
// the handler's HostExecPayload. Returns nil — "payload not delivered", which
// fails the task rather than inventing an argv — when the task carries neither
// a job_id nor an argv.
func hostExecPayloadFromTask(task *transport.Task) *HostExecPayload {
	if task == nil {
		return nil
	}
	p := task.Payload
	if p.HostExecJobID == 0 && len(p.HostExecArgv) == 0 {
		return nil
	}
	timeoutS := p.HostExecTimeoutS
	return &HostExecPayload{
		JobID:    int(p.HostExecJobID),
		Argv:     p.HostExecArgv,
		Cwd:      p.HostExecCwd,
		Env:      p.HostExecEnv,
		RunAs:    p.HostExecRunAs,
		TimeoutS: timeoutS,
		Mode:     p.HostExecMode,
	}
}

// hostExecWorkerIDOf renders a numeric WorkerLicense id for the output endpoint's
// path parameter (/v1/workers/{worker_id}/exec/{job_id}/output). 0 means "the
// API never told this worker its id" -> empty string, which keeps the relay
// unwired (fail closed) rather than posting to /workers//exec/.
func hostExecWorkerIDOf(workerID int64) string {
	if workerID <= 0 {
		return ""
	}
	return strconv.FormatInt(workerID, 10)
}
