// OPT1-DISPATCH-AND-CLAIM (lane gpu1-1, 11-24) — per-task credential PREFETCH.
//
// Measured on xeon's embedded worker (11-24, worker log + api_task_queue):
// the /workers/tasks/{id}/credentials grant costs ~0.38 s and runs in SERIES
// after ValidatePhase + ReportStarted, yet the API only requires the task to
// be CLAIMED or RUNNING (CORE/API/routers/worker_tasks.py:5108 — anything
// else is a 409) — true the moment the claim returned. This helper starts
// the fetch at claim time and joins it where the serial fetch sat, so the
// grant overlaps the advisory phase validation and the /started report.
//
// Behaviour that must stay identical (and does): exactly ONE GetCredentials
// call per prefetch — the same transport call, the same worker-authenticated
// endpoint, the same API-side tenant paywall, ownership check and minting;
// nothing is cached, reused across tasks, or skipped. The joined (creds,
// err) pair is what the serial call would have returned, so every consumer
// downstream (Harbor creds, tenant_harbor, build token, model cache token,
// the nonce gate) sees the same values.
//
// The one observable difference: a task whose phase validation REJECTS it
// (the reschedule path) already had its ephemeral per-task grant issued. The
// grant is never handed to another task — the row reverts to PENDING and
// re-claims later with a fresh grant — and the grant is the same tenant/
// worker pair the endpoint would issue anyway. Security is unchanged.
package transport

import (
	"context"
	"sync"
)

// CredsProvider is the credential source — satisfied structurally by both
// APITransport and DirectTransport (embedded workers delegate to their
// embedded APITransport), same shape as the Transport interface's method.
type CredsProvider interface {
	GetCredentials(ctx context.Context, taskID int) (*Credentials, error)
}

// CredsFetch is the handle StartCredsPrefetch returns; Join() yields the
// result exactly where the serial fetch used to sit.
type CredsFetch struct {
	prov   CredsProvider
	taskID int

	creds *Credentials
	err   error
	done  chan struct{} // closed when (creds, err) are set

	joined bool // Join() consumed the result (tests pin single-join)
	mu     sync.Mutex
}

// StartCredsPrefetch issues the per-task credential grant in the
// background. The caller MUST call Join() exactly once where the
// credentials are needed; a prefetch whose Join never happens ends its
// goroutine and leaks nothing (the grant is an ephemeral per-task
// credential the API already issued server-side).
func StartCredsPrefetch(ctx context.Context, prov CredsProvider, taskID int) *CredsFetch {
	f := &CredsFetch{prov: prov, taskID: taskID, done: make(chan struct{})}
	go func() {
		c, e := prov.GetCredentials(ctx, taskID)
		f.creds, f.err = c, e
	 close(f.done)
	}()
	return f
}

// Join blocks until the grant completed (the transport's own HTTP timeouts
// bound it) and returns what the serial fetch would have returned.
func (f *CredsFetch) Join() (*Credentials, error) {
	<-f.done
	f.mu.Lock()
	f.joined = true
	f.mu.Unlock()
	return f.creds, f.err
}
