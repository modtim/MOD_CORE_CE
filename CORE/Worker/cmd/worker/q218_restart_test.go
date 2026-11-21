package main

import (
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
)

// GSEC Q21.8 — degraded-boot recovery must not mutate cfg (the heartbeat reads
// h.cfg.BootstrapToken on the same *config.Config every beat) but must trigger
// the existing graceful-shutdown path so the supervisor restarts the worker and
// the boot path applies the recovered bootstrap token to every transport.

func resetRestartState() {
	restartOnce = sync.Once{}
	restartReason = atomic.Value{}
}

func TestRestartExitCodeDefaultZero(t *testing.T) {
	resetRestartState()
	if got := restartExitCode(); got != 0 {
		t.Fatalf("ordinary shutdown must exit 0, got %d", got)
	}
}

func TestRequestGracefulRestartFiresOnceAndExitsNonZero(t *testing.T) {
	resetRestartState()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	var stops atomic.Int64
	stop := func() { stops.Add(1) }

	requestGracefulRestart(stop, log, "bootstrap token recovered from Vault after degraded boot")
	if stops.Load() != 1 {
		t.Fatalf("first recovery must trigger the graceful stop exactly once, got %d", stops.Load())
	}
	if code := restartExitCode(); code != exitCodeRestartForRecovery {
		t.Fatalf("recovery exit code must be the supervisor-restart code %d, got %d",
			exitCodeRestartForRecovery, code)
	}
	if exitCodeRestartForRecovery == 0 {
		t.Fatal("recovery exit code must be non-zero so the supervisor restarts")
	}

	// A second recovery event must not re-trigger a shutdown already draining.
	requestGracefulRestart(stop, log, "duplicate")
	if stops.Load() != 1 {
		t.Fatalf("second recovery must not re-trigger stop, got %d", stops.Load())
	}
}
