// Package credsrefresher implements GSEC Q21.7: the per-task storage-creds
// file and its auto-refresh loop.
//
// The worker writes the task's storage credentials to a single 0600 JSON
// file (<state-dir>/creds/task-{id}.json) that the executor bind-mounts
// read-only into the task container at the same path, so opt-in images can
// re-read refreshed creds without a new worker-side endpoint. The MINIO_*
// env vars remain the bootstrap values (first write) so every existing
// image keeps working byte-for-byte.
//
// While the task is live, one refresher goroutine per task re-calls the
// existing /workers/tasks/{id}/credentials endpoint on a cadence and
// atomically swaps the file. On ANY failure (API down, 5xx, Vault sealed,
// network) it keeps the last good creds, backs off 5m->1h, and reports a
// heartbeat warning — it NEVER fails or kills the task. Only the task
// lifecycle calling Stop() ends the loop and deletes the file (an
// ephemeral credential, not a record).
package credsrefresher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// State-dir convention shared with the executor's image GC
// (internal/executor/imagegc.go) and availability history: MOD_WORKER_STATE_DIR
// when set, otherwise the production default.
const (
	// StateDirEnv is the worker state-dir override, shared with the
	// executor's image GC and availability history.
	StateDirEnv     = "MOD_WORKER_STATE_DIR"
	defaultStateDir = "/opt/mod-worker-state"
	credsSubDir     = "creds"

	minBackoff          = 5 * time.Minute
	maxBackoff          = time.Hour
	defaultBaseInterval = 300 * time.Second
	defaultMinInterval  = 5 * time.Second
	refreshCallTimeout  = 3 * time.Minute
	stopExitGrace       = 3 * time.Second
)

// BaseDir is the worker state directory the creds file lives under.
func BaseDir() string {
	if d := os.Getenv(StateDirEnv); d != "" {
		return d
	}
	return defaultStateDir
}

// TaskCredsPath is the host path of the per-task creds file. The executor
// bind-mounts it read-only into the task container at the SAME path; Q21.9
// (non-root host) shares this constant for its ReadWritePaths.
func TaskCredsPath(taskID int) string {
	return filepath.Join(BaseDir(), credsSubDir, fmt.Sprintf("task-%d.json", taskID))
}

// CredsFile is the JSON written to the creds file.
type CredsFile struct {
	TaskID       int        `json:"task_id"`
	Endpoint     string     `json:"endpoint"`
	AccessKey    string     `json:"access_key"`
	SecretKey    string     `json:"secret_key"`
	SessionToken string     `json:"session_token,omitempty"`
	ExpiresAt    *time.Time `json:"expires_at,omitempty"`
	RefreshedAt  time.Time  `json:"refreshed_at"`
}

// Provider is the credential source — the transport's GetCredentials
// (both APITransport and DirectTransport satisfy it).
type Provider interface {
	GetCredentials(ctx context.Context, taskID int) (*transport.Credentials, error)
}

// Options configures a Refresher. All fields optional (zero ⇒ defaults).
type Options struct {
	Log *slog.Logger
	// BaseInterval is the slow re-validation cadence used when the API
	// reports neither a TTL nor a refresh interval (platform setting
	// worker.creds_refresh_interval; default 300s).
	BaseInterval func() time.Duration
	// Dir overrides the creds directory (tests).
	Dir string
	// Now overrides the clock (tests).
	Now func() time.Time
	// MinInterval floors the effective cadence (default 5s) so a
	// misconfigured API value cannot turn the loop into a storm.
	MinInterval time.Duration
	// BackoffBase is the first failure backoff (default 5m; doubled up
	// to the 1h cap).
	BackoffBase time.Duration
}

// Refresher is the per-task creds-file refresh loop. One goroutine per
// running task; Stop() joins it and deletes the file.
type Refresher struct {
	taskID      int
	path        string
	dir         string
	prov        Provider
	log         *slog.Logger
	now         func() time.Time
	base        func() time.Duration
	minIv       time.Duration
	backoffBase time.Duration

	mu       sync.Mutex
	last     *transport.MinIOCredentials // last good creds (for cadence math)
	backoff  time.Duration
	warn     string
	stopped  bool
	lastGood time.Time

	done     chan struct{}
	wait     chan struct{}
	stopOnce sync.Once
	regID    int
}

// Start writes the initial creds file and begins the refresh loop.
// Returns an error only when the initial file write fails; the caller
// degrades to bootstrap-env-only in that case and must not fail the task.
func Start(prov Provider, taskID int, initial *transport.MinIOCredentials, opts Options) (*Refresher, error) {
	log := opts.Log
	if log == nil {
		log = slog.Default()
	}
	now := time.Now
	if opts.Now != nil {
		now = opts.Now
	}
	base := func() time.Duration { return defaultBaseInterval }
	if opts.BaseInterval != nil {
		base = opts.BaseInterval
	}
	minIv := defaultMinInterval
	if opts.MinInterval > 0 {
		minIv = opts.MinInterval
	}
	backoffBase := minBackoff
	if opts.BackoffBase > 0 {
		backoffBase = opts.BackoffBase
	}
	dir := filepath.Join(BaseDir(), credsSubDir)
	if opts.Dir != "" {
		dir = opts.Dir
	}
	path := filepath.Join(dir, fmt.Sprintf("task-%d.json", taskID))
	if initial != nil {
		if err := writeFile(dir, taskID, path, initial, now()); err != nil {
			return nil, fmt.Errorf("credsrefresher: initial write: %w", err)
		}
	}
	r := &Refresher{
		taskID:      taskID,
		path:        path,
		dir:         dir,
		prov:        prov,
		log:         log,
		now:         now,
		base:        base,
		minIv:       minIv,
		backoffBase: backoffBase,
		last:        initial,
		lastGood:    now(),
		done:        make(chan struct{}),
		wait:        make(chan struct{}),
	}
	register(r)
	go r.loop()
	return r, nil
}

// loop is the single per-task refresh goroutine. It has no maximum
// lifetime (weeks-long tasks): it runs until Stop or an explicit task
// end (409/410 from the refresh call) — and even then the lifecycle
// Stop() is what deletes the file.
func (r *Refresher) loop() {
	defer close(r.wait)
	for {
		timer := time.NewTimer(r.nextInterval())
		select {
		case <-r.done:
			timer.Stop()
			return
		case <-timer.C:
		}
		if r.isStopped() {
			return
		}

		ctx, cancel := context.WithTimeout(context.Background(), refreshCallTimeout)
		creds, err := r.prov.GetCredentials(ctx, r.taskID)
		cancel()

		if err != nil {
			if isExplicitTaskEnd(err) {
				r.log.Info("creds refresh: task no longer live, refresher stopping",
					"task_id", r.taskID, "error", safeErrSummary(err))
				return
			}
			// ANY failure (API down, 5xx, Vault sealed, network): keep the
			// last good creds in the file, back off, warn — never kill the
			// task.
			r.mu.Lock()
			r.backoff = nextBackoff(r.backoff, r.backoffBase)
			age := r.now().Sub(r.lastGood)
			r.warn = fmt.Sprintf("storage-creds refresh failing for task %d (%v since last good: %v)",
				r.taskID, r.backoff, age.Round(time.Second))
			next := r.backoff
			r.mu.Unlock()
			// Never log the provider's message text (it can carry endpoint
			// response content): status class only.
			r.log.Warn("creds refresh failed; keeping last good creds",
				"task_id", r.taskID, "error", safeErrSummary(err), "backoff", next)
			timer = time.NewTimer(next)
			select {
			case <-r.done:
				timer.Stop()
				return
			case <-timer.C:
			}
			continue
		}

		r.mu.Lock()
		wasStopped := r.stopped
		r.last = &creds.MinIO
		r.backoff = 0
		r.warn = ""
		r.lastGood = r.now()
		r.mu.Unlock()
		if wasStopped {
			continue // task ending: no further file swaps
		}
		if err := writeFile(r.dir, r.taskID, r.path, &creds.MinIO, r.now()); err != nil {
			r.mu.Lock()
			r.backoff = r.backoffBase
			r.warn = fmt.Sprintf("storage-creds file write failing for task %d (last good kept)", r.taskID)
			r.mu.Unlock()
			r.log.Warn("creds file write failed; keeping last good file",
				"task_id", r.taskID, "path", r.path, "error", err)
			continue
		}
		r.log.Debug("creds file refreshed", "task_id", r.taskID, "path", r.path)
	}
}

// Path is the host path of the task's creds file (""-safe: always set).
func (r *Refresher) Path() string { return r.path }

// Stop ends the task's refresh loop and deletes the (ephemeral) creds
// file. Idempotent — safe to defer at every task-lifecycle exit.
func (r *Refresher) Stop() {
	r.stopOnce.Do(func() {
		r.mu.Lock()
		r.stopped = true
		r.mu.Unlock()
		close(r.done)
	})
	select {
	case <-r.wait:
	case <-time.After(stopExitGrace):
		r.log.Warn("creds refresher did not exit in time; abandoning (file may re-align once more)",
			"task_id", r.taskID)
	}
	deregister(r.regID)
	if err := os.Remove(r.path); err != nil && !os.IsNotExist(err) {
		r.log.Warn("could not remove creds file", "task_id", r.taskID, "path", r.path, "error", err)
	}
	// A stale .tmp (failed rename, crash mid-write) holds the same creds:
	// it must not outlive the task either.
	if err := removeCredsTmp(r.path + ".tmp"); err != nil {
		r.log.Warn("could not remove creds temp file", "task_id", r.taskID, "error", safeErrSummary(err))
	}
	r.mu.Lock()
	r.warn = ""
	r.mu.Unlock()
}

// Warning is the heartbeat warning this refresher currently carries
// ("" when healthy).
func (r *Refresher) Warning() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.warn
}

// Backoff is the current failure backoff (0 when healthy) — exported for
// tests and operator debugging.
func (r *Refresher) Backoff() time.Duration {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.backoff
}

func (r *Refresher) isStopped() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.stopped
}

// nextInterval computes the cadence for the next refresh from the last
// good creds: the API's refresh_interval_s when present, ~50% of the
// issued TTL when present, and the tunable base otherwise. A cred with
// neither field therefore refreshes at the base cadence only — no storm.
func (r *Refresher) nextInterval() time.Duration {
	base := r.base()
	if base <= 0 {
		base = defaultBaseInterval
	}
	iv := base
	r.mu.Lock()
	last := r.last
	r.mu.Unlock()
	if last != nil {
		if last.RefreshIntervalS > 0 {
			if d := time.Duration(last.RefreshIntervalS) * time.Second; d < iv {
				iv = d
			}
		}
		if !last.ExpiresAt.IsZero() {
			remaining := last.ExpiresAt.Sub(r.now())
			if remaining > 0 {
				if d := remaining / 2; d < iv {
					iv = d
				}
			}
		}
	}
	if iv < r.minIv {
		iv = r.minIv
	}
	return iv
}

// nextBackoff grows the failure backoff base -> 2x -> 4x ... up to the
// 1h cap.
func nextBackoff(cur, base time.Duration) time.Duration {
	if cur == 0 {
		return base
	}
	cur *= 2
	if cur > maxBackoff {
		cur = maxBackoff
	}
	return cur
}

// isExplicitTaskEnd reports whether the refresh call came back with a
// 409/410 — the task no longer CLAIMED/RUNNING, i.e. an explicit
// cancel/finish. The loop stops on that; every other error is transient.
func isExplicitTaskEnd(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "HTTP 409") || strings.Contains(msg, "HTTP 410")
}

// writeFile atomically (tmp + rename, same dir) writes the creds JSON to
// a 0600 file in a 0700 dir, so a reader never sees a partial file.
func writeFile(dir string, taskID int, path string, c *transport.MinIOCredentials, now time.Time) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	cf := CredsFile{
		TaskID:       taskID,
		Endpoint:     c.Endpoint,
		AccessKey:    c.AccessKey,
		SecretKey:    c.SecretKey,
		SessionToken: c.SessionToken,
		RefreshedAt:  now,
	}
	if !c.ExpiresAt.IsZero() {
		exp := c.ExpiresAt
		cf.ExpiresAt = &exp
	}
	data, err := json.Marshal(cf)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	// fail removes the temp file (it holds the creds) before returning err.
	// A cleanup failure is reported by class only, never by path or content.
	fail := func(err error) error {
		if rmErr := removeCredsTmp(tmp); rmErr != nil {
			return fmt.Errorf("%w (temp cleanup also failed: %s)", err, safeErrSummary(rmErr))
		}
		return err
	}
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fail(err)
	}
	if err := os.Chmod(tmp, 0o600); err != nil { // enforce regardless of umask
		return fail(err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fail(err)
	}
	return nil
}

// removeCredsTmp deletes a creds temp file; a missing file is not an error.
func removeCredsTmp(tmp string) error {
	if err := os.Remove(tmp); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// httpStatusRe pulls only the status digits out of a provider error text.
var httpStatusRe = regexp.MustCompile(`HTTP (\d{3})`)

// safeErrSummary renders an error for logs without its message text: the
// HTTP status class when one is present, otherwise the innermost error's
// type name. The provider's message (which may echo response bodies) never
// reaches a log line.
func safeErrSummary(err error) string {
	if err == nil {
		return ""
	}
	if m := httpStatusRe.FindStringSubmatch(err.Error()); m != nil {
		return "http_" + m[1]
	}
	inner := err
	for {
		next := errors.Unwrap(inner)
		if next == nil {
			break
		}
		inner = next
	}
	return fmt.Sprintf("%T", inner)
}

// Package-level registry of live refreshers so the heartbeat can report
// per-task refresh failures in its warnings field.
var (
	regMu    sync.Mutex
	regSeq   int
	registry = map[int]*Refresher{}
)

func register(r *Refresher) {
	regMu.Lock()
	regSeq++
	r.regID = regSeq
	registry[r.regID] = r
	regMu.Unlock()
}

func deregister(id int) {
	regMu.Lock()
	delete(registry, id)
	regMu.Unlock()
}

// Warnings returns the current heartbeat warnings for all live task
// refreshers (empty when healthy). Wired to the heartbeat in main.
func Warnings() []string {
	regMu.Lock()
	defer regMu.Unlock()
	var out []string
	for _, r := range registry {
		if w := r.Warning(); w != "" {
			out = append(out, w)
		}
	}
	return out
}
