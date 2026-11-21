package monitor

import "testing"

// SampleGPU reports memory.used, which is the WHOLE CARD. Recording that as a
// task's vram_mb attributes every other tenant's memory to whichever task was
// sampled — and it errs in the damaging direction, OVERSTATING each node's
// demand so a packer refuses to co-schedule work that would comfortably fit.
// These cover the per-PID attribution that replaces it.

func TestSumVRAMRows_OnlyCountsOurPIDs(t *testing.T) {
	raw := "111, 1024\n222, 2048\n333, 512\n"
	got, ok := sumVRAMRows(raw, map[int]bool{111: true, 333: true})
	if !ok {
		t.Fatal("answered=false for a populated nvidia-smi response")
	}
	if got != 1536 {
		t.Fatalf("got %.0f MB, want 1536 (1024+512); 2048 belongs to another container", got)
	}
}

func TestSumVRAMRows_ZeroIsAMeasurementNotAnUnknown(t *testing.T) {
	// The tool answered; our container simply holds no GPU memory. That is a
	// real zero and must be recorded, not fall back to the whole card.
	got, ok := sumVRAMRows("999, 4096\n", map[int]bool{111: true})
	if !ok {
		t.Fatal("answered=false although nvidia-smi returned rows")
	}
	if got != 0 {
		t.Fatalf("got %.0f, want 0", got)
	}
}

func TestSumVRAMRows_EmptyOutputIsUnknown(t *testing.T) {
	// No rows at all means we cannot tell — the caller must fall back rather
	// than record a confident zero.
	if _, ok := sumVRAMRows("", map[int]bool{111: true}); ok {
		t.Fatal("answered=true for empty output; caller would record a false zero")
	}
}

func TestSumVRAMRows_IgnoresGarbageLines(t *testing.T) {
	raw := "# comment\npid, used_memory\n111, 100\nnot,a,number\n\n111, 50\n"
	got, ok := sumVRAMRows(raw, map[int]bool{111: true})
	if !ok || got != 150 {
		t.Fatalf("got %.0f ok=%v, want 150 true", got, ok)
	}
}

func TestSumVRAMRows_ToleratesWhitespaceAndUnits(t *testing.T) {
	raw := "  111 ,  2048  \n"
	got, ok := sumVRAMRows(raw, map[int]bool{111: true})
	if !ok || got != 2048 {
		t.Fatalf("got %.0f ok=%v, want 2048 true", got, ok)
	}
}

func TestContainerVRAM_UnknownContainerIsUnknown(t *testing.T) {
	if _, ok := ContainerVRAMUsedMB("CUDA", ""); ok {
		t.Fatal("claimed a measurement for an empty container id")
	}
	if _, ok := ContainerVRAMUsedMB("CUDA", "does-not-exist-0000"); ok {
		t.Fatal("claimed a measurement for a container with no cgroup")
	}
}

func TestContainerVRAM_UnsupportedVendorIsUnknown(t *testing.T) {
	// Intel and NPU expose no comparable per-process query. Reporting nothing
	// is correct; reporting the whole card as this task's would be a lie.
	if _, ok := ContainerVRAMUsedMB("Intel", "abc123"); ok {
		t.Fatal("claimed per-container VRAM for a vendor that cannot report it")
	}
}
