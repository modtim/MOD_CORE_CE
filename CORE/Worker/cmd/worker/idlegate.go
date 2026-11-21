package main

import (
	"context"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/availability"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/heartbeat"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// idleGate is the seam between the worker's claim loop / local UI and the
// idle-desktop availability gate. The non-CE build satisfies it with the full
// gate (noah.go); the Community Edition overlay satisfies it with a no-op gate
// that always allows claims (idlegate_ce.go), so main.go and the local UI never
// reference the concrete gate type.
type idleGate interface {
	// applySettings / applyServerMode receive the heartbeat-pushed settings
	// and lend/pause pin.
	applySettings(heartbeat.NoahSettings)
	applyServerMode(wire string)
	// wire attaches the collaborators main.go builds after the gate: the
	// shared tunables Store, the preempt controller's drain report, and the
	// yield/resume callbacks.
	wire(tun *tunables.Store, handback func() (time.Time, int, bool), onYield, onResume func())
	// run samples availability until ctx is cancelled.
	run(ctx context.Context)
	// shouldClaim reports whether the claim loop may take work now.
	shouldClaim() bool
	// PauseGraceS is the drain grace the preempt controller uses.
	PauseGraceS() time.Duration
	pinnedMode() availability.LocalMode
	SetNoahMode(mode availability.LocalMode)
	snapshot() noahView
}

// noahView is a snapshot of the gate's configuration and live state for the
// local UI page. Thresholds arrive from the server or static config and stay
// read-only there; Pinned is the local lend/pause override in effect.
type noahView struct {
	Enabled         bool
	Available       bool
	Reason          string
	Pinned          availability.LocalMode
	SustainedQuietS int
	PauseGraceS     int
	PluggedInOnly   bool
	ScheduleStart   string
	ScheduleEnd     string
	CanClaimNow     bool
}
