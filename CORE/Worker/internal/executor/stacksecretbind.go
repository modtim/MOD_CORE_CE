package executor

// 13.5b — verified file-secrets guard for containerized workers.
//
// The blanket detectContainerized() fail-closed guard that shipped with
// GX.S10b was correct that a containerized worker's private /dev/shm is
// invisible to the docker daemon — but it also made file-secret stacks
// undeployable on EVERY real worker, because every worker runs as a Docker
// container. This file replaces it with a verified check: a containerized
// worker MAY deploy file secrets when its secrets root (containerSecretsRoot)
// is (a) a tmpfs mount point AND (b) a same-path host bind, so the daemon
// (outside the worker) sees exactly the bytes the worker wrote.
//
// Both conditions are checked per process and cached, re-checked at most every
// secretsRootRecheckInterval, so a deploy's cost is bounded and a bind added
// while the worker is running is picked up within a few minutes.

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"
)

// secretsRootRecheckInterval bounds how often the containerized worker re-
// verifies its secrets root after the first check. A few minutes is plenty: a
// bind is added by recreating the worker container, not by a runtime hot-swap.
const secretsRootRecheckInterval = 5 * time.Minute

// mountInfoPath is what the worker's own mount table is read from. Tests point
// the parseMountinfoFSType seam at a synthetic file instead.
const mountInfoPath = "/proc/self/mountinfo"

// cgroupPath is where the worker's own container id is read from (cgroup v2
// unified hierarchy: .../docker/<container-id>). hostname is the fallback for
// v1 / runtimes that do not expose the id in the cgroup path.
const cgroupPath = "/proc/self/cgroup"

// --- seam: mountinfo fstype -------------------------------------------------

// parseMountinfoFSType is the seam tests swap to return a controlled fstype for
// containerSecretsRoot (the real implementation reads /proc/self/mountinfo).
// It returns the fstype of the mount whose mountpoint is path, and ok=false
// when no such mount is present (path is not itself a mount point).
var parseMountinfoFSType = fstypeOfMount

// fstypeOfMount parses the process's own mountinfo (mountInfoPath) and returns
// the filesystem type of the mount whose MOUNTPOINT is exactly path.
func fstypeOfMount(path string) (string, bool) {
	return fstypeOfMountFile(mountInfoPath, path)
}

// fstypeOfMountFile is the file-backed parse, split out so a test can point it
// at a synthetic mountinfo fixture. The line format is
//
//	id parent_id maj:min root mountpoint opts [optional tags:] - fstype source superopts
//
// where fields 1..6 are fixed (id, parent, maj:min, root, mountpoint, opts)
// and everything after the " - " separator is optional tags followed by fstype,
// source, super-options. The MOUNTPOINT is field 5, unescaped (the kernel
// percent-escapes space/tab/newline); the fstype is the FIRST token after the
// " - " separator.
func fstypeOfMountFile(mountinfoFile, path string) (string, bool) {
	data, err := os.ReadFile(mountinfoFile)
	if err != nil {
		return "", false
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		seps := strings.SplitN(line, " - ", 2)
		if len(seps) != 2 {
			continue
		}
		fixed := strings.Fields(seps[0])
		if len(fixed) < 5 {
			continue
		}
		mp := unescapeMountinfo(fixed[4])
		if mp != path {
			continue
		}
		tail := strings.Fields(seps[1])
		// After the " - ": fstype, source, super-options, then [tags...].
		// The fstype is the FIRST token of the tail.
		if len(tail) == 0 {
			return "", false
		}
		return tail[0], true
	}
	return "", false
}

// unescapeMountinfo reverses the kernel's mountinfo field escaping: %XX hex
// (0x20 space, 0x09 tab, 0x0a newline, 0x25 literal %).
func unescapeMountinfo(s string) string {
	if !strings.Contains(s, "%") {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) {
			if v, err := strconv.ParseUint(s[i+1:i+3], 16, 8); err == nil {
				b.WriteByte(byte(v))
				i += 2
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// --- seam: self-inspect via the docker client -------------------------------

// containerInspectFn is the seam tests swap to return a faked
// ContainerInspect result for the worker's own container id. The real
// implementation builds a docker client from the environment and inspects by
// the id (or hostname) it read from /proc.
type containerInspectFn func(ctx context.Context, idOrName string) (types.ContainerJSON, error)

// containerInspect is the seam tests swap to inject a faked ContainerInspect.
var containerInspect containerInspectFn = defaultContainerInspect

// defaultContainerInspect builds a docker client the same way the executor does
// and inspects the named container, returning the raw SDK response.
func defaultContainerInspect(ctx context.Context, idOrName string) (types.ContainerJSON, error) {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return types.ContainerJSON{}, err
	}
	return cli.ContainerInspect(ctx, idOrName)
}

// selfContainerID returns the worker's own container id if it can determine it:
// the 64-hex id from /proc/self/cgroup (cgroup v2), else the container
// hostname (the docker container_name / short-id the daemon also accepts).
func selfContainerID() string {
	if data, err := os.ReadFile(cgroupPath); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			if id := extractContainerID(line); id != "" {
				return id
			}
		}
	}
	// B86i-h — cgroup v2's unified hierarchy can read back as a bare "0::/"
	// with no "/docker/<id>" path segment at all (the same shape that broke
	// insideContainer()'s substring check), leaving extractContainerID
	// nothing to find. The os.Hostname() fallback below is ALSO wrong on
	// this shape of host: it returns the container's configured hostname
	// (e.g. compose's `hostname: mod-worker`), which is not a name or id
	// the daemon's ContainerInspect can resolve — "mod-worker" 404s while
	// the real container name is "my_mod_worker" — so the self-inspect
	// failed and fileSecretsRootStatus fell back to its fail-closed DENIED
	// branch, blocking every file-secret deploy on this worker. dockerd
	// reliably bind-mounts /etc/hostname (and /etc/hosts, /etc/resolv.conf)
	// from <docker-root>/containers/<64-hex-id>/... for every container,
	// on every cgroup version and storage driver, so that mount's root path
	// is a stable, version-agnostic source for the worker's own id — tried
	// before the hostname fallback.
	if id := containerIDFromMountinfo(mountInfoPath); id != "" {
		return id
	}
	if h, err := os.Hostname(); err == nil {
		return h
	}
	return ""
}

// containerIDFromMountinfo reads mountinfoFile (normally /proc/self/mountinfo)
// looking for the /etc/hostname, /etc/hosts, or /etc/resolv.conf mount —
// always present for a Docker container — and returns the 64-hex container id
// embedded in its mount root (".../containers/<id>/hostname"), or "" when
// none of those three mounts is present or its root doesn't carry one.
func containerIDFromMountinfo(mountinfoFile string) string {
	data, err := os.ReadFile(mountinfoFile)
	if err != nil {
		return ""
	}
	wantMountpoints := map[string]bool{"/etc/hostname": true, "/etc/hosts": true, "/etc/resolv.conf": true}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		seps := strings.SplitN(line, " - ", 2)
		if len(seps) != 2 {
			continue
		}
		fixed := strings.Fields(seps[0])
		if len(fixed) < 5 {
			continue
		}
		if !wantMountpoints[unescapeMountinfo(fixed[4])] {
			continue
		}
		root := strings.Trim(unescapeMountinfo(fixed[3]), "/")
		segs := strings.Split(root, "/")
		if len(segs) < 2 {
			continue
		}
		if candidate := segs[len(segs)-2]; hexLen(candidate) >= 64 {
			return candidate
		}
	}
	return ""
}

// extractContainerID pulls a docker container id out of a cgroup line. The
// unified v2 hierarchy is a single "0::/docker/<id>" token (no whitespace
// around the path), while v1 is "N:controllers:/docker/<id>" — so the id is the
// last path segment AFTER a "docker" path element, and it must be 12+ hex
// (a 12-hex short id also matches). Anything shorter or non-hex is ignored.
func extractContainerID(line string) string {
	// The path is everything after the last ":" (the cgroup hierarchy string).
	colon := strings.LastIndex(line, ":")
	if colon < 0 {
		return ""
	}
	path := line[colon+1:]
	segs := strings.Split(path, "/")
	for i, seg := range segs {
		if seg == "docker" && i+1 < len(segs) && hexLen(segs[i+1]) >= 12 {
			return segs[i+1]
		}
	}
	return ""
}

// hexLen reports whether s is all lower/upper-hex and returns its length, or
// 0 if it is not pure hex.
func hexLen(s string) int {
	if s == "" {
		return 0
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return 0
		}
	}
	return len(s)
}

// secretsRootBindSource reports the Source of the bind mount whose
// Destination is containerSecretsRoot, whatever shape that Source string is.
//
// B86i-stack114 — Docker Desktop (WSL2) workers NEVER report the literal host
// path as Source: ContainerInspect always returns Docker Desktop's internal
// translated proxy path (/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/
// Ubuntu/<hash>), permanently, for every container, on every WSL2 host. A
// strict Source == Destination string compare therefore can never pass on
// that host class, even though the bind is real and correct — denying on
// string shape, not on an actual capability gap. The Source string itself is
// no longer trusted; verifySecretsRootProbe below proves capability instead.
func secretsRootBindSource(resp types.ContainerJSON) (string, bool) {
	for _, m := range resp.Mounts {
		if m.Type == mount.TypeBind && m.Destination == containerSecretsRoot {
			return m.Source, true
		}
	}
	return "", false
}

// --- seam: daemon round-trip probe ------------------------------------------

// secretsRootProbeTimeout bounds the probe helper's ContainerWait. The helper
// only `cat`s one small file, so anything slower than this is a wedged
// container, not a slow-but-real round trip.
const secretsRootProbeTimeout = 8 * time.Second

// verifySecretsRootProbeFn is the seam tests swap to fake the daemon
// round-trip probe result.
type verifySecretsRootProbeFn func(ctx context.Context, source, image string) (bool, error)

// verifySecretsRootProbe is the seam tests swap to inject a faked probe.
var verifySecretsRootProbe verifySecretsRootProbeFn = defaultVerifySecretsRootProbe

// probeContainerConfig builds the probe helper's container.Config, split out
// so tests can assert it without a docker daemon.
func probeContainerConfig(image, fileName string) *container.Config {
	return &container.Config{
		Image: image,
		// Run the helper as the worker's own uid:gid so it can read the 0600
		// nonce this process wrote (CapDrop ALL means root-without-caps cannot
		// read a file owned by another user; the image USER 10001 cannot read a
		// root-written file, which is the CE embedded worker's case).
		User:       fmt.Sprintf("%d:%d", os.Getuid(), os.Getgid()),
		Entrypoint: []string{"cat"},
		Cmd:        []string{fileName},
		Labels:     map[string]string{"mod.purpose": "secrets-root-bind-probe"},
	}
}

// probeHostConfig builds the probe helper's HostConfig, split out so tests can
// assert the hardening (CapDrop ALL, no Privileged) without a docker daemon.
func probeHostConfig(source, probeMount string) *container.HostConfig {
	return &container.HostConfig{
		Binds: []string{source + ":" + probeMount + ":ro"},
		// Hardening: this helper only needs to cat one read-only file, so
		// it gets no network, no capabilities, no writable rootfs, and no
		// privilege escalation - none of that is needed for the probe to
		// succeed, so none of it is granted.
		NetworkMode:    "none",
		CapDrop:        []string{"ALL"},
		ReadonlyRootfs: true,
		SecurityOpt:    []string{"no-new-privileges"},
	}
}

// defaultVerifySecretsRootProbe proves the daemon can actually see what this
// worker writes under containerSecretsRoot, instead of trusting the mount's
// Source string (which Docker Desktop always translates). It writes a random
// nonce file under containerSecretsRoot, then starts a short-lived helper
// container — from the SAME image this worker is already running, so no
// network pull is needed — with a read-only bind of source (the mount's
// reported Source, whatever shape it is) and has it cat the nonce file back.
// A match proves the daemon's view of source is this worker's
// containerSecretsRoot; any failure (create/start/wait/logs/mismatch) is
// fail-closed: false, not an assumed pass.
func defaultVerifySecretsRootProbe(ctx context.Context, source, image string) (bool, error) {
	if source == "" {
		return false, fmt.Errorf("secrets root probe: empty bind source")
	}
	if image == "" {
		return false, fmt.Errorf("secrets root probe: empty worker image")
	}

	nonceBytes := make([]byte, 16)
	if _, err := rand.Read(nonceBytes); err != nil {
		return false, fmt.Errorf("secrets root probe: generate nonce: %w", err)
	}
	nonce := hex.EncodeToString(nonceBytes)
	probeFileName := ".bind-probe-" + nonce
	probeFilePath := filepath.Join(containerSecretsRoot, probeFileName)
	if err := os.WriteFile(probeFilePath, []byte(nonce), 0o600); err != nil {
		return false, fmt.Errorf("secrets root probe: write probe file: %w", err)
	}
	defer os.Remove(probeFilePath)

	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return false, fmt.Errorf("secrets root probe: new docker client: %w", err)
	}

	const probeMount = "/mod-secrets-bind-probe"
	created, err := cli.ContainerCreate(ctx,
		probeContainerConfig(image, probeMount+"/"+probeFileName),
		probeHostConfig(source, probeMount),
		&network.NetworkingConfig{}, nil, "",
	)
	if err != nil {
		return false, fmt.Errorf("secrets root probe: create helper: %w", err)
	}
	containerID := created.ID

	// Unconditional teardown on every return path — mirrors
	// gpu_tenant_scrub.go's scrubWaitAndRead: a probe helper left running
	// would otherwise strand a container on every deploy attempt.
	defer func() {
		rmCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = cli.ContainerRemove(rmCtx, containerID, container.RemoveOptions{Force: true})
	}()

	if err := cli.ContainerStart(ctx, containerID, container.StartOptions{}); err != nil {
		return false, fmt.Errorf("secrets root probe: start helper: %w", err)
	}

	waitCtx, cancelWait := context.WithTimeout(ctx, secretsRootProbeTimeout)
	defer cancelWait()
	statusCh, errCh := cli.ContainerWait(waitCtx, containerID, container.WaitConditionNotRunning)
	select {
	case werr := <-errCh:
		// Timeout or daemon wait error: cannot prove the round trip completed,
		// so kill the wedged helper and fail closed rather than assume success.
		killCtx, cancelKill := context.WithTimeout(context.Background(), 5*time.Second)
		_ = cli.ContainerKill(killCtx, containerID, "SIGKILL")
		cancelKill()
		if werr != nil {
			return false, fmt.Errorf("secrets root probe: wait: %w", werr)
		}
		return false, fmt.Errorf("secrets root probe: wait returned no status")
	case <-statusCh:
	}

	logReader, err := cli.ContainerLogs(ctx, containerID, container.LogsOptions{ShowStdout: true})
	if err != nil {
		return false, fmt.Errorf("secrets root probe: read logs: %w", err)
	}
	var stdout, stderr bytes.Buffer
	_, _ = stdcopy.StdCopy(&stdout, &stderr, logReader)
	logReader.Close()

	if strings.TrimSpace(stdout.String()) != nonce {
		return false, nil
	}
	return true, nil
}

// --- cached guard -----------------------------------------------------------

// fileSecretsRootStatus is the decision the deploy/teardown guard and the
// heartbeat both read. Allowed means file secrets MAY be materialized on this
// worker; Reason is a human-accurate explanation (safe to put in a log or task
// error) of why — for a deny it names the exact bind to add.
type fileSecretsRootDecision struct {
	Allowed bool
	Reason  string
}

var (
	fileSecretsStatusMu sync.Mutex
	cachedStatus        fileSecretsRootDecision
	cachedAt            time.Time
)

// resetFileSecretsStatusCache clears the cache. Tests call it to force a
// re-check after swapping the seams.
func resetFileSecretsStatusCache() {
	fileSecretsStatusMu.Lock()
	defer fileSecretsStatusMu.Unlock()
	cachedStatus = fileSecretsRootDecision{}
	cachedAt = time.Time{}
}

// fileSecretsRootStatus returns the cached (≤ secretsRootRecheckInterval old)
// decision for whether this worker may materialize stack file secrets.
//
//   - Not containerized: ALLOWED. A host-process worker's /dev/shm is the
//     host's tmpfs, which the daemon sees; the on-disk fallback is also on the
//     host filesystem, so it is visible too.
//   - Containerized: ALLOWED only when BOTH (a) containerSecretsRoot is a
//     tmpfs mount point (from /proc/self/mountinfo) AND (b) the worker's own
//     container has a same-path host bind Source == Destination ==
//     containerSecretsRoot. Otherwise DENIED, with the exact bind to add.
//
// A daemon error during the self-inspect is treated as DENIED (fail closed):
// we cannot confirm the daemon can see the files, so we must not serve a
// deploy that would mount them empty.
func fileSecretsRootStatus() fileSecretsRootDecision {
	fileSecretsStatusMu.Lock()
	defer fileSecretsStatusMu.Unlock()
	if (cachedStatus != (fileSecretsRootDecision{})) && time.Since(cachedAt) < secretsRootRecheckInterval {
		return cachedStatus
	}
	st := computeFileSecretsStatus()
	cachedStatus = st
	cachedAt = time.Now()
	return st
}

// computeFileSecretsStatus is the pure (uncached) decision. Split out so tests
// can exercise each branch directly without the cache.
func computeFileSecretsStatus() fileSecretsRootDecision {
	if !detectContainerized() {
		return fileSecretsRootDecision{
			Allowed: true,
			Reason:  "worker is a host process; the daemon sees its secrets root",
		}
	}

	// (a) the secrets root must be a tmpfs mount point we can actually write to.
	fstype, ok := parseMountinfoFSType(containerSecretsRoot)
	if !ok {
		return denyStatus(fmt.Sprintf(
			"secrets root %s is not a mount point in this container", containerSecretsRoot))
	}
	if fstype != "tmpfs" {
		return denyStatus(fmt.Sprintf(
			"secrets root %s is mounted as %s, not tmpfs — secret files must not land on durable disk",
			containerSecretsRoot, fstype))
	}

	// (b) the host must bind that same path into this container, so the daemon
	// sees the files. Inspect our own container via the daemon we already use.
	idOrName := selfContainerID()
	if idOrName == "" {
		return denyStatus(fmt.Sprintf(
			"could not determine this worker's own container id to verify the %s host bind", containerSecretsRoot))
	}
	resp, err := containerInspect(context.Background(), idOrName)
	if err != nil {
		return denyStatus(fmt.Sprintf(
			"could not inspect this worker's own container to verify the %s host bind: %v",
			containerSecretsRoot, err))
	}
	source, ok := secretsRootBindSource(resp)
	if !ok {
		return denyStatus(fmt.Sprintf(
			"container %s has no host bind for %s", idOrName, containerSecretsRoot))
	}
	var selfImage string
	if resp.Config != nil {
		selfImage = resp.Config.Image
	}
	// The Source string's SHAPE is not trusted (Docker Desktop/WSL2 always
	// reports a translated proxy path, never the literal host path) — a probe
	// proves the daemon's view of source round-trips to what this worker
	// actually writes, instead of string-comparing paths.
	verified, err := verifySecretsRootProbe(context.Background(), source, selfImage)
	if err != nil {
		return denyStatus(fmt.Sprintf(
			"could not verify the %s host bind for container %s via daemon round-trip probe: %v",
			containerSecretsRoot, idOrName, err))
	}
	if !verified {
		return denyStatus(fmt.Sprintf(
			"container %s has no same-path host bind for %s", idOrName, containerSecretsRoot))
	}
	return fileSecretsRootDecision{
		Allowed: true,
		Reason:  fmt.Sprintf("secrets root %s is a tmpfs host bind the daemon can see (verified by round-trip probe)", containerSecretsRoot),
	}
}

// denyStatus builds a denied status whose reason names the EXACT bind to add,
// so the operator's fix is unambiguous.
func denyStatus(why string) fileSecretsRootDecision {
	return fileSecretsRootDecision{
		Allowed: false,
		Reason: why + " — add the bind " + containerSecretsRoot + ":" + containerSecretsRoot +
			" to the worker container (host /run is tmpfs on systemd and WSL hosts)",
	}
}

// FileSecretsRootAllowed reports whether this worker may materialize stack file
// secrets: always true for a host-process worker, and for a containerized
// worker only when its secrets root is a verified tmpfs same-path host bind.
// Exported so the heartbeat can report the honest `file_secrets` capability
// (heartbeat must not import executor, so main.go wires this via a provider)
// and the API's placement gate can trust it.
func FileSecretsRootAllowed() bool {
	return fileSecretsRootStatus().Allowed
}

// fileSecretsDeployError is the fail-closed refusal for a file-secret deploy on
// a worker that cannot be verified to hand its secrets to the daemon. It reads
// the cached decision so the message is accurate, not a blanket claim.
func fileSecretsDeployError() error {
	st := fileSecretsRootStatus()
	if st.Allowed {
		// Defensive: the guard should never hand this to an allowed worker.
		return fmt.Errorf("file secrets unexpectedly allowed yet refused (reason: %s)", st.Reason)
	}
	return fmt.Errorf("file secrets cannot be deployed on this worker: %s", st.Reason)
}
