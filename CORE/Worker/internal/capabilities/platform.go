package capabilities

import (
	"os"
	"runtime"
	"strings"
)

// Platform capability tags.
//
// These answer questions the MANAGER cannot answer for itself. The manager only
// sees what a worker reports, and inferring "gpu_type is CUDA, therefore this
// box can run GPU containers" is a guess that is wrong in a way that matters:
// the classic case is a host that enumerates its GPU and reports a real VRAM
// figure but cannot hand the device to a container (for example NVIDIA without
// the container toolkit). WSL2 is NOT such a case: NVIDIA, AMD and Intel GPUs
// all work under WSL2 (owner-confirmed and tested), so WSL2 is never a reason
// to refuse a GPU.
//
// runtime.GOOS cannot tell a WSL2 host from bare-metal Linux either: WSL2 *is*
// Linux and returns
// "linux". Only the machine itself can tell, by looking at its own kernel. So
// the worker probes and reports; the server matches at claim time.
const (
	// TagGPURuntime — this host can actually execute accelerated container
	// workloads, not merely enumerate an accelerator.
	TagGPURuntime = "gpu-runtime"

	// TagBareMetalGPU — accelerator access is native rather than through a
	// virtualisation bridge. Absent on WSL2 / GPU-PV. This is a topology
	// qualifier only: GPUs of every vendor work under WSL2, and gpu-runtime
	// (not this tag) is the capability gate.
	TagBareMetalGPU = "bare-metal-gpu"

	// TagServiceHost — this host can run a long-lived service that other
	// machines connect INTO. The concrete test lands with the overlay work
	// (mod-connect-service-mesh D2 decides whether it needs /dev/net/tun +
	// NET_ADMIN); the name is defined now so the matching mechanism is
	// already in place and filling it in stays additive.
	TagServiceHost = "service-host"
)

// isWSL reports whether this Linux kernel is a WSL bridge rather than bare metal.
//
// MEASURED, 2026-07-28, on an NVIDIA RTX 5000 Ada (16 GB) under WSL2 kernel
// 5.15.146.1:
//   - Plain CUDA compute WORKS. A 4096x4096 matmul in a container dispatched
//     and returned. (An older note in MULTI_OS_WORKER_STRATEGY said kernel
//     submit fails for Intel Battlemage and AMD R9000 under WSL2; the owner
//     has since confirmed all GPU vendors work under WSL2, so that claim is
//     retired.)
//   - vLLM WORKS TOO, with VLLM_WSL2_ENABLE_PIN_MEMORY=1. Without it vLLM
//     aborts at startup with "UVA is not available", but that is vLLM's own
//     conservative default, not a hardware limit: is_uva_available() is just
//     is_pin_memory_available(), which vLLM disables on WSL unless the kernel
//     is >= 4.19.121 AND that env var is set. Pinned memory and
//     CU_DEVICE_ATTRIBUTE_UNIFIED_ADDRESSING both probe as available here, and
//     two models served real completions concurrently on the one card.
//
// So this tag is a statement about TOPOLOGY, not capability: a WSL host is
// genuinely not bare metal (expect degraded pinned-memory throughput), but it
// can serve. Do NOT use bare-metal-gpu as a hard prerequisite for placing an
// accelerated stack — gpu-runtime is the capability gate. Using this one would
// wrongly refuse a host that demonstrably works.
//
// Both files are cheap reads and either can carry the marker depending on WSL
// version and distro.
func isWSL() bool {
	for _, path := range []string{"/proc/sys/kernel/osrelease", "/proc/version"} {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		lower := strings.ToLower(string(data))
		if strings.Contains(lower, "microsoft") || strings.Contains(lower, "wsl") {
			return true
		}
	}
	return false
}

// wslProbe is the WSL check used by derivePlatformTags; a variable so unit
// tests can simulate a WSL2 kernel on any host.
var wslProbe = isWSL

// hasContainerGPUSupport reports whether the container runtime can hand this
// host's accelerator to a container.
//
// Presence of the runtime hook is what actually matters — an NVIDIA box without
// nvidia-container-toolkit installed enumerates a GPU that no container can
// reach: it enumerates a GPU that nothing in a container can use.
func hasContainerGPUSupport(p *Profile) bool {
	if len(p.detectedAccelerators) == 0 {
		return false
	}
	cpuOnly := true
	for _, a := range p.detectedAccelerators {
		if a != "cpu" {
			cpuOnly = false
			break
		}
	}
	if cpuOnly {
		return false
	}
	// NVIDIA needs the container runtime hook. AMD/Intel/NPU pass devices
	// through directly (/dev/kfd, /dev/dri, /dev/accel), so device presence
	// is the test there and detectGPU already established it.
	for _, a := range p.detectedAccelerators {
		if a == "cuda" || a == "tensorrt" {
			for _, path := range []string{
				"/usr/bin/nvidia-container-runtime",
				"/usr/bin/nvidia-container-runtime-hook",
				"/usr/bin/nvidia-container-toolkit",
			} {
				if _, err := os.Stat(path); err == nil {
					return true
				}
			}
			return false
		}
	}
	return true
}

// derivePlatformTags returns the self-probed capability tags for this host.
//
// Deliberately conservative: a tag is emitted only when the capability is
// positively established. The claim-time guard uses AND semantics and fails
// closed, so a missing tag means "not placed here" rather than "placed and
// crashes later" — which is the correct direction to be wrong in.
func derivePlatformTags(p *Profile) []string {
	var tags []string

	gpuRuntime := hasContainerGPUSupport(p)
	if gpuRuntime {
		tags = append(tags, TagGPURuntime)
	}

	// Bare-metal accelerator access. Linux only in practice, and not through
	// a WSL bridge.
	if gpuRuntime && runtime.GOOS == "linux" && !wslProbe() {
		tags = append(tags, TagBareMetalGPU)
	}

	// Hosting a reachable service needs no special privilege on Linux today.
	// This tightens when the overlay work settles its requirements.
	if runtime.GOOS == "linux" {
		tags = append(tags, TagServiceHost)
	}

	return tags
}
