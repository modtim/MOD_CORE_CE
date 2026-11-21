package executor

// gw-stack-networking GW.2 — host ports are MOD-owned, worker side.
//
// The API renders an `x-mod.host_ports` entry of `host: "any"` into a
// container-only short publish ("8000/tcp"), which is safe on its own: Docker
// picks an ephemeral host port. The problem it leaves is that the choice is
// invisible — the manager can neither see it, persist it, nor guarantee two
// stacks never grab the same one. This file closes the loop:
//
//   - the worker is the only party that can see the host's real port usage,
//     so IT allocates: `applyStackHostPorts` rewrites each `any` entry to a
//     concrete host port taken from a per-worker range (platform setting
//     `worker.stack_host_port_range`, default 20000-29999), skipping every
//     port the daemon reports bound and every port this worker already
//     reserved for another stack;
//   - the chosen mapping is reported back in the deploy result (a sentinel
//     JSON block appended to the result tail), and the API persists it on the
//     stack's `x-mod.allocated_host_ports` — subsequent renders then emit the
//     FIXED mapping, so a stack keeps its host port across redeploys and
//     survives a migrate;
//   - the per-protocol set of host ports bound on this worker (docker port
//     bindings of every running container) rides on every heartbeat
//     (`host_bound_ports`) so the manager's placement (GW.3) can treat a
//     requested fixed host port as a resource.
//
// Allocation is a HARD failure on the spec side: an exhausted range or a
// fixed port already bound by another project fails the deploy with a clear
// message rather than letting `compose up` die with a daemon error nobody can
// act on.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"gopkg.in/yaml.v3"
)

// stackHostPortRangeTunableKey is the platform_settings registry key (GS.PSR.7
// delivery, see internal/tunables) that pushes the allocation range. A string,
// "lo-hi" — the registry stores one value fleet-wide, so the "per-worker
// range" is per-worker only in that each worker applies the value the API
// pushes at the moment of allocation.
const stackHostPortRangeTunableKey = "worker.stack_host_port_range"

// defaultStackHostPortRange is the fallback when the API has pushed nothing
// (older manager, or the setting left at default so it is never echoed).
const defaultStackHostPortRange = "20000-29999"

// The deploy result's sentinel block. Same convention as
// _ARTIFACT_BEGIN_SENTINEL in routers/node_definitions_domains/_shared.py:
// the result tail is free-form compose output, so a machine-readable payload
// travels between sentinels rather than in a new transport field (which a
// fleet of older API releases would drop).
const (
	stackHostPortSentinelBegin = "=====MOD_STACK_HOST_PORTS_BEGIN====="
	stackHostPortSentinelEnd   = "=====MOD_STACK_HOST_PORTS_END====="
)

// errHostPortRangeExhausted is returned (wrapped) when no port in the range
// is free. Callers may match it with errors.Is.
var errHostPortRangeExhausted = errors.New("no free host port in the allocated range")

// hostPortMapping is one service→host-port decision, for the result sentinel
// and the x-mod.allocated_host_ports record the API persists.
type hostPortMapping struct {
	Service  string `json:"service"`
	Target   int    `json:"target"`
	Host     int    `json:"host"`
	Protocol string `json:"protocol"`
}

// in-memory reservations: project → host ports handed out but possibly not yet
// bound (the gap between `applyStackHostPorts` and the containers actually
// coming up). Mirrors subnetReserved in stacksubnet.go — the same two-deploy
// race, one port instead of one /24.
var (
	hostPortReserveMu sync.Mutex
	hostPortReserved  = map[string]map[int]bool{}
)

// reserveStackHostPorts records the ports handed out for project.
func reserveStackHostPorts(project string, ports []int) {
	hostPortReserveMu.Lock()
	defer hostPortReserveMu.Unlock()
	set := hostPortReserved[project]
	if set == nil {
		set = map[int]bool{}
		hostPortReserved[project] = set
	}
	for _, p := range ports {
		set[p] = true
	}
}

// releaseStackHostPorts forgets project's reservations (teardown/stop).
func releaseStackHostPorts(project string) {
	hostPortReserveMu.Lock()
	defer hostPortReserveMu.Unlock()
	delete(hostPortReserved, project)
}

// reservedHostPortsUnion is every port reserved for any project, for the
// allocator's skip set.
func reservedHostPortsUnion() map[int]bool {
	hostPortReserveMu.Lock()
	defer hostPortReserveMu.Unlock()
	out := map[int]bool{}
	for _, set := range hostPortReserved {
		for p := range set {
			out[p] = true
		}
	}
	return out
}

// hasNoHostPorts is the cheap pre-check applyStackHostPorts uses to skip the
// daemon query entirely when the spec carries no x-mod.host_ports block at
// all (the common case — host publishing is opt-in). Substring match on the
// YAML key: a false negative is impossible for a spec the API renders (the
// key is always a bare "host_ports" line), and a false positive costs one
// extra parse that renderStackHostPorts then answers authoritatively.
func hasNoHostPorts(spec string) bool {
	return !strings.Contains(spec, "host_ports")
}

// parseHostPortRange parses "lo-hi" into (lo, hi). Anything else is an error:
// a malformed range is an operator typo in a platform setting, and guessing a
// correction would allocate from a range nobody asked for.
func parseHostPortRange(s string) (int, int, error) {
	loStr, hiStr, ok := strings.Cut(strings.TrimSpace(s), "-")
	if !ok {
		return 0, 0, fmt.Errorf("host port range %q must be \"lo-hi\"", s)
	}
	lo, err1 := strconv.Atoi(strings.TrimSpace(loStr))
	hi, err2 := strconv.Atoi(strings.TrimSpace(hiStr))
	if err1 != nil || err2 != nil || lo < 1 || hi > 65535 || lo > hi {
		return 0, 0, fmt.Errorf("host port range %q must be \"lo-hi\" with 1 <= lo <= hi <= 65535", s)
	}
	return lo, hi, nil
}

// stackHostPortRange resolves the allocation range: server-pushed tunable >
// built-in default. A value that does not parse falls back to the default
// rather than failing every deploy — a newer manager can send a range this
// binary's parser rejects, and degrading to the default is the "unknown value
// is ignored" contract the tunables store applies to enums.
func (e *Executor) stackHostPortRange() (int, int) {
	if e != nil && e.tunables != nil {
		if raw := strings.TrimSpace(e.tunables.String(stackHostPortRangeTunableKey, "", "")); raw != "" {
			if lo, hi, err := parseHostPortRange(raw); err == nil {
				return lo, hi
			}
		}
	}
	lo, hi, _ := parseHostPortRange(defaultStackHostPortRange)
	return lo, hi
}

// allocateHostPorts picks the LOWEST port in [lo,hi] that is in neither
// blocked (protocol-keyed sets of ports the daemon reports bound) nor
// reserved (ports already handed out to other stacks on this worker). Pure,
// so the range/collision/exhaustion behaviour is unit-testable without a
// daemon.
func allocateHostPorts(lo, hi int, protocol string, blocked map[string]map[int]bool,
	reserved map[int]bool) (int, error) {
	blockedSet := blocked[strings.ToLower(protocol)]
	for p := lo; p <= hi; p++ {
		if blockedSet[p] || reserved[p] {
			continue
		}
		return p, nil
	}
	return 0, fmt.Errorf("%w: %d-%d are all bound or reserved for %s",
		errHostPortRangeExhausted, lo, hi, protocol)
}

// runningOnlyFilters is the daemon-side "status=running" filter, factored out
// so a test can assert the bound-port scan never regresses to enumerating the
// host's whole container history (the fence-enumeration failure mode, see
// stackListOptions in stackobserve.go).
func runningOnlyFilters() filters.Args {
	return filters.NewArgs(filters.Arg("status", "running"))
}

// boundHostPorts returns the host ports this worker's containers are bound
// on, per protocol: {"tcp": {8080, 20000}, "udp": {...}}.
//
// Source is the daemon's own port list — the only complete view available
// from inside the worker container. Only RUNNING containers carry live
// bindings (the daemon releases a stopped container's ports at stop), so the
// query is running-only. Containers without the compose project label are
// included — a port bound by ANY container on the host is a port this worker
// must not hand out, whether or not MOD deployed it.
//
// bound is the union across every container; boundByProject attributes the
// same ports to their compose project ("" for non-stack containers) so a
// fixed-port collision check can tell "blocked by my own running stack"
// (a redeploy — the old containers stop before the new ones bind) from
// "blocked by something else" (a real collision).
func (e *Executor) boundHostPorts(ctx context.Context) (map[string]map[int]bool,
	map[string]map[string]map[int]bool, error) {
	listCtx, cancel := context.WithTimeout(ctx, stackListBudget)
	defer cancel()
	containers, err := e.docker.ContainerList(listCtx, container.ListOptions{
		Filters: runningOnlyFilters(),
	})
	if err != nil {
		return nil, nil, fmt.Errorf("listing running containers: %w", err)
	}
	bound := map[string]map[int]bool{}
	boundByProject := map[string]map[string]map[int]bool{}
	for _, c := range containers {
		project := strings.TrimSpace(c.Labels[composeProjectLabel])
		for _, p := range c.Ports {
			if p.PublicPort == 0 {
				continue // expose-only: no host binding
			}
			proto := strings.ToLower(strings.TrimSpace(p.Type))
			if proto == "" {
				proto = "tcp"
			}
			port := int(p.PublicPort)
			set := bound[proto]
			if set == nil {
				set = map[int]bool{}
				bound[proto] = set
			}
			set[port] = true
			pset := boundByProject[project]
			if pset == nil {
				pset = map[string]map[int]bool{}
				boundByProject[project] = pset
			}
			if pset[proto] == nil {
				pset[proto] = map[int]bool{}
			}
			pset[proto][port] = true
		}
	}
	return bound, boundByProject, nil
}

// hostBoundPortsShape converts boundHostPorts' per-protocol sets to the
// heartbeat's wire shape: a protocol → SORTED port-list map. An empty input
// yields an EMPTY (non-nil) map — "looked, nothing is bound" is a real answer
// and must reach the manager (as host_bound_ports: {}) or a stopped container
// would leave the manager's last reading of its port blocked forever.
func hostBoundPortsShape(bound map[string]map[int]bool) map[string][]int {
	out := make(map[string][]int, len(bound))
	for proto, set := range bound {
		ports := make([]int, 0, len(set))
		for p := range set {
			ports = append(ports, p)
		}
		sort.Ints(ports)
		out[proto] = ports
	}
	return out
}

// HostBoundPorts is the heartbeat-facing form of boundHostPorts: a
// protocol → sorted port-list map. It returns nil ONLY when the daemon
// cannot be listed (the heartbeat then omits the field — "cannot say",
// never "nothing bound"); a reachable daemon with nothing bound answers
// an empty non-nil map, which the heartbeat sends as host_bound_ports: {}
// so the manager clears its stored reading.
func (e *Executor) HostBoundPorts(ctx context.Context) map[string][]int {
	bound, _, err := e.boundHostPorts(ctx)
	if err != nil {
		return nil
	}
	return hostBoundPortsShape(bound)
}

// hostBoundByProjectShape converts boundHostPorts' per-project attribution to
// the heartbeat wire shape: project → protocol → sorted port-list. Empty
// input yields an empty non-nil map (same "looked, nothing bound" contract as
// hostBoundPortsShape). Non-stack containers live under the "" project key
// and are NOT dropped — the manager only special-cases the redeploying
// stack's OWN project.
func hostBoundByProjectShape(boundByProject map[string]map[string]map[int]bool) map[string]map[string][]int {
	out := make(map[string]map[string][]int, len(boundByProject))
	for project, byProto := range boundByProject {
		protoOut := make(map[string][]int, len(byProto))
		for proto, set := range byProto {
			ports := make([]int, 0, len(set))
			for p := range set {
				ports = append(ports, p)
			}
			sort.Ints(ports)
			protoOut[proto] = ports
		}
		out[project] = protoOut
	}
	return out
}

// HostBoundPortsByProject is the per-project half of HostBoundPorts: which
// compose project holds which host ports, for the manager to tell a
// redeploying stack's OWN running containers (safe to displace) from
// everything else (a real collision). Nil ONLY on a daemon error, exactly
// like HostBoundPorts.
func (e *Executor) HostBoundPortsByProject(ctx context.Context) map[string]map[string][]int {
	_, byProject, err := e.boundHostPorts(ctx)
	if err != nil {
		return nil
	}
	return hostBoundByProjectShape(byProject)
}

// hostPortEntry is one x-mod.host_ports item as parsed off the spec.
type hostPortEntry struct {
	Service  string
	Target   int
	Host     string // "any" or the concrete port as a string
	IsAny    bool
	Protocol string
	Bind     string
}

// parseHostPortsYAML reads top-level x-mod.host_ports from a compose spec.
// Returns (entries, error); a spec without the key yields (nil, nil) — the
// worker must leave every stack that never opted in byte-for-byte alone.
func parseHostPortsYAML(spec string) ([]hostPortEntry, error) {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &root); err != nil {
		return nil, fmt.Errorf("parse compose spec for host ports: %w", err)
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return nil, nil
	}
	xmod := mapGet(root.Content[0], "x-mod")
	if xmod == nil || xmod.Kind != yaml.MappingNode {
		return nil, nil
	}
	ports := mapGet(xmod, "host_ports")
	if ports == nil || ports.Kind != yaml.SequenceNode {
		return nil, nil
	}
	var out []hostPortEntry
	for _, item := range ports.Content {
		if item.Kind != yaml.MappingNode {
			continue
		}
		var e hostPortEntry
		for i := 0; i+1 < len(item.Content); i += 2 {
			switch strings.TrimSpace(item.Content[i].Value) {
			case "service":
				e.Service = strings.TrimSpace(item.Content[i+1].Value)
			case "target":
				e.Target, _ = strconv.Atoi(strings.TrimSpace(item.Content[i+1].Value))
			case "host":
				v := strings.TrimSpace(item.Content[i+1].Value)
				if strings.EqualFold(v, "any") {
					e.IsAny = true
					e.Host = "any"
				} else {
					e.Host = v
				}
			case "protocol":
				e.Protocol = strings.ToLower(strings.TrimSpace(item.Content[i+1].Value))
			case "bind":
				e.Bind = strings.TrimSpace(item.Content[i+1].Value)
			}
		}
		if e.Service != "" && e.Target > 0 {
			if e.Protocol == "" {
				e.Protocol = "tcp"
			}
			out = append(out, e)
		}
	}
	return out, nil
}

// rewritePortsEntry rewrites one service's ports entry so (target, protocol)
// publishes on host. Returns (changed, found):
//
//   - found=true when the entry already carries a host side (the fixed form
//     the API renders, or a previously-allocated publish) — that entry is
//     never rewritten, so a redeploy cannot drift a stable port;
//   - a host-less entry ("8000/tcp" — the `any` form) or a long-form mapping
//     without `published` matching target+protocol is rewritten to
//     "bind:host:target/protocol" (or gains published/host_ip).
//
// A service whose ports list holds nothing matching returns (false, false).
func rewritePortsEntry(portsNode *yaml.Node, target int, host int,
	protocol, bind, full string) (bool, bool) {
	if portsNode == nil || portsNode.Kind != yaml.SequenceNode {
		return false, false
	}
	for i, node := range portsNode.Content {
		switch node.Kind {
		case yaml.ScalarNode:
			s := strings.TrimSpace(node.Value)
			eTarget, eProto, hasHost, ok := scalarPortFields(s)
			if !ok || eTarget != target || eProto != protocol {
				// A different target/protocol (including an already-rewritten
				// entry for a sibling host_ports item on the same service):
				// keep scanning — the entry for THIS target may still be
				// further down the list.
				continue
			}
			if hasHost {
				// Already carries a host side for this (target, proto) — never
				// rewrite, so a redeploy cannot drift a stable port.
				return false, true
			}
			portsNode.Content[i] = scalar(full)
			return true, true
		case yaml.MappingNode:
			tNode := mapGet(node, "target")
			if tNode == nil {
				continue
			}
			t, _ := strconv.Atoi(strings.TrimSpace(tNode.Value))
			if t != target {
				continue
			}
			// Same protocol gate as the scalar case: a long-form entry for
			// another protocol (or a default-tcp entry when the request is
			// udp) is not the publish this host_ports item owns.
			p := "tcp"
			if pNode := mapGet(node, "protocol"); pNode != nil {
				p = strings.ToLower(strings.TrimSpace(pNode.Value))
			}
			if p != protocol {
				continue
			}
			if pub := mapGet(node, "published"); pub != nil {
				return false, true // already carries a host side
			}
			node.Content = append(node.Content,
				scalar("published"), &yaml.Node{Kind: yaml.ScalarNode, Value: strconv.Itoa(host)})
			if bind != "" {
				node.Content = append(node.Content,
					scalar("host_ip"), &yaml.Node{Kind: yaml.ScalarNode, Value: bind})
			}
			return true, true
		}
	}
	return false, false
}

// portTargetProto parses the host-less forms ("8000", "8000/tcp").
func portTargetProto(s, wantProto string) (int, bool) {
	body, proto := s, "tcp"
	if i := strings.LastIndex(s, "/"); i > 0 {
		body, proto = s[:i], strings.ToLower(s[i+1:])
	}
	t, err := strconv.Atoi(strings.TrimSpace(body))
	if err != nil || t < 1 || t > 65535 {
		return 0, false
	}
	return t, strings.ToLower(proto) == strings.ToLower(wantProto)
}

// scalarPortFields classifies one scalar ports entry:
//
//	"9000"                 → (9000, "tcp", false, true)   host-less
//	"9000/tcp"             → (9000, "tcp", false, true)   host-less
//	"9100:9000"            → (9000, "tcp", true,  true)   host side
//	"0.0.0.0:9100:9000/tcp"→ (9000, "tcp", true,  true)   host side
//
// Non-port scalars (an image tag that slipped in, a bare word) report
// ok=false. Splitting on "/" first keeps IPv6 binds ("[::1]:9100:9000/tcp")
// intact — the protocol, when present, is always after the LAST slash.
func scalarPortFields(s string) (target int, proto string, hasHost bool, ok bool) {
	body := s
	if i := strings.LastIndex(s, "/"); i > 0 {
		body, proto = s[:i], strings.ToLower(s[i+1:])
	}
	if proto == "" {
		proto = "tcp"
	}
	parts := strings.Split(body, ":")
	switch len(parts) {
	case 1:
		// "9000"
	case 2:
		// "9100:9000" — host:target
		hasHost = true
	case 3:
		// "0.0.0.0:9100:9000" or "[::1]:9100:9000" — bind:host:target
		hasHost = true
	default:
		return 0, "", false, false
	}
	t, err := strconv.Atoi(strings.TrimSpace(parts[len(parts)-1]))
	if err != nil || t < 1 || t > 65535 {
		return 0, "", false, false
	}
	return t, proto, hasHost, true
}

func orDefault(s, def string) string {
	if strings.TrimSpace(s) == "" {
		return def
	}
	return strings.TrimSpace(s)
}

// publishForm renders the concrete host publish string for a host_ports entry.
// A bind is always emitted when one was requested. Otherwise an `any`
// allocation (a WORKER decision) pins the interface explicitly (0.0.0.0) while
// a fixed port (a USER-pinned mapping) keeps the minimal "host:target" short
// form the API renders — so a fixed publish stays byte-identical across
// redeploys and matches what the manager shows.
func publishForm(e hostPortEntry, host int) string {
	if e.Bind != "" {
		return fmt.Sprintf("%s:%d:%d/%s", e.Bind, host, e.Target, e.Protocol)
	}
	if e.IsAny {
		return fmt.Sprintf("0.0.0.0:%d:%d/%s", host, e.Target, e.Protocol)
	}
	return fmt.Sprintf("%d:%d/%s", host, e.Target, e.Protocol)
}

// netnsOwner resolves which service's `ports:` list a host_ports item may
// publish on. A netns holder — `network_mode: service:<carrier>` — must never
// carry the publish itself: docker refuses the pair with
// "conflicting options: port publishing and the container type network mode",
// and the ports belong to <carrier>, whose container owns the network and the
// container type. The API renders the publish onto the holder, so the worker
// moves it back to the carrier (following the chain while it keeps resolving,
// which covers a carrier that is itself a netns holder).
//
// Returns (owner, skip):
//
//	skip=false → publish on `owner` (owner == name for a plain service, so
//	             a spec without network_mode is untouched);
//	skip=true  → this item can never be published: the chain dead-ends at a
//	             missing service, or the service runs on a non-service
//	             network_mode (host/none), which takes no publish either.
//	             The whole item is then left out of the render — including its
//	             allocation, so a skipped publish cannot burn a port from the
//	             range or land in the persisted record.
func netnsOwner(services *yaml.Node, name string) (string, bool) {
	seen := map[string]bool{}
	for hops := 0; hops < 32; hops++ {
		owner := mapGet(services, name)
		if owner == nil || owner.Kind != yaml.MappingNode {
			// hops==0: the host_ports item names a service the spec has no
			// node for (the old silent no-op); hops>0: the chain dead-ended at
			// a carrier the spec never declares — publish nowhere, loudly.
			return name, hops > 0
		}
		nm := mapGet(owner, "network_mode")
		v := ""
		if nm != nil {
			v = strings.TrimSpace(nm.Value)
		}
		if v == "" {
			return name, false // plain service: publishes on itself
		}
		if !strings.HasPrefix(v, "service:") {
			return "", true // host/none: no container type to inherit a port
		}
		next := strings.TrimSpace(strings.TrimPrefix(v, "service:"))
		if next == "" || seen[name] {
			return "", true // dangling or cyclic chain: publish nowhere
		}
		seen[name] = true
		name = next
	}
	// A chain longer than the hop budget is a pathological spec; publishing
	// nowhere is the safe reading (docker would refuse the holder anyway).
	return "", true
}

// hostPortKey is the record key for one (service, target, protocol).
func hostPortKey(svc string, target int, proto string) string {
	return svc + "|" + strconv.Itoa(target) + "|" + proto
}

func splitHostPortKey(k string) (svc, tgt, proto string) {
	parts := strings.SplitN(k, "|", 3)
	if len(parts) != 3 {
		return "", "", ""
	}
	return parts[0], parts[1], parts[2]
}

// renderStackHostPorts is the pure core of applyStackHostPorts:
//
//   - each `any` entry REUSES the port already recorded in
//     x-mod.allocated_host_ports when one is present (a redeploy — no
//     allocation, no sentinel line, the port stays stable); otherwise it
//     allocates the lowest free port (allocateHostPorts, skipping every port
//     handed out earlier in THIS render) and records it in out (→ the result
//     sentinel);
//   - a FIXED entry (host is an integer) that another project's container
//     already holds fails the deploy before `compose up` can die on it. A
//     collision against the project's OWN running containers is not an error:
//     that is a redeploy, and compose stops the old containers first;
//   - each entry's service ports entry is rewritten to carry the concrete
//     host port (only the host-less `any` form is ever rewritten; an entry
//     that already carries a host side for the same target/protocol is left
//     alone, and a sibling entry for another target on the same service does
//     not short-circuit the scan). The rewrite targets the PUBLISHING service:
//     a netns holder (`network_mode: service:<carrier>`) can never carry one,
//     so the publish moves to the carrier (see netnsOwner); an item whose
//     chain dead-ends at a missing carrier is skipped entirely — no publish,
//     no allocation, no record entry;
//   - the spec's x-mod.allocated_host_ports is rebuilt to the full record
//     (previously-allocated entries kept, new ones appended) so the API's
//     next render can emit the fixed mapping without the worker.
//
// Returns the spec unchanged when there is nothing to do (no entries, or no
// new allocations and no rewrites).
func renderStackHostPorts(spec, project string, lo, hi int,
	blocked map[string]map[int]bool, ownBlocked map[string]map[int]bool,
	reserved map[int]bool) (string, []hostPortMapping, error) {
	entries, err := parseHostPortsYAML(spec)
	if err != nil {
		return spec, nil, err
	}
	if len(entries) == 0 {
		return spec, nil, nil
	}

	var root yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &root); err != nil {
		return spec, nil, fmt.Errorf("parse compose spec for host ports: %w", err)
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return spec, nil, nil
	}
	doc := root.Content[0]
	services := mapGet(doc, "services")
	xmod := mapGet(doc, "x-mod")

	// Previously-allocated record (x-mod.allocated_host_ports): the API
	// persists the worker's earlier choices, and this render must reproduce
	// them exactly — determinism is what makes the port stable across
	// redeploys and migrates.
	allocated := map[string]int{} // hostPortKey -> host
	if xmod != nil && xmod.Kind == yaml.MappingNode {
		if rec := mapGet(xmod, "allocated_host_ports"); rec != nil && rec.Kind == yaml.SequenceNode {
			for _, item := range rec.Content {
				if item.Kind != yaml.MappingNode {
					continue
				}
				var svc, proto string
				var tgt, host int
				for i := 0; i+1 < len(item.Content); i += 2 {
					v := strings.TrimSpace(item.Content[i+1].Value)
					switch strings.TrimSpace(item.Content[i].Value) {
					case "service":
						svc = v
					case "target":
						tgt, _ = strconv.Atoi(v)
					case "host":
						host, _ = strconv.Atoi(v)
					case "protocol":
						proto = strings.ToLower(v)
					}
				}
				if svc != "" && tgt > 0 && host > 0 {
					if proto == "" {
						proto = "tcp"
					}
					allocated[hostPortKey(svc, tgt, proto)] = host
				}
			}
		}
	}

	var out []hostPortMapping
	changed := false
	// taken folds this render's own `any` allocations into the allocator's
	// skip set: the ledger only sees a port after recordStackHostPorts runs
	// (post `compose up`), and the daemon's bound list likewise lags by one
	// deploy, so WITHOUT it the second `any` entry of the same spec would be
	// handed the first entry's port.
	taken := map[int]bool{}
	for k := range allocated {
		// Ports the persisted record already owns are off-limits to NEW
		// allocations too — two `any` entries must never collide with the
		// record (e.g. a fresh `any` on target B while target A keeps its
		// recorded port).
		taken[allocated[k]] = true
	}
	for _, e := range entries {
		key := hostPortKey(e.Service, e.Target, e.Protocol)
		// GW.2/B76b: a netns holder (`network_mode: service:<carrier>`) cannot
		// carry a publish — docker refuses the pair outright ("conflicting
		// options: port publishing and the container type network mode") — so
		// the rewrite targets the carrier that owns the container type and the
		// container's ports. Resolved BEFORE the allocation: an item that can
		// never publish must not burn a port from the range nor land in the
		// persisted record.
		publisher := e.Service
		if services != nil && services.Kind == yaml.MappingNode {
			owner, unpublishable := netnsOwner(services, e.Service)
			if unpublishable {
				slog.Warn("stack host_ports: publish skipped for a network_mode service",
					"project", project, "service", e.Service, "target", e.Target,
					"protocol", e.Protocol)
				continue
			}
			publisher = owner
		}
		host := 0
		if e.IsAny {
			// The persisted record wins over fresh allocation: it is what the
			// API stored from a previous deploy, and reusing it is what makes
			// the port stable across redeploys (no new sentinel allocation is
			// emitted — `out` stays empty on a pure redeploy).
			if h, ok := allocated[key]; ok {
				host = h
			} else {
				reservedWithTaken := make(map[int]bool, len(reserved)+len(taken))
				for p := range reserved {
					reservedWithTaken[p] = true
				}
				for p := range taken {
					reservedWithTaken[p] = true
				}
				h, aerr := allocateHostPorts(lo, hi, e.Protocol, blocked, reservedWithTaken)
				if aerr != nil {
					return spec, nil, fmt.Errorf("host port for %s:%d/%s: %w",
						e.Service, e.Target, e.Protocol, aerr)
				}
				host = h
				allocated[key] = host
				taken[host] = true
				out = append(out, hostPortMapping{Service: e.Service, Target: e.Target,
					Host: host, Protocol: e.Protocol})
				changed = true
			}
		} else {
			h, perr := strconv.Atoi(e.Host)
			if perr != nil || h < 1 || h > 65535 {
				// A malformed host value is an API-side validation gap, not a
				// condition the worker should guess its way around.
				return spec, nil, fmt.Errorf(
					"host port for %s:%d: unparseable host %q", e.Service, e.Target, e.Host)
			}
			host = h
			// Fixed port: fail fast when another project holds it. The
			// daemon would refuse with an opaque "port is already allocated"
			// anyway — this says which port and why, before anything starts.
			if set := blocked[e.Protocol]; set[host] && !ownBlocked[e.Protocol][host] {
				return spec, nil, fmt.Errorf(
					"host port %d/%s for %s:%d is already bound by another container on this worker",
					host, e.Protocol, e.Service, e.Target)
			}
		}

		// Rewrite the PUBLISHING service's ports entry (the carrier when the
		// item named a netns holder) so the publish carries the concrete host
		// side. A carrier that already lists the container port gets that
		// entry rewritten in place — never a second, duplicate publish.
		if services != nil && services.Kind == yaml.MappingNode {
			if svc := mapGet(services, publisher); svc != nil && svc.Kind == yaml.MappingNode {
				ports := mapGet(svc, "ports")
				full := publishForm(e, host)
				rewrote, found := rewritePortsEntry(ports, e.Target, host, e.Protocol, e.Bind, full)
				changed = changed || rewrote
				if !found {
					// Defensive: the API render always adds a ports entry per
					// host_ports item, but a hand-edited spec may have lost
					// it. Add one rather than silently publishing nothing.
					ports = &yaml.Node{Kind: yaml.SequenceNode, Content: []*yaml.Node{
						scalar(publishForm(e, host))}}
					svc.Content = append(svc.Content, scalar("ports"), ports)
					changed = true
				}
			}
		}
	}

	if len(out) > 0 {
		// Rebuild x-mod.allocated_host_ports from the full record so the API
		// persists one canonical list.
		seq := &yaml.Node{Kind: yaml.SequenceNode}
		keys := make([]string, 0, len(allocated))
		for k := range allocated {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			svc, tgtStr, proto := splitHostPortKey(k)
			item := &yaml.Node{Kind: yaml.MappingNode, Content: []*yaml.Node{
				scalar("service"), scalar(svc),
				scalar("target"), scalar(tgtStr),
				scalar("host"), scalar(strconv.Itoa(allocated[k])),
				scalar("protocol"), scalar(proto),
			}}
			seq.Content = append(seq.Content, item)
		}
		if xmod == nil || xmod.Kind != yaml.MappingNode {
			xmod = &yaml.Node{Kind: yaml.MappingNode, Content: []*yaml.Node{
				scalar("allocated_host_ports"), seq}}
			doc.Content = append(doc.Content, scalar("x-mod"), xmod)
		} else {
			xmod.Content = append(xmod.Content, scalar("allocated_host_ports"), seq)
		}
		changed = true
	}

	if !changed {
		return spec, nil, nil
	}
	outBytes, err := yaml.Marshal(&root)
	if err != nil {
		return spec, nil, fmt.Errorf("marshal compose spec after host ports: %w", err)
	}
	return string(outBytes), out, nil
}

// formatStackHostPortSentinel renders the result-tail block the API parses
// (extract_stack_host_ports in routers/worker_tasks.py).
func formatStackHostPortSentinel(project string, allocs []hostPortMapping) string {
	if len(allocs) == 0 {
		return ""
	}
	payload, err := json.Marshal(map[string]interface{}{
		"project":     project,
		"allocations": allocs,
	})
	if err != nil {
		return ""
	}
	return "\n" + stackHostPortSentinelBegin + "\n" + string(payload) + "\n" + stackHostPortSentinelEnd
}

// applyStackHostPorts is the executor entry point used by the deploy and
// migrate paths before the compose file is written. It gathers the daemon's
// live bindings, resolves the range, and hands the pure core the inputs.
//
// The error IS the deploy error: a stack whose `any` entry cannot be
// satisfied must not come up on an ephemeral port the manager cannot see —
// that is precisely the invisibility this change exists to remove.
func (e *Executor) applyStackHostPorts(ctx context.Context, project, spec string) (string, []hostPortMapping, error) {
	if hasNoHostPorts(spec) {
		return spec, nil, nil // fast path: no docker call for the common case
	}
	blocked, byProject, err := e.boundHostPorts(ctx)
	if err != nil {
		return spec, nil, fmt.Errorf("host port allocation: %w", err)
	}
	lo, hi := e.stackHostPortRange()
	own := map[string]map[int]bool{}
	if set, ok := byProject[project]; ok {
		own = set
	}
	return renderStackHostPorts(spec, project, lo, hi, blocked, own, reservedHostPortsUnion())
}

// recordStackHostPorts reserves the allocated ports in the in-memory ledger,
// so a concurrent deploy of ANOTHER stack cannot be handed a port this one
// was just given but whose containers have not bound yet.
func (e *Executor) recordStackHostPorts(project string, allocs []hostPortMapping) {
	if len(allocs) == 0 {
		return
	}
	ports := make([]int, 0, len(allocs))
	for _, a := range allocs {
		ports = append(ports, a.Host)
	}
	reserveStackHostPorts(project, ports)
}
