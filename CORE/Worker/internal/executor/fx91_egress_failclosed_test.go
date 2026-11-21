package executor

// FX.91 step 2 (owner decision Q77=A — node container egress default-deny),
// design items 1 and 2:
//
//   1. NetworkCreate failure must NOT fall back to the main network. The
//      worker keeps the isolated user-network name, and the per-task network
//      selection site refuses to start a user container whose network does
//      not exist (requireUserNetwork / NetworkInspect).
//   2. An AllowedEgress task must never run on bare mod-user-egress without
//      the proxy bridge: any non-"enforce" mode is a hard error.
//
import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/docker/docker/api/types/network"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

const (
	fx91MainNet = "mod_core_network"
	fx91UserNet = "mod-user-internal"
	fx91EgrNet  = "mod-user-egress"
)

// fx91Probe is a stub userNetworkProbe: fx91Missing names the network it
// reports as absent; "" means every network exists.
type fx91Probe struct {
	fx91Missing string
}

func (p *fx91Probe) NetworkInspect(_ context.Context, name string, _ network.InspectOptions) (network.Inspect, error) {
	if p.fx91Missing != "" && p.fx91Missing == name {
		return network.Inspect{}, fx91ProbeErr
	}
	return network.Inspect{}, nil
}

var fx91ProbeErr = errors.New("network not found")

// ---------------------------------------------------------------------------
// Design item 1a: the create-failure decision is pure and never widens to main.
// ---------------------------------------------------------------------------

func TestFx91UserNetworkAfterCreate(t *testing.T) {
	t.Run("nil error is success", func(t *testing.T) {
		name, ok := _userNetworkAfterCreate(fx91UserNet, nil)
		if name != fx91UserNet || !ok {
			t.Errorf("want (%s,true), got (%s,%v)", fx91UserNet, name, ok)
		}
	})

	t.Run("already exists is success", func(t *testing.T) {
		name, ok := _userNetworkAfterCreate(fx91EgrNet, errors.New("NetworkCreate: 407 Conflict: 'mod-user-egress' already exists"))
		if name != fx91EgrNet || !ok {
			t.Errorf("want (%s,true), got (%s,%v)", fx91EgrNet, name, ok)
		}
	})

	// The regression this closes: the old code did `userNet = dockerNetwork`
	// on ANY other failure, putting user containers on the main network.
	t.Run("hard failure keeps isolated name, never main", func(t *testing.T) {
		name, ok := _userNetworkAfterCreate(fx91UserNet, errors.New("NetworkCreate: 500 daemon out of netns slots"))
		if name != fx91UserNet {
			t.Errorf("network must stay %s on failure; got %q", fx91UserNet, name)
		}
		if ok {
			t.Error("createdOk must be false for a non-already-exists failure")
		}
		if name == fx91MainNet {
			t.Error("fell back to the main network")
		}
	})
}

// ---------------------------------------------------------------------------
// Design item 1b: the per-task check refuses a user container whose network
// is missing, instead of letting it land on main.
// ---------------------------------------------------------------------------

func TestFx91RequireUserNetwork(t *testing.T) {
	t.Run("present network passes", func(t *testing.T) {
		if err := requireUserNetwork(&fx91Probe{}, fx91UserNet); err != nil {
			t.Errorf("want nil, got %q", err)
		}
	})

	t.Run("missing mod-user-internal fails closed", func(t *testing.T) {
		err := requireUserNetwork(&fx91Probe{fx91Missing: fx91UserNet}, fx91UserNet)
		if err == nil {
			t.Fatal("a missing default-deny home must be refused")
		}
		if !strings.Contains(err.Error(), "refusing to run user container on main network") {
			t.Errorf("error must name the refusal, got %q", err)
		}
		if !strings.Contains(err.Error(), fx91UserNet) {
			t.Errorf("error must name the network, got %q", err)
		}
	})

	t.Run("missing mod-user-egress fails closed", func(t *testing.T) {
		err := requireUserNetwork(&fx91Probe{fx91Missing: fx91EgrNet}, fx91EgrNet)
		if err == nil || !strings.Contains(err.Error(), fx91EgrNet) {
			t.Errorf("want an error naming %s, got %v", fx91EgrNet, err)
		}
	})
}

// ---------------------------------------------------------------------------
// Design item 2: AllowedEgress + non-enforce mode is a hard error; enforce
// keeps the proxy path untouched.
// ---------------------------------------------------------------------------

func fx91AllowlistSel() NetworkSelection {
	return NetworkSelection{
		DockerNetwork: fx91EgrNet,
		Reason:        "AllowedEgress declared (api.example.com)",
		EnvAdditions:  []string{"MOD_EGRESS_ALLOWED=true"},
		FromAllowlist: true,
		EgressHosts:   []string{"api.example.com"},
	}
}

func TestFx91EgressGuard(t *testing.T) {
	t.Run("enforce engages the proxy so no guard", func(t *testing.T) {
		proxied := ApplyEgressProxyMode(fx91AllowlistSel(), "enforce")
		if !proxied.Proxied {
			t.Fatal("enforce must flip Proxied (proxy path unchanged)")
		}
		if proxied.DockerNetwork != fx91EgrNet {
			t.Errorf("enforce must not rename the verdict's network here, got %q", proxied.DockerNetwork)
		}
		if err := _egressGuard(proxied, "enforce"); err != nil {
			t.Errorf("want nil for a proxied verdict, got %q", err)
		}
	})

	t.Run("off refuses the unproxied allowlist verdict", func(t *testing.T) {
		unproxied := ApplyEgressProxyMode(fx91AllowlistSel(), "off")
		if unproxied.Proxied {
			t.Fatal("off must not engage the proxy")
		}
		err := _egressGuard(unproxied, "off")
		if err == nil {
			t.Fatal("AllowedEgress + mode off must fail, not run unenforced")
		}
		if !strings.Contains(err.Error(), "bare mod-user-egress") {
			t.Errorf("error must explain the unproxied-egress refusal, got %q", err)
		}
	})

	// There is no audit-only escape hatch: a report-only mode would leave the
	// task unproxied, so it is treated exactly like "off".
	t.Run("audit mode is treated like off", func(t *testing.T) {
		unproxied := ApplyEgressProxyMode(fx91AllowlistSel(), "audit")
		if err := _egressGuard(unproxied, "audit"); err == nil {
			t.Fatal("audit mode must not grant unproxied outbound access")
		}
	})

	// Design item 3 is a later task: marker/platform verdicts keep their
	// current behaviour, so the guard must not touch them.
	t.Run("non-allowlist verdicts are untouched", func(t *testing.T) {
		platform := SelectNetworkMode(transport.TaskPayload{ContainerEnv: map[string]string{"PLATFORM_TASK": "1"}},
			fx91MainNet, fx91UserNet, fx91EgrNet)
		if platform.FromAllowlist {
			t.Fatal("PLATFORM_TASK verdict must not be an allowlist verdict")
		}
		if err := _egressGuard(platform, "off"); err != nil {
			t.Errorf("platform/main marker tasks keep their current behaviour; got %q", err)
		}
		deny := SelectNetworkMode(transport.TaskPayload{}, fx91MainNet, fx91UserNet, fx91EgrNet)
		if err := _egressGuard(deny, "off"); err != nil {
			t.Errorf("default-deny home must still run; got %q", err)
		}
	})
}

// The mode the task actually sees is normalised: anything but "enforce"
// (including a bogus/audit value pushed via the platform-settings tunable)
// collapses to "off", which the guard then refuses.
func TestFx91EgressProxyModeForTask(t *testing.T) {
	t.Run("enforce stays enforce", func(t *testing.T) {
		e := &Executor{egressProxyMode: "enforce"}
		if got := e.egressProxyModeForTask(); got != "enforce" {
			t.Errorf("want enforce, got %q", got)
		}
	})

	t.Run("off tunable collapses to off", func(t *testing.T) {
		store := tunables.New()
		store.Apply(map[string]interface{}{"worker.egress_proxy_mode": "off"})
		e := &Executor{egressProxyMode: "enforce", tunables: store}
		if got := e.egressProxyModeForTask(); got != "off" {
			t.Errorf("a non-enforce tunable must collapse to off (guard refuses it); got %q", got)
		}
	})

	// A value outside the registry's allowed set ("audit") is rejected by the
	// tunable store, so it falls back to the env-derived default — and if that
	// default is itself not "enforce", the task mode is still "off".
	t.Run("no tunables store, env-derived off stays off", func(t *testing.T) {
		e := &Executor{egressProxyMode: "off"}
		if got := e.egressProxyModeForTask(); got != "off" {
			t.Errorf("want off, got %q", got)
		}
	})
}
