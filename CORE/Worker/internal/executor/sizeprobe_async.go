package executor

// OPT-SIZE-PROBE-OFF-CRITICAL-PATH (sz-tcp.16b): the size-probe sidecar is a
// PRICING AID only. Its JSON is cached in Redis under size_probe:<sha> and is
// read by the API (services/size_probe_lookup.py) on the post-completion
// telemetry / quote path — there is no consumer before the node container
// starts, and the worker's own Result has no size_probe field (the only
// worker-side use today is a log line). Running it synchronously between
// download and image pull put its full create/start/wait/remove cost (~0.5 s
// at the docker daemon floor, more on a cold cache) on EVERY task's critical
// path. This helper starts it on the same pipelineCtx as the task and joins
// it only at completion, so the sidecar overlaps pull + node execution.
//
// The cache key, the cached JSON bytes, the read-only /data mount, the 24 h
// TTL, the log messages and the never-fail-the-task contract are unchanged:
// this wraps the existing SizeProbe, it does not alter its algorithm.
//
// The goroutine is bounded by the SAME pipelineCtx cancel/timeout as the
// task (Executor.Run defers cancel()), and Finish uses a done channel so a
// cancelled task never leaves a probe goroutine behind.

import (
	"context"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// probeAsync is a pending size-probe. StartAsyncSizeProbe returns one; it
// begins the probe in the background and returns immediately. Finish must be
// called exactly once (on every path, including early task failures) before
// the task returns; it joins the probe and logs its outcome with the same
// wording as the old synchronous call site.
type probeAsync struct {
	done    chan *SizeProbeDoc
	taskID  int
	started bool
}

// StartAsyncSizeProbe starts the probe concurrently with the rest of the
// pipeline. It never blocks the caller: the lookup + pull + container work
// in SizeProbe happens off the task path. A task with no inputs (or no cache
// /registry wiring) gets an immediate nil from SizeProbe in the goroutine,
// so there is still no task-path cost.
func StartAsyncSizeProbe(ctx context.Context, taskID int, task *transport.Task, creds *transport.Credentials, dockerNetwork, localRegistry, volName string, d dockerProbeAPI) *probeAsync {
	p := &probeAsync{done: make(chan *SizeProbeDoc, 1), taskID: taskID, started: true}
	// The probe receives the same pipelineCtx as every other step. When the
	// task ends (success or failure) Run's deferred cancel() reaches this
	// goroutine; SizeProbe's bounded waitCtx derives from it too.
	go func() {
		p.done <- SizeProbe(ctx, task, creds, dockerNetwork, localRegistry, volName, d)
	}()
	return p
}

// FinishSizeProbe joins a started probe and logs its outcome. It is a no-op
// when p is nil (nothing to probe) and never fails the task: SizeProbe's own
// contract already returns nil on every error path.
func FinishSizeProbe(p *probeAsync, logInfo func(string, ...interface{})) *SizeProbeDoc {
	if p == nil || !p.started {
		return nil
	}
	doc := <-p.done
	if doc != nil && logInfo != nil {
		logInfo("size-probe ok", "task", p.taskID, "probes", len(doc.Probes))
	}
	return doc
}
