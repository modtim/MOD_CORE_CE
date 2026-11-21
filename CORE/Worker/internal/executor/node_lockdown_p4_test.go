// GSEC FX.115 p4 — the RUNTIME knob (hosted gVisor / runsc). Pinned here:
//
//   - the knob's own mode vocabulary (off|report|enforce) and its default
//     "off" — unlike p1's "report", because switching the runtime of every
//     node container is a decision the owner takes knowingly, and an
//     untouched fleet must not even pay for a daemon probe;
//   - report-first: report records what enforce WOULD change and writes
//     HostConfig.Runtime nothing;
//   - fail-soft: runsc ABSENT (the daemon says so, or the probe cannot tell)
//     is a fallback to the daemon's default runtime plus the note
//     "runsc_unavailable" — NEVER a failed task and never a refused plan;
//   - the GPU rule: a container that REQUESTS GPU hardware (NVIDIA
//     DeviceRequests capabilities [[gpu]] OR raw --device mounts /dev/dxg,
//     /dev/dri, /dev/kfd, ...) is never switched unless the GPU sub-flag is
//     explicitly on, and either way the report says "gpu_skipped";
//   - a payload that already chose runsc (sandbox.go SelectRuntime) is
//     already hardened: Missing=false, nothing re-written;
//   - the p3 exception list relaxes it like every other flag: `kind:gvisor`
//     keeps that node on runc, names the flag relaxed, and leaves the other
//     knobs enforcing;
//   - the verdict rides BOTH report surfaces (planMap + planFields).
//
// No live docker daemon: runscProbe is this repo's stubbable-var convention,
// so the fake Info() answer is the whole input (the task forbids running
// containers or restarting docker here anyway).
package executor

import (
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// resetRunscCache forgets the memoized daemon answer, so one test's fake
// probe cannot leak into the next.
func resetRunscCache() {
	runscCacheMu.Lock()
	runscCacheValid, runscCacheAt = false, time.Time{}
	runscCachePresent, runscCacheNames = false, []string{}
	runscCacheMu.Unlock()
}

// probeFake makes every daemon probe answer as if the daemon reported
// `runtimes` (empty list = the probe itself failed, which is also "absent").
func probeFake(runtimes ...string) {
	names := append([]string{}, runtimes...)
	has := false
	for _, n := range names {
		if n == runscRuntimeName {
			has = true
		}
	}
	runscProbe = func(*client.Client) (bool, []string) { return has, names }
	resetRunscCache()
}

// store builds a tunables store from raw entries.
func storeWith2(kv ...string) *tunables.Store {
	s := tunables.New()
	m := map[string]interface{}{}
	for i := 0; i+1 < len(kv); i += 2 {
		m[kv[i]] = kv[i+1]
	}
	s.Apply(m)
	return s
}

// --- mode resolution -------------------------------------------------------

func TestNodeLockdownP4_ModeDefaultsToOff(t *testing.T) {
	// An untouched fleet (nil store, no heartbeat yet) is OFF, not report:
	// nothing probes the daemon and nothing switches.
	if gvisorMode(nil) != "off" {
		t.Errorf("nil store must default the runtime knob to off")
	}
	if gvisorGPUAllowed(nil) {
		t.Errorf("nil store must not allow runsc on GPU nodes")
	}
	s := storeWith2()
	if gvisorMode(s) != "off" || gvisorGPUAllowed(s) {
		t.Errorf("empty store must default off/false")
	}
}

func TestNodeLockdownP4_ModeVocabularyAndFallback(t *testing.T) {
	s := storeWith2(gvisorKey, "ENFORCE")
	if gvisorMode(s) != "enforce" {
		t.Errorf("mode must case-fold like p1's knobs: got %q", gvisorMode(s))
	}
	bad := storeWith2(gvisorKey, "always")
	if gvisorMode(bad) != "off" {
		t.Errorf("an unrecognized value must fall back to off (the safe default): got %q",
			gvisorMode(bad))
	}
}

// --- report mode changes nothing ------------------------------------------

func TestNodeLockdownP4_ReportRecordsButDoesNotSwitch(t *testing.T) {
	probeFake("runc", runscRuntimeName)
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "report"), payloadFor("video_upscale"))
	if !plan.Gvisor.Needed || plan.Gvisor.Applied || hc.Runtime != "" {
		t.Errorf("report must say runsc WOULD be used but write nothing: needed=%v applied=%v runtime=%q",
			plan.Gvisor.Needed, plan.Gvisor.Applied, hc.Runtime)
	}
	if plan.GvisorNote != "" {
		t.Errorf("a host that HAS runsc must carry no fail-soft note: got %q", plan.GvisorNote)
	}
	if len(plan.GvisorRuntimes) != 2 {
		t.Errorf("the report must echo the daemon's runtime list: got %v", plan.GvisorRuntimes)
	}
}

// --- enforce switches ONLY when the daemon has runsc ----------------------

func TestNodeLockdownP4_EnforceSwitchesWhenRunscPresent(t *testing.T) {
	probeFake("runc", runscRuntimeName)
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce"), payloadFor("video_upscale"))
	if hc.Runtime != runscRuntimeName || !plan.Gvisor.Applied || !plan.Gvisor.Needed {
		t.Errorf("enforce on a runsc-registered host must set HostConfig.Runtime: runtime=%q applied=%v",
			hc.Runtime, plan.Gvisor.Applied)
	}
	if plan.GvisorNote != "" {
		t.Errorf("no fail-soft note when the switch happened: got %q", plan.GvisorNote)
	}
}

func TestNodeLockdownP4_EnforceFallsBackWhenRunscAbsent(t *testing.T) {
	// The daemon answered (runc only) and runsc is simply not registered.
	probeFake("runc")
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce"), payloadFor("video_upscale"))
	if hc.Runtime != "" || plan.Gvisor.Applied || plan.Gvisor.Needed {
		t.Errorf("no runsc must NEVER switch the runtime: runtime=%q applied=%v",
			hc.Runtime, plan.Gvisor.Applied)
	}
	if plan.GvisorNote != "runsc_unavailable" {
		t.Errorf("the report must name the fallback reason: got %q", plan.GvisorNote)
	}
	if len(plan.GvisorRuntimes) != 1 || plan.GvisorRuntimes[0] != "runc" {
		t.Errorf("the report must echo what the host actually has: got %v", plan.GvisorRuntimes)
	}
}

func TestNodeLockdownP4_EnforceSurvivesAFailedProbe(t *testing.T) {
	// A daemon that cannot answer at all (no client wired, or Info errored):
	// still no switch, still no failure — the task runs under the default.
	probeFake()
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce"), payloadFor("video_upscale"))
	if hc.Runtime != "" || plan.Gvisor.Applied {
		t.Errorf("a failed probe must not switch the runtime")
	}
	if plan.GvisorNote != "runsc_unavailable" {
		t.Errorf("an unknown capability must report runsc_unavailable: got %q", plan.GvisorNote)
	}
	if len(plan.GvisorRuntimes) != 0 {
		t.Errorf("a failed probe reports no runtime list: got %v", plan.GvisorRuntimes)
	}
}

func TestNodeLockdownP4_OffNeverProbesAndNeverSwitches(t *testing.T) {
	// gvisor off: the daemon is not even asked (no extra `docker info` per
	// container on an untouched fleet) and nothing switches.
	probeFake()
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(), payloadFor("video_upscale"))
	if hc.Runtime != "" || plan.Gvisor.Applied || plan.Gvisor.Needed {
		t.Errorf("off must change nothing")
	}
	if len(plan.GvisorRuntimes) != 0 || plan.GvisorNote != "" {
		t.Errorf("off must not probe the daemon nor note a reason: runtimes=%v note=%q",
			plan.GvisorRuntimes, plan.GvisorNote)
	}
	if plan.Gvisor.Mode != "off" {
		t.Errorf("the report still names the mode in effect: got %q", plan.Gvisor.Mode)
	}
}

// --- the GPU rule ---------------------------------------------------------

// gpuRequesting builds a HostConfig that REQUESTS GPU hardware the way the
// executor actually attaches cards: NVIDIA via DeviceRequests capabilities
// [[gpu]] (gpu_assignment.go nvidiaDeviceRequest), AMD/Intel/NPU via raw
// --device mounts (executor.go's /dev/dxg path). Named so it cannot collide
// with k8s_gpu_placement_test.go's gpuNode in this same package.
func gpuRequesting(nvidia, rocm bool) *container.HostConfig {
	hc := &container.HostConfig{}
	if nvidia {
		hc.Resources.DeviceRequests = []container.DeviceRequest{
			{Driver: "nvidia", DeviceIDs: []string{"0"},
				Capabilities: [][]string{{"gpu"}}},
		}
	}
	if rocm {
		hc.Resources.Devices = []container.DeviceMapping{
			{PathOnHost: "/dev/dxg", PathInContainer: "/dev/dxg",
				CgroupPermissions: "rw"},
		}
	}
	return hc
}

func TestNodeLockdownP4_GPUNodeIsSkippedUnderEnforce(t *testing.T) {
	probeFake("runc", runscRuntimeName) // runsc IS available: the GPU rule alone decides
	hc := gpuRequesting(true, false)
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce"), payloadFor("video_upscale"))
	if hc.Runtime != "" || plan.Gvisor.Applied || plan.Gvisor.Needed {
		t.Errorf("a GPU-requesting container must stay on the default runtime: runtime=%q", hc.Runtime)
	}
	if plan.GvisorNote != "gpu_skipped" {
		t.Errorf("the skip must be visible: got %q", plan.GvisorNote)
	}
}

func TestNodeLockdownP4_RawDeviceGPUNodeIsSkippedToo(t *testing.T) {
	probeFake("runc", runscRuntimeName)
	hc := gpuRequesting(false, true) // /dev/dxg: the WSL2 ROCm/Intel path, no DeviceRequest
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce"), payloadFor("hardware_benchmark"))
	if hc.Runtime != "" || plan.Gvisor.Applied || !plan.Gvisor.Missing {
		t.Errorf("raw --device GPU mounts must be caught by the same rule: runtime=%q missing=%v",
			hc.Runtime, plan.Gvisor.Missing)
	}
	if plan.GvisorNote != "gpu_skipped" {
		t.Errorf("raw-device GPU nodes report gpu_skipped too: got %q", plan.GvisorNote)
	}
}

func TestNodeLockdownP4_NongpuDeviceIsNotAGPU(t *testing.T) {
	// A tailscale sidecar's /dev/net/tun is NOT a GPU: the rule keys on the
	// GPU device paths only, so a node carrying it can still move to runsc.
	probeFake("runc", runscRuntimeName)
	hc := &container.HostConfig{}
	hc.Resources.Devices = []container.DeviceMapping{
		{PathInContainer: "/dev/net/tun", PathOnHost: "/dev/net/tun"},
	}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce"), payloadFor("video_upscale"))
	if hc.Runtime != runscRuntimeName || !plan.Gvisor.Applied {
		t.Errorf("a non-GPU device must not block the switch: runtime=%q", hc.Runtime)
	}
	if plan.GvisorNote != "" {
		t.Errorf("no GPU skip note for /dev/net/tun: got %q", plan.GvisorNote)
	}
}

func TestNodeLockdownP4_GPUSubFlagAllowsTheSwitch(t *testing.T) {
	// The owner explicitly said runsc is fine for this GPU node's path.
	probeFake("runc", runscRuntimeName)
	hc := gpuRequesting(true, false)
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce", gvisorGPUKey, "true"),
		payloadFor("video_upscale"))
	if hc.Runtime != runscRuntimeName || !plan.Gvisor.Applied {
		t.Errorf("with the GPU sub-flag on, enforce must switch: runtime=%q", hc.Runtime)
	}
	if plan.GvisorNote != "" {
		t.Errorf("an allowed GPU switch carries no note: got %q", plan.GvisorNote)
	}
}

// --- already on runsc is already hardened --------------------------------

func TestNodeLockdownP4_PayloadThatAlreadyChoseRunscIsNotTouched(t *testing.T) {
	// sandbox.go SelectRuntime (payload.UseSandboxRuntime) may already have set
	// HostConfig.Runtime before this layer runs. This layer never re-writes or
	// removes a runtime, so Missing=false and Needed stays false.
	probeFake("runc", runscRuntimeName)
	hc := &container.HostConfig{Runtime: runscRuntimeName}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce"), payloadFor("video_upscale"))
	if plan.Gvisor.Missing || plan.Gvisor.Needed || plan.Gvisor.Applied {
		t.Errorf("an already-runsc container is already hardened: missing=%v needed=%v",
			plan.Gvisor.Missing, plan.Gvisor.Needed)
	}
	if hc.Runtime != runscRuntimeName {
		t.Errorf("the existing runtime must be left alone: got %q", hc.Runtime)
	}
	if plan.GvisorNote != "" {
		t.Errorf("already-hardened carries no note: got %q", plan.GvisorNote)
	}
}

// --- the p3 exception list relaxes the runtime knob ----------------------

func TestNodeLockdownP4_GvisorExceptionKeepsNodeOnRunc(t *testing.T) {
	probeFake("runc", runscRuntimeName)
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{Image: "node"}, hc,
		storeWith2(gvisorKey, "enforce", capDropAllKey, "enforce",
			exemptListKey, "hardware_benchmark:gvisor"),
		payloadFor("hardware_benchmark"))
	if hc.Runtime != "" || plan.Gvisor.Applied || plan.Gvisor.Needed {
		t.Errorf("a kind:gvisor exception must keep the node on the default runtime: runtime=%q", hc.Runtime)
	}
	if !plan.Gvisor.Missing {
		t.Errorf("the posture IS missing (that is what the owner wants to see): missing=%v",
			plan.Gvisor.Missing)
	}
	// The relaxation is named so the API can quote the exception's reason...
	found := false
	for _, f := range plan.RelaxedFlags {
		if f == "gvisor" {
			found = true
		}
	}
	if !found {
		t.Errorf("relaxed_flags must name gvisor: got %v", plan.RelaxedFlags)
	}
	// ...and the OTHER knobs still enforce for the same node.
	if hc.CapDrop == nil || len(hc.CapDrop) == 0 {
		t.Errorf("cap-drop ALL must still enforce for an exception that only relaxes gvisor")
	}
}

// --- the verdict rides BOTH report surfaces ------------------------------

func TestNodeLockdownP4PlanMap_CarriesRuntimeVerdict(t *testing.T) {
	probeFake("runc", runscRuntimeName)
	plan := applyNodeLockdown(&container.Config{Image: "node"},
		&container.HostConfig{}, storeWith2(gvisorKey, "enforce"),
		payloadFor("video_upscale"))
	m := planMap(plan)
	for _, k := range []string{"gvisor_mode", "gvisor_missing", "gvisor_would_apply",
		"gvisor_applied", "gvisor_runtime", "gvisor_note", "gvisor_runtimes"} {
		if _, ok := m[k]; !ok {
			t.Errorf("planMap must carry %s", k)
		}
	}
	if m["gvisor_runtime"] != runscRuntimeName {
		t.Errorf("the report must name the runtime the knob targets: got %v", m["gvisor_runtime"])
	}
	// planFields (the log line) carries the same keys.
	flat := planFields(payloadFor("video_upscale"), plan)
	seen := map[string]bool{}
	for i := 0; i+1 < len(flat); i += 2 {
		k, ok := flat[i].(string)
		if ok {
			seen[k] = true
		}
	}
	if !seen["gvisor_mode"] || !seen["gvisor_note"] || !seen["gvisor_runtimes"] {
		t.Errorf("the log line must carry the runtime verdict too")
	}
	if len(flat)%2 != 0 {
		t.Errorf("report fields must be key/value pairs: len=%d", len(flat))
	}
}
