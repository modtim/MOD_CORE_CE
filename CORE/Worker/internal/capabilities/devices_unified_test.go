package capabilities

import (
	"math"
	"os"
	"testing"
)

// Unified-memory GPUs (NVIDIA GX10 / DGX Spark: Grace CPU + Blackwell GPU on
// one 121 GB pool) report nvidia-smi memory.total as "[N/A]"/"Not Supported".
// The card must be sized from the system RAM pool instead of being left at 0 —
// an unsized card is refused by the API's claim-time fit guard and never
// profiled, which is exactly the "VRAM: Unknown / Profiling deadline exceeded"
// failure a GX10 worker showed.

// gb10NvidiaSMI is captured `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`
// output from a DGX Spark class box: one GX10, memory.total not supported.
const gb10NvidiaSMI = "0, GPU-1d93e2b1-8e95-3d83-bf11-4d0c2f9b34a1, NVIDIA GX10, [N/A], 0018:01:00.0, 580.95.05\n"

func withNvidiaStub(t *testing.T, out string, unifiedGB float64) {
	t.Helper()
	oldRun := nvidiaSMIRunner
	oldUni := nvidiaUnifiedMemoryGB
	t.Cleanup(func() { nvidiaSMIRunner, nvidiaUnifiedMemoryGB = oldRun, oldUni })
	nvidiaSMIRunner = func() ([]byte, error) { return []byte(out), nil }
	nvidiaUnifiedMemoryGB = func() float64 { return unifiedGB }
}

func TestEnumerateNVIDIA_UnifiedMemoryFallsBackToSystemRAM(t *testing.T) {
	withNvidiaStub(t, gb10NvidiaSMI, 121.0)
	devs := enumerateNVIDIA()
	if len(devs) != 1 {
		t.Fatalf("enumerateNVIDIA() = %d devices, want 1", len(devs))
	}
	d := devs[0]
	if d.Name != "NVIDIA GX10" {
		t.Errorf("Name = %q, want NVIDIA GX10", d.Name)
	}
	if d.VRAMGB != 121.0 {
		t.Errorf("VRAMGB = %v, want 121 (unified-memory fallback to system RAM)", d.VRAMGB)
	}
	if d.PCIBusID != "0018:01:00.0" || d.DriverVersion != "580.95.05" || d.UUID == "" {
		t.Errorf("device fields not parsed: %+v", d)
	}
}

func TestEnumerateNVIDIA_UnifiedMemoryNotSupportedSpelling(t *testing.T) {
	out := "0, GPU-aaaa, NVIDIA GX10, Not Supported, 0000:01:00.0, 580.95.05\n"
	withNvidiaStub(t, out, 128.5)
	devs := enumerateNVIDIA()
	if len(devs) != 1 || devs[0].VRAMGB != 128.5 {
		t.Fatalf("Not Supported spelling: got %+v, want VRAMGB 128.5", devs)
	}
}

func TestEnumerateNVIDIA_DedicatedVRAMIsKept(t *testing.T) {
	// A normal dGPU reports a real memory.total — the fallback must NOT fire,
	// and the (much larger in other shapes) system RAM figure must not win.
	out := "0, GPU-bbbb, NVIDIA A100 80GB, 81920, 0000:41:00.0, 535.104.05\n"
	withNvidiaStub(t, out, 512.0)
	devs := enumerateNVIDIA()
	if len(devs) != 1 || devs[0].VRAMGB != 80.0 {
		t.Fatalf("dedicated VRAM: got %+v, want VRAMGB 80 (81920 MiB)", devs)
	}
}

func TestEnumerateNVIDIA_NoUnifiedFigureAvailableStaysZero(t *testing.T) {
	// No memory figure AND no readable /proc/meminfo (returns 0): the card stays
	// unsized, preserving the legacy behaviour rather than inventing a number.
	withNvidiaStub(t, gb10NvidiaSMI, 0)
	devs := enumerateNVIDIA()
	if len(devs) != 1 || devs[0].VRAMGB != 0 {
		t.Fatalf("no RAM figure: got %+v, want VRAMGB 0", devs)
	}
}

func TestReadMemTotalGB_ParsesMeminfo(t *testing.T) {
	f := t.TempDir() + "/meminfo"
	if err := os.WriteFile(f, []byte("MemTotal:       134217728 kB\nMemFree:      100000 kB\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// 134217728 kB / 1024 / 1024 = 128 GB exactly.
	if got := readMemTotalGB(f); math.Abs(got-128.0) > 1e-9 {
		t.Errorf("readMemTotalGB = %v, want 128.0", got)
	}
	if got := readMemTotalGB(f + "/missing"); got != 0 {
		t.Errorf("missing file = %v, want 0", got)
	}
}
