package main

import (
	"testing"
)

// BUG P2b (2026-10-09): build task 50645 (node build-cpu, queue CPU,
// node_kind system_build_one_variant) was claimed by the HP worker and never
// POSTed /started for 4+ minutes. The scheduler's phase plan carried
// vram_mb=39542 for container_start + processing — a poisoned
// resource_patterns[system_build_one_variant/CPU/any].avg_peak_vram_mb (worker
// TaskResourceSample rows report the BOX's GPU, and _get_pattern()'s queue-wide
// fallback can hand a GPU node's profile to a CPU task). validate-phase rejected
// it ("VRAM: need 39542MB contiguous, largest block is 0MB"), reverted the claim
// to PENDING, and executeTask RETURNED before ReportStarted — no report at all,
// so the claim→validate→revert loop burned scheduling_version 1172→1446 while
// started_at stayed NULL.
//
// These tests pin the worker-side guard: on a known non-GPU queue, a rejection
// whose only complaints are VRAM / GPU power-state is phantom, so the worker
// proceeds instead of stalling silently. Anything else (RAM, disk, unknown
// queue, GPU queue) keeps the old reschedule-and-return behaviour.

func TestPhantomVRAMRejectionIsScopedToNonGPQueues(t *testing.T) {
    // The live case: CPU queue, VRAM-only complaint → phantom → proceed.
    if !phantomVRAMRejection("CPU", "VRAM: need 39542MB contiguous, largest block is 0MB") {
        t.Errorf("live 50645 case must be recognised as phantom")
    }
    // GPU power-state complaint alone is the same artifact (the API emits it
    // only when the plan demanded VRAM).
    if !phantomVRAMRejection("cpu", "GPU in low_power state, need active for processing") {
        t.Errorf("low_power-only on cpu queue must be phantom")
    }
    // Both blocking complaints together.
    if !phantomVRAMRejection("CPU", "VRAM: need 39542MB contiguous, largest block is 8267MB; GPU in low_power state, need active for processing") {
        t.Errorf("combined VRAM + low_power on cpu queue must be phantom")
    }
}

func TestRealRejectionsStillReschedule(t *testing.T) {
    // A RAM complaint is real: the box genuinely cannot fit the phase.
    if phantomVRAMRejection("CPU", "RAM: need 12000MB available, have 400MB") {
        t.Errorf("RAM complaint must not be treated as phantom")
    }
    // Mixed: real + phantom → honour the real one.
    if phantomVRAMRejection("CPU", "RAM: need 12000MB available, have 400MB; VRAM: need 39542MB contiguous, largest block is 0MB") {
        t.Errorf("mixed RAM+VRAM must not be treated as phantom")
    }
    // A GPU queue legitimately needs VRAM — never exempt it.
    if phantomVRAMRejection("cuda", "VRAM: need 39542MB contiguous, largest block is 0MB") {
        t.Errorf("VRAM complaint on a GPU queue must not be phantom")
    }
    // Unknown/empty queue: never widen the exemption on a guess.
    if phantomVRAMRejection("", "VRAM: need 39542MB contiguous, largest block is 0MB") {
        t.Errorf("empty queue name must not be treated as non-GPU")
    }
    // Empty reason (API returned proceed=false with no detail): nothing proves
    // the phantom pattern, so keep the old reschedule path.
    if phantomVRAMRejection("CPU", "") {
        t.Errorf("empty reason must not be phantom")
    }
}

func TestGPUQueueNamesMatchScheduler(t *testing.T) {
    // Must mirror scheduler/overlap_scheduler._gpu_queue_names() and
    // routers/worker_tasks validate_phase guard 1 exactly.
    for _, q := range []string{"cuda", "rocm", "intel", "npu", "accelerator", "GPU"} {
        if !isGPUQueue(q) {
            t.Errorf("queue %s must be GPU-capable", q)
        }
    }
    for _, q := range []string{"CPU", " cpu ", "system", "network", "storage_migration", ""} {
        if isGPUQueue(q) {
            t.Errorf("queue %s must not be GPU-capable", q)
        }
    }
}
