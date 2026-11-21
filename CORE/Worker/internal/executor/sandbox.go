package executor

import (
	_ "embed"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/go-units"
)

//go:embed seccomp-node.json
var embeddedSeccompProfile []byte

// seccompProfilePath lazily materializes the embedded seccomp JSON to a real
// file path that dockerd can read. Path is picked once per process and
// returned on every subsequent call. Empty string signals "skip seccomp wiring"
// (the embed failed or the filesystem is read-only) so the caller falls back
// to the default profile.
var (
	seccompPathOnce sync.Once
	seccompPathVal  string
)

func seccompProfilePath() string {
	seccompPathOnce.Do(func() {
		if len(embeddedSeccompProfile) == 0 {
			return
		}
		// Prefer /var/lib/mod-worker/ (package-standard), fall back to
		// system temp. Stable paths are friendlier to ops poking around.
		candidates := []string{
			"/var/lib/mod-worker",
			os.TempDir(),
		}
		for _, dir := range candidates {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				continue
			}
			p := filepath.Join(dir, "seccomp-node.json")
			if err := os.WriteFile(p, embeddedSeccompProfile, 0o644); err != nil {
				continue
			}
			seccompPathVal = p
			return
		}
	})
	return seccompPathVal
}

// hardenedSandboxEnabled returns false only when DISABLE_HARDENED_SANDBOX is
// explicitly set to a truthy value. Default is on — the kill switch is opt-out
// so deployed workers pick up the hardening without config flips, and can be
// rolled back per-host with a single env var if a specific image breaks.
func hardenedSandboxEnabled() bool {
	return strings.ToLower(os.Getenv("DISABLE_HARDENED_SANDBOX")) != "true"
}

// SelectRuntime picks the Docker OCI runtime for a container. Returns
// one of:
//
//   - "runsc" when UseSandboxRuntime is true AND the worker is
//     configured to have the gVisor runtime registered (via the env
//     GVISOR_AVAILABLE=true — set during worker bootstrap when
//     `docker info | grep -i runsc` succeeds).
//   - ""       (empty) = Docker's default runtime (`runc`).
//
// Silently degrades to runc when gVisor isn't installed — a node
// asking for gVisor doesn't fail, it just runs with the standard
// runtime. Operators see the downgrade in the audit log.
func SelectRuntime(useSandbox bool) (runtime, reason string) {
	if !useSandbox {
		return "", "default runtime (runc)"
	}
	if strings.ToLower(os.Getenv("GVISOR_AVAILABLE")) != "true" {
		return "", "runsc requested but gVisor unavailable on host — falling back to runc"
	}
	return "runsc", "gVisor sandbox requested by payload.UseSandboxRuntime"
}

// applyHardenedSandbox mutates hostConfig with the Layer-3 runtime sandbox
// settings. A compromised node process running inside a container with these
// settings cannot:
//   - write to image layers (/usr, /bin, /etc, ...) — ReadonlyRootfs
//   - drop core dumps containing secrets — Ulimits core=0
//
// Bind mounts (/data, optional /var/run/docker.sock) ignore ReadonlyRootfs
// and keep their own rw/ro config, so I/O to the tenant's input/output volume
// still works.
func applyHardenedSandbox(hostConfig *container.HostConfig, enabled bool) {
	if !enabled || hostConfig == nil {
		return
	}

	hostConfig.ReadonlyRootfs = true

	// Docker's default /dev/shm is 64 MB — enough to bust DataLoader workers
	// passing tensors through shared memory (bus error / no-space-on-shm).
	// 1 GB is the practical lower bound for SDXL/PyTorch pipelines. Only
	// apply when the caller hasn't already set one (an explicit ShmSize
	// wins), and cap to half the container's memory limit when that limit
	// is set and below 1 GB — /dev/shm counts against the memory cgroup,
	// so a shm bigger than the limit would just trade a bus error for an
	// OOM kill.
	if hostConfig.ShmSize == 0 {
		shmSize := int64(1 * 1024 * 1024 * 1024)
		if hostConfig.Memory > 0 && hostConfig.Memory < shmSize {
			shmSize = hostConfig.Memory / 2
		}
		hostConfig.ShmSize = shmSize
	}

	// Ephemeral scratch — writable tmpfs for the paths well-behaved
	// containers still need even under a readonly rootfs. All writes
	// vanish on container exit; nodes with real output data should
	// land it at /data/output/ (volume bind, not tmpfs).
	//
	// Why `/root` and `/home`: tools that store per-user config in
	// HOME (docker CLI → /root/.docker, pip → /root/.cache/pip,
	// npm → /root/.npm, git → /root/.gitconfig) fail under readonly
	// rootfs with "mkdir /root/.docker: read-only file system".
	// We hit this on the ImageBuilder platform container which runs
	// real `docker build` inside itself — it created /root/.docker
	// on startup. Making /root tmpfs is strictly safer than dropping
	// ReadonlyRootfs and costs ~32 MB peak per container.
	hostConfig.Tmpfs = map[string]string{
		"/tmp":     "rw,size=256m,mode=1777",
		"/var/tmp": "rw,size=64m,mode=1777",
		"/run":     "rw,size=16m,mode=0755",
		"/root":    "rw,size=32m,mode=0755",
		"/home":    "rw,size=32m,mode=0755",
	}

	hostConfig.Ulimits = []*units.Ulimit{
		{Name: "core", Hard: 0, Soft: 0},
	}

	// Attach the embedded custom seccomp profile. The Docker SDK
	// (unlike the CLI) requires the profile JSON INLINE after the
	// `seccomp=` prefix — it does NOT accept a path. Passing a path
	// here produces: `Decoding seccomp profile failed: invalid
	// character '/' looking for beginning of value`. Skip silently
	// when the embed is empty; Docker's default profile takes over.
	if len(embeddedSeccompProfile) > 0 {
		hostConfig.SecurityOpt = append(hostConfig.SecurityOpt, "seccomp="+string(embeddedSeccompProfile))
	}
}
