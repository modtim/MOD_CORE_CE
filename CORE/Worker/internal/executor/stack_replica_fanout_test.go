package executor

import (
	"context"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"gopkg.in/yaml.v3"
)

// §14.7b / task 27.4 — the API renders an x-mod.serving profile as a gateway
// `lb` service + N engine replicas, each pinned to ONE distinct GPU. These
// tests prove the worker's stack-deploy rewrite keeps each replica on its own
// card, leaves the load-balancer without a GPU, preserves explicit per-service
// pinning, and (on a WSL worker) hands each ROCm replica the /dev/dxg treatment.

// cudaServingCompose models the stack30 vLLM gateway fixture: a GPU-free `lb`
// plus two vLLM replicas, each declaring an all-GPU nvidia reservation.
const cudaServingCompose = `services:
  lb:
    image: lmsysorg/sgl-model-gateway
    ports:
      - "8080:8080"
    command:
      - --worker-urls
      - http://vllm-0:8000
      - http://vllm-1:8000
  vllm-0:
    image: vllm/vllm-openai
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
  vllm-1:
    image: vllm/vllm-openai
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
`

// rocmServingCompose models the stack29 llama.cpp gateway fixture: a GPU-free
// `lb` plus two ROCm engine replicas, each with the bare-metal kfd/dri passthrough.
const rocmServingCompose = `services:
  lb:
    image: lmsysorg/sgl-model-gateway
    ports:
      - "8080:8080"
  llama-server-0:
    image: ghcr.io/ggml-org/llama.cpp:server-rocm
    devices:
      - /dev/kfd
      - /dev/dri
    group_add:
      - "44"
      - "992"
  llama-server-1:
    image: ghcr.io/ggml-org/llama.cpp:server-rocm
    devices:
      - /dev/kfd
      - /dev/dri
    group_add:
      - "44"
      - "992"
`

// serviceDeviceIDs extracts the nvidia device_ids list of one service from a
// rewritten compose spec. Returns nil when the service has no nvidia reservation.
func serviceDeviceIDs(t *testing.T, spec, service string) []string {
	t.Helper()
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil {
		t.Fatalf("re-parse output: %v", err)
	}
	services := mapValue(doc.Content[0], "services")
	svc := mapValue(services, service)
	deploy := mapValue(svc, "deploy")
	resources := mapValue(deploy, "resources")
	reservations := mapValue(resources, "reservations")
	devices := mapValue(reservations, "devices")
	if devices == nil || devices.Kind != yaml.SequenceNode {
		return nil
	}
	for _, dev := range devices.Content {
		if !isNvidiaGPUReservation(dev) {
			continue
		}
		ids := mapValue(dev, "device_ids")
		if ids == nil || ids.Kind != yaml.SequenceNode {
			return nil
		}
		out := make([]string, 0, len(ids.Content))
		for _, c := range ids.Content {
			out = append(out, c.Value)
		}
		return out
	}
	return nil
}

// serviceDevices lists the host-side device sources of one service.
func serviceDevices(t *testing.T, spec, service string) []string {
	t.Helper()
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil {
		t.Fatalf("re-parse output: %v", err)
	}
	services := mapValue(doc.Content[0], "services")
	devices := mapValue(mapValue(services, service), "devices")
	if devices == nil {
		return nil
	}
	out := make([]string, 0, len(devices.Content))
	for _, d := range devices.Content {
		out = append(out, deviceSource(d))
	}
	return out
}

func contains(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// TestFanoutCUDAReplicasGetDistinctCards — two vLLM replicas on a 2-GPU host
// land on different cards, the load-balancer gets none, and each replica keeps
// only its own card (no collapse onto GPU0).
func TestFanoutCUDAReplicasGetDistinctCards(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "exclusive",
		Cards: []transport.AssignedGPU{
			{CardID: "c0", DeviceUUID: "GPU-CUDA-A", Index: 0, Kind: "cuda"},
			{CardID: "c1", DeviceUUID: "GPU-CUDA-B", Index: 1, Kind: "cuda"},
		},
		ServiceCards: map[string][]string{
			"vllm-0": {"c0"},
			"vllm-1": {"c1"},
		},
	}
	out, changed := pinStackGPUs(cudaServingCompose, a, fakeDRIResolver)

	if len(changed) != 2 || changed[0] != "vllm-0" || changed[1] != "vllm-1" {
		t.Fatalf("changed = %v, want [vllm-0 vllm-1] (lb must not be pinned)", changed)
	}
	if got := serviceDeviceIDs(t, out, "lb"); got != nil {
		t.Fatalf("lb must have no GPU, got device_ids %v", got)
	}
	got0 := serviceDeviceIDs(t, out, "vllm-0")
	got1 := serviceDeviceIDs(t, out, "vllm-1")
	if len(got0) != 1 || got0[0] != "GPU-CUDA-A" {
		t.Fatalf("vllm-0 device_ids = %v, want [GPU-CUDA-A]", got0)
	}
	if len(got1) != 1 || got1[0] != "GPU-CUDA-B" {
		t.Fatalf("vllm-1 device_ids = %v, want [GPU-CUDA-B]", got1)
	}
	if !contains(got0, "GPU-CUDA-A") || contains(got0, "GPU-CUDA-B") {
		t.Fatalf("vllm-0 must hold only its own card, got %v", got0)
	}
	if contains(got1, "GPU-CUDA-A") || !contains(got1, "GPU-CUDA-B") {
		t.Fatalf("vllm-1 must hold only its own card, got %v", got1)
	}
}

// TestFanoutROCmReplicasGetDistinctCards — two llama.cpp replicas on a 2-card
// ROCm host each get a different /dev/dri card + a HIP_VISIBLE_DEVICES pinning
// that card to in-container device 0; the load-balancer is left alone.
func TestFanoutROCmReplicasGetDistinctCards(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "exclusive",
		Cards: []transport.AssignedGPU{
			{CardID: "r0", Index: 0, Kind: "rocm"},
			{CardID: "r1", Index: 1, Kind: "rocm"},
		},
		ServiceCards: map[string][]string{
			"llama-server-0": {"r0"},
			"llama-server-1": {"r1"},
		},
	}
	out, changed := pinStackGPUs(rocmServingCompose, a, fakeDRIResolver)

	if len(changed) != 2 || changed[0] != "llama-server-0" || changed[1] != "llama-server-1" {
		t.Fatalf("changed = %v, want both llama replicas", changed)
	}
	devs0 := serviceDevices(t, out, "llama-server-0")
	devs1 := serviceDevices(t, out, "llama-server-1")
	// each keeps the shared /dev/kfd ...
	if !contains(devs0, "/dev/kfd") || !contains(devs1, "/dev/kfd") {
		t.Fatalf("both replicas must keep /dev/kfd: %v / %v", devs0, devs1)
	}
	// ... and are narrowed to a distinct card + render node.
	if !contains(devs0, "/dev/dri/card0") || !contains(devs0, "/dev/dri/renderD128") {
		t.Fatalf("llama-server-0 = %v, want card0/renderD128", devs0)
	}
	if contains(devs0, "/dev/dri/card1") {
		t.Fatalf("llama-server-0 must not get card1: %v", devs0)
	}
	if !contains(devs1, "/dev/dri/card1") || !contains(devs1, "/dev/dri/renderD129") {
		t.Fatalf("llama-server-1 = %v, want card1/renderD129", devs1)
	}
	if contains(devs1, "/dev/dri/card0") {
		t.Fatalf("llama-server-1 must not get card0: %v", devs1)
	}
	// each replica maps its single card to in-container device 0.
	if !strings.Contains(out, "HIP_VISIBLE_DEVICES=0") || !strings.Contains(out, "ROCR_VISIBLE_DEVICES=0") {
		t.Fatalf("want HIP/ROCR_VISIBLE_DEVICES=0, got:\n%s", out)
	}
}

// TestFanoutWSLRemapsRocmToDxg — on a WSL2 worker (no /dev/kfd, no /dev/dri)
// each ROCm replica's bare-metal passthrough is remapped to /dev/dxg and the
// host GPU libraries are bound in; the GPU-free lb service is left untouched.
func TestFanoutWSLRemapsRocmToDxg(t *testing.T) {
	const lib = "/usr/lib/librocdxg.so"

	remapped, changedRemap := wslRemapRocmDevices(rocmServingCompose)
	if len(changedRemap) != 2 || changedRemap[0] != "llama-server-0" || changedRemap[1] != "llama-server-1" {
		t.Fatalf("remap changed = %v, want both llama replicas", changedRemap)
	}
	for _, svc := range []string{"llama-server-0", "llama-server-1"} {
		devs := serviceDevices(t, remapped, svc)
		if !contains(devs, "/dev/dxg") {
			t.Fatalf("%s must be remapped to /dev/dxg, got %v", svc, devs)
		}
		if contains(devs, "/dev/kfd") || contains(devs, "/dev/dri") {
			t.Fatalf("%s must drop bare-metal nodes, got %v", svc, devs)
		}
	}
	if d := serviceDevices(t, remapped, "lb"); d != nil {
		t.Fatalf("lb has no devices to remap, got %v", d)
	}

	boundSpec, bound := wslStackGPUBinds(context.Background(), remapped, lib, nil)
	if len(bound) != 2 || bound[0] != "llama-server-0" || bound[1] != "llama-server-1" {
		t.Fatalf("binds changed = %v, want both llama replicas", bound)
	}
	if !strings.Contains(boundSpec, "/usr/lib/wsl:/usr/lib/wsl:ro") {
		t.Fatalf("want the WSL user-mode driver bind, got:\n%s", boundSpec)
	}
	if !strings.Contains(boundSpec, "/opt/rocm/lib/librocdxg.so:ro") {
		t.Fatalf("want the host librocdxg bind, got:\n%s", boundSpec)
	}
}
