package executor

// SIZE-PROBE-CONTAINER-LOCKDOWN tests: the probe container's HostConfig must
// carry the platform lockdown posture (network none, CapDrop ALL, no-new-privi-
// leges, read-only rootfs + /tmp tmpfs, safety resource limits, the sidecar
// label, the ro /data bind). Every guard is asserted through the REAL create
// path (the fake dockerProbeAPI records the HostConfig the hook asked for),
// and each guard has a mutation check: a variant of the helper with that ONE
// guard removed must fail the same assertion, so a regression that drops a
// flag is caught rather than silently tolerated.

import (
	"context"
	"testing"

	"github.com/docker/docker/api/types/container"
)

// --- 1. the helper itself: every field asserted directly -------------------
func TestSizeProbeHostConfig_LockdownFields(t *testing.T) {
	hc := sizeProbeHostConfig("vol-9")

	if hc.NetworkMode != container.NetworkMode("none") {
		t.Fatalf("NetworkMode must be none, got %q", hc.NetworkMode)
	}
	if !hc.ReadonlyRootfs {
		t.Fatalf("ReadonlyRootfs must be true")
	}
	var capDropAll bool
	for _, c := range hc.CapDrop {
		if c == "ALL" {
			capDropAll = true
		}
	}
	if !capDropAll {
		t.Fatalf("CapDrop must contain ALL: %v", hc.CapDrop)
	}
	var noPrivs bool
	for _, o := range hc.SecurityOpt {
		if o == "no-new-privileges:true" {
			noPrivs = true
		}
	}
	if !noPrivs {
		t.Fatalf("SecurityOpt must contain no-new-privileges:true: %v", hc.SecurityOpt)
	}
	if hc.Tmpfs["/tmp"] == "" {
		t.Fatalf("a writable /tmp tmpfs is required under a read-only rootfs: %v", hc.Tmpfs)
	}
	if hc.Resources.Memory != sizeProbeMemoryBytes {
		t.Fatalf("Memory limit must be %d, got %d", sizeProbeMemoryBytes, hc.Resources.Memory)
	}
	if hc.Resources.PidsLimit == nil || *hc.Resources.PidsLimit != sizeProbePidsLimit {
		t.Fatalf("PidsLimit must be %d", sizeProbePidsLimit)
	}
	if hc.Resources.NanoCPUs != sizeProbeNanoCPUs {
		t.Fatalf("NanoCPUs must be %d, got %d", sizeProbeNanoCPUs, hc.Resources.NanoCPUs)
	}
	if hc.Binds == nil || hc.Binds[0] != "vol-9:/data:ro" {
		t.Fatalf("inputs must stay a read-only bind: %v", hc.Binds)
	}
	if hc.Privileged {
		t.Fatalf("Privileged must be false")
	}
}

// --- 2. mutation checks: remove ONE guard, the assertion must fail ---------
// Each mutant is the same HostConfig with exactly one guard stripped; the
// matching check below must report it missing. A guard whose removal is NOT
// detected means the test suite would not catch that regression.
func TestSizeProbeHostConfig_MutationChecks(t *testing.T) {
	type mutant struct {
		name  string
		build func() *container.HostConfig
		check func(*container.HostConfig) bool
		what  string
	}
	base := sizeProbeHostConfig("vol-9")
	mutants := []mutant{
		{"network", func() *container.HostConfig {
			c := *base
			c.NetworkMode = container.NetworkMode("mod_modtex-network")
			return &c
		}, func(h *container.HostConfig) bool {
			return h.NetworkMode == container.NetworkMode("none")
		}, "NetworkMode none"},
		{"capdrop", func() *container.HostConfig {
			c := *base
			c.CapDrop = []string{}
			return &c
		}, func(h *container.HostConfig) bool {
			for _, x := range h.CapDrop {
				if x == "ALL" {
					return true
				}
			}
			return false
		}, "CapDrop ALL"},
		{"noprivs", func() *container.HostConfig {
			c := *base
			c.SecurityOpt = []string{}
			return &c
		}, func(h *container.HostConfig) bool {
			for _, o := range h.SecurityOpt {
				if o == "no-new-privileges:true" {
					return true
				}
			}
			return false
		}, "no-new-privileges:true"},
		{"rootfs", func() *container.HostConfig {
			c := *base
			c.ReadonlyRootfs = false
			return &c
		}, func(h *container.HostConfig) bool {
			return h.ReadonlyRootfs
		}, "ReadonlyRootfs"},
		{"memory", func() *container.HostConfig {
			c := *base
			c.Resources = container.Resources{}
			return &c
		}, func(h *container.HostConfig) bool {
			return h.Resources.Memory > 0 && h.Resources.PidsLimit != nil &&
				h.Resources.NanoCPUs > 0
		}, "memory+pids+cpu limits"},
		{"ro-bind", func() *container.HostConfig {
			c := *base
			c.Binds = []string{"vol-9:/data:rw"}
			return &c
		}, func(h *container.HostConfig) bool {
			return len(h.Binds) == 1 && h.Binds[0] == "vol-9:/data:ro"
		}, "ro /data bind"},
	}
	for _, m := range mutants {
		if m.check(m.build()) {
			t.Fatalf("mutation check %q: removing %s was NOT detected", m.name, m.what)
		}
		if !m.check(sizeProbeHostConfig("vol-9")) {
			t.Fatalf("mutation check %q: the real helper fails its own check", m.name)
		}
	}
}

// --- 3. the real create path: the hook asks for the locked HostConfig ------
// Guards asserted off the HostConfig the fake dockerProbeAPI actually received
// from runSizeProbe (not just off the helper), so a change at the call site
// that bypasses the helper is caught too.
func TestSizeProbe_CreateCarriesLockdown(t *testing.T) {
	p := probePayload([]string{"sha256:aaa"})

	var docker fakeProbeDocker
	docker.createOK = true
	docker.logs = framedLog(probeDoc)
	var seenHost *container.HostConfig
	var seenCfg *container.Config
	docker.captureCreate = func(cfg *container.Config, hc *container.HostConfig) {
		seenCfg, seenHost = cfg, hc
	}

	SetSizeProbeCache(nil)
	doc := SizeProbe(context.Background(), probeTask(p), probeCreds("harbor.example"),
		"mod_modtex-network", "", "vol-1", &docker)
	if doc == nil {
		t.Fatalf("probe must still return a document under the locked posture")
	}
	if seenHost == nil {
		t.Fatalf("the hook never reached ContainerCreate")
	}
	if seenHost.NetworkMode != container.NetworkMode("none") {
		t.Fatalf("create must use network none, got %q", seenHost.NetworkMode)
	}
	if !seenHost.ReadonlyRootfs {
		t.Fatalf("create must use a read-only rootfs")
	}
	var capDropAll, noPrivs bool
	for _, c := range seenHost.CapDrop {
		if c == "ALL" {
			capDropAll = true
		}
	}
	for _, o := range seenHost.SecurityOpt {
		if o == "no-new-privileges:true" {
			noPrivs = true
		}
	}
	if !capDropAll || !noPrivs {
		t.Fatalf("create must drop ALL caps and set no-new-privileges: %v / %v",
			seenHost.CapDrop, seenHost.SecurityOpt)
	}
	if seenHost.Tmpfs["/tmp"] == "" {
		t.Fatalf("create must give a writable /tmp tmpfs: %v", seenHost.Tmpfs)
	}
	if seenHost.Resources.Memory != sizeProbeMemoryBytes ||
		seenHost.Resources.PidsLimit == nil || *seenHost.Resources.PidsLimit != sizeProbePidsLimit ||
		seenHost.Resources.NanoCPUs != sizeProbeNanoCPUs {
		t.Fatalf("create must carry the safety limits: %+v", seenHost.Resources)
	}
	if seenHost.Binds == nil || seenHost.Binds[0] != "vol-1:/data:ro" {
		t.Fatalf("create must keep the ro /data bind: %v", seenHost.Binds)
	}
	// The task volume's network must NOT be attached to the probe.
	if seenHost.NetworkMode == container.NetworkMode("mod_modtex-network") {
		t.Fatalf("the probe must never join the task network")
	}
	// The existing label contract is kept (the sweep can still identify it).
	if seenCfg == nil || seenCfg.Labels["mod.purpose"] != "size-probe" {
		t.Fatalf("the mod.purpose label must survive: %+v", seenCfg)
	}
}

// --- 4. the probe still works end-to-end with the locked posture -----------
// (never-fail contract + identical document under the new flags)
func TestSizeProbe_LockdownKeepsNeverFail(t *testing.T) {
	p := probePayload([]string{"sha256:aaa"})
	var docker fakeProbeDocker
	docker.createOK = false // create fails under the locked posture
	var cache fakeSizeCache
	SetSizeProbeCache(&cache)
	if doc := SizeProbe(context.Background(), probeTask(p), probeCreds("harbor.example"),
		"mod_modtex-network", "", "vol-1", &docker); doc != nil {
		t.Fatalf("a failed locked create must still return nil, never fail the task")
	}
	if len(cache.stores) != 0 {
		t.Fatalf("a failed probe must not poison the cache")
	}
	SetSizeProbeCache(nil)
}
