package main

import (
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/mining"
)

// FX.135 p4a1b — the feed path: main() wires the counter with SetDefault, the
// sample-drain sites (task monitor loop, idle-host heartbeat read) call Feed.
// This proves samples actually reach the counter through that path.

func TestFeed_WiredReachesCounter(t *testing.T) {
	c := mining.New(mining.DefaultConfig())
	mining.SetDefault(c)

	base := time.Now()
	// A non-GPU task pinned at full utilisation: the mining signature.
	for i := 0; i < 4; i++ {
		mining.Feed(mining.Sample{
			Timestamp: base.Add(time.Duration(i*30) * time.Second),
			GPUUtil:   99.0,
			Phase:     "idle",
		})
	}

	got := mining.Default()
	if got != c {
		t.Fatalf("Default() must return the counter wired by SetDefault")
	}
	s := got.Snapshot()
	if s.TotalSamples != 4 {
		t.Fatalf("expected 4 samples through the feed path, got %d", s.TotalSamples)
	}
	if s.IdleHighUtilSamples != 4 {
		t.Fatalf("expected 4 idle-high-util samples, got %d", s.IdleHighUtilSamples)
	}
	if s.GPUUtilMaxPct != 99.0 {
		t.Fatalf("expected max 99, got %f", s.GPUUtilMaxPct)
	}
}

func TestFeed_UnwiredIsNoop(t *testing.T) {
	mining.SetDefault(nil)
	mining.Feed(mining.Sample{Timestamp: time.Unix(0, 0), GPUUtil: 100.0, Phase: "idle"})
	if c := mining.Default(); c != nil {
		t.Fatalf("Feed must be a no-op when no counter is wired")
	}
}

func TestFeed_PhaseFromTaskDeclaration(t *testing.T) {
	c := mining.New(mining.DefaultConfig())
	mining.SetDefault(c)

	base := time.Now()
	// GPURequired task: samples are labelled "task", so they never count as
	// idle-high-util even at full utilisation.
	for i := 0; i < 3; i++ {
		mining.Feed(mining.Sample{
			Timestamp: base.Add(time.Duration(i*30) * time.Second),
			GPUUtil:   99.0,
			Phase:     "task",
		})
	}

	s := mining.Default().Snapshot()
	if s.TotalSamples != 3 {
		t.Fatalf("expected 3 samples, got %d", s.TotalSamples)
	}
	if s.IdleHighUtilSamples != 0 {
		t.Fatalf("task-phase samples must not count as idle-high-util, got %d", s.IdleHighUtilSamples)
	}
}
