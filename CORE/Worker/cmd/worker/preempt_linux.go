//go:build linux

package main

import (
	"os"
	"strings"
)

// sz-mac-worker.3 (launch-completion-megaplan): the Linux half of the
// preemption controller's swap probe. /proc/meminfo only exists on Linux, so
// the read lives here behind a filename build tag; preempt.go keeps the
// platform-neutral helpers (swapTotalKB) and the controller, and
// preempt_darwin.go / preempt_other.go carry the macOS and everything-else
// halves. Every platform file exports the same name and signature:
// func swapPresent() bool.

// swapPresent reports whether the machine has swap. Without swap, held RAM is
// effectively pinned, so pausing cannot protect the human and we evict instead.
func swapPresent() bool {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "SwapTotal:") {
			return swapTotalKB(line) > 0
		}
	}
	return false
}
