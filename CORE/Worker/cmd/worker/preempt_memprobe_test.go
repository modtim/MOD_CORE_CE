package main

// sz-mac-worker.3 (launch-completion-megaplan): the darwin memory probes must
// parse without a Mac. The syscall/exec half lives in preempt_darwin.go, but the
// parsing is pure and untagged (vmStatAvailBytes / firstInt / trailingInt /
// swapTotalKB in preempt.go), so Linux CI pins the macOS logic against captured
// vm_stat fixtures. The file is deliberately NOT named preempt_darwin_test.go:
// Go's filename OS inference would hide it from Linux CI.

import (
	"testing"
)

// vmStatReal is the macOS spelling: count BEFORE the label, page size named in
// the trailer.
var vmStatReal = "Statistics for the System-Wide Memory Cache:\n" +
	"Memory Pages In Use: 412345\n" +
	"Memory Pages Free: 12000\n" +
	"Memory Pages Available (Will Be Reclaimed On Demand): 34000\n" +
	"Memory Pages Dirty: 5000\n" +
	"File-Backed Pages: 90000\n" +
	"Non-File-Backed Pages: 50000\n" +
	"page size of 16384 bytes"

// vmStatFixture is the captured-fixture spelling used by internal/monitor
// (label first, count after). Both must answer the same headroom.
var vmStatFixture = "Memory page size: 4096\n" +
	"Wired Pages: 188204\n" +
	"Free Pages: 63045\n" +
	"Available Pages: 1000\n" +
	"Inactive Pages: 4000\n"

func TestVmStatAvailBytesRealMacOS(t *testing.T) {
	 // (12000 free + 34000 available) x 16384 page size.
	 if got := vmStatAvailBytes(vmStatReal); got != uint64(12000+34000) * 16384 {
	  t.Fatalf("real macOS vm_stat reading parsed to %d bytes, want %d",
	   got, uint64(12000+34000) * 16384)
	 }
}

func TestVmStatAvailBytesFixture(t *testing.T) {
	 // (63045 free + 1000 available) x 4096. Inactive and file-backed pages are
	 // excluded on purpose: never claim headroom the OS cannot reclaim.
	 if got := vmStatAvailBytes(vmStatFixture); got != uint64(63045+1000) * 4096 {
	  t.Fatalf("fixture vm_stat reading parsed to %d bytes, want %d",
	   got, uint64(63045+1000) * 4096)
	 }
}

func TestVmStatAvailBytesNoHeadroom(t *testing.T) {
	 // A reading with neither a free nor an available row answers 0, which makes
	 // swapPresent() false and the controller EVICT instead of pause.
	 if got := vmStatAvailBytes("Wired Pages: 1\nInactive Pages: 2\n"); got != 0 {
	  t.Fatalf("vm_stat reading with no headroom parsed to %d, want 0", got)
	 }
	 if got := vmStatAvailBytes(""); got != 0 {
	  t.Fatalf("empty vm_stat reading parsed to %d, want 0", got)
	 }
}

func TestVmStatAvailBytesDefaultPageSize(t *testing.T) {
	 // No page-size trailer: fall back to the Apple-Silicon default rather than
	 // treating the reading as unusable.
	 vmStatDefaultPageSizeBytes = 4096
	 if got := vmStatAvailBytes("Free Pages: 10\n"); got != 10 * 4096 {
	  t.Fatalf("vm_stat reading without a page-size trailer parsed to %d, want %d",
	   got, 10 * 4096)
	 }
	 vmStatDefaultPageSizeBytes = 8192
	 if got := vmStatAvailBytes("Free Pages: 10\n"); got != 10 * 8192 {
	  t.Fatalf("overridden page size ignored: parsed %d, want %d", got, 10 * 8192)
	 }
	 vmStatDefaultPageSizeBytes = 4096
}

func TestSwapTotalKB(t *testing.T) {
	 // The Linux reader's line parser stays shared with the controller's tests.
	 if got := swapTotalKB("SwapTotal:       8388604 kB"); got != 8388604 {
	  t.Fatalf("SwapTotal line parsed to %d, want 8388604", got)
	 }
	 if got := swapTotalKB("SwapTotal: 0 kB"); got != 0 {
	  t.Fatalf("SwapTotal 0 line parsed to %d, want 0", got)
	 }
	 if got := swapTotalKB("SwapTotal:"); got != 0 {
	  t.Fatalf("SwapTotal with no count parsed to %d, want 0", got)
	 }
}

func TestProbeHelpersDoNotPanic(t *testing.T) {
	 // Garbage in, zero out: a probe that could not answer must never make the
	 // controller claim headroom.
	 if got := firstInt([]string{"page", "size", "of", "16384", "bytes"}); got != 16384 {
	  t.Fatalf("firstInt skipped to %d, want 16384", got)
	 }
	 if got := firstInt([]string{}); got != 0 {
	  t.Fatalf("firstInt on empty list parsed to %d, want 0", got)
	 }
	 if got := trailingInt([]string{"page", "size", "of", "16384", "bytes"}); got != 0 {
	  t.Fatalf("trailingInt on a non-numeric tail parsed to %d, want 0", got)
	 }
	 if got := trailingInt([]string{"page", "size", "of", "16384"}); got != 16384 {
	  t.Fatalf("trailingInt on the page-size trailer parsed to %d, want 16384", got)
	 }
}
