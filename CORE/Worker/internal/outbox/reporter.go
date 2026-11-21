package outbox

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// Sender is the slice of the transport the reporter needs. Kept narrow so the
// drain loop can be tested without a live manager.
type Sender interface {
	ReportComplete(ctx context.Context, taskID int, result transport.TaskResult) error
	ReportFailed(ctx context.Context, taskID int, errMsg string, shouldRetry bool,
		logTail string, oomKilled bool, errorDetail string) error
}

// completePayload / failedPayload are the replayable forms of each report.
type completePayload struct {
	Result transport.TaskResult `json:"result"`
}

type failedPayload struct {
	ErrMsg      string `json:"err_msg"`
	ShouldRetry bool   `json:"should_retry"`
	LogTail     string `json:"log_tail"`
	OOMKilled   bool   `json:"oom_killed"`
	// ErrorDetail is the failing sidecar's raw stderr —
	// calibration-health-fixtures-and-timeouts §3.
	ErrorDetail string `json:"error_detail"`
	// Egress is the optional network egress decision, carried on the fail path
	// (EGR.11a). Preserved on replay so the manager records it alongside the
	// failure even when delivery was deferred.
	Egress *transport.EgressReport `json:"egress,omitempty"`
}

// IsOutboxPermanentRejection is the outbox's own class of "a manager decision
// this replay cannot change". It is deliberately NOT transport.IsPermanentRejection:
// that predicate is shared with the claim path (ReportStarted -> retryBackoffTerminal
// -> IsTerminalRejection), where a 401 must stay terminal so a worker never starts
// a container the manager refused. For the OUTBOX a 401 is credential state, not a
// refusal: the token expired or was rotated while the worker ran, or a worker
// restarted with an expired token. Discarding the held result on that would drop
// finished work, so 401 is retryable here (queue / Bump). 403 (the manager
// reassigned the task) stays permanent, as its test demands.
func IsOutboxPermanentRejection(err error) bool {
	var he *transport.HTTPError
	if errors.As(err, &he) {
		if he.StatusCode == http.StatusUnauthorized {
			return false
		}
		return he.Permanent()
	}
	return false
}

// Reporter delivers task reports, queueing anything the manager could not be
// told about and retrying until it lands or is refused outright.
type Reporter struct {
	store    *Store
	send     Sender
	log      *slog.Logger
	interval time.Duration
}

// NewReporter wires a reporter to a store and a transport.
func NewReporter(store *Store, send Sender, log *slog.Logger, interval time.Duration) *Reporter {
	if interval <= 0 {
		interval = 30 * time.Second
	}
	if store != nil && log != nil {
		store.SetLog(log)
	}
	return &Reporter{store: store, send: send, log: log, interval: interval}
}

// Complete reports success, queueing the result if the manager is unreachable.
// Never returns an error for a delivery failure: the result is safe either way,
// and the caller has nothing useful to do about it.
func (r *Reporter) Complete(ctx context.Context, taskID int, result transport.TaskResult) {
	err := r.send.ReportComplete(ctx, taskID, result)
	if err == nil {
		return
	}
	if IsOutboxPermanentRejection(err) {
		// The manager refused — almost always because it reassigned the task
		// while we were unreachable, and someone else's result now stands.
		// Queueing this would mean retrying a decision, not a delivery.
		r.log.Warn("manager refused completion; result discarded", "task_id", taskID, "error", err)
		return
	}
	if qerr := r.store.Add(KindComplete, taskID, completePayload{Result: result}); qerr != nil {
		r.log.Error("could not queue completion — RESULT LOST",
			"task_id", taskID, "report_error", err, "queue_error", qerr)
		return
	}
	r.log.Warn("manager unreachable; completion queued for retry",
		"task_id", taskID, "error", err)
}

// Failed reports failure, with the same queueing behaviour.
func (r *Reporter) Failed(ctx context.Context, taskID int, errMsg string,
	shouldRetry bool, logTail string, oomKilled bool, errorDetail string,
	egress ...*transport.EgressReport) {

	var eg *transport.EgressReport
	if len(egress) > 0 {
		eg = egress[0]
	}
	err := r.sendReportFailed(ctx, taskID, errMsg, shouldRetry, logTail, oomKilled, errorDetail, eg)
	if err == nil {
		return
	}
	if IsOutboxPermanentRejection(err) {
		r.log.Warn("manager refused failure report; discarded", "task_id", taskID, "error", err)
		return
	}
	p := failedPayload{ErrMsg: errMsg, ShouldRetry: shouldRetry, LogTail: logTail, OOMKilled: oomKilled, ErrorDetail: errorDetail, Egress: eg}
	if qerr := r.store.Add(KindFailed, taskID, p); qerr != nil {
		r.log.Error("could not queue failure report",
			"task_id", taskID, "report_error", err, "queue_error", qerr)
		return
	}
	r.log.Warn("manager unreachable; failure report queued for retry",
		"task_id", taskID, "error", err)
}

// sendReportFailed issues the failure report, preferring the egress-aware form
// when the manager supports it and falling back to the plain form otherwise.
func (r *Reporter) sendReportFailed(ctx context.Context, taskID int, errMsg string,
	shouldRetry bool, logTail string, oomKilled bool, errorDetail string, egress *transport.EgressReport) error {
	type egressReporter interface {
		ReportFailedWithEgress(ctx context.Context, taskID int, errMsg string, shouldRetry bool,
			logTail string, oomKilled bool, errorDetail string, egress *transport.EgressReport) error
	}
	if er, ok := r.send.(egressReporter); ok {
		return er.ReportFailedWithEgress(ctx, taskID, errMsg, shouldRetry, logTail, oomKilled, errorDetail, egress)
	}
	return r.send.ReportFailed(ctx, taskID, errMsg, shouldRetry, logTail, oomKilled, errorDetail)
}

// Run drains the outbox on a ticker until ctx is cancelled. Safe to start even
// when the queue is empty.
func (r *Reporter) Run(ctx context.Context) {
	// One immediate pass so results held across a worker restart go out as
	// soon as the manager is reachable, rather than after a full interval.
	r.Drain(ctx)

	t := time.NewTicker(r.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			r.Drain(ctx)
		}
	}
}

// Drain attempts delivery of every queued entry once, oldest first. Returns
// how many were delivered.
func (r *Reporter) Drain(ctx context.Context) int {
	entries, err := r.store.List()
	if err != nil {
		r.log.Warn("outbox: cannot list queued reports", "error", err)
		return 0
	}
	if len(entries) == 0 {
		return 0
	}

	delivered := 0
	for _, e := range entries {
		if ctx.Err() != nil {
			return delivered
		}
		err := r.replay(ctx, e)
		switch {
		case err == nil:
			if rmErr := r.store.Remove(e); rmErr != nil {
				// Left in place it would be re-sent, which the manager
				// absorbs idempotently — noisy, not harmful.
				r.log.Warn("outbox: delivered but could not dequeue",
					"task_id", e.TaskID, "error", rmErr)
			}
			delivered++
			r.log.Info("outbox: delivered queued report",
				"task_id", e.TaskID, "kind", string(e.Kind),
				"held_for", e.Age().Round(time.Second), "attempts", e.Attempts+1)

		case transport.IsConflict(err):
			// 409 = task may have reached a terminal state. Inspect the detail
			// to decide: terminal states are dropped; transient conflicts are
			// bumped so the worker retries once the manager is ready again.
			detail := transport.ConflictDetail(err)
			if transport.ConflictDetailTerminalState(err) {
				r.log.Warn("outbox: task terminal state confirmed, dropping report",
					"task_id", e.TaskID, "kind", string(e.Kind), "detail", detail)
				if rmErr := r.store.Remove(e); rmErr != nil {
					// The entry stays queued, so the drop is not silent: it is
					// replayed next tick and refused again. Say so.
					r.log.Warn("outbox: terminal-state drop failed; entry still queued",
						"task_id", e.TaskID, "kind", string(e.Kind), "error", rmErr)
				}
			} else {
				r.store.Bump(e)
				r.log.Debug("outbox: 409 transient, retry",
					"task_id", e.TaskID, "detail", detail)
			}

		case IsOutboxPermanentRejection(err):
			// The manager has moved on. Retrying cannot change that, so drop
			// it rather than accumulate a queue that never empties.
			r.log.Warn("outbox: manager refused queued report; dropping",
				"task_id", e.TaskID, "kind", string(e.Kind),
				"held_for", e.Age().Round(time.Second), "error", err)
			_ = r.store.Remove(e)

		default:
			r.store.Bump(e)
			r.log.Debug("outbox: retry failed, still queued",
				"task_id", e.TaskID, "attempts", e.Attempts+1, "error", err)
		}
	}
	if delivered > 0 {
		r.log.Info("outbox: drain complete", "delivered", delivered, "remaining", r.store.Len())
	}
	return delivered
}

// replay re-issues one queued report.
func (r *Reporter) replay(ctx context.Context, e Entry) error {
	switch e.Kind {
	case KindComplete:
		var p completePayload
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			// Undecodable: nothing to send, ever. Treat as permanent so the
			// entry is dropped instead of retried forever.
			return &transport.HTTPError{Path: "outbox", StatusCode: 400, Body: err.Error()}
		}
		return r.send.ReportComplete(ctx, e.TaskID, p.Result)

	case KindFailed:
		var p failedPayload
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			return &transport.HTTPError{Path: "outbox", StatusCode: 400, Body: err.Error()}
		}
		var egress *transport.EgressReport
		if p.Egress != nil {
			egress = p.Egress
		}
		type egressReporter interface {
			ReportFailedWithEgress(ctx context.Context, taskID int, errMsg string, shouldRetry bool,
				logTail string, oomKilled bool, errorDetail string, egress *transport.EgressReport) error
		}
		if er, ok := r.send.(egressReporter); ok {
			return er.ReportFailedWithEgress(ctx, e.TaskID, p.ErrMsg, p.ShouldRetry, p.LogTail, p.OOMKilled, p.ErrorDetail, egress)
		}
		return r.send.ReportFailed(ctx, e.TaskID, p.ErrMsg, p.ShouldRetry, p.LogTail, p.OOMKilled, p.ErrorDetail)
	}
	return &transport.HTTPError{Path: "outbox", StatusCode: 400, Body: "unknown entry kind " + string(e.Kind)}
}
