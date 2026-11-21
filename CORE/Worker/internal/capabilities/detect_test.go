package capabilities

import (
	"reflect"
	"testing"
)

func TestDeriveAutoTags(t *testing.T) {
	// deriveAutoTags reads p.detectedAccelerators (populated by detectGPU)
	// and always appends "cpu". GPUType is the derived summary field, not
	// the source of truth for tags.
	tests := []struct {
		name     string
		detected []string
		want     []string
	}{
		{"cuda-only", []string{"cuda"}, []string{"cuda", "cpu"}},
		{"rocm-only", []string{"rocm"}, []string{"rocm", "cpu"}},
		{"intel-only", []string{"intel"}, []string{"intel", "cpu"}},
		{"multi-gpu", []string{"rocm", "intel"}, []string{"rocm", "intel", "cpu"}},
		{"nothing-detected", nil, []string{"cpu"}},
		{"empty-slice", []string{}, []string{"cpu"}},
		{"cpu-already-present", []string{"cpu", "cuda"}, []string{"cpu", "cuda"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := &Profile{detectedAccelerators: tt.detected}
			got := deriveAutoTags(p)
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("detected=%v: got %v, want %v", tt.detected, got, tt.want)
			}
		})
	}
}

func TestMergeHardwareTags_AutoOnly(t *testing.T) {
	got := mergeHardwareTags([]string{"cuda"}, "")
	want := []string{"cuda"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("auto-only: got %v, want %v", got, want)
	}
}

func TestMergeHardwareTags_WithEnvVar(t *testing.T) {
	got := mergeHardwareTags([]string{"cuda"}, "tensorrt,triton")
	want := []string{"cuda", "tensorrt", "triton"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("with env: got %v, want %v", got, want)
	}
}

func TestMergeHardwareTags_DeduplicatesAndNormalizes(t *testing.T) {
	got := mergeHardwareTags([]string{"cuda"}, " CUDA , TensorRT , cuda ")
	want := []string{"cuda", "tensorrt"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("dedup: got %v, want %v", got, want)
	}
}

func TestMergeHardwareTags_EmptyAutoAndEnv(t *testing.T) {
	got := mergeHardwareTags([]string{}, "")
	if len(got) != 0 {
		t.Errorf("empty: got %v, want empty", got)
	}
}

func TestSupportedQueues_FromTags(t *testing.T) {
	p := &Profile{HardwareTags: []string{"cuda", "tensorrt"}}
	queues := p.SupportedQueues()

	has := func(q string) bool {
		for _, s := range queues {
			if s == q {
				return true
			}
		}
		return false
	}

	if !has("CPU") || !has("default") {
		t.Error("should always include CPU and default")
	}
	if !has("CUDA") {
		t.Error("should include CUDA queue from cuda tag")
	}
	if !has("TENSORRT") {
		t.Error("should include TENSORRT queue from tensorrt tag")
	}
}

func TestSupportedQueues_CPUOnly(t *testing.T) {
	p := &Profile{HardwareTags: []string{"cpu"}}
	queues := p.SupportedQueues()

	if len(queues) != 2 {
		t.Errorf("cpu-only should have 2 queues (CPU, default), got %v", queues)
	}
}

// MAC.1.s8a — the Metal lane. A linux CUDA profile must be byte-identical to
// what it was before this change: the Metal opt-out is gated on darwin, so
// nothing a Linux worker advertises moves.
func TestSupportedQueues_LinuxCUDAUnchanged(t *testing.T) {
	p := &Profile{OS: "linux", HardwareTags: []string{"cuda", "cpu"}}
	got := p.SupportedQueues()
	want := []string{"CPU", "default", "CUDA", "ACCELERATOR"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("linux cuda queues changed: got %v, want %v", got, want)
	}
}

func TestSupportedQueues_LinuxMultiVariantUnchanged(t *testing.T) {
	p := &Profile{OS: "linux", HardwareTags: []string{"rocm", "intel", "npu", "cpu"}}
	got := p.SupportedQueues()
	want := []string{"CPU", "default", "ROCM", "INTEL", "NPU", "ACCELERATOR"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("linux multi-variant queues changed: got %v, want %v", got, want)
	}
}

// A Linux host that somehow carries a `metal` tag (a WSL box with a Metal-ish
// tag hand-set in WORKER_HARDWARE_TAGS) keeps its historical queue list — the
// opt-out must never reach it.
func TestSupportedQueues_LinuxStrayMetalTagUnchanged(t *testing.T) {
	p := &Profile{OS: "linux", HardwareTags: []string{"metal", "cpu"}}
	got := p.SupportedQueues()
	want := []string{"CPU", "default", "METAL", "ACCELERATOR"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("linux stray metal tag changed queues: got %v, want %v", got, want)
	}
}

// MAC.1.s8a — a darwin worker with the metal tag advertises the API's
// METAL_QUEUE spelling ("Metal", queue_selector.py) and no longer
// auto-subscribes to the CPU/default container pools.
func TestSupportedQueues_DarwinMetal(t *testing.T) {
	p := &Profile{OS: "darwin", HardwareTags: []string{"metal", "cpu"}}
	queues := p.SupportedQueues()

	if !reflect.DeepEqual(queues, []string{"Metal", "ACCELERATOR"}) {
		t.Errorf("darwin+metal should poll exactly [Metal ACCELERATOR], got %v", queues)
	}
}

// Darwin without a metal tag keeps CPU + default (documented decision: it has
// no Metal queue to poll and must not be stranded idle).
func TestSupportedQueues_DarwinNoMetalTag(t *testing.T) {
	p := &Profile{OS: "darwin", HardwareTags: []string{"cpu"}}
	queues := p.SupportedQueues()

	if !reflect.DeepEqual(queues, []string{"CPU", "default"}) {
		t.Errorf("darwin without metal keeps CPU+default, got %v", queues)
	}
}

// The metal tag is matched case-insensitively (WORKER_HARDWARE_TAGS is
// free-form) and never duplicated.
func TestSupportedQueues_DarwinMetalCaseAndDedup(t *testing.T) {
	p := &Profile{OS: "darwin", HardwareTags: []string{"METAL", "metal", "cpu"}}
	queues := p.SupportedQueues()

	if !reflect.DeepEqual(queues, []string{"Metal", "ACCELERATOR"}) {
		t.Errorf("darwin metal spelling/dedup: got %v", queues)
	}
}
