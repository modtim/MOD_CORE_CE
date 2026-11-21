//go:build !windows

package main

// runMaybeAsService is the non-Windows passthrough: only Windows has a
// Service Control Manager to register with (the HX9W installer's
// `sc.exe create mod-host-exec`), so everywhere else — the Linux systemd
// unit (CORE/Worker/scripts/mod-host-exec.service) and the macOS/darwin
// build — this does nothing and the helper's existing listen/serve story is
// unchanged. Mirrors cmd/worker/service_other.go:14. Returns a no-op wait
// func so main() can call it unconditionally on every platform.
func runMaybeAsService(_ func(), _ <-chan struct{}) func() { return func() {} }
