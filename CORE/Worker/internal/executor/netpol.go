package executor

import (
	"context"
	"strings"
	"time"

	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// netpolDocker is the narrow slice of the Docker client the FX.91 p5 posture
// network needs: inspect (to read the live Internal flag) and create. Mirrors
// userNetworkProbe and the egress proxy's dockerAPI subset so the behaviour is
// unit-testable against a fake client.
type netpolDocker interface {
	NetworkInspect(ctx context.Context, networkID string, options network.InspectOptions) (network.Inspect, error)
	NetworkCreate(ctx context.Context, name string, options network.CreateOptions) (network.CreateResponse, error)
}

var _ netpolDocker = (*client.Client)(nil)

// NetpolPosture is what this worker observed about the internal-only posture
// network a FX.91 p5 stamped node is parked on. It is reported by NetpolStatus
// so the API/Dashboard can see whether the posture is actually enforced on this
// host — a net that exists WITHOUT Internal is an egress gap we cannot fix by
// recreating it (live containers are attached), so it has to be visible.
type NetpolPosture struct {
	Network      string    // the posture network inspected
	Internal     bool      // the live network's Internal flag
	Created      bool      // this worker created it (with the requested Internal)
	Conflict     bool      // internal-only purpose but NOT internal — stamped nodes can still egress
	Observations int       // how many times this worker has checked it
	ObservedAt   time.Time // last observation
}

// ensurePostureNetwork makes the posture network `name` exist on this host and
// reports what posture was actually observed. FX.91 p5: when the caller's
// verdict is internal-only (a gov-stamped node), the network we create is
// created WITH Internal: true, so the "internal-only" claim is enforced rather
// than just asserted.
//
// When the network already exists we never delete or recreate it — live
// containers are attached to it and recreating would drop them. If it exists
// WITHOUT Internal, that is an egress gap we can only report: WARN once and
// record the posture for the heartbeat. Callers that need egress pass
// internalOnly=false and get the pre-existing behaviour unchanged.
func (e *Executor) ensurePostureNetwork(ctx context.Context, d netpolDocker, name string, internalOnly bool) NetpolPosture {
	if ins, err := d.NetworkInspect(ctx, name, network.InspectOptions{}); err == nil {
		// Already present — the normal case on the NIM's host. Never delete or
		// recreate it: live containers are attached. Read the live flag.
		e.recordNetpolPosture(name, ins.Internal, false, internalOnly)
		if internalOnly && !ins.Internal {
			e.warnPostureNotInternal(name)
		}
		return e.NetpolStatusPosture()
	}
	_, err := d.NetworkCreate(ctx, name, network.CreateOptions{
		Driver:   "bridge",
		Internal: internalOnly,
		Labels:   map[string]string{"mod.purpose": netpolPosturePurposeLabel(internalOnly)},
	})
	if err != nil && !strings.Contains(err.Error(), "already exists") {
		e.log.Warn("could not ensure posture network exists; attach may fail",
			"network", name, "internal", internalOnly, "error", err)
		e.recordNetpolPosture(name, false, false, internalOnly)
		return e.NetpolStatusPosture()
	}
	// "already exists" means another process (the NIM stack's compose, or the
	// API) created it first, so its Internal flag is unknown from here — record
	// it as not-created rather than claiming we enforced it.
	created := err == nil
	e.recordNetpolPosture(name, internalOnly && created, created, internalOnly)
	if internalOnly && !created {
		e.warnPostureNotInternal(name)
	}
	return e.NetpolStatusPosture()
}

// NetpolStatusPosture returns a copy of the recorded posture under the lock.
func (e *Executor) NetpolStatusPosture() NetpolPosture {
	e.netpolMu.RLock()
	defer e.netpolMu.RUnlock()
	if e.netpolPosture == nil {
		return NetpolPosture{}
	}
	cp := *e.netpolPosture
	return cp
}

func netpolPosturePurposeLabel(internalOnly bool) string {
	if internalOnly {
		return "gov-undeclared-internal-only-posture"
	}
	return "nim-stack-posture-net"
}

// recordNetpolPosture stores an observation of the posture network.
func (e *Executor) recordNetpolPosture(name string, internal, created, internalOnly bool) {
	e.netpolMu.Lock()
	defer e.netpolMu.Unlock()
	if e.netpolPosture == nil {
		e.netpolPosture = &NetpolPosture{Network: name}
	}
	e.netpolPosture.Network = name
	e.netpolPosture.Internal = internal
	e.netpolPosture.Created = created
	// Only an internal-only purpose can be in conflict: an egress-purpose net
	// without Internal is exactly what it was asked to be.
	e.netpolPosture.Conflict = internalOnly && !internal
	e.netpolPosture.Observations++
	e.netpolPosture.ObservedAt = time.Now()
}

// warnPostureNotInternal logs the FX.91 p5 posture WARN once per process. The
// first observation is the one that matters to an operator; a per-task repeat
// would flood the log for every stamped node.
func (e *Executor) warnPostureNotInternal(name string) {
	e.netpolMu.Lock()
	first := !e.netpolPostureWarned
	if first {
		e.netpolPostureWarned = true
	}
	e.netpolMu.Unlock()
	if first {
		e.log.Warn("FX.91 p5 internal-only posture network is NOT internal-only",
			"network", name,
			"posture", "exists without Internal:true — stamped nodes can still egress",
			"action", "not recreated (live containers use it); recreate on a clean host or fix the compose that created it",
		)
	}
}

// NetpolStatus is the heartbeat-facing view of the posture observation. Returns
// nil when nothing has been observed yet, so the heartbeat omits the key rather
// than claiming a posture we never checked.
func (e *Executor) NetpolStatus() map[string]interface{} {
	e.netpolMu.RLock()
	p := e.netpolPosture
	warned := e.netpolPostureWarned
	e.netpolMu.RUnlock()
	if p == nil {
		return nil
	}
	return map[string]interface{}{
		"network":           p.Network,
		"internal":          p.Internal,
		"created_by_worker": p.Created,
		"egress_gap":        p.Conflict,
		"warned":            warned,
		"observations":      p.Observations,
		"observed_at":       p.ObservedAt.UTC().Format(time.RFC3339),
	}
}

// NetworkSelection captures the per-container network decision + the
// env entries we want pushed into the container so tenant code and
// operators can see the egress posture from inside.
type NetworkSelection struct {
	// DockerNetwork is the docker network the container joins. Either
	// the isolated user-sandbox network (no access to API/Redis/Vault)
	// or the main network (access to external hosts + platform services).
	DockerNetwork string

	// Reason is a short human-readable string suitable for audit logs.
	Reason string

	// EnvAdditions are extra env entries to append to the container's
	// environment. Carries MOD_EGRESS_ALLOWED=true + a comma-joined
	// allowlist so a tenant's code (and operators trawling logs) can
	// see the declared outbound hosts without digging into the DB.
	EnvAdditions []string

	// FromAllowlist is true only when the verdict was the AllowedEgress
	// branch — the network is externally routable rather than MOD-owned
	// platform/main, so it's the only verdict ApplyEgressProxyMode is
	// entitled to rewrite.
	FromAllowlist bool

	// Proxied is set true when the network is routed through the tenant
	// egress proxy bridge (ApplyEgressProxyMode with proxyMode="enforce").
	// It is advisory to the executor: the network name itself is filled
	// in later by the executor, not here.
	Proxied bool

	// EgressHosts is the allowlisted set of outbound hosts that this
	// proxy-mode network is scoped to (the AllowedEgress payload). The
	// executor binds the egress proxy bridge to exactly these hosts.
	EgressHosts []string

	// InternalOnly is true when the verdict's network is supposed to refuse
	// every outbound connection — the FX.91 p5 gov-undeclared posture verdict
	// and the default-deny user network. It is what tells the executor to
	// create a missing posture network WITH Internal: true instead of the plain
	// bridge the NIM stack-posture net has.
	InternalOnly bool
}

// stackAPINetwork is the single docker network a task is permitted to
// request by name via ContainerEnv["MOD_STACK_NETWORK"]. It is the
// posture network the ephemeral NIM stacks are pinned to (declared
// external by the NIM compose and created by ensureExternalNetworks on
// the host), so a client task that must reach a co-located NIM by its
// service name (e.g. NIM-studiovoice:8001) has to land on it too. The
// allowlist is a single literal value on purpose: this marker lets a
// payload name a network directly, so anything other than this exact
// name is rejected and falls through to the normal policy.
const stackAPINetwork = "mod_stack-api-net"

// govUndeclaredInternalOnlyMarker is the FX.91 p5 stamp the API writes onto a
// task whose node declares NO egress_domains while a gov/DoD compliance profile
// is active (routers/worker_tasks.py _stamp_gov_internal_only). It is a
// platform-reserved container_env key (TENANT_RESERVED_CONTAINER_ENV), so a
// tenant cannot set it, and it can only ever RESTRICT a container — it never
// grants outbound. See the policy note below for how it is honoured.
const govUndeclaredInternalOnlyMarker = "MOD_GOV_UNDECLARED_INTERNAL_ONLY"

// SelectNetworkMode picks the docker network for a task based on the
// payload. Pure function — no side effects, no Docker calls — so it's
// the one piece of egress policy we can unit-test deterministically.
//
// Policy, in priority order:
//  0. PLATFORM_TASK flag set → main network. Explicit marker the API
//     puts on its own system containers (Containerizer,
//     ImageBuilder, ContainerAdapter, nightly base-image rebuilds).
//     Distinct from API_KEY so the policy reason is auditable and
//     doesn't require handing every MOD task a real API key just to
//     get onto the right bridge.
//     0b. Container declares MOD_STACK_NETWORK and it is allowlisted to
//     mod_stack-api-net — join that posture network so the task can
//     resolve a co-located ephemeral NIM by service name. Checked
//     ABOVE the API_KEY marker: the Studio Voice client carries both an
//     API_KEY (its graceful fallback) and MOD_STACK_NETWORK, and the
//     API_KEY marker would otherwise route it to the platform bridge
//     where the NIM's service name does not resolve. Any other value is
//     rejected and falls through to the markers below.
//  1. Container declares API_KEY / NIM_API_KEY — main network (access
//     to platform services like the API or internal NIM catalog).
//  2. NEEDS_INTERNET flag set → egress network (external only, no
//     platform services).
//  3. AllowedEgress non-empty → egress network; the allowlist is
//     exported into MOD_ALLOWED_EGRESS_HOSTS so tenant code can
//     self-enforce and operators can audit.
//  4. Default → internal-only user network (Internal=true on the
//     Docker bridge). Zero outbound, zero access to platform services.
//     A compromised node here cannot open a TCP socket outside the
//     bridge — not to evilcorp, not to the MOD API, not to Vault.
//
// FX.91 p5 (owner 10-07 Q5=A) — UNDECLARED NODES GET NO EXTERNAL NETWORK
// UNDER A GOV PROFILE. Branches 1/2/3 above (API_KEY ~:104, MOD_TASK_TOKEN_URL
// ~:116, NIM_API_KEY ~:123) all open the MAIN network, which is internet +
// every platform service. Under a gov compliance profile that is wrong for a
// node whose manifest declares NO egress_domains: the node vouches for nothing,
// so it must not get external network — but it must still report results, so
// the MOD API / task-token callback has to stay open. The API stamps the task
// with MOD_GOV_UNDECLARED_INTERNAL_ONLY (routers/worker_tasks.py
// _stamp_gov_internal_only) when it resolves such a node under an active gov
// profile; here we honour that stamp by parking the container on the EXISTING
// internal-only posture net (mod_stack-api-net — Internal: true, the platform
// API attached, no internet; the same posture services/k8s_stack_netpol.py
// mode 'api' enforces on the k8s backend) instead of the main network. The
// stamp can only ever RESTRICT: it is checked BELOW PLATFORM_TASK and MOD_STACK
// NETWORK (both platform placement decisions) and it never grants outbound.
func SelectNetworkMode(
	payload transport.TaskPayload,
	mainNetwork, userNetwork, egressNetwork string,
) NetworkSelection {
	if _, ok := payload.ContainerEnv["PLATFORM_TASK"]; ok {
		return NetworkSelection{
			DockerNetwork: mainNetwork,
			Reason:        "PLATFORM_TASK — MOD-owned system container",
			EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
		}
	}
	if v, ok := payload.ContainerEnv["MOD_STACK_NETWORK"]; ok && v == stackAPINetwork {
		return NetworkSelection{
			DockerNetwork: stackAPINetwork,
			Reason:        "MOD_STACK_NETWORK=" + stackAPINetwork + " — join posture net to reach co-located NIM by service name",
			EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
		}
	}
	// FX.91 p5 (owner 10-07 Q5=A) — gov profile + node declaring NO egress_domains.
	// The API stamps such a task with MOD_GOV_UNDECLARED_INTERNAL_ONLY
	// (routers/worker_tasks.py _stamp_gov_internal_only; a platform-reserved key
	// a tenant cannot set). Without this branch the keyed markers below would
	// open the MAIN network — internet + every platform service — for a node
	// whose manifest vouches for nothing. Park it on the EXISTING internal-only
	// posture net instead: the MOD API / task-token endpoints resolve there
	// (verified live: mod_stack-api-net carries my_fastapi_app + the 'api'
	// alias, Internal: true, so the node still reports its results) and the net
	// refuses every outbound connection. Checked BELOW PLATFORM_TASK and MOD_STACK_NETWORK
	// (both platform placement decisions, so a MOD-owned container and an
	// explicitly co-located NIM client keep their existing verdicts) and ABOVE
	// the keyed branches — the stamp can only RESTRICT, never widen.
	// MOD_EGRESS_ALLOWED=false: this verdict grants no outbound, unlike every
	// other posture above.
	if _, ok := payload.ContainerEnv[govUndeclaredInternalOnlyMarker]; ok {
		return NetworkSelection{
			DockerNetwork: stackAPINetwork,
			Reason:        "MOD_GOV_UNDECLARED_INTERNAL_ONLY — gov profile, node declares no egress: internal-only posture net (API callback open, zero internet)",
			EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=false"},
			InternalOnly:  true,
		}
	}
	if _, ok := payload.ContainerEnv["API_KEY"]; ok {
		return NetworkSelection{
			DockerNetwork: mainNetwork,
			Reason:        "API_KEY present — system container calling back to API",
			EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
		}
	}
	// worker-mediated-node-credentials (Group 5.3): a container declaring
	// MOD_TASK_TOKEN_URL fetches short-lived run_task tokens from the worker's
	// local token endpoint, so it needs worker + API reachability exactly like
	// an API_KEY container. This is the replacement marker for API_KEY; both are
	// honoured during migration (API_KEY is removed at cutover, Group 9.1).
	if _, ok := payload.ContainerEnv["MOD_TASK_TOKEN_URL"]; ok {
		return NetworkSelection{
			DockerNetwork: mainNetwork,
			Reason:        "MOD_TASK_TOKEN_URL present — node fetches worker-vended run tokens",
			EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
		}
	}
	if _, ok := payload.ContainerEnv["NIM_API_KEY"]; ok {
		return NetworkSelection{
			DockerNetwork: mainNetwork,
			Reason:        "NIM_API_KEY present — NIM bridge needs NVIDIA endpoints",
			EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
		}
	}
	if _, ok := payload.ContainerEnv["NEEDS_INTERNET"]; ok {
		return NetworkSelection{
			DockerNetwork: egressNetwork,
			Reason:        "NEEDS_INTERNET flag set",
			EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
		}
	}

	if len(payload.AllowedEgress) > 0 {
		joined := strings.Join(payload.AllowedEgress, ",")
		return NetworkSelection{
			DockerNetwork: egressNetwork,
			Reason:        "AllowedEgress declared (" + joined + ")",
			EnvAdditions: []string{
				"MOD_EGRESS_ALLOWED=true",
				"MOD_ALLOWED_EGRESS_HOSTS=" + joined,
			},
			FromAllowlist: true,
			EgressHosts:   payload.AllowedEgress,
		}
	}

	return NetworkSelection{
		DockerNetwork: userNetwork,
		Reason:        "default-deny — internal-only user network (zero outbound)",
		EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=false"},
		InternalOnly:  true,
	}
}

// EffectiveEgressProxyMode computes the proxy enforcement verdict for a task
// by considering the fleet-wide MOD_EGRESS_PROXY setting alongside any
// per-task egress mode stamp from the API.
//
// Rules:
//   - payload.EgressMode "direct" or "mediated" → "enforce"
//   - payload.EgressMode "" (unset) → return the fleet value unchanged
//     (the existing MOD_EGRESS_PROXY setting on this worker node).
//   - any other non-empty payload.EgressMode → "enforce" (fail-closed).
//
// A payload mode can NEVER widen the fleet mode — it can only narrow to
// "enforce" (when non-empty) or pass through (when empty).  It will never
// return "off" just because the payload says so.
func EffectiveEgressProxyMode(fleet string, p transport.TaskPayload) string {
	switch p.EgressMode {
	case "direct", "mediated":
		return "enforce"
	case "":
		return fleet
	default:
		// Unknown non-empty mode — fail closed.
		return "enforce"
	}
}

// ApplyEgressProxyMode rewrites a network verdict from SelectNetworkMode
// when the tenant has opted the allowlisted egress network into the
// external proxy bridge. It ONLY rewrites verdicts that came from the
// AllowedEgress branch (FromAllowlist) — a platform/main or stack-posture
// verdict is never proxied, and a non-proxy mode leaves everything as-is
// except flipping the MOD_EGRESS_ENFORCED marker.
//
//   - proxyMode == "enforce": Proxied=true and MOD_EGRESS_ENFORCED=true.
//     The DockerNetwork name is deliberately left blank here — the
//     executor fills in the real egress-proxy bridge net once it has
//     confirmed the bridge exists; Proxied is the signal that triggers
//     that resolution.
//   - anything else: the verdict passes through with
//     MOD_EGRESS_ENFORCED=false so downstream code can tell the proxy is
//     off even for an allowlisted network.
func ApplyEgressProxyMode(sel NetworkSelection, proxyMode string) NetworkSelection {
	if !sel.FromAllowlist {
		return sel
	}
	if proxyMode == "enforce" {
		sel.Proxied = true
		sel.EnvAdditions = append(sel.EnvAdditions, "MOD_EGRESS_ENFORCED=true")
		return sel
	}
	sel.EnvAdditions = append(sel.EnvAdditions, "MOD_EGRESS_ENFORCED=false")
	return sel
}
