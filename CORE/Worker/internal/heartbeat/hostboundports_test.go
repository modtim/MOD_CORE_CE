package heartbeat

// gw-stack-networking GW.2 — the heartbeat's host_bound_ports contract.
//
// The bug this pins: HostBoundPorts used to return nil when the daemon listed
// nothing, so once a worker's last publishing container stopped, the field was
// omitted on every subsequent beat and the manager kept its stale reading of
// that port forever. An EMPTY (non-nil) provider answer is a real "nothing
// bound" and must ride the beat as host_bound_ports: {}; only a nil answer
// ("could not say") is omitted.

import (
	"context"
	"encoding/json"
	"testing"
)

// TestSend_HostBoundPortsEmptyIsSent verifies an empty (non-nil) provider
// answer is transmitted as an empty JSON object, NOT omitted. This is the
// regression the live GPU1 outage hit: the manager must see {} to release a
// port whose container has stopped.
func TestSend_HostBoundPortsEmptyIsSent(t *testing.T) {
	srv, got := captureHeartbeatBody(t)
	defer srv.Close()

	h := newSendTestHeartbeat(srv)
	h.OnHostBoundPorts(func() map[string][]int { return map[string][]int{} })

	h.send(context.Background())

	v, ok := (*got)["host_bound_ports"]
	if !ok {
		t.Fatalf("expected host_bound_ports: {} in heartbeat payload (keys %#v); " +
			"an empty answer must be SENT, not omitted", *got)
	}
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal captured value: %v", err)
	}
	if string(b) != "{}" {
		t.Fatalf("host_bound_ports must serialise as {} (got %s)", b)
	}
}

// TestSend_HostBoundPortsNilIsOmitted verifies a nil provider answer ("could
// not say" — daemon failed) is OMITTED, so the manager carries its last
// reading forward instead of reading "no ports bound" off a wedged daemon.
func TestSend_HostBoundPortsNilIsOmitted(t *testing.T) {
	srv, got := captureHeartbeatBody(t)
	defer srv.Close()

	h := newSendTestHeartbeat(srv)
	h.OnHostBoundPorts(func() map[string][]int { return nil })

	h.send(context.Background())

	if _, ok := (*got)["host_bound_ports"]; ok {
		t.Fatal("host_bound_ports must be omitted when the provider answers nil")
	}
}

// TestSend_HostBoundPortsWithValuesSent verifies a populated answer rides the
// beat unchanged (the common case, sanity).
func TestSend_HostBoundPortsWithValuesSent(t *testing.T) {
	srv, got := captureHeartbeatBody(t)
	defer srv.Close()

	h := newSendTestHeartbeat(srv)
	h.OnHostBoundPorts(func() map[string][]int {
		return map[string][]int{"tcp": {8080, 20000}, "udp": {53}}
	})

	h.send(context.Background())

	v, ok := (*got)["host_bound_ports"]
	if !ok {
		t.Fatalf("expected host_bound_ports in payload, got keys %#v", *got)
	}
	m, ok := v.(map[string]interface{})
	if !ok {
		t.Fatalf("host_bound_ports not a map: %#v", v)
	}
	b, _ := json.Marshal(m["tcp"])
	if string(b) != "[8080,20000]" {
		t.Fatalf("tcp ports = %s, want [8080,20000]", b)
	}
}

// TestSend_HostBoundPortsByProject follows the same nil/empty contract for
// the per-project attribution field used by the manager's self-exclusion.
func TestSend_HostBoundPortsByProject(t *testing.T) {
	// populated -> sent
	srv, got := captureHeartbeatBody(t)
	h := newSendTestHeartbeat(srv)
	h.OnHostBoundPortsByProject(func() map[string]map[string][]int {
		return map[string]map[string][]int{
			"modstack1g0": {"tcp": {8080}},
			"":            {"tcp": {9100}},
		}
	})
	h.send(context.Background())
	if _, ok := (*got)["host_bound_ports_by_project"]; !ok {
		t.Fatalf("expected host_bound_ports_by_project in payload, got keys %#v", *got)
	}
	srv.Close()

	// empty non-nil -> sent as {}
	srv2, got2 := captureHeartbeatBody(t)
	h2 := newSendTestHeartbeat(srv2)
	h2.OnHostBoundPortsByProject(func() map[string]map[string][]int {
		return map[string]map[string][]int{}
	})
	h2.send(context.Background())
	if v, ok := (*got2)["host_bound_ports_by_project"]; !ok {
		t.Fatalf("empty by-project answer must be sent, keys %#v", *got2)
	} else if b, _ := json.Marshal(v); string(b) != "{}" {
		t.Fatalf("empty by-project must serialise as {} (got %s)", b)
	}
	srv2.Close()

	// nil -> omitted
	srv3, got3 := captureHeartbeatBody(t)
	h3 := newSendTestHeartbeat(srv3)
	h3.OnHostBoundPortsByProject(func() map[string]map[string][]int { return nil })
	h3.send(context.Background())
	if _, ok := (*got3)["host_bound_ports_by_project"]; ok {
		t.Fatal("host_bound_ports_by_project must be omitted when provider answers nil")
	}
	srv3.Close()
}
