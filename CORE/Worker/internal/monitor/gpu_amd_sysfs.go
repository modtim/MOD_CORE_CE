package monitor

import (
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// GPUCard is one physical card's measurement, keyed on the same identity the
// worker uses for ROCm devices elsewhere:
//
//   - Index is the ordinal among AMD cards under /sys/class/drm — the exact
//     ordinal capabilities.enumerateROCmFromDRI assigns to AcceleratorDevice
//     rows the heartbeat sends, and the value service_cards card ids carry
//     (the API's card id is str(index)).
//   - PCIBusID is the base of the card's /sys/class/drm/device symlink, the
//     same correlation key §14.3's index→renderD pinning uses (via
//     driPathsFromSysfs) to resolve an AssignedGPU to its render node.
//
// Per-card values therefore line up with gpu_assignment.service_cards by
// either key.
type GPUCard struct {
	Index       int     `json:"index"`
	PCIBusID    string  `json:"pci_bus_id,omitempty"`
	VRAMTotalMB float64 `json:"vram_total_mb"`
	VRAMUsedMB  float64 `json:"vram_used_mb"`
	UtilPct     float64 `json:"utilization_pct"`
}

// amdSysfsRoot is the sysfs prefix the AMD sampler reads; overridable in
// tests. Production is the host sysfs, which the worker container sees
// unmodified (sysfs is never namespaced away from a container).
var amdSysfsRoot = "/sys"

// sampleAMDsysfs reads per-card AMD VRAM and utilization straight from the
// amdgpu driver's sysfs nodes — no external binary, so it works in the
// worker image as-is where rocm-smi is absent.
//
// Card discovery mirrors capabilities.enumerateROCmFromDRI exactly (numeric
// cardN directories, vendor 0x1002, in ReadDir order, ordinal assigned only
// to AMD cards) so Index stays aligned with the heartbeat's accelerator
// rows. A card whose driver exposes none of the mem_info nodes (radeon
// driver instead of amdgpu) yields nothing rather than a fake zero, and a
// box with no /sys/class/drm at all (WSL2) returns an empty list — the
// caller then keeps its previous sampling behaviour.
func sampleAMDsysfs(root string) []GPUCard {
	entries, err := os.ReadDir(filepath.Join(root, "class", "drm"))
	if err != nil {
		return nil
	}
	var cards []GPUCard
	idx := 0
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, "card") || strings.Contains(name, "-") {
			continue
		}
		devDir := filepath.Join(root, "class", "drm", name, "device")
		vendorData, err := os.ReadFile(filepath.Join(devDir, "vendor"))
		if err != nil {
			continue
		}
		if strings.TrimSpace(string(vendorData)) != "0x1002" { // AMD
			continue
		}
		total, ok := readSysfsInt64(filepath.Join(devDir, "mem_info_vram_total"))
		if !ok {
			continue // amdgpu mem_info absent (e.g. radeon driver)
		}
		used, _ := readSysfsInt64(filepath.Join(devDir, "mem_info_vram_used"))
		util, _ := readSysfsFloat(filepath.Join(devDir, "gpu_busy_percent"))
		cards = append(cards, GPUCard{
			Index:       idx,
			PCIBusID:    amdCardPCIBusID(devDir),
			VRAMTotalMB: bytesToMB(total),
			VRAMUsedMB:  bytesToMB(used),
			UtilPct:     util,
		})
		idx++
	}
	return cards
}

func readSysfsInt64(path string) (int64, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	v, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

func readSysfsFloat(path string) (float64, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(string(data)), 64)
	if err != nil || math.IsNaN(v) || v < 0 {
		return 0, false
	}
	return v, true
}

// amdCardPCIBusID returns the base of the card's device symlink (e.g.
// "0000:0a:00.0") — the PCI address §14.3 card pinning correlates on — or
// "" when the symlink is absent (as in test trees without one).
func amdCardPCIBusID(devDir string) string {
	target, err := os.Readlink(devDir)
	if err != nil {
		return ""
	}
	return filepath.Base(target)
}

func bytesToMB(b int64) float64 {
	return float64(b) / (1024.0 * 1024.0)
}

// sampleAMDSysfsSnapshot aggregates the per-card sysfs reads into the
// box-wide GPUSnapshot the heartbeat sends: VRAM summed across cards,
// utilization taken from the busiest card (the bottleneck is the box's
// load), free clamped at zero. Returns an empty snapshot (no Type) when no
// card is measurable — the caller must keep its previous behaviour then.
func sampleAMDSysfsSnapshot() GPUSnapshot {
	cards := sampleAMDsysfs(amdSysfsRoot)
	if len(cards) == 0 {
		return GPUSnapshot{}
	}
	snap := GPUSnapshot{Type: "ROCm", Cards: cards}
	for _, c := range cards {
		snap.VRAMTotalMB += c.VRAMTotalMB
		snap.VRAMUsedMB += c.VRAMUsedMB
		if c.UtilPct > snap.UtilizationPct {
			snap.UtilizationPct = c.UtilPct
		}
	}
	snap.VRAMFreeMB = snap.VRAMTotalMB - snap.VRAMUsedMB
	if snap.VRAMFreeMB < 0 {
		snap.VRAMFreeMB = 0
	}
	if snap.UtilizationPct > 0 {
		snap.PowerState = "active"
	} else {
		snap.PowerState = "idle"
	}
	return snap
}
