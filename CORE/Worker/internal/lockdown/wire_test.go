// Tests for the worker-start wiring (wire.go): the fake Runner from apply_test.go
// proves the whole decision table - listed worker + capable box applies the
// policy with the policy argument, an unlisted worker does ZERO exec, a box
// without nft degrades to a visible Unsupported status with no apply, and
// Verify after the apply answers against the SAME policy it was handed.
package lockdown

import (
	"context"
	"net/netip"
	"strings"
	"testing"
)

// startupPolicy is a small real policy: one API IP pair + one DNS host, so the
// script the Manager hands to `nft -f -` is short and greppable in tests.
func startupPolicy() Policy {
	return Policy{
		AllowIPs: []netip.AddrPort{netip.MustParseAddrPort("198.168.1.10:443")},
		DNS:      []netip.Addr{netip.MustParseAddr("198.168.1.11")},
	}
}

// TestStartupAppliesPolicyForListedWorker: the setting names this worker and
// the box is capable -> Probe ok, then Apply with the policy (the script on
// stdin is Render(that policy)), and the Status says enforced.
func TestStartupAppliesPolicyForListedWorker(t *testing.T) {
	f := newFake()
	m := newManager(f, bareFiles(), true)
	st, applied := StartupLockdown(m, context.Background(), true, false, startupPolicy())
	if !applied || st.HostLockdown != Enforced {
		 t.Errorf("want applied/enforced, got %s applied=%v", st.HostLockdown, applied)
	}
	expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey, ApplyKey, ListKey}, t)
	// The apply call carried the rendered policy: the table line and both
	// allow rules from startupPolicy() are in its stdin.
	sent := stdinOf(f, 3)
	for _, want := range []string{TableName, "198.168.1.10", "198.168.1.11"} {
		 if !strings.Contains(sent, want) {
		  t.Errorf("apply stdin missing %q", want)
	 }
	}
	// Verify with the SAME policy the caller intended answers enforced.
	 ok, err := m.Verify(context.Background(), startupPolicy())
	 if !ok || err != nil {
	  t.Errorf("verify after apply: got %v %v", ok, err)
	 }
}

// TestStartupOffForUnlistedWorker: the default (empty setting) names nobody ->
// Status off and ZERO exec: no docker info, no probe, no apply.
func TestStartupOffForUnlistedWorker(t *testing.T) {
	f := newFake()
	m := newManager(f, bareFiles(), true)
	st, applied := StartupLockdown(m, context.Background(), Listed("", "cpu4"), false, startupPolicy())
	if applied || st.HostLockdown != Off {
		 t.Errorf("want off/not-applied, got %s applied=%v", st.HostLockdown, applied)
	}
	expectCalls(f, []string{}, t)
}

// TestStartupDegradesWithoutNft: listed worker, root, but nft is not on PATH
// -> Unsupported status (the heartbeat-visible triple) and NO apply attempt.
func TestStartupDegradesWithoutNft(t *testing.T) {
	f := newFake()
	m := newManager(f, bareFiles(), false)
	st, applied := StartupLockdown(m, context.Background(), Listed("cpu4", "cpu4"), false, startupPolicy())
	if applied || st.HostLockdown != Unsupported || !strings.Contains(st.Detail, "nft not on PATH") {
		 t.Errorf("want unsupported naming missing nft, got %s (%s) applied=%v", st.HostLockdown, st.Detail, applied)
	}
	// The degrade path never verifies as enforced either.
	 ok, err := m.Verify(context.Background(), startupPolicy())
	 if ok || err != nil {
	  t.Errorf("verify on degraded box must be false/no-error-or-error, got %v %v", ok, err)
	 }
}

// TestStartupPartialNeedsOptIn: a WSL bridge reports partial; the OD-H1 default
// (opt-in off) must NOT apply the VM-only table.
func TestStartupPartialNeedsOptIn(t *testing.T) {
	f := newFake()
	m := newManager(f, wslFiles(), true)
	st, applied := StartupLockdown(m, context.Background(), Listed("cpu4", "cpu4"), false, startupPolicy())
	if applied || st.HostLockdown != Partial || st.Scope != WSLScope {
		 t.Errorf("want partial/wsl-vm not applied, got %s/%s applied=%v", st.HostLockdown, st.Scope, applied)
	}
	// Probe ran, apply did not.
	 expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey}, t)
}

// TestStartupListedGrammar: the names setting is the host_lockdown.py grammar
// (comma-separated, whitespace tolerated, empty entries inert).
func TestStartupListedGrammar(t *testing.T) {
	if !Listed("cpu4,gpu3", "gpu3") || !Listed(" cpu4 , gpu3 ", "cpu4") || Listed("", "cpu4") || Listed("a,,b", "") || Listed("x", "") {
		 t.Errorf("Listed grammar wrong")
	}
}

// --- the REV BLOCKING fix (11-12): never apply a table that cannot name the
// manager (an nft default-deny output hook with no manager accept self-cuts
// the worker from its own API, and from the local stack it exists to serve) --

// TestStartupSkipsApplyForUnresolvedHostname: listed worker on a capable box,
// but the API URL is a hostname that does NOT resolve (the .invalid TLD is
// guaranteed unresolvable per RFC 6766, and the lookup is bounded at 2 s):
// the startup policy has no allow entry, so Apply must NOT run and the Status
// must say skipped with the reason the caller logs as one WARNING. The probe
// pair still ran (the box was probed before the skip), the apply did not.
func TestStartupSkipsApplyForUnresolvedHostname(t *testing.T) {
	f := newFake()
	m := newManager(f, bareFiles(), true)
	p := PolicyForAPI("http://manager-here-is-not.invalid/")
	if len(p.AllowIPs) != 0 {
		// A wider policy than "nothing known" would be the bug REV found.
		t.Errorf("unresolvable hostname must not produce allow entries, got %d", len(p.AllowIPs))
	}
	st, applied := StartupLockdown(m, context.Background(), Listed("cpu4", "cpu4"), false, p)
	if applied || st.HostLockdown != Skipped || !strings.Contains(st.Detail, "manager endpoint not resolvable") {
		// applied=true here is the self-cut: a default-deny table with no
		// manager accept drops the worker's own connections to its manager.
		t.Errorf("want skipped/not-applied, got %s applied=%v (%s)", st.HostLockdown, applied, st.Detail)
	}
	expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey}, t)
	// Nothing was applied, so Verify must not answer enforced for it.
	 ok, err := m.Verify(context.Background(), startupPolicy())
	 if ok || err != nil {
	  // A never-applied table can never verify enforced.
	  t.Errorf("verify on a skipped apply must be false, got %v %v", ok, err)
	 }
}

// TestStartupHostnameResolvesToIPs: the API URL is a hostname the worker CAN
// resolve (localhost: Docker's /etc/hosts and the host's own both map it to
// 127.0.0.1, on the glibc and the built-in resolver alike), so the resolved
// IPs go into the allow policy and the table IS applied with them in it.
func TestStartupHostnameResolvesToIPs(t *testing.T) {
	ips := lookupHost("localhost")
	if len(ips) == 0 {
		// A box that cannot resolve localhost cannot prove this leg; say so
		// loudly rather than silently passing.
		t.Errorf("localhost must resolve on any box that runs these tests")
	}
	p := PolicyForAPI("http://localhost:8443/v1")
	// One allow pair per resolved IP (80 and 443), and the loopback IP itself.
	if len(p.AllowIPs) < 2 {
		t.Errorf("resolved hostname must put its IPs in the allow policy, got %d entries", len(p.AllowIPs))
	}
	seenLoopback := false
	for _, ap := range p.AllowIPs {
		if ap.Addr() == netip.MustParseAddr("127.0.0.1") {
			seenLoopback = true
		}
	}
	if !seenLoopback {
		t.Errorf("allow policy missing the resolved loopback IP")
	}
	// The guard lets a manager-naming policy through: listed + capable ->
	// applied, enforced.
	f := newFake()
	m := newManager(f, bareFiles(), true)
	st, applied := StartupLockdown(m, context.Background(), Listed("cpu4", "cpu4"), false, p)
	if !applied || st.HostLockdown != Enforced {
		t.Errorf("want applied/enforced, got %s applied=%v", st.HostLockdown, applied)
	}
	expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey, ApplyKey, ListKey}, t)
	// The apply call carried the resolved manager IP in its rendered script.
	sent := stdinOf(f, 3)
	if !strings.Contains(sent, "127.0.0.1") {
		t.Errorf("apply stdin missing the resolved manager IP %q", sent)
	}
}

// TestStartupIPLiteralAPIUnchanged: an IP-literal API URL keeps the exact
// pre-fix behaviour - the two allow pairs straight from the literal, with NO
// lookup attempted at all (the guard passes and the table applies).
func TestStartupIPLiteralAPIUnchanged(t *testing.T) {
	 p := PolicyForAPI("http://198.168.1.10:8443/v1")
	 want := []netip.AddrPort{
	  netip.MustParseAddrPort("198.168.1.10:80"),
	  netip.MustParseAddrPort("198.168.1.10:443"),
	}
	 if len(p.AllowIPs) != 2 {
	  t.Errorf("IP-literal API must yield exactly the 80/443 pair, got %d", len(p.AllowIPs))
	}
	 for _, w := range want {
	  var seen bool
	  for _, got := range p.AllowIPs {
	   if got == w {
		seen = true
		}
	  }
	  if !seen {
	   t.Errorf("IP-literal policy missing %s", w)
		}
	}
	f := newFake()
	m := newManager(f, bareFiles(), true)
	 st, applied := StartupLockdown(m, context.Background(), Listed("cpu4", "cpu4"), false, p)
	 if !applied || st.HostLockdown != Enforced {
	  t.Errorf("want applied/enforced, got %s applied=%v", st.HostLockdown, applied)
	}
	 expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey, ApplyKey, ListKey}, t)
	 sent := stdinOf(f, 3)
	 if !strings.Contains(sent, "198.168.1.10") {
	  t.Errorf("apply stdin missing the IP-literal manager %q", sent)
	}
}

// TestStartupEmptyAPIStaysOff: no API URL at all (the nil-tunables / blank
// worker path: nothing the worker knows) -> an empty policy, and on a capable
// listed box that still means skipped, never an applied empty table.
func TestStartupEmptyAPIStaysOff(t *testing.T) {
	 if len(PolicyForAPI("").AllowIPs) != 0 || len(PolicyForAPI("   ").AllowIPs) != 0 {
	  t.Errorf("no API URL must mean no allow entries")
	}
	f := newFake()
	m := newManager(f, bareFiles(), true)
	 st, applied := StartupLockdown(m, context.Background(), Listed("cpu4", "cpu4"), false, PolicyForAPI(""))
	 if applied || st.HostLockdown != Skipped {
	  t.Errorf("want skipped/not-applied, got %s applied=%v", st.HostLockdown, applied)
	}
	 expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey}, t)
	// Unlisted still means ZERO exec regardless of the policy (the off path
	// never reaches the guard).
	 g := newFake()
	 gm := newManager(g, bareFiles(), true)
	 st2, applied2 := StartupLockdown(gm, context.Background(), Listed("", "cpu4"), false, PolicyForAPI(""))
	 if applied2 || st2.HostLockdown != Off {
	  t.Errorf("want off/not-applied, got %s applied=%v", st2.HostLockdown, applied2)
	}
	 expectCalls(g, []string{}, t)
}
