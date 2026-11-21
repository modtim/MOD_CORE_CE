//go:build linux

package executor

import "syscall"

// statfsUsedPct returns percent-used for the filesystem holding path.
// used% = 100 * (Blocks - Bavail) / Blocks. Bavail (blocks available to
// unprivileged users) is used for "free" to match monitor.readDiskFree —
// the conservative choice that counts reserved blocks as used.
func statfsUsedPct(path string) (int, bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, false
	}
	if st.Blocks == 0 {
		return 0, false
	}
	used := st.Blocks - st.Bavail
	return int(used * 100 / st.Blocks), true
}

// getDiskSpace returns total and available bytes for the filesystem at path.
func getDiskSpace(path string) (total, available uint64, ok bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, 0, false
	}
	total = st.Blocks * uint64(st.Bsize)
	available = st.Bavail * uint64(st.Bsize)
	return total, available, true
}
