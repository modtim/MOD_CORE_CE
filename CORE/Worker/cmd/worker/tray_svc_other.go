//go:build !windows

package main

// isWindowsServiceMode is always false off Windows (no SCM service mode).
// It is declared here so tray_detect.go / the tray hooks compile identically
// on every OS; the Windows definition lives in tray_svc_windows.go.
func isWindowsServiceMode() bool { return false }
