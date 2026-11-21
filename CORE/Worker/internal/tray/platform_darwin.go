//go:build darwin && cgo

package tray

import (
	"context"
	"log/slog"
	"os/exec"
	"time"

	"fyne.io/systray"
)

// platformOpen opens url in the system default browser (macOS `open`).
func platformOpen(url string) {
	_ = exec.Command("open", url).Start()
}

// iconBytes returns the OS-appropriate icon bytes: on darwin the systray
// library loads an image (PNG) into an NSImage.
func iconBytes(s HealthState) []byte { return iconPNG(s) }

// NewPlatform returns the macOS systray backend. On darwin fyne.io/systray
// uses cgo + Cocoa (NSStatusBar), so this file is only compiled when cgo is
// available — i.e. a native macOS build. Cross-compiling darwin from Linux
// runs with cgo disabled and instead compiles the no-op in
// platform_darwin_nocgo.go, which is what keeps `GOOS=darwin go build` green
// off a mac.
//
// NOTE (live validation): systray's darwin backend calls [NSApp run] and
// marshals menu/icon updates onto the main thread (AppKit is main-thread
// affined). That is fine to exercise here for compilation, but the tray
// actually appearing and responding on an Intel Mac is part of the GP.9
// live check.
func NewPlatform(ctrl *Controller, log *slog.Logger) Platform {
	return &darwinPlatform{ctrl: ctrl, log: log}
}

// darwinPlatform is the Platform implementation backed by fyne.io/systray.
type darwinPlatform struct {
	ctrl *Controller
	log  *slog.Logger
}

// Start runs the systray event loop. It recovers from any init panic so a
// tray failure never crashes the worker (same principle as the GP.8 local UI).
func (p *darwinPlatform) Start(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			p.log.Error("tray: systray failed to start; continuing without a tray", "error", r)
		}
	}()

	systray.Run(func() { p.onReady(ctx) }, func() {})
	<-ctx.Done()
}

// onReady builds the menu and starts the sync/click loops.
func (p *darwinPlatform) onReady(ctx context.Context) {
	openItem := systray.AddMenuItem("Open manager", "Open the local management UI")
	pauseItem := systray.AddMenuItem("Pause", "Pause task claiming")
	systray.AddSeparator()
	quitItem := systray.AddMenuItem("Quit", "Gracefully stop the worker")

	p.apply(p.ctrl.Model(), pauseItem)
	go p.pollLoop(ctx, pauseItem)
	go p.handleClick(ctx, ActOpenManager, openItem)
	go p.handleClick(ctx, ActTogglePause, pauseItem)
	go p.handleClick(ctx, ActQuit, quitItem)
}

// apply pushes a model onto the systray.
func (p *darwinPlatform) apply(m Model, pauseItem *systray.MenuItem) {
	systray.SetIcon(m.Icon)
	systray.SetTooltip(m.Tooltip)
	pauseItem.SetTitle(m.PauseTitle)
	pauseItem.SetTooltip(m.PauseTooltip)
	if m.PauseChecked {
		pauseItem.Check()
	} else {
		pauseItem.Uncheck()
	}
}

// pollLoop keeps the tray in sync with the live worker until ctx is done.
func (p *darwinPlatform) pollLoop(ctx context.Context, pauseItem *systray.MenuItem) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.apply(p.ctrl.Model(), pauseItem)
		}
	}
}

// handleClick dispatches a menu click to the controller; Quit also posts
// systray.Quit() so the icon disappears while the worker drains.
func (p *darwinPlatform) handleClick(ctx context.Context, action Action, item *systray.MenuItem) {
	for {
		select {
		case <-item.ClickedCh:
			p.ctrl.Dispatch(action)
			if action == ActQuit {
				systray.Quit()
				return
			}
		case <-ctx.Done():
			return
		}
	}
}
