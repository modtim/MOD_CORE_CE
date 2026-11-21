package executor

import (
	"runtime"
	"testing"
)

// TestVMStatAvailableBytes pins the pure darwin headroom parse on any host: the
// real macOS row puts the count before the label, captured fixtures put it
// after, and the page size may be named in the trailer.
func TestVMStatAvailableBytes(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want int64
	}{
		{
			name: "real macOS spelling, default page size",
			raw:  "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free:                             63045.\nPages available:                        100000.\n",
			want: (63045 + 100000) * 4096,
		},
		{
			name: "fixture spelling with a named page size",
			raw:  "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nFree pages: 100\nPages available: 50\n",
			want: 150 * 16384,
		},
		{
			name: "no headroom is zero, not a guess",
			raw:  "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 0.\nPages available: 0.\nPages inactive: 999999.\n",
			want: 0,
		},
		{name: "empty reading", raw: "", want: 0},
		{
			name: "no page-size trailer falls back to 4096",
			raw:  "Free pages: 10\n",
			want: 10 * 4096,
		},
		{
			name: "file cache and inactive pages are deliberately excluded",
			raw:  "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages active: 500000.\nPages inactive: 500000.\nPages free: 10.\nPages available: 20.\n",
			want: 30 * 4096,
		},
	}
	for _, c := range cases {
		if got := vmStatAvailableBytes(c.raw); got != c.want {
			t.Errorf("%s: vmStatAvailableBytes = %d, want %d", c.name, got, c.want)
		}
	}
}

// TestDarwinPickMemory pins the darwin selection: vm_stat headroom when the
// probe worked, the hw.memsize pool when it did not, and 0 (the safe fallback
// the caller reads as "not measured") when both failed.
func TestDarwinPickMemory(t *testing.T) {
	cases := []struct {
		total, available, want int64
	}{
		{17179869184, 4294967296, 4294967296}, // headroom wins over the pool
		{17179869184, 0, 17179869184},         // vm_stat probe failed -> pool
		{0, 0, 0},                             // nothing measured -> safe fallback
	}
	for _, c := range cases {
		if got := darwinPickMemory(c.total, c.available); got != c.want {
			t.Errorf("darwinPickMemory(%d, %d) = %d, want %d", c.total, c.available, got, c.want)
		}
	}
}

// TestHostMemoryBytesPerOS pins the runtime.GOOS switch on the host running the
// test: Linux still reads /proc/meminfo, darwin must have its probe wired, and
// any other OS must return 0 so the caller uses its fixed default rather than
// guessing.
func TestHostMemoryBytesPerOS(t *testing.T) {
	switch runtime.GOOS {
	case "linux":
		if got := hostMemoryBytes(); got != hostMemoryLinux() {
			t.Fatalf("hostMemoryBytes on linux = %d, want hostMemoryLinux() %d", got, hostMemoryLinux())
		}
	case "darwin":
		if darwinMemoryProbe == nil {
			t.Fatal("darwin probe not wired: hostMemoryBytes would return 0 on a Mac worker")
		}
		total, available := darwinMemoryProbe()
		if got := hostMemoryBytes(); got != darwinPickMemory(total, available) {
			t.Fatalf("hostMemoryBytes on darwin = %d, want %d", got, darwinPickMemory(total, available))
		}
	default:
		if darwinMemoryProbe != nil {
			t.Fatalf("darwin probe wired on %s", runtime.GOOS)
		}
		if got := hostMemoryBytes(); got != 0 {
			t.Fatalf("hostMemoryBytes on %s = %d, want 0 (safe fallback)", runtime.GOOS, got)
		}
	}
}
