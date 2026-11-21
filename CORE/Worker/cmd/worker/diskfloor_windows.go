//go:build windows

package main

import (
	"golang.org/x/sys/windows"
)

// MAC.1.s13b (launch-completion-megaplan design 4.6): the Windows half of
// the pre-claim free-disk probe. syscall.Statfs does not exist here, so the
// reading comes from GetDiskFreeSpaceEx (golang.org/x/sys/windows, already
// pinned in go.mod for the svc/tray/NOAH halves). freeBytesAvailableToCaller
// is the right figure for the worker: it is the quota the process itself
// gets, which is what a job's scratch writes actually contend with.
//
// diskStatfsOS returns free bytes on the volume holding path, ok=false when
// the probe fails — the gate in diskfloor.go then fails open, exactly as it
// does on a platform with no probe at all.

// diskFreeMax is the largest int64 — the saturation point for the free-byte
// reading (no sane floor is anywhere near it, so a bigger volume is just
// "way above the floor").
const diskFreeMax = int64(9223372036854775807)

func diskStatfsOS(path string) (int64, bool) {
	// GetDiskFreeSpaceExW wants a UTF-16 path with a terminating NUL;
	// windows.UTF16FromString adds it and rejects embedded NULs.
	name, err := windows.UTF16FromString(path)
	if err != nil {
		return 0, false
	}
	var free, total, freeTotal uint64
	// &name[0] is the buffer head; UTF16FromString already appended the NUL.
	if err := windows.GetDiskFreeSpaceEx(&name[0], &free, &total, &freeTotal); err != nil {
	 return 0, false
	}
	if free > uint64(diskFreeMax) {
	 // Saturate: a volume larger than int64 still clears any sane floor.
	 return diskFreeMax, true
	}
	return int64(free), true
}
