// GSEC FX.115 p3 — the per-node exception table's delivery + per-flag
// relaxation. The API derives the exemptListKey ("kind" bare = whole node,
// "kind:flag" = ONE flag relaxed) off the live, un-expired
// models.NodeLockdownException rows UNION the legacy worker.node_lockdown_exempt_nodes
// allow-list, so this side only parses that spelling. Pinned here:
//
//   - the legacy allow-list still exempts the WHOLE node with its original
//     reason spelling (the union fallback for one release — nothing breaks
//     when the new list lands);
//   - a bare entry on the NEW key exempts the whole node too, reason
//     "exempt_list_setting";
//   - a `kind:flag` entry relaxes ONLY that flag: the flag's Missing verdict
//     stays (the posture IS missing) but Needed/Applied are false and nothing
//     is written, while the OTHER flags still enforce;
//   - expiry is NOT this side's job (an expired row is simply absent from the
//     derived list), so no date is parsed here;
//   - the relaxed flag names ride the report (planMap/planFields) so the API
//     can quote the exception's recorded reason.
package executor

import (
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// storeEntries builds a store whose derived p3 list is `entries` (the exact
// comma-separated spelling the API pushes).
func storeEntries(entries string) *tunables.Store {
	s := tunables.New()
	s.Apply(map[string]interface{}{exemptListKey: entries})
	return s
}

// --- parsing the derived list ---------------------------------------------

func TestNodeLockdownP3_Entries_SplitBareAndQualified(t *testing.T) {
	whole, per := exemptEntries(storeEntries(" video_upscale , hardware_benchmark:capdrop , hardware_benchmark:nonroot "))
	if !whole["video_upscale"] {
		t.Errorf("bare entry must register the whole node: got %v", whole)
	}
	if whole["hardware_benchmark"] {
		t.Errorf("a qualified entry must NOT exempt the whole node")
	}
	if m, ok := per["hardware_benchmark"]; !ok || !(m["capdrop"] && m["nonroot"]) {
		t.Errorf("qualified entries must register both flags for the kind: got %v", per)
	}
	// Case-insensitive on the kind.
	whole2, per2 := exemptEntries(storeEntries("HARDWARE_BENCHMARK:CapDrop"))
	if whole2["hardware_benchmark"] {
		t.Errorf("qualified entry must not exempt the whole node (case-folded)")
	}
	if m, ok := per2["hardware_benchmark"]; !ok || !m["capdrop"] {
		t.Errorf("kind/flag case must fold: got %v", per2)
	}
	// A nil store means nothing is exempt.
	w3, p3 := exemptEntries(nil)
	if len(w3) != 0 || len(p3) != 0 {
		t.Errorf("nil store must yield no exemptions: got %v/%v", w3, p3)
	}
	// An entry with a dangling colon is whole-node, not a blank flag.
	w4, p4 := exemptEntries(storeEntries("image_blur:"))
	if !w4["image_blur"] || len(p4) != 0 {
		t.Errorf("dangling colon = whole node: got %v/%v", w4, p4)
	}
	_ = p3
	_ = p4
}

// --- whole-node exemption via the NEW key ---------------------------------

func TestNodeLockdownP3_BareEntryExemptsWholeNode(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		nonrootKey: "enforce", capDropAllKey: "enforce", noNewPrivsKey: "enforce",
		limitsMemoryKey: "enforce", limitsMemoryCapKey: 4096,
		limitsPidsKey: "enforce", limitsPidsCapKey: 128,
		exemptListKey: "hardware_benchmark",
	})
	plan := applyNodeLockdown(cfg, hc, s, payloadFor("hardware_benchmark"))
	if !plan.Exempt || plan.ExemptReason != "exempt_list_setting" {
		t.Errorf("a bare entry on the new key must exempt the whole node with the new reason: exempt=%v reason=%q",
			plan.Exempt, plan.ExemptReason)
	}
	if cfg.User != "" || len(hc.CapDrop) != 0 || len(hc.SecurityOpt) != 0 ||
		hc.Resources.Memory != 0 || hc.Resources.NanoCPUs != 0 ||
		(hc.Resources.PidsLimit != nil && *hc.Resources.PidsLimit != 0) {
		t.Errorf("an exempt node must be untouched: user=%q caps=%d mem=%d",
			cfg.User, len(hc.CapDrop), hc.Resources.Memory)
	}
}

func TestNodeLockdownP3_LegacySettingStillExemptsAndWinsReason(t *testing.T) {
	// The union fallback: an older API that only sends the legacy allow-list
	// keeps working, and its reason spelling stays stable.
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		nonrootKey: "enforce", exemptNodesKey: " video_upscale ",
	})
	plan := applyNodeLockdown(cfg, hc, s, payloadFor("VIDEO_UPSCALE"))
	if !plan.Exempt || plan.ExemptReason != "exempt_nodes_setting" {
		t.Errorf("legacy allow-list must still exempt with its own reason: exempt=%v reason=%q",
			plan.Exempt, plan.ExemptReason)
	}
	if cfg.User != "" {
		t.Errorf("legacy exempt node must be untouched: user=%q", cfg.User)
	}
}

// --- per-flag relaxation ---------------------------------------------------

func TestNodeLockdownP3_QualifiedEntryRelaxesOnlyThatFlag(t *testing.T) {
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		nonrootKey: "enforce", capDropAllKey: "enforce", noNewPrivsKey: "enforce",
		limitsMemoryKey: "enforce", limitsMemoryCapKey: 4096,
		exemptListKey: "hardware_benchmark:capdrop",
	})
	plan := applyNodeLockdown(cfg, hc, s, payloadFor("hardware_benchmark"))
	if plan.Exempt {
		t.Errorf("a per-flag exception must NOT exempt the whole node")
	}
	// The relaxed flag: posture still missing, nothing needed/written.
	if !plan.CapDrop.Missing || plan.CapDrop.Needed || plan.CapDrop.Applied ||
		plan.CapDrop.Mode != "off" {
		t.Errorf("capdrop must stay Missing but relaxed (mode off, nothing written): missing=%v needed=%v applied=%v mode=%q",
			plan.CapDrop.Missing, plan.CapDrop.Needed, plan.CapDrop.Applied, plan.CapDrop.Mode)
	}
	if len(hc.CapDrop) != 0 {
		t.Errorf("the relaxed flag must write nothing: capdrop=%d", len(hc.CapDrop))
	}
	// The untouched flags still enforce.
	if !plan.Nonroot.Applied || cfg.User != "65534" {
		t.Errorf("nonroot must still enforce on a per-flag exception: applied=%v user=%q",
			plan.Nonroot.Applied, cfg.User)
	}
	if !plan.NoPrivs.Applied || len(hc.SecurityOpt) != 1 {
		t.Errorf("noprivs must still enforce: applied=%v n=%d", plan.NoPrivs.Applied, len(hc.SecurityOpt))
	}
	if !plan.Memory.Applied || hc.Resources.Memory != 4096*int64(1024*1024) {
		t.Errorf("memory floor must still enforce: applied=%v mem=%d",
			plan.Memory.Applied, hc.Resources.Memory)
	}
	// The report names the relaxed flag so the API can quote its reason.
	if len(plan.RelaxedFlags) != 1 || plan.RelaxedFlags[0] != "capdrop" {
		t.Errorf("relaxed_flags must name exactly the relaxed flag: got %v", plan.RelaxedFlags)
	}
}

func TestNodeLockdownP3_UnknownFlagEntryRelaxesNothing(t *testing.T) {
	// A flag name the layer does not report is inert (never a whole-node
	// exemption, never a typo that silently turns a real knob off).
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		nonrootKey: "enforce", exemptListKey: "hardware_benchmark:not_a_flag",
	})
	plan := applyNodeLockdown(cfg, hc, s, payloadFor("hardware_benchmark"))
	if plan.Exempt || !plan.Nonroot.Applied || cfg.User != "65534" {
		t.Errorf("an unknown flag must change nothing: exempt=%v applied=%v user=%q",
			plan.Exempt, plan.Nonroot.Applied, cfg.User)
	}
	if len(plan.RelaxedFlags) != 0 {
		t.Errorf("an unknown flag must not be reported as relaxed: got %v", plan.RelaxedFlags)
	}
}

func TestNodeLockdownP3_ScratchTmpfsRelaxationFollowsRootfs(t *testing.T) {
	// scratch_tmpfs is not a mode knob, so relaxing it names the scratch set
	// the rootfs knob would add; the rootfs flag itself is unaffected.
	_, per := exemptEntries(storeEntries("video_upscale:scratch_tmpfs"))
	if m, ok := per["video_upscale"]; !ok || !m["scratch_tmpfs"] {
		t.Errorf("scratch_tmpfs must be a nameable exception: got %v", per)
	}
	cfg := &container.Config{Image: "node"}
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		nonrootKey: "enforce", rootfsKey: "enforce",
		exemptListKey: "video_upscale:scratch_tmpfs",
	})
	plan := applyNodeLockdown(cfg, hc, s, payloadFor("video_upscale"))
	if !plan.Rootfs.Applied || !hc.ReadonlyRootfs {
		t.Errorf("rootfs must still enforce when only scratch_tmpfs is relaxed")
	}
	if len(plan.RelaxedFlags) != 1 || plan.RelaxedFlags[0] != "scratch_tmpfs" {
		t.Errorf("relaxed_flags must name scratch_tmpfs: got %v", plan.RelaxedFlags)
	}
}

// --- the report carries the relaxation ------------------------------------

func TestNodeLockdownP3PlanMap_CarriesRelaxedFlags(t *testing.T) {
	plan := applyNodeLockdown(&container.Config{}, &container.HostConfig{},
		storeEntries("video_upscale:capdrop,video_upscale:limits_cpu"),
		payloadFor("video_upscale"))
	m := planMap(plan)
	v, ok := m["relaxed_flags"]
	if !ok {
		t.Errorf("planMap must carry relaxed_flags")
		return
	}
	list, ok := v.([]string)
	if !ok || len(list) != 2 || list[0] != "capdrop" || list[1] != "limits_cpu" {
		t.Errorf("relaxed_flags must list the relaxed flags in report order: got %v", v)
	}
	if plan.Exempt {
		t.Errorf("a per-flag exception must not exempt the whole node")
	}
	// planFields (the log line) carries the same key.
	flat := planFields(payloadFor("video_upscale"), plan)
	seen := map[string]bool{}
	for i := 0; i+1 < len(flat); i += 2 {
		k, ok := flat[i].(string)
		if ok {
			seen[k] = true
		}
	}
	if !seen["relaxed_flags"] {
		t.Errorf("the log line must carry relaxed_flags too")
	}
}
