//go:build windows

package main

import "golang.org/x/sys/windows/svc"

// isWindowsServiceMode reports whether this process was launched by the SCM
// (worker_install.ps1 registers it as "MODWorker"). A service has no user
// session, so the desktop tray must stay off (D-P7: the tray needs an
// interactive session). Mirrors the check in runMaybeAsService.
func isWindowsServiceMode() bool {
	ok, err := svc.IsWindowsService()
	return err == nil && ok
}
