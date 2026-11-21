package executor

import (
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// GS.PSR.7 — currentEgressProxyMode/preferLocalImages must resolve with
// precedence: server-pushed tunable > local env-derived default > built-in
// default. These are pure (no Docker client needed), so &Executor{} struct
// literals are used directly, matching this package's existing test
// convention (see netpol_test.go, checkpoint_test.go).

func TestCurrentEgressProxyMode_NoTunablesStoreUsesLocalDefault(t *testing.T) {
	e := &Executor{egressProxyMode: "off"}
	if got := e.currentEgressProxyMode(); got != "off" {
		t.Errorf("with no tunables store, should return the local env-derived default: got %q", got)
	}
}

func TestCurrentEgressProxyMode_EmptyStoreFallsBackToLocalDefault(t *testing.T) {
	e := &Executor{egressProxyMode: "enforce", tunables: tunables.New()}
	if got := e.currentEgressProxyMode(); got != "enforce" {
		t.Errorf("empty store (no heartbeat yet) should fall back to local default: got %q", got)
	}
}

func TestCurrentEgressProxyMode_APIValueOverridesLocalDefault(t *testing.T) {
	store := tunables.New()
	store.Apply(map[string]interface{}{"worker.egress_proxy_mode": "off"})
	e := &Executor{egressProxyMode: "enforce", tunables: store}
	if got := e.currentEgressProxyMode(); got != "off" {
		t.Errorf("server-pushed tunable should override the local default: got %q", got)
	}
}

func TestCurrentEgressProxyMode_UnrecognizedAPIValueFallsBackToLocalDefault(t *testing.T) {
	store := tunables.New()
	store.Apply(map[string]interface{}{"worker.egress_proxy_mode": "bogus"})
	e := &Executor{egressProxyMode: "enforce", tunables: store}
	if got := e.currentEgressProxyMode(); got != "enforce" {
		t.Errorf("an unrecognized API value should degrade to the local default: got %q", got)
	}
}

func TestCurrentEgressProxyMode_ChangeAppliedOnNextHeartbeat(t *testing.T) {
	store := tunables.New()
	e := &Executor{egressProxyMode: "enforce", tunables: store}
	if got := e.currentEgressProxyMode(); got != "enforce" {
		t.Fatalf("precondition: expected local default 'enforce', got %q", got)
	}
	// Simulate a heartbeat response landing.
	store.Apply(map[string]interface{}{"worker.egress_proxy_mode": "off"})
	if got := e.currentEgressProxyMode(); got != "off" {
		t.Errorf("value should change on the very next read after Apply (next heartbeat): got %q", got)
	}
	// Simulate the operator clearing the platform setting.
	store.Apply(nil)
	if got := e.currentEgressProxyMode(); got != "enforce" {
		t.Errorf("clearing the API override should revert to the local default: got %q", got)
	}
}

func TestPreferLocalImages_NoTunablesStoreReadsEnv(t *testing.T) {
	e := &Executor{}
	t.Setenv("MOD_WORKER_PREFER_LOCAL_IMAGES", "1")
	if !e.preferLocalImages() {
		t.Error("with no tunables store, should fall back to reading the env var directly")
	}
}

func TestPreferLocalImages_APIValueOverridesEnv(t *testing.T) {
	t.Setenv("MOD_WORKER_PREFER_LOCAL_IMAGES", "0")
	store := tunables.New()
	store.Apply(map[string]interface{}{"worker.prefer_local_images": true})
	e := &Executor{tunables: store}
	if !e.preferLocalImages() {
		t.Error("server-pushed tunable (true) should override local env (0)")
	}
}

func TestPreferLocalImages_EnvFallbackWhenAPIUnset(t *testing.T) {
	t.Setenv("MOD_WORKER_PREFER_LOCAL_IMAGES", "1")
	e := &Executor{tunables: tunables.New()}
	if !e.preferLocalImages() {
		t.Error("empty store should fall back to env var")
	}
}

func TestPreferLocalImages_DefaultFalseWhenNeitherSet(t *testing.T) {
	t.Setenv("MOD_WORKER_PREFER_LOCAL_IMAGES", "")
	e := &Executor{tunables: tunables.New()}
	if e.preferLocalImages() {
		t.Error("should default to false when neither API nor env is set")
	}
}
