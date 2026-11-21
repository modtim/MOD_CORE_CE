//go:build (!windows && !darwin) && !(linux && tray)

package main

import (
	"context"
	"log/slog"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/localui"
)

// startTray is the no-op tray hook for platforms without a native tray (linux).
// It exists so main() can call a single, identical startTray on every OS; on
// linux it does nothing and, because it lives in a file that does not import
// internal/tray, the linux binary never pulls in the systray/GUI dependency at
// all. (The signature matches the windows/darwin version in tray_native.go.)
func startTray(_ context.Context, _ context.CancelFunc, _ *workerProvider, _ *localui.Server, _ *slog.Logger) {
}
