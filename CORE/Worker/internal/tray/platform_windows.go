//go:build windows

package tray

import (
	"context"
	"log/slog"
	"os/exec"
	"time"

	"fyne.io/systray"
)

// platformOpen opens url in the system default browser.
func platformOpen(url string) {
	// rundll32 url.dll,FileProtocolHandler is the standard way to open a URL
	// in the default browser on Windows.
	_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
}

// iconBytes returns the OS-appropriate icon bytes: on Windows the systray
// library feeds Windows' LoadImageW/IMAGE_ICON, which requires a .ico.
func iconBytes(s HealthState) []byte { return iconICO(s) }

// NewPlatform returns the Windows systray backend. fyne.io/systray is pure Go
// on Windows (Win32 via syscall, no cgo) and pumps its own message loop, so it
// is safe to run from a worker goroutine — the worker's main goroutine stays
// free for the job loop.
func NewPlatform(ctrl *Controller, log *slog.Logger) Platform {
	return &winPlatform{ctrl: ctrl, log: log}
}

// winPlatform is the Platform implementation backed by fyne.io/systray.
type winPlatform struct {
	ctrl *Controller
	log  *slog.Logger
}

// Start builds the tray menu, wires its actions to the controller, and runs
// the systray event loop. It blocks until the tray is torn down (Quit) or the
// process is shutting down. A tray failure is never load-bearing for task
// execution (same principle as the GP.8 local UI), so any init panic is
// recovered and logged rather than crashing the worker.
func (p *winPlatform) Start(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			p.log.Error("tray: systray failed to start; continuing without a tray", "error", r)
		}
	}()

	systray.Run(func() { p.onReady(ctx) }, func() {
		// onExit: the systray event loop is ending. Worker shutdown is driven
		// by Provider.Quit (which cancelled the root context) or by
		// SIGTERM/SIGINT, not by the tray exiting, so there is nothing to do.
	})
	// systray.Run returns when systray.Quit() is posted. If the process is
	// shutting down for another reason first, the process exit ends this
	// goroutine anyway.
	<-ctx.Done()
}

// onReady is invoked by the systray library once the icon and menu are live.
// It builds the menu and starts the background loops that keep the icon and
// the pause/resume item in sync with the live worker, and route clicks to the
// controller.
func (p *winPlatform) onReady(ctx context.Context) {
	openItem := systray.AddMenuItem("Open manager", "Open the local management UI")
	pauseItem := systray.AddMenuItem("Pause", "Pause task claiming")
	systray.AddSeparator()
	quitItem := systray.AddMenuItem("Quit", "Gracefully stop the worker")

	// Initial paint, then a slow poll to keep the icon / tooltip / pause item
	// current. The state is cheap to read (atomics + a snapshot), so a 2s tick
	// is plenty and keeps the tray unobtrusive.
	p.apply(p.ctrl.Model(), pauseItem)
	go p.pollLoop(ctx, pauseItem)
	go p.handleClick(ctx, ActOpenManager, openItem)
	go p.handleClick(ctx, ActTogglePause, pauseItem)
	go p.handleClick(ctx, ActQuit, quitItem)
}

// apply pushes a model onto the systray: icon, tooltip, and the pause/resume
// item's title/tooltip/checked state.
func (p *winPlatform) apply(m Model, pauseItem *systray.MenuItem) {
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
func (p *winPlatform) pollLoop(ctx context.Context, pauseItem *systray.MenuItem) {
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

// handleClick reads a menu item's click channel and dispatches the matching
// action to the controller. For Quit it also posts systray.Quit() so the icon
// disappears immediately while the worker drains in the background.
func (p *winPlatform) handleClick(ctx context.Context, action Action, item *systray.MenuItem) {
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
