package executor

// NODE-RETRY witness — the node-token endpoint must re-derive its bind set the
// INSTANT the executor reports a posture network it is about to attach to, not
// on the next probeLoop round (tokenRebindProbe = 5s).
//
// Hermetic: stubGateways + stubMinter from tokenserver_test.go; no docker, no
// daemon. The symptom it pins (CE run 19, tasks 67/68): the first node
// container scheduled onto mod_stack-api-net (created ON DEMAND by
// executor.go ensureAttachableNetwork) got a connection-refused on
// MOD_TASK_TOKEN_URL -> mod_node_auth raised -> container exited 1 -> API
// requeued -> retry ~10s later succeeded, because probeLoop had rebound the
// gateway by then. Doubling cold-start wall clock.
//
// STATIC-CHECK ONLY per the lane rule for Go-worker fixes: this file is
// compiled (go build), never executed here.

import (
	"io"
	"log/slog"
	"strings"
	"testing"
)

func TestNodeRetry_NoteNetworkRebindsWithoutProbeRound(t *testing.T) {
	gw := &stubGateways{gws: map[string]string{
		"mod_modtex-network": "198.51.100.1",
		"mod-user-egress":    "198.51.100.1",
		"mod-user-internal":  "198.51.100.1",
	}}
	ts := NewTokenServer(&stubMinter{token: "t", exp: 1}, slog.New(slog.NewTextHandler(io.Discard, nil)), bindAuto, "19179", "mod_modtex-network")
	ts.SetDockerGateways(gw)
	defer ts.Close()

	_, pending := ts.bindAddrs()
	if !pending {
		t.Error("stack-posture net missing at start -> expected pending=true")
	}

	// The net appears mid-task. The note must trigger a FRESH derivation
	// synchronously — proven by the gateway probe for that name happening
	// during NoteNetwork, before any caller asks bindAddrs again.
	gw.gws["mod_stack-api-net"] = "198.51.100.1"
	before := len(gw.calls)
	ts.NoteNetwork("mod_stack-api-net")
	if len(gw.calls) <= before {
		t.Error("NoteNetwork must re-derive the bind set immediately (no probe round)")
	}

	addrs, pending := ts.bindAddrs()
	assertNarrowBind(t, addrs)
	if pending {
		t.Error("expected pending=false once the net resolves")
	}
	if !containsHostPort(addrs, "198.51.100.1:19179") {
		t.Errorf("new gateway must land in the bind set; got %v", addrs)
	}
}

func TestNodeRetry_NoteNetworkIsIdempotent(t *testing.T) {
	gw := &stubGateways{gws: map[string]string{
		"mod_modtex-network": "198.51.100.1",
		"mod-user-egress":    "198.51.100.1",
		"mod-user-internal":  "198.51.100.1",
		"mod_stack-api-net":  "198.51.100.1",
	}}
	ts := NewTokenServer(&stubMinter{token: "t", exp: 1}, slog.New(slog.NewTextHandler(io.Discard, nil)), bindAuto, "19179", "mod_modtex-network")
	ts.SetDockerGateways(gw)
	defer ts.Close()

	ts.Start()
	for i := 0; i < 3; i++ {
		ts.NoteNetwork("mod_stack-api-net") // every task calls the hook
	}
	// rebind keeps already-live addresses; a repeat note must never retire them
	// and must never widen the bind to a wildcard.
	for _, a := range ts.currentAddrs() {
		if a == "0.0.0.0:19179" || strings.HasPrefix(a, "0.0.0.0:") {
			t.Errorf("repeat notes widened the bind: %v", ts.currentAddrs())
		}
	}
}

func TestNodeRetry_NoteNetworkNoOpUnderExplicitBind(t *testing.T) {
	// k8s/explicit bind ("::") has nothing to derive: the note must not probe
	// gateways and must not touch the configured bind.
	gw := &stubGateways{gws: map[string]string{"mod_modtex-network": "198.51.100.1"}}
	ts := NewTokenServer(&stubMinter{token: "t", exp: 1}, slog.New(slog.NewTextHandler(io.Discard, nil)), "::", "19179", "mod_modtex-network")
	ts.SetDockerGateways(gw)
	defer ts.Close()

	ts.NoteNetwork("mod_stack-api-net")
	if len(gw.calls) != 0 {
		t.Errorf("explicit bind must skip gateway derivation; got %d probes", len(gw.calls))
	}
}
