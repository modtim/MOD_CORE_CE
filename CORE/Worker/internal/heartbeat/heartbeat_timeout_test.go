package heartbeat

// rc20d / ce-cleanroom-cr14 (r2): the heartbeat hop carried its OWN hardcoded
// 10 s http.Client cap — 9x tighter than the transport's 90 s budget (transport/api.go:85)
// and two thirds of the 15 s beat (config/config.go:156 default — NOT 30 s: every
// shipped worker config writes heartbeat_interval: 15s). countsAsLostContact
// (fence.go) reads a client timeout as genuine unreachability, so a manager that
// was merely SLOW — 12 s beats while its API process restarted — advanced the
// fence clock: 29 WARN lines in 150 m of worker logs, and the premature fence
// is what swallowed the run's failure report. The cap is now derived from the
// beat: heartbeatHTTPBudget(h.cfg.HeartbeatInterval) = interval - margin, floored,
// always strictly under the interval (13 s at the shipped 15 s beat: under the
// beat so beats never overlap, over the 12 s slow-but-alive case).
//
// Deliberately behavioural rather than a reference to the new name: this file
// must COMPILE against the pre-fix heartbeat.go so the old-file proof goes RED
// (a failing assertion), not merely fail to compile. It reuses newTestHeartbeat
// (tunables_test.go) and discardLog (failover_test.go) — same package.

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

// TestSlowButAliveAPIBeatIsNotLostContact posts one heartbeat against a
// manager that answers 200 only after 12 s — the field's slow-but-alive shape.
// The beat must succeed and must NOT be reported as lost contact.
func TestSlowButAliveAPIBeatIsNotLostContact(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// slow, but the HOST is alive and answers
		time.Sleep(12 * time.Second)
		w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	h := newTestHeartbeat(srv)
	_, resp, err := h.sendAPI(context.Background(), map[string]interface{}{
		"worker_name": "test-worker",
		"status":      "idle",
	})

	if err != nil {
		t.Errorf("12 s slow-but-alive API beat reported lost contact: %v", err)
	}
	if resp == nil {
		t.Errorf("12 s slow-but-alive API beat returned no HTTP response, but the manager HOST answered 200")
	}
	// The fence decision itself: a response of ANY status is contact, so the
	// lease clock must not move.
	if countsAsLostContact(err, resp) {
		t.Errorf("countsAsLostContact classified a 200-after-12-s beat as lost contact")
	}
}

// TestHeartbeatBudgetNotHardcoded reads the package source (go test runs the
// binary with cwd = the package dir) and asserts:
//  - the old 10 s literal is gone;
//  - the client is built from a budget DERIVED from the beat — the Timeout
//    expression reads h.cfg.HeartbeatInterval, not a hardcoded literal;
//  - the package clamps the budget STRICTLY UNDER the interval (otherwise a
//    beat longer than its slot overlaps and one worker holds two live POSTs);
//  - the beat default really is 15 s at config/config.go:156 — the arithmetic
//    above (13 s budget: under 15 s, over the 12 s slow-but-alive case) is
//    pinned to that value, and the old "default 30s" comment was wrong.
func TestHeartbeatBudgetNotHardcoded(t *testing.T) {
	src, err := os.ReadFile("heartbeat.go")
	if err != nil {
		t.Errorf("cannot read heartbeat.go to check the client budget: %v", err)
		return
	}
	s := string(src)
	beat := 15 * time.Second // config/config.go:156 default

	if strings.Contains(s, "Timeout: 10 * time.Second") {
		t.Errorf("heartbeat.go still hardcodes a 10 s client timeout against a %v beat", beat)
	}
	if !strings.Contains(s, "http.Client{Timeout: heartbeatHTTPBudget(h.cfg.HeartbeatInterval)}") {
		t.Errorf("heartbeat.go does not build its heartbeat client from a budget derived from h.cfg.HeartbeatInterval")
	}
	if !strings.Contains(s, "if budget >= interval") {
		t.Errorf("heartbeat.go has no clamp keeping the budget strictly under the interval")
	}

	cfg, cerr := os.ReadFile("../config/config.go")
	if cerr != nil {
		t.Errorf("cannot read ../config/config.go to check the beat default: %v", cerr)
		return
	}
	if !strings.Contains(string(cfg), "HeartbeatInterval: 15 * time.Second") {
		t.Errorf("config/config.go beat default is not 15 s — the budget arithmetic (under the beat, over the 12 s slow-but-alive case) assumed 15 s")
	}
}
