package monitor

import "testing"

// sz-mac-worker.3b: computeDarwinMemInfo is the pure glue between the
// already-tested sysctl/vm_stat parsers (powermetrics_parse_test.go) and the
// readMemInfo() shape every platform returns. Exercised on any GOOS — it
// takes raw strings, not a live macOS probe.
func TestComputeDarwinMemInfo_UsesUnifiedPoolAndResidentUsage(t *testing.T) {
	vmRaw := fixture(t, "testdata/vm_stat.txt")
	// 16 GiB unified pool.
	totalMB, availableMB, usedMB, swapUsedMB := computeDarwinMemInfo("17179869184\n", vmRaw)

	if totalMB != 16384 {
		t.Fatalf("totalMB = %.0f, want 16384", totalMB)
	}
	wantUsed := parseVMStat(vmRaw).UsedMB
	if usedMB != wantUsed {
		t.Fatalf("usedMB = %.0f, want %.0f (vm_stat wired+active+dirty)", usedMB, wantUsed)
	}
	if availableMB != totalMB-usedMB {
		t.Fatalf("availableMB = %.0f, want totalMB-usedMB = %.0f", availableMB, totalMB-usedMB)
	}
	if availableMB < 0 {
		t.Fatal("availableMB went negative")
	}
	// vm_stat's swap counters are cumulative event counts, not a current
	// swap-used size; computeDarwinMemInfo must not fabricate one.
	if swapUsedMB != 0 {
		t.Fatalf("swapUsedMB = %.0f, want 0", swapUsedMB)
	}
}

func TestComputeDarwinMemInfo_NoMemsizeLeavesAvailableZero(t *testing.T) {
	totalMB, availableMB, usedMB, _ := computeDarwinMemInfo("", "Wired pages: 1024\n")
	if totalMB != 0 {
		t.Fatalf("totalMB = %.0f, want 0 when sysctl probe failed", totalMB)
	}
	if availableMB != 0 {
		t.Fatalf("availableMB = %.0f, want 0 (can't subtract from an unknown pool)", availableMB)
	}
	if usedMB <= 0 {
		t.Fatal("usedMB should still come from vm_stat even without a pool size")
	}
}
