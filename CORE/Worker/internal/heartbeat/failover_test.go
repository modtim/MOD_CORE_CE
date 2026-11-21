package heartbeat

import (
	"io"
	"log/slog"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
)

func discardLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestNextAfterRoundRobin(t *testing.T) {
	c := []string{"http://api:80", "http://p1", "http://p2"}
	cases := map[string]string{
		"http://api:80":   "http://p1",
		"http://p1":       "http://p2",
		"http://p2":       "http://api:80", // wraps back to cfg.APIURL
		"http://api:80/":  "http://p1",     // trailing slash normalised
		"http://unlisted": "http://api:80", // not in list -> first candidate (cfg.APIURL)
	}
	for cur, want := range cases {
		if got := nextAfter(c, cur); got != want {
			t.Errorf("nextAfter(cur=%q) = %q; want %q", cur, got, want)
		}
	}
	if got := nextAfter(nil, "x"); got != "" {
		t.Errorf("nextAfter(nil) = %q; want empty", got)
	}
	if got := nextAfter([]string{"http://only"}, "http://only"); got != "http://only" {
		t.Errorf("single candidate should return itself, got %q", got)
	}
}

// Regression for the ~3h embedded-worker outage: when every roster entry is
// unreachable, the worker must NOT get stranded rotating among dead peers — it
// must cycle back to cfg.APIURL (the internal http://api:80 that actually works).
func TestFailoverAlwaysReturnsToConfiguredURL(t *testing.T) {
	h := &Heartbeat{
		cfg:    &config.Config{APIURL: "http://api:80"},
		curURL: "http://api:80",
		log:    discardLog(),
	}
	// A successful heartbeat delivers a roster of peers that later all go dead.
	h.noteHeartbeat(true, []string{"http://dead1", "http://dead2"})
	if h.currentURL() != "http://api:80" {
		t.Fatalf("curURL changed on a successful heartbeat: %q", h.currentURL())
	}

	// Everything is unreachable now. First confirm it rotates OFF cfg.APIURL.
	for i := 0; i < 12 && h.currentURL() == "http://api:80"; i++ {
		h.noteHeartbeat(false, nil)
	}
	if h.currentURL() == "http://api:80" {
		t.Fatalf("expected rotation off cfg.APIURL onto a peer, still on cfg.APIURL")
	}
	// Keep failing — it must cycle back to cfg.APIURL within a bounded number of
	// rotations (pre-fix it oscillated between the dead peers forever).
	returned := false
	for i := 0; i < 30; i++ {
		h.noteHeartbeat(false, nil)
		if h.currentURL() == "http://api:80" {
			returned = true
			break
		}
	}
	if !returned {
		t.Fatalf("worker never rotated back to cfg.APIURL; stuck on %q", h.currentURL())
	}
}

// With no roster at all, the single candidate is cfg.APIURL — the worker keeps
// retrying it rather than blanking curURL.
func TestFailoverNoRosterStaysOnConfigured(t *testing.T) {
	h := &Heartbeat{
		cfg:    &config.Config{APIURL: "http://api:80"},
		curURL: "http://api:80",
		log:    discardLog(),
	}
	for i := 0; i < 20; i++ {
		h.noteHeartbeat(false, nil)
	}
	if h.currentURL() != "http://api:80" {
		t.Fatalf("curURL should stay http://api:80 with no roster, got %q", h.currentURL())
	}
}
