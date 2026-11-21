//go:build !windows

package executor

import (
	"os"
	"syscall"
)

// deviceGID extracts the group ID from a device node's stat struct.
// Returns (0, false) on non-Unix platforms or when the stat cannot be
// converted to syscall.Stat_t.
func deviceGID(info os.FileInfo) (uint32, bool) {
	if stat, ok := info.Sys().(*syscall.Stat_t); ok {
		return stat.Gid, true
	}
	return 0, false
}
