package monitor

import (
	"os"
	"path/filepath"
	"testing"
)

// The AMD heartbeat path used to be rocm-smi based, so a worker image
// without that binary reported 0/0 VRAM for ROCm boxes and left placement
// guards blind. These tests pin the sysfs sampler against a fake tree:
// values in the MB units the heartbeat expects, card identity aligned with
// the rocm accelerator rows, and fail-soft behaviour on missing nodes.

// writeSysfsCard builds root/class/drm/card with a device symlink whose base
// is the PCI bus id, plus the named files, under a backing dir in
// root/pci/<pci> — the real-sysfs shape.
func writeSysfsCard(t *testing.T, root, card, pci, vendor string, files map[string]string) {
	t.Helper()
	cardDir := filepath.Join(root, "class", "drm", card)
	if err := os.MkdirAll(cardDir, 0o755); err != nil {
		t.Fatal(err)
	}
	realDir := filepath.Join(root, "pci", pci)
	if err := os.MkdirAll(realDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(realDir, "vendor"), []byte(vendor+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for name, val := range files {
		if err := os.WriteFile(filepath.Join(realDir, name), []byte(val), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(realDir, filepath.Join(cardDir, "device")); err != nil {
		t.Fatal(err)
	}
}

func twoAMDCards(t *testing.T, root string) {
	// 32 GiB cards: 34359738368 bytes total; card0 holds 3 GiB (3221225472).
	writeSysfsCard(t, root, "card0", "0000:0a:00.0", "0x1002", map[string]string{
		"mem_info_vram_total":  "34359738368",
		"mem_info_vram_used":   "3221225472",
		"gpu_busy_percent":     "42",
	})
	writeSysfsCard(t, root, "card2", "0001:08:00.0", "0x1002", map[string]string{
		"mem_info_vram_total":  "34359738368",
		"mem_info_vram_used":   "0",
		"gpu_busy_percent":     "0",
	})
}

func TestSampleAMDCards_TwoCardsParsedInMBUnits(t *testing.T) {
	root := t.TempDir()
	twoAMDCards(t, root)
	// An Intel card and an output-node entry must both be skipped, and the
	// Intel card must not consume a rocm ordinal.
	writeSysfsCard(t, root, "card1", "0000:02:00.0", "0x8086", map[string]string{
		"mem_info_vram_total": "17179869184",
	})
	if err := os.MkdirAll(filepath.Join(root, "class", "drm", "card0-HDMI1"), 0o755); err != nil {
		t.Fatal(err)
	}

	cards := sampleAMDsysfs(root)
	if len(cards) != 2 {
		t.Fatalf("got %d cards, want 2 (intel + HDMI output skipped)", len(cards))
	}
	c0, c1 := cards[0], cards[1]
	if c0.Index != 0 || c1.Index != 1 {
		t.Fatalf("indices %d,%d — want 0,1 (must match rocm accelerator rows)", c0.Index, c1.Index)
	}
	if c0.PCIBusID != "0000:0a:00.0" || c1.PCIBusID != "0001:08:00.0" {
		t.Fatalf("pci ids %q,%q — want the device-symlink bases", c0.PCIBusID, c1.PCIBusID)
	}
	if c0.VRAMTotalMB != 32768 || c0.VRAMUsedMB != 3072 || c0.UtilPct != 42 {
		t.Fatalf("card0 = total %.0f used %.0f util %v — want 32768/3072/42 MB", c0.VRAMTotalMB, c0.VRAMUsedMB, c0.UtilPct)
	}
	if c1.VRAMTotalMB != 32768 || c1.VRAMUsedMB != 0 || c1.UtilPct != 0 {
		t.Fatalf("card1 = total %.0f used %.0f util %v — want 32768/0/0", c1.VRAMTotalMB, c1.VRAMUsedMB, c1.UtilPct)
	}
}

func TestSampleAMDCards_MissingMemInfoYieldsNoCard(t *testing.T) {
	root := t.TempDir()
	// Vendor is AMD but the driver exposes none of the amdgpu mem nodes
	// (e.g. radeon driver): skip, never report a fake zero card.
	writeSysfsCard(t, root, "card0", "0000:0a:00.0", "0x1002", nil)
	if cards := sampleAMDsysfs(root); len(cards) != 0 {
		t.Fatalf("got %d cards, want none without mem_info_vram_total", len(cards))
	}
}

func TestSampleAMDCards_NoDRMDirFailsSoft(t *testing.T) {
	// WSL2 has no /sys/class/drm: empty result, no panic.
	if cards := sampleAMDsysfs(t.TempDir()); cards != nil {
		t.Fatalf("got %v, want nil for a tree without class/drm", cards)
	}
}

func TestSampleAMDCards_NonAMDVendorsSkipped(t *testing.T) {
	root := t.TempDir()
	writeSysfsCard(t, root, "card0", "0000:02:00.0", "0x8086", map[string]string{
		"mem_info_vram_total": "17179869184",
		"mem_info_vram_used":  "104857600",
		"gpu_busy_percent":    "7",
	})
	if cards := sampleAMDsysfs(root); len(cards) != 0 {
		t.Fatalf("got %d cards, want 0 — only amdgpu cards are sampled", len(cards))
	}
}

func TestSampleAMDCards_MalformedValuesNoPanic(t *testing.T) {
	root := t.TempDir()
	writeSysfsCard(t, root, "card0", "0000:0a:00.0", "0x1002", map[string]string{
		"mem_info_vram_total": "not-a-number",
	})
	writeSysfsCard(t, root, "card1", "0001:08:00.0", "0x1002", map[string]string{
		"mem_info_vram_total": "34359738368",
		"gpu_busy_percent":    "NaN",
	})
	cards := sampleAMDsysfs(root)
	if len(cards) != 1 {
		t.Fatalf("got %d cards, want 1 (unparseable total skips the card)", len(cards))
	}
	if cards[0].UtilPct != 0 {
		t.Fatalf("garbage busy value reported util %v, want 0", cards[0].UtilPct)
	}
}

func withFakeSysfs(t *testing.T, root string) {
	t.Helper()
	prev := amdSysfsRoot
	amdSysfsRoot = root
	t.Cleanup(func() { amdSysfsRoot = prev })
}

func TestSampleAMDSysfsSnapshot_AggregatesTwoCards(t *testing.T) {
	root := t.TempDir()
	twoAMDCards(t, root)
	withFakeSysfs(t, root)

	snap := sampleAMDSysfsSnapshot()
	if snap.Type != "ROCm" || len(snap.Cards) != 2 {
		t.Fatalf("type %q with %d cards, want ROCm with 2", snap.Type, len(snap.Cards))
	}
	// The heartbeat reports box-wide MB figures: both 32 GiB cards summed.
	if snap.VRAMTotalMB != 65536 || snap.VRAMUsedMB != 3072 || snap.VRAMFreeMB != 62464 {
		t.Fatalf("total %.0f used %.0f free %.0f MB — want 65536/3072/62464",
			snap.VRAMTotalMB, snap.VRAMUsedMB, snap.VRAMFreeMB)
	}
	if snap.UtilizationPct != 42 {
		t.Fatalf("utilization %v — want the busiest card (42)", snap.UtilizationPct)
	}
	if snap.PowerState != "active" {
		t.Fatalf("power state %q, want active while a card is busy", snap.PowerState)
	}
}

func TestSampleAMDSysfsSnapshot_IdleWhenNothingBusy(t *testing.T) {
	root := t.TempDir()
	writeSysfsCard(t, root, "card0", "0000:0a:00.0", "0x1002", map[string]string{
		"mem_info_vram_total":  "34359738368",
		"mem_info_vram_used":   "0",
		"gpu_busy_percent":     "0",
	})
	withFakeSysfs(t, root)

	snap := sampleAMDSysfsSnapshot()
	if snap.PowerState != "idle" {
		t.Fatalf("power state %q, want idle", snap.PowerState)
	}
}

func TestSampleAMDSysfsSnapshot_EmptyWhenNoAMDCards(t *testing.T) {
	withFakeSysfs(t, t.TempDir())
	if snap := sampleAMDSysfsSnapshot(); snap.Type != "" {
		t.Fatalf("type %q, want empty snapshot when no amdgpu card is measurable", snap.Type)
	}
}

func TestSampleGPU_UnprofiledBoxStillFindsSysfsAMDCards(t *testing.T) {
	root := t.TempDir()
	twoAMDCards(t, root)
	withFakeSysfs(t, root)

	snap := SampleGPU("") // no GPU type profiled at boot
	if snap.Type != "ROCm" || snap.VRAMTotalMB != 65536 {
		t.Fatalf("got %q total %.0f — want ROCm 65536 MB from sysfs alone", snap.Type, snap.VRAMTotalMB)
	}

	withFakeSysfs(t, t.TempDir()) // no amdgpu cards anywhere
	if snap := SampleGPU(""); snap.Type != "none" {
		t.Fatalf("got %q, want none with no amdgpu sysfs (WSL fail-soft)", snap.Type)
	}
}

func TestSampleAMD_PrefersSysfsAndFallsBackToRocmSMI(t *testing.T) {
	root := t.TempDir()
	twoAMDCards(t, root)
	withFakeSysfs(t, root)

	snap := SampleGPU("ROCm")
	if snap.Type != "ROCm" || len(snap.Cards) != 2 || snap.VRAMUsedMB != 3072 {
		t.Fatalf("got type %q cards %d used %.0f — want sysfs-backed ROCm sample",
			snap.Type, len(snap.Cards), snap.VRAMUsedMB)
	}

	// No amdgpu sysfs (WSL2): keep the previous rocm-smi-based behaviour —
	// a ROCm-typed snapshot, whatever the tool can say.
	withFakeSysfs(t, t.TempDir())
	if snap := SampleGPU("ROCm"); snap.Type != "ROCm" {
		t.Fatalf("got type %q, want ROCm kept on the rocm-smi fallback", snap.Type)
	}
}

func TestFormatResourceSnapshot_IncludesPerCardValues(t *testing.T) {
	root := t.TempDir()
	twoAMDCards(t, root)
	withFakeSysfs(t, root)

	gpu := SampleGPU("ROCm")
	snap := FormatResourceSnapshot(SystemSnapshot{}, gpu, nil, 0, 0)

	cards, ok := snap["gpu_cards"].([]GPUCard)
	if !ok || len(cards) != 2 {
		t.Fatalf("gpu_cards missing or wrong shape: %#v", snap["gpu_cards"])
	}
	// Card identity lines up with service_cards: id is str(index).
	if cards[0].Index != 0 || cards[1].Index != 1 {
		t.Fatalf("card indices %d,%d — want 0,1", cards[0].Index, cards[1].Index)
	}
	if snap["vram_total_mb"] != float64(65536) {
		t.Fatalf("box-wide vram_total_mb %v, want 65536", snap["vram_total_mb"])
	}
}

func TestFormatResourceSnapshot_OmitsPerCardWhenAbsent(t *testing.T) {
	withFakeSysfs(t, t.TempDir()) // no amdgpu sysfs → no per-card data
	gpu := SampleGPU("ROCm")      // rocm-smi fallback path: no Cards
	snap := FormatResourceSnapshot(SystemSnapshot{}, gpu, nil, 0, 0)
	if _, present := snap["gpu_cards"]; present {
		t.Fatal("gpu_cards present without per-card measurements")
	}
}
