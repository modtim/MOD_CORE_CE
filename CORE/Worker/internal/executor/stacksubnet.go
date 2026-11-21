package executor

// Pre-`compose up` subnet pinning for stack networks.
//
// Compose-managed stack networks normally take a subnet from the daemon's
// default address pools. When those run out, Docker (Desktop especially) falls
// back to 192.168.x/20 blocks, which can shadow a host's own LAN subnet: the
// stack network's bridge then captures packets for that whole private range and
// every container on the host loses LAN reach. A second network hit the same
// fallback the next day, so the outage was reproducible, not a one-off.
//
// So before compose runs, the worker gives every compose-managed network that
// has no explicit ipam subnet one from a MOD-owned pool (default
// 10.208.0.0/12 in /24s; MOD_STACK_NET_POOL overrides), avoiding every subnet
// already used by a docker network, every subnet the worker can see on its own
// interfaces/routes, and MOD_STACK_NET_AVOID. External networks and networks
// with an explicit ipam config are left alone. A network that already exists
// is also left alone — its subnet is kept as-is (adding ipam would change
// compose's config hash and make it recreate a live network) — unless that
// live subnet looks LAN-shadowing: then it is re-pinned from the pool on
// purpose, so compose recreates it off the LAN range (and a warning is logged).

import (
	"context"
	"fmt"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

const (
	defaultStackNetPool = "10.208.0.0/12"
	stackNetPrefixLen   = 24
	subnetReserveTTL    = 10 * time.Minute
)

// Subnets handed out but possibly not yet created by compose — keeps two
// concurrent deploys from picking the same block.
var (
	subnetMu       sync.Mutex
	subnetReserved = map[string]time.Time{}
)

// lanShadowRanges is where home/office LANs usually live; a live stack network
// inside it is worth a warning even when the worker cannot see the host LAN
// itself (Docker Desktop hides it from containers). 172.16.0.0/12 is not
// listed: Docker's own default pools live there, so it only warns when it
// overlaps a subnet the worker can actually see (or MOD_STACK_NET_AVOID).
var lanShadowRanges = mustCIDRs("192.168.0.0/16")

func mustCIDRs(cidrs ...string) []*net.IPNet {
	var out []*net.IPNet
	for _, c := range cidrs {
		_, n, err := net.ParseCIDR(c)
		if err != nil {
			panic(err)
		}
		out = append(out, n)
	}
	return out
}

func overlaps(a, b *net.IPNet) bool {
	return a.Contains(b.IP) || b.Contains(a.IP)
}

func overlapsAny(n *net.IPNet, set []*net.IPNet) bool {
	for _, s := range set {
		if overlaps(n, s) {
			return true
		}
	}
	return false
}

// shadowsLAN reports whether an already-existing stack network's subnet s is
// worth a LAN-shadow warning. A subnet inside our own pool is never a LAN —
// it is either something we pinned, or (harmlessly) a subnet Docker's default
// pools never hand out — so it is excluded unconditionally rather than relying
// on avoid/hostVisibleSubnets to happen to exclude it (see isDockerBridgeIface:
// on 2026-09-28 this network's own bridge route, visible to the worker via its
// own routing table, made both a 10.208.0.0/24 pool subnet AND an unrelated
// 172.28.0.0/16 Docker-default subnet falsely flag themselves as shadowing).
func shadowsLAN(s, pool *net.IPNet, avoid []*net.IPNet) bool {
	if pool.Contains(s.IP) {
		return false
	}
	return overlapsAny(s, avoid) || overlapsAny(s, lanShadowRanges)
}

// parseCIDRList parses a comma/space separated CIDR list, skipping junk and
// anything that is not IPv4.
func parseCIDRList(s string) []*net.IPNet {
	var out []*net.IPNet
	for _, f := range strings.FieldsFunc(s, func(r rune) bool { return r == ',' || r == ' ' || r == '\n' }) {
		if _, n, err := net.ParseCIDR(strings.TrimSpace(f)); err == nil && n.IP.To4() != nil {
			out = append(out, n)
		}
	}
	return out
}

// allocateSubnet returns the first /prefixLen block of pool that overlaps
// nothing in avoid.
func allocateSubnet(pool *net.IPNet, prefixLen int, avoid []*net.IPNet) (*net.IPNet, error) {
	ones, bits := pool.Mask.Size()
	if bits != 32 || prefixLen < ones || prefixLen > 30 {
		return nil, fmt.Errorf("pool %s cannot be split into /%d blocks", pool, prefixLen)
	}
	base := ipToUint(pool.IP.To4())
	step := uint32(1) << uint(32-prefixLen)
	count := uint32(1) << uint(prefixLen-ones)
	mask := net.CIDRMask(prefixLen, 32)
	for i := uint32(0); i < count; i++ {
		cand := &net.IPNet{IP: uintToIP(base + i*step), Mask: mask}
		if !overlapsAny(cand, avoid) {
			return cand, nil
		}
	}
	return nil, fmt.Errorf("stack network pool %s is exhausted", pool)
}

func ipToUint(ip net.IP) uint32 {
	return uint32(ip[0])<<24 | uint32(ip[1])<<16 | uint32(ip[2])<<8 | uint32(ip[3])
}

func uintToIP(v uint32) net.IP {
	return net.IPv4(byte(v>>24), byte(v>>16), byte(v>>8), byte(v)).To4()
}

// stackNetPool is the configured pool (MOD_STACK_NET_POOL) or the default.
func stackNetPool() *net.IPNet {
	if nets := parseCIDRList(os.Getenv("MOD_STACK_NET_POOL")); len(nets) > 0 {
		return nets[0]
	}
	_, n, _ := net.ParseCIDR(defaultStackNetPool)
	return n
}

// hostVisibleSubnets are the subnets on the worker's own interfaces and in its
// routing table (/proc/net/route) — what it can see of the host from inside
// its container.
func hostVisibleSubnets() []*net.IPNet {
	var out []*net.IPNet
	if ifaces, err := net.Interfaces(); err == nil {
		for _, ifc := range ifaces {
			if isDockerBridgeIface(ifc.Name) {
				continue
			}
			addrs, err := ifc.Addrs()
			if err != nil {
				continue
			}
			for _, a := range addrs {
				if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil && !n.IP.IsLoopback() {
					_, masked, err := net.ParseCIDR(n.String())
					if err == nil {
						out = append(out, masked)
					}
				}
			}
		}
	}
	if raw, err := os.ReadFile("/proc/net/route"); err == nil {
		out = append(out, parseProcNetRoute(string(raw))...)
	}
	return out
}

// isDockerBridgeIface reports whether name is a docker-managed bridge —
// `docker0` (the default bridge) or `br-<id>` (compose/user-defined
// networks). Their routes are a network's own presence, not the host LAN;
// counting them made hostVisibleSubnets self-detect any already-existing
// stack network as "shadowing a LAN" (see shadowsLAN).
func isDockerBridgeIface(name string) bool {
	return name == "docker0" || strings.HasPrefix(name, "br-")
}

// parseProcNetRoute extracts the destination subnets of non-default routes.
func parseProcNetRoute(raw string) []*net.IPNet {
	var out []*net.IPNet
	for i, line := range strings.Split(raw, "\n") {
		f := strings.Fields(line)
		if i == 0 || len(f) < 8 {
			continue
		}
		if isDockerBridgeIface(f[0]) {
			continue
		}
		dst, err1 := hexLE(f[1])
		mask, err2 := hexLE(f[7])
		if err1 != nil || err2 != nil || mask == 0 {
			continue
		}
		out = append(out, &net.IPNet{IP: uintToIP(dst), Mask: net.IPMask(uintToIP(mask))})
	}
	return out
}

// hexLE parses /proc/net/route's little-endian hex address.
func hexLE(s string) (uint32, error) {
	var v uint32
	if _, err := fmt.Sscanf(s, "%x", &v); err != nil {
		return 0, err
	}
	return v>>24 | (v>>8)&0xff00 | (v<<8)&0xff0000 | v<<24, nil
}

// dockerNetworkSubnets maps every existing docker network name to its IPv4
// subnets.
func dockerNetworkSubnets(ctx context.Context) (map[string][]*net.IPNet, error) {
	ids, err := dockerCLI(ctx, "network", "ls", "-q")
	if err != nil {
		return nil, fmt.Errorf("docker network ls: %v %s", err, strings.TrimSpace(ids))
	}
	out := map[string][]*net.IPNet{}
	fields := strings.Fields(ids)
	if len(fields) == 0 {
		return out, nil
	}
	args := append([]string{"network", "inspect", "-f",
		"{{.Name}}|{{range .IPAM.Config}}{{.Subnet}} {{end}}"}, fields...)
	raw, err := dockerCLI(ctx, args...)
	if err != nil {
		return nil, fmt.Errorf("docker network inspect: %v %s", err, strings.TrimSpace(raw))
	}
	return parseNetworkSubnets(raw), nil
}

// parseNetworkSubnets parses lines of "<name>|<subnet> <subnet> ".
func parseNetworkSubnets(raw string) map[string][]*net.IPNet {
	out := map[string][]*net.IPNet{}
	for _, line := range strings.Split(raw, "\n") {
		name, subnets, ok := strings.Cut(strings.TrimSpace(line), "|")
		if !ok || name == "" {
			continue
		}
		out[name] = parseCIDRList(subnets)
	}
	return out
}

// pinPlan is what pinStackSubnetsYAML decided for one top-level network key.
type pinPlan struct {
	Key    string
	Name   string // daemon-side name
	Subnet string // assigned subnet ("" when left alone)
	Reason string // why it was left alone
}

// pinStackSubnetsYAML rewrites composeYAML so each compose-managed network
// without an explicit ipam subnet (and not already existing) gets a subnet
// from pool. Returns the new YAML (unchanged text when nothing was pinned),
// the per-network decisions, and warnings.
func pinStackSubnetsYAML(composeYAML, project string, pool *net.IPNet,
	existing map[string][]*net.IPNet, avoid []*net.IPNet) (string, []pinPlan, []string, error) {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(composeYAML), &root); err != nil {
		return composeYAML, nil, nil, err
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return composeYAML, nil, nil, nil
	}
	doc := root.Content[0]
	proj := composeProjectName(project)

	netsNode := mapGet(doc, "networks")
	keys := networkKeysInUse(doc, netsNode)
	if len(keys) == 0 {
		return composeYAML, nil, nil, nil
	}

	var used []*net.IPNet
	used = append(used, avoid...)
	for _, subs := range existing {
		used = append(used, subs...)
	}

	var plans []pinPlan
	var warns []string
	changed := false
	for _, key := range keys {
		var cfg *yaml.Node
		if netsNode != nil {
			cfg = mapGet(netsNode, key)
		}
		name := proj + "_" + key
		if cfg != nil && cfg.Kind == yaml.MappingNode {
			if ext := mapGet(cfg, "external"); ext != nil && !(ext.Kind == yaml.ScalarNode && ext.Value == "false") {
				plans = append(plans, pinPlan{Key: key, Name: key, Reason: "external"})
				continue
			}
			if n := mapGet(cfg, "name"); n != nil && strings.TrimSpace(n.Value) != "" {
				name = strings.TrimSpace(n.Value)
			}
			if ipam := mapGet(cfg, "ipam"); ipam != nil {
				plans = append(plans, pinPlan{Key: key, Name: name, Reason: "explicit ipam"})
				continue
			}
		}
		if subs, ok := existing[name]; ok {
			// A redeploy sees this network already created. If we were the ones
			// who pinned it (its live subnet lies inside our pool — Docker's own
			// default pools never hand one out), re-emit the IDENTICAL ipam block
			// so compose's config hash matches what is live and it never deletes
			// and recreates the network. Skipping ipam here (the old behaviour)
			// made every redeploy diverge from the live config and forced a
			// recreate on Docker's default pool instead, breaking the embedded
			// DNS the other services depend on for service-name resolution.
			var rePinned *net.IPNet
			for _, s := range subs {
				if pool.Contains(s.IP) {
					rePinned = s
					break
				}
			}
			if rePinned != nil {
				setNetworkSubnet(doc, &netsNode, key, rePinned.String())
				plans = append(plans, pinPlan{Key: key, Name: name, Subnet: rePinned.String(), Reason: "already exists, re-pinned"})
				changed = true
				continue
			}
			var shadow *net.IPNet
			for _, s := range subs {
				if shadowsLAN(s, pool, avoid) {
					shadow = s
					break
				}
			}
			if shadow == nil {
				plans = append(plans, pinPlan{Key: key, Name: name, Reason: "already exists"})
				continue
			}
			// A live network that shadows the LAN is the outage itself: an isolated
			// net created before pinning existed, sitting in a Docker fallback
			// block, cut every container on the manager off the LAN the moment
			// the stack restarted). Warning and keeping it is not enough — pin a
			// pool subnet so compose's config hash diverges and it recreates the
			// network off the LAN range on this deploy.
			subnet, err := allocateSubnet(pool, stackNetPrefixLen, used)
			if err != nil {
				return composeYAML, plans, warns, err
			}
			used = append(used, subnet)
			setNetworkSubnet(doc, &netsNode, key, subnet.String())
			plans = append(plans, pinPlan{Key: key, Name: name, Subnet: subnet.String(), Reason: "LAN-shadowing, re-pinned"})
			warns = append(warns, fmt.Sprintf(
				"WARNING: stack network %s uses %s, which may shadow a LAN — re-pinning it to %s so compose recreates it off the LAN range.",
				name, shadow, subnet))
			changed = true
			continue
		}
		subnet, err := allocateSubnet(pool, stackNetPrefixLen, used)
		if err != nil {
			return composeYAML, plans, warns, err
		}
		used = append(used, subnet)
		setNetworkSubnet(doc, &netsNode, key, subnet.String())
		plans = append(plans, pinPlan{Key: key, Name: name, Subnet: subnet.String()})
		changed = true
	}
	if !changed {
		return composeYAML, plans, warns, nil
	}
	out, err := yaml.Marshal(&root)
	if err != nil {
		return composeYAML, plans, warns, err
	}
	return string(out), plans, warns, nil
}

// networkKeysInUse returns the top-level network keys plus `default` when any
// service falls back to compose's implicit default network. Sorted.
func networkKeysInUse(doc, netsNode *yaml.Node) []string {
	set := map[string]bool{}
	if netsNode != nil && netsNode.Kind == yaml.MappingNode {
		for i := 0; i+1 < len(netsNode.Content); i += 2 {
			set[netsNode.Content[i].Value] = true
		}
	}
	if svcs := mapGet(doc, "services"); svcs != nil && svcs.Kind == yaml.MappingNode {
		for i := 0; i+1 < len(svcs.Content); i += 2 {
			svc := svcs.Content[i+1]
			if svc.Kind != yaml.MappingNode {
				continue
			}
			if nm := mapGet(svc, "network_mode"); nm != nil && strings.TrimSpace(nm.Value) != "" {
				continue
			}
			nets := mapGet(svc, "networks")
			if nets == nil {
				set["default"] = true
				continue
			}
			switch nets.Kind {
			case yaml.SequenceNode:
				for _, n := range nets.Content {
					if n.Value == "default" {
						set["default"] = true
					}
				}
			case yaml.MappingNode:
				for j := 0; j+1 < len(nets.Content); j += 2 {
					if nets.Content[j].Value == "default" {
						set["default"] = true
					}
				}
			}
		}
	}
	keys := make([]string, 0, len(set))
	for k := range set {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// mapGet returns the value node for key in a mapping node, or nil.
func mapGet(m *yaml.Node, key string) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

func scalar(v string) *yaml.Node { return &yaml.Node{Kind: yaml.ScalarNode, Value: v} }

func mapping(kv ...*yaml.Node) *yaml.Node {
	return &yaml.Node{Kind: yaml.MappingNode, Content: kv}
}

// setNetworkSubnet adds `ipam: {config: [{subnet: X}]}` to networks.<key>,
// creating the top-level networks map / the key's entry when missing (a
// `key:` with a null value becomes a mapping).
func setNetworkSubnet(doc *yaml.Node, netsNode **yaml.Node, key, subnet string) {
	if *netsNode == nil || (*netsNode).Kind != yaml.MappingNode {
		n := mapping()
		if *netsNode != nil {
			// `networks:` present but null — replace its value in place.
			for i := 0; i+1 < len(doc.Content); i += 2 {
				if doc.Content[i].Value == "networks" {
					doc.Content[i+1] = n
				}
			}
		} else {
			doc.Content = append(doc.Content, scalar("networks"), n)
		}
		*netsNode = n
	}
	ipam := mapping(scalar("config"), &yaml.Node{Kind: yaml.SequenceNode,
		Content: []*yaml.Node{mapping(scalar("subnet"), scalar(subnet))}})
	nets := *netsNode
	for i := 0; i+1 < len(nets.Content); i += 2 {
		if nets.Content[i].Value == key {
			cfg := nets.Content[i+1]
			if cfg.Kind != yaml.MappingNode {
				cfg = mapping()
				nets.Content[i+1] = cfg
			}
			cfg.Content = append(cfg.Content, scalar("ipam"), ipam)
			return
		}
	}
	nets.Content = append(nets.Content, scalar(key), mapping(scalar("ipam"), ipam))
}

// pinStackSubnets is the executor entry point: it never fails the deploy —
// on any error it logs and returns the spec unchanged (the daemon's default
// pools are then used, as before).
func (e *Executor) pinStackSubnets(ctx context.Context, cb Callbacks, project, composeYAML string) string {
	var lines []string
	defer func() {
		if cb.OnLogBatch != nil && len(lines) > 0 {
			cb.OnLogBatch(lines)
		}
	}()
	existing, err := dockerNetworkSubnets(ctx)
	if err != nil {
		lines = append(lines, fmt.Sprintf("subnet pinning: skipped, %v", err))
		return composeYAML
	}
	avoid := append(hostVisibleSubnets(), parseCIDRList(os.Getenv("MOD_STACK_NET_AVOID"))...)

	subnetMu.Lock()
	defer subnetMu.Unlock()
	now := time.Now()
	for s, t := range subnetReserved {
		if now.Sub(t) > subnetReserveTTL {
			delete(subnetReserved, s)
		} else if _, n, err := net.ParseCIDR(s); err == nil {
			avoid = append(avoid, n)
		}
	}
	out, plans, warns, err := pinStackSubnetsYAML(composeYAML, project, stackNetPool(), existing, avoid)
	lines = append(lines, warns...)
	if err != nil {
		lines = append(lines, fmt.Sprintf("subnet pinning: skipped, %v", err))
		return composeYAML
	}
	for _, p := range plans {
		if p.Subnet != "" {
			subnetReserved[p.Subnet] = now
			lines = append(lines, fmt.Sprintf("subnet pinning: network %s -> %s", p.Name, p.Subnet))
		}
	}
	return out
}
