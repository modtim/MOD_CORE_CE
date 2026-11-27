package main

// OPT1-WIRE-CREDS-PREFETCH-HARDEN (lane r9700-2, 11-25) — the credential-
// prefetch decision and the join/cancel handling from executeTask, extracted
// into the worker package so a cmd/worker test can drive it without Docker
// (the closer's F-2 gap: nothing exercised the executeTask wiring itself).
//
// Two decisions live here:
//
// 1. credsPrefetchAllowed — may the grant start before the task is
//    validated / reported started? The API's /started dispatch gates
//    (routers/worker_tasks.py: the per-tag gate and the Rego policy gate)
//    resolve the WORKFLOW's policy tags server-side (task -> workflow_run
//    -> workflow tags); the claim payload never carries them, so the
//    worker CANNOT know in advance that a gate will refuse. The narrowest
//    conservative predicate the worker CAN know from the claim payload:
//    the payload markers the same gate family stamps for export-control /
//    label-restricted inputs (the worker_tasks.py enqueue stamps). Any
//    marker present => the serial fetch as before, so a dispatch a gate
//    refuses gets no early grant. A task whose workflow carries policy
//    tags but whose payload carries no marker still prefetches — stated
//    openly in the result file, not silently accepted.
//
// 2. The prefetch runs on its OWN cancellable context (NOT the process
//    ctx), and every early-return path in executeTask calls Abort(): the
//    grant's retry loop (retryBackoff selects on ctx.Done) unwinds
//    immediately and Abort drains the goroutine, so no goroutine and no
//    bounded retry loop outlives a refused task.
import (
	"context"
	"errors"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// credsPrefetchAllowed is the start-prefetch-or-serial decision. False for
// any task carrying an export-control / label marker in its payload.
func credsPrefetchAllowed(task *transport.Task) bool {
	if task.Payload.ExportControlLevel != "" ||
		(task.Payload.LabelWorkerPools != nil && len(task.Payload.LabelWorkerPools) > 0) ||
		(task.Payload.LabelRegions != nil && len(task.Payload.LabelRegions) > 0) {
		return false
	}
	return true
}

// CredsWire is the handle executeTask holds: fetch == nil means the serial
// path. cancel belongs to the prefetch's OWN context — cancelling it never
// touches the task's own ctx.
type CredsWire struct {
	tp      transport.Transport
	taskID  int
	fetch   *transport.CredsFetch
	cancel  context.CancelFunc
	aborted bool // Abort ran: the wire is spent, Join never mints again
}

// NewCredsWire starts the grant (prefetch path) or defers it (serial path).
func NewCredsWire(ctx context.Context, tp transport.Transport, task *transport.Task) *CredsWire {
	w := &CredsWire{tp: tp, taskID: task.ID}
	if credsPrefetchAllowed(task) {
		pctx, cancel := context.WithCancel(ctx)
		w.fetch = transport.StartCredsPrefetch(pctx, tp, task.ID)
		w.cancel = cancel
	}
	return w
}

// Abort cancels the prefetch's own context and waits for its goroutine to
// end (the cancelled transport call returns, the goroutine closes the
// done-chan), so nothing outlives the caller. Idempotent; a no-op on the
// serial path.
func (w *CredsWire) Abort() {
	if w.cancel != nil {
		w.cancel()
	}
	if w.fetch != nil {
		w.fetch.Join() // result discarded: the refused task never uses the grant
	}
	w.fetch = nil
	w.aborted = true
}

// Join returns exactly what the serial tp.GetCredentials(ctx, taskID) call
// returned. On the prefetch path it consumes the started grant; after an
// Abort the wire is spent and Join reports that instead of minting a
// second grant (executeTask returns on those paths — a Join there is a
// wiring bug, and minting again would be the wrong fix).
func (w *CredsWire) Join(ctx context.Context) (*transport.Credentials, error) {
	if w.aborted {
		return nil, errors.New("credential fetch aborted for a refused task")
	}
	if w.fetch == nil {
		return w.tp.GetCredentials(ctx, w.taskID)
	}
	c, e := w.fetch.Join()
	w.fetch = nil
	w.cancel = nil
	return c, e
}
