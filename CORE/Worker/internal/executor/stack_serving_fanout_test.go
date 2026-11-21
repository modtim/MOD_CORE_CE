package executor

import (
	"context"
	"strconv"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"gopkg.in/yaml.v3"
)

// megaplan 27.4 — the worker side of the MOD-native serving profile's replica
// device fan-out. services/llm_serving_render.py renders x-mod.serving as a
// GPU-free `lb` gateway plus N engine replicas, each declaring ONE card
// (`deploy...device_ids: [i]` on CUDA, `/dev/kfd + /dev/dri` plus
// `HIP_VISIBLE_DEVICES=i` on ROCm). These tests prove the stack deploy keeps
// replica i on card i, that replicas sharing one card both land on it, that
// `lb` never gets a GPU, and that the ROCm visible-devices value is renumbered
// to the container's own card. The last one is the bug this task found: the
// API's pin is written against the HOST's card numbering, so after
// pinAmdIntelService narrows a replica to a single /dev/dri node, replica 1
// still asked HIP for device 1 — a device its own container never mounts.

// cudaRendered models the renderer's CUDA output: `lb` with no GPU at all, and
// two replicas each already declaring a single-card nvidia reservation.
const cudaRendered = `services:
  lb:
    image: mod/sgl-model-gateway
    ports:
      - "8080:8080"
  vllm-0:
    image: vllm/vllm-openai
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids:
                - "0"
              capabilities: [gpu]
  vllm-1:
    image: vllm/vllm-openai
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids:
                - "1"
              capabilities: [gpu]
`

// rocmRendered models the renderer's ROCm output (stack29 shape): the bare
// /dev/kfd + /dev/dri passthrough, the render/group GIDs, and the per-replica
// HIP pin written against the HOST's card numbering.
const rocmRendered = `services:
  lb:
    image: mod/sgl-model-gateway
  llama-server-0:
    image: mod/llama-server-rocm
    devices:
      - /dev/kfd
      - /dev/dri
    group_add:
      - "44"
      - "992"
    environment:
      HIP_VISIBLE_DEVICES: "0"
  llama-server-1:
    image: mod/llama-server-rocm
    devices:
      - /dev/kfd
      - /dev/dri
    group_add:
      - "44"
      - "992"
    environment:
      HIP_VISIBLE_DEVICES: "1"
`

func dri(kind string, index int, preferBDF string) (string, string) {
	return "/dev/dri/card" + strconv.Itoa(index), "/dev/dri/renderD" + strconv.Itoa(128+index)
}

// gpuIDs returns a service's nvidia device_ids, or nil when it declares no
// GPU reservation (the gateway must stay nil).
func gpuIDs(t *testing.T, spec, service string) []string {
	t.Helper()
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	svc := mapValue(mapValue(doc.Content[0], "services"), service)
	deploy := mapValue(svc, "deploy")
	resources := mapValue(deploy, "resources")
	reservations := mapValue(resources, "reservations")
	devices := mapValue(reservations, "devices")
	if devices == nil || devices.Kind != yaml.SequenceNode {
		return nil
	}
	for _, dev := range devices.Content {
		if dev.Kind != yaml.MappingNode || !isNvidiaGPUReservation(dev) {
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

// hostDevices lists a service's `devices:` host sources.
func hostDevices(t *testing.T, spec, service string) []string {
	t.Helper()
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	svc := mapValue(mapValue(doc.Content[0], "services"), service)
	devices := mapValue(svc, "devices")
	if devices == nil || devices.Kind != yaml.SequenceNode {
		return nil
	}
	out := make([]string, 0, len(devices.Content))
	for _, d := range devices.Content {
		out = append(out, deviceSource(d))
	}
	return out
}

// envValue reads one key from a service's `environment:` (map or list form).
func servingEnv(t *testing.T, spec, service, key string) (string, bool) {
	t.Helper()
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	svc := mapValue(mapValue(doc.Content[0], "services"), service)
	env := mapValue(svc, "environment")
	if env == nil {
		return "", false
	}
	if env.Kind == yaml.MappingNode {
		_, v := mapEntry(env, key)
		if v == nil {
			return "", false
		}
		return v.Value, true
	}
	for _, e := range env.Content {
		if strings.HasPrefix(e.Value, key+"=") {
			return e.Value[len(key)+1:], true
		}
	}
	return "", false
}

func hasIn(t *testing.T, list []string, want string) bool {
	t.Helper()
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// TestServingFanoutCudaTwoCards: the API already pinned each replica to one
// card; the worker must keep that split (uuid pins), not collapse both replicas
// onto one card or hand every card to every replica. `lb` gets no GPU.
func TestServingFanoutCudaTwoCards(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "exclusive",
		Cards: []transport.AssignedGPU{
			{CardID: "c0", DeviceUUID: "GPU-A", Index: 0, Kind: "cuda"},
			{CardID: "c1", DeviceUUID: "GPU-B", Index: 1, Kind: "cuda"},
		},
		ServiceCards: map[string][]string{"vllm-0": {"c0"}, "vllm-1": {"c1"}},
	}
	out, changed := pinStackGPUs(cudaRendered, a, dri)
	if len(changed) != 2 {
		t.Fatalf("changed = %v, want [vllm-0 vllm-1]", changed)
	}
	if got := gpuIDs(t, out, "lb"); got != nil {
		t.Fatalf("lb must declare no GPU, got %v", got)
	}
	g0 := gpuIDs(t, out, "vllm-0")
	g1 := gpuIDs(t, out, "vllm-1")
	if len(g0) != 1 || g0[0] != "GPU-A" {
		t.Fatalf("vllm-0 device_ids = %v, want [GPU-A]", g0)
	}
	if len(g1) != 1 || g1[0] != "GPU-B" {
		t.Fatalf("vllm-1 device_ids = %v, want [GPU-B]", g1)
	}
}

// TestServingFanoutCudaOneCardTwoSlots: 1 card, 2 replicas sharing it (mode
// shared). Both replicas must land on that one card and nothing else.
func TestServingFanoutCudaOneCardShared(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode:         "shared",
		Cards:        []transport.AssignedGPU{{CardID: "c0", DeviceUUID: "GPU-A", Index: 0, Kind: "cuda"}},
		ServiceCards: map[string][]string{"vllm-0": {"c0"}, "vllm-1": {"c0"}},
	}
	out, changed := pinStackGPUs(cudaRendered, a, dri)
	if len(changed) != 2 {
		t.Fatalf("changed = %v, want both replicas", changed)
	}
	for _, name := range []string{"vllm-0", "vllm-1"} {
		got := gpuIDs(t, out, name)
		if len(got) != 1 || got[0] != "GPU-A" {
			t.Fatalf("%s device_ids = %v, want [GPU-A]", name, got)
		}
	}
	if got := gpuIDs(t, out, "lb"); got != nil {
		t.Fatalf("lb must declare no GPU, got %v", got)
	}
}

// TestServingFanoutRocmTwoCards: each replica keeps the shared /dev/kfd, is
// narrowed to its own /dev/dri card + render node, and its HIP pin is RENUMBERED
// to 0 — the container now holds exactly one card, so HIP_VISIBLE_DEVICES=1
// (the API's host-index pin) would have pointed at a device that is never
// mounted. ROCR is not invented when the author only pinned HIP.
func TestServingFanoutRocmTwoCards(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode: "exclusive",
		Cards: []transport.AssignedGPU{
			{CardID: "r0", Index: 0, Kind: "rocm"},
			{CardID: "r1", Index: 1, Kind: "rocm"},
		},
		ServiceCards: map[string][]string{"llama-server-0": {"r0"}, "llama-server-1": {"r1"}},
	}
	out, changed := pinStackGPUs(rocmRendered, a, dri)
	if len(changed) != 2 {
		t.Fatalf("changed = %v, want both llama replicas", changed)
	}
	d0 := hostDevices(t, out, "llama-server-0")
	d1 := hostDevices(t, out, "llama-server-1")
	if !hasIn(t, d0, "/dev/kfd") || !hasIn(t, d1, "/dev/kfd") {
		t.Fatalf("both replicas must keep /dev/kfd: %v / %v", d0, d1)
	}
	if !hasIn(t, d0, "/dev/dri/card0") || !hasIn(t, d0, "/dev/dri/renderD128") {
		t.Fatalf("llama-server-0 = %v, want card0/renderD128", d0)
	}
	if !hasIn(t, d1, "/dev/dri/card1") || !hasIn(t, d1, "/dev/dri/renderD129") {
		t.Fatalf("llama-server-1 = %v, want card1/renderD129", d1)
	}
	if hasIn(t, d0, "/dev/dri/card1") || hasIn(t, d1, "/dev/dri/card0") {
		t.Fatalf("replicas must not share a card: %v / %v", d0, d1)
	}
	for _, name := range []string{"llama-server-0", "llama-server-1"} {
		hip, ok := servingEnv(t, out, name, "HIP_VISIBLE_DEVICES")
		if !ok || hip != "0" {
			t.Fatalf("%s HIP_VISIBLE_DEVICES = %s (present=%v), want 0 — the container holds one card",
				name, hip, ok)
		}
		rocr, present := servingEnv(t, out, name, "ROCR_VISIBLE_DEVICES")
		if present {
			t.Fatalf("%s must not gain an invented ROCR_VISIBLE_DEVICES: %s", name, rocr)
		}
	}
	if hostDevices(t, out, "lb") != nil {
		t.Fatalf("lb must stay GPU-free")
	}
}

// TestServingFanoutRocmSharedCard: 1 card, 2 replicas sharing it. Both keep
// /dev/kfd and both get card 0's nodes only — never a second card.
func TestServingFanoutRocmSharedCard(t *testing.T) {
	a := &transport.GPUAssignment{
		Mode:         "shared",
		Cards:        []transport.AssignedGPU{{CardID: "r0", Index: 0, Kind: "rocm"}},
		ServiceCards: map[string][]string{"llama-server-0": {"r0"}, "llama-server-1": {"r0"}},
	}
	out, changed := pinStackGPUs(rocmRendered, a, dri)
	if len(changed) != 2 {
		t.Fatalf("changed = %v, want both replicas", changed)
	}
	for _, name := range []string{"llama-server-0", "llama-server-1"} {
		devs := hostDevices(t, out, name)
		if !hasIn(t, devs, "/dev/kfd") || !hasIn(t, devs, "/dev/dri/card0") ||
			!hasIn(t, devs, "/dev/dri/renderD128") {
			t.Fatalf("%s = %v, want /dev/kfd + card0/renderD128", name, devs)
		}
		if hasIn(t, devs, "/dev/dri/card1") {
			t.Fatalf("%s must not get a second card: %v", name, devs)
		}
		hip, ok := servingEnv(t, out, name, "HIP_VISIBLE_DEVICES")
		if !ok || hip != "0" {
			t.Fatalf("%s HIP_VISIBLE_DEVICES = %s, want 0", name, hip)
		}
	}
}

// TestServingFanoutRocmWSL: on a WSL2 worker the bare-metal nodes are remapped
// to /dev/dxg (no /dev/kfd, no /dev/dri exists there) and the host GPU libs are
// bound in; the GPU-free lb is left alone. This is the existing wslRemapRocmDevices
// contract, asserted for the rendered serving shape.
func TestServingFanoutRocmWSL(t *testing.T) {
	remapped, changed := wslRemapRocmDevices(rocmRendered)
	if len(changed) != 2 {
		t.Fatalf("remap changed = %v, want both llama replicas", changed)
	}
	for _, name := range []string{"llama-server-0", "llama-server-1"} {
		devs := hostDevices(t, remapped, name)
		if !hasIn(t, devs, "/dev/dxg") {
			t.Fatalf("%s must be remapped to /dev/dxg, got %v", name, devs)
		}
		if hasIn(t, devs, "/dev/kfd") || hasIn(t, devs, "/dev/dri") {
			t.Fatalf("%s must drop bare-metal nodes, got %v", name, devs)
		}
	}
	if hostDevices(t, remapped, "lb") != nil {
		t.Fatalf("lb has no devices to remap")
	}
	bound, boundNames := wslStackGPUBinds(context.Background(), remapped,
		"/usr/lib/librocdxg.so", nil)
	if len(boundNames) != 2 {
		t.Fatalf("binds changed = %v, want both llama replicas", boundNames)
	}
	if !strings.Contains(bound, "/usr/lib/wsl:/usr/lib/wsl:ro") {
		t.Fatalf("want the WSL user-mode driver bind, got:\n%s", bound)
	}
}
