//go:build darwin

package main

import (
	"context"
	"os/exec"
	"time"
	"golang.org/x/sys/unix"
)

// sz-mac-worker.3 (launch-completion-megaplan): the macOS half of the
// preemption controller's swap probe. A native Mac worker has no /proc/meminfo,
// so the Linux reader in preempt_linux.go would break at runtime here.
//
// macOS never runs out of backing memory the way a Linux box without a swap
// partition does: it grows anonymous swap on demand, so pausing a container can
// always hand its pages back to the OS. We report swap present when the machine
// reports real memory (hw.memsize) AND the VM reports addressable pages (vm_stat)
// — the honest "held RAM is not pinned" answer the controller needs. If either
// probe fails we return false, which makes the controller evict instead of pause:
// the safe direction (never claim headroom we did not measure).
//
// hostMemTotalBytes() is the darwin equivalent of the Linux MemTotal read:
// hw.memsize in bytes, via unix.SysctlUint64 (golang.org/x/sys is already pinned
// in go.mod). hostMemAvailableBytes() is the conservative headroom estimate the
// budget math wants: (free + available) pages from vm_stat x the page size that
// reading names. The file cache and inactive-clean pages are deliberately left
// out so we never over-report reclaimable headroom.
//
// The vm_stat parsing itself lives in preempt.go (vmStatAvailBytes): pure and
// untagged, so Linux CI can pin the macOS logic against captured fixtures.

func swapPresent() bool {
	return hostMemTotalBytes() > 0 && hostMemAvailableBytes() > 0
}

// hostMemTotalBytes reads hw.memsize (bytes) straight from the sysctl MIB.
func hostMemTotalBytes() uint64 {
 v, err := unix.SysctlUint64("hw.memsize")
 if err != nil {
  return 0
 }
 return v
}

// darwinSwapProbeTimeout bounds the vm_stat probe: a hung probe must never stall
// worker start-up (the controller then just sees no headroom and evicts).
var darwinSwapProbeTimeout = 3 * time.Second

// hostMemAvailableBytes runs `vm_stat` under that timeout and hands the reading
// to the pure parser in preempt.go.
func hostMemAvailableBytes() uint64 {
 ctx, cancel := context.WithTimeout(context.Background(), darwinSwapProbeTimeout)
 defer cancel()
 out, err := exec.CommandContext(ctx, "vm_stat").Output()
 if err != nil {
  return 0
 }
 return vmStatAvailBytes(string(out))
}
