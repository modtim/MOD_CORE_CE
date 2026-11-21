//go:build linux

package monitor

import "syscall"

func statfsImpl(path string, buf *syscallStatfs) error {
	var sfs syscall.Statfs_t
	if err := syscall.Statfs(path, &sfs); err != nil {
		return err
	}
	buf.Bsize = sfs.Bsize
	buf.Bavail = sfs.Bavail
	buf.Blocks = sfs.Blocks
	return nil
}
