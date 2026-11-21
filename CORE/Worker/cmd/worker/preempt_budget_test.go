package main

import (
	"testing"
	"time"
)

// waitEvicted polls the fake's recorded evictions (EvictContainer runs in a
// goroutine) until containerID appears or the timeout elapses.
func waitEvicted(t *testing.T, fake *fakeLifecycle, containerID string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		fake.mu.Lock()
		found := false
		for _, id := range fake.evicted {
			if id == containerID {
				found = true
			}
		}
		fake.mu.Unlock()
		if found {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("container %q was never evicted", containerID)
}

// assertNotEvicted confirms no container was evicted (and none is pending in
// the cancelled map) after an enforceCardBudget call.
func assertNotEvicted(t *testing.T, fake *fakeLifecycle, c *preemptController) {
	t.Helper()
	fake.mu.Lock()
	n := len(fake.evicted)
	fake.mu.Unlock()
	if n != 0 {
		t.Fatalf("expected no evictions, got %d", n)
	}
	c.mu.Lock()
	nc := len(c.cancelled)
	c.mu.Unlock()
	if nc != 0 {
		t.Fatalf("expected no pending cancels, got %d", nc)
	}
}

func TestPreemptOverBudgetUnderThresholdDoesNothing(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	c.registerGPU(1, "c-shared-1", "shared", 2048, nil)
	// card used 9000, total 16384, slack 1024 -> threshold 15360, not over.
	id, ok := c.enforceCardBudget(9000, 16384, 1024, func(cid string) int64 {
		if cid == "c-shared-1" {
			return 4096
		}
		return 0
	})
	if id != 0 || ok {
		t.Fatalf("enforceCardBudget = (%d, %v), want (0, false) under threshold", id, ok)
	}
	assertNotEvicted(t, fake, c)
}

func TestPreemptOverBudgetEvictsMostOverSharedTask(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	// Task 1 over by 2048 (6144 used, 4096 reserved); task 2 over by 4096
	// (8192 used, 4096 reserved) -> task 2 must be the one evicted.
	c.registerGPU(1, "c-a", "shared", 4096, nil)
	c.registerGPU(2, "c-b", "shared", 4096, nil)
	used := map[string]int64{"c-a": 6144, "c-b": 8192}
	lookup := func(cid string) int64 { return used[cid] }

	id, ok := c.enforceCardBudget(16000, 16384, 1024, lookup)
	if !ok || id != 2 {
		t.Fatalf("enforceCardBudget = (%d, %v), want (2, true)", id, ok)
	}
	waitEvicted(t, fake, "c-b")
	if len(fake.evicted) != 1 {
		t.Fatalf("expected exactly 1 eviction, got %v", fake.evicted)
	}
}

func TestPreemptOverBudgetNeverEvictsExclusive(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	// Exclusive task is the most over (over by 10000); shared task over by 512.
	c.registerGPU(1, "c-exclusive", "exclusive", 16384, nil)
	c.registerGPU(2, "c-shared", "shared", 4096, nil)
	used := map[string]int64{"c-exclusive": 26384, "c-shared": 4608}
	lookup := func(cid string) int64 { return used[cid] }

	id, ok := c.enforceCardBudget(32000, 32768, 1024, lookup)
	if !ok || id != 2 {
		t.Fatalf("enforceCardBudget = (%d, %v), want (2, true) — exclusive must never be picked", id, ok)
	}
	waitEvicted(t, fake, "c-shared")
}

func TestPreemptOverBudgetNoSharedOverDoesNothing(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	// Card is over budget but the only shared task is under its reservation.
	c.registerGPU(1, "c-shared", "shared", 8192, nil)
	c.registerGPU(2, "c-excl", "exclusive", 0, nil)

	id, ok := c.enforceCardBudget(32000, 32768, 1024, func(cid string) int64 {
		if cid == "c-shared" {
			return 4096
		}
		if cid == "c-excl" {
			return 27000
		}
		return 0
	})
	if id != 0 || ok {
		t.Fatalf("enforceCardBudget = (%d, %v), want (0, false)", id, ok)
	}
	assertNotEvicted(t, fake, c)
}

func TestPreemptOverBudgetIgnoresUnknownMode(t *testing.T) {
	fake := newFakeLifecycle()
	c := newTestController(fake)
	// Task 1 registered via plain register() -> mode "", even though it is
	// the most over; must be ignored.
	c.register(1, "c-unknown", nil)
	// Task 1 is not in the used map as "c-unknown"; give it a huge usage via
	// the lookup by registering it with a reserved budget it cannot have —
	// instead simulate the lookup returning a large value for its container.
	used := map[string]int64{"c-unknown": 999999, "c-shared": 4096}
	c.registerGPU(2, "c-shared", "shared", 4096, nil)

	id, ok := c.enforceCardBudget(32000, 32768, 1024, func(cid string) int64 { return used[cid] })
	// Nothing evictable: unknown mode is not shared, shared task is under budget.
	if id != 0 || ok {
		t.Fatalf("enforceCardBudget = (%d, %v), want (0, false) — unknown-mode task must be ignored", id, ok)
	}
	assertNotEvicted(t, fake, c)
}

func TestPickOverBudgetSkipsNonPositiveReservation(t *testing.T) {
	tasks := map[int]*preemptTask{
		1: {containerID: "c-zero", mode: "shared", reservedMB: 0},
		2: {containerID: "c-neg", mode: "shared", reservedMB: -1},
		3: {containerID: "c-ok", mode: "shared", reservedMB: 1024},
	}
	used := func(cid string) int64 {
		switch cid {
		case "c-zero":
			return 8192
		case "c-neg":
			return 8192
		case "c-ok":
			return 2048
		}
		return 0
	}
	id, over, ok := pickOverBudget(tasks, used)
	if !ok || id != 3 || over != 1024 {
		t.Fatalf("pickOverBudget = (%d, %d, %v), want (3, 1024, true)", id, over, ok)
	}
}
