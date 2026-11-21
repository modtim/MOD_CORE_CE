package executor

import (
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const (
	mainNet   = "mod_core_network"
	userNet   = "mod-user-internal" // Internal=true, zero outbound
	egressNet = "mod-user-egress"   // Internal=false, routable
	stackNet  = "mod_stack-api-net" // Internal=true posture net: API attached, no internet (FX.91 p5)
)

func TestSelectNetworkMode_DefaultIsolated(t *testing.T) {
	payload := transport.TaskPayload{NodeKind: "test"}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)

	if sel.DockerNetwork != userNet {
		t.Errorf("empty payload should land on internal-only userNet; got %q", sel.DockerNetwork)
	}
	if !containsEnv(sel.EnvAdditions, "MOD_EGRESS_ALLOWED=false") {
		t.Error("default-deny should export MOD_EGRESS_ALLOWED=false")
	}
}

func TestSelectNetworkMode_APIKeyPromotesToMain(t *testing.T) {
	payload := transport.TaskPayload{
		NodeKind:     "input_validator",
		ContainerEnv: map[string]string{"API_KEY": "x"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)

	if sel.DockerNetwork != mainNet {
		t.Errorf("API_KEY container should be on mainNet; got %q", sel.DockerNetwork)
	}
	if sel.Reason == "" {
		t.Error("Reason should be populated for audit log")
	}
}

func TestSelectNetworkMode_PlatformTaskPromotesToMain(t *testing.T) {
	// PLATFORM_TASK is the explicit marker the API sets for its own
	// system containers (Containerizer, ImageBuilder, etc.) so they
	// land on the main network — needed to reach Ollama / Harbor /
	// MinIO / the API itself during the build.
	payload := transport.TaskPayload{
		NodeKind:     "albedo_generate",
		ContainerEnv: map[string]string{"PLATFORM_TASK": "1"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)

	if sel.DockerNetwork != mainNet {
		t.Errorf("PLATFORM_TASK container should be on mainNet; got %q", sel.DockerNetwork)
	}
	if sel.Reason == "" {
		t.Error("Reason should be populated for audit log")
	}
	if !containsEnv(sel.EnvAdditions, "MOD_EGRESS_ALLOWED=true") {
		t.Error("platform tasks should announce egress-allowed to their children")
	}
}

func TestSelectNetworkMode_PlatformTaskBeatsAllowedEgress(t *testing.T) {
	// A platform task with AllowedEgress listed still gets main network.
	// Priority-0 short-circuits — we want the predictability of "MOD
	// code = main network" rather than routing through the tenant
	// egress bridge based on hosts the LLM happened to pick.
	payload := transport.TaskPayload{
		NodeKind:      "albedo_generate",
		ContainerEnv:  map[string]string{"PLATFORM_TASK": "1"},
		AllowedEgress: []string{"huggingface.co"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)

	if sel.DockerNetwork != mainNet {
		t.Errorf("PLATFORM_TASK should beat AllowedEgress; got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_TokenURLPromotesToMain(t *testing.T) {
	// worker-mediated-node-credentials: a token-fetch container declaring
	// MOD_TASK_TOKEN_URL needs worker + API reachability, same as an API_KEY
	// container, so it must land on the main network.
	payload := transport.TaskPayload{
		NodeKind:     "some_callback_node",
		ContainerEnv: map[string]string{"MOD_TASK_TOKEN_URL": "http://host.docker.internal:9179/run-token"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)

	if sel.DockerNetwork != mainNet {
		t.Errorf("MOD_TASK_TOKEN_URL container should be on mainNet; got %q", sel.DockerNetwork)
	}
	if !containsString(sel.Reason, "MOD_TASK_TOKEN_URL") {
		t.Errorf("Reason should cite MOD_TASK_TOKEN_URL for audit; got %q", sel.Reason)
	}
	if !containsEnv(sel.EnvAdditions, "MOD_EGRESS_ALLOWED=true") {
		t.Error("token-fetch container should announce egress-allowed")
	}
}

func TestSelectNetworkMode_TokenFetchVsIsolated(t *testing.T) {
	// The isolation guarantee we care about: a node with a token URL reaches the
	// main network, while an otherwise-identical node WITHOUT one stays on the
	// zero-egress internal network.
	tokenNode := transport.TaskPayload{
		NodeKind:     "n",
		ContainerEnv: map[string]string{"MOD_TASK_TOKEN_URL": "http://host.docker.internal:9179/run-token"},
	}
	isolatedNode := transport.TaskPayload{NodeKind: "n"}

	if got := SelectNetworkMode(tokenNode, mainNet, userNet, egressNet).DockerNetwork; got != mainNet {
		t.Errorf("token-fetch node should be on mainNet; got %q", got)
	}
	if got := SelectNetworkMode(isolatedNode, mainNet, userNet, egressNet).DockerNetwork; got != userNet {
		t.Errorf("node without a token URL must stay isolated on userNet; got %q", got)
	}
}

func TestSelectNetworkMode_NIMKeyPromotesToMain(t *testing.T) {
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{"NIM_API_KEY": "abc"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != mainNet {
		t.Errorf("NIM_API_KEY container should be on mainNet; got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_NeedsInternetPromotesToEgress(t *testing.T) {
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{"NEEDS_INTERNET": "1"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != egressNet {
		t.Errorf("NEEDS_INTERNET container should be on egressNet (external-only); got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_AllowedEgressRoutesToEgressNetwork(t *testing.T) {
	payload := transport.TaskPayload{
		AllowedEgress: []string{"api.openai.com", "huggingface.co"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)

	if sel.DockerNetwork != egressNet {
		t.Errorf("non-empty AllowedEgress should route to egressNet (NOT mainNet — platform services stay unreachable); got %q", sel.DockerNetwork)
	}
	if !containsEnv(sel.EnvAdditions, "MOD_EGRESS_ALLOWED=true") {
		t.Error("allowlisted egress should export MOD_EGRESS_ALLOWED=true")
	}
	if !containsEnv(sel.EnvAdditions, "MOD_ALLOWED_EGRESS_HOSTS=api.openai.com,huggingface.co") {
		t.Errorf("allowlist should be exported as comma-joined env: got %v", sel.EnvAdditions)
	}
	if sel.Reason == "" {
		t.Error("Reason should include the allowlist for audit")
	}
}

func TestSelectNetworkMode_EmptyAllowedEgressIsDefaultDeny(t *testing.T) {
	payload := transport.TaskPayload{AllowedEgress: []string{}}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != userNet {
		t.Errorf("empty AllowedEgress should stay on internal-only userNet; got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_PriorityOrderAPIOverAllowedEgress(t *testing.T) {
	// Both API_KEY present AND AllowedEgress set — API_KEY wins the reason
	// (system container), but either way we're on mainNet.
	payload := transport.TaskPayload{
		ContainerEnv:  map[string]string{"API_KEY": "x"},
		AllowedEgress: []string{"api.openai.com"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != mainNet {
		t.Errorf("either rule triggers mainNet; got %q", sel.DockerNetwork)
	}
	if !containsString(sel.Reason, "API_KEY") {
		t.Errorf("API_KEY should dominate the reason; got %q", sel.Reason)
	}
}

func TestSelectNetworkMode_StackNetworkPinsToPostureNet(t *testing.T) {
	// A client task that must reach a co-located ephemeral NIM by service
	// name declares MOD_STACK_NETWORK=mod_stack-api-net. It must land on
	// that posture net, NOT the main/platform bridge.
	payload := transport.TaskPayload{
		NodeKind:     "studiovoice_client",
		ContainerEnv: map[string]string{"MOD_STACK_NETWORK": "mod_stack-api-net"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != "mod_stack-api-net" {
		t.Errorf("MOD_STACK_NETWORK should pin to mod_stack-api-net; got %q", sel.DockerNetwork)
	}
	if sel.DockerNetwork == mainNet {
		t.Error("MOD_STACK_NETWORK must NOT route to the platform/main network")
	}
	if !containsEnv(sel.EnvAdditions, "MOD_EGRESS_ALLOWED=true") {
		t.Error("posture-net task should export MOD_EGRESS_ALLOWED=true")
	}
}

func TestSelectNetworkMode_StackNetworkBeatsAPIKey(t *testing.T) {
	// The Studio Voice client carries BOTH an API_KEY (graceful fallback)
	// and MOD_STACK_NETWORK. The stack-network pin must win — otherwise the
	// API_KEY marker sends it to the platform bridge where NIM-studiovoice
	// does not resolve. This is the exact end-to-end bug being fixed.
	payload := transport.TaskPayload{
		NodeKind: "studiovoice_client",
		ContainerEnv: map[string]string{
			"API_KEY":           "x",
			"MOD_STACK_NETWORK": "mod_stack-api-net",
		},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != "mod_stack-api-net" {
		t.Errorf("MOD_STACK_NETWORK must beat the API_KEY marker; got %q", sel.DockerNetwork)
	}
	if !containsString(sel.Reason, "MOD_STACK_NETWORK") {
		t.Errorf("the stack-net pin should dominate the audit reason; got %q", sel.Reason)
	}
}

func TestSelectNetworkMode_StackNetworkRejectsNonAllowlistedValue(t *testing.T) {
	// Only mod_stack-api-net is honoured. Any other value is rejected and
	// falls through to the normal markers — here, back to API_KEY→mainNet.
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{
			"API_KEY":           "x",
			"MOD_STACK_NETWORK": "mod_modtex-network",
		},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != mainNet {
		t.Errorf("a non-allowlisted MOD_STACK_NETWORK must be ignored and fall through to API_KEY→mainNet; got %q", sel.DockerNetwork)
	}

	// With no other marker, a rejected value falls all the way through to
	// the internal-only default-deny network.
	payloadNoMarker := transport.TaskPayload{
		ContainerEnv: map[string]string{"MOD_STACK_NETWORK": "evil-net"},
	}
	sel2 := SelectNetworkMode(payloadNoMarker, mainNet, userNet, egressNet)
	if sel2.DockerNetwork != userNet {
		t.Errorf("a bogus MOD_STACK_NETWORK with no other marker must default-deny to userNet; got %q", sel2.DockerNetwork)
	}
}

func TestSelectNetworkMode_AllowedEgressSetsFromAllowlist(t *testing.T) {
	payload := transport.TaskPayload{
		AllowedEgress: []string{"api.openai.com", "huggingface.co"},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)

	if !sel.FromAllowlist {
		t.Error("AllowedEgress verdict must mark FromAllowlist=true")
	}
	if !strings.EqualFold(strings.Join(sel.EgressHosts, ","), "api.openai.com,huggingface.co") {
		t.Errorf("AllowedEgress verdict must carry EgressHosts; got %v", sel.EgressHosts)
	}
}

func TestSelectNetworkMode_DefaultDenyIsNotFromAllowlist(t *testing.T) {
	sel := SelectNetworkMode(transport.TaskPayload{NodeKind: "n"}, mainNet, userNet, egressNet)
	if sel.FromAllowlist {
		t.Error("default-deny must NOT be FromAllowlist")
	}
	if len(sel.EgressHosts) != 0 {
		t.Errorf("default-deny must carry no EgressHosts; got %v", sel.EgressHosts)
	}
}

func TestApplyEgressProxyMode_EnforceMarksProxied(t *testing.T) {
	sel := NetworkSelection{
		DockerNetwork: egressNet,
		FromAllowlist: true,
		EgressHosts:   []string{"api.openai.com"},
		EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
	}

	out := ApplyEgressProxyMode(sel, "enforce")

	if !out.Proxied {
		t.Error("enforce proxy mode must set Proxied=true")
	}
	if !containsEnv(out.EnvAdditions, "MOD_EGRESS_ENFORCED=true") {
		t.Errorf("enforce must append MOD_EGRESS_ENFORCED=true; got %v", out.EnvAdditions)
	}
	// The network name is resolved by the executor, not here — enforce must
	// leave it untouched rather than overwriting it.
	if out.DockerNetwork != egressNet {
		t.Errorf("enforce must NOT overwrite the DockerNetwork; got %q", out.DockerNetwork)
	}
	// The pre-existing env entries must survive the append.
	if !containsEnv(out.EnvAdditions, "MOD_EGRESS_ALLOWED=true") {
		t.Error("existing MOD_EGRESS_ALLOWED must survive proxy-mode rewrite")
	}
}

func TestApplyEgressProxyMode_OffSetsMarkerButLeavesNetwork(t *testing.T) {
	sel := NetworkSelection{
		DockerNetwork: egressNet,
		FromAllowlist: true,
		EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
	}

	out := ApplyEgressProxyMode(sel, "off")

	if out.Proxied {
		t.Error("off proxy mode must NOT set Proxied")
	}
	if !containsEnv(out.EnvAdditions, "MOD_EGRESS_ENFORCED=false") {
		t.Errorf("off must append MOD_EGRESS_ENFORCED=false; got %v", out.EnvAdditions)
	}
	if out.DockerNetwork != egressNet {
		t.Errorf("off must leave the DockerNetwork unchanged; got %q", out.DockerNetwork)
	}
}

func TestApplyEgressProxyMode_NonAllowlistPassesThrough(t *testing.T) {
	// A platform/main verdict must be left completely untouched — no Proxied
	// marker, no MOD_EGRESS_ENFORCED env, network name unchanged.
	sel := NetworkSelection{
		DockerNetwork: mainNet,
		Reason:        "PLATFORM_TASK — MOD-owned system container",
		EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
	}

	out := ApplyEgressProxyMode(sel, "enforce")

	if out.Proxied {
		t.Error("non-allowlist verdict must NOT become Proxied")
	}
	if containsEnv(out.EnvAdditions, "MOD_EGRESS_ENFORCED") {
		t.Errorf("non-allowlist verdict must carry no MOD_EGRESS_ENFORCED marker; got %v", out.EnvAdditions)
	}
	if out.DockerNetwork != mainNet {
		t.Errorf("non-allowlist verdict DockerNetwork must be unchanged; got %q", out.DockerNetwork)
	}
}

func TestSelectNetworkMode_EgressNetworkDiffersFromMain(t *testing.T) {
	// Important: the egress network must NOT equal the main network,
	// otherwise a tenant with AllowedEgress=["example.com"] would reach
	// platform services (API / Redis / Vault) just because the host
	// string happened to match. The two networks carry different DNS
	// records and different connectivity.
	if userNet == egressNet || mainNet == egressNet {
		t.Error("egressNet must be distinct from userNet AND mainNet for isolation to mean anything")
	}
}

func TestEffectiveEgressProxyMode(t *testing.T) {
	type tc struct {
		fleet    string
		egress   string
		expected string
	}
	cases := []tc{
		// Known payload modes always resolve to "enforce", regardless of fleet.
		{"off", "direct", "enforce"},
		{"off", "mediated", "enforce"},
		{"report", "direct", "enforce"},
		{"report", "mediated", "enforce"},
		{"enforce", "direct", "enforce"},
		{"enforce", "mediated", "enforce"},
		// Unknown non-empty payload mode fails closed.
		{"off", "tunnel", "enforce"},
		{"report", "shim", "enforce"},
		{"enforce", "foobar", "enforce"},
		// Empty payload mode passes through fleet unchanged.
		{"off", "", "off"},
		{"report", "", "report"},
		{"enforce", "", "enforce"},
	}

	for _, tc := range cases {
		t.Run(tc.fleet+"_"+tc.egress, func(t *testing.T) {
			p := transport.TaskPayload{EgressMode: tc.egress}
			got := EffectiveEgressProxyMode(tc.fleet, p)
			if got != tc.expected {
				t.Errorf("fleet=%q mode=%q => %q, want %q", tc.fleet, tc.egress, got, tc.expected)
			}
		})
	}

	// The single rule that matters: a non-empty payload mode NEVER widens
	// the fleet mode.  Fleet "off" + payload mode -> enforce.
	p := transport.TaskPayload{EgressMode: "mediated"}
	if EffectiveEgressProxyMode("off", p) != "enforce" {
		t.Fatal("payload mode must never widen fleet mode")
	}
}

// helpers
func containsEnv(env []string, needle string) bool {
	for _, e := range env {
		if e == needle {
			return true
		}
	}
	return false
}

func containsString(s, needle string) bool {
	return len(s) >= len(needle) && (s == needle || indexOf(s, needle) >= 0)
}

func indexOf(s, needle string) int {
	for i := 0; i+len(needle) <= len(s); i++ {
		if s[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

// ---------------------------------------------------------------------------
// FX.91 p5 (owner 10-07 Q5=A) — gov profile + node declaring NO egress_domains
// gets NO external network. The API stamps the task (routers/worker_tasks.py
// _stamp_gov_internal_only); netpol parks it on the internal-only posture net
// instead of the main network the keyed markers would have opened.
// ---------------------------------------------------------------------------

const govInternalOnly = "MOD_GOV_UNDECLARED_INTERNAL_ONLY"

func TestSelectNetworkMode_GovUndeclaredBeatsAPIKey(t *testing.T) {
	// The hole p5 closes: API_KEY alone used to open the MAIN network (internet
	// + platform services) for a node that declares nothing.
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{
			"API_KEY":       "x",
			govInternalOnly: "1",
		},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != stackNet {
		t.Errorf("stamped API_KEY node must land on the internal-only posture net %q; got %q", stackNet, sel.DockerNetwork)
	}
	if sel.FromAllowlist {
		t.Error("a gov-undeclared verdict is never an allowlist verdict (it grants no outbound)")
	}
	if !containsEnv(sel.EnvAdditions, "MOD_EGRESS_ALLOWED=false") {
		t.Error("gov-undeclared verdict must advertise MOD_EGRESS_ALLOWED=false")
	}
}

func TestSelectNetworkMode_GovUndeclaredBeatsTokenURL(t *testing.T) {
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{
			"MOD_TASK_TOKEN_URL": "http://host.docker.internal:9179/run-token",
			govInternalOnly:      "1",
		},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != stackNet {
		t.Errorf("stamped token-fetch node must stay internal-only; got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_GovUndeclaredBeatsNIMKey(t *testing.T) {
	// NIM nodes declare their NVIDIA hosts (FX.91 p4c), so the API does NOT
	// stamp them; if a stamp arrives anyway it must never grant the NVIDIA
	// hosts by way of NIM_API_KEY.
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{
			"NIM_API_KEY":   "k",
			govInternalOnly: "1",
		},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != stackNet {
		t.Errorf("stamped NIM-key node must not get the main network; got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_PlatformTaskBeatsGovStamp(t *testing.T) {
	// The stamp can only RESTRICT: a MOD-owned system container keeps its
	// main-network verdict.
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{
			"PLATFORM_TASK": "1",
			govInternalOnly: "1",
		},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != mainNet {
		t.Errorf("PLATFORM_TASK must beat the gov stamp; got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_StackNetworkBeatsGovStamp(t *testing.T) {
	// An explicit co-location request (NIM client) is a platform placement
	// decision and keeps its verdict — same net, but the platform reason.
	payload := transport.TaskPayload{
		ContainerEnv: map[string]string{
			"MOD_STACK_NETWORK": stackNet,
			govInternalOnly:     "1",
		},
	}
	sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
	if sel.DockerNetwork != stackNet {
		t.Errorf("MOD_STACK_NETWORK must keep its posture verdict; got %q", sel.DockerNetwork)
	}
}

func TestSelectNetworkMode_NoStampUnchanged(t *testing.T) {
	// Non-gov behaviour byte-for-byte: no stamp → the keyed markers still open
	// the main network exactly as before.
	for _, key := range []string{"API_KEY", "MOD_TASK_TOKEN_URL", "NIM_API_KEY"} {
		payload := transport.TaskPayload{ContainerEnv: map[string]string{key: "v"}}
		sel := SelectNetworkMode(payload, mainNet, userNet, egressNet)
		if sel.DockerNetwork != mainNet {
			t.Errorf("unstamped %s node should still be on mainNet; got %q", key, sel.DockerNetwork)
		}
	}
}
