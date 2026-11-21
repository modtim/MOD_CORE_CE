package credsrefresher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// fakeProvider scripts per-call outcomes; call counts are asserted to pin
// the cadence/stop behaviour of the loop.
type fakeProvider struct {
	mu    sync.Mutex
	calls int
	creds *transport.Credentials
	errs  []error // per-call, in order; beyond len -> success with creds
}

func (f *fakeProvider) GetCredentials(ctx context.Context, taskID int) (*transport.Credentials, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	n := f.calls
	if n <= len(f.errs) && f.errs[n-1] != nil {
		return nil, f.errs[n-1]
	}
	c := f.creds
	if c == nil {
		c = &transport.Credentials{}
	}
	return c, nil
}

func (f *fakeProvider) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func fastOpts(dir string) Options {
	return Options{
		Dir:          dir,
		BaseInterval: func() time.Duration { return 10 * time.Millisecond },
		MinInterval:  time.Millisecond,
		BackoffBase:  10 * time.Millisecond,
	}
}

func credsFile(t *testing.T, path string) CredsFile {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read creds file: %v", err)
	}
	var cf CredsFile
	if err := json.Unmarshal(data, &cf); err != nil {
		t.Fatalf("creds file not valid JSON: %v", err)
	}
	return cf
}

func TestInitialWriteFilePerms(t *testing.T) {
	// Use a creds subdir the refresher creates itself, so the 0700 check
	// exercises MkdirAll (t.TempDir() itself is 0755).
	dir := filepath.Join(t.TempDir(), "creds")
	fp := &fakeProvider{creds: &transport.Credentials{MinIO: transport.MinIOCredentials{
		AccessKey: "ak1", SecretKey: "sk1", Endpoint: "s3.example.com",
	}}}
	r, err := Start(fp, 7, &fp.creds.MinIO, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()
	path := filepath.Join(dir, "task-7.json")
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("creds file missing: %v", err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("file perms = %v, want 0600", fi.Mode().Perm())
	}
	di, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if di.Mode().Perm() != 0o700 {
		t.Fatalf("dir perms = %v, want 0700", di.Mode().Perm())
	}
	cf := credsFile(t, path)
	if cf.TaskID != 7 || cf.AccessKey != "ak1" || cf.SecretKey != "sk1" || cf.Endpoint != "s3.example.com" {
		t.Fatalf("creds file content = %+v", cf)
	}
}

func TestTaskCredsPathFollowsStateDirEnv(t *testing.T) {
	t.Setenv("MOD_WORKER_STATE_DIR", "/tmp/custom-state")
	if got := TaskCredsPath(42); got != "/tmp/custom-state/creds/task-42.json" {
		t.Fatalf("TaskCredsPath = %q", got)
	}
	t.Setenv("MOD_WORKER_STATE_DIR", "")
	if got := TaskCredsPath(42); got != "/opt/mod-worker-state/creds/task-42.json" {
		t.Fatalf("default TaskCredsPath = %q", got)
	}
}

// TestNextIntervalHalfTTL pins the "refresh at ~50% of the issued TTL"
// math and the no-expiry ⇒ base-cadence-only (no storm) rule.
func TestNextIntervalHalfTTL(t *testing.T) {
	now := time.Date(2026, 10, 4, 12, 0, 0, 0, time.UTC)
	mk := func() *Refresher {
		return &Refresher{
			now:   func() time.Time { return now },
			base:  func() time.Duration { return 300 * time.Second },
			minIv: defaultMinInterval,
			regID: 0,
		}
	}
	cases := []struct {
		name string
		c    *transport.MinIOCredentials
		want time.Duration
	}{
		{"no expiry no interval => base only",
			&transport.MinIOCredentials{}, 300 * time.Second},
		{"half TTL shorter than base",
			&transport.MinIOCredentials{ExpiresAt: now.Add(4 * time.Minute)}, 2 * time.Minute},
		{"half TTL longer than base",
			&transport.MinIOCredentials{ExpiresAt: now.Add(2 * time.Hour)}, 300 * time.Second},
		{"refresh_interval_s honoured",
			&transport.MinIOCredentials{RefreshIntervalS: 60}, 60 * time.Second},
		{"both => min of half-TTL and interval",
			&transport.MinIOCredentials{ExpiresAt: now.Add(10 * time.Minute), RefreshIntervalS: 900}, 5 * time.Minute},
		{"misconfigured 1s floored",
			&transport.MinIOCredentials{RefreshIntervalS: 1}, defaultMinInterval},
		{"expired creds => base only",
			&transport.MinIOCredentials{ExpiresAt: now.Add(-time.Minute)}, 300 * time.Second},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := mk()
			r.last = tc.c
			if got := r.nextInterval(); got != tc.want {
				t.Fatalf("nextInterval = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestNextBackoffGrowthAndCap(t *testing.T) {
	got := nextBackoff(0, minBackoff)
	for i, want := range []time.Duration{5 * time.Minute, 10 * time.Minute, 20 * time.Minute, 40 * time.Minute, time.Hour, time.Hour} {
		if i > 0 {
			got = nextBackoff(got, minBackoff)
		}
		if got != want {
			t.Fatalf("backoff[%d] = %v, want %v", i, got, want)
		}
	}
}

// TestFailureKeepsLastGoodAndBackoff: API down / 5xx keeps the last good
// file untouched, grows backoff, warns; recovery re-aligns the file and
// resets backoff. The task itself is never failed by this package.
func TestFailureKeepsLastGoodAndBackoff(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-3.json")
	apiDown := errors.New("get credentials: HTTP 500: internal error")
	fp := &fakeProvider{
		creds: &transport.Credentials{MinIO: transport.MinIOCredentials{AccessKey: "new-ak"}},
		errs:  []error{apiDown, apiDown, nil},
	}
	r, err := Start(fp, 3, &transport.MinIOCredentials{AccessKey: "good-ak"}, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()

	// Poll until TWO failures are recorded (backoff reached 2x BackoffBase)
	// — count() bumps before the loop records each failure, so the backoff is
	// the stable signal.
	deadline := time.Now().Add(5 * time.Second)
	for r.Backoff() != 20*time.Millisecond && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if r.Backoff() != 20*time.Millisecond {
		t.Fatalf("backoff after 2 failures = %v, want 20ms (2x BackoffBase); calls=%d", r.Backoff(), fp.count())
	}
	if cf := credsFile(t, path); cf.AccessKey != "good-ak" {
		t.Fatalf("file changed during API failure: %+v", cf)
	}
	if w := r.Warning(); !strings.Contains(w, "task 3") {
		t.Fatalf("warning = %q, want mention of task 3", w)
	}

	// Third call succeeds: file re-aligns (poll the file — count() bumps
	// before the loop's writeFile lands), backoff + warning clear.
	deadline = time.Now().Add(5 * time.Second)
	var cf CredsFile
	for time.Now().Before(deadline) {
		cf = credsFile(t, path)
		if cf.AccessKey == "new-ak" {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if cf.AccessKey != "new-ak" {
		t.Fatalf("file not re-aligned after recovery: %+v", cf)
	}
	if r.Backoff() != 0 || r.Warning() != "" {
		t.Fatalf("state after recovery: backoff=%v warn=%q", r.Backoff(), r.Warning())
	}
}

// TestStopOnTaskEnd: Stop() joins the goroutine (no leak), deletes the
// creds file, and the provider is no longer called.
func TestStopOnTaskEnd(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-9.json")
	fp := &fakeProvider{}
	r, err := Start(fp, 9, &transport.MinIOCredentials{AccessKey: "ak"}, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	r.Stop()
	r.Stop() // idempotent
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("creds file not removed after Stop: %v", err)
	}
	before := fp.count()
	time.Sleep(80 * time.Millisecond) // several base intervals elapse
	if got := fp.count(); got != before {
		t.Fatalf("provider called after Stop: %d -> %d", before, got)
	}
}

// TestExplicit409StopsLoop: a 409/410 (task no longer live) ends the loop
// on its own; the file survives until the lifecycle Stop() deletes it.
func TestExplicit409StopsLoop(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-5.json")
	fp := &fakeProvider{errs: []error{errors.New("get credentials: HTTP 409: task not running")}}
	r, err := Start(fp, 5, &transport.MinIOCredentials{AccessKey: "ak"}, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	// Wait for the loop to make its (failing) first call, then let the
	// self-stop settle.
	deadline := time.Now().Add(5 * time.Second)
	for fp.count() < 1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(80 * time.Millisecond)
	if got, want := fp.count(), 1; got != want {
		t.Fatalf("provider calls after explicit 409 = %d, want 1", got)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("file must survive until lifecycle Stop: %v", err)
	}
	r.Stop()
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("Stop must delete the file: %v", err)
	}
}

// TestAtomicWriteNoTornReads: readers polling the file through many
// refreshes never observe a partial/invalid document (tmp+rename), and no
// .tmp file lingers.
func TestAtomicWriteNoTornReads(t *testing.T) {
	dir := t.TempDir()
	fp := &fakeProvider{creds: &transport.Credentials{MinIO: transport.MinIOCredentials{AccessKey: "ak", SecretKey: "sk"}}}
	r, err := Start(fp, 11, &fp.creds.MinIO, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()
	path := filepath.Join(dir, "task-11.json")
	var bad int
	deadline := time.Now().Add(300 * time.Millisecond)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read: %v", err)
		}
		var cf CredsFile
		if err := json.Unmarshal(data, &cf); err != nil {
			bad++
		}
		if bad > 3 {
			t.Fatalf("too many torn reads: %d", bad)
		}
	}
	// Stop first: the atomic write is tmp + rename, so a .tmp is on disk
	// only for the duration of one write. Scanning while the loop is still
	// running races the rename; the torn-read assertion above is the part
	// that must hold concurrently.
	r.Stop()
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Fatalf("stray tmp file: %s", e.Name())
		}
	}
}

// TestWarningsRegistry: a failing refresher surfaces in Warnings(); a
// healthy one does not; Stop clears it.
func TestWarningsRegistry(t *testing.T) {
	dir := t.TempDir()
	fp := &fakeProvider{errs: make([]error, 1000)}
	for i := range fp.errs {
		fp.errs[i] = errors.New("get credentials: HTTP 503: service unavailable")
	}
	r, err := Start(fp, 21, &transport.MinIOCredentials{AccessKey: "ak"}, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for fp.count() < 1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	var found bool
	for i := 0; i < 50; i++ {
		for _, w := range Warnings() {
			if strings.Contains(w, "task 21") {
				found = true
			}
		}
		if found {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !found {
		t.Fatalf("Warnings() did not surface task 21: %v", Warnings())
	}
	r.Stop()
	if w := Warnings(); len(w) != 0 {
		t.Fatalf("Warnings after Stop = %v, want empty", w)
	}
}
