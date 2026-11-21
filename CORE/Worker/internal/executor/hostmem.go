package executor

import (
	"os"
	"strconv"
	"strings"

	goruntime "runtime"
)

// Host-memory helpers split out of executor.go (mac-worker.3c): hostMemoryBytes and its
// per-OS readers. hostMemoryLinux is HEAD's /proc/meminfo reading byte-for-byte; the
// darwin probe itself lives in hostmem_darwin.go (darwin-only, because x/sys/unix's sysctl
// helpers are darwin-only at compile time) and is wired in through darwinMemoryProbe.

// hostMemoryBytes reports the host's memory in bytes, or 0 when it cannot be
// determined, so callers fall back to a fixed default rather than guessing.
// Linux reads MemTotal from /proc/meminfo (unchanged); darwin reads hw.memsize
// and prefers vm_stat's conservative headroom; any other OS returns 0.
func hostMemoryBytes() int64 {
	switch goruntime.GOOS {
	case "linux":
		return hostMemoryLinux()
	case "darwin":
		if darwinMemoryProbe == nil {
			return 0
		}
		total, available := darwinMemoryProbe()
		return darwinPickMemory(total, available)
	default:
		return 0
	}
}

// darwinPickMemory prefers vm_stat's reclaimable headroom; when that probe
// failed it falls back to the hw.memsize pool. Pure so the darwin choice is
// testable on any host.
func darwinPickMemory(total, available int64) int64 {
	if available > 0 {
		return available
	}
	return total
}

// darwinMemoryProbe is the macOS reading, installed by hostmem_darwin.go. It is
// nil off darwin because x/sys/unix's sysctl helpers are darwin-only at compile
// time, so the switch above falls back to 0 rather than guessing.
var darwinMemoryProbe func() (total, available int64)

// hostMemoryLinux reads MemTotal from /proc/meminfo. Returns 0 when it cannot
// be determined, so callers fall back to a fixed default rather than guessing.
func hostMemoryLinux() int64 {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "MemTotal:") {
			continue
		}
		fields := strings.Fields(line) // "MemTotal: 16384000 kB"
		if len(fields) < 2 {
			return 0
		}
		kb, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil {
			return 0
		}
		return kb * 1024
	}
	return 0
}

// vmStatDefaultPageSizeBytes is the page size used when a vm_stat reading does
// not name one (Apple Silicon is 4096; overridable in tests).
var vmStatDefaultPageSizeBytes = 4096

// vmStatAvailableBytes turns a `vm_stat` reading into a conservative headroom
// estimate for darwin: (free + available) pages x the page size the reading
// names. The file cache and inactive-clean pages are deliberately left out so
// the worker never claims headroom it cannot reclaim. Pure (no syscalls, no
// /proc) so the macOS logic is testable on any host.
func vmStatAvailableBytes(raw string) int64 {
	pageSize := int64(vmStatDefaultPageSizeBytes)
	var free, available int64
	for _, line := range strings.Split(raw, "\n") {
		lower := strings.ToLower(line)
		if strings.Contains(lower, "page size") {
			if v := vmStatFirstInt(strings.Fields(line)); v > 0 {
				pageSize = v
			}
			continue
		}
		if strings.Contains(lower, "free pages") || strings.Contains(lower, "pages free") {
			free = vmStatFirstInt(strings.Fields(line))
			continue
		}
		if strings.Contains(lower, "available pages") || strings.Contains(lower, "pages available") {
			available = vmStatFirstInt(strings.Fields(line))
		}
	}
	if free+available <= 0 || pageSize <= 0 {
		return 0
	}
	return (free + available) * pageSize
}

// vmStatFirstInt reads the first number in a token list ("63045." -> 63045), 0
// when there is none. It accepts both vm_stat spellings: the real macOS row puts
// the count BEFORE the label ("63045 free pages."), captured fixtures put it
// after ("Free pages: 63045").
func vmStatFirstInt(fields []string) int64 {
	for _, tok := range fields {
		v, err := strconv.ParseInt(strings.Trim(strings.TrimRight(tok, "."), ","), 10, 64)
		if err == nil {
			return v
		}
	}
	return 0
}
