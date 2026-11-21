package main

import (
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/availability"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/capabilities"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/heartbeat"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/localui"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/monitor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/nativeapps"
)

// workerProvider adapts the live worker process (config, heartbeat, NOAH
// gate, local pause flag, detected native apps) to localui.Provider. Every
// method reads live state directly — there is no caching/snapshot step — so
// the UI is never more than one request-cycle stale. See main.go for
// construction and startLocalUI (localui_hook.go) for how it's wired to the
// actual HTTP server.
type workerProvider struct {
	cfg     *config.Config
	version string
	profile capabilities.Profile
	hb      *heartbeat.Heartbeat
	noah    idleGate
	pause   *pauseGate
}

func (p *workerProvider) Status() localui.StatusInfo {
	lastHB, connected := p.hb.LastHeartbeat()
	return localui.StatusInfo{
		WorkerName:    p.cfg.WorkerName,
		Version:       p.version,
		Status:        string(p.hb.Status()),
		Connected:     connected,
		LastHeartbeat: lastHB,
		RunningTasks:  p.hb.ActiveTaskIDs(),
		Paused:        p.pause.Paused(),
	}
}

func (p *workerProvider) Paused() bool     { return p.pause.Paused() }
func (p *workerProvider) SetPaused(v bool) { p.pause.SetPaused(v) }

// The NOAH lend/pause override (WIN.1.s13) lives on the gate itself — the
// provider just forwards, so the local UI and the app both drive the SAME
// pinned state the poll loop and shouldClaim already read.
func (p *workerProvider) PinnedNoahMode() availability.LocalMode { return p.noah.pinnedMode() }
func (p *workerProvider) SetNoahMode(mode availability.LocalMode) { p.noah.SetNoahMode(mode) }

func (p *workerProvider) NOAH() localui.NOAHInfo {
	v := p.noah.snapshot()
	return localui.NOAHInfo{
		Enabled:         v.Enabled,
		Available:       v.Available,
		Reason:          v.Reason,
		Pinned:          v.Pinned,
		SustainedQuietS: v.SustainedQuietS,
		PauseGraceS:     v.PauseGraceS,
		PluggedInOnly:   v.PluggedInOnly,
		ScheduleStart:   v.ScheduleStart,
		ScheduleEnd:     v.ScheduleEnd,
		CanClaimNow:     v.CanClaimNow,
	}
}

// Logs reports "not available": this worker build has no in-memory log
// ring buffer (it writes structured JSON logs straight to stdout — see
// main.go's slog.NewJSONHandler(os.Stdout, ...)), so there's nothing local
// to tail. Should a ring buffer ever be added, wire it in here.
func (p *workerProvider) Logs() ([]string, bool) { return nil, false }

func (p *workerProvider) Apps() []nativeapps.DetectedApp {
	return p.profile.NativeApps
}

func (p *workerProvider) DiskUsage() (monitor.DiskUsage, bool) {
	return monitor.LatestDiskUsage()
}
