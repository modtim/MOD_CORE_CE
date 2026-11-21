//go:build windows

package executor

import "os"

// deviceGID extracts the group ID from a device node's stat struct.
// Windows does not use Unix-style group IDs, so this always returns (0, false).
func deviceGID(info os.FileInfo) (uint32, bool) {
	return 0, false
}
