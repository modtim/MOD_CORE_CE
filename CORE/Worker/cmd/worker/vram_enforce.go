package main

import (
	"context"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// defaultVRAMSlackMB is the fallback headroom (MiB) enforceCardBudget leaves
// below a card's total before evicting the shared task most over its
// reservation. Overridden by MOD_WORKER_VRAM_SLACK_MB.
const defaultVRAMSlackMB = 512

// vramSlackMB reads MOD_WORKER_VRAM_SLACK_MB, falling back to
// defaultVRAMSlackMB on unset/invalid/negative input.
func vramSlackMB() int64 {
	v := os.Getenv("MOD_WORKER_VRAM_SLACK_MB")
	if v == "" {
		return defaultVRAMSlackMB
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n < 0 {
		return defaultVRAMSlackMB
	}
	return n
}

// gpuCardTask is the card-assignment side of a running GPU task: which
// container it is, and which cards the API pinned it to. Tracked separately
// from preemptTask (which knows mode/reservedMB but not cards) so the VRAM
// sampler can attribute a container's sampled usage to a physical card.
type gpuCardTask struct {
	containerID string
	cards       []transport.AssignedGPU
}

// cardTotals is one card's usage snapshot: used is the summed sampled MiB of
// every task attributed to it this tick, total is that card's known VRAM
// capacity (0 when no task assigned to it reports a CardVRAMMB).
type cardTotals struct {
	used  int64
	total int64
}

// cardUsage groups per-container VRAM samples by physical card. A task
// assigned to N cards has its usage attributed entirely to its FIRST card
// (cards[0]) — a documented approximation: the sampler can only see total
// container VRAM, not its per-card split, so a multi-card task can make that
// first card look more contended than it is and the rest look less. Good
// enough for single-card tasks (the overwhelming majority) and for picking
// which task to evict, since eviction removes the whole task from every card
// it holds regardless of which one tripped the budget.
func cardUsage(tasks map[int]gpuCardTask, samples map[string]int64) map[string]cardTotals {
	out := make(map[string]cardTotals)
	for _, t := range tasks {
		if len(t.cards) == 0 || t.cards[0].CardID == "" {
			continue
		}
		used, ok := samples[t.containerID]
		if !ok {
			continue
		}
		card := t.cards[0]
		entry := out[card.CardID]
		entry.used += used
		if card.CardVRAMMB > 0 {
			entry.total = int64(card.CardVRAMMB)
		}
		out[card.CardID] = entry
	}
	return out
}

// vramSampleObserver is the subset of *executor.VRAMSampler vramEnforcer
// needs. Declaring it lets tests substitute a fake sampler directly (the
// real type's run/readCgroup injection points are private to the executor
// package) instead of shelling out to nvidia-smi/rocm-smi.
type vramSampleObserver interface {
	SampleOnce(ctx context.Context) map[string]int64
	Observe(samples map[string]int64)
	Peak(containerID string) int64
	Forget(containerID string)
}

// vramEnforcer ticks the VRAM sampler and, per card, evicts the shared task
// most over its reservation when a card's usage crosses its budget. One
// instance runs for the life of the worker process (main.go), fed by every
// GPU task's OnContainerCreated hook via register/unregister.
type vramEnforcer struct {
	mu      sync.Mutex
	tasks   map[int]gpuCardTask
	sampler vramSampleObserver
	preempt *preemptController
	slackMB int64
}

func newVramEnforcer(sampler vramSampleObserver, preempt *preemptController, slackMB int64) *vramEnforcer {
	return &vramEnforcer{
		tasks:   map[int]gpuCardTask{},
		sampler: sampler,
		preempt: preempt,
		slackMB: slackMB,
	}
}

// register records a GPU task's card assignment so the next tick can
// attribute its sampled usage. Called from OnContainerCreated, once the
// container (and therefore the sampler's cgroup-derived container ID) exists.
func (v *vramEnforcer) register(taskID int, containerID string, cards []transport.AssignedGPU) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.tasks[taskID] = gpuCardTask{containerID: containerID, cards: cards}
}

// unregister drops the task's card tracking and its sampler peak, so a
// future task never inherits a stale peak for a reused containerID.
func (v *vramEnforcer) unregister(taskID int) {
	v.mu.Lock()
	t, ok := v.tasks[taskID]
	delete(v.tasks, taskID)
	v.mu.Unlock()
	if ok {
		v.sampler.Forget(t.containerID)
	}
}

// peak returns the sampler's highest observed VRAM MiB for containerID (0 if
// never sampled). Used at task completion to fill TaskResult.PeakVRAMMB.
func (v *vramEnforcer) peak(containerID string) int64 {
	return v.sampler.Peak(containerID)
}

// hasTasks reports whether any GPU task is currently tracked — the sampler
// loop's active() gate, so a worker running no GPU tasks doesn't shell out to
// nvidia-smi/rocm-smi every interval for nothing.
func (v *vramEnforcer) hasTasks() bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	return len(v.tasks) > 0
}

// tick takes one sample, records it into the sampler's peak tracking, then
// enforces every known card's budget. A method (rather than inlined into
// run) so tests can call it directly with an injected sampler/preempt
// controller instead of waiting on a real ticker.
func (v *vramEnforcer) tick(ctx context.Context) {
	samples := v.sampler.SampleOnce(ctx)
	v.sampler.Observe(samples)

	v.mu.Lock()
	tasks := make(map[int]gpuCardTask, len(v.tasks))
	for id, t := range v.tasks {
		tasks[id] = t
	}
	v.mu.Unlock()

	usage := cardUsage(tasks, samples)
	used := func(containerID string) int64 { return samples[containerID] }
	for _, u := range usage {
		if u.total <= 0 {
			continue // unknown card capacity: nothing to enforce against
		}
		v.preempt.enforceCardBudget(u.used, u.total, v.slackMB, used)
	}
}

// run ticks every interval (default 5s when interval<=0) until ctx is done,
// skipping a sample while no GPU task is tracked.
func (v *vramEnforcer) run(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = 5 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if v.hasTasks() {
				v.tick(ctx)
			}
		}
	}
}
