package transport

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"
)

// DirectTransport implements Transport for embedded and datacenter workers.
// Uses Redis pub/sub for task signaling and direct service access.
// Result reporting still goes through the API (durable Postgres writes).
type DirectTransport struct {
	rdb        *redis.Client
	workerName string
	queues     []string
	apiURL     string // for result reporting (ReportComplete, etc.)
	apiKey     string // service API key from Vault

	// Embedded API transport for result reporting
	apiTransport *APITransport
}

// ErrInvalidRedisURL is the fixed-text error ParseRedisURL returns. It carries
// no part of the URL: go-redis ParseURL returns the raw net/url error whose
// text quotes the whole URL, including the password in redis://:pass@host/.
var ErrInvalidRedisURL = errors.New("transport: invalid Redis URL (parse failed)")

// ParseRedisURL parses a Redis URL and, on failure, returns ErrInvalidRedisURL
// instead of the library error (never wrapped: Error() would carry the URL).
func ParseRedisURL(redisURL string) (*redis.Options, error) {
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, ErrInvalidRedisURL
	}
	return opts, nil
}

// NewDirectTransport creates a transport for embedded/datacenter mode.
// Redis is used for task signaling; the API is still used for durable state changes.
func NewDirectTransport(redisURL, workerName string, queues []string, apiURL, apiKey string, log *slog.Logger) (*DirectTransport, error) {
	opts, err := ParseRedisURL(redisURL)
	if err != nil {
		return nil, err
	}

	rdb := redis.NewClient(opts)

	// Verify connectivity
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis ping: %w", err)
	}

	return &DirectTransport{
		rdb:        rdb,
		workerName: workerName,
		queues:     queues,
		apiURL:     apiURL,
		apiKey:     apiKey,
		// Re-use APITransport for result reporting endpoints.
		// In direct mode, the API is on the same LAN — low latency.
		apiTransport: NewAPITransport(apiURL, workerName, apiKey, queues, log),
	}, nil
}

// WaitForTask subscribes to Redis pub/sub channels for task availability,
// then claims the task via the API (Postgres FOR UPDATE SKIP LOCKED).
//
// Flow:
//  1. SUBSCRIBE to task:available:{queue}:{tenant} channels
//  2. Wait for signal (or periodic fallback poll every 30s)
//  3. Call API to atomically claim the task
//
// This means idle workers create ZERO Postgres load.
func (t *DirectTransport) WaitForTask(ctx context.Context) (*Task, error) {
	// Build channel names for all queues
	channels := make([]string, 0, len(t.queues))
	for _, q := range t.queues {
		// Subscribe to all-tenant channel; API handles tenant filtering during claim
		channels = append(channels, fmt.Sprintf("task:available:%s", q))
	}

	pubsub := t.rdb.Subscribe(ctx, channels...)
	defer pubsub.Close()

	// Fallback timer — short interval since pub/sub messages can be missed
	// during the gap between task completion and re-subscription
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()

	// If the server told us to drain (operator update, pause, or platform
	// update), return nil immediately so main.go can check the
	// self-update branch. Previously this path stayed spinning in the
	// inner for-select because apiTransport.WaitForTask fast-returns
	// (nil,nil) under drain — no error, no task, so the loop kept
	// ticking every 3s forever and the embedded worker never got a
	// chance to drain → self-update → restart. That looked like a
	// "freeze" to the operator.
	if t.apiTransport.Draining() {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(1 * time.Second):
			return nil, nil
		}
	}

	// Try an immediate non-blocking poll — catches tasks enqueued while we were busy
	if task, _ := t.apiTransport.TryClaimNow(ctx); task != nil {
		return task, nil
	}

	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()

		case msg := <-pubsub.Channel():
			// Got a signal that work is available
			_ = msg // we just need the wakeup, claim happens below
			task, err := t.apiTransport.WaitForTask(ctx)
			if err != nil {
				return nil, err
			}
			if task != nil {
				return task, nil
			}
			// Someone else claimed it, keep waiting

		case <-ticker.C:
			// Drain arrived mid-wait: bail out so the main loop can
			// notice and self-update. Same reason as the pre-loop
			// check above.
			if t.apiTransport.Draining() {
				return nil, nil
			}
			// Fallback poll in case pub/sub message was missed
			task, err := t.apiTransport.WaitForTask(ctx)
			if err != nil {
				return nil, err
			}
			if task != nil {
				return task, nil
			}
		}
	}
}

// WaitForStackOp delegates straight to the embedded APITransport — the NOAH
// claim-gate bypass talks directly to the API's claim endpoint rather than
// going through the pub/sub wakeup path WaitForTask uses, since it must stay
// responsive on its own short timeout regardless of whether a Redis publish
// arrives. See Transport.WaitForStackOp.
func (t *DirectTransport) WaitForStackOp(ctx context.Context) (*Task, error) {
	return t.apiTransport.WaitForStackOp(ctx)
}

// ReportStarted delegates to the API (durable state change belongs in Postgres).
func (t *DirectTransport) ReportStarted(ctx context.Context, taskID int, estimatedDuration time.Duration) error {
	return t.apiTransport.ReportStarted(ctx, taskID, estimatedDuration)
}

// ReportComplete delegates to the API.
func (t *DirectTransport) ReportComplete(ctx context.Context, taskID int, result TaskResult) error {
	return t.apiTransport.ReportComplete(ctx, taskID, result)
}

// ReportFailed delegates to the API.
func (t *DirectTransport) ReportFailed(ctx context.Context, taskID int, errMsg string, shouldRetry bool, logTail string, oomKilled bool, errorDetail string) error {
	return t.apiTransport.ReportFailed(ctx, taskID, errMsg, shouldRetry, logTail, oomKilled, errorDetail)
}

// ReportFailedWithEgress delegates to the API.
func (t *DirectTransport) ReportFailedWithEgress(ctx context.Context, taskID int, errMsg string, shouldRetry bool, logTail string, oomKilled bool, errorDetail string, egress *EgressReport) error {
	return t.apiTransport.ReportFailedWithEgress(ctx, taskID, errMsg, shouldRetry, logTail, oomKilled, errorDetail, egress)
}

// GetCredentials for direct mode: delegates to the API transport. Since Gate 2/6
// (remove-minio-versitygw) the API's /workers/tasks/{id}/credentials endpoint is
// the single credential source on all paths, so there is no separate worker-side
// STS exchange to route around.
func (t *DirectTransport) GetCredentials(ctx context.Context, taskID int) (*Credentials, error) {
	return t.apiTransport.GetCredentials(ctx, taskID)
}

// MintRunToken delegates to the API transport — token minting is a durable,
// worker-authenticated API operation, same as GetCredentials.
func (t *DirectTransport) MintRunToken(ctx context.Context, taskID int) (string, int64, error) {
	return t.apiTransport.MintRunToken(ctx, taskID)
}

// SendCheckpoints delegates to the API, like every other durable state change.
//
// Deliberately NOT the Redis shortcut SendLog uses. Logs are signalling and can
// take the fast path; a checkpoint is state, and state that only reaches Redis
// is state nobody can read back after its 24h TTL — or at all, since the
// manager never sees this worker's log lines.
func (t *DirectTransport) SendCheckpoints(ctx context.Context, taskID int, cps []Checkpoint) error {
	return t.apiTransport.SendCheckpoints(ctx, taskID, cps)
}

// ReportStorageMigrationProgress delegates to the API, like every other
// durable state change.
func (t *DirectTransport) ReportStorageMigrationProgress(ctx context.Context, taskID int, objectCount int, byteCount int64) error {
	return t.apiTransport.ReportStorageMigrationProgress(ctx, taskID, objectCount, byteCount)
}

// SendLog writes log lines to Redis (direct, no API round-trip).
func (t *DirectTransport) SendLog(ctx context.Context, taskID int, lines []string) error {
	key := fmt.Sprintf("task:%d:logs", taskID)
	pipe := t.rdb.Pipeline()
	for _, line := range lines {
		pipe.RPush(ctx, key, line)
	}
	pipe.Expire(ctx, key, 24*time.Hour)
	_, err := pipe.Exec(ctx)
	return err
}

// PublishTaskAvailable is called by the API (or Traffic Cop) to signal
// that a new task is available on a queue. Workers subscribed to that
// channel will wake up and attempt to claim it.
func PublishTaskAvailable(rdb *redis.Client, ctx context.Context, queueName string) error {
	channel := fmt.Sprintf("task:available:%s", queueName)
	return rdb.Publish(ctx, channel, "1").Err()
}

// SetWorkerHeartbeat writes this worker's status to a Redis hash with TTL.
func (t *DirectTransport) SetWorkerHeartbeat(ctx context.Context, data map[string]interface{}) error {
	key := fmt.Sprintf("worker:%s:heartbeat", t.workerName)

	jsonData, err := json.Marshal(data)
	if err != nil {
		return err
	}

	pipe := t.rdb.Pipeline()
	pipe.Set(ctx, key, jsonData, 60*time.Second)
	_, err = pipe.Exec(ctx)
	return err
}

// ValidatePhase delegates to the API.
func (t *DirectTransport) ValidatePhase(ctx context.Context, taskID int, phase string, actual ResourceSnapshot) (*ValidationResponse, error) {
	return t.apiTransport.ValidatePhase(ctx, taskID, phase, actual)
}

// SendResourceSamples delegates to the API.
func (t *DirectTransport) SendResourceSamples(ctx context.Context, taskID int, samples []ResourceSample) error {
	return t.apiTransport.SendResourceSamples(ctx, taskID, samples)
}

// ResolveSecrets delegates to the API (secrets are tenant-scoped in Vault,
// the API handles tenant isolation and decryption).
func (t *DirectTransport) ResolveSecrets(ctx context.Context, taskID int) (map[string]string, error) {
	return t.apiTransport.ResolveSecrets(ctx, taskID)
}

// StackState delegates the boot_restore live desired-state check to the API
// (review H4). Without it the embedded (direct) worker fails closed and never
// restores a stack container.
func (t *DirectTransport) StackState(ctx context.Context, taskID int) (bool, string, error) {
	return t.apiTransport.StackState(ctx, taskID)
}

func (t *DirectTransport) Close() error {
	t.apiTransport.Close()
	return t.rdb.Close()
}

// FetchLoopMarkerSetter delegation (BUG.B83). Embedded workers — the ones
// that hit the false-positive stall — are DirectTransports, so the marker
// must reach the embedded APITransport whose pollForTask/TryClaimNow
// actually refresh the stamp on every completed poll round-trip.
func (t *DirectTransport) SetFetchLoopMarker(fn func()) {
	if t.apiTransport != nil {
		t.apiTransport.SetFetchLoopMarker(fn)
	}
}

// ServerStateReceiver + SelfUpdateChecker delegation.
//
// The heartbeat loop pushes server-side coordination signals (drain,
// update-available, server-updating) into whichever transport wants
// them, and the main loop checks those flags before triggering a
// self-update. Previously the worker's main loop hard-coded a
// `tp.(*APITransport)` type assertion, so DirectTransport (embedded +
// datacenter) silently dropped every one of those signals — admins
// clicking Update on an embedded worker ended up in an infinite 403
// "paused or updating" poll loop that only an operator-driven image
// rebuild could resolve.
//
// These delegating methods forward to the APITransport that
// DirectTransport already embeds for result reporting (see line 52),
// which is wired into the same heartbeat and therefore already tracks
// the right state. Now both transports implement the coordination
// interfaces and the main loop can interface-assert instead of
// concrete-type-assert, giving embedded workers proper self-update.
func (t *DirectTransport) SetServerState(updating, drain, updateAvail bool, latestVersion string) {
	if t.apiTransport != nil {
		t.apiTransport.SetServerState(updating, drain, updateAvail, latestVersion)
	}
}

func (t *DirectTransport) SetBaseURL(url string) {
	if t.apiTransport != nil {
		t.apiTransport.SetBaseURL(url)
	}
}

func (t *DirectTransport) Draining() bool {
	if t.apiTransport == nil {
		return false
	}
	return t.apiTransport.Draining()
}

func (t *DirectTransport) UpdateAvailable() (bool, string) {
	if t.apiTransport == nil {
		return false, ""
	}
	return t.apiTransport.UpdateAvailable()
}

func (t *DirectTransport) ServerUpdating() bool {
	if t.apiTransport == nil {
		return false
	}
	return t.apiTransport.ServerUpdating()
}
