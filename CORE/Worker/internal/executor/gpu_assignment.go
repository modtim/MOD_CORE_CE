package executor

import (
	"sort"
	"strconv"
	"strings"

	"github.com/docker/docker/api/types/container"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// nvidiaDeviceRequest attaches NVIDIA GPUs to a container. With an API
// card assignment (§14.2) only the pinned cards are attached, by uuid when
// known and by index otherwise; without one the container gets every GPU,
// which is the behaviour older APIs rely on.
func nvidiaDeviceRequest(a *transport.GPUAssignment) container.DeviceRequest {
	req := container.DeviceRequest{
		Driver:       "nvidia",
		Count:        -1, // all GPUs
		Capabilities: [][]string{{"gpu"}},
	}
	ids := assignedDeviceIDs(a)
	if len(ids) > 0 {
		req.Count = 0
		req.DeviceIDs = ids
	}
	return req
}

func assignedDeviceIDs(a *transport.GPUAssignment) []string {
	if a == nil {
		return nil
	}
	var ids []string
	for _, c := range a.Cards {
		if c.Kind != "" && c.Kind != "cuda" {
			continue
		}
		if c.DeviceUUID != "" {
			ids = append(ids, c.DeviceUUID)
		} else {
			ids = append(ids, strconv.Itoa(c.Index))
		}
	}
	return ids
}

// assignedIndicesForKind returns the sorted host device indices §14.3 pinned
// for `kind` ("rocm"/"intel"). Nil assignment or no cards of that kind
// returns nil — callers fall back to the pre-pinning single TARGET_INDEX
// behaviour (older API, or an assignment for a different kind).
func assignedIndicesForKind(a *transport.GPUAssignment, kind string) []int {
	if a == nil {
		return nil
	}
	var idx []int
	for _, c := range a.Cards {
		if c.Kind == kind {
			idx = append(idx, c.Index)
		}
	}
	sort.Ints(idx)
	return idx
}

// assignedPCIBusIDForIndex returns the PCI bus id §14.3 recorded for the
// pinned `kind` card at host `index`, or "" when the API's inventory scan
// never reported one (older AcceleratorProfile row, or a unified-memory APU
// with no discrete PCI device). Callers use this to look the card up by its
// exact /sys/class/drm bus id instead of trusting that `index` still means
// the same physical card it did when the card was profiled.
func assignedPCIBusIDForIndex(a *transport.GPUAssignment, kind string, index int) string {
	if a == nil {
		return ""
	}
	for _, c := range a.Cards {
		if c.Kind == kind && c.Index == index {
			return c.PCIBusID
		}
	}
	return ""
}

// amdIntelVisibleDevicesEnv sets the in-container device-visibility env for
// a pinned rocm/intel assignment ("count" cards were just mounted into
// /dev/dri by the caller, in ascending host-index order). ROCm and Level
// Zero both enumerate only the /dev/dri nodes actually present in the
// container's namespace, in the same ascending order — so "device 0..N-1"
// inside the container always lines up with the N cards narrowDRIForKind
// mounted, regardless of what their indices were on the host. Values the
// node itself set in nodeEnv always win — they are never re-emitted here.
func amdIntelVisibleDevicesEnv(kind string, count int, nodeEnv map[string]string) []string {
	if count <= 0 {
		return nil
	}
	ids := make([]string, count)
	for i := range ids {
		ids[i] = strconv.Itoa(i)
	}
	joined := strings.Join(ids, ",")
	var candidates []string
	switch kind {
	case "rocm":
		candidates = []string{"HIP_VISIBLE_DEVICES=" + joined, "ROCR_VISIBLE_DEVICES=" + joined}
	case "intel":
		candidates = []string{"ZE_AFFINITY_MASK=" + joined}
	default:
		return nil
	}
	var env []string
	for _, kv := range candidates {
		key := kv[:strings.IndexByte(kv, '=')]
		if _, set := nodeEnv[key]; set {
			continue
		}
		env = append(env, kv)
	}
	return env
}

// gpuAssignmentEnv tells the node what it was given, so frameworks that can
// cap their own memory (PyTorch, TensorFlow, XLA) stay inside the reservation
// on a shared card and grow on demand instead of pre-allocating. Values the
// node itself set in nodeEnv always win — they are never re-emitted here.
// Fires for any pinned kind (cuda/rocm/intel), not only cuda — PYTORCH_HIP_
// ALLOC_CONF and the VRAM-limit vars are exactly as relevant on a shared
// rocm card as a shared cuda one.
func gpuAssignmentEnv(a *transport.GPUAssignment, nodeEnv map[string]string) []string {
	if a == nil || len(a.Cards) == 0 {
		return nil
	}
	candidates := []string{
		"MOD_GPU_MODE=" + a.Mode,
		"PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
		"PYTORCH_HIP_ALLOC_CONF=expandable_segments:True",
		"XLA_PYTHON_CLIENT_PREALLOCATE=false",
		"TF_FORCE_GPU_ALLOW_GROWTH=true",
	}
	if a.Mode == "shared" && a.VRAMLimitMB > 0 {
		candidates = append(candidates, "MOD_GPU_VRAM_LIMIT_MB="+strconv.Itoa(a.VRAMLimitMB))
		minCard := 0
		for _, c := range a.Cards {
			if c.CardVRAMMB > 0 && (minCard == 0 || c.CardVRAMMB < minCard) {
				minCard = c.CardVRAMMB
			}
		}
		if minCard > 0 {
			candidates = append(candidates, "MOD_GPU_CARD_VRAM_MB="+strconv.Itoa(minCard))
			frac := float64(a.VRAMLimitMB) / float64(minCard)
			if frac > 1.0 {
				frac = 1.0
			}
			candidates = append(candidates, "XLA_PYTHON_CLIENT_MEM_FRACTION="+strconv.FormatFloat(frac, 'f', 2, 64))
		}
	}
	var env []string
	for _, kv := range candidates {
		key := kv
		if eq := strings.IndexByte(kv, '='); eq >= 0 {
			key = kv[:eq]
		}
		if _, set := nodeEnv[key]; set {
			continue
		}
		env = append(env, kv)
	}
	return env
}
