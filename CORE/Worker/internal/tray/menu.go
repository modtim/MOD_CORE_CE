package tray

import (
	"context"
	"log/slog"
	"time"
)

// This file holds the tray's event loop in platform-neutral form: the sync
// tick that keeps the native tray in sync with the live worker, and the click
// routing that turns a menu-item click into a Controller.Dispatch. It is
// compiled on every build (no build tag) and depends only on the MenuSink
// seam, so it is unit-testable with a fake sink — no display, no DBus, no
// systray library. The linux tray (platform_linux.go, build tag `tray`) runs
// exactly this loop; windows/darwin keep their own equivalent loops against
// the same shared Model.

// SyncInterval is how often the tray re-reads live worker state and repaints.
// The reads are cheap (atomics + a snapshot), so a slow tick keeps the tray
// unobtrusive.
const SyncInterval = 2 * time.Second

// MenuSink is the platform-neutral seam between the event loop and a native
// tray. Each platform implements it over its own notification-area backend
// (windows/darwin systray items, linux DBus StatusNotifierItem menu items).
type MenuSink interface {
	// Paint applies a model to the native tray (icon, tooltip, pause item).
	Paint(m Model)
	// Wait blocks until the menu item for the given action is clicked. It
	// returns true when the click happened and false when the wait ended for
	// another reason (the worker is shutting down, or the tray was torn down)
	// — a false return means "stop following this item".
	Wait(ctx context.Context, action Action) bool
	// Quit tears the native tray down (the icon disappears immediately).
	Quit()
}

// MenuLoop is the tray's event loop: it holds the decision logic (Controller)
// and the native tray (MenuSink), and keeps the two in step until the worker
// shuts down.
type MenuLoop struct {
	ctrl *Controller
	sink MenuSink
	log  *slog.Logger
}

// NewMenuLoop builds a MenuLoop. log may be nil (defaults to slog.Default).
func NewMenuLoop(ctrl *Controller, sink MenuSink, log *slog.Logger) *MenuLoop {
	if log == nil {
		log = slog.Default()
	}
	return &MenuLoop{ctrl: ctrl, sink: sink, log: log}
}

// Paint pushes the current model onto the native tray.
func (l *MenuLoop) Paint() { l.sink.Paint(l.ctrl.Model()) }

// Sync re-paints on the SyncInterval tick until ctx is done. It returns when
// the worker is shutting down.
func (l *MenuLoop) Sync(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			l.Paint()
		}
	}
}

// Follow waits for clicks on one menu item and dispatches them. Quit is
// terminal: it also tears the tray down, so the icon disappears while the
// worker drains in the background.
func (l *MenuLoop) Follow(ctx context.Context, action Action) {
	for {
		if !l.sink.Wait(ctx, action) {
			return
		}
		l.ctrl.Dispatch(action)
		if action == ActQuit {
			l.sink.Quit()
			return
		}
	}
}

// Run starts the sync loop and one follower per menu action, after an initial
// paint so the tray is correct before the first tick.
func (l *MenuLoop) Run(ctx context.Context) {
	l.Paint()
	go l.Sync(ctx, SyncInterval)
	go l.Follow(ctx, ActOpenManager)
	go l.Follow(ctx, ActTogglePause)
	go l.Follow(ctx, ActQuit)
}
