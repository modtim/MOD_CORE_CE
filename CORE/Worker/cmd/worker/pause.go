package main

import "sync/atomic"

// pauseGate holds the operator-triggered local pause (GP.8/D-P4's "Pause"
// button in the local web UI). Distinct from NOAH's own yield/resume: this
// is a deliberate human action, not an automatic availability reaction, and
// it never auto-clears — it stays paused until the same (or another)
// operator hits Resume.
//
// The main job loop's gate chain in cmd/worker/main.go checks Paused() the
// same way it already checks noah.shouldClaim(): spin with
// hb.MarkFetchLoopIteration() on every pass so the fetch-loop watchdog
// (startFetchLoopWatchdog / MarkFetchLoopIteration) never mistakes an
// intentionally-paused worker for a wedged one.
type pauseGate struct {
	paused atomic.Bool
}

// Paused reports the current pause state. Safe for concurrent use.
func (p *pauseGate) Paused() bool { return p.paused.Load() }

// SetPaused pauses (true) or resumes (false) local task claiming.
func (p *pauseGate) SetPaused(v bool) { p.paused.Store(v) }
