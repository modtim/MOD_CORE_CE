package executor

// gw-stack-networking GW.2 — unit tests for the host-port allocation and
// bound-port extraction. Pure: no docker daemon, no executor — the pure core
// (allocateHostPorts / renderStackHostPorts / parseHostPortRange) plus the
// port-extraction shape HostBoundPorts builds from daemon list entries.

import (
	"errors"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func TestAllocateHostPorts_FirstFreeIsLowest(t *testing.T) {
	lo, hi := 20000, 29999
	got, err := allocateHostPorts(lo, hi, "tcp", nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != 20000 {
		t.Fatalf("want 20000, got %d", got)
	}
}

func TestAllocateHostPorts_SkipsBoundAndReserved(t *testing.T) {
	blocked := map[string]map[int]bool{
		"tcp": {20000: true, 20001: true},
		"udp": {20000: true},
	}
	reserved := map[int]bool{20002: true}
	// tcp: 20000/20001 bound, 20002 reserved -> 20003
	got, err := allocateHostPorts(20000, 29999, "tcp", blocked, reserved)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != 20003 {
		t.Fatalf("want 20003, got %d", got)
	}
	// udp: 20000 bound only, 20002 still reserved -> 20001
	got, err = allocateHostPorts(20000, 29999, "udp", blocked, reserved)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != 20001 {
		t.Fatalf("want 20001, got %d", got)
	}
}

func TestAllocateHostPorts_ProtocolsAreIndependent(t *testing.T) {
	// A tcp block must not block udp, and vice versa.
	blocked := map[string]map[int]bool{"tcp": {20000: true}}
	got, err := allocateHostPorts(20000, 29999, "udp", blocked, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != 20000 {
		t.Fatalf("udp should not be blocked by tcp: got %d, want 20000", got)
	}
}

func TestAllocateHostPorts_ExhaustionErrors(t *testing.T) {
	blocked := map[string]map[int]bool{"tcp": {20000: true, 20001: true}}
	_, err := allocateHostPorts(20000, 20001, "tcp", blocked, nil)
	if err == nil {
		t.Fatal("expected exhaustion error, got nil")
	}
	if !errors.Is(err, errHostPortRangeExhausted) {
		t.Fatalf("want errHostPortRangeExhausted, got %v", err)
	}
	// Exhaustion via reservation (the two-concurrent-deploys race).
	reserved := map[int]bool{20000: true, 20001: true}
	if _, err := allocateHostPorts(20000, 20001, "tcp", nil, reserved); !errors.Is(err, errHostPortRangeExhausted) {
		t.Fatalf("want exhaustion via reservation, got %v", err)
	}
}

func TestParseHostPortRange(t *testing.T) {
	cases := []struct {
		in   string
		lo   int
		hi   int
		want bool
	}{
		{"20000-29999", 20000, 29999, true},
		{"  1000 - 2000 ", 1000, 2000, true},
		{"0-100", 0, 0, false},
		{"300-200", 0, 0, false},
		{"abc-def", 0, 0, false},
		{"100", 0, 0, false},
		{"1-99999", 0, 0, false},
	}
	for _, c := range cases {
		lo, hi, err := parseHostPortRange(c.in)
		if c.want {
			if err != nil || lo != c.lo || hi != c.hi {
				t.Errorf("parseHostPortRange(%q) = %d,%d,%v; want %d,%d,nil",
					c.in, lo, hi, err, c.lo, c.hi)
			}
		} else if err == nil {
			t.Errorf("parseHostPortRange(%q) = %d,%d,nil; want error", c.in, lo, hi)
		}
	}
}

const hostPortSpec = `
services:
  api:
    image: nginx
    ports:
      - "8000/tcp"
      - "9000/tcp"
  db:
    image: postgres
x-mod:
  ports:
    - {service: api, target: 8000, protocol: tcp}
    - {service: db, target: 5432, protocol: tcp}
  host_ports:
    - {service: api, target: 8000, host: any, protocol: tcp}
    - {service: api, target: 9000, host: 9100, protocol: tcp}
`

func TestRenderStackHostPorts_AllocatesAnyAndKeepsFixed(t *testing.T) {
	spec, allocs, err := renderStackHostPorts(hostPortSpec, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(allocs) != 1 {
		t.Fatalf("want 1 allocation (the 'any' entry), got %d: %+v", len(allocs), allocs)
	}
	a := allocs[0]
	if a.Service != "api" || a.Target != 8000 || a.Protocol != "tcp" {
		t.Fatalf("wrong allocation: %+v", a)
	}
	if a.Host < 20000 || a.Host > 29999 {
		t.Fatalf("allocation outside range: %d", a.Host)
	}

	// The rendered spec: `any` entry now carries the concrete host port...
	if got := portStringFor(spec, "api", 8000); got != "0.0.0.0:"+itoa(a.Host)+":8000/tcp" {
		t.Fatalf("api:8000 publish = %q, want %q", got, "0.0.0.0:"+itoa(a.Host)+":8000/tcp")
	}
	// ...the fixed entry is UNCHANGED...
	if got := portStringFor(spec, "api", 9000); got != "9100:9000/tcp" {
		t.Fatalf("api:9000 publish = %q, want unchanged 9100:9000/tcp", got)
	}
	// ...and the full record is written to x-mod.allocated_host_ports.
	if got := portStringFor(spec, "db", 5432); got != "" {
		t.Fatalf("db must not gain a host port: %q", got)
	}
}

func TestRenderStackHostPorts_RecordMakesItDeterministic(t *testing.T) {
	// First render allocates; feed the result back through a SECOND render
	// (a redeploy on the same host) — the allocation must be stable: the
	// API persisted it as x-mod.allocated_host_ports, so no new allocation
	// happens and the publish keeps the same port.
	spec1, allocs1, err := renderStackHostPorts(hostPortSpec, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("first render: %v", err)
	}
	first := allocs1[0].Host
	spec2, allocs2, err := renderStackHostPorts(spec1, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("second render: %v", err)
	}
	if len(allocs2) != 0 {
		t.Fatalf("redeploy must not re-allocate: %+v", allocs2)
	}
	if got := portStringFor(spec2, "api", 8000); got != "0.0.0.0:"+itoa(first)+":8000/tcp" {
		t.Fatalf("port drifted across redeploys: %q (first was %d)", got, first)
	}
}

func TestRenderStackHostPorts_AvoidsOtherAllocations(t *testing.T) {
	// Two `any` entries on one deploy: the second must not get the port the
	// first one took (renderStackHostPorts folds each allocation into the
	// record before the next entry reads it).
	two := `
services:
  api:
    image: nginx
    ports:
      - "8000/tcp"
      - "9000/tcp"
x-mod:
  host_ports:
    - {service: api, target: 8000, host: any, protocol: tcp}
    - {service: api, target: 9000, host: any, protocol: tcp}
`
	spec, allocs, err := renderStackHostPorts(two, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(allocs) != 2 {
		t.Fatalf("want 2 allocations, got %d: %+v", len(allocs), allocs)
	}
	if allocs[0].Host == allocs[1].Host {
		t.Fatalf("two 'any' entries got the same port %d", allocs[0].Host)
	}
	if allocs[0].Host+1 != allocs[1].Host {
		t.Fatalf("want consecutive 20000/20001, got %d/%d", allocs[0].Host, allocs[1].Host)
	}
	if portStringFor(spec, "api", 8000) == portStringFor(spec, "api", 9000) {
		t.Fatal("two services published on the same host port")
	}
}

func TestRenderStackHostPorts_FixedCollisionFails(t *testing.T) {
	blocked := map[string]map[int]bool{"tcp": {9100: true}}
	_, _, err := renderStackHostPorts(hostPortSpec, "modstack1g0", 20000, 29999, blocked, nil, nil)
	if err == nil {
		t.Fatal("fixed port bound by another project must fail the deploy")
	}
	if err != nil && !strContains(err.Error(), "already bound") {
		t.Fatalf("error should name the collision: %v", err)
	}
	// The SAME collision against the project's OWN running containers is a
	// redeploy (compose stops the old ones first) — must NOT fail.
	own := map[string]map[int]bool{"tcp": {9100: true}}
	if _, _, err := renderStackHostPorts(hostPortSpec, "modstack1g0", 20000, 29999, blocked, own, nil); err != nil {
		t.Fatalf("own-container collision (redeploy) must pass: %v", err)
	}
}

func TestRenderStackHostPorts_NoHostPortsIsNoop(t *testing.T) {
	plain := "services:\n  api:\n    image: nginx\n    ports:\n      - \"8000/tcp\"\n"
	spec, allocs, err := renderStackHostPorts(plain, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(allocs) != 0 || spec != plain {
		t.Fatalf("spec without host_ports must pass through untouched (allocs=%+v, changed=%v)",
			allocs, spec != plain)
	}
}

func TestRenderStackHostPorts_RangeExhaustion(t *testing.T) {
	blocked := map[string]map[int]bool{"tcp": {20000: true, 20001: true}}
	_, _, err := renderStackHostPorts(hostPortSpec, "modstack1g0", 20000, 20001, blocked, nil, nil)
	if !errors.Is(err, errHostPortRangeExhausted) {
		t.Fatalf("want exhaustion error, got %v", err)
	}
}

func TestHasNoHostPorts(t *testing.T) {
	if hasNoHostPorts(hostPortSpec) {
		t.Fatal("spec with host_ports must not fast-path")
	}
	if !hasNoHostPorts("services:\n  api:\n    image: nginx\n") {
		t.Fatal("spec without host_ports must fast-path")
	}
}

// B76b: the API renders the publish onto a netns holder
// (`network_mode: service:<carrier>`), which docker rejects outright
// ("conflicting options: port publishing and the container type network mode").
// The worker must move it to the carrier instead.
const netnsCarrierSpec = `
services:
  web:
    image: nginx
    network_mode: service:ts-web-net
  ts-web-net:
    image: sleep
    ports:
      - "8080/tcp"
x-mod:
  host_ports:
    - {service: web, target: 8080, host: 20002, protocol: tcp, bind: 127.0.0.1}
`

func TestRenderStackHostPorts_NetnsHolderPublishLandsOnCarrier(t *testing.T) {
	spec, allocs, err := renderStackHostPorts(netnsCarrierSpec, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(allocs) != 0 {
		t.Fatalf("fixed host port must not allocate: %+v", allocs)
	}
	// The holder keeps NO publish — that is what docker refuses otherwise.
	if got := portStringFor(spec, "web", 8080); got != "" {
		t.Fatalf("netns holder must not carry a publish: %q", got)
	}
	// The carrier's existing 8080/tcp entry is rewritten IN PLACE: one entry,
	// never a duplicate appended next to it.
	if got := portStringFor(spec, "ts-web-net", 8080); got != "127.0.0.1:20002:8080/tcp" {
		t.Fatalf("carrier publish = %q, want 127.0.0.1:20002:8080/tcp", got)
	}
	if !strContains(spec, "network_mode: service:ts-web-net") {
		t.Fatalf("the holder's network_mode must survive the rewrite: %q", spec)
	}
}

func TestRenderStackHostPorts_NetnsHolderCarrierWithoutPortsGainsOne(t *testing.T) {
	// A hand-edited spec can lose the carrier's ports list; the defensive
	// append must land on the carrier, never on the holder.
	none := `
services:
  web:
    image: nginx
    network_mode: service:ts-web-net
  ts-web-net:
    image: sleep
x-mod:
  host_ports:
    - {service: web, target: 8080, host: any, protocol: tcp}
`
	spec, allocs, err := renderStackHostPorts(none, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(allocs) != 1 || allocs[0].Host != 20000 {
		t.Fatalf("want one allocation on 20000, got %+v", allocs)
	}
	if got := portStringFor(spec, "web", 8080); got != "" {
		t.Fatalf("netns holder must not carry a publish: %q", got)
	}
	if got := portStringFor(spec, "ts-web-net", 8080); got != "0.0.0.0:20000:8080/tcp" {
		t.Fatalf("carrier gained %q, want 0.0.0.0:20000:8080/tcp", got)
	}
}

func TestRenderStackHostPorts_NetnsMissingCarrierPublishesNowhere(t *testing.T) {
	// The chain dead-ends at a carrier the spec never declares: publish
	// nowhere, and do NOT burn a port from the range (the plain sibling still
	// gets the lowest one).
	gap := `
services:
  web:
    image: nginx
    network_mode: service:ts-gone
  api:
    image: nginx
    ports:
      - "8000/tcp"
x-mod:
  host_ports:
    - {service: web, target: 8080, host: any, protocol: tcp}
    - {service: api, target: 8000, host: any, protocol: tcp}
`
	spec, allocs, err := renderStackHostPorts(gap, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(allocs) != 1 || allocs[0].Host != 20000 || allocs[0].Service != "api" {
		t.Fatalf("only the plain sibling may allocate: %+v", allocs)
	}
	if got := portStringFor(spec, "web", 8080); got != "" {
		t.Fatalf("skipped item must publish nowhere: %q", got)
	}
	if got := portStringFor(spec, "api", 8000); got != "0.0.0.0:20000:8000/tcp" {
		t.Fatalf("plain sibling publish = %q, want 0.0.0.0:20000:8000/tcp", got)
	}
}

func TestRenderStackHostPorts_NetnsChainHopsToTheFinalCarrier(t *testing.T) {
	// A carrier that is itself a netns holder (web -> mid -> base) must follow
	// the chain to the service that owns the container type.
	chain := `
services:
  web:
    image: nginx
    network_mode: service:mid
  mid:
    image: mid
    network_mode: service:base
  base:
    image: base
    ports:
      - "8080/tcp"
x-mod:
  host_ports:
    - {service: web, target: 8080, host: 20002, protocol: tcp}
`
	spec, allocs, err := renderStackHostPorts(chain, "modstack1g0", 20000, 29999, nil, nil, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(allocs) != 0 {
		t.Fatalf("fixed host port must not allocate: %+v", allocs)
	}
	if got := portStringFor(spec, "web", 8080); got != "" {
		t.Fatalf("holder web must not carry a publish: %q", got)
	}
	if got := portStringFor(spec, "mid", 8080); got != "" {
		t.Fatalf("holder mid must not carry a publish: %q", got)
	}
	if got := portStringFor(spec, "base", 8080); got != "20002:8080/tcp" {
		t.Fatalf("final carrier publish = %q, want 20002:8080/tcp", got)
	}
}

func TestFormatStackHostPortSentinel(t *testing.T) {
	got := formatStackHostPortSentinel("modstack1g0", []hostPortMapping{
		{Service: "api", Target: 8000, Host: 20000, Protocol: "tcp"},
	})
	if got == "" {
		t.Fatal("sentinel empty")
	}
	for _, needle := range []string{stackHostPortSentinelBegin, stackHostPortSentinelEnd,
		`"project":"modstack1g0"`, `"service":"api"`, `"host":20000`} {
		if !strContains(got, needle) {
			t.Fatalf("sentinel missing %q: %s", needle, got)
		}
	}
	if formatStackHostPortSentinel("p", nil) != "" {
		t.Fatal("no allocations must produce no sentinel")
	}
}

// ---------------------------------------------------------------------------
// bound-port extraction — the pure shape HostBoundPorts builds.
// ---------------------------------------------------------------------------

func TestBoundHostPortsShape(t *testing.T) {
	// The same port bookkeeping HostBoundPorts applies to a daemon list:
	// running containers, PublicPort 0 = expose-only (skipped), protocol
	// split, per-project attribution.
	bound := map[string]map[int]bool{}
	boundByProject := map[string]map[string]map[int]bool{}
	addPort := func(project string, pub int, proto string) {
		if pub == 0 {
			return
		}
		if proto == "" {
			proto = "tcp"
		}
		if bound[proto] == nil {
			bound[proto] = map[int]bool{}
		}
		bound[proto][pub] = true
		if boundByProject[project] == nil {
			boundByProject[project] = map[string]map[int]bool{}
		}
		if boundByProject[project][proto] == nil {
			boundByProject[project][proto] = map[int]bool{}
		}
		boundByProject[project][proto][pub] = true
	}
	addPort("modstack1g0", 8080, "tcp")
	addPort("modstack1g0", 20000, "udp")
	addPort("modstack1g0", 0, "tcp") // expose-only — skipped
	addPort("", 9100, "tcp")         // non-stack container

	if len(bound["tcp"]) != 2 || !bound["tcp"][8080] || !bound["tcp"][9100] {
		t.Fatalf("tcp bound set wrong: %v", bound["tcp"])
	}
	if len(bound["udp"]) != 1 || !bound["udp"][20000] {
		t.Fatalf("udp bound set wrong: %v", bound["udp"])
	}
	if !boundByProject["modstack1g0"]["udp"][20000] {
		t.Fatal("per-project attribution lost the stack's udp port")
	}
	if boundByProject["modstack1g0"]["tcp"][9100] {
		t.Fatal("9100 belongs to a non-stack container, not the project")
	}
}

func TestReservationLedger(t *testing.T) {
	releaseStackHostPorts("t1")
	releaseStackHostPorts("t2")
	reserveStackHostPorts("t1", []int{20000, 20001})
	reserveStackHostPorts("t2", []int{20002})
	union := reservedHostPortsUnion()
	if !union[20000] || !union[20001] || !union[20002] {
		t.Fatalf("union missing reservations: %v", union)
	}
	releaseStackHostPorts("t1")
	union = reservedHostPortsUnion()
	if union[20000] || union[20001] {
		t.Fatalf("released ports still reserved: %v", union)
	}
	if !union[20002] {
		t.Fatal("unrelated project's reservation lost")
	}
	releaseStackHostPorts("t2")
}

// --- tiny local helpers ----------------------------------------------------

// strContains is a substring check (the package-level `contains` is
// []string-based, see stack_replica_fanout_test.go).
func strContains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}

// portStringFor finds the ports entry on a service that publishes `target`
// and returns its raw YAML scalar ("" when absent).
func portStringFor(spec string, svc string, target int) string {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &root); err != nil || len(root.Content) == 0 {
		return ""
	}
	svcs := mapGet(root.Content[0], "services")
	if svcs == nil {
		return ""
	}
	svcNode := mapGet(svcs, svc)
	if svcNode == nil {
		return ""
	}
	ports := mapGet(svcNode, "ports")
	if ports == nil || ports.Kind != yaml.SequenceNode {
		return ""
	}
	for _, node := range ports.Content {
		if node.Kind == yaml.ScalarNode {
			s := strings.TrimSpace(node.Value)
			if t, ok := portTargetProto(s, "tcp"); ok && t == target {
				return s
			}
			if strings.Contains(s, ":") && strings.HasSuffix(s, itoa(target)+"/tcp") {
				return s
			}
		}
	}
	return ""
}
