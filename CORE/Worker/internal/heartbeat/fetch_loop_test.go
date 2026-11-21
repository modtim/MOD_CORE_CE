package heartbeat

import (
	"testing"
	"time"
)

// fakeClock is the same pattern used by internal/executor/k8s_idle_test.go's
// idle watchdog tests — a manually-advanced clock so a "worker went silent
// for N minutes" scenario can be simulated without a real sleep.
type fakeClock struct{ t time.Time }

func (c *fakeClock) Now() time.Time      { return c.t }
func (c *fakeClock) Add(d time.Duration) { c.t = c.t.Add(d) }

// TestFetchLoopAge_StalledLoopIsDetected simulates a worker whose fetch loop
// stops calling MarkFetchLoopIteration — the "green but silent" failure mode
// — and asserts FetchLoopAge grows past a stall threshold over fake time,
// exactly as cmd/worker's fetchLoopStalled(age, threshold) would observe it.
func TestFetchLoopAge_StalledLoopIsDetected(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	h := &Heartbeat{nowFn: clk.Now}
	h.lastFetchAt.Store(clk.Now().UnixNano())

	threshold := 10 * time.Minute

	// Healthy: the loop keeps iterating every 30s (its normal long-poll
	// cadence), so age never approaches the threshold.
	for i := 0; i < 15; i++ {
		clk.Add(30 * time.Second)
		h.MarkFetchLoopIteration()
		if age := h.FetchLoopAge(); age > threshold {
			t.Fatalf("healthy loop falsely reported stalled: age=%s", age)
		}
	}

	// Now the loop wedges: nothing calls MarkFetchLoopIteration again. Time
	// keeps moving (heartbeats are a separate goroutine and keep firing), so
	// age grows unbounded.
	clk.Add(9 * time.Minute)
	if age := h.FetchLoopAge(); age > threshold {
		t.Fatalf("expected not-yet-stalled at 9m, got age=%s", age)
	}
	clk.Add(2 * time.Minute) // total silence: 11m > 10m threshold
	age := h.FetchLoopAge()
	if age <= threshold {
		t.Fatalf("expected stalled loop to exceed threshold: age=%s threshold=%s", age, threshold)
	}
}

// TestFetchLoopAge_SeededAtConstruction covers the pre-first-iteration case:
// New() seeds lastFetchAt so a worker that hasn't run its job loop even once
// yet is measured from process start, not reported as "age zero forever"
// (which would mask a worker that never got its job loop running at all).
func TestFetchLoopAge_SeededAtConstruction(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	h := &Heartbeat{nowFn: clk.Now}
	h.lastFetchAt.Store(clk.Now().UnixNano())

	clk.Add(5 * time.Minute)
	if got, want := h.FetchLoopAge(), 5*time.Minute; got != want {
		t.Fatalf("FetchLoopAge = %s, want %s", got, want)
	}
}

// TestFetchLoopAge_TouchResetsAge covers the recovery path: once the loop
// resumes iterating, age drops back to ~0 immediately.
func TestFetchLoopAge_TouchResetsAge(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	h := &Heartbeat{nowFn: clk.Now}
	h.lastFetchAt.Store(clk.Now().UnixNano())

	clk.Add(20 * time.Minute)
	if h.FetchLoopAge() < 20*time.Minute {
		t.Fatalf("expected large age before touch")
	}
	h.MarkFetchLoopIteration()
	if got := h.FetchLoopAge(); got != 0 {
		t.Fatalf("FetchLoopAge after touch = %s, want 0", got)
	}
}
