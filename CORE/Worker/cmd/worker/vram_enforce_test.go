package main

import (
	"context"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// TestCardUsageGroupsBySingleCard covers the common case: two single-card
// tasks on two different cards each contribute only their own usage, and a
// card with no CardVRAMMB reported by any of its tasks comes back total=0.
func TestCardUsageGroupsBySingleCard(t *testing.T) {
	tasks := map[int]gpuCardTask{
		1: {containerID: "c1", cards: []transport.AssignedGPU{{CardID: "gpu-0", CardVRAMMB: 16384}}},
		2: {containerID: "c2", cards: []transport.AssignedGPU{{CardID: "gpu-1", CardVRAMMB: 24576}}},
		3: {containerID: "c3", cards: []transport.AssignedGPU{{CardID: "gpu-0"}}}, // no CardVRAMMB known
	}
	samples := map[string]int64{"c1": 5000, "c2": 9000, "c3": 1000}

	got := cardUsage(tasks, samples)

	if u := got["gpu-0"]; u.used != 6000 || u.total != 16384 {
		t.Errorf("gpu-0 = %+v, want used=6000 total=16384", u)
	}
	if u := got["gpu-1"]; u.used != 9000 || u.total != 24576 {
		t.Errorf("gpu-1 = %+v, want used=9000 total=24576", u)
	}
}

// TestCardUsageMultiCardAttributesToFirst documents the multi-card
// approximation: a task assigned N cards has ALL of its sampled usage
// attributed to cards[0], never split across the rest.
func TestCardUsageMultiCardAttributesToFirst(t *testing.T) {
	tasks := map[int]gpuCardTask{
		1: {containerID: "c1", cards: []transport.AssignedGPU{
			{CardID: "gpu-0", CardVRAMMB: 16384},
			{CardID: "gpu-1", CardVRAMMB: 16384},
		}},
	}
	samples := map[string]int64{"c1": 12000}

	got := cardUsage(tasks, samples)

	if u := got["gpu-0"]; u.used != 12000 {
		t.Errorf("gpu-0.used = %d, want 12000 (all usage attributed to first card)", u.used)
	}
	if _, ok := got["gpu-1"]; ok {
		t.Errorf("gpu-1 should carry no usage for a task attributed to its first card, got %+v", got["gpu-1"])
	}
}

// TestCardUsageSkipsUnsampledAndUnknownCard covers: a task with no card
// assignment, and a task whose container the sampler didn't report this
// tick (e.g. it exited between register and the tick), contribute nothing.
func TestCardUsageSkipsUnsampledAndUnknownCard(t *testing.T) {
	tasks := map[int]gpuCardTask{
		1: {containerID: "c1"},                                                  // no cards
		2: {containerID: "c2", cards: []transport.AssignedGPU{{CardID: "gpu-0"}}}, // not in samples
	}
	got := cardUsage(tasks, map[string]int64{})
	if len(got) != 0 {
		t.Errorf("expected no card usage, got %+v", got)
	}
}

// fakeVRAMSampler is a vramSampleObserver double for the tick integration
// test: SampleOnce returns a fixed snapshot, Observe/Peak/Forget are
// recorded but not exercised for correctness here (covered directly against
// the real *executor.VRAMSampler in internal/executor).
type fakeVRAMSampler struct {
	samples   map[string]int64
	forgotten []string
}

func (f *fakeVRAMSampler) SampleOnce(ctx context.Context) map[string]int64 { return f.samples }
func (f *fakeVRAMSampler) Observe(samples map[string]int64)                {}
func (f *fakeVRAMSampler) Peak(containerID string) int64                  { return f.samples[containerID] }
func (f *fakeVRAMSampler) Forget(containerID string)                      { f.forgotten = append(f.forgotten, containerID) }

// TestVramEnforcerTickEvictsOverBudgetTask is the integration-ish check: a
// fake sampler reports a shared task's container using more VRAM than its
// reservation, and over the card's total-minus-slack — tick() must evict it
// through the real preemptController, exactly as the 5s production loop would.
func TestVramEnforcerTickEvictsOverBudgetTask(t *testing.T) {
	fake := newFakeLifecycle()
	preempt := newTestController(fake)
	preempt.registerGPU(1, "c-over", "shared", 4096, nil)

	sampler := &fakeVRAMSampler{samples: map[string]int64{"c-over": 9000}}
	enf := newVramEnforcer(sampler, preempt, 1024)
	enf.register(1, "c-over", []transport.AssignedGPU{{CardID: "gpu-0", CardVRAMMB: 8192}})

	// card used 9000 > total 8192 - slack 1024 (=7168) -> over budget.
	enf.tick(context.Background())

	waitEvicted(t, fake, "c-over")
}

// TestVramEnforcerTickUnderBudgetDoesNothing is the negative case: usage
// under the card's total-minus-slack must not evict anything.
func TestVramEnforcerTickUnderBudgetDoesNothing(t *testing.T) {
	fake := newFakeLifecycle()
	preempt := newTestController(fake)
	preempt.registerGPU(1, "c-ok", "shared", 4096, nil)

	sampler := &fakeVRAMSampler{samples: map[string]int64{"c-ok": 3000}}
	enf := newVramEnforcer(sampler, preempt, 1024)
	enf.register(1, "c-ok", []transport.AssignedGPU{{CardID: "gpu-0", CardVRAMMB: 8192}})

	enf.tick(context.Background())

	assertNotEvicted(t, fake, preempt)
}

// TestVramEnforcerRegisterUnregisterForgetsSampler covers that unregister
// forgets the sampler's peak for the departed task's container.
func TestVramEnforcerRegisterUnregisterForgetsSampler(t *testing.T) {
	fake := newFakeLifecycle()
	preempt := newTestController(fake)
	sampler := &fakeVRAMSampler{samples: map[string]int64{"c1": 1234}}
	enf := newVramEnforcer(sampler, preempt, 512)

	enf.register(1, "c1", []transport.AssignedGPU{{CardID: "gpu-0", CardVRAMMB: 8192}})
	if !enf.hasTasks() {
		t.Fatal("expected hasTasks true after register")
	}
	enf.unregister(1)
	if enf.hasTasks() {
		t.Fatal("expected hasTasks false after unregister")
	}
	if len(sampler.forgotten) != 1 || sampler.forgotten[0] != "c1" {
		t.Errorf("expected sampler.Forget(\"c1\") called once, got %v", sampler.forgotten)
	}
}
