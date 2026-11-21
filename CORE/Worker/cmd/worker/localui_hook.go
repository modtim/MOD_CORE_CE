package main

import (
	"context"
	"log/slog"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/localui"
)

// startLocalUI wires up the GP.8/D-P4 local management web UI. Best-effort
// and non-blocking: MOD_WORKER_LOCAL_UI_PORT=0 disables it outright, and any
// setup error (e.g. the port is already taken) is logged and swallowed —
// this is an operator convenience, never load-bearing for task execution,
// so a failure here must never block or crash worker startup. Stops on
// ctx.Done() (the same root context SIGTERM/SIGINT/the Windows SCM handler
// cancel — see signal.NotifyContext in main()).
// startLocalUI returns the started *localui.Server, or nil when the UI is
// disabled (port 0) or setup failed. Callers that need a live UI (e.g. the
// GP.9 tray's "Open manager") check for nil.
func startLocalUI(ctx context.Context, provider *workerProvider, log *slog.Logger) *localui.Server {
	port := localui.PortFromEnv()
	if port == 0 {
		log.Info("local UI disabled", "env", localui.PortEnv, "value", "0")
		return nil
	}

	srv, err := localui.New(localui.Config{
		Host:     "127.0.0.1",
		Port:     port,
		StateDir: localui.StateDir(),
		Provider: provider,
		Log:      log,
	})
	if err != nil {
		log.Warn("local UI disabled: setup failed", "error", err)
		return nil
	}

	srv.Start()
	go func() {
		<-ctx.Done()
		stopCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := srv.Stop(stopCtx); err != nil {
			log.Warn("local UI shutdown error", "error", err)
		}
	}()
	return srv
}
