// Package localui implements the worker's local management web UI
// (launch-completion-megaplan GP.8, design decision D-P4): a loopback-only
// HTTP server, gated by a per-process random token file, that shows worker
// status, lets an operator pause/resume claiming, and shows NOAH settings,
// recent logs, detected native apps and disk usage. Same code on Windows,
// macOS and Linux — see cmd/worker's wiring for OS-specific plumbing (there
// is none; this package is pure net/http + html/template).
//
// Handlers depend only on the small Provider interface below, not on the
// concrete worker process (*heartbeat.Heartbeat, the NOAH gate, the
// executor) — so they're unit-testable with a fake Provider, no Docker
// daemon or live manager connection required.
package localui

import (
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/availability"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/monitor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/nativeapps"
)

// StatusInfo is a point-in-time snapshot of worker identity and health for
// the status page.
type StatusInfo struct {
	WorkerName string
	Version    string
	// Status is the worker's reported heartbeat status: "idle", "busy" or
	// "draining".
	Status string
	// Connected reports whether the most recent heartbeat attempt to the
	// manager succeeded.
	Connected bool
	// LastHeartbeat is the time of the most recent heartbeat attempt (success
	// or failure). Zero if none has completed yet.
	LastHeartbeat time.Time
	// RunningTasks is the set of task IDs currently executing.
	RunningTasks []int
	Paused       bool
}

// NOAHInfo is a snapshot of the worker's NOAH (opportunistic availability
// harvest) configuration and live state. The thresholds arrive from the server
// or the worker's static config and stay read-only here; the one thing a local
// user can change is the pinned lend/pause override (WIN.1.s13), which the
// page both displays (Pinned) and offers buttons for.
type NOAHInfo struct {
	Enabled         bool
	Available       bool
	Reason          string
	// Pinned is the local override in effect (availability's LocalMode).
	Pinned          availability.LocalMode
	SustainedQuietS int
	PauseGraceS     int
	PluggedInOnly   bool
	ScheduleStart   string
	ScheduleEnd     string
	// CanClaimNow is the gate's live shouldClaim() decision.
	CanClaimNow bool
}

// Provider is the seam between localui's HTTP handlers and the real worker
// process. cmd/worker wires up the concrete implementation; tests use a
// fake.
type Provider interface {
	// Status returns the current worker status snapshot.
	Status() StatusInfo

	// Paused reports whether the worker is currently withholding new-task
	// claims because of a local pause (distinct from a NOAH yield).
	Paused() bool
	// SetPaused pauses or resumes local task claiming.
	SetPaused(paused bool)

	// NOAH returns the current NOAH/availability configuration and state.
	NOAH() NOAHInfo

	// PinnedNoahMode reports the local NOAH override in effect (lend/pause,
	// or LocalModeNone when nothing is pinned). SetNoahMode pins one (LocalMode
	// None clears it, back to the automatic verdict). Both speak the
	// availability package's vocabulary — the handler validates the wire string
	// with availability.ParseLocalMode before calling these.
	PinnedNoahMode() availability.LocalMode
	SetNoahMode(mode availability.LocalMode)

	// Logs returns the most recent log lines (oldest first) and whether a
	// log ring buffer is available at all. (nil, false) means "not
	// available" (no ring buffer wired up in this build), which the page
	// must render differently from "available but empty".
	Logs() (lines []string, available bool)

	// Apps returns the native applications detected on this worker (see
	// internal/nativeapps). Empty on a non-native (container-only) worker.
	Apps() []nativeapps.DetectedApp

	// DiskUsage returns the worker's cached disk-usage snapshot and whether
	// one has been collected yet.
	DiskUsage() (monitor.DiskUsage, bool)
}
