package executor

import (
	"strconv"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func fakeDRIResolver(kind string, index int, preferBDF string) (string, string) {
	return "/dev/dri/card" + strconv.Itoa(index), "/dev/dri/renderD" + strconv.Itoa(128+index)
}

func TestStackGPUPinNvidiaDeviceIDs(t *testing.T) {
	spec := `services:
  sdxl:
    image: mod/sdxl:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
`
	a := &transport.GPUAssignment{Mode: "exclusive", Cards: []transport.AssignedGPU{
		{DeviceUUID: "GPU-1", Index: 0, Kind: "cuda"},
	}}
	out, changed := pinStackGPUs(spec, a, fakeDRIResolver)
	if len(changed) != 1 || changed[0] != "sdxl" {
		t.Fatalf("changed = %v, want [sdxl]", changed)
	}
	if strings.Contains(out, "count: all") {
		t.Fatalf("count: all must be removed, got:\n%s", out)
	}
	if !strings.Contains(out, "device_ids:") || !strings.Contains(out, "GPU-1") {
		t.Fatalf("want device_ids: [GPU-1], got:\n%s", out)
	}
}

func TestStackGPUPinAmdNarrowsDRIAndSetsEnv(t *testing.T) {
	spec := `services:
  whisper:
    image: mod/whisper-rocm:latest
    devices:
      - /dev/kfd
      - /dev/dri
`
	a := &transport.GPUAssignment{Mode: "shared", Cards: []transport.AssignedGPU{
		{Index: 1, Kind: "rocm"},
	}}
	out, changed := pinStackGPUs(spec, a, fakeDRIResolver)
	if len(changed) != 1 || changed[0] != "whisper" {
		t.Fatalf("changed = %v, want [whisper]", changed)
	}
	if !strings.Contains(out, "/dev/kfd") {
		t.Fatalf("/dev/kfd must be kept, got:\n%s", out)
	}
	if !strings.Contains(out, "/dev/dri/card1") || !strings.Contains(out, "/dev/dri/renderD129") {
		t.Fatalf("want narrowed card1/renderD129, got:\n%s", out)
	}
	if strings.Contains(out, "- /dev/dri\n") {
		t.Fatalf("bare /dev/dri must be dropped, got:\n%s", out)
	}
	if !strings.Contains(out, "HIP_VISIBLE_DEVICES=0") || !strings.Contains(out, "ROCR_VISIBLE_DEVICES=0") {
		t.Fatalf("want HIP/ROCR visible-devices env, got:\n%s", out)
	}
}

func TestStackGPUPinNoAssignmentUnchanged(t *testing.T) {
	spec := `services:
  sdxl:
    image: mod/sdxl:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
`
	out, changed := pinStackGPUs(spec, nil, fakeDRIResolver)
	if changed != nil {
		t.Fatalf("changed = %v, want nil", changed)
	}
	if out != spec {
		t.Fatalf("spec must be unchanged with no assignment")
	}
}

// §14.7b — per-service card pinning via GPUAssignment.ServiceCards.

func TestStackGPUPinPerServiceMixedKinds(t *testing.T) {
	spec := `services:
  llm:
    image: vllm/vllm-openai:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
  stt:
    image: mod/whisper-rocm:latest
    devices:
      - /dev/kfd
      - /dev/dri
`
	a := &transport.GPUAssignment{
		Mode: "shared",
		Cards: []transport.AssignedGPU{
			{CardID: "c0", DeviceUUID: "GPU-CUDA-0", Index: 0, Kind: "cuda"},
			{CardID: "r0", Index: 1, Kind: "rocm"},
		},
		ServiceCards: map[string][]string{
			"llm": {"c0"},
			"stt": {"r0"},
		},
	}
	out, changed := pinStackGPUs(spec, a, fakeDRIResolver)
	if len(changed) != 2 || changed[0] != "llm" || changed[1] != "stt" {
		t.Fatalf("changed = %v, want [llm stt]", changed)
	}
	if !strings.Contains(out, "GPU-CUDA-0") {
		t.Fatalf("llm must be pinned to its cuda card, got:\n%s", out)
	}
	if !strings.Contains(out, "/dev/dri/card1") {
		t.Fatalf("stt must be pinned to its rocm card, got:\n%s", out)
	}
}

func TestStackGPUPinPerServiceSharedCard(t *testing.T) {
	spec := `services:
  gen:
    image: vllm/vllm-openai:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
  reasoner:
    image: vllm/vllm-openai:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
`
	a := &transport.GPUAssignment{
		Mode: "shared",
		Cards: []transport.AssignedGPU{
			{CardID: "c0", DeviceUUID: "GPU-SHARED-0", Index: 0, Kind: "cuda"},
		},
		ServiceCards: map[string][]string{
			"gen":      {"c0"},
			"reasoner": {"c0"},
		},
	}
	out, changed := pinStackGPUs(spec, a, fakeDRIResolver)
	if len(changed) != 2 {
		t.Fatalf("changed = %v, want both services pinned", changed)
	}
	if strings.Count(out, "GPU-SHARED-0") != 2 {
		t.Fatalf("both services must be pinned to the same shared card, got:\n%s", out)
	}
}

func TestStackGPUPinServiceMissingFromSplitUsesTaskWide(t *testing.T) {
	// An older API (or a service the API's split never named) falls back to
	// the whole task-wide Cards, exactly as if ServiceCards were unset.
	spec := `services:
  sdxl:
    image: mod/sdxl:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
`
	a := &transport.GPUAssignment{
		Mode:  "exclusive",
		Cards: []transport.AssignedGPU{{CardID: "c0", DeviceUUID: "GPU-1", Index: 0, Kind: "cuda"}},
		ServiceCards: map[string][]string{
			"other-service": {"c0"},
		},
	}
	out, changed := pinStackGPUs(spec, a, fakeDRIResolver)
	if len(changed) != 1 || changed[0] != "sdxl" {
		t.Fatalf("changed = %v, want [sdxl]", changed)
	}
	if !strings.Contains(out, "GPU-1") {
		t.Fatalf("sdxl must still be pinned to the task-wide card, got:\n%s", out)
	}
}

func TestStackVLLMMemUtilFlagAdded(t *testing.T) {
	spec := `services:
  llm:
    image: vllm/vllm-openai:latest
    command: ["vllm", "serve", "meta-llama/Llama-3-8b"]
`
	a := &transport.GPUAssignment{Mode: "shared", VRAMLimitMB: 8192, Cards: []transport.AssignedGPU{
		{CardVRAMMB: 16384},
	}}
	out, changed, llama := pinVLLMMemUtil(spec, a)
	if len(changed) != 1 || changed[0] != "llm" {
		t.Fatalf("changed = %v, want [llm]", changed)
	}
	if llama != nil {
		t.Fatalf("llama = %v, want nil", llama)
	}
	if !strings.Contains(out, "--gpu-memory-utilization=0.50") {
		t.Fatalf("want --gpu-memory-utilization=0.50, got:\n%s", out)
	}
}

func TestStackVLLMMemUtilFlagAlreadyPresentUntouched(t *testing.T) {
	spec := `services:
  llm:
    image: vllm/vllm-openai:latest
    command: ["vllm", "serve", "--gpu-memory-utilization", "0.8", "meta-llama/Llama-3-8b"]
`
	a := &transport.GPUAssignment{Mode: "shared", VRAMLimitMB: 8192, Cards: []transport.AssignedGPU{
		{CardVRAMMB: 16384},
	}}
	out, changed, _ := pinVLLMMemUtil(spec, a)
	if changed != nil {
		t.Fatalf("changed = %v, want nil (flag already present)", changed)
	}
	if out != spec {
		t.Fatalf("spec must be unchanged when the flag is already set")
	}
}

func TestStackVLLMSkipsLlamaCpp(t *testing.T) {
	spec := `services:
  llm:
    image: ghcr.io/ggml-org/llama.cpp:server
    command: ["llama-server", "-m", "model.gguf"]
`
	a := &transport.GPUAssignment{Mode: "shared", VRAMLimitMB: 8192, Cards: []transport.AssignedGPU{
		{CardVRAMMB: 16384},
	}}
	out, changed, llama := pinVLLMMemUtil(spec, a)
	if changed != nil {
		t.Fatalf("changed = %v, want nil", changed)
	}
	if len(llama) != 1 || llama[0] != "llm" {
		t.Fatalf("llama = %v, want [llm]", llama)
	}
	if out != spec {
		t.Fatalf("llama.cpp spec must be left untouched")
	}
}

func TestStackVLLMNoAssignmentUnchanged(t *testing.T) {
	spec := `services:
  llm:
    image: vllm/vllm-openai:latest
    command: ["vllm", "serve", "model"]
`
	out, changed, llama := pinVLLMMemUtil(spec, nil)
	if changed != nil || llama != nil || out != spec {
		t.Fatalf("no assignment must be a no-op, got changed=%v llama=%v", changed, llama)
	}
}
