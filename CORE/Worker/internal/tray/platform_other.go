//go:build (!windows && (!darwin || !cgo)) && !(linux && tray)

package tray

import (
	"log/slog"
)

// This file is the no-op tray backend for every build where a native systray
// is not applicable:
//
//   - linux, unless the desktop-tray build tag is on (see platform_linux.go,
//     `//go:build linux && tray`, and docs/LINUX_TRAY.md). A default linux
//     build — which is what the Makefile/Dockerfile ship, CGO_ENABLED=0
//     GOOS=linux — compiles THIS file, so a headless server or container
//     worker never links the systray/GUI dependency at all; and
//   - darwin cross-compiled from a host without cgo (e.g. `GOOS=darwin go
//     build` from Linux, which disables cgo). On a real macOS build with cgo,
//     platform_darwin.go compiles instead and provides a working tray.
//
// The no-op keeps the worker completely unaffected: NewPlatform returns
// noopPlatform, whose Start returns immediately and shows nothing.

// platformOpen is a no-op: there is no tray, so nothing is ever opened.
func platformOpen(string) {}

// iconBytes returns the PNG icon for a health state. It is only ever called on
// a no-op build where the result is discarded, so PNG is a harmless default.
func iconBytes(s HealthState) []byte { return iconPNG(s) }

// NewPlatform returns the no-op Platform: on this platform the tray is simply
// not shown.
func NewPlatform(_ *Controller, _ *slog.Logger) Platform { return noopPlatform{} }
