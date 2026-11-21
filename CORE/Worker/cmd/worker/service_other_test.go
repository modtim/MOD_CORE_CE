//go:build !windows

package main

import (
	"context"
	"testing"
)

// TestRunMaybeAsServiceNoop verifies the non-Windows passthrough does not
// block, panic, or touch ctx/stop/done — the worker's existing
// signal.NotifyContext shutdown path must be completely untouched on
// platforms with no Windows Service Control Manager to register with.
func TestRunMaybeAsServiceNoop(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan struct{})
	defer close(done)

	// Must return immediately; must not close done or call cancel itself.
	runMaybeAsService(ctx, cancel, done)

	select {
	case <-ctx.Done():
		t.Fatal("runMaybeAsService must not cancel ctx on non-Windows")
	case <-done:
		t.Fatal("runMaybeAsService must not close done on non-Windows")
	default:
	}
}
