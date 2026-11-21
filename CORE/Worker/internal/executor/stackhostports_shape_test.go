package executor

// gw-stack-networking GW.2 — the heartbeat wire shape for host-bound ports.
//
// The contract under test: "looked and found nothing" is a REAL answer and
// must reach the manager as an empty (non-nil) map, while a daemon error is
// the only path to nil ("could not say"). hostBoundPortsShape is the pure
// half of HostBoundPorts / HostBoundPortsByProject (the nil-on-error gate is
// the three lines of daemon call in between, covered here by exercising the
// shape the success path hands off).

import (
	"reflect"
	"testing"
)

func TestHostBoundPortsShape_EmptyInputIsEmptyNonNilMap(t *testing.T) {
	out := hostBoundPortsShape(map[string]map[int]bool{})
	if out == nil {
		t.Fatal("empty daemon list must yield a non-nil empty map, not nil: " +
			"nil is the 'daemon error' signal and would keep the manager's " +
			"stale reading of a stopped container's port forever")
	}
	if len(out) != 0 {
		t.Fatalf("expected empty map, got %v", out)
	}
}

func TestHostBoundPortsShape_SortsAndSplitsProtocols(t *testing.T) {
	bound := map[string]map[int]bool{
		"tcp": {20000: true, 8080: true},
		"udp": {53: true},
	}
	want := map[string][]int{"tcp": {8080, 20000}, "udp": {53}}
	if got := hostBoundPortsShape(bound); !reflect.DeepEqual(got, want) {
		t.Fatalf("shape = %v, want %v", got, want)
	}
	// An empty per-protocol set (defensive: caller filters these) still
	// serialises as an empty LIST, never a nil slice — the API sanitiser
	// treats both the same, but the list keeps the protocol legible.
	empty := map[string]map[int]bool{"tcp": {}}
	if got := hostBoundPortsShape(empty); !reflect.DeepEqual(got, map[string][]int{"tcp": {}}) {
		t.Fatalf("empty proto set = %v, want map[tcp:[]]", got)
	}
}

func TestHostBoundByProjectShape_EmptyInputIsEmptyNonNilMap(t *testing.T) {
	out := hostBoundByProjectShape(map[string]map[string]map[int]bool{})
	if out == nil {
		t.Fatal("empty daemon list must yield a non-nil empty map (same contract)")
	}
	if len(out) != 0 {
		t.Fatalf("expected empty map, got %v", out)
	}
}

func TestHostBoundByProjectShape_KeepsNonStackProject(t *testing.T) {
	// The "" key is a non-stack container: it MUST be kept — the manager
	// only special-cases the redeploying stack's own project.
	bp := map[string]map[string]map[int]bool{
		"modstack1g0": {"tcp": {8080: true, 20001: true}},
		"":            {"tcp": {9100: true}, "udp": {53: true}},
	}
	want := map[string]map[string][]int{
		"modstack1g0": {"tcp": {8080, 20001}},
		"":            {"tcp": {9100}, "udp": {53}},
	}
	if got := hostBoundByProjectShape(bp); !reflect.DeepEqual(got, want) {
		t.Fatalf("shape = %v, want %v", got, want)
	}
}
