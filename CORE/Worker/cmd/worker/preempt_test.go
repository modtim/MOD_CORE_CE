package main

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"
)

// fakeLifecycle is a containerLifecycle double that never touches a real
// Docker daemon, so these tests can run in a sandbox with no docker socket.
// It only records what was called — the preemptController's own bookkeeping
// (the preempted map under test) happens synchronously, before the
// EvictContainer call is even dispatched, so no synchronization with the
// async EvictContainer goroutine is needed to assert on it.
type fakeLifecycle struct {
	mu       sync.Mutex
	paused   map[string]bool
	evicted  []string
	pauseErr error
}

func newFakeLifecycle() *fakeLifecycle {
	return &fakeLifecycle{paused: map[string]bool{}}
}

func (f *fakeLifecycle) PauseContainer(ctx context.Context, containerID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.pauseErr != nil {
		return f.pauseErr
	}
	f.paused[containerID] = true
	return nil
}

func (f *fakeLifecycle) UnpauseContainer(ctx context.Context, containerID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.paused, containerID)
	return nil
}

func (f *fakeLifecycle) EvictContainer(containerID, reason string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.evicted = append(f.evicted, containerID)
}

func testLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, &slog.HandlerOptions{Level: slog.LevelError}))
}

// newTestController builds a preemptController with a fake, panic-free
// executor and a grace window long enough that the real timer started by
// yieldAll never fires during a test — grace-expiry is exercised by calling
// evictAfterGrace directly, exactly as the real timer callback would.
func newTestController(fake *fakeLifecycle) *preemptController {
	c := newPreemptController(fake, func() time.Duration { return time.Hour }, testLog())
	return c
}

// yieldAll must mark an evicted task preempted, and wasPreempted must read
// it exactly once (the second read finds it already consumed).
func TestYieldMarksPreemptedOnce(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	c.swapPresent = false // no swap => every task is pinned => evict immediately
	const taskID = 1
	c.tasks[taskID] = &preemptTask{containerID: "container-1"}

	c.yieldAll()

	if !c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = false after yieldAll evicted the task, want true")
	}
	if c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = true on second read, want false (must read-and-clear)")
	}
}

// A task evicted because PauseContainer itself failed is also a NOAH
// preemption, not a hard-cancel, so it must be marked too.
func TestYieldMarksPreemptedOnPauseFailure(t *testing.T) {
	fake := newFakeLifecycle()
	fake.pauseErr = context.DeadlineExceeded
	c := newTestController(fake)
	c.swapPresent = true // swap present => would normally pause, but pause fails
	const taskID = 2
	c.tasks[taskID] = &preemptTask{containerID: "container-2"}

	c.yieldAll()

	if !c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = false after pause-failure eviction, want true")
	}
}

// A task that pauses cleanly is not preempted yet — only eviction (pinned,
// pause failure, or grace expiry) counts. Grace expiry, simulated by calling
// evictAfterGrace directly as the real timer would, must then mark it.
func TestGraceExpiryMarksPreempted(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	c.swapPresent = true // swap present + low VRAM => pauses instead of evicting
	const taskID = 3
	c.tasks[taskID] = &preemptTask{containerID: "container-3"}

	c.yieldAll()
	if c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = true right after a clean pause, want false — nothing evicted yet")
	}

	c.evictAfterGrace(taskID, "container-3")

	if !c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = false after grace expired and evicted the task, want true")
	}
}

// A task resumed within the grace window (human left again) must never be
// marked preempted when its stale grace timer eventually fires.
func TestResumeWithinGraceNeverMarksPreempted(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	c.swapPresent = true
	const taskID = 4
	c.tasks[taskID] = &preemptTask{containerID: "container-4"}

	c.yieldAll()
	c.resumeAll()
	// Simulate the stale timer firing after resumeAll already unpaused it.
	c.evictAfterGrace(taskID, "container-4")

	if c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = true for a task resumed within grace, want false")
	}
}

// The hard-cancel path (evictTasks) is a server-initiated cancel, never a
// NOAH yield, and must not be readable as preempted.
func TestHardCancelEvictDoesNotMarkPreempted(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	const taskID = 5
	c.tasks[taskID] = &preemptTask{containerID: "container-5"}

	c.evictTasks([]int{taskID}, "hard-cancel")

	if c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = true after a hard-cancel evict, want false")
	}
}

// unregister must clear a pending preempted mark, e.g. when executeTask's
// deferred cleanup runs after a caller has already handled preemption some
// other way and the flag is stale.
func TestUnregisterClearsPreempted(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	c.swapPresent = false
	const taskID = 6
	c.tasks[taskID] = &preemptTask{containerID: "container-6"}

	c.yieldAll()
	c.unregister(taskID)

	if c.wasPreempted(taskID) {
		t.Fatal("wasPreempted() = true after unregister, want false — unregister must clear it")
	}
}
