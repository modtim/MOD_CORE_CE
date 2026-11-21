// GSEC FX.115 p1a — lockdown-flag tests: each flag x {off, report, enforce}
// x {normal node, exempt node}, plus the nil-store default (report), the
// "posture already present" case that keeps Needed=false, and the allow-list
// exemption matching. Pure functions, no Docker client needed — this
// package's existing convention (see tunables_precedence_test.go).
package executor

import (
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// storeWith builds a tunables store that sets ONE knob to a mode.
func storeWith(key, mode string) *tunables.Store {
	s := tunables.New()
	s.Apply(map[string]interface{}{key: mode})
	return s
}

// storeExempting builds a store whose allow-list contains kind.
func storeExempting(kind string) *tunables.Store {
	s := tunables.New()
	s.Apply(map[string]interface{}{exemptNodesKey: " video_upscale , " + kind})
	return s
}

func payloadFor(kind string) transport.TaskPayload {
	var p transport.TaskPayload
	p.NodeKind = kind
	return p
}

func TestNodeLockdownModes_NilStoreDefaultsToReport(t *testing.T) {
	nr, cd, np, _, _, _, _, _ := modes(nil)
	if nr != "report" || cd != "report" || np != "report" {
		t.Errorf("nil store (no heartbeat yet) must default every knob to report: got %s/%s/%s", nr, cd, np)
	}
}

func TestNodeLockdownModes_UnrecognizedValueFallsBackToReport(t *testing.T) {
	s := tunables.New()
	s.Apply(map[string]interface{}{nonrootKey: "bogus"})
	nr, _, _, _, _, _, _, _ := modes(s)
	if nr != "report" {
		t.Errorf("an unrecognized mode must fall back to report: got %q", nr)
	}
}

func TestNodeLockdownModes_CaseInsensitive(t *testing.T) {
	s := storeWith(capDropAllKey, "ENFORCE")
	_, cd, _, _, _, _, _, _ := modes(s)
	if cd != "enforce" {
		t.Errorf("ENFORCE should resolve to enforce: got %q", cd)
	}
}

// --- non-root flag ---------------------------------------------------------

func TestNodeLockdown_Nonroot_OffLeavesUserUntouched(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{CapDrop: []string{"ALL"}}
	plan := applyNodeLockdown(cfg, hc, storeWith(nonrootKey, "off"), payloadFor("video_upscale"))
	if plan.Nonroot.Needed || plan.Nonroot.Applied || cfg.User != "" {
		t.Errorf("off must change nothing (Needed/Applied false, User untouched): user=%q needed=%v applied=%v",
			cfg.User, plan.Nonroot.Needed, plan.Nonroot.Applied)
	}
}

func TestNodeLockdown_Nonroot_ReportLogsWouldApplyButChangesNothing(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{CapDrop: []string{"ALL"}}
	plan := applyNodeLockdown(cfg, hc, storeWith(nonrootKey, "report"), payloadFor("video_upscale"))
	if !plan.Nonroot.Needed || plan.Nonroot.Applied || cfg.User != "" {
		t.Errorf("report must say would-apply WITHOUT writing: user=%q needed=%v applied=%v",
			cfg.User, plan.Nonroot.Needed, plan.Nonroot.Applied)
	}
}

func TestNodeLockdown_Nonroot_EnforceWritesUID(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{CapDrop: []string{"ALL"}}
	plan := applyNodeLockdown(cfg, hc, storeWith(nonrootKey, "enforce"), payloadFor("video_upscale"))
	if !plan.Nonroot.Needed || !plan.Nonroot.Applied || cfg.User != nodeLockdownUID {
		t.Errorf("enforce must write User=65534: user=%q applied=%v", cfg.User, plan.Nonroot.Applied)
	}
}

func TestNodeLockdown_Nonroot_EnforceLeavesAnAlreadyNamedUserAlone(t *testing.T) {
	cfg := &container.Config{Image: "node", User: "1001:1001"}
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(cfg, hc, storeWith(nonrootKey, "enforce"), payloadFor("video_upscale"))
	if plan.Nonroot.Needed || plan.Nonroot.Applied || cfg.User != "1001:1001" {
		t.Errorf("a container that already names a user is not a candidate: user=%q needed=%v",
			cfg.User, plan.Nonroot.Needed)
	}
}

func TestNodeLockdown_Nonroot_EnforceTreatsExplicitRootAsNeeded(t *testing.T) {
	for _, u := range []string{"0", "root", "ROOT"} {
		cfg := &container.Config{Image: "node", User: u}
		hc := &container.HostConfig{}
		plan := applyNodeLockdown(cfg, hc, storeWith(nonrootKey, "enforce"), payloadFor("video_upscale"))
		if !plan.Nonroot.Applied || cfg.User != nodeLockdownUID {
			t.Errorf("explicit root user %q must be dropped to the lockdown uid: user=%q applied=%v",
				u, cfg.User, plan.Nonroot.Applied)
		}
	}
}

// --- cap-drop ALL flag -----------------------------------------------------

func TestNodeLockdown_CapDrop_OffLeavesCapDropAlone(t *testing.T) {
	cfg := &container.Config{}
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(cfg, hc, storeWith(capDropAllKey, "off"), payloadFor("video_upscale"))
	if plan.CapDrop.Applied || len(hc.CapDrop) != 0 {
		t.Errorf("off must not write CapDrop: capdrop=%v", hc.CapDrop)
	}
}

func TestNodeLockdown_CapDrop_ReportMarksNeededOnAnUncappedContainer(t *testing.T) {
	cfg := &container.Config{}
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(cfg, hc, storeWith(capDropAllKey, "report"), payloadFor("video_upscale"))
	if !plan.CapDrop.Needed || plan.CapDrop.Applied || len(hc.CapDrop) != 0 {
		t.Errorf("report on an uncapped container must mark Needed without writing: needed=%v applied=%v",
			plan.CapDrop.Needed, plan.CapDrop.Applied)
	}
}

func TestNodeLockdown_CapDrop_EnforceWritesAll(t *testing.T) {
	cfg := &container.Config{}
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(cfg, hc, storeWith(capDropAllKey, "enforce"), payloadFor("video_upscale"))
	if !plan.CapDrop.Applied || !hasCapDropAll(hc) {
		t.Errorf("enforce must write CapDrop=ALL: capdrop=%v applied=%v", hc.CapDrop, plan.CapDrop.Applied)
	}
}

func TestNodeLockdown_CapDrop_EnforceIsNoopWhenPostureAlreadyPresent(t *testing.T) {
	cfg := &container.Config{}
	hc := &container.HostConfig{CapDrop: []string{"ALL"}}
	plan := applyNodeLockdown(cfg, hc, storeWith(capDropAllKey, "enforce"), payloadFor("video_upscale"))
	if plan.CapDrop.Needed || plan.CapDrop.Applied || len(hc.CapDrop) != 1 {
		t.Errorf("an already cap-dropped container must not be touched or reported as changing: needed=%v n=%d",
			plan.CapDrop.Needed, len(hc.CapDrop))
	}
}

// --- no-new-privileges flag ------------------------------------------------

func TestNodeLockdown_NoPrivs_EnforceWritesOpt(t *testing.T) {
	cfg := &container.Config{}
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(cfg, hc, storeWith(noNewPrivsKey, "enforce"), payloadFor("video_upscale"))
	if !plan.NoPrivs.Applied || !hasNoNewPrivs(hc) {
		t.Errorf("enforce must append no-new-privileges: applied=%v", plan.NoPrivs.Applied)
	}
}

func TestNodeLockdown_NoPrivs_EnforceNoopWhenPerCapFormPresent(t *testing.T) {
	cfg := &container.Config{}
	hc := &container.HostConfig{SecurityOpt: []string{"no-new-privileges:true"}}
	plan := applyNodeLockdown(cfg, hc, storeWith(noNewPrivsKey, "enforce"), payloadFor("video_upscale"))
	if plan.NoPrivs.Needed || plan.NoPrivs.Applied || len(hc.SecurityOpt) != 1 {
		t.Errorf("the :true per-cap form already satisfies the flag: needed=%v n=%d",
			plan.NoPrivs.Needed, len(hc.SecurityOpt))
	}
}

func TestNodeLockdown_NoPrivs_ReportMarksNeeded(t *testing.T) {
	cfg := &container.Config{}
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(cfg, hc, storeWith(noNewPrivsKey, "report"), payloadFor("video_upscale"))
	if !plan.NoPrivs.Needed || plan.NoPrivs.Applied || len(hc.SecurityOpt) != 0 {
		t.Errorf("report must mark Needed without writing: needed=%v applied=%v",
			plan.NoPrivs.Needed, plan.NoPrivs.Applied)
	}
}

// --- exemptions (allow-list) ----------------------------------------------

func TestNodeLockdown_Exempt_OffModeStillReportsNothing(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(cfg, hc, storeExempting("hardware_benchmark"),
		payloadFor("hardware_benchmark"))
	if !plan.Exempt || plan.Nonroot.Applied || plan.CapDrop.Applied || plan.NoPrivs.Applied {
		t.Errorf("an allow-listed node must be exempt and untouched: exempt=%v reason=%q",
			plan.Exempt, plan.ExemptReason)
	}
	if plan.ExemptReason != "exempt_nodes_setting" {
		t.Errorf("exempt reason must be recorded: got %q", plan.ExemptReason)
	}
}

func TestNodeLockdown_Exempt_EnforceModeStillChangesNothing(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		nonrootKey: "enforce", capDropAllKey: "enforce", noNewPrivsKey: "enforce",
		exemptNodesKey: "hardware_benchmark",
	})
	plan := applyNodeLockdown(cfg, hc, s, payloadFor("HARDWARE_BENCHMARK"))
	if !plan.Exempt || cfg.User != "" || len(hc.CapDrop) != 0 || len(hc.SecurityOpt) != 0 {
		t.Errorf("enforce must NOT lock down an exempt node (case-insensitive kind match): user=%q", cfg.User)
	}
}

func TestNodeLockdown_Exempt_NonListedNodeIsNotExempt(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		nonrootKey: "enforce", exemptNodesKey: "hardware_benchmark, docker_builder",
	})
	plan := applyNodeLockdown(cfg, hc, s, payloadFor("video_upscale"))
	if plan.Exempt || !plan.Nonroot.Applied {
		t.Errorf("a node not on the allow-list must be locked down: exempt=%v applied=%v",
			plan.Exempt, plan.Nonroot.Applied)
	}
}

func TestNodeLockdown_Exempt_EmptyNodeKindNeverMatchesList(t *testing.T) {
	plan := applyNodeLockdown(&container.Config{}, &container.HostConfig{},
		storeExempting("video_upscale"), payloadFor("  "))
	if plan.Exempt {
		t.Errorf("a blank node kind must not match the allow-list")
	}
}

// --- the report line itself -----------------------------------------------

func TestNodeLockdownPlanFields_CarriesEveryKnob(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{CapDrop: []string{"ALL"},
		SecurityOpt: []string{"no-new-privileges"}}
	plan := applyNodeLockdown(cfg, hc, tunables.New(), payloadFor("video_upscale"))
	flat := planFields(payloadFor("video_upscale"), plan)
	if len(flat)%2 != 0 {
		t.Errorf("report fields must be key/value pairs: len=%d", len(flat))
		return
	}
	want := []string{"node_kind", "exempt", "exempt_reason", "nonroot_mode",
		"nonroot_missing", "nonroot_would_apply", "nonroot_applied", "capdrop_mode",
		"capdrop_missing", "capdrop_would_apply", "capdrop_applied", "noprivs_mode",
		"noprivs_missing", "noprivs_would_apply", "noprivs_applied", "lockdown_uid"}
	seen := map[string]bool{}
	for i := 0; i < len(flat); i += 2 {
		k, ok := flat[i].(string)
		if !ok {
			t.Errorf("report key at %d is not a string", i)
			continue
		}
		seen[k] = true
	}
	for _, w := range want {
		if seen[w] != true {
			t.Errorf("report line missing key %s", w)
		}
	}
	// Empty store = every knob "report". On the executor's shipped posture
	// (cap-drop ALL + no-new-privileges, root user) only non-root is a
	// would-change, and NOTHING is applied.
	if !plan.Nonroot.Needed || plan.CapDrop.Needed || plan.NoPrivs.Needed {
		t.Errorf("only non-root should be needed on the current posture: %v/%v/%v",
			plan.Nonroot.Needed, plan.CapDrop.Needed, plan.NoPrivs.Needed)
	}
	if plan.Nonroot.Applied || plan.CapDrop.Applied || plan.NoPrivs.Applied {
		t.Errorf("report mode must apply nothing")
	}
}
