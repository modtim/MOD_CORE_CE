// Package tray implements the worker's native desktop tray (launch-completion
// megaplan GP.9, design decision D-P5): a thin cross-platform systray that
// shows worker health (ok / degraded / error / paused), offers "Open manager"
// (opens the GP.8 local management UI), Pause/Resume (toggling the worker's
// existing local pause gate) and Quit (graceful shutdown).
//
// All of the decision logic lives in this platform-neutral file — the
// health-state-to-icon mapping, the menu model, and the action handlers —
// behind a small Provider interface, so it is unit-testable with a fake
// Provider and no systray, no GUI, and no live worker. Only the Platform
// implementation (see the *_windows/_darwin/_other files) touches the OS
// notification area, and only on windows/darwin does it pull in the systray
// library at all; the linux build never imports it.
package tray

import (
	"context"
	"log/slog"
)

// HealthState is the coarse worker health the tray icon encodes.
type HealthState int

// The four icon states. Order is not significant; they are compared by value.
const (
	// StateOK: worker is healthy and claiming work.
	StateOK HealthState = iota
	// StateDegraded: worker is up but reduced — e.g. NOAH is yielding the
	// machine to a human, or no heartbeat has completed yet at startup.
	StateDegraded
	// StateError: worker is up but cannot reach the manager (last heartbeat
	// failed).
	StateError
	// StatePaused: an operator paused local task claiming.
	StatePaused
)

// Label is the short human-readable name of the state (used in the tooltip).
func (s HealthState) Label() string {
	switch s {
	case StateOK:
		return "ok"
	case StateDegraded:
		return "degraded"
	case StateError:
		return "error"
	case StatePaused:
		return "paused"
	}
	return "unknown"
}

// Action identifies a menu action the operator can take.
type Action string

// The tray's fixed menu actions, in display order.
const (
	// ActOpenManager opens the GP.8 local management UI in the default browser.
	ActOpenManager Action = "open-manager"
	// ActTogglePause pauses or resumes local task claiming (toggles).
	ActTogglePause Action = "toggle-pause"
	// ActQuit triggers a graceful worker shutdown.
	ActQuit Action = "quit"
)

// Provider is the seam between the tray and the live worker process.
// cmd/worker wires up the concrete implementation (heartbeat, NOAH gate, the
// GP.8 local-UI server, and the root context cancel); tests use a fake.
type Provider interface {
	// Name is the worker's display name, shown in the tray tooltip.
	Name() string
	// Health reports the current coarse worker health for the icon.
	Health() HealthState
	// Paused reports whether local task claiming is currently paused.
	Paused() bool
	// SetPaused pauses (true) or resumes (false) local task claiming. It must
	// drive the worker's existing pause mechanism — the same gate the GP.8
	// local-UI pause button uses — not a new one.
	SetPaused(paused bool)
	// ManagerURL is the GP.8 local management UI to open, already carrying
	// the auth token. Empty means the local UI is unavailable (disabled or
	// failed to start), in which case "Open manager" does nothing.
	ManagerURL() string
	// Quit triggers a graceful worker shutdown.
	Quit()
}

// Model is a point-in-time description of what the tray should display. It is
// produced by Controller.Model and applied by the platform runner. It is a
// plain value so it is trivially comparable in tests.
type Model struct {
	// Icon is the OS-appropriate icon bytes for the current HealthState.
	Icon []byte
	// Tooltip is the text shown when hovering the tray icon.
	Tooltip string
	// OpenTitle/OpenTooltip label the "Open manager" item.
	OpenTitle   string
	OpenTooltip string
	// PauseTitle/PauseTooltip label the pause/resume item; PauseChecked
	// reports whether it should show as ticked.
	PauseTitle   string
	PauseTooltip string
	PauseChecked bool
	// QuitTitle/QuitTooltip label the "Quit" item.
	QuitTitle   string
	QuitTooltip string
}

// Controller holds the tray's decision logic. It is stateless apart from its
// dependencies: each Model() reads live state from the Provider, and each
// Dispatch() runs an action against it.
type Controller struct {
	provider Provider
	log      *slog.Logger
	// openURL launches a URL in the system default browser. It is a field (not
	// a hard dependency on the platform opener) so tests can substitute a
	// recorder; NewController defaults it to the platform opener (defined in
	// the platform-tagged file for the current OS).
	openURL func(url string)
}

// NewController builds a Controller. log may be nil (defaults to slog.Default).
func NewController(provider Provider, log *slog.Logger) *Controller {
	if log == nil {
		log = slog.Default()
	}
	return &Controller{provider: provider, log: log, openURL: platformOpen}
}

// Model returns the current tray UI model.
func (c *Controller) Model() Model {
	st := c.provider.Health()
	name := c.provider.Name()
	if name == "" {
		name = "MOD worker"
	}

	m := Model{
		Icon:        iconBytes(st),
		Tooltip:     name + " — " + st.Label(),
		OpenTitle:   "Open manager",
		OpenTooltip: "Open the local management UI",
		QuitTitle:   "Quit",
		QuitTooltip: "Gracefully stop the worker",
	}
	if c.provider.Paused() {
		m.PauseTitle = "Resume"
		m.PauseTooltip = "Resume task claiming"
		m.PauseChecked = true
	} else {
		m.PauseTitle = "Pause"
		m.PauseTooltip = "Pause task claiming"
	}
	return m
}

// Dispatch runs the given menu action. Unknown actions are ignored.
func (c *Controller) Dispatch(a Action) {
	switch a {
	case ActOpenManager:
		u := c.provider.ManagerURL()
		if u == "" {
			c.log.Warn("tray: manager UI not available; nothing to open")
			return
		}
		c.openURL(u)
	case ActTogglePause:
		next := !c.provider.Paused()
		c.provider.SetPaused(next)
		c.log.Info("tray: pause toggled", "paused", next)
	case ActQuit:
		c.log.Info("tray: quit requested")
		c.provider.Quit()
	default:
		c.log.Warn("tray: unknown action", "action", string(a))
	}
}

// Platform is the OS notification-area backend. It is produced by NewPlatform
// (build-tagged per OS) and started by cmd/worker. On windows/darwin it wraps
// the systray library; everywhere else (and on darwin without cgo) it is a
// no-op so the worker is unaffected.
type Platform interface {
	// Start builds the native tray, wires its menu to the controller, and runs
	// the event loop. It returns when the tray is torn down. Implementations
	// must be safe to start from a non-main goroutine on the target OS.
	Start(ctx context.Context)
}

// noopPlatform is the Platform used when the tray is not applicable (linux,
// or darwin cross-compiled without cgo). Starting it does nothing and returns
// immediately, so a worker built for (or running on) such a host is entirely
// unaffected.
type noopPlatform struct{}

// Start is a no-op.
func (noopPlatform) Start(context.Context) {}
