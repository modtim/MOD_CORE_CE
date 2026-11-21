//go:build linux && tray

package tray

import (
	"context"
	"log/slog"
	"os/exec"

	"fyne.io/systray"
)

// This file is the native Linux desktop tray (owner decision 10-08 Q3=A): on
// a Linux desktop the worker gets the same tray as on Windows/macOS, through
// the SAME code path — the platform-neutral menu model in tray.go and the
// shared event loop in menu.go — and only this file touches the OS
// notification area.
//
// BUILD TAG: this file compiles only under the `tray` tag:
//
//     go build -tags tray ./cmd/worker        # desktop build, tray enabled
//     go build ./cmd/worker                   # default: no tray, no GUI dep
//
// A default linux build (no tag) compiles the no-op stub in platform_other.go
// instead, so the shipped binaries (the Makefile/Dockerfile cross builds use
// CGO_ENABLED=0 GOOS=linux) and every headless server/container worker gain
// ZERO new dependencies — nothing GUI-related is even linked.
//
// BACKEND: fyne.io/systray's linux implementation is pure Go — it speaks the
// StatusNotifierItem spec over the user's DBus session bus (github.com/godbus
// /dbus/v5) and serves the com.canonical.AppMenu dropdown. It needs NO cgo and
// NO libayatana-appindicator C library, so the tag build links no system
// libraries beyond the Go runtime. Runtime requirements on the desktop (build
// time needs none of these):
//
//   - KDE / modern XFCE / LXQt: the tray works natively;
//   - Gnome: install a StatusNotifierItem proxy — snixembed is what the
//     library's README recommends ("StatusNotifierItems XEmbedded");
//   - no DBus session (headless, ssh-only, Wayland-only with no provider):
//     the library logs one line to its own log and never receives clicks, so
//     the worker is unaffected.
//
// See CORE/Worker/docs/LINUX_TRAY.md for the package-deps note.

// platformOpen opens url in the system default browser (xdg-open).
func platformOpen(url string) {
	_ = exec.Command("xdg-open", url).Start()
}

// iconBytes returns the PNG icon bytes for a health state (linux uses PNG,
// unlike Windows' .ico).
func iconBytes(s HealthState) []byte { return iconPNG(s) }

// NewPlatform returns the linux systray backend.
func NewPlatform(ctrl *Controller, log *slog.Logger) Platform {
	return &linuxPlatform{ctrl: ctrl, log: log}
}

// linuxPlatform is the Platform implementation backed by fyne.io/systray's
// DBus StatusNotifierItem backend.
type linuxPlatform struct {
	ctrl *Controller
	log  *slog.Logger
}

// Start runs the systray event loop. Like the windows/darwin backends (and
// the GP.8 local UI) a tray failure is never load-bearing for task execution,
// so any init panic is recovered and logged rather than crashing the worker.
func (p *linuxPlatform) Start(ctx context.Context) {
	// The recover guard must be installed before systray touches DBus, so the
	// deferred handler is registered first.
	defer func() {
		if r := recover(); r != nil {
			p.log.Error("tray: systray failed to start; continuing without a tray", "error", r)
		}
	}()

	systray.Run(func() { p.onReady(ctx) }, func() {})
	<-ctx.Done()
}

// onReady builds the menu and hands the shared, platform-neutral event loop
// (menu.go) the native sink. Everything after this point — the sync tick, the
// click routing, Quit's teardown — is the same code windows/darwin run.
func (p *linuxPlatform) onReady(ctx context.Context) {
	openItem := systray.AddMenuItem("Open manager", "Open the local management UI")
	pauseItem := systray.AddMenuItemCheckbox("Pause", "Pause task claiming", false)
	systray.AddSeparator()
	quitItem := systray.AddMenuItem("Quit", "Gracefully stop the worker")

	sink := &linuxSink{items: map[Action]*systray.MenuItem{
		ActOpenManager: openItem,
		ActTogglePause: pauseItem,
		ActQuit:        quitItem,
		}}
	NewMenuLoop(p.ctrl, sink, p.log).Run(ctx)
}

// linuxSink is the MenuSink implementation over systray menu items.
type linuxSink struct {
	items map[Action]*systray.MenuItem
}

// Paint pushes a model onto the DBus tray: icon, tooltip, and the pause
// item's title/tooltip/checkmark.
func (s *linuxSink) Paint(m Model) {
	systray.SetIcon(m.Icon)
	systray.SetTooltip(m.Tooltip)
	it := s.items[ActTogglePause]
	if it == nil {
		return
	}
	it.SetTitle(m.PauseTitle)
	it.SetTooltip(m.PauseTooltip)
	if m.PauseChecked {
		it.Check()
	} else {
		it.Uncheck()
	}
}

// Wait blocks until the item for action is clicked; false means the wait
// ended for another reason (worker shutdown), which stops the follower.
func (s *linuxSink) Wait(ctx context.Context, action Action) bool {
	it := s.items[action]
	if it == nil {
		return false
	}
	for {
		select {
		case <-it.ClickedCh:
			return true
		case <-ctx.Done():
			return false
		}
	}
}

// Quit tears the native tray down so the icon disappears while the worker
// drains in the background.
func (s *linuxSink) Quit() { systray.Quit() }
