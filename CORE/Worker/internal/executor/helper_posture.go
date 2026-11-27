package executor

// HELPER-CONTAINERS-LOCKDOWN (posture, 11-25): one shared lockdown posture
// for the platform's short-lived helper containers, so the flags the size-probe
// lockdown (SIZE-PROBE-CONTAINER-LOCKDOWN, 7ff31387fa) proved can never drift
// between helpers. The four helpers the size-probe audit flagged get their
// HostConfig from the builders here:
//   resultReadHostConfig  - the result-JSON read helper (executor.go
//                           readResultJSONFiles): cats UNTRUSTED tenant output
//                           files; needs NO network, so it gets the full
//                           read-helper lockdown (proven on gpu5: `cat` output
//                           byte-identical under the exact flags).
//   sidecarHostConfig     - the mc staging/upload sidecars (executor.go
//                           runSidecarWithOutput): network is REQUIRED (mc
//                           <-> MinIO/S3) and the /data bind stays rw, so
//                           those stay; caps, no-new-privileges and the
//                           safety limits are added.
//   prepHostConfig        - the volume-prep helper (volume_prep_api.go):
//                           created but NEVER started, so it gets network
//                           none + cap drop on the create (zero behaviour
//                           change: it never runs).
//   archiveReadHostConfig - the created-never-started archive-read twins
//                           (ce_archive_read.go / output_archive_read.go):
//                           same never-started posture.
//   scrubHostConfig       - the GPU VRAM tenant-scrub helper (gpu_tenant_scrub.go
//                           launchScrubHelper): needs the NVIDIA runtime's
//                           device passthrough (DeviceRequests, set by the
//                           caller exactly as gpu_assignment.go does for the
//                           processing container) but NO network: the helper
//                           only talks to the card through the device
//                           passthrough, so network none + cap drop ALL +
//                           limits apply; the GPU access itself is NOT a
//                           capability and is left intact.
//
// The Resources limits are TECHNICAL SAFETY limits, NOT product caps (owner
// rule: no quantity caps): they are deliberately generous and only ever bind
// a runaway helper.

import (
	"github.com/docker/docker/api/types/container"
)

// helperMemoryBytes / helperPidsLimit are the shared safety envelope for
// short-lived helpers (512 MiB / 256 pids, the size-probe envelope). The mc
// sidecars get their own, larger envelope (see sidecarHostConfig): an `mc cp`
// of a multi-GB output set legitimately streams, and the sidecar's liveness
// model is progress-based, so the memory limit must never bind a real transfer.
const helperMemoryBytes = int64(512 * 1024 * 1024) // 512 MiB
const helperPidsLimit = int64(256)

// sidecarMemoryBytes / sidecarPidsLimit: the mc sidecar envelope. 2 GiB is
// far above what `mc` itself holds in memory for a streamed multipart copy
// (it buffers parts, not whole objects), and 512 pids covers the sh wrapper
// plus mc; both are runaway guards, never transfer caps.
const sidecarMemoryBytes = int64(2 * 1024 * 1024 * 1024) // 2 GiB
const sidecarPidsLimit = int64(512)

// helperTmpfsOpts is the small writable /tmp under a read-only rootfs (the
// size-probe's proven value; the read helper needs it for sh/mktemp-class
// scratch without making image layers writable).
const helperTmpfsOpts = "rw,size=64m,mode=1777"

// mcConfigTmpfsOpts is the tmpfs the mc sidecar needs under a read-only
// rootfs: the minio-client image runs as root with HOME=/root and writes its
// config to /root/.mc (proven on gpu5: without a tmpfs there, `mc` fails
// "Unable to save new mc config. mkdir /root/.mc: read-only file system";
// with `--tmpfs /root/.mc:rw,size=16m,mode=1777` every mc leg used by the
// pipeline - version, local cp, network cp - runs rc 0).
const mcConfigTmpfsOpts = "rw,size=16m,mode=1777"

// helperCapAdd is the ONE capability the two file-READING helpers keep after
// CapDrop ALL: DAC_READ_SEARCH (skip permission checks when READING/searching
// files and directories). HELPER-CONTAINERS-LOCKDOWN FIX 1 (closer 4e425ad615
// found the regression): CapDrop ALL also drops CAP_DAC_OVERRIDE, which the
// pre-lockdown containers relied on to read output files a NON-ROOT tenant
// container wrote with mode 0600 (Python tempfile / umask 077). Both helpers
// run as root against a /data bind owned by another uid, so without it the
// upload sidecar fails `sha256sum: Permission denied` (rc 1, 0 uploaded) and
// the result-JSON read helper prints `cat: Permission denied`. DAC_READ_SEARCH
// is the minimal capability that restores the OLD read behaviour (DAC_OVERRIDE
// also works but additionally grants WRITE); no other capability is added.
// The never-started helpers (prep, archive-read twins) and the GPU scrub get
// NO CapAdd: they never read another uid's private files.
const helperCapAddName = "DAC_READ_SEARCH"

// helperCapAdd returns a FRESH single-capability slice (never share one array
// between HostConfigs: a caller mutating one posture must not bend another's).
func helperCapAdd() []string {
	return []string{helperCapAddName}
}

// helperBaseHostConfig is the common core: no network, every capability
// dropped, no privilege escalation, and the shared safety limits. Every
// helper builder starts from this and only ADDS what its job needs.
func helperBaseHostConfig() *container.HostConfig {
	pids := helperPidsLimit
	return &container.HostConfig{
		NetworkMode:   container.NetworkMode("none"),
		CapDrop:       []string{"ALL"},
		SecurityOpt:   []string{"no-new-privileges:true"},
		Resources: container.Resources{
			Memory:    helperMemoryBytes,
			PidsLimit: &pids,
		},
	}
}

// resultReadHostConfig is the result-JSON read helper's posture: the base
// plus a read-only rootfs (it only cats a ro bind) and the small /tmp tmpfs.
// Proven on gpu5 (HELPER-CONTAINERS-LOCKDOWN): the alpine `cat` command
// produces byte-identical stdout under exactly these flags.
func (e *Executor) resultReadHostConfig(volName string) *container.HostConfig {
	hc := helperBaseHostConfig()
	// FIX 1 (0600 parity): the helper cats tenant output files that a NON-ROOT
	// container may have written mode 0600; cap drop ALL alone breaks that read
	// (closer's `cat: Permission denied`). DAC_READ_SEARCH restores it.
	hc.CapAdd = helperCapAdd()
	hc.Binds = []string{volName + ":/data:ro"}
	hc.ReadonlyRootfs = true
	hc.Tmpfs = map[string]string{"/tmp": helperTmpfsOpts}
	return hc
}

// sidecarHostConfig is the mc staging/upload sidecar's posture. The network
// STAYS (mc must reach MinIO/S3 - the task brief's expected-network helper)
// and the /data bind STAYS rw (upload staging seeds /data). What is added:
// cap drop ALL, no-new-privileges, the (generous) sidecar limits, and the
// read-only rootfs + /root/.mc tmpfs, which the real minio-client image on
// gpu5 proved works for every leg the pipeline runs (mc cp /data/... to
// myminio/..., mc ls, mc rm) once its config dir is a tmpfs.
func (e *Executor) sidecarHostConfig(volName string) *container.HostConfig {
	pids := sidecarPidsLimit
	return &container.HostConfig{
		NetworkMode: container.NetworkMode(e.network),
		Binds:       []string{volName + ":/data:rw"},
		AutoRemove:  false, // we read logs before removing
		CapDrop:     []string{"ALL"},
		// FIX 1 (0600 parity, closer 4e425ad615): the sidecar runs as root and
		// hashes/uploads /data/output files a NON-ROOT tenant container may
		// have written mode 0600; CapDrop ALL alone made `sha256sum` fail
		// (rc 1, 0 uploaded). DAC_READ_SEARCH is the minimal capability that
		// restores the pre-lockdown read behaviour.
		CapAdd: helperCapAdd(),
		SecurityOpt: []string{"no-new-privileges:true"},
		// Read-only image layers: mc's only writable needs are the rw /data
		// bind and its config dir, which the tmpfs covers (gpu5-proven).
		ReadonlyRootfs: true,
		Tmpfs:          map[string]string{"/root/.mc": mcConfigTmpfsOpts},
		Resources: container.Resources{
			Memory:    sidecarMemoryBytes,
			PidsLimit: &pids,
		},
	}
}

// prepHostConfig is the volume-prep helper's posture: it is created but NEVER
// started (seeding is CopyToContainer), so it gets the base (network none: a
// container that never runs must never touch a network) plus the rw bind the
// archive PUT extracts into. Zero behaviour change: the container never
// executes a command.
func (e *Executor) prepHostConfig(volName string) *container.HostConfig {
	hc := helperBaseHostConfig()
	hc.Binds = []string{volName + ":/data:rw"}
	return hc
}

// archiveReadHostConfig is the archive-read twins' posture (ce_archive_read.go
// / output_archive_read.go): also created-never-started (CopyFromContainer
// reads the volume), so base + ro bind. The Config-level NetworkDisabled the
// twins already set stays; HostConfig NetworkMode "none" aligns them with
// every other helper.
func (e *Executor) archiveReadHostConfig(volName string) *container.HostConfig {
	hc := helperBaseHostConfig()
	hc.Binds = []string{volName + ":/data:ro"}
	return hc
}

// scrubHostConfig is the GPU tenant-scrub helper's posture. The helper is a
// CUDA program: its GPU access comes from the NVIDIA runtime's DeviceRequests
// (set by the caller, gpu_tenant_scrub.go), NOT from any capability or from
// the network - torch talks to the card through the device passthrough. So
// the GPU passthrough stays exactly as it is, and everything else locks down:
// network none, cap drop ALL, no-new-privileges, safety limits. No
// privileged flag is needed or added (the worker never used one).
func (e *Executor) scrubHostConfig() *container.HostConfig {
	return helperBaseHostConfig()
}
