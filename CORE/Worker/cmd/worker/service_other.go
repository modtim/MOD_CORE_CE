//go:build !windows

package main

import "context"

// runMaybeAsService is the non-Windows passthrough: only Windows has a
// Service Control Manager to register with (worker_install.ps1's
// `sc.exe create MODWorker`), so everywhere else — Linux systemd units,
// macOS/darwin builds, or a bare interactive run — this does nothing and
// the worker's own signal.NotifyContext-based shutdown (main.go) is the
// entire story, exactly as before this hook was added. See
// service_windows.go for the Windows implementation.
func runMaybeAsService(_ context.Context, _ context.CancelFunc, _ <-chan struct{}) {}
