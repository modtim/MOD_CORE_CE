package transport

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/ecdh"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tlsconf"
)

// APITransport implements Transport for remote workers.
// Everything goes through the HTTPS API — the only external-facing surface.
// All critical calls (result reporting, credentials) use retry with exponential
// backoff so that transient outages (server updates, network blips) don't lose work.
type APITransport struct {
	baseURL        string
	workerName     string
	bootstrapToken string
	queues         []string
	client         *http.Client
	log            *slog.Logger

	// Server-pushed state from heartbeat response
	mu             sync.RWMutex
	serverUpdating bool // true while the server is mid-update
	draining       bool // true when server says "finish tasks, don't take new ones"
	updateAvail    bool // true when a newer agent binary is available
	latestVersion  string

	// fetchLoopMarker is the worker's fetch-loop liveness stamp
	// (heartbeat.MarkFetchLoopIteration), registered via the
	// transport.FetchLoopMarkerSetter interface (BUG.B83). nil = nobody
	// registered one (unit tests, the dedicated run-token minter), so
	// markFetchLoopAlive is a no-op there. Set once in main.go before the
	// job loop starts; only read by the single-threaded fetch paths below.
	//
	// BUG.B83: the stamp was only touched by cmd/worker's job loop, but an
	// idle embedded worker spends ALL its time inside DirectTransport.
	// WaitForTask's own pub/sub/ticker loop, whose polls never return
	// control to main.go — so the watchdog saw a stale stamp on a worker
	// that was polling normally and force-exited it every ~11 minutes.
	// markFetchLoopAlive (below) refreshes the stamp on every completed
	// poll round-trip instead, which is the honest definition of alive.
	//
	// Set once at wiring time and read only from single-threaded fetch
	// paths, so it needs no guard.
	fetchLoopMarker func()
}

// apiPrefix is the API's single mount point (v2, no backward-compat). Every
// endpoint this transport calls (/workers/tasks/poll, /workers/heartbeat via
// the heartbeat package, etc.) now lives under it — the API dropped its
// unprefixed mount entirely, so a request built without this prefix 404s.
const apiPrefix = "/v1"

// Caps on bytes read from the manager. A body past the cap is never used as if
// it were complete: error bodies are truncated (they only feed an error message),
// and success bodies that callers decode fail with errResponseTooLarge.
const (
	maxErrorBodyBytes = 64 << 10
	maxResponseBytes  = 16 << 20
)

var errResponseTooLarge = fmt.Errorf("response too large: body exceeds %d bytes", maxResponseBytes)

// readResponseBody reads a whole success body. A body larger than maxResponseBytes
// is an error, never a silently truncated body.
func readResponseBody(r io.Reader) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, maxResponseBytes+1))
	if err != nil {
		return nil, err
	}
	if len(b) > maxResponseBytes {
		return nil, errResponseTooLarge
	}
	return b, nil
}

// boundedReader is an io.Reader that never pulls more than maxResponseBytes+1
// bytes out of a network stream and records the largest number of bytes actually
// consumed, so a test can prove a call site stopped at its cap instead of
// buffering an unbounded body.
type boundedReader struct {
	r    io.Reader
	left int
	read int
}

func (b *boundedReader) Read(dst []byte) (int, error) {
	if b.left <= 0 {
		return 0, io.EOF
	}
	n := len(dst)
	if n > b.left {
		n = b.left
	}
	got, err := b.r.Read(dst[:n])
	if got < 0 {
		got = 0
	}
	b.read += got
	b.left -= got
	return got, err
}

func (b *boundedReader) Close() error { return nil }

// decodeJSONBounded decodes a success body without ever reading more than
// maxResponseBytes+1 bytes off the wire, and refuses a body that crosses the cap:
// a truncated stream is never handed back as if it were complete. For bodies
// within the cap the behaviour is byte-identical to json.NewDecoder(...).Decode.
func decodeJSONBounded(r io.Reader, v any) error {
	b := &boundedReader{r: r, left: maxResponseBytes + 1}
	err := json.NewDecoder(b).Decode(v)
	if b.read > maxResponseBytes {
		return errResponseTooLarge
	}
	return err
}

// NewAPITransport creates a transport for remote mode.
//
// baseURL is normalized to include apiPrefix ONCE here so every caller of
// base()/post()/postWithResponse() inherits it automatically, including the
// dedicated run-token minter transport in main.go — which is constructed
// with the same cfg.APIURL (internal api:80) and must stay pinned there per
// worker-failover-breaks-token-mint. This only changes the PATH prefix, not
// which host is targeted.
func NewAPITransport(baseURL, workerName, bootstrapToken string, queues []string, log *slog.Logger) *APITransport {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL != "" && !strings.HasSuffix(baseURL, apiPrefix) {
		baseURL += apiPrefix
	}
	client := &http.Client{
		Timeout: 90 * time.Second, // longer than max long-poll timeout (60s)
	}
	// GSEC FX.13 (R-059): trust the platform internal CA for API hops when
	// (and only when) MOD_INTERNAL_CA_BUNDLE names a readable bundle;
	// otherwise the client stays byte-identical to today's behaviour.
	tlsconf.SetClientTLS(client)
	return &APITransport{
		baseURL:        baseURL,
		workerName:     workerName,
		bootstrapToken: bootstrapToken,
		queues:         queues,
		log:            log,
		client:         client,
	}
}

// base returns the current manager base URL (guarded — the heartbeat loop can
// swap it on failover so task-claim/report follow the worker to a live manager).
func (t *APITransport) base() string {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.baseURL
}

// SetBaseURL repoints this transport at a different manager (mesh failover). Called
// from the heartbeat loop when the current manager goes unreachable.
//
// url arrives bare (from the server's approved-manager roster, same shape as
// cfg.APIURL) — normalized through the same apiPrefix logic as the
// constructor so a failover rotation can't silently drop the /v1 prefix and
// start 404ing on the new manager.
func (t *APITransport) SetBaseURL(url string) {
	if url == "" {
		return
	}
	url = strings.TrimRight(strings.TrimSpace(url), "/")
	if url != "" && !strings.HasSuffix(url, apiPrefix) {
		url += apiPrefix
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.baseURL != url {
		t.baseURL = url
		t.log.Warn("manager failover: transport repointed", "manager", url)
	}
}

// SetFetchLoopMarker registers the worker's fetch-loop liveness stamp
// (heartbeat.MarkFetchLoopIteration) so every completed poll round-trip can
// refresh it. BUG.B83: the stamp used to be touched only by cmd/worker's job
// loop, but an idle embedded worker never returns control there — DirectTransport.WaitForTask owns the whole idle wait (pub/sub + its own 3s ticker + the API long-poll), so the job loop's marks could not see the ~30s poll cadence the access log clearly shows. The watchdog then measured ~11 minutes of "no fetch" on a healthy worker and force-exited it, orphaning whatever task the API had just handed out.
//
// Wired in main.go through the FetchLoopMarkerSetter interface (same
// interface-based pattern as SetServerState: embedded workers are
// DirectTransports, so a concrete APITransport assertion would miss them).
// The dedicated run-token minter transport is deliberately NOT wired — it
// never polls, and refreshing the stamp from it would mask a genuinely wedged
// fetch loop.
func (t *APITransport) SetFetchLoopMarker(fn func()) {
	t.fetchLoopMarker = fn
}

// markFetchLoopAlive refreshes the fetch-loop liveness stamp after a poll
// round-trip COMPLETED — whatever its outcome. The watchdog's job is to catch
// a loop that is blocked/hung (a poll that never returns), not one whose
// requests keep landing: an empty queue, an HTTP 5xx from a restarting API,
// and a dropped connection are all proof of life, and all three used to leave
// the stamp stale. No-op when no stamp was registered (unit tests, the
// run-token minter).
func (t *APITransport) markFetchLoopAlive() {
	if t.fetchLoopMarker != nil {
		t.fetchLoopMarker()
	}
}

// SetServerState is called by the heartbeat loop to push server-side signals into the transport.
func (t *APITransport) SetServerState(updating, drain, updateAvail bool, latestVersion string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.serverUpdating = updating
	t.draining = drain
	t.updateAvail = updateAvail
	t.latestVersion = latestVersion
}

// ServerUpdating returns true while the server is mid-update.
func (t *APITransport) ServerUpdating() bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.serverUpdating
}

// Draining returns true when the server has told this worker to drain.
func (t *APITransport) Draining() bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.draining
}

// UpdateAvailable returns true if a newer agent binary is available, plus the version string.
func (t *APITransport) UpdateAvailable() (bool, string) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.updateAvail, t.latestVersion
}

// retryBackoff calls fn with exponential backoff until it succeeds or maxDuration elapses.
// Used for critical operations (result reporting) that MUST eventually succeed.
func retryBackoff(ctx context.Context, maxDuration time.Duration, label string, log *slog.Logger, fn func() error) error {
	deadline := time.Now().Add(maxDuration)
	attempt := 0
	baseDelay := 1 * time.Second

	for {
		err := fn()
		if err == nil {
			if attempt > 0 {
				log.Info("retry succeeded", "op", label, "attempts", attempt+1)
			}
			return nil
		}

		// A success body over the read cap is terminal: another attempt cannot
		// make it fit, and the raw error must reach the caller unwrapped (these
		// endpoints' bodies carry credentials/secrets).
		if errors.Is(err, errResponseTooLarge) {
			return err
		}

		attempt++
		if time.Now().After(deadline) {
			return fmt.Errorf("%s: failed after %d attempts over %v: %w", label, attempt, maxDuration, err)
		}

		// Exponential backoff: 1s, 2s, 4s, 8s, 16s, capped at 30s
		delay := time.Duration(float64(baseDelay) * math.Pow(2, float64(attempt-1)))
		if delay > 30*time.Second {
			delay = 30 * time.Second
		}

		log.Warn("retrying", "op", label, "attempt", attempt, "delay", delay.Round(time.Second), "error", err)

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
}

// retryBackoffTerminal is retryBackoff for the calls where a deliberate 4xx
// answer is terminal: the manager understood the request and refused it, so
// another attempt cannot change the outcome and the caller must see the raw
// error (unwrapped, unlike retryBackoff's "%s: failed after ..." wrapper) to
// decide what to do — e.g. ReportStarted's caller must not start the container
// when /started says 409. 408 and 429 are the two 4xx that ARE invitations to
// retry, so they keep backing off; 5xx and transport errors are unchanged.
func retryBackoffTerminal(ctx context.Context, maxDuration time.Duration, label string, log *slog.Logger, fn func() error) error {
	deadline := time.Now().Add(maxDuration)
	attempt := 0
	baseDelay := 1 * time.Second

	for {
		err := fn()
		if err == nil {
			if attempt > 0 {
				log.Info("retry succeeded", "op", label, "attempts", attempt+1)
			}
			return nil
		}
		if IsTerminalRejection(err) {
			log.Warn("terminal rejection", "op", label, "error", err)
			return err
		}

		attempt++
		if time.Now().After(deadline) {
			return fmt.Errorf("%s: failed after %d attempts over %v: %w", label, attempt, maxDuration, err)
		}

		delay := time.Duration(float64(baseDelay) * math.Pow(2, float64(attempt-1)))
		if delay > 30*time.Second {
		 delay = 30 * time.Second
		}

		log.Warn("retrying", "op", label, "attempt", attempt, "delay", delay.Round(time.Second), "error", err)

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
}

// TryClaimNow does a non-blocking poll (1 second timeout) to check if any tasks are available.
// Used immediately after completing a task to avoid the pub/sub re-subscription gap.
func (t *APITransport) TryClaimNow(ctx context.Context) (*Task, error) {
	body, err := json.Marshal(map[string]interface{}{
		"queues":  t.queues,
		"timeout": 1,
	})
	if err != nil {
		return nil, err
	}

	reqCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, "POST", t.base()+"/workers/tasks/poll", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	t.setHeaders(req)
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.client.Do(req)
	if err != nil {
		t.markFetchLoopAlive()
		return nil, nil // Swallow errors on quick poll — not critical
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// The API answered (even a 5xx): the loop is alive.
		t.markFetchLoopAlive()
		return nil, nil
	}

	var pollResp struct {
		TaskID         *int                   `json:"task_id"`
		QueueName      *string                `json:"queue_name"`
		WorkflowRunID  *int                   `json:"workflow_run_id"`
		NodeID         *string                `json:"node_id"`
		NodeRunID      *int                   `json:"node_run_id"`
		ClaimExpiresAt *string                `json:"claim_expires_at"`
		Payload        map[string]interface{} `json:"payload"`
		PhasePlan      *PhasePlan             `json:"phase_plan"`
		StartAfter     *string                `json:"start_after"`
		RetryAfter     int                    `json:"retry_after_seconds"`
	}
if err := decodeJSONBounded(resp.Body, &pollResp); err != nil {
		// A body over the read cap is reported, not swallowed: the manager
		// answered with something we cannot use, and the caller must not
		// treat a truncated claim as an empty queue.
		t.markFetchLoopAlive()
		if errors.Is(err, errResponseTooLarge) {
			return nil, fmt.Errorf("try-claim: %w", err)
		}
		return nil, nil
	}

	// Empty queue: the round-trip completed, so the fetch loop is alive.
	// This is the path an idle embedded worker hits every ~30s (BUG.B83).
	if pollResp.TaskID == nil {
		t.markFetchLoopAlive()
		return nil, nil
	}

	task, err := mapPollResponse(pollResp.TaskID, pollResp.QueueName, pollResp.WorkflowRunID,
		pollResp.NodeID, pollResp.NodeRunID, pollResp.ClaimExpiresAt, pollResp.Payload)
	if err != nil {
		t.markFetchLoopAlive()
		return nil, nil
	}

	task.PhasePlan = pollResp.PhasePlan
	if pollResp.StartAfter != nil {
		if t, err := time.Parse(time.RFC3339, *pollResp.StartAfter); err == nil {
			task.StartAfter = &t
		}
	}

	// A task was claimed: the round-trip completed, so refresh the stamp
	// before main.go starts the job goroutine (BUG.B83's orphaned-claim
	// window: a claim handed out just as the watchdog tried used to die
	// with the process).
	t.markFetchLoopAlive()
	return task, nil
}

func (t *APITransport) WaitForTask(ctx context.Context) (*Task, error) {
	// If server told us to drain (e.g. mid-update), don't pick up new tasks.
	// Sleep briefly and return nil so the main loop can check shutdown signals.
	if t.Draining() {
		t.log.Debug("draining — not polling for new tasks")
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(5 * time.Second):
			return nil, nil
		}
	}
	return t.pollForTask(ctx, 30, false)
}

// WaitForStackOp is the NOAH claim-gate bypass (see Transport.WaitForStackOp
// doc). A short timeout (rather than WaitForTask's 30s long-poll) keeps the
// gated loop in main.go responsive to noah.shouldClaim() flipping back to
// true, since that is only re-checked between calls.
//
// Drains the same as WaitForTask: a worker mid-update/shutdown must not pick
// up new work of any kind, stack ops included.
func (t *APITransport) WaitForStackOp(ctx context.Context) (*Task, error) {
	if t.Draining() {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(1 * time.Second):
			return nil, nil
		}
	}
	return t.pollForTask(ctx, 5, true)
}

// pollForTask is the shared body of WaitForTask/WaitForStackOp: POST
// /workers/tasks/poll with the given long-poll timeout and stack_ops_only
// flag, and decode the result into a *Task (nil, nil when none available).
func (t *APITransport) pollForTask(ctx context.Context, timeoutSecs int, stackOpsOnly bool) (*Task, error) {
	body, err := json.Marshal(map[string]interface{}{
		"queues":         t.queues,
		"timeout":        timeoutSecs,
		"stack_ops_only": stackOpsOnly,
	})
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, "POST", t.base()+"/workers/tasks/poll", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	t.setHeaders(req)
	req.Header.Set("Content-Type", "application/json")

	// BUG.B83: a round-trip that came back at all — transport error, HTTP
	// error status, undecodable body, empty queue, or a task — proves the
	// fetch loop is alive; only a poll that never returns is a stall. Every
	// exit below therefore refreshes the watchdog's stamp.
	resp, err := t.client.Do(req)
	if err != nil {
		t.markFetchLoopAlive()
		return nil, fmt.Errorf("poll: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// 5xx still proves the fetch loop is alive (B83).
		t.markFetchLoopAlive()
		b, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBodyBytes))
		return nil, fmt.Errorf("poll: HTTP %d: %s", resp.StatusCode, truncateBytes(b, 500))
	}

	var pollResp struct {
		TaskID         *int                   `json:"task_id"`
		QueueName      *string                `json:"queue_name"`
		WorkflowRunID  *int                   `json:"workflow_run_id"`
		NodeID         *string                `json:"node_id"`
		NodeRunID      *int                   `json:"node_run_id"`
		ClaimExpiresAt *string                `json:"claim_expires_at"`
		Payload        map[string]interface{} `json:"payload"`
		PhasePlan      *PhasePlan             `json:"phase_plan"`
		StartAfter     *string                `json:"start_after"`
		RetryAfter     int                    `json:"retry_after_seconds"`
	}
if err := decodeJSONBounded(resp.Body, &pollResp); err != nil {
		t.markFetchLoopAlive()
		return nil, fmt.Errorf("poll decode: %w", err)
	}

	// No task available — caller should retry. The round-trip completed, so
	// the fetch loop is alive even though it returned nothing (BUG.B83's
	// exact false positive: an idle embedded worker polling an empty queue).
	if pollResp.TaskID == nil {
		t.markFetchLoopAlive()
		return nil, nil
	}

	task, err := mapPollResponse(pollResp.TaskID, pollResp.QueueName, pollResp.WorkflowRunID,
		pollResp.NodeID, pollResp.NodeRunID, pollResp.ClaimExpiresAt, pollResp.Payload)
	if err != nil {
		t.markFetchLoopAlive()
		return nil, fmt.Errorf("poll map response: %w", err)
	}

	// Attach scheduler plan if present
	task.PhasePlan = pollResp.PhasePlan
	if pollResp.StartAfter != nil {
		if t, err := time.Parse(time.RFC3339, *pollResp.StartAfter); err == nil {
			task.StartAfter = &t
		}
	}

	// A task was claimed: the round-trip completed, so refresh the stamp
	// BEFORE main.go starts the job goroutine. Without this, a worker that
	// claims and executes while its job loop is otherwise slow can still be
	// read as stalled by the watchdog (BUG.B83's orphaned-claim window).
	t.markFetchLoopAlive()
	return task, nil
}

func (t *APITransport) ReportStarted(ctx context.Context, taskID int, estimatedDuration time.Duration) error {
	body, _ := json.Marshal(map[string]interface{}{
		"estimated_duration_seconds": int(estimatedDuration.Seconds()),
	})
	// Retry for up to 2 minutes — server might be restarting. The exception is
	// a 4xx the manager answered deliberately (409 = the task must not run:
	// cancelled/expired/failed, or a Fortress compliance-gate refusal; 403 =
	// the task was reassigned). 408/429 stay invitations to try again, and 5xx
	// / dropped connections keep the old retry-forever behaviour. The raw error
	// is returned unwrapped so the caller can inspect it (IsConflict) and skip
	// starting the container — this is scoped to ReportStarted; the other
	// retryBackoff callers keep their previous "keep trying" behaviour.
	return retryBackoffTerminal(ctx, 2*time.Minute, "report-started", t.log, func() error {
		return t.post(ctx, fmt.Sprintf("/workers/tasks/%d/started", taskID), body)
	})
}

func (t *APITransport) ReportComplete(ctx context.Context, taskID int, result TaskResult) error {
	// GSEC R-159 — the HF cache re-layout report rides as a flat field so
	// the completion handler records it without touching the file-output
	// bookkeeping (outputs[0] would be persisted as a workflow_node_output).
	// GSEC FX.115 p2 — the per-container lockdown verdict (node_lockdown.go
	// planMap) rides the SAME way as a flat "lockdown_report" object: the
	// API persists per (node kind, flag) what WOULD change under enforce
	// from this field, never by scraping the worker's log line. Both
	// optional fields are folded into one map so neither can clobber the
	// other (the old shape rebuilt the body per field, which silently
	// dropped the first when both were present). Absent for tasks that ran
	// no node container (native/k8s/stack paths), which keeps the body
	// byte-for-byte unchanged for them.
	_body := map[string]interface{}{
		"outputs":  result.Outputs,
		"log_tail": result.LogTail,
	}
	if result.HFCacheReport != nil {
		_body["hf_cache_report"] = result.HFCacheReport
	}
	if result.LockdownReport != nil {
		_body["lockdown_report"] = result.LockdownReport
	}
	// fortress 2.3b — the worker's ephemeral-key signature over its own
	// contribution rides the body as flat "worker_signature" +
	// "worker_contribution" so the API verifier can check the signature
	// against the exact payload bytes it covers. Both keys are set together
	// or not at all (main.go only attaches a signature when the
	// contribution it signs is already built), which keeps the body
	// unchanged for unsigned tasks.
	if result.WorkerSignature != nil {
		_body["worker_signature"] = result.WorkerSignature
	}
	if result.WorkerContribution != nil {
		_body["worker_contribution"] = result.WorkerContribution
	}
	// FIX-BYO-MIGRATION-SKIP-MISSING — the BYO-S3 copy job's skip report
	// (dangling source keys the activation opted to skip) rides the body as
	// a flat field, same idiom as the reports above: nil means "nothing
	// skipped / flag off" and the key is omitted, so every other completion
	// body is byte-for-byte unchanged.
	if result.StorageMigrationReport != nil {
		_body["storage_migration_report"] = result.StorageMigrationReport
	}
	body, _ := json.Marshal(_body)
	// Retry for up to 5 minutes — this is the MOST critical call.
	// If the worker did the work, the result MUST be delivered.
	return retryBackoff(ctx, 5*time.Minute, "report-complete", t.log, func() error {
		return t.post(ctx, fmt.Sprintf("/workers/tasks/%d/complete", taskID), body)
	})
}

func (t *APITransport) ReportFailed(ctx context.Context, taskID int, errMsg string, shouldRetry bool, logTail string, oomKilled bool, errorDetail string) error {
	return t.ReportFailedWithEgress(ctx, taskID, errMsg, shouldRetry, logTail, oomKilled, errorDetail, nil)
}

// ReportFailedWithEgress is ReportFailed plus an optional egress report. When
// egress is non-nil it is folded into the JSON body under "egress" so the
// manager can record the network egress decision alongside the failure. A nil
// egress omits the key entirely, which keeps the body byte-for-byte identical
// to ReportFailed (EGR.11a: fail-path transport carries the egress report
// without an interface break).
func (t *APITransport) ReportFailedWithEgress(ctx context.Context, taskID int, errMsg string, shouldRetry bool, logTail string, oomKilled bool, errorDetail string, egress *EgressReport) error {
	body, _ := json.Marshal(map[string]interface{}{
		"error_message": errMsg,
		"should_retry":  shouldRetry,
		"log_tail":      logTail,
		"oom_killed":    oomKilled,
		"error_detail":  errorDetail,
	})
	if egress != nil {
		body, _ = json.Marshal(map[string]interface{}{
			"error_message": errMsg,
			"should_retry":  shouldRetry,
			"log_tail":      logTail,
			"oom_killed":    oomKilled,
			"error_detail":  errorDetail,
			"egress":        egress,
		})
	}
	// Retry for up to 5 minutes — failure reports are equally critical
	return retryBackoff(ctx, 5*time.Minute, "report-failed", t.log, func() error {
		return t.post(ctx, fmt.Sprintf("/workers/tasks/%d/failed", taskID), body)
	})
}

func (t *APITransport) GetCredentials(ctx context.Context, taskID int) (*Credentials, error) {
	var creds Credentials
	// Retry for up to 2 minutes — server might bounce during credential fetch
	err := retryBackoff(ctx, 2*time.Minute, "get-credentials", t.log, func() error {
		req, err := http.NewRequestWithContext(ctx, "POST", fmt.Sprintf("%s/workers/tasks/%d/credentials", t.base(), taskID), nil)
		if err != nil {
			return err
		}
		t.setHeaders(req)

		resp, err := t.client.Do(req)
		if err != nil {
			return fmt.Errorf("get credentials: %w", err)
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			// Status only: this endpoint's body carries storage
			// credentials and must never reach an error, log or warning.
			return fmt.Errorf("get credentials: HTTP %d", resp.StatusCode)
		}

		if err := decodeJSONBounded(resp.Body, &creds); err != nil {
			// Over-cap stays unwrapped so retryBackoff sees it terminal;
			// every other decode error keeps its old text.
			if errors.Is(err, errResponseTooLarge) {
				return err
			}
			return fmt.Errorf("credentials decode: %w", err)
		}

		return nil
	})
	if err != nil {
		return nil, err
	}
	return &creds, nil
}

// MintRunToken obtains a short-lived run_task JWT for a task from the API's
// worker-mint endpoint, over the worker's already-authenticated channel (the
// same X-Worker-Name / X-Bootstrap-Token headers every other worker call uses).
// Called by the worker's local token endpoint when a node presents a valid
// per-task nonce. The worker never signs tokens itself — it only relays the
// API's. The API resolves the task's tenant_id + workflow_run_id server-side.
//
// A single attempt, no retry: the node-auth helper drives its own retry/refresh
// loop and a node is blocked on this response, so a fast failure it can react to
// beats a long internal backoff. A permanent rejection (task no longer live)
// surfaces as an *HTTPError the caller can inspect.
func (t *APITransport) MintRunToken(ctx context.Context, taskID int) (string, int64, error) {
	raw, err := t.postWithResponse(ctx, fmt.Sprintf("/workers/tasks/%d/run-token", taskID), nil)
	if err != nil {
		return "", 0, err
	}
	var out struct {
		Token string `json:"token"`
		Exp   int64  `json:"exp"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", 0, fmt.Errorf("run-token decode: %w", err)
	}
	if out.Token == "" {
		return "", 0, fmt.Errorf("run-token: API returned empty token")
	}
	return out.Token, out.Exp, nil
}

// ReapableCheckpoints asks the manager which checkpoint volumes may be removed.
//
// The worker holds them and can delete them; only the manager knows whether a
// run is still going to want one. Asking keeps the direction outbound and the
// authority where the truth is. An error means "do not reap" — refusing costs
// disk, reaping wrongly costs a six-hour job.
func (t *APITransport) ReapableCheckpoints(ctx context.Context, volumes []string) ([]string, error) {
	body, err := json.Marshal(map[string]interface{}{"volumes": volumes})
	if err != nil {
		return nil, err
	}
	raw, err := t.postWithResponse(ctx, "/workers/checkpoints/reapable", body)
	if err != nil {
		return nil, err
	}
	var out struct {
		Reapable []string `json:"reapable"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out.Reapable, nil
}

// SendCheckpoints reports node progress as durable state.
//
// Best-effort by design: a dropped checkpoint costs a stale progress bar, and
// failing the task over one would trade a cosmetic loss for a real one.
func (t *APITransport) SendCheckpoints(ctx context.Context, taskID int, cps []Checkpoint) error {
	if len(cps) == 0 {
		return nil
	}
	body, err := json.Marshal(map[string]interface{}{"checkpoints": cps})
	if err != nil {
		return err
	}
	return t.post(ctx, fmt.Sprintf("/workers/tasks/%d/checkpoints", taskID), body)
}

// ReportStorageMigrationProgress reports cumulative object/byte counts for
// a storage_migration task. Best-effort, single attempt (like SendCheckpoints)
// — a stalled progress report must never block or abort the migration itself.
func (t *APITransport) ReportStorageMigrationProgress(ctx context.Context, taskID int, objectCount int, byteCount int64) error {
	// Field names must match routers/worker_tasks.py's
	// StorageMigrationProgressRequest (object_count/byte_count) — NOT the
	// StorageConnection column names (migrated_object_count/migrated_bytes)
	// those values ultimately land in on the API side.
	body, err := json.Marshal(map[string]interface{}{
		"object_count": objectCount,
		"byte_count":   byteCount,
	})
	if err != nil {
		return err
	}
	return t.post(ctx, fmt.Sprintf("/workers/tasks/%d/storage-migration-progress", taskID), body)
}

func (t *APITransport) SendLog(ctx context.Context, taskID int, lines []string) error {
	body, _ := json.Marshal(map[string]interface{}{
		"lines": lines,
	})
	return t.post(ctx, fmt.Sprintf("/workers/tasks/%d/log", taskID), body)
}

func (t *APITransport) ValidatePhase(ctx context.Context, taskID int, phase string, actual ResourceSnapshot) (*ValidationResponse, error) {
	body, err := json.Marshal(map[string]interface{}{
		"phase":            phase,
		"actual_resources": actual,
	})
	if err != nil {
		return nil, err
	}

	path := fmt.Sprintf("/workers/tasks/%d/validate-phase", taskID)
	req, err := http.NewRequestWithContext(ctx, "POST", t.base()+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	t.setHeaders(req)
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("validate-phase: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBodyBytes))
		return nil, fmt.Errorf("validate-phase: HTTP %d: %s", resp.StatusCode, truncateBytes(b, 500))
	}

	var result ValidationResponse
if err := decodeJSONBounded(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("validate-phase: decode: %w", err)
	}
	return &result, nil
}

func (t *APITransport) SendResourceSamples(ctx context.Context, taskID int, samples []ResourceSample) error {
	body, err := json.Marshal(map[string]interface{}{
		"samples": samples,
	})
	if err != nil {
		return err
	}
	return t.post(ctx, fmt.Sprintf("/workers/tasks/%d/resource-samples", taskID), body)
}

// encryptedSecretsResponse is the API's response with X25519+AES-256-GCM encrypted secrets.
type encryptedSecretsResponse struct {
	EncryptedSecrets string `json:"encrypted_secrets"`
	APIPublicKey     string `json:"api_public_key"`
	Nonce            string `json:"nonce"`
}

func (t *APITransport) ResolveSecrets(ctx context.Context, taskID int) (map[string]string, error) {
	// 1. Generate ephemeral X25519 keypair (lives only for this call)
	curve := ecdh.X25519()
	workerPriv, err := curve.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate X25519 key: %w", err)
	}
	workerPubB64 := base64.StdEncoding.EncodeToString(workerPriv.PublicKey().Bytes())

	var envSecrets map[string]string

	err = retryBackoff(ctx, 2*time.Minute, "resolve-secrets", t.log, func() error {
		// 2. Send our public key to the API
		reqBody, _ := json.Marshal(map[string]string{"worker_public_key": workerPubB64})
		req, err := http.NewRequestWithContext(ctx, "POST",
			fmt.Sprintf("%s/workers/tasks/%d/secrets", t.base(), taskID),
			bytes.NewReader(reqBody))
		if err != nil {
			return err
		}
		t.setHeaders(req)
		req.Header.Set("Content-Type", "application/json")

		resp, err := t.client.Do(req)
		if err != nil {
			return fmt.Errorf("resolve secrets: %w", err)
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			b, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBodyBytes))
			return fmt.Errorf("resolve secrets: HTTP %d: %s", resp.StatusCode, truncateBytes(b, 500))
		}

		var envelope encryptedSecretsResponse
		if err := decodeJSONBounded(resp.Body, &envelope); err != nil {
			// Over-cap stays unwrapped so retryBackoff sees it terminal (the
			// secrets envelope is not going to fit on a retry).
			if errors.Is(err, errResponseTooLarge) {
				return err
			}
			return fmt.Errorf("resolve secrets decode: %w", err)
		}

		// Empty response means no secrets to resolve
		if envelope.EncryptedSecrets == "" {
			envSecrets = map[string]string{}
			return nil
		}

		// 3. Decode API's ephemeral public key
		apiPubBytes, err := base64.StdEncoding.DecodeString(envelope.APIPublicKey)
		if err != nil {
			return fmt.Errorf("decode api public key: %w", err)
		}
		apiPub, err := curve.NewPublicKey(apiPubBytes)
		if err != nil {
			return fmt.Errorf("parse api public key: %w", err)
		}

		// 4. ECDH → shared secret → AES-256 key
		sharedSecret, err := workerPriv.ECDH(apiPub)
		if err != nil {
			return fmt.Errorf("ECDH: %w", err)
		}
		keyHash := sha256.Sum256(sharedSecret)
		aesKey := keyHash[:]

		// 5. Decrypt with AES-256-GCM
		ciphertext, err := base64.StdEncoding.DecodeString(envelope.EncryptedSecrets)
		if err != nil {
			return fmt.Errorf("decode ciphertext: %w", err)
		}
		nonce, err := base64.StdEncoding.DecodeString(envelope.Nonce)
		if err != nil {
			return fmt.Errorf("decode nonce: %w", err)
		}

		block, err := aes.NewCipher(aesKey)
		if err != nil {
			return fmt.Errorf("aes cipher: %w", err)
		}
		gcm, err := cipher.NewGCM(block)
		if err != nil {
			return fmt.Errorf("aes gcm: %w", err)
		}
		plaintext, err := gcm.Open(nil, nonce, ciphertext, nil)
		if err != nil {
			return fmt.Errorf("decrypt secrets: %w", err)
		}

		// 6. Parse JSON → env var map
		if err := json.Unmarshal(plaintext, &envSecrets); err != nil {
			return fmt.Errorf("unmarshal secrets: %w", err)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return envSecrets, nil
}

func (t *APITransport) Close() error {
	t.client.CloseIdleConnections()
	return nil
}

// setHeaders adds the worker authentication headers required by all endpoints.
func (t *APITransport) setHeaders(req *http.Request) {
	req.Header.Set("X-Worker-Name", t.workerName)
	req.Header.Set("X-Bootstrap-Token", t.bootstrapToken)
}

func (t *APITransport) post(ctx context.Context, path string, body []byte) error {
	req, err := http.NewRequestWithContext(ctx, "POST", t.base()+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	t.setHeaders(req)
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.client.Do(req)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBodyBytes))
		return &HTTPError{Path: path, StatusCode: resp.StatusCode, Body: truncateBytes(b, 500)}
	}

	return nil
}

// postWithResponse is `post` for the calls that need the body back. Kept
// separate rather than changing `post`'s signature, because every existing
// caller discards the body and threading an ignored return through all of them
// would be churn for one new use.
func (t *APITransport) postWithResponse(ctx context.Context, path string, body []byte) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, "POST", t.base()+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	t.setHeaders(req)
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBodyBytes))
		return nil, &HTTPError{Path: path, StatusCode: resp.StatusCode, Body: truncateBytes(b, 500)}
	}
	b, err := readResponseBody(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return b, nil
}

// HTTPError carries the status code so callers can tell a transient failure
// from a decision. The outbox needs this: a 5xx or a dropped connection means
// "retry later", but a 403 means the manager has reassigned the task and this
// result is no longer wanted — retrying that forever would be a leak.
type HTTPError struct {
	Path       string
	StatusCode int
	Body       string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("%s: HTTP %d: %s", e.Path, e.StatusCode, e.Body)
}

// Permanent reports whether retrying could ever succeed. 4xx means the manager
// understood and refused — except 408 (timeout) and 429 (rate limit), which are
// invitations to try again.
func (e *HTTPError) Permanent() bool {
	if e.StatusCode == http.StatusRequestTimeout || e.StatusCode == http.StatusTooManyRequests {
		return false
	}
	return e.StatusCode >= 400 && e.StatusCode < 500
}

// IsPermanentRejection unwraps err looking for a refusal the manager will not
// change its mind about.
func IsPermanentRejection(err error) bool {
	var he *HTTPError
	if errors.As(err, &he) {
		return he.Permanent()
	}
	return false
}

// IsTerminalRejection is IsPermanentRejection under the name the terminal retry
// loop reads: 4xx the manager answered deliberately (408/429 excluded, they
// invite a retry). A caller that can act on it (skip the container start) must
// get the raw error back rather than retryBackoff's wrapper.
func IsTerminalRejection(err error) bool {
	return IsPermanentRejection(err)
}

// IsConflict is the 409 specifically: /started answers it when the task must
// not run at all — cancelled/expired/failed, or the Fortress compliance gate
// refused it (detail="compliance_gate_refused"). The API has already finalised
// the task, so the worker must abort it, not start the container and not
// report failed/complete afterwards.
func IsConflict(err error) bool {
	var he *HTTPError
	if errors.As(err, &he) {
		return he.StatusCode == http.StatusConflict
	}
	return false
}

// ConflictDetail extracts the API's "detail" string from a 409 body (FastAPI
// shape: {"detail":"compliance_gate_refused"}) so the worker logs WHY the task
// was refused and nothing else — no headers, no tokens, no other body fields.
// Empty when the body is not that shape.
func ConflictDetail(err error) string {
	var he *HTTPError
	if !errors.As(err, &he) {
		return ""
	}
	var parsed map[string]interface{}
	if json.Unmarshal([]byte(he.Body), &parsed) != nil {
		return ""
	}
	detail, ok := parsed["detail"]
	if !ok {
		return ""
	}
	if s, isStr := detail.(string); isStr {
		return s
	}
	return fmt.Sprintf("%v", detail)
}

// ConflictDetailTerminalState returns true when the 409 "detail" string
// indicates the task has already reached a terminal state (cancelled, expired,
// completed, or otherwise final).  When true the outbox reporter must DROP
// the queued report rather than retrying — the manager will not accept it
// again.  Transient conflicts (e.g. "database lock contention") return false
// so the reporter bumps the entry for a bounded retry.
func ConflictDetailTerminalState(err error) bool {
	d := ConflictDetail(err)
	lower := strings.ToLower(d)
	return strings.Contains(lower, "cancelled") ||
		strings.Contains(lower, "expired") ||
		strings.Contains(lower, "failed") ||
		strings.Contains(lower, "completed") ||
		strings.Contains(lower, "already reported") ||
		strings.Contains(lower, "already completed")
}

// mapPollResponse converts the raw API poll response into a typed Task.
func mapPollResponse(taskID *int, queueName *string, workflowRunID *int,
	nodeID *string, nodeRunID *int, claimExpiresAt *string, payload map[string]interface{}) (*Task, error) {

	task := &Task{
		ID: *taskID,
	}

	if queueName != nil {
		task.QueueName = *queueName
	}
	if workflowRunID != nil {
		task.WorkflowRunID = *workflowRunID
	}
	if nodeID != nil {
		task.NodeID = *nodeID
	}
	if nodeRunID != nil {
		task.NodeRunID = *nodeRunID
	}
	if claimExpiresAt != nil {
		if t, err := time.Parse(time.RFC3339, *claimExpiresAt); err == nil {
			task.ClaimExpiresAt = t
		}
	}

	// Re-serialize payload to extract typed fields
	if payload != nil {
		raw, _ := json.Marshal(payload)
		_ = json.Unmarshal(raw, &task.Payload)
	}

	return task, nil
}

func truncateBytes(b []byte, n int) string {
	s := string(b)
	if len(s) > n {
		return s[:n]
	}
	return s
}

// StackState asks the manager, right before a boot_restore starts any stack
// container, whether the operator still wants the stack running (review H4).
// Read-only; the answer carries no secret. Any error means "do not start".
func (t *APITransport) StackState(ctx context.Context, taskID int) (bool, string, error) {
	raw, err := t.postWithResponse(ctx, fmt.Sprintf("/workers/tasks/%d/stack-state", taskID), nil)
	if err != nil {
		return false, "", err
	}
	var out struct {
		Start        bool   `json:"start"`
		DesiredState string `json:"desired_state"`
		Status       string `json:"status"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return false, "", fmt.Errorf("stack-state decode: %w", err)
	}
	return out.Start, out.DesiredState + "/" + out.Status, nil
}
