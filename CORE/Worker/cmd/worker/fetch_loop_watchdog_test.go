package main

import (
	"os"
	"testing"
	"time"
)

// TestFetchLoopStalled_TripsAboveThreshold covers the watchdog's actual trip
// condition (fetchLoopStalled), independent of env parsing or the goroutine
// loop — this is what startFetchLoopWatchdog evaluates against hb.FetchLoopAge()
// on every tick.
func TestFetchLoopStalled_TripsAboveThreshold(t *testing.T) {
	threshold := 10 * time.Minute

	cases := []struct {
		name string
		age  time.Duration
		want bool
	}{
		{"idle-fresh", 0, false},
		{"busy-but-alive", 9 * time.Minute, false},
		{"exactly-at-threshold-not-yet-stalled", 10 * time.Minute, false},
		{"stalled-just-over", 10*time.Minute + time.Second, true},
		{"stalled-way-over", time.Hour, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := fetchLoopStalled(tc.age, threshold); got != tc.want {
				t.Errorf("fetchLoopStalled(%s, %s) = %v, want %v", tc.age, threshold, got, tc.want)
			}
		})
	}
}

func TestFetchLoopStalled_ZeroThresholdNeverTrips(t *testing.T) {
	// A zero/negative threshold (e.g. misconfiguration) must never trip the
	// watchdog — the worker should not self-kill on a config error.
	if fetchLoopStalled(time.Hour, 0) {
		t.Fatal("zero threshold must never report stalled")
	}
}

func TestFetchLoopStallThreshold_DefaultIsTenMinutes(t *testing.T) {
	clearFetchLoopEnv(t)
	got := fetchLoopStallThreshold()
	want := 20 * fetchLoopPollInterval // N=20 default * 30s = 10m
	if got != want {
		t.Fatalf("default threshold = %s, want %s", got, want)
	}
	if want != 10*time.Minute {
		t.Fatalf("sanity: default threshold should be 10m, computed %s", want)
	}
}

func TestFetchLoopStallThreshold_MultiplierOverride(t *testing.T) {
	clearFetchLoopEnv(t)
	t.Setenv("MOD_WORKER_FETCH_STALL_MULTIPLIER", "2")
	got := fetchLoopStallThreshold()
	want := 2 * fetchLoopPollInterval // 60s
	if got != want {
		t.Fatalf("threshold with N=2 = %s, want %s", got, want)
	}
}

func TestFetchLoopStallThreshold_AbsoluteOverrideWinsOverMultiplier(t *testing.T) {
	clearFetchLoopEnv(t)
	t.Setenv("MOD_WORKER_FETCH_STALL_MULTIPLIER", "20")
	t.Setenv("MOD_WORKER_FETCH_STALL_TIMEOUT_MS", "5000")
	got := fetchLoopStallThreshold()
	if got != 5*time.Second {
		t.Fatalf("absolute override threshold = %s, want 5s", got)
	}
}

func TestFetchLoopWatchdogCheckInterval_FloorsAtTenSeconds(t *testing.T) {
	if got := fetchLoopWatchdogCheckInterval(20 * time.Second); got != 10*time.Second {
		t.Fatalf("check interval for a 20s threshold = %s, want floor 10s", got)
	}
	if got := fetchLoopWatchdogCheckInterval(40 * time.Minute); got != 10*time.Minute {
		t.Fatalf("check interval for a 40m threshold = %s, want 10m", got)
	}
}

func clearFetchLoopEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{"MOD_WORKER_FETCH_STALL_MULTIPLIER", "MOD_WORKER_FETCH_STALL_TIMEOUT_MS"} {
		v, had := os.LookupEnv(k)
		os.Unsetenv(k)
		if had {
			t.Cleanup(func() { os.Setenv(k, v) })
		}
	}
}
