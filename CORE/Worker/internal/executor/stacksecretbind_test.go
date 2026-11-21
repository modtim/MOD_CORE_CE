package executor

// 1564 — tests for the verified file-secrets probe that replaced the strict
// Source-string compare. Docker Desktop (WSL2) workers ALWAYS report a
// translated proxy path as the bind's Source, never the literal host path,
// so the fix stops trusting the Source string's shape and instead proves the
// daemon's view round-trips to what the worker actually writes. Swap seams
// only — no real docker daemon needed.

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
)

// translatedBindSource is a Docker-Desktop-style translated proxy path: the
// shape ContainerInspect ALWAYS reports on a WSL2 worker, deliberately never
// equal to containerSecretsRoot.
const translatedBindSource = "/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/deadbeef"

// withVerifySecretsRootProbe swaps the daemon round-trip probe seam so a test
// controls whether the (fake) bind "actually" sees the worker's files,
// without any real docker daemon. Returns restore.
func withVerifySecretsRootProbe(fn verifySecretsRootProbeFn) func() {
	orig := verifySecretsRootProbe
	verifySecretsRootProbe = fn
	return func() { verifySecretsRootProbe = orig }
}

// TestComputeFileSecretsStatusTranslatedPathPassesViaProbe is the regression
// case: a containerized worker whose secrets-root bind's Source is a
// Docker-Desktop-translated path (never equal to containerSecretsRoot) must
// be ALLOWED once the daemon round-trip probe proves the bind is real — today
// (before 1564) this scenario is wrongly DENIED by the strict string compare.
func TestComputeFileSecretsStatusTranslatedPathPassesViaProbe(t *testing.T) {
	defer withContainerized(true)()
	defer withMountinfoFSType("tmpfs", true)()
	defer withContainerInspect(types.ContainerJSON{
		Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: translatedBindSource, Destination: containerSecretsRoot},
		},
		Config: &container.Config{Image: "fake/worker-image:latest"},
	}, nil)()

	var gotSource, gotImage string
	defer withVerifySecretsRootProbe(func(_ context.Context, source, image string) (bool, error) {
		gotSource, gotImage = source, image
		return true, nil
	})()

	st := computeFileSecretsStatus()
	if !st.Allowed {
		t.Fatalf("translated-path bind verified by the probe must be allowed, got %q", st.Reason)
	}
	if gotSource != translatedBindSource {
		t.Fatalf("probe must be called with the exact translated Source, got %q", gotSource)
	}
	if gotImage != "fake/worker-image:latest" {
		t.Fatalf("probe must be called with the worker's own image, got %q", gotImage)
	}
}

// TestComputeFileSecretsStatusDeniesWhenProbeFails covers a bind that LOOKS
// present (a Destination match exists) but is genuinely wrong or stale — the
// probe's round-trip fails (no error, just a verified-false result) — and
// proves the fail-closed property still holds: Allowed must be false and the
// reason must name containerSecretsRoot (the operator's exact fix).
func TestComputeFileSecretsStatusDeniesWhenProbeFails(t *testing.T) {
	defer withContainerized(true)()
	defer withMountinfoFSType("tmpfs", true)()
	defer withContainerInspect(types.ContainerJSON{
		Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: "/genuinely/different/host/path", Destination: containerSecretsRoot},
		},
		Config: &container.Config{Image: "fake/worker-image:latest"},
	}, nil)()
	defer withVerifySecretsRootProbe(func(context.Context, string, string) (bool, error) {
		return false, nil
	})()

	st := computeFileSecretsStatus()
	if st.Allowed {
		t.Fatalf("a bind the probe cannot verify must be denied")
	}
	if !strings.Contains(st.Reason, containerSecretsRoot) {
		t.Fatalf("deny reason must name %s, got %q", containerSecretsRoot, st.Reason)
	}
}

// TestComputeFileSecretsStatusDeniesOnProbeError covers the case where the
// probe round-trip itself cannot complete (helper create/start/wait/logs
// error) — a probe that cannot be verified must NOT be treated as success.
func TestComputeFileSecretsStatusDeniesOnProbeError(t *testing.T) {
	defer withContainerized(true)()
	defer withMountinfoFSType("tmpfs", true)()
	defer withContainerInspect(types.ContainerJSON{
		Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: translatedBindSource, Destination: containerSecretsRoot},
		},
		Config: &container.Config{Image: "fake/worker-image:latest"},
	}, nil)()
	defer withVerifySecretsRootProbe(func(context.Context, string, string) (bool, error) {
		return false, fmt.Errorf("secrets root probe: create helper: daemon unreachable")
	})()

	st := computeFileSecretsStatus()
	if st.Allowed {
		t.Fatalf("a probe error must fail closed (denied), not be treated as success")
	}
	if !strings.Contains(st.Reason, containerSecretsRoot) {
		t.Fatalf("deny reason must name %s, got %q", containerSecretsRoot, st.Reason)
	}
}

// TestComputeFileSecretsStatusDeniesWithNoSecretsRootBindAtAll covers the
// unchanged existing behavior: when no mount entry's Destination is
// containerSecretsRoot at all, the worker is denied without ever calling the
// probe (nothing to verify).
func TestComputeFileSecretsStatusDeniesWithNoSecretsRootBindAtAll(t *testing.T) {
	defer withContainerized(true)()
	defer withMountinfoFSType("tmpfs", true)()
	defer withContainerInspect(types.ContainerJSON{
		Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: "/elsewhere", Destination: "/elsewhere"},
		},
		Config: &container.Config{Image: "fake/worker-image:latest"},
	}, nil)()
	probeCalled := false
	defer withVerifySecretsRootProbe(func(context.Context, string, string) (bool, error) {
		probeCalled = true
		return true, nil
	})()

	st := computeFileSecretsStatus()
	if st.Allowed {
		t.Fatalf("no bind to the secrets root at all must be denied")
	}
	if probeCalled {
		t.Fatalf("the probe must not be called when there is no bind to verify")
	}
}

// TestSecretsRootBindSourceMatchMismatch: secretsRootBindSource only requires
// Destination == containerSecretsRoot && Type == mount.TypeBind — returning
// the Source as-is, whatever shape it is (translated or literal) — and does
// NOT match a volume or a bind to a different destination.
func TestSecretsRootBindSourceMatchMismatch(t *testing.T) {
	mk := func(ms ...types.MountPoint) types.ContainerJSON {
		return types.ContainerJSON{Mounts: ms}
	}
	if src, ok := secretsRootBindSource(mk(
		types.MountPoint{Type: mount.TypeBind, Source: translatedBindSource, Destination: containerSecretsRoot},
	)); !ok || src != translatedBindSource {
		t.Fatalf("a translated-path bind to the secrets root should match on Destination alone, got (%q,%v)", src, ok)
	}
	if _, ok := secretsRootBindSource(mk(
		types.MountPoint{Type: mount.TypeVolume, Source: containerSecretsRoot, Destination: containerSecretsRoot},
	)); ok {
		t.Fatalf("a volume is not a host bind; must NOT match")
	}
	if _, ok := secretsRootBindSource(mk(
		types.MountPoint{Type: mount.TypeBind, Source: containerSecretsRoot, Destination: "/elsewhere"},
	)); ok {
		t.Fatalf("a bind to a different destination must NOT match")
	}
	if _, ok := secretsRootBindSource(mk()); ok {
		t.Fatalf("no mounts must not match")
	}
}

// TestProbeContainerConfigRunsAsWorkerUser pins the CE embedded-worker fix:
// the probe helper must run as the worker's OWN uid:gid. The embedded worker
// runs as root (compose user "0:DOCKER_GID") while the image default USER is
// 10001, and with CapDrop ALL a helper at the image uid cannot read the 0600
// nonce a root process wrote, so the round trip failed and the heartbeat
// reported file_secrets=false. Also asserts the helper config keeps its
// hardening: cat entrypoint, CapDrop ALL in the HostConfig, never Privileged.
func TestProbeContainerConfigRunsAsWorkerUser(t *testing.T) {
	const fileName = "/mod-secrets-bind-probe/.bind-probe-nonce"
	cfg := probeContainerConfig("fake/worker-image:latest", fileName)
	want := fmt.Sprintf("%d:%d", os.Getuid(), os.Getgid())
	if cfg.User != want {
		t.Fatalf("probe helper must run as the worker's own uid:gid %s, got %q", want, cfg.User)
	}
	if len(cfg.Entrypoint) != 1 || cfg.Entrypoint[0] != "cat" {
		t.Fatalf("probe helper entrypoint must stay cat, got %v", cfg.Entrypoint)
	}
	if len(cfg.Cmd) != 1 || cfg.Cmd[0] != fileName {
		t.Fatalf("probe helper cmd must be the nonce path, got %v", cfg.Cmd)
	}
	if got := cfg.Labels["mod.purpose"]; got != "secrets-root-bind-probe" {
		t.Fatalf("probe helper label must stay secrets-root-bind-probe, got %q", got)
	}
	hc := probeHostConfig("/host/secret-root", "/mod-secrets-bind-probe")
	if len(hc.CapDrop) != 1 || hc.CapDrop[0] != "ALL" {
		t.Fatalf("probe helper must drop ALL caps, got %v", hc.CapDrop)
	}
	if hc.Privileged {
		t.Fatalf("probe helper must never be Privileged")
	}
	if !hc.ReadonlyRootfs {
		t.Fatalf("probe helper rootfs must stay read-only")
	}
	if hc.NetworkMode != "none" {
		t.Fatalf("probe helper must get no network, got %q", hc.NetworkMode)
	}
	if len(hc.SecurityOpt) != 1 || hc.SecurityOpt[0] != "no-new-privileges" {
		t.Fatalf("probe helper must keep no-new-privileges, got %v", hc.SecurityOpt)
	}
	if len(hc.Binds) != 1 || !strings.HasSuffix(hc.Binds[0], ":ro") {
		t.Fatalf("probe helper secrets-root bind must stay read-only, got %v", hc.Binds)
	}
}

// TestProbeFailureReasonIsTheNoSamePathBindDeny: when the probe RAN but the
// nonce did not come back (verified=false, no error), the deny reason the
// heartbeat/API sees is fileSecretsRootStatus's own "no same-path host bind"
// string, so the platform can tell a real capability gap from a probe error.
func TestProbeFailureReasonIsTheNoSamePathBindDeny(t *testing.T) {
	defer withContainerized(true)()
	defer withMountinfoFSType("tmpfs", true)()
	defer withContainerInspect(types.ContainerJSON{
		Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: translatedBindSource, Destination: containerSecretsRoot},
		},
		Config: &container.Config{Image: "fake/worker-image:latest"},
	}, nil)()
	defer withVerifySecretsRootProbe(func(context.Context, string, string) (bool, error) {
		return false, nil
	})()

	st := computeFileSecretsStatus()
	if st.Allowed {
		t.Fatalf("a probe that did not read the nonce back must be denied")
	}
	if !strings.Contains(st.Reason, "has no same-path host bind for") {
		t.Fatalf("failed-probe deny reason must be fileSecretsRootStatus's own no-same-path-bind string, got %q", st.Reason)
	}
}
