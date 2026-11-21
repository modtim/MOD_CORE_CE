package executor

import (
	"reflect"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestNvidiaDeviceRequestWithoutAssignmentTakesAllGPUs(t *testing.T) {
	req := nvidiaDeviceRequest(nil)
	if req.Count != -1 || len(req.DeviceIDs) != 0 {
		t.Fatalf("want all GPUs, got count=%d ids=%v", req.Count, req.DeviceIDs)
	}
}

func TestNvidiaDeviceRequestPinsAssignedCards(t *testing.T) {
	a := &transport.GPUAssignment{Mode: "exclusive", VRAMLimitMB: 12000, Cards: []transport.AssignedGPU{
		{DeviceUUID: "GPU-aaa", Index: 0, Kind: "cuda"},
		{Index: 3, Kind: "cuda"},
		{DeviceUUID: "rocm-x", Index: 1, Kind: "rocm"},
	}}
	req := nvidiaDeviceRequest(a)
	if req.Count != 0 {
		t.Fatalf("count must be 0 when DeviceIDs are set, got %d", req.Count)
	}
	if want := []string{"GPU-aaa", "3"}; !reflect.DeepEqual(req.DeviceIDs, want) {
		t.Fatalf("ids = %v, want %v", req.DeviceIDs, want)
	}
	env := gpuAssignmentEnv(a, nil)
	if want := []string{
		"MOD_GPU_MODE=exclusive",
		"PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
		"PYTORCH_HIP_ALLOC_CONF=expandable_segments:True",
		"XLA_PYTHON_CLIENT_PREALLOCATE=false",
		"TF_FORCE_GPU_ALLOW_GROWTH=true",
	}; !reflect.DeepEqual(env, want) {
		t.Fatalf("env = %v, want %v", env, want)
	}
}

func TestGPUPinAssignmentEnvFiresForRocmCards(t *testing.T) {
	// §14.3: gpuAssignmentEnv is not cuda-only — a pinned rocm card gets the
	// same MOD_GPU_MODE/alloc-conf env a cuda one does.
	a := &transport.GPUAssignment{Mode: "shared", Cards: []transport.AssignedGPU{{Index: 0, Kind: "rocm", CardVRAMMB: 16384}}}
	env := gpuAssignmentEnv(a, nil)
	found := false
	for _, kv := range env {
		if kv == "MOD_GPU_MODE=shared" {
			found = true
		}
	}
	if !found {
		t.Fatalf("want MOD_GPU_MODE=shared in %v", env)
	}
}

func TestGPUPinAssignmentEnvNilWithNoCards(t *testing.T) {
	a := &transport.GPUAssignment{Mode: "shared"}
	if env := gpuAssignmentEnv(a, nil); env != nil {
		t.Fatalf("want no env, got %v", env)
	}
}

func TestGPUPinAssignedIndicesForKindFiltersByKindAndSorts(t *testing.T) {
	a := &transport.GPUAssignment{Cards: []transport.AssignedGPU{
		{Index: 2, Kind: "rocm"}, {Index: 0, Kind: "cuda"}, {Index: 1, Kind: "rocm"},
	}}
	if got := assignedIndicesForKind(a, "rocm"); !reflect.DeepEqual(got, []int{1, 2}) {
		t.Fatalf("rocm indices = %v, want [1 2]", got)
	}
	if got := assignedIndicesForKind(a, "intel"); got != nil {
		t.Fatalf("intel indices = %v, want nil", got)
	}
	if got := assignedIndicesForKind(nil, "rocm"); got != nil {
		t.Fatalf("nil assignment indices = %v, want nil", got)
	}
}

func TestGPUPinAmdIntelVisibleDevicesEnv(t *testing.T) {
	if got := amdIntelVisibleDevicesEnv("rocm", 2, nil); !reflect.DeepEqual(got,
		[]string{"HIP_VISIBLE_DEVICES=0,1", "ROCR_VISIBLE_DEVICES=0,1"}) {
		t.Fatalf("rocm env = %v", got)
	}
	if got := amdIntelVisibleDevicesEnv("intel", 1, nil); !reflect.DeepEqual(got,
		[]string{"ZE_AFFINITY_MASK=0"}) {
		t.Fatalf("intel env = %v", got)
	}
	if got := amdIntelVisibleDevicesEnv("cuda", 1, nil); got != nil {
		t.Fatalf("cuda env = %v, want nil (nvidia uses DeviceRequests, not this)", got)
	}
	if got := amdIntelVisibleDevicesEnv("rocm", 0, nil); got != nil {
		t.Fatalf("zero count env = %v, want nil", got)
	}
	nodeEnv := map[string]string{"HIP_VISIBLE_DEVICES": "3"}
	got := amdIntelVisibleDevicesEnv("rocm", 1, nodeEnv)
	if !reflect.DeepEqual(got, []string{"ROCR_VISIBLE_DEVICES=0"}) {
		t.Fatalf("node-set HIP_VISIBLE_DEVICES must not be re-emitted, got %v", got)
	}
}

func TestGPUSoftCapShared(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "shared", VRAMLimitMB: 4096,
		Cards: []transport.AssignedGPU{{Index: 0, Kind: "cuda", CardVRAMMB: 16384}},
	}
	env := gpuAssignmentEnv(a, nil)
	want := []string{
		"MOD_GPU_MODE=shared",
		"PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
		"PYTORCH_HIP_ALLOC_CONF=expandable_segments:True",
		"XLA_PYTHON_CLIENT_PREALLOCATE=false",
		"TF_FORCE_GPU_ALLOW_GROWTH=true",
		"MOD_GPU_VRAM_LIMIT_MB=4096",
		"MOD_GPU_CARD_VRAM_MB=16384",
		"XLA_PYTHON_CLIENT_MEM_FRACTION=0.25",
	}
	if !reflect.DeepEqual(env, want) {
		t.Fatalf("env = %v, want %v", env, want)
	}
}

func TestGPUSoftCapExclusiveNoLimits(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "exclusive", VRAMLimitMB: 4096,
		Cards: []transport.AssignedGPU{{Index: 0, Kind: "cuda", CardVRAMMB: 16384}},
	}
	env := gpuAssignmentEnv(a, nil)
	for _, kv := range env {
		if kv == "MOD_GPU_VRAM_LIMIT_MB=4096" || kv == "MOD_GPU_CARD_VRAM_MB=16384" || kv == "XLA_PYTHON_CLIENT_MEM_FRACTION=0.25" {
			t.Fatalf("exclusive assignment must not carry soft caps, got %v in %v", kv, env)
		}
	}
	found := false
	for _, kv := range env {
		if kv == "MOD_GPU_MODE=exclusive" {
			found = true
		}
	}
	if !found {
		t.Fatalf("want MOD_GPU_MODE=exclusive in %v", env)
	}
}

func TestGPUSoftCapNodeEnvWins(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "shared", VRAMLimitMB: 4096,
		Cards: []transport.AssignedGPU{{Index: 0, Kind: "cuda", CardVRAMMB: 16384}},
	}
	nodeEnv := map[string]string{"PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128"}
	env := gpuAssignmentEnv(a, nodeEnv)
	for _, kv := range env {
		if len(kv) >= len("PYTORCH_CUDA_ALLOC_CONF=") && kv[:len("PYTORCH_CUDA_ALLOC_CONF=")] == "PYTORCH_CUDA_ALLOC_CONF=" {
			t.Fatalf("node-set PYTORCH_CUDA_ALLOC_CONF must not be re-emitted, got %v", env)
		}
	}
}

func TestGPUSoftCapNil(t *testing.T) {
	if env := gpuAssignmentEnv(nil, nil); env != nil {
		t.Fatalf("want nil env, got %v", env)
	}
}

func TestGPUSoftCapUnknownCardSize(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "shared", VRAMLimitMB: 4096,
		Cards: []transport.AssignedGPU{{Index: 0, Kind: "cuda"}},
	}
	env := gpuAssignmentEnv(a, nil)
	for _, kv := range env {
		if kv == "MOD_GPU_CARD_VRAM_MB" || kv == "XLA_PYTHON_CLIENT_MEM_FRACTION" {
			t.Fatalf("unknown card size must not emit %q, got %v", kv, env)
		}
	}
	found := false
	for _, kv := range env {
		if kv == "MOD_GPU_VRAM_LIMIT_MB=4096" {
			found = true
		}
	}
	if !found {
		t.Fatalf("want MOD_GPU_VRAM_LIMIT_MB=4096 in %v", env)
	}
}
