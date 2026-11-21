package credsrefresher

// Q21.10 coverage gaps on top of credsrefresher_test.go (which already pins
// the initial 0600 write, half-TTL cadence math, backoff growth, last-good
// retention, 409 self-stop, torn-read safety and the warnings registry).
// Added here:
//   - the refresh actually lands BEFORE the issued expiry (cadence, not math)
//   - a long failure streak retries and alerts but never cancels the task
//   - the REWRITE path enforces 0600 regardless of the pre-existing mode
//   - a multi-day task with an injected clock survives many rotations
//   - Stop() at task completion is not blocked by a pending backoff sleep

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// q21Provider records every call (and the clock reading at call time) so the
// tests can assert cadence, not just interval arithmetic.
type q21Provider struct {
	mu       sync.Mutex
	calls    int
	seen     []time.Time
	creds    *transport.Credentials
	errs     []error
	perCall  func(n int) *transport.Credentials // rotating creds per call
}

func (f *q21Provider) GetCredentials(ctx context.Context, taskID int) (*transport.Credentials, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	n := f.calls
	if n <= len(f.errs) && f.errs[n-1] != nil {
		return nil, f.errs[n-1]
	}
	if f.perCall != nil {
		return f.perCall(n), nil
	}
	if f.creds == nil {
		return &transport.Credentials{}, nil
	}
	return f.creds, nil
}

func (f *q21Provider) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func q21Opts(dir string) Options {
	return Options{
		Dir:          dir,
		BaseInterval: func() time.Duration { return 10 * time.Millisecond },
		MinInterval:  time.Millisecond,
		BackoffBase:  10 * time.Millisecond,
	}
}

func q21Read(t *testing.T, path string) (CredsFile, os.FileMode) {
	t.Helper()
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("creds file missing: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	var cf CredsFile
	if err := json.Unmarshal(data, &cf); err != nil {
		t.Fatalf("torn/invalid JSON: %v", err)
	}
	return cf, fi.Mode().Perm()
}

// TestRefreshLandsBeforeExpiry: a short issued TTL must produce a refresh at
// ~50% of it, so the file is re-written while the creds are still valid.
func TestRefreshLandsBeforeExpiry(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-31.json")
	// The TTL is virtual: on a loaded -race container a 60 ms wall TTL is
	// routinely overtaken by machine jitter, so "the refresh lands before the
	// issued expiry" must be pinned in injected time, not real time.
	ttl := 60 * time.Millisecond
	var mu sync.Mutex
	vclock := time.Now()
	vnow := func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		vclock = vclock.Add(10 * time.Millisecond)
		return vclock
	}
	rot := func(n int) *transport.Credentials {
		mu.Lock()
		exp := vclock.Add(ttl)
		mu.Unlock()
		return &transport.Credentials{MinIO: transport.MinIOCredentials{
			AccessKey: "ak-rot",
			ExpiresAt: exp,
		}}
	}
	fp := &q21Provider{creds: &transport.Credentials{MinIO: transport.MinIOCredentials{
		AccessKey: "ak-rot", ExpiresAt: vclock.Add(ttl),
	}}, perCall: rot}
	opts := q21Opts(dir)
	opts.Now = vnow
	r, err := Start(fp, 31, &fp.creds.MinIO, opts)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && fp.count() < 3 {
		time.Sleep(5 * time.Millisecond)
	}
	cf, perm := q21Read(t, path)
	if cf.AccessKey != "ak-rot" {
		t.Fatalf("file not refreshed: %+v", cf)
	}
	if perm != 0o600 {
		t.Fatalf("rewritten file perms = %v, want 0600", perm)
	}
	if cf.ExpiresAt == nil || !cf.RefreshedAt.Before(*cf.ExpiresAt) {
		t.Fatalf("file was written at/after the creds expired (no pre-expiry refresh): %+v", cf)
	}
	if got := fp.count(); got < 2 {
		t.Fatalf("provider calls = %d, want >=2 (cadence must be well under the TTL)", got)
	}
	if iv := r.nextInterval(); iv >= ttl {
		t.Fatalf("cadence %v is not earlier than the %v TTL", iv, ttl)
	}
}

// TestFailureStreakRetriesAndNeverCancelsTask: many consecutive failures grow
// the backoff and raise a heartbeat alert, but the refresher stays alive and
// re-aligns the file once the API returns — the task is never cancelled.
func TestFailureStreakRetriesAndNeverCancelsTask(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-32.json")
	apiDown := errors.New("get credentials: HTTP 500: api down")
	streak := make([]error, 6)
	for i := range streak {
		streak[i] = apiDown
	}
	fp := &q21Provider{
		creds: &transport.Credentials{MinIO: transport.MinIOCredentials{AccessKey: "recovered-ak"}},
		errs:  streak,
	}
	r, err := Start(fp, 32, &transport.MinIOCredentials{AccessKey: "last-good-ak"}, q21Opts(dir))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && r.Backoff() < 320*time.Millisecond {
		time.Sleep(5 * time.Millisecond)
	}
	if r.Backoff() < 320*time.Millisecond {
		t.Fatalf("backoff after 6 failures = %v, want >=320ms (5x doubling)", r.Backoff())
	}
	if w := r.Warning(); !strings.Contains(w, "task 32") {
		t.Fatalf("no heartbeat alert during the streak: %q", w)
	}
	if cf, _ := q21Read(t, path); cf.AccessKey != "last-good-ak" {
		t.Fatalf("last good creds overwritten during failure: %+v", cf)
	}
	if r.isStopped() {
		t.Fatal("failure streak must not stop the refresher (task must survive)")
	}

	deadline = time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cf, _ := q21Read(t, path); cf.AccessKey == "recovered-ak" {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if cf, _ := q21Read(t, path); cf.AccessKey != "recovered-ak" {
		t.Fatalf("no retry after the streak: %+v", cf)
	}
	if r.Backoff() != 0 || r.Warning() != "" {
		t.Fatalf("state after recovery: backoff=%v warn=%q", r.Backoff(), r.Warning())
	}
}

// TestRewriteEnforces0600RegardlessOfExistingMode: the atomic swap must chmod
// the tmp file before rename, so a pre-existing looser-mode file is tightened
// on the first refresh (the executor bind-mounts this path read-only).
func TestRewriteEnforces0600RegardlessOfExistingMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-33.json")
	if err := os.WriteFile(path, []byte(`{"task_id":33,"access_key":"old"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	fp := &q21Provider{creds: &transport.Credentials{MinIO: transport.MinIOCredentials{AccessKey: "new-ak"}}}
	r, err := Start(fp, 33, &fp.creds.MinIO, q21Opts(dir))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		cf, perm := q21Read(t, path)
		if cf.AccessKey == "new-ak" {
			if perm != 0o600 {
				t.Fatalf("rewritten perms = %v, want 0600", perm)
			}
			entries, _ := os.ReadDir(dir)
			for _, e := range entries {
				if strings.HasSuffix(e.Name(), ".tmp") {
					t.Fatalf("stray tmp left by the atomic swap: %s", e.Name())
				}
			}
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("file was never rewritten")
}

// TestMultiDayTaskSurvivesManyRotations: an injected clock advancing one hour
// per call simulates a task that lives for days. The loop has no maximum
// lifetime, so hundreds of rotations must all land, a transient failure must
// not end them, and Stop() at completion still removes the file.
func TestMultiDayTaskSurvivesManyRotations(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-34.json")

	var mu sync.Mutex
	clock := time.Date(2026, 10, 4, 0, 0, 0, 0, time.UTC)
	peekClock := func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		return clock
	}
	now := func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		return clock
	}

	fp := &q21Provider{perCall: func(n int) *transport.Credentials {
		mu.Lock()
		// the virtual clock ticks once per refresh attempt, not per reading
		clock = clock.Add(time.Hour)
		exp := clock.Add(2 * time.Hour)
		mu.Unlock()
		return &transport.Credentials{MinIO: transport.MinIOCredentials{
			AccessKey:       "rot-" + strconv.Itoa(n),
			ExpiresAt:       exp,
		}}
	}}
	// one transient failure in the middle of the run must not end the run
	fp.errs = []error{nil, nil, errors.New("get credentials: HTTP 503: sealed"), nil}

	opts := q21Opts(dir)
	opts.Now = now
	r, err := Start(fp, 34, &transport.MinIOCredentials{AccessKey: "rot-1"}, opts)
	if err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) && fp.count() < 150 {
		time.Sleep(5 * time.Millisecond)
	}
	if got := fp.count(); got < 150 {
		t.Fatalf("rotations = %d, want >=150 (multi-day task must survive)", got)
	}
	cf, perm := q21Read(t, path)
	if perm != 0o600 {
		t.Fatalf("perms after many rotations = %v, want 0600", perm)
	}
	if cf.RefreshedAt.Before(peekClock().Add(-2 * time.Hour)) {
		t.Fatalf("refreshed_at %v did not track the injected clock %v", cf.RefreshedAt, peekClock())
	}
	if r.isStopped() {
		t.Fatal("transient failure stopped the multi-day loop")
	}
	r.Stop()
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("Stop at task completion must delete the ephemeral file: %v", err)
	}
}

// TestStopIsNotBlockedByPendingBackoff: Stop() at task completion exits
// promptly even while the loop is sleeping in a failure backoff, and clears
// the alert it had raised.
func TestStopIsNotBlockedByPendingBackoff(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-35.json")
	fp := &q21Provider{errs: make([]error, 500)}
	for i := range fp.errs {
		fp.errs[i] = errors.New("get credentials: HTTP 500: api down")
	}
	opts := q21Opts(dir)
	opts.BackoffBase = 2 * time.Second // long pending sleep
	r, err := Start(fp, 35, &transport.MinIOCredentials{AccessKey: "ak"}, opts)
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && r.Backoff() == 0 {
		time.Sleep(5 * time.Millisecond)
	}
	if r.Backoff() == 0 {
		t.Fatalf("no backoff recorded before Stop: calls=%d", fp.count())
	}
	started := time.Now()
	r.Stop()
	if elapsed := time.Since(started); elapsed > stopExitGrace {
		t.Fatalf("Stop waited %v for the pending backoff sleep", elapsed)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("file not removed on completion: %v", err)
	}
	if r.Warning() != "" {
		t.Fatalf("alert not cleared on Stop: %q", r.Warning())
	}
}

