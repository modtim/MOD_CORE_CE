//go:build linux || darwin

package main

import (
	"syscall"
)

// MAC.1.s13b (launch-completion-megaplan design 4.6): the unix half of the
// pre-claim free-disk probe. syscall.Statfs only exists on linux/darwin, so
// it lives here behind a build tag; diskfloor.go keeps the mockable seam
// (diskStatfs) and the gate logic, and diskfloor_windows.go carries the
// Windows reading. Same per-OS split as noah_native_*/tray_*/service_*.
//
// diskStatfsOS returns free bytes on the filesystem holding path, ok=false
// when the probe fails (the gate then fails open).

func diskStatfsOS(path string) (int64, bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, false
	}
	return int64(st.Bavail) * int64(st.Bsize), true
}
