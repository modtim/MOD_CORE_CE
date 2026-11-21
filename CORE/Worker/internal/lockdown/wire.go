// Startup wiring: the worker-start call path for the host egress lockdown
// (owner Tim 11-12: the nft egress lockdown must be LIVE at CE launch).
//
// The POLICY decision stays on the API side: CORE/API/services/host_lockdown.py
// :45-47 registers egress.host_lockdown_workers (the named list of workers that
// lock) and egress.host_lockdown_accept_wsl_workers (the OD-H1 opt-in that
// accepts a WSL-VM-only `partial` as sufficient). Both reach the worker through
// the heartbeat tunables map (GS.PSR.7, internal/tunables.Store), so this file
// only turns that decision into Probe -> Apply at worker start; it adds no new
// setting and no new env var (the env names below are the spec's documented
// fallbacks, read by tunables.Store.String/Bool exactly like every other
// tunable reader).
//
// Degrade policy (chosen + justified): a worker that cannot lock — no CAP_NET_ADMIN
// (apply.go:137 hasNetAdmin), no nft on PATH (apply.go:153), Docker Desktop
// (apply.go:181) — answers a logged WARNING plus the heartbeat-visible
// Unsupported/Partial Status triple (apply.go:62), and tasks keep running. The
// CE worker commonly runs unprivileged, so a hard failure would brick every
// install; fail-closed belongs to the RULES instead: Render's single
// delete+add transaction (rules.go:46-49) leaves the old table or the new one,
// never nothing.
package lockdown

import (
	"context"
	"net"
	"net/netip"
	"strings"
	"time"
)

// The platform-settings registry keys the API registers (host_lockdown.py:45
// and :47) and the spec's documented env fallbacks (never a NEW env var).
const (
	HostLockdownSetting = "egress.host_lockdown_workers"
	HostLockdownEnv     = "MOD_HOST_LOCKDOWN_WORKERS"
	AcceptWSLSetting    = "egress.host_lockdown_accept_wsl_workers"
	AcceptWSLEnv        = "MOD_HOST_LOCKDOWN_ACCEPT_WSL"
)

// Listed answers whether workerName appears in a comma-separated names setting
// (the grammar host_lockdown.py:_validate_worker_names accepts: clean names,
// no empty entries). A blank setting lists nobody.
func Listed(namesSetting, workerName string) bool {
	if workerName == "" {
		return false
	}
	for _, part := range strings.Split(namesSetting, ",") {
		if strings.TrimSpace(part) == workerName {
			return true
		}
	}
	return false
}

// Skipped is the heartbeat state for "this box CAN lock, but the startup
// policy is not complete yet": the table is NOT applied, so the worker keeps
// its existing behaviour (fail open) rather than being cut off from its own
// manager by a default-deny table with no manager allow entry (the REV BLOCKING
// finding on commit f9da4c98c0: PolicyForAPI allowed only an IP-literal manager
// URL, so a hostname API produced an empty allow set and the nft default-deny
// output hook dropped every new connection - heartbeat keepalives aside -
// including the local model stack the worker exists to serve).
const Skipped = "skipped"

// ManagerResolveTimeout bounds the ONE hostname lookup the startup policy is
// allowed to do: a hung resolver must never stall the worker-start goroutine.
var ManagerResolveTimeout = 2 * time.Second

// HostOf strips the scheme, path and port from an API URL, leaving the bare
// host the policy needs ("http://api.example.com:8443/v1" -> "api.example.com").
func HostOf(apiURL string) string {
	host := strings.TrimSpace(apiURL)
	if i := strings.Index(host, "://"); i >= 0 {
		host = host[i+3:]
	}
	if i := strings.IndexByte(host, '/'); i >= 0 {
		host = host[:i]
	}
	if i := strings.IndexByte(host, ':'); i >= 0 {
		host = host[:i]
	}
	return host
}

// ManagerPorts are the two egress ports the allow rules cover, exactly as
// rules.go:Render emits them (an nft ip dpt rule per port).
var ManagerPorts = []int{80, 443}

// AllowManager turns the manager IPs the worker knows into the 80/443 allow
// pairs. An empty addrs list yields an empty policy (nothing resolvable).
func AllowManager(addrs []netip.Addr) Policy {
	p := Policy{}
	for _, a := range addrs {
		for _, port := range ManagerPorts {
			p.AllowIPs = append(p.AllowIPs, netip.AddrPortFrom(a, uint16(port)))
		}
	}
	return p
}

// lookupHost resolves one host with ONE bounded net.LookupHost (2 s). The
// lookup runs in a goroutine feeding a 1-slot channel: the send always
// completes, so the goroutine exits on EITHER select branch (no leak), and a
// hung resolver costs at most ManagerResolveTimeout. An empty result (timeout
// or error) means "not resolvable" - callers must treat it as such, never as
// "allow nothing and apply anyway".
func lookupHost(host string) []netip.Addr {
	ch := make(chan []netip.Addr, 1)
	go func() {
		ips := []netip.Addr{}
		// net.LookupHost answers dotted strings; an unparsable entry is dropped.
		// "localhost" resolves to 127.0.0.1 on both the glibc and the built-in
		// resolver, which is what makes it the deterministic resolving test host.
		hosts, _ := net.LookupHost(host)
		// A lookup error folds into "no IPs" = "not resolvable": the caller
		// then skips Apply, which is the safe direction (no table, no self-cut).
		// The bounded goroutine still sends, so nothing hangs and nothing leaks.
		for _, s := range hosts {
			a, err := netip.ParseAddr(s)
			if a.IsValid() && err == nil {
				ips = append(ips, a)
			}
		}
		ch <- ips
	}()
	got := []netip.Addr{}
	select {
	case sent := <-ch:
		// The send always completed (1-slot channel, single sender), so the
		// goroutine has already exited: no leak on either branch.
		got = sent
	case <-time.After(ManagerResolveTimeout):
		// Left empty: "not resolvable in time" - the caller skips Apply, it
		// never applies an empty table.
	}
	return got
}

// HasManager answers whether a policy names the manager at all (an allow IP,
// a DNS host, or a tailnet prefix). A policy with none of these would leave
// the worker cut off from its own manager, so it must never be applied.
func HasManager(p Policy) bool {
	if len(p.AllowIPs) > 0 || len(p.DNS) > 0 {
		return true
	}
	for _, pfx := range []netip.Prefix{p.TailnetV4, p.TailnetV6} {
		if pfx.IsValid() && pfx.Bits() > 0 {
			return true
		}
	}
	return false
}

// PolicyForAPI builds the startup policy from what the worker itself knows:
// its own API endpoint (the manager the lockdown must never cut). An IP
// literal is used as-is; a hostname gets ONE bounded resolution (see the
// Skipped note above for why an unresolved hostname must never become an
// empty table). Host-named targets beyond the manager (Harbor, the egress
// gateway, the tailnet) still need the follow-up directive; until then
// everything else stays default-deny.
func PolicyForAPI(apiURL string) Policy {
	host := HostOf(apiURL)
	if host == "" {
		return Policy{}
	}
	a, err := netip.ParseAddr(host)
	if a.IsValid() && err == nil {
		// IP literal: unchanged behaviour, no lookup at all.
		return AllowManager([]netip.Addr{a})
	}
	// A hostname (the common case): bounded resolution, and only its IPs go
	// into the policy.
	return AllowManager(lookupHost(host))
}

// StartupLockdown is the worker-start call: off (no exec at all) when the
// setting does not name this worker; Probe first so an incapable box never
// attempts an apply; Apply only on Enforced, or on Partial when the OD-H1
// opt-in accepts the WSL-VM-only report. Returns the heartbeat Status triple
// and whether the table was applied — the caller logs and reports, never
// crashes.
//
// REV BLOCKING fix (11-12): when the startup policy carries NO allow entry for
// the manager (a hostname API that did not resolve in time), the table is NOT
// applied: an nft default-deny output hook with no manager accept would cut
// the worker off from its own manager (heartbeats, and the local model stack
// that is the worker's whole job). The Status says Skipped so the caller logs
// one WARNING naming the reason, and the worker keeps running (fail open to
// the existing behaviour). A policy that DOES name the manager - an IP-literal
// API, or a hostname that resolved to IPs - applies exactly as before.
func StartupLockdown(m *Manager, ctx context.Context, listed, acceptWSL bool, p Policy) (Status, bool) {
	if !listed {
		return Status{HostLockdown: Off, Scope: HostScope, Detail: "worker not named in " + HostLockdownSetting}, false
	}
	st := m.Probe(ctx)
	proceed := false
	switch st.HostLockdown {
	case Enforced:
		proceed = true
	case Partial:
		// OD-H1 default: the Windows host behind a WSL VM is not locked by
		// the worker, so a VM-only table is not accepted unless opted in.
		proceed = acceptWSL
	default:
		// Unsupported / Errored: degrade, never apply, never crash.
	}
	if !proceed {
		return st, false
	}
	if !HasManager(p) {
		// The box can lock, but the startup policy cannot name the manager:
		// applying it would self-cut the worker, so skip the table instead.
		return Status{HostLockdown: Skipped, Scope: st.Scope, Detail: "skipped: manager endpoint not resolvable to an IP"}, false
	}
	if err := m.Apply(ctx, p); err != nil {
		return Status{HostLockdown: Errored, Scope: st.Scope, Detail: "apply failed: " + err.Error()}, false
	}
	return st, true
}
