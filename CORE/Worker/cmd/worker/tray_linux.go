//go:build linux && tray

package main

import (
	"context"
	"log/slog"
	"net/url"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/localui"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tray"
)

// startTray wires up the GP.9/D-P5 native desktop tray on a Linux desktop
// (owner decision 10-08 Q3=A). This file exists only in the tagged desktop
// build (`go build -tags tray ./cmd/worker`); the default linux build compiles
// the no-op in tray_other.go instead, so headless servers and container
// workers are untouched and gain no GUI dependency.
//
// It is the same code path windows/darwin use (tray_native.go): the same
// Provider adapter over the live worker, the same Controller (menu model),
// and the same Platform event loop — only internal/tray/platform_linux.go
// differs, and only it touches the OS notification area.
//
// Like the GP.8 local UI it is best-effort: the tray is an operator
// convenience and never load-bearing, so any failure is logged and swallowed
// and the worker continues.
func startTray(ctx context.Context, stop context.CancelFunc, p *workerProvider, uiSrv *localui.Server, log *slog.Logger) {
	if inContainerEnv() {
		log.Info("tray disabled: running in a container")
		return
	}

	provider := trayWorkerProvider{base: p, uiSrv: uiSrv, quit: stop}
	ctrl := tray.NewController(provider, log)
	log.Info("tray starting (linux desktop build)")
	go tray.NewPlatform(ctrl, log).Start(ctx)
}

// trayWorkerProvider adapts the live worker (its localui workerProvider, the
// GP.8 local-UI server, and the root context cancel) to tray.Provider. It
// mirrors the windows/darwin adapter in tray_native.go exactly.
type trayWorkerProvider struct {
	base  *workerProvider
	uiSrv *localui.Server
	quit  func()
}

// Name is the worker's display name.
func (t trayWorkerProvider) Name() string { return t.base.cfg.WorkerName }

// Health maps live worker state to the tray icon state (same mapping as
// windows/darwin):
//
//	paused   -> StatePaused (operator threw the local pause switch)
//	!connected -> StateError (up, but the last heartbeat to the manager failed)
//	NOAH yielding -> StateDegraded (up, but withholding claims for a human)
//	otherwise -> StateOK
func (t trayWorkerProvider) Health() tray.HealthState {
	if t.base.Paused() {
		return tray.StatePaused
	}
	st := t.base.Status()
	if !st.Connected && !st.LastHeartbeat.IsZero() {
		return tray.StateError
	}
	if n := t.base.NOAH(); n.Enabled && !n.CanClaimNow {
		return tray.StateDegraded
	}
	return tray.StateOK
}

// Paused reports the local pause state (same gate the local-UI pause button
// uses).
func (t trayWorkerProvider) Paused() bool { return t.base.Paused() }

// SetPaused drives the worker's existing local pause gate — no new control
// path is invented for the tray.
func (t trayWorkerProvider) SetPaused(v bool) { t.base.SetPaused(v) }

// ManagerURL is the GP.8 local management UI, token-authenticated, for
// "Open manager". Empty when the local UI is disabled or failed to start, in
// which case the menu item simply does nothing.
func (t trayWorkerProvider) ManagerURL() string {
	if t.uiSrv == nil || t.uiSrv.Addr() == "" || t.uiSrv.Token() == "" {
		return ""
	}
	return "http://" + t.uiSrv.Addr() + "/?token=" + url.QueryEscape(t.uiSrv.Token())
}

// Quit triggers the same graceful shutdown as SIGTERM/SIGINT: it cancels the
// worker's root context, so the job loop drains and the process exits cleanly.
func (t trayWorkerProvider) Quit() { t.quit() }
