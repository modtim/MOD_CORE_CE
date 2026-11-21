//go:build darwin

package executor

import (
	"context"
	"os/exec"
	"time"

	"golang.org/x/sys/unix"
)

// darwinMemProbeTimeout bounds the macOS vm_stat probe: a hung probe must never
// stall the memory cap, so a timeout is read as "not measured" (0) and the
// caller falls back to the fixed default.
var darwinMemProbeTimeout = 3 * time.Second

// darwinMemoryProbe is the darwin half of hostMemoryBytes: hw.memsize is the ONE
// unified pool (total), and vm_stat's free+available pages say how much of it is
// actually reclaimable (available). Both are best-effort — a failed probe
// returns 0 rather than guessing. It is wired through the package-level var so
// the untagged switch in executor.go still compiles for linux/windows, where
// x/sys/unix's sysctl helpers do not exist.
func init() { darwinMemoryProbe = darwinHostMemory }

func darwinHostMemory() (total, available int64) {
	if v, err := unix.SysctlUint64("hw.memsize"); err == nil {
		total = int64(v)
	}
	ctx, cancel := context.WithTimeout(context.Background(), darwinMemProbeTimeout)
	defer cancel()
	out, err := exec.CommandContext(ctx, "vm_stat").Output()
	if err != nil {
		return total, 0
	}
	return total, vmStatAvailableBytes(string(out))
}
