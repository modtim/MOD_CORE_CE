//go:build linux || darwin

package cleanup

import (
	"syscall"
)

// fsStat returns (total, used) bytes for the partition holding path.
// ok=false when statfs is unavailable for path.
func fsStat(path string) (int64, int64, bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		// Retry on "/" in case the path is gone mid-call (data root moved).
		if err = syscall.Statfs("/", &st); err != nil {
			return 0, 0, false
		}
	}
	total := int64(st.Blocks) * int64(st.Bsize)
	free := int64(st.Bfree) * int64(st.Bsize)
	used := total - free
	if used < 0 {
		used = 0
	}
	return total, used, true
}


