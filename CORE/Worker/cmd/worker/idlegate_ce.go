package main

import (
	"context"
	"log/slog"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/availability"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/heartbeat"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// openGate is the Community Edition idleGate: a worker with no idle-desktop
// availability gate. Claims are never withheld, nothing yields, and the local
// lend/pause pin is not stored.
type openGate struct{}

func newIdleGate(_ *config.Config, _ string, _ *heartbeat.Heartbeat, _ *slog.Logger, _ *executor.Executor) idleGate {
	return openGate{}
}

func (openGate) applySettings(heartbeat.NoahSettings) {}
func (openGate) applyServerMode(string)               {}
func (openGate) wire(*tunables.Store, func() (time.Time, int, bool), func(), func()) {
}
func (openGate) run(ctx context.Context)            { <-ctx.Done() }
func (openGate) shouldClaim() bool                  { return true }
func (openGate) PauseGraceS() time.Duration         { return 2 * time.Minute }
func (openGate) pinnedMode() availability.LocalMode { return availability.LocalModeNone }
func (openGate) SetNoahMode(availability.LocalMode) {}
func (openGate) snapshot() noahView {
	return noahView{Available: true, CanClaimNow: true}
}
