// GSEC FX.115 p1b — the second half of the node lockdown layer: seccomp
// profile (Docker default, or a named MOD profile), read-only rootfs +
// writable scratch tmpfs, and the resource-limit floors (pids / memory /
// cpu). Each mode knob is its OWN platform setting taking off|report|enforce
// with default "report", exactly p1a's contract, and the verdicts land in the
// SAME LockdownPlan / report line p1a built.
//
// The invariants that matter:
//   - report changes nothing, enforce writes, off adds nothing;
//   - a posture the executor already ships (the embedded seccomp profile,
//     the hardened sandbox's readonly rootfs + tmpfs, the payload's own
//     memory/cpu/pids caps) is NEVER rewritten and never stepped DOWN;
//   - a limit floor is only written where the container is UNLIMITED, so
//     this layer can never go BELOW a cap the node/executor already chose;
//   - a scratch path that is a bind-mount destination is never turned into
//     tmpfs (tmpfs would shadow the bind and hide the tenant's I/O data);
//   - GPU nodes keep their device access: this layer never touches
//     Resources.Devices/DeviceRequests.
package executor

import (
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
	"strings"
)

// p1bStoreKnobs builds a store setting one key to one value.
func p1bStoreKnobs(key string, val interface{}) *tunables.Store {
	s := tunables.New()
	s.Apply(map[string]interface{}{key: val})
	return s
}

// p1bStoreSeccomp builds a store for the seccomp knob + its profile name.
func p1bStoreSeccomp(mode, profile string) *tunables.Store {
	s := tunables.New()
	s.Apply(map[string]interface{}{seccompKey: mode, seccompProfileKey: profile})
	return s
}

// p1bStoreRootfs builds a store for the read-only-rootfs knob.
func p1bStoreRootfs(mode string) *tunables.Store {
	return p1bStoreKnobs(rootfsKey, mode)
}

// p1bStoreLimit builds a store for one limit knob + its cap.
func p1bStoreLimit(modeKey, capKey, mode string, cap int64) *tunables.Store {
	s := tunables.New()
	s.Apply(map[string]interface{}{modeKey: mode, capKey: cap})
	return s
}

func p1bPayloadFor(kind string) transport.TaskPayload {
	var p transport.TaskPayload
	p.NodeKind = kind
	return p
}

// p1bTmpfsHas reports whether a tmpfs mount exists at path (a map lookup on a
// nil map is fine in Go, so no nil guard is needed).
// p1bPidsGot reads the container's pids cap for an error message (nil = unset).
func p1bPidsGot(hc *container.HostConfig) int64 {
	if hc.Resources.PidsLimit == nil {
		return 0
	}
	return *hc.Resources.PidsLimit
}

func p1bTmpfsHas(hc *container.HostConfig, path string) bool {
	return hc != nil && hc.Tmpfs != nil && hc.Tmpfs[path] != ""
}

// --- mode + cap resolution -------------------------------------------------

func TestNodeLockdownP1bModes_NilStoreDefaultsToReport(t *testing.T) {
	_, _, _, sc, rf, pd, mm, cc := modes(nil)
	if sc != "report" || rf != "report" || pd != "report" || mm != "report" || cc != "report" {
		t.Errorf("nil store must default the p1b knobs to report too: %s/%s/%s/%s/%s",
			"want report", sc, rf, pd, mm)
	}
}

func TestNodeLockdownP1bModes_UnrecognizedValueFallsBackToReport(t *testing.T) {
	s := tunables.New()
	s.Apply(map[string]interface{}{rootfsKey: "yes-please", limitsCpuKey: "on"})
	_, _, _, _, rf, _, _, cc := modes(s)
	if rf != "report" || cc != "report" {
		t.Errorf("unrecognized p1b modes must fall back to report: got %q/%q", rf, cc)
	}
}

func TestNodeLockdownP1bModes_CaseInsensitiveEnforce(t *testing.T) {
	s := tunables.New()
	s.Apply(map[string]interface{}{limitsPidsKey: "ENFORCE", rootfsKey: "Off"})
	_, _, _, _, rf, pd, _, _ := modes(s)
	if pd != "enforce" || rf != "off" {
		t.Errorf("ENFORCE/off must resolve case-insensitively: got %q/%q", pd, rf)
	}
}

func TestNodeLockdownP1bCaps_NilStoreRequestsNoFloor(t *testing.T) {
	p, m, c := caps(nil)
	if p != 0 || m != 0 || c != 0 {
		t.Errorf("no heartbeat = no requested floor: got %d/%d/%d", p, m, c)
	}
}

func TestNodeLockdownP1bCaps_NegativeAndStringValues(t *testing.T) {
	s := tunables.New()
	s.Apply(map[string]interface{}{
		limitsPidsCapKey:   256,
		limitsMemoryCapKey: -1, // an uncapping request: this layer never uncaps
		limitsCpuCapKey:    "8",
	})
	p, m, c := caps(s)
	if p != 256 || m != 0 || c != 8 {
		t.Errorf("want pids=256 memory=0(no floor) cpu=8: got %d/%d/%d", p, m, c)
	}
}

func TestNodeLockdownP1bSeccompProfile_DefaultsToDocker(t *testing.T) {
	if seccompProfileName(nil) != "docker" {
		t.Errorf("nil store means Docker's default profile")
	}
	s := tunables.New()
	s.Apply(map[string]interface{}{seccompProfileKey: "   "})
	if seccompProfileName(s) != "docker" {
		t.Errorf("a blank profile name means Docker's default profile")
	}
}

func TestNodeLockdownP1bSeccompProfile_NamedProfileIsLowercased(t *testing.T) {
	s := tunables.New()
	s.Apply(map[string]interface{}{seccompProfileKey: "MOD-NODE"})
	if seccompProfileName(s) != modNodeProfileName {
		t.Errorf("MOD-NODE should resolve to the mod-node profile: got %q", seccompProfileName(s))
	}
}

// --- seccomp knob ----------------------------------------------------------

// The worker's hardened sandbox (sandbox.go) already attaches the embedded
// node profile to every node container, so on the shipped posture the knob
// CONFIRMS it — under report AND enforce — and never re-writes it.
func TestNodeLockdownP1bSeccomp_EnforceIsNoopWhenProfileAlreadyAttached(t *testing.T) {
	hc := &container.HostConfig{SecurityOpt: []string{"no-new-privileges",
		"seccomp={\"actions\":[],\"default\":\"ALLOW\"}"}}
	plan := applyNodeLockdown(&container.Config{}, hc,
		// profile name irrelevant: the container is already covered
		p1bStoreSeccomp("enforce", modNodeProfileName), p1bPayloadFor("video_upscale"))
	if plan.Seccomp.Missing || plan.Seccomp.Needed || plan.Seccomp.Applied ||
		len(hc.SecurityOpt) != 2 {
		t.Errorf("a container already carrying a seccomp= option is covered: missing=%v applied=%v n=%d",
			plan.Seccomp.Missing, plan.Seccomp.Applied, len(hc.SecurityOpt))
	}
}

func TestNodeLockdownP1bSeccomp_DockerProfileWritesNothingEvenUnderEnforce(t *testing.T) {
	// No seccomp= option = Docker's built-in default, which is exactly what
	// the "docker" profile name asks for. Writing the worker's stricter
	// profile here would be an unrequested step UP, and writing a default
	// would be a step DOWN onto a weaker filter — so: nothing.
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreSeccomp("enforce", "docker"), p1bPayloadFor("video_upscale"))
	if plan.Seccomp.Needed || plan.Seccomp.Applied || len(hc.SecurityOpt) != 0 {
		t.Errorf("profile 'docker' must not write a seccomp option: needed=%v applied=%v n=%d",
			plan.Seccomp.Needed, plan.Seccomp.Applied, len(hc.SecurityOpt))
	}
}

func TestNodeLockdownP1bSeccomp_ModNodeProfileReportMarksNeededWithoutWriting(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreSeccomp("report", modNodeProfileName), p1bPayloadFor("video_upscale"))
	if !plan.Seccomp.Needed || plan.Seccomp.Applied || len(hc.SecurityOpt) != 0 {
		t.Errorf("report must say would-apply WITHOUT writing: needed=%v applied=%v n=%d",
			plan.Seccomp.Needed, plan.Seccomp.Applied, len(hc.SecurityOpt))
	}
	// The profile name is echoed so the audit line names what enforce WOULD
	// attach.
	if plan.SeccompProfile != modNodeProfileName {
		t.Errorf("profile name must be reported: got %q", plan.SeccompProfile)
	}
}

func TestNodeLockdownP1bSeccomp_ModNodeProfileEnforceAttachesEmbeddedProfile(t *testing.T) {
	hc := &container.HostConfig{SecurityOpt: []string{"no-new-privileges"}}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreSeccomp("enforce", modNodeProfileName), p1bPayloadFor("video_upscale"))
	if !plan.Seccomp.Applied || len(hc.SecurityOpt) != 2 ||
		!strings.HasPrefix(hc.SecurityOpt[1], "seccomp=") {
		t.Errorf("enforce + mod-node must attach the embedded profile inline: applied=%v n=%d",
			plan.Seccomp.Applied, len(hc.SecurityOpt))
	}
	// The existing posture survived.
	if hc.SecurityOpt[0] != "no-new-privileges" {
		t.Errorf("the layer must not reorder or drop existing SecurityOpt entries: got %q",
			hc.SecurityOpt[0])
	}
}

func TestNodeLockdownP1bSeccomp_OffModeAddsNothing(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreSeccomp("off", modNodeProfileName), p1bPayloadFor("video_upscale"))
	if plan.Seccomp.Needed || plan.Seccomp.Applied || len(hc.SecurityOpt) != 0 {
		t.Errorf("off must add nothing: needed=%v applied=%v", plan.Seccomp.Needed,
			plan.Seccomp.Applied)
	}
}

func TestNodeLockdownP1bSeccomp_UnknownProfileNameWritesNothing(t *testing.T) {
	// Only the MOD-named profile this worker can actually render is actionable;
	// an arbitrary name (a future registry value this binary does not know)
	// reports Missing but is not Needed — the same "unknown values degrade to
	// the local default" contract the tunables store applies.
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreSeccomp("enforce", "gvisor-lite"), p1bPayloadFor("video_upscale"))
	if plan.Seccomp.Needed || plan.Seccomp.Applied || len(hc.SecurityOpt) != 0 {
		t.Errorf("an unknown profile name must not be attached: needed=%v applied=%v",
			plan.Seccomp.Needed, plan.Seccomp.Applied)
	}
}

// --- read-only rootfs + scratch tmpfs -------------------------------------

func TestNodeLockdownP1bRootfs_EnforceWritesReadonlyAndScratch(t *testing.T) {
	hc := &container.HostConfig{Binds: []string{"/srv/data:/data:rw",
		"cp-vol-1:/var/lib/mod-node:ro"}}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreRootfs("enforce"), p1bPayloadFor("video_upscale"))
	if !plan.Rootfs.Applied || !hc.ReadonlyRootfs {
		t.Errorf("enforce must set ReadonlyRootfs: applied=%v", plan.Rootfs.Applied)
	}
	// Every scratch path is added because none of them is a bind destination.
	if len(plan.ScratchTmpfs) != len(scratchPaths) {
		t.Errorf("want every scratch path added, got %v", plan.ScratchTmpfs)
	}
	for _, p := range scratchPaths {
		if hc.Tmpfs[p] != scratchTmpfsOpts[p] {
			t.Errorf("scratch tmpfs %s must carry the hardened-sandbox options: got %q",
				p, hc.Tmpfs[p])
		}
	}
	// The bind mounts survive untouched: the tenant's I/O volume stays
	// writable even though the rootfs is read-only.
	if hc.Binds[0] != "/srv/data:/data:rw" {
		t.Errorf("a bind mount must keep its own rw mode: got %q", hc.Binds[0])
	}
	// /data and the checkpoint path were NOT turned into tmpfs.
	if p1bTmpfsHas(hc, "/data") {
		t.Errorf("a bind destination must never become tmpfs: /data=%q", hc.Tmpfs["/data"])
	}
	// /dev/shm is never tmpfs: it is ShmSize, sized by the executor/ephemeral.
	if p1bTmpfsHas(hc, "/dev/shm") {
		t.Errorf("/dev/shm must stay host-backed, not tmpfs")
	}
}

func TestNodeLockdownP1bRootfs_EnforceSkipsBindDestinations(t *testing.T) {
	// A pathological node that binds INTO a scratch path: tmpfs there would
	// shadow the bind and hide the tenant's data, so that path is skipped.
	hc := &container.HostConfig{Binds: []string{"/srv/tmp:/tmp:rw", "/x:/data:rw"}}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreRootfs("enforce"), p1bPayloadFor("video_upscale"))
	if !plan.Rootfs.Applied {
		t.Errorf("rootfs must still be enforced")
	}
	if p1bTmpfsHas(hc, "/tmp") {
		t.Errorf("/tmp is a bind destination: tmpfs would shadow it, got %q", hc.Tmpfs["/tmp"])
	}
	// The remaining four scratch paths are still added.
	if len(plan.ScratchTmpfs) != len(scratchPaths)-1 {
		t.Errorf("want every scratch path except /tmp: got %v", plan.ScratchTmpfs)
	}
}

func TestNodeLockdownP1bRootfs_EnforceLeavesExistingTmpfsAlone(t *testing.T) {
	// The hardened sandbox already sized /tmp; the node's own ephemeral
	// request rewrites it AFTER this layer. This layer must not fight either
	// one by re-writing an existing entry.
	hc := &container.HostConfig{Tmpfs: map[string]string{
		"/tmp":     "rw,size=1024m,mode=1777",
		"/var/tmp": "rw,size=64m,mode=1777",
		"/run":     "rw,size=16m,mode=0755",
		"/root":    "rw,size=32m,mode=0755",
		"/home":    "rw,size=32m,mode=0755",
	}}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreRootfs("enforce"), p1bPayloadFor("video_upscale"))
	if !plan.Rootfs.Applied || !hc.ReadonlyRootfs {
		t.Errorf("rootfs must still be enforced: applied=%v", plan.Rootfs.Applied)
	}
	if len(plan.ScratchTmpfs) != 0 {
		t.Errorf("an already-tmpfs'd container needs no scratch additions: got %v",
			plan.ScratchTmpfs)
	}
	if hc.Tmpfs["/tmp"] != "rw,size=1024m,mode=1777" {
		t.Errorf("the node's own /tmp size must survive: got %q", hc.Tmpfs["/tmp"])
	}
}

func TestNodeLockdownP1bRootfs_ReportNamesScratchButChangesNothing(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreRootfs("report"), p1bPayloadFor("video_upscale"))
	if plan.Rootfs.Applied || hc.ReadonlyRootfs || hc.Tmpfs != nil {
		t.Errorf("report must not write rootfs or tmpfs: applied=%v ro=%v",
			plan.Rootfs.Applied, hc.ReadonlyRootfs)
	}
	if !plan.Rootfs.Needed || len(plan.ScratchTmpfs) != len(scratchPaths) {
		t.Errorf("report must name every scratch path it WOULD add: needed=%v got=%v",
			plan.Rootfs.Needed, plan.ScratchTmpfs)
	}
}

func TestNodeLockdownP1bRootfs_OffAddsNothingEvenScratch(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreRootfs("off"), p1bPayloadFor("video_upscale"))
	if plan.Rootfs.Needed || plan.Rootfs.Applied || hc.Tmpfs != nil ||
		plan.ScratchTmpfs != nil {
		t.Errorf("off adds nothing at all — not even scratch: needed=%v scratch=%v",
			plan.Rootfs.Needed, plan.ScratchTmpfs)
	}
}

func TestNodeLockdownP1bRootfs_AlreadyReadonlyIsAlreadyHardened(t *testing.T) {
	// The hardened sandbox path: ReadonlyRootfs is already true, so the knob
	// confirms it under every mode and writes nothing.
	hc := &container.HostConfig{ReadonlyRootfs: true}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreRootfs("enforce"), p1bPayloadFor("video_upscale"))
	if plan.Rootfs.Missing || plan.Rootfs.Needed || plan.Rootfs.Applied ||
		plan.ScratchTmpfs != nil {
		t.Errorf("an already read-only rootfs must not be touched or reported as changing: missing=%v",
			plan.Rootfs.Missing)
	}
}

// --- resource-limit floors -------------------------------------------------

func TestNodeLockdownP1bLimits_EnforceFillsAnUnlimitedPids(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreLimit(limitsPidsKey, limitsPidsCapKey, "enforce", 512),
		p1bPayloadFor("video_upscale"))
	if !plan.Pids.Applied || hc.Resources.PidsLimit == nil || *hc.Resources.PidsLimit != 512 {
		t.Errorf("enforce must write the pids floor where unlimited: applied=%v got=%v",
			plan.Pids.Applied, p1bPidsGot(hc))
	}
	if plan.PidsCap != 512 {
		t.Errorf("the requested cap must be reported: got %d", plan.PidsCap)
	}
}

func TestNodeLockdownP1bLimits_MemoryFloorIsWrittenInMB(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreLimit(limitsMemoryKey, limitsMemoryCapKey, "enforce", 2048),
		p1bPayloadFor("video_upscale"))
	if !plan.Memory.Applied || hc.Resources.Memory != 2048*bytesPerMB {
		t.Errorf("memory floor must land in BYTES (2048 MB): got %d", hc.Resources.Memory)
	}
}

func TestNodeLockdownP1bLimits_CPUFloorIsWrittenInNanoCPUs(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreLimit(limitsCpuKey, limitsCpuCapKey, "enforce", 4),
		p1bPayloadFor("video_upscale"))
	if !plan.CPU.Applied || hc.Resources.NanoCPUs != 4*nanoPerCPU {
		t.Errorf("cpu floor must land in nano-CPU units (4 cores): got %d",
			hc.Resources.NanoCPUs)
	}
}

// The load-bearing invariant: a cap the payload/executor already chose is
// NEVER rewritten, tighter or looser. Going BELOW it would OOM or starve a
// node that asked for more; going above it would silently break the node's
// declared budget contract.
func TestNodeLockdownP1bLimits_NeverRewritesAnExistingCap(t *testing.T) {
	pids := int64(4096)
	hc := &container.HostConfig{
		Resources: container.Resources{PidsLimit: &pids,
			Memory: 8192 * bytesPerMB, NanoCPUs: 22 * nanoPerCPU},
	}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		limitsPidsKey: "enforce", limitsPidsCapKey: 256, // BELOW 4096
		limitsMemoryKey: "enforce", limitsMemoryCapKey: 512, // BELOW 8192
		limitsCpuKey: "enforce", limitsCpuCapKey: 2, // BELOW 22
	})
	plan := applyNodeLockdown(&container.Config{}, hc, s, p1bPayloadFor("video_upscale"))
	if hc.Resources.PidsLimit == nil || *hc.Resources.PidsLimit != 4096 ||
		hc.Resources.Memory != 8192*bytesPerMB || hc.Resources.NanoCPUs != 22*nanoPerCPU {
		t.Errorf("an existing cap must survive untouched")
	}
	if plan.Pids.Missing || plan.Pids.Needed || plan.Pids.Applied ||
		plan.Memory.Applied || plan.CPU.Applied {
		t.Errorf("a capped container is already hardened: missing=%v/%v/%v",
			plan.Pids.Missing, plan.Memory.Missing, plan.CPU.Missing)
	}
}

func TestNodeLockdownP1bLimits_UnlimitedSentinelCountsAsMissing(t *testing.T) {
	// Docker spells "unlimited" as 0/-1; those are exactly where a floor
	// belongs.
	unlimited := int64(-1)
	hc := &container.HostConfig{
		Resources: container.Resources{PidsLimit: &unlimited}}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreLimit(limitsPidsKey, limitsPidsCapKey, "enforce", 1024),
		p1bPayloadFor("video_upscale"))
	if !plan.Pids.Missing || !plan.Pids.Applied || *hc.Resources.PidsLimit != 1024 {
		t.Errorf("an unlimited sentinel must be floored: missing=%v applied=%v got=%d",
			plan.Pids.Missing, plan.Pids.Applied, *hc.Resources.PidsLimit)
	}
}

func TestNodeLockdownP1bLimits_ReportMarksNeededWithoutWriting(t *testing.T) {
	hc := &container.HostConfig{}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreLimit(limitsMemoryKey, limitsMemoryCapKey, "report", 4096),
		p1bPayloadFor("video_upscale"))
	if !plan.Memory.Needed || plan.Memory.Applied || hc.Resources.Memory != 0 {
		t.Errorf("report must mark Needed without writing: needed=%v applied=%v mem=%d",
			plan.Memory.Needed, plan.Memory.Applied, hc.Resources.Memory)
	}
}

func TestNodeLockdownP1bLimits_ModeOnButNoCapWritesNothing(t *testing.T) {
	// enforce without a number in the registry is a no-op, not a guess: the
	// layer refuses to invent a budget for a node.
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{limitsPidsKey: "enforce"})
	plan := applyNodeLockdown(&container.Config{}, hc, s, p1bPayloadFor("video_upscale"))
	if plan.Pids.Needed || plan.Pids.Applied || hc.Resources.PidsLimit != nil {
		t.Errorf("enforce with no cap must write nothing: needed=%v applied=%v",
			plan.Pids.Needed, plan.Pids.Applied)
	}
	if !plan.Pids.Missing {
		t.Errorf("the container IS unlimited, so Missing must be true: got %v",
			plan.Pids.Missing)
	}
}

func TestNodeLockdownP1bLimits_OffLeavesEverythingUncapped(t *testing.T) {
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		limitsPidsKey: "off", limitsPidsCapKey: 256,
		limitsMemoryKey: "off", limitsMemoryCapKey: 512,
		limitsCpuKey: "off", limitsCpuCapKey: 2,
	})
	plan := applyNodeLockdown(&container.Config{}, hc, s, p1bPayloadFor("video_upscale"))
	if plan.Pids.Needed || plan.Memory.Needed || plan.CPU.Needed ||
		hc.Resources.PidsLimit != nil || hc.Resources.Memory != 0 || hc.Resources.NanoCPUs != 0 {
		t.Errorf("off adds nothing: needed=%v/%v/%v", plan.Pids.Needed,
			plan.Memory.Needed, plan.CPU.Needed)
	}
}

// GPU nodes must not lose device access: the floor block never reads or
// writes Resources.Devices / DeviceRequests.
func TestNodeLockdownP1bLimits_GPUNodeKeepsItsDevices(t *testing.T) {
	hc := &container.HostConfig{
		Resources: container.Resources{
			DeviceRequests: []container.DeviceRequest{{
				Driver: "nvidia", Capabilities: [][]string{{"gpu"}}}},
			Devices: []container.DeviceMapping{{
				PathOnHost: "/dev/dxg", PathInContainer: "/dev/dxg",
				CgroupPermissions: "rwx"}},
		},
	}
	plan := applyNodeLockdown(&container.Config{}, hc,
		p1bStoreLimit(limitsMemoryKey, limitsMemoryCapKey, "enforce", 4096),
		p1bPayloadFor("image_upscale"))
	if !plan.Memory.Applied || hc.Resources.Memory != 4096*bytesPerMB {
		t.Errorf("the memory floor must still apply to a GPU node: applied=%v mem=%d",
			plan.Memory.Applied, hc.Resources.Memory)
	}
	if len(hc.Resources.DeviceRequests) != 1 || hc.Resources.DeviceRequests[0].Capabilities[0][0] != "gpu" ||
		len(hc.Resources.Devices) != 1 || hc.Resources.Devices[0].PathInContainer != "/dev/dxg" {
		t.Errorf("device passthrough must survive the limit block")
	}

}

// --- exemption + the report line ------------------------------------------

func TestNodeLockdownP1bExempt_EnforceStillChangesNothing(t *testing.T) {
	hc := &container.HostConfig{}
	s := tunables.New()
	s.Apply(map[string]interface{}{
		rootfsKey: "enforce", seccompKey: "enforce",
		seccompProfileKey: modNodeProfileName,
		limitsPidsKey:     "enforce", limitsPidsCapKey: 256,
		limitsMemoryKey: "enforce", limitsMemoryCapKey: 512,
		limitsCpuKey: "enforce", limitsCpuCapKey: 2,
		exemptNodesKey: "hardware_benchmark",
	})
	plan := applyNodeLockdown(&container.Config{}, hc, s, p1bPayloadFor("HARDWARE_BENCHMARK"))
	if !plan.Exempt || hc.ReadonlyRootfs || hc.Tmpfs != nil || hc.Resources.PidsLimit != nil ||
		hc.Resources.Memory != 0 || hc.Resources.NanoCPUs != 0 || len(hc.SecurityOpt) != 0 {
		t.Errorf("an exempt node must be untouched by the p1b knobs too")
	}
	if plan.Rootfs.Needed || plan.Seccomp.Needed || plan.Pids.Needed ||
		plan.Memory.Needed || plan.CPU.Needed || plan.ScratchTmpfs != nil {
		t.Errorf("an exempt node must report nothing as needed: got scratch=%v",
			plan.ScratchTmpfs)
	}
	if plan.ExemptReason != "exempt_nodes_setting" {
		t.Errorf("exempt reason must be recorded: got %q", plan.ExemptReason)
	}
}

// The report line must carry the p1b knobs alongside p1a's, so one structured
// line per container describes the whole lockdown posture.
func TestNodeLockdownP1bPlanFields_CarriesTheNewKnobs(t *testing.T) {
	hc := &container.HostConfig{CapDrop: []string{"ALL"},
		SecurityOpt: []string{"no-new-privileges"}}
	plan := applyNodeLockdown(&container.Config{}, hc, tunables.New(),
		p1bPayloadFor("video_upscale"))
	flat := planFields(p1bPayloadFor("video_upscale"), plan)
	if len(flat)%2 != 0 {
		t.Errorf("report fields must be key/value pairs: len=%d", len(flat))
		return
	}
	seen := map[string]bool{}
	for i := 0; i < len(flat); i += 2 {
		k, ok := flat[i].(string)
		if !ok {
			t.Errorf("report key at %d is not a string", i)
			continue
		}
		seen[k] = true
	}
	want := []string{"seccomp_mode", "seccomp_profile", "seccomp_missing",
		"seccomp_would_apply", "seccomp_applied",
		"rootfs_mode", "rootfs_missing", "rootfs_would_apply", "rootfs_applied",
		"scratch_tmpfs",
		"limits_pids_mode", "limits_pids_missing", "limits_pids_would_apply",
		"limits_pids_applied", "limits_pids_cap",
		"limits_memory_mode", "limits_memory_missing", "limits_memory_would_apply",
		"limits_memory_applied", "limits_memory_cap_mb",
		"limits_cpu_mode", "limits_cpu_missing", "limits_cpu_would_apply",
		"limits_cpu_applied", "limits_cpu_cap_cores"}
	for _, w := range want {
		if seen[w] != true {
			t.Errorf("report line missing key %s", w)
		}
	}
	// Untouched fleet (every knob "report", no caps in the registry): the
	// embedded seccomp profile is already attached and the limit knobs have
	// no requested floor, so only the read-only rootfs is a would-change —
	// and NOTHING is applied.
	if plan.Seccomp.Needed || plan.Pids.Needed || plan.Memory.Needed || plan.CPU.Needed {
		t.Errorf("only the rootfs should be a would-change here: %v/%v/%v/%v",
			plan.Seccomp.Needed, plan.Pids.Needed, plan.Memory.Needed, plan.CPU.Needed)
	}
	if !plan.Rootfs.Needed || len(plan.ScratchTmpfs) != len(scratchPaths) {
		t.Errorf("a writable-rootfs container must report the rootfs + every scratch path as would-change: needed=%v scratch=%v",
			plan.Rootfs.Needed, plan.ScratchTmpfs)
	}
	if plan.Seccomp.Applied || plan.Rootfs.Applied || plan.Pids.Applied ||
		plan.Memory.Applied || plan.CPU.Applied {
		t.Errorf("report mode must apply nothing")
	}
	if plan.SeccompProfile != "docker" || plan.PidsCap != 0 ||
		plan.MemoryCapMB != 0 || plan.CPUCores != 0 {
		t.Errorf("an untouched registry reports docker + no floors: got %q/%d/%d/%d",
			plan.SeccompProfile, plan.PidsCap, plan.MemoryCapMB, plan.CPUCores)
	}
}
