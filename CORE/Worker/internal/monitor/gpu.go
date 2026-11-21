package monitor

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// GPUSnapshot captures GPU resource state at a point in time.
type GPUSnapshot struct {
	Type             string  `json:"type"` // CUDA, ROCm, Intel, none
	VRAMUsedMB       float64 `json:"vram_used_mb"`
	VRAMTotalMB      float64 `json:"vram_total_mb"`
	VRAMFreeMB       float64 `json:"vram_free_mb"`
	FragmentationPct float64 `json:"vram_fragmentation_pct"` // approximation
	UtilizationPct   float64 `json:"gpu_utilization_pct"`
	TemperatureC     float64 `json:"gpu_temperature_c"`
	PowerState       string  `json:"gpu_power_state"` // "active", "idle", "low_power"
	MemoryUtilPct    float64 `json:"gpu_memory_util_pct"`
	ThrottleReasons  string  `json:"throttle_reasons,omitempty"`
	ClockMHz         float64 `json:"gpu_clock_mhz"`
	// MAC.1.s14 — Metal/darwin extras. PowerMW is the GPU's power draw in
	// milliwatts (powermetrics reports mW; nvidia-smi's csv path has no
	// comparable field, so it stays 0 there). MetalProbeError names why a
	// darwin GPU probe could not answer ("powermetrics needs root" / a
	// timeout) instead of letting the box look idle; absent when the probe
	// worked. GPUTasks is the PID-attributed GPU time gpu_activity reports
	// (per-process, the darwin analogue of the nvidia --query-compute-apps
	// rows gpu.go intersects with a container's cgroup).
	PowerMW          float64          `json:"gpu_power_mw"`
	MetalProbeError  string           `json:"metal_probe_error,omitempty"`
	GPUTasks         []GPUPID         `json:"gpu_tasks,omitempty"`
	// Cards carries the per-card breakdown where the sampler can measure it
	// (ROCm sysfs path); nil for the single-card tool paths.
	Cards []GPUCard `json:"cards,omitempty"`
}

// ContainerVRAMUsedMB returns GPU memory attributed to the processes inside
// one container, or (0, false) when it cannot be determined.
//
// Why this exists: SampleGPU reports memory.used, which is the WHOLE CARD.
// Recording that as a task's vram_mb attributes every other tenant's memory to
// whichever task happened to be sampled. It errs in the damaging direction —
// it OVERSTATES each node's demand, so a packer fed those numbers refuses to
// co-schedule work that would comfortably fit.
//
// nvidia-smi --query-compute-apps gives per-PROCESS memory. Intersecting it
// with the PIDs in the container's cgroup gives that container's real share.
// Host-wide usage is still reported separately as the contention environment,
// which is a different and legitimate question.
func ContainerVRAMUsedMB(gpuType, containerID string) (float64, bool) {
	if containerID == "" {
		return 0, false
	}
	pids := containerPIDs(containerID)
	if len(pids) == 0 {
		return 0, false
	}
	switch gpuType {
	case "CUDA":
		return nvidiaVRAMForPIDs(pids)
	case "ROCm":
		return rocmVRAMForPIDs(pids)
	default:
		// Intel/NPU expose no comparable per-process query; better to report
		// nothing than to report the whole card as if it were this task's.
		return 0, false
	}
}

// containerPIDs reads the PIDs in a container's cgroup. Covers cgroup v2 and
// v1, and both the docker and containerd path layouts.
func containerPIDs(containerID string) map[int]bool {
	candidates := []string{
		"/sys/fs/cgroup/system.slice/docker-" + containerID + ".scope/cgroup.procs",
		"/sys/fs/cgroup/docker/" + containerID + "/cgroup.procs",
		"/sys/fs/cgroup/pids/docker/" + containerID + "/cgroup.procs",
		"/sys/fs/cgroup/memory/docker/" + containerID + "/cgroup.procs",
	}
	out := map[int]bool{}
	for _, path := range candidates {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		for _, line := range strings.Fields(string(data)) {
			if pid, err := strconv.Atoi(line); err == nil {
				out[pid] = true
			}
		}
		if len(out) > 0 {
			return out
		}
	}
	return out
}

func nvidiaVRAMForPIDs(pids map[int]bool) (float64, bool) {
	out, err := exec.Command("nvidia-smi",
		"--query-compute-apps=pid,used_memory",
		"--format=csv,noheader,nounits").Output()
	if err != nil {
		return 0, false
	}
	return sumVRAMRows(string(out), pids)
}

func rocmVRAMForPIDs(pids map[int]bool) (float64, bool) {
	out, err := exec.Command("rocm-smi", "--showpidgpus", "--csv").Output()
	if err != nil {
		return 0, false
	}
	return sumVRAMRows(string(out), pids)
}

// sumVRAMRows totals "pid, mem_mb" rows for PIDs in the set. Returns
// (0, true) when the tool answered but none of our PIDs hold GPU memory —
// a real measurement of zero, distinct from "could not tell".
func sumVRAMRows(raw string, pids map[int]bool) (float64, bool) {
	var total float64
	answered := false
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.Split(line, ",")
		if len(parts) < 2 {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(parts[0]))
		if err != nil {
			continue
		}
		answered = true
		if !pids[pid] {
			continue
		}
		mb, err := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
		if err != nil {
			continue
		}
		total += mb
	}
	return total, answered
}

// SampleGPU detects and samples the GPU. Returns a zero-value snapshot if no GPU.
func SampleGPU(gpuType string) GPUSnapshot {
	switch gpuType {
	case "CUDA":
		return sampleNVIDIA()
	case "ROCm":
		return sampleAMD()
	case "Intel":
	 return sampleIntel()
	case "Metal":
	 // MAC.1.s14 — darwin only (gpu_darwin.go). Off darwin the stub returns
	 // an empty snapshot, which SampleGPU then falls through to "none".
	 if snap := sampleDarwinMetal(); snap.Type != "" {
	 return snap
	 }
	 return GPUSnapshot{Type: "none"}
	default:
		// No accelerator was profiled at boot, but the amdgpu driver may
		// still be visible in sysfs — sample it rather than report a blind
		// "none" (and fail soft: no amdgpu cards → "none" as before).
		if snap := sampleAMDSysfsSnapshot(); snap.Type != "" {
			return snap
		}
		return GPUSnapshot{Type: "none"}
	}
}

func sampleNVIDIA() GPUSnapshot {
	snap := GPUSnapshot{Type: "CUDA"}

	out, err := exec.Command("nvidia-smi",
		"--query-gpu=memory.used,memory.total,memory.free,utilization.gpu,utilization.memory,temperature.gpu,pstate,clocks_throttle_reasons.active,clocks.current.graphics",
		"--format=csv,noheader,nounits",
	).Output()
	if err != nil {
		return snap
	}

	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) == 0 {
		return snap
	}

	// Use first GPU (aggregate multi-GPU later if needed)
	fields := strings.Split(lines[0], ", ")
	if len(fields) < 7 {
		return snap
	}

	snap.VRAMUsedMB = parseF64(fields[0])
	snap.VRAMTotalMB = parseF64(fields[1])
	snap.VRAMFreeMB = parseF64(fields[2])
	snap.UtilizationPct = parseF64(fields[3])
	snap.MemoryUtilPct = parseF64(fields[4])
	snap.TemperatureC = parseF64(fields[5])

	// Power state: P0 = active, P5-P8 = idle, P12 = low_power
	pstate := strings.TrimSpace(fields[6])
	snap.PowerState = classifyPowerState(pstate)

	if len(fields) > 7 {
		snap.ThrottleReasons = strings.TrimSpace(fields[7])
	}
	if len(fields) > 8 {
		snap.ClockMHz = parseF64(fields[8])
	}

	// Approximate fragmentation: without CUDA API we can't know the largest
	// free block, so we estimate based on memory utilization.
	// High util with fragmented allocations is common — use a heuristic:
	// fragmentation increases as more small allocations are made.
	if snap.VRAMTotalMB > 0 && snap.VRAMFreeMB > 0 {
		usedPct := snap.VRAMUsedMB / snap.VRAMTotalMB
		// Rough heuristic: fragmentation grows with utilization
		snap.FragmentationPct = usedPct * 0.15 // 0-15% range
	}

	return snap
}

func classifyPowerState(pstate string) string {
	pstate = strings.ToUpper(strings.TrimSpace(pstate))
	if pstate == "" {
		return "unknown"
	}
	// Extract numeric part: P0, P1, ... P12
	if strings.HasPrefix(pstate, "P") {
		num, err := strconv.Atoi(pstate[1:])
		if err == nil {
			switch {
			case num <= 2:
				return "active"
			case num <= 8:
				return "idle"
			default:
				return "low_power"
			}
		}
	}
	return "active" // default assumption
}

func sampleAMD() GPUSnapshot {
	// sysfs first: no binary dependency, and the worker image carries no
	// rocm-smi. When no amdgpu card is visible there (WSL2 has no amdgpu
	// sysfs at all), fall through to rocm-smi — the previous behaviour.
	if snap := sampleAMDSysfsSnapshot(); snap.Type != "" {
		return snap
	}

	snap := GPUSnapshot{Type: "ROCm"}

	out, err := exec.Command("rocm-smi",
		"--showmeminfo", "vram",
		"--showuse",
		"--showtemp",
		"--csv",
	).Output()
	if err != nil {
		return snap
	}

	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	for _, line := range lines {
		fields := strings.Split(line, ",")
		if len(fields) < 2 {
			continue
		}
		key := strings.TrimSpace(fields[0])
		val := strings.TrimSpace(fields[1])

		switch {
		case strings.Contains(key, "VRAM Total"):
			snap.VRAMTotalMB = parseF64(val) / (1024 * 1024) // bytes to MB
		case strings.Contains(key, "VRAM Used"):
			snap.VRAMUsedMB = parseF64(val) / (1024 * 1024)
		case strings.Contains(key, "GPU use"):
			snap.UtilizationPct = parseF64(val)
		case strings.Contains(key, "Temperature"):
			snap.TemperatureC = parseF64(val)
		}
	}
	snap.VRAMFreeMB = snap.VRAMTotalMB - snap.VRAMUsedMB
	snap.PowerState = "active" // ROCm doesn't expose pstate easily
	return snap
}

// intelSysfsRoot is the sysfs prefix the Intel sampler reads; overridable in
// tests, production is the host sysfs (sysfs is never namespaced away from a
// container).
var intelSysfsRoot = "/sys"

// intelTileVRAM sums one DRM card's Intel Xe driver VRAM nodes: every
// tileN/vram0/total (some driver builds nest it under tileN/memory/vram0 —
// the same glob 1900ARC5's sizer reads at enumeration), plus vram0/free and
// vram0/heap_size when the driver exposes them. Bytes. found=false when no
// tile reports a total — iGPUs and i915-driver cards have no Xe vram0 nodes,
// and the caller must not invent a number for them.
func intelTileVRAM(cardDir string) (total, free, heap int64, freeKnown, found bool) {
	entries, err := os.ReadDir(filepath.Join(cardDir, "device"))
	if err != nil {
		return 0, 0, 0, false, false
	}
	var tiles, tilesWithFree int64
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "tile") {
			continue
		}
		for _, base := range []string{
			filepath.Join(cardDir, "device", name, "vram0"),
			(filepath.Join(cardDir, "device", name, "memory", "vram0")),
		} {
			t, ok := readSysfsInt64(filepath.Join(base, "total"))
			if !ok {
				continue
			}
			total += t
			tiles++
			if f, okFree := readSysfsInt64(filepath.Join(base, "free")); okFree {
			 free += f
			 tilesWithFree++
			}
			h, _ := readSysfsInt64(filepath.Join(base, "heap_size"))
			heap += h
			break
		}
	}
	return total, free, heap, tiles > 0 && tilesWithFree == tiles, tiles > 0
}

// sampleIntelSysfsSnapshot aggregates the Xe sysfs reads into the box-wide
// heartbeat snapshot: VRAM summed across every 0x8086 card (vendor guard
// mirrors capabilities.enumerateIntel), free straight from vram0/free when
// every tile exposes it, otherwise total - heap_size (the driver's allocated
// heap — the same total-minus-used shape the AMD sysfs path uses). Returns an
// empty snapshot (no Type) when no Intel card exposes vram0 nodes; the caller
// then falls back to xpu-smi.
func sampleIntelSysfsSnapshot() GPUSnapshot {
	entries, err := os.ReadDir(filepath.Join(intelSysfsRoot, "class", "drm"))
	if err != nil {
		return GPUSnapshot{}
	}
	var total, free, heap int64
	var cards, cardsWithoutFree int64
	for _, entry := range entries {
		name := entry.Name()
	if !strings.HasPrefix(name, "card") || strings.Contains(name, "-") {
		continue
	}
	cardDir := filepath.Join(intelSysfsRoot, "class", "drm", name)
	vendorData, err := os.ReadFile(filepath.Join(cardDir, "device", "vendor"))
	if err != nil || strings.TrimSpace(string(vendorData)) != "0x8086" {
		continue // not an Intel card
	}
	t, f, h, freeKnown, found := intelTileVRAM(cardDir)
	if !found {
		continue // iGPU / i915 driver: no Xe vram0 nodes — report nothing
	}
	total += t
	free += f
	heap += h
	cards++
	if !freeKnown {
		cardsWithoutFree++
	}
	}
	if cards == 0 {
		return GPUSnapshot{}
	}
	snap := GPUSnapshot{Type: "Intel"}
	snap.VRAMTotalMB = bytesToMB(total)
	snap.VRAMUsedMB = bytesToMB(heap)
	if cardsWithoutFree == 0 {
		snap.VRAMFreeMB = bytesToMB(free)
	} else {
		snap.VRAMFreeMB = snap.VRAMTotalMB - snap.VRAMUsedMB
	}
	if snap.VRAMFreeMB < 0 {
		snap.VRAMFreeMB = 0
	}
	// Xe sysfs exposes no busy-percent node; "unknown" keeps the API's
	// low_power hard stop honest without claiming activity we didn't see.
	snap.PowerState = "unknown"
	return snap
}

// parseIntelXpuStatsMB pulls the memory figures out of xpu-smi stats --json
// without unmarshalling — the JSON shape varies by xpu-smi version (nested
// objects, l0_* vs gpu_* keys, pretty or compact), so scan every "key":
// number pair and sum across devices. xpu-smi's stats memory values are
// already MB.
var xpuStatsField = regexp.MustCompile(`"([A-Za-z0-9_.-]+)"\s*:\s*([0-9]+(?:\.[0-9]+)?)`)

func parseIntelXpuStatsMB(s string) (total, free, used, util float64) {
	for _, m := range xpuStatsField.FindAllStringSubmatch(s, -1) {
		key := strings.ToLower(m[1])
		v, err := strconv.ParseFloat(m[2], 64)
		if err != nil {
			continue
		}
		if strings.Contains(key, "utilization") {
			util += v
			continue
		}
		if !strings.Contains(key, "memory") {
			continue
		}
		if strings.Contains(key, "total") {
			total += v
		} else if strings.Contains(key, "free") {
			free += v
		} else if strings.Contains(key, "used") {
			used += v
		}
	}
	return total, free, used, util
}

func sampleIntel() GPUSnapshot {
	// 1900ARC9: the Xe driver's own sysfs byte counts first — no binary
	// dependency, and the same tileN/vram0 glob 1900ARC5 proved authoritative
	// at enumeration. xpu-smi stats (MB) is only the fallback for boxes whose
	// kernel doesn't expose Xe sysfs but do ship xpu-smi.
	if snap := sampleIntelSysfsSnapshot(); snap.Type != "" {
		return snap
	}

	snap := GPUSnapshot{Type: "Intel"}

	out, err := exec.Command("xpu-smi", "stats", "-d", "0", "--json").Output()
	if err != nil {
		snap.PowerState = "unknown"
		return snap
	}

	s := string(out)
	total, free, used, util := parseIntelXpuStatsMB(s)
	snap.VRAMTotalMB = total
	snap.VRAMUsedMB = used
	snap.VRAMFreeMB = free
	if total > 0 && free == 0 {
		snap.VRAMFreeMB = total - used
		if snap.VRAMFreeMB < 0 {
			snap.VRAMFreeMB = 0
		}
	}
	if strings.Contains(s, "gpu_utilization") || util > 0 {
		snap.PowerState = "active"
	}
	return snap
}

func parseF64(s string) float64 {
	v, _ := strconv.ParseFloat(strings.TrimSpace(s), 64)
	return v
}
