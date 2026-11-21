package main

// GP.17e unit tests for the wslc NOAH session-lifecycle manager. Everything
// is faked: no wslc.exe, no WSL, no real backend — the manager is tested
// through its injected closures, clock, and a recording backend double.

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
)

// fakeWslcBackend records Submit/Cancel calls and reports every task as
// running until the test tells it otherwise.
type fakeWslcBackend struct {
	mu              sync.Mutex
	submits         []*executor.Handle
	cancels         []*executor.Handle
	clearLogins     int // GP.17f: ClearRegistryLogins invocations
}

func (f *fakeWslcBackend) Submit(_ context.Context, spec *executor.TaskSpec) (*executor.Handle, error) {
	h := &executor.Handle{Backend: "wslc", ID: spec.Env["MOD_TASK_ID"]}
	f.mu.Lock()
	f.submits = append(f.submits, h)
	f.mu.Unlock()
	return h, nil
}

func (f *fakeWslcBackend) Poll(context.Context, *executor.Handle) (*executor.TaskStatus, error) {
	return &executor.TaskStatus{Phase: executor.TaskRunning}, nil
}

func (f *fakeWslcBackend) Cancel(_ context.Context, h *executor.Handle) error {
	f.mu.Lock()
	f.cancels = append(f.cancels, h)
	f.mu.Unlock()
	return nil
}

func (f *fakeWslcBackend) ClearRegistryLogins() {
	f.mu.Lock()
	f.clearLogins++
	f.mu.Unlock()
}

// wslcTestEnv wires a manager with a controllable clock and counting
// session start/stop closures.
type wslcTestEnv struct {
	m        *wslcSessionManager
	backend  *fakeWslcBackend
	starts   int
	stops    int
	startErr error
	now      time.Time
	mu       sync.Mutex // guards now/starts/stops for the concurrent test
}

func newWslcTestEnv(t *testing.T) *wslcTestEnv {
	t.Helper()
	env := &wslcTestEnv{
		backend: &fakeWslcBackend{},
		now:     time.Date(2026, 9, 30, 12, 0, 0, 0, time.UTC),
	}
	env.m = newWslcSessionManager(slog.New(slog.NewTextHandler(io.Discard, nil)))
	env.m.backend = env.backend
	env.m.Clock = func() time.Time {
		env.mu.Lock()
		defer env.mu.Unlock()
		return env.now
	}
	env.m.StartSession = func() error {
		env.mu.Lock()
		defer env.mu.Unlock()
		env.starts++
		return env.startErr
	}
	env.m.StopSession = func() error {
		env.mu.Lock()
		env.stops++
		env.mu.Unlock()
		return nil
	}
	return env
}

func (env *wslcTestEnv) advance(d time.Duration) {
	env.mu.Lock()
	defer env.mu.Unlock()
	env.now = env.now.Add(d)
}

func (env *wslcTestEnv) startCount() int {
	env.mu.Lock()
	defer env.mu.Unlock()
	return env.starts
}

func (env *wslcTestEnv) stopCount() int {
	env.mu.Lock()
	defer env.mu.Unlock()
	return env.stops
}

// 1. Lazy start on first claim; no double-start on subsequent claims.
func TestWslcSessionLazyStartNoDoubleStart(t *testing.T) {
	env := newWslcTestEnv(t)

	if live, _, _ := env.m.Status(); live {
		t.Fatal("session must be off before any claim")
	}
	if env.startCount() != 0 {
		t.Fatal("no session start expected before the first claim")
	}

	if err := env.m.OnTaskClaimed(1, &executor.Handle{Backend: "wslc", ID: "1"}); err != nil {
		t.Fatalf("first claim: %v", err)
	}
	if err := env.m.OnTaskClaimed(2, &executor.Handle{Backend: "wslc", ID: "2"}); err != nil {
		t.Fatalf("second claim: %v", err)
	}
	if got := env.startCount(); got != 1 {
		t.Fatalf("session started %d times, want exactly 1 (lazy + no double-start)", got)
	}
	if live, n, _ := env.m.Status(); !live || n != 2 {
		t.Fatalf("Status = (live=%v tasks=%d), want (true 2)", live, n)
	}
}

// 2. Yield hard-evicts: Cancel is called for EVERY running task, then the
// session is stopped.
func TestWslcSessionYieldCancelsAllThenStops(t *testing.T) {
	env := newWslcTestEnv(t)
	h1 := &executor.Handle{Backend: "wslc", ID: "1"}
	h2 := &executor.Handle{Backend: "wslc", ID: "2"}
	if err := env.m.OnTaskClaimed(1, h1); err != nil {
		t.Fatal(err)
	}
	if err := env.m.OnTaskClaimed(2, h2); err != nil {
		t.Fatal(err)
	}

	env.m.OnYield("user returned")

	env.backend.mu.Lock()
	got := append([]*executor.Handle(nil), env.backend.cancels...)
	env.backend.mu.Unlock()
	if len(got) != 2 {
		t.Fatalf("Cancel called %d times, want 2 (every running task)", len(got))
	}
	seen := map[string]bool{}
	for _, h := range got {
		seen[h.ID] = true
	}
	if !seen["1"] || !seen["2"] {
		t.Fatalf("expected cancels for tasks 1 and 2, got %v", seen)
	}
	if !env.m.wasPreempted(1) || !env.m.wasPreempted(2) {
		t.Fatal("evicted tasks must be flagged preempted (for the [noah-preempted] requeue report)")
	}
	if env.stopCount() != 1 {
		t.Fatalf("session stop called %d times, want 1 (after cancels)", env.stopCount())
	}
	if live, n, _ := env.m.Status(); live || n != 0 {
		t.Fatalf("after yield Status = (live=%v tasks=%d), want (false 0)", live, n)
	}
}

// 3. Idle timeout stops the session; a claim during the idle window resets
// the timer (no stop happens).
func TestWslcSessionIdleTimeoutAndReset(t *testing.T) {
	env := newWslcTestEnv(t)
	if err := env.m.OnTaskClaimed(1, &executor.Handle{Backend: "wslc", ID: "1"}); err != nil {
		t.Fatal(err)
	}
	env.m.OnTaskFinished(1)

	// 9m idle: still under the 10m default.
	env.advance(9 * time.Minute)
	env.m.Tick(env.now)
	if env.stopCount() != 0 {
		t.Fatal("session stopped before the idle timeout elapsed")
	}

	// A task arrives at 9m10s: timer resets.
	if err := env.m.OnTaskClaimed(2, &executor.Handle{Backend: "wslc", ID: "2"}); err != nil {
		t.Fatal(err)
	}
	env.advance(9 * time.Minute) // 18m10s from the first finish — would have
	// fired if the timer had not been reset.
	env.m.Tick(env.now)
	if env.stopCount() != 0 {
		t.Fatal("idle timer was not reset by the new task")
	}
	env.m.OnTaskFinished(2)

	// Now let the timeout actually elapse.
	env.advance(10 * time.Minute)
	env.m.Tick(env.now)
	if env.stopCount() != 1 {
		t.Fatalf("session stop called %d times after idle timeout, want 1", env.stopCount())
	}
	if live, _, _ := env.m.Status(); live {
		t.Fatal("session still live after idle-timeout stop")
	}

	// A further tick must not stop it again.
	env.advance(10 * time.Minute)
	env.m.Tick(env.now)
	if env.stopCount() != 1 {
		t.Fatalf("idle timeout stopped the session %d times, want 1", env.stopCount())
	}
}

// 4. Yield with no session (nothing ever claimed, or nothing left) is a
// no-op: no Cancel, no stop.
func TestWslcSessionYieldNoSessionNoOp(t *testing.T) {
	env := newWslcTestEnv(t)
	env.m.OnYield("user returned") // never started
	if env.stopCount() != 0 || env.startCount() != 0 {
		t.Fatalf("yield with no session must be a no-op (starts=%d stops=%d)",
			env.startCount(), env.stopCount())
	}
	env.backend.mu.Lock()
	n := len(env.backend.cancels)
	env.backend.mu.Unlock()
	if n != 0 {
		t.Fatalf("yield with no session cancelled %d tasks, want 0", n)
	}

	// And after the session already went idle-stopped:
	env.m.OnTaskClaimed(1, &executor.Handle{Backend: "wslc", ID: "1"})
	env.m.OnTaskFinished(1)
	env.advance(10 * time.Minute)
	env.m.Tick(env.now)
	if env.stopCount() != 1 {
		t.Fatalf("setup: expected 1 stop, got %d", env.stopCount())
	}
	env.m.OnYield("user returned")
	if env.stopCount() != 1 {
		t.Fatalf("second yield on an already-stopped session must not stop again (stops=%d)", env.stopCount())
	}
}

// 5. Start failure: the claim reports the error (caller fails the task),
// and the manager does NOT retry on its own — a second claim reports the
// failure again instead of silently looping (no retry storm: the manager
// performs exactly one StartSession call per claim, never a background
// retry loop, so the failure count is bounded by the API's own requeue
// policy, not by the worker).
func TestWslcSessionStartFailureFailsTaskNoRetry(t *testing.T) {
	env := newWslcTestEnv(t)
	env.mu.Lock()
	env.startErr = errors.New("wslc.exe wedged")
	env.mu.Unlock()

	err := env.m.OnTaskClaimed(1, &executor.Handle{Backend: "wslc", ID: "1"})
	if err == nil {
		t.Fatal("OnTaskClaimed must surface the start failure so the caller can fail the task")
	}
	if got := env.startCount(); got != 1 {
		t.Fatalf("StartSession called %d times, want exactly 1 (no self-retry)", got)
	}
	if live, _, _ := env.m.Status(); live {
		t.Fatal("session must not be live after a failed start")
	}
	// The failed task never ran — the caller untracks it.
	env.m.OnTaskFinished(1)

	// Yield after a failed start: no live session to stop.
	env.m.OnYield("user returned")
	if env.stopCount() != 0 {
		t.Fatalf("yield after failed start stopped a session that was never started (stops=%d)", env.stopCount())
	}

	// A second claim (e.g. the API requeued) makes exactly one more start
	// attempt — bounded, not a storm.
	env.mu.Lock()
	env.startErr = nil
	env.mu.Unlock()
	if err := env.m.OnTaskClaimed(2, &executor.Handle{Backend: "wslc", ID: "2"}); err != nil {
		t.Fatalf("retry claim after recovery: %v", err)
	}
	if got := env.startCount(); got != 2 {
		t.Fatalf("StartSession called %d times after recovery, want 2", got)
	}
	if live, _, _ := env.m.Status(); !live {
		t.Fatal("session should be live after a successful start")
	}
}

// Concurrent claims must not double-start the session (the starting flag
// dedupes).
func TestWslcSessionConcurrentClaimsNoDoubleStart(t *testing.T) {
	env := newWslcTestEnv(t)
	slow := &sync.WaitGroup{}
	release := make(chan struct{})
	env.mu.Lock()
	env.m.StartSession = func() error {
		slow.Add(1)
		<-release
		slow.Done()
		env.mu.Lock()
		env.starts++
		env.mu.Unlock()
		return nil
	}
	env.mu.Unlock()

	done := make(chan error, 8)
	for i := 1; i <= 8; i++ {
		i := i
		go func() {
			done <- env.m.OnTaskClaimed(i, &executor.Handle{Backend: "wslc", ID: "x"})
		}()
	}
	// Let the first claim enter StartSession, then let the rest queue.
	time.Sleep(50 * time.Millisecond)
	close(release)
	for i := 0; i < 8; i++ {
		if err := <-done; err != nil {
			t.Fatalf("claim: %v", err)
		}
	}
	slow.Wait()
	if got := env.startCount(); got != 1 {
		t.Fatalf("concurrent claims started the session %d times, want 1", got)
	}
}

// 8. GP.17f: every session-stop path clears the backend's cached registry
// logins (a fresh session starts with no stored registry credentials), and
// neither the yield path nor a no-op yield leaves the cache stale.
func TestWslcSessionStopClearsRegistryLogins(t *testing.T) {
	// Yield stop:
	env := newWslcTestEnv(t)
	if err := env.m.OnTaskClaimed(1, &executor.Handle{Backend: "wslc", ID: "1"}); err != nil {
		t.Fatal(err)
	}
	env.m.OnYield("user returned")
	if got := env.backend.clearLogins; got != 1 {
		t.Fatalf("yield stop cleared registry logins %d times, want 1", got)
	}

	// No-op yield (nothing live) must NOT clear:
	env2 := newWslcTestEnv(t)
	env2.m.OnYield("user returned")
	if got := env2.backend.clearLogins; got != 0 {
		t.Fatalf("no-op yield cleared registry logins %d times, want 0", got)
	}

	// Idle-timeout stop:
	env3 := newWslcTestEnv(t)
	if err := env3.m.OnTaskClaimed(1, &executor.Handle{Backend: "wslc", ID: "1"}); err != nil {
		t.Fatal(err)
	}
	env3.m.OnTaskFinished(1)
	env3.advance(10 * time.Minute)
	env3.m.Tick(env3.now)
	if got := env3.backend.clearLogins; got != 1 {
		t.Fatalf("idle-timeout stop cleared registry logins %d times, want 1", got)
	}
}
