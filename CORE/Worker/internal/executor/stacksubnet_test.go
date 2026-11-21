package executor

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

func cidr(t *testing.T, s string) *net.IPNet {
	t.Helper()
	_, n, err := net.ParseCIDR(s)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func TestAllocateSubnetFirstFreeBlock(t *testing.T) {
	pool := cidr(t, "10.208.0.0/12")
	got, err := allocateSubnet(pool, 24, nil)
	if err != nil || got.String() != "10.208.0.0/24" {
		t.Fatalf("got %v, %v", got, err)
	}
	avoid := []*net.IPNet{cidr(t, "10.208.0.0/23"), cidr(t, "10.208.2.128/25")}
	got, err = allocateSubnet(pool, 24, avoid)
	if err != nil || got.String() != "10.208.3.0/24" {
		t.Fatalf("got %v, %v", got, err)
	}
}

func TestAllocateSubnetAvoidsSupernet(t *testing.T) {
	// A /16 route covering the start of the pool pushes allocation past it.
	got, err := allocateSubnet(cidr(t, "10.208.0.0/12"), 24, []*net.IPNet{cidr(t, "10.208.0.0/16")})
	if err != nil || got.String() != "10.209.0.0/24" {
		t.Fatalf("got %v, %v", got, err)
	}
}

func TestAllocateSubnetExhausted(t *testing.T) {
	pool := cidr(t, "10.208.0.0/23")
	if _, err := allocateSubnet(pool, 24, []*net.IPNet{pool}); err == nil {
		t.Fatal("expected exhaustion error")
	}
	if _, err := allocateSubnet(pool, 16, nil); err == nil {
		t.Fatal("expected error splitting /23 into /16")
	}
}

func TestParseProcNetRoute(t *testing.T) {
	raw := "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n" +
		"eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n" +
		"eth0\t000200C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n" +
		"eth1\t007100CB\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
	got := parseProcNetRoute(raw)
	if len(got) != 2 || got[0].String() != "192.0.2.0/24" || got[1].String() != "203.0.113.0/24" {
		t.Fatalf("got %v", got)
	}
}

// 2026-09-28: a stack network's own bridge route, visible to the worker via
// its own routing table in this WSL/Docker-Desktop setup, self-matched as a
// "LAN" the network was shadowing — for both a MOD-pool subnet and an
// unrelated Docker-default one. parseProcNetRoute must not surface either
// docker0 or a compose-managed br-* interface as host-visible.
func TestParseProcNetRouteSkipsDockerBridges(t *testing.T) {
	raw := "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n" +
		"docker0\t000200C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n" + // 192.0.2.0/24, own default bridge (RFC 5737)
		"br-abcdef012345\t0000D00A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n" + // 10.208.0.0/24, own stack net
		"eth1\t007100CB\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n" // 203.0.113.0/24, a real interface
	got := parseProcNetRoute(raw)
	if len(got) != 1 || got[0].String() != "203.0.113.0/24" {
		t.Fatalf("got %v, want only the non-bridge route", got)
	}
}

func TestIsDockerBridgeIface(t *testing.T) {
	for iface, want := range map[string]bool{
		"docker0":         true,
		"br-abcdef012345": true,
		"eth0":            false,
		"bridge0":         false, // must not fuzzy-match "br" as a prefix of anything
		"tun0":            false,
	} {
		if got := isDockerBridgeIface(iface); got != want {
			t.Fatalf("isDockerBridgeIface(%q) = %v, want %v", iface, got, want)
		}
	}
}

func TestParseNetworkSubnets(t *testing.T) {
	got := parseNetworkSubnets("bridge|192.0.2.0/24 \nnone|\nmodstack22g0_mod-stack-egress|10.204.22.0/24 fd00::/64 \n")
	if len(got["bridge"]) != 1 || got["bridge"][0].String() != "192.0.2.0/24" {
		t.Fatalf("bridge: %v", got["bridge"])
	}
	if _, ok := got["none"]; !ok || len(got["none"]) != 0 {
		t.Fatalf("none: %v", got["none"])
	}
	if len(got["modstack22g0_mod-stack-egress"]) != 1 {
		t.Fatalf("v6 should be skipped: %v", got["modstack22g0_mod-stack-egress"])
	}
}

// subnetsOf decodes the rewritten YAML and returns networks.<key>.ipam subnet.
func subnetsOf(t *testing.T, y string) map[string]string {
	t.Helper()
	var doc struct {
		Networks map[string]struct {
			Ipam struct {
				Config []struct {
					Subnet string `yaml:"subnet"`
				} `yaml:"config"`
			} `yaml:"ipam"`
		} `yaml:"networks"`
	}
	if err := yaml.Unmarshal([]byte(y), &doc); err != nil {
		t.Fatalf("rewritten yaml does not parse: %v\n%s", err, y)
	}
	out := map[string]string{}
	for k, v := range doc.Networks {
		if len(v.Ipam.Config) > 0 {
			out[k] = v.Ipam.Config[0].Subnet
		}
	}
	return out
}

const pinSpec = `services:
  llama-server:
    image: x
    networks: [mod-stack-isolated]
    environment:
      FLAG: "on"
  ingress-llama-server:
    image: y
    ports: ["8000:8000"]
    networks:
      mod-stack-isolated:
      mod-stack-egress:
        aliases: [edge]
  sidecar:
    image: z
    network_mode: service:llama-server
networks:
  mod-stack-isolated:
    driver: bridge
    internal: true
  mod-stack-egress:
    driver: bridge
`

func TestPinStackSubnetsListAndMapForms(t *testing.T) {
	out, plans, warns, err := pinStackSubnetsYAML(pinSpec, "modstack14g0", cidr(t, "10.208.0.0/12"), nil, nil)
	if err != nil || len(warns) != 0 {
		t.Fatalf("err=%v warns=%v", err, warns)
	}
	subs := subnetsOf(t, out)
	if subs["mod-stack-egress"] != "10.208.0.0/24" || subs["mod-stack-isolated"] != "10.208.1.0/24" {
		t.Fatalf("subnets %v\n%s", subs, out)
	}
	if len(plans) != 2 || plans[0].Name != "modstack14g0_mod-stack-egress" {
		t.Fatalf("plans %+v", plans)
	}
	// The rest of the spec survives: internal flag, aliases, quoted "on".
	for _, want := range []string{"internal: true", "aliases: [edge]", `"on"`, "network_mode: service:llama-server"} {
		if !strings.Contains(out, want) {
			t.Fatalf("lost %q in\n%s", want, out)
		}
	}
}

func TestPinStackSubnetsImplicitDefaultNetwork(t *testing.T) {
	spec := "services:\n  app:\n    image: x\n    ports: [\"4000:4000\"]\n"
	out, _, _, err := pinStackSubnetsYAML(spec, "modstack22g0", cidr(t, "10.208.0.0/12"), nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if subnetsOf(t, out)["default"] != "10.208.0.0/24" {
		t.Fatalf("default not pinned:\n%s", out)
	}
}

func TestPinStackSubnetsNoNetworksAllNetworkMode(t *testing.T) {
	spec := "services:\n  app:\n    image: x\n    network_mode: host\n"
	out, plans, _, err := pinStackSubnetsYAML(spec, "p", cidr(t, "10.208.0.0/12"), nil, nil)
	if err != nil || out != spec || len(plans) != 0 {
		t.Fatalf("expected untouched: %v %v\n%s", err, plans, out)
	}
}

func TestPinStackSubnetsSkipsExternalAndExplicitIpam(t *testing.T) {
	spec := `services:
  app:
    image: x
    networks: [mod_modtex-network, pinned, legacy]
networks:
  mod_modtex-network:
    external: true
  legacy:
    external:
      name: old-net
  pinned:
    ipam:
      config:
        - subnet: 10.99.0.0/24
`
	out, plans, _, err := pinStackSubnetsYAML(spec, "p", cidr(t, "10.208.0.0/12"), nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if out != spec {
		t.Fatalf("spec should be unchanged:\n%s", out)
	}
	reasons := map[string]string{}
	for _, p := range plans {
		reasons[p.Key] = p.Reason
	}
	if reasons["mod_modtex-network"] != "external" || reasons["legacy"] != "external" || reasons["pinned"] != "explicit ipam" {
		t.Fatalf("reasons %v", reasons)
	}
}

func TestShadowsLAN(t *testing.T) {
	pool := cidr(t, "10.208.0.0/12")
	// Inside our own pool: never a shadow, regardless of avoid.
	if shadowsLAN(cidr(t, "10.208.0.0/24"), pool, []*net.IPNet{cidr(t, "10.208.0.0/24")}) {
		t.Fatal("in-pool subnet must never be reported as shadowing")
	}
	// Genuine 192.168/16 overlap (the whole RFC1918 range, same constant
	// lanShadowRanges carries, so the fixture names no concrete LAN sub-range).
	if !shadowsLAN(cidr(t, "192.168.0.0/16"), pool, nil) {
		t.Fatal("192.168/16 overlap must still warn")
	}
	// Genuine MOD_STACK_NET_AVOID-style overlap outside the pool.
	if !shadowsLAN(cidr(t, "172.20.0.0/16"), pool, []*net.IPNet{cidr(t, "172.20.5.0/24")}) {
		t.Fatal("explicit avoid overlap outside the pool must still warn")
	}
	// Outside the pool, nothing to avoid: no warn.
	if shadowsLAN(cidr(t, "172.20.0.0/16"), pool, nil) {
		t.Fatal("unrelated out-of-pool subnet with nothing to avoid must not warn")
	}
}

// A. The bug fixed here: stack 30's second deploy re-created both its
// networks because pinStackSubnetsYAML left an already-existing, already-MOD-
// pool-pinned network with no ipam block at all — a different config than
// what it was created with — so compose's hash mismatched and it deleted and
// recreated the network (breaking the other containers' embedded-DNS
// resolution). An existing network whose live subnet lies inside our pool
// must get the IDENTICAL ipam re-emitted.
func TestPinStackSubnetsRepinsExistingInPoolNetwork(t *testing.T) {
	existing := map[string][]*net.IPNet{
		"modstack30g0_mod-stack-isolated": {cidr(t, "10.208.0.0/24")},
		"modstack30g0_mod-stack-egress":   {cidr(t, "10.208.1.0/24")},
	}
	out, plans, warns, err := pinStackSubnetsYAML(pinSpec, "modstack30g0", cidr(t, "10.208.0.0/12"), existing, nil)
	if err != nil {
		t.Fatalf("err=%v", err)
	}
	if len(warns) != 0 {
		t.Fatalf("an in-pool subnet must never warn: %v", warns)
	}
	subs := subnetsOf(t, out)
	if subs["mod-stack-isolated"] != "10.208.0.0/24" || subs["mod-stack-egress"] != "10.208.1.0/24" {
		t.Fatalf("re-pinned subnets do not match what the networks were created with: %v", subs)
	}
	for _, p := range plans {
		if p.Reason != "already exists, re-pinned" {
			t.Fatalf("plan %+v: expected re-pinned", p)
		}
	}
}

// An existing network outside the pool (created before pinning existed, or
// by Docker's own default pool) must still be left alone, not "fixed" onto a
// pool subnet — that would just cause the same recreate this feature exists
// to prevent, now for a network nothing was ever wrong with.
func TestPinStackSubnetsLeavesOutOfPoolNetworkAlone(t *testing.T) {
	existing := map[string][]*net.IPNet{
		"modstack30g0_mod-stack-isolated": {cidr(t, "172.28.0.0/16")},
		"modstack30g0_mod-stack-egress":   {cidr(t, "172.29.0.0/16")},
	}
	out, plans, _, err := pinStackSubnetsYAML(pinSpec, "modstack30g0", cidr(t, "10.208.0.0/12"), existing, nil)
	if err != nil {
		t.Fatalf("err=%v", err)
	}
	subs := subnetsOf(t, out)
	if len(subs) != 0 {
		t.Fatalf("out-of-pool existing networks must get no ipam: %v", subs)
	}
	for _, p := range plans {
		if p.Reason != "already exists" {
			t.Fatalf("plan %+v: expected plain already-exists", p)
		}
	}
}

func TestPinStackSubnetsReusesExistingNetwork(t *testing.T) {
	existing := map[string][]*net.IPNet{
		"modstack14g0_mod-stack-egress": {cidr(t, "10.204.22.0/24")},
		"other":                         {cidr(t, "10.208.0.0/24")},
	}
	out, plans, warns, err := pinStackSubnetsYAML(pinSpec, "modstack14g0", cidr(t, "10.208.0.0/12"), existing, nil)
	if err != nil || len(warns) != 0 {
		t.Fatalf("err=%v warns=%v", err, warns)
	}
	subs := subnetsOf(t, out)
	if _, ok := subs["mod-stack-egress"]; ok {
		t.Fatalf("existing network must not get ipam (would force compose to recreate it):\n%s", out)
	}
	// Other docker networks' subnets are avoided.
	if subs["mod-stack-isolated"] != "10.208.1.0/24" {
		t.Fatalf("isolated got %v", subs)
	}
	if plans[0].Reason != "already exists" {
		t.Fatalf("plans %+v", plans)
	}
}

func TestPinStackSubnetsRepinsLANShadowingExisting(t *testing.T) {
	existing := map[string][]*net.IPNet{"modstack22g0_mod-stack-egress": {cidr(t, "192.168.0.0/16")}}
	spec := "services:\n  a:\n    image: x\n    networks: [mod-stack-egress]\nnetworks:\n  mod-stack-egress: {}\n"
	out, plans, warns, err := pinStackSubnetsYAML(spec, "modstack22g0", cidr(t, "10.208.0.0/12"), existing, nil)
	if err != nil || len(warns) != 1 || !strings.Contains(warns[0], "192.168.0.0/16") {
		t.Fatalf("err=%v warns=%v", err, warns)
	}
	// The shadowing network gets a pool subnet so compose recreates it off the LAN.
	if got := subnetsOf(t, out)["mod-stack-egress"]; got != "10.208.0.0/24" {
		t.Fatalf("LAN-shadowing network must be re-pinned into the pool, got %q:\n%s", got, out)
	}
	if len(plans) != 1 || plans[0].Reason != "LAN-shadowing, re-pinned" || plans[0].Subnet != "10.208.0.0/24" {
		t.Fatalf("plans %+v", plans)
	}
	// Docker's own 172.x default pool is not warned about unless it overlaps a
	// subnet the worker can see.
	existing = map[string][]*net.IPNet{"modstack22g0_mod-stack-egress": {cidr(t, "172.20.0.0/16")}}
	_, _, warns, _ = pinStackSubnetsYAML(spec, "modstack22g0", cidr(t, "10.208.0.0/12"), existing, nil)
	if len(warns) != 0 {
		t.Fatalf("unexpected warns %v", warns)
	}
	_, _, warns, _ = pinStackSubnetsYAML(spec, "modstack22g0", cidr(t, "10.208.0.0/12"), existing,
		[]*net.IPNet{cidr(t, "172.20.5.0/24")})
	if len(warns) != 1 {
		t.Fatalf("expected warn when overlapping a host-visible subnet, got %v", warns)
	}
}

func TestPinStackSubnetsNullNetworkEntry(t *testing.T) {
	spec := "services:\n  a:\n    image: x\n    networks: [n]\nnetworks:\n  n:\n"
	out, _, _, err := pinStackSubnetsYAML(spec, "p", cidr(t, "10.208.0.0/12"), nil, nil)
	if err != nil || subnetsOf(t, out)["n"] != "10.208.0.0/24" {
		t.Fatalf("err=%v\n%s", err, out)
	}
}

func TestPinStackSubnetsExecutorFallsBackOnDockerError(t *testing.T) {
	orig := dockerCLI
	defer func() { dockerCLI = orig }()
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		return "boom", context.DeadlineExceeded
	}
	var logged []string
	e := &Executor{}
	out := e.pinStackSubnets(context.Background(), Callbacks{OnLogBatch: func(l []string) { logged = append(logged, l...) }}, "p", pinSpec)
	if out != pinSpec {
		t.Fatal("spec must be unchanged when docker is unavailable")
	}
	if len(logged) != 1 || !strings.Contains(logged[0], "skipped") {
		t.Fatalf("logged %v", logged)
	}
}

func TestPinStackSubnetsExecutorReservesAcrossDeploys(t *testing.T) {
	orig := dockerCLI
	defer func() { dockerCLI = orig }()
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if args[1] == "ls" {
			return "abc\n", nil
		}
		return "bridge|192.0.2.0/24 \n", nil
	}
	t.Setenv("MOD_STACK_NET_POOL", "10.250.0.0/22")
	t.Setenv("MOD_STACK_NET_AVOID", "10.250.0.0/24")
	subnetMu.Lock()
	saved := subnetReserved
	subnetReserved = map[string]time.Time{}
	subnetMu.Unlock()
	defer func() { subnetMu.Lock(); subnetReserved = saved; subnetMu.Unlock() }()

	spec := "services:\n  a:\n    image: x\n    networks: [n]\nnetworks:\n  n: {}\n"
	e := &Executor{}
	first := subnetsOf(t, e.pinStackSubnets(context.Background(), Callbacks{}, "one", spec))["n"]
	// Docker still reports no such network (compose has not run yet), so only
	// the in-memory reservation keeps the second deploy off the same block.
	second := subnetsOf(t, e.pinStackSubnets(context.Background(), Callbacks{}, "two", spec))["n"]
	if first != "10.250.1.0/24" || second != "10.250.2.0/24" {
		t.Fatalf("first=%s second=%s", first, second)
	}
}
