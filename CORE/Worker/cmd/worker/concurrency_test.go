package main

import "testing"

// MAX_CONCURRENT_JOBS was parsed by the config package and then read by
// nothing. The docker-compose default of 1 therefore described a cap that did
// not exist — a live worker was measured running 3 tasks at once under it.
// Wiring it up is only safe if 0 keeps meaning "no opinion", since the
// defaults had to change in the same breath to avoid capping every deployment
// at a single task.

func TestEffectiveConcurrency(t *testing.T) {
	cases := []struct {
		name           string
		server, local  int64
		want           int64
	}{
		{"both unset means unlimited", 0, 0, 0},
		{"local only", 0, 4, 4},
		{"server only", 6, 0, 6},
		{"stricter local wins", 8, 2, 2},
		{"stricter server wins", 2, 8, 2},
		{"equal", 5, 5, 5},
		{"negative server treated as no opinion", -1, 3, 3},
		{"negative local treated as no opinion", 3, -1, 3},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := effectiveConcurrency(c.server, c.local); got != c.want {
				t.Fatalf("effectiveConcurrency(%d, %d) = %d, want %d",
					c.server, c.local, got, c.want)
			}
		})
	}
}

// The host owner's ceiling must survive a control plane that asks for more,
// and a license cap must survive an operator who asks for more. Neither side
// can raise the other.
func TestNeitherSideCanRaiseTheOther(t *testing.T) {
	if got := effectiveConcurrency(64, 2); got != 2 {
		t.Fatalf("control plane overrode the host's own ceiling: got %d", got)
	}
	if got := effectiveConcurrency(2, 64) ; got != 2 {
		t.Fatalf("operator exceeded the license cap: got %d", got)
	}
}
