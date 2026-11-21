package executor

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	goruntime "runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/volume"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/auth"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/cleanup"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/credsrefresher"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/lockdown"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/sockproxy"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// Executor runs Docker container pipelines based on task instructions.
// It's intentionally dumb — it does exactly what the task payload says.
// No business logic, no orchestration decisions.
//
// Harbor registry is intentionally NOT stored here. It's sourced per-task from
// Vault via creds.Harbor.Registry (the API resolves the tenant's harbor_registry
// secret and passes it with each task claim). This keeps Vault as the single
// source of truth and prevents registry/credential mismatches across workers.

// composeCmdRedactedFunc is a test hook type: when a non-nil value is set on
// Executor.composeCmdRedactedHook, unit tests intercept all docker compose
// invocations without needing a daemon.
type composeCmdRedactedFunc func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error)

// imageInspectHook is a test-only override (same pattern as composeCmdRedactedHook
// just below): when set, unit tests can drive the post-pull image-inspect step —
// the one that runs right after a prefer-local cache hit — without a daemon.
type imageInspectFunc func(ctx context.Context, image string) (types.ImageInspect, error)

type Executor struct {
	docker              *client.Client
	localRegistry       string   // e.g. registry:5000 (user containers, local-network only)
	network             string   // Docker network for sidecar containers + platform-service access
	userNetwork         string   // #83: Internal-only user network — zero outbound, zero internal service access. Default home for nodes with empty AllowedEgress.
	egressNetwork       string   // #87: External-egress user network — outbound routable, no internal services. For nodes with non-empty AllowedEgress (or API_KEY / NIM / NEEDS_INTERNET system containers).
	apiURL              string   // worker's own API URL; used to override container_env.API_URL so remote-worker containers can reach a routable host instead of the Docker-internal `api` hostname
	egressProxyMode     string   // MOD_EGRESS_PROXY default "enforce": rewrite FromAllowlist verdicts into the proxy bridge
	egressProxyImage    string   // MOD_EGRESS_PROXY_IMAGE: the egress proxy bridge image (resolved lazily in runContainerWithCreds)
	hardwareTags        []string // open-ended hardware tags from capabilities detection, priority-ordered

	// composeCmdRedactedHook is a test-only override: when set, unit tests
	// can mock all docker compose invocations without needing a daemon.
	composeCmdRedactedHook composeCmdRedactedFunc

	// imageInspectHook is a test-only override for the post-pull image-inspect
	// step (see the hook type above); nil in production, where the daemon is
	// the source of truth.
	imageInspectHook imageInspectFunc

	// pruneMu serializes disk-cleanup prunes (diskcleanup.go) so a
	// threshold sweep and a post-build light prune can't stack into a
	// concurrent prune storm. TryLock → skip-if-busy.
	pruneMu sync.Mutex

	// pausedContainers holds container IDs currently paused by NOAH yield.
	// The inactivity-liveness guard treats a paused container as making
	// progress so it is not killed as "no progress" while frozen.
	pausedContainers sync.Map // containerID -> struct{}

	// workerName / instanceID stamp every sidecar this executor spawns.
	// instanceID is regenerated on each process start, which is what lets
	// PruneStaleContainers distinguish a sidecar belonging to THIS live
	// process (never reclaim — it may be a legitimate multi-hour transfer)
	// from one orphaned by a previous incarnation that was SIGKILLed
	// mid-task (always reclaim, regardless of age).
	workerName string
	instanceID string

	// reapCheckpoints asks the manager which checkpoint volumes may be
	// removed. nil disables the sweep entirely, which is the correct default:
	// a worker with no way to ask must never guess, because guessing wrong
	// destroys a six-hour job's progress.
	reapCheckpoints CheckpointReaper

	// NODE-RETRY: postureNetNotify is wired by main to the token server so a
	// posture network created MID-TASK (mod_stack-api-net is created on
	// demand — see ensureAttachableNetwork) rebinds the node-token endpoint
	// immediately instead of waiting for the next probeLoop round (tokenRebindProbe
	// = 5s in tokenserver.go). Without it the first node container scheduled
	// onto that net cannot reach MOD_TASK_TOKEN_URL: mod_node_auth._fetch_from_worker
	// raises, the container exits 1 (executor.go:1332 "container exited with code
	// %d"), the API requeues, and the retry ~10s later lands after the probe
	// round and succeeds (CE run 19, tasks 67/68). nil = no notification.
	postureNetNotify func(name string)

	// imageLRU tracks the last time each image was used (when a container
	// started from it). Used by the LRU image GC to decide which images
	// to prune first under disk pressure. Protected by imageLRUMu.
	imageLRUMu sync.RWMutex
	imageLRU   map[string]int64 // imageID -> unix timestamp of last use

	// pinnedImages is the list of image name patterns that should never
	// be pruned (MOD_WORKER_PINNED_IMAGES env var, plus worker's own image).
	pinnedImages []string

	// lastCleanup records the most recent cleanup pass for heartbeat reporting.
	lastCleanupMu sync.RWMutex
	lastCleanup   *LastCleanup

	// FX.91 p5: the posture the worker actually observed on the internal-only
	// posture network (see netpol.go). Guards NetpolStatus, which the heartbeat
	// reports; netpolPostureWarned keeps the egress-gap WARN to once per process.
	netpolMu            sync.RWMutex
	netpolPosture       *NetpolPosture
	netpolPostureWarned bool

	// 25.5 self-cleanup (internal/cleanup): the latest full report (for the
	// heartbeat `cleanup` summary) and the live active-task-ID provider.
	// cleanupMu guards both; the report is written by the self-cleanup
	// goroutine and read by the heartbeat sender.
	cleanupMu            sync.RWMutex
	cleanupActiveTaskIDs func() []int
	cleanupLastReport    *cleanup.Report

	// tunables holds server-pushed config from the API's platform_settings
	// registry (GS.PSR.7), refreshed on every heartbeat via SetTunables. nil
	// until the caller wires it (e.g. tests constructing &Executor{} by
	// hand), in which case every reader falls back to its local env/default
	// exactly as before this landed — see currentEgressProxyMode/
	// preferLocalImages.
	tunables *tunables.Store

	// dockProxyMu/dockProxy hold the Q20 Docker socket proxy (see
	// dockproxy.go), started lazily when worker.docker_proxy_mode is
	// report/enforce. nil until first use; the proxy lives for the
	// process — its socket is bind-mounted into every task container
	// that asks for a docker socket while it is armed.
	dockProxyMu sync.Mutex
	dockProxy   *sockproxy.Server

	// harborHostMu/harborHost cache the most recently seen platform Harbor
	// registry host (from a task's creds.Harbor.Registry — see
	// harborRegistry / noteHarborHost) so PullImagePublic (GF.C3b), which
	// runs at pre-pull/profiling time before any task creds exist, still
	// has a host to rewrite public image refs against once at least one
	// task has run. Empty until then; the harbor-proxy-cache pull path
	// treats an empty host as "not known yet" and skips the rewrite.
	harborHostMu sync.RWMutex
	harborHost   string

	// modelCache picks the per-download model-cache source (GF.C2) and caches
	// the latest choice + scores for the heartbeat. Built in New() so every
	// executor has one; its probes degrade to "upstream direct" when the
	// tunables store or the site status are unavailable.
	modelCache *modelCacheManager

	// tenantScrubLast is the per-card "last tenant" ledger (FX.80.p2): card
	// device identity -> tenant label of the last job that ran on it.
	// tenantScrubHistory is the capped ring of ScrubResults the heartbeat
	// reports as `gpu_scrubs`. Both are guarded by tenantScrubMu, which also serialises
	// the scrub decision itself so two concurrent tasks cannot both "claim"
	// one card with neither scrubbing. tenantScrub is the scrubber (its
	// launch field is the test seam).
	tenantScrubMu      sync.Mutex
	tenantScrubLast    map[string]string
	tenantScrubHistory []ScrubResult
	tenantScrub        *VramScrubber

	log *slog.Logger
}

// SetTunables wires the shared tunables store this executor consults for
// server-pushed config (GS.PSR.7). Called once from main.go after both the
// executor and the heartbeat sender exist; safe to call before any tasks run
// since the store itself is safe for concurrent use from the moment it is
// constructed.
func (e *Executor) SetTunables(t *tunables.Store) {
	e.tunables = t
	if e.modelCache != nil {
		e.modelCache.tunables = t
	}
}

// Callbacks bundles the per-task hooks the executor fires while a
// processing container runs. Used to be FIELDS on the Executor itself,
// but the Executor is a singleton shared across goroutines — main.go
// dispatches each claimed task in `go executeTask(..., exec, ...)`, so
// when two tasks ran concurrently on the same worker they raced on
// assignment of `exec.OnLogBatch`. The second task's reassignment
// silently rebound the callback, and the first container's log stream
// then fired the SECOND task's closure — sending Container A's lines
// into `task:{B.id}:logs` in Redis. Symptom: a wizard build's
// `[test-and-repair]` output appearing in a user's PNG-to-PDF run
// detail. Threading these per-call instead removes the race.
type Callbacks struct {
	// OnContainerCreated is called with the container ID when the
	// processing container is created. Used by the resource monitor
	// to track container stats.
	OnContainerCreated func(containerID string)
	// OnLogBatch is called during container execution with each batch
	// of stdout/stderr lines (demuxed, empty lines dropped). main.go
	// wires this to transport.SendLog so the API's /container-logs
	// endpoint can serve live output via Redis. Batches flush every
	// 500ms or every 32 lines, whichever comes first.
	OnLogBatch func(lines []string)
	// ResourceActive reports whether the task's container is currently using
	// CPU/GPU (per the resource monitor). It's a progress signal for the
	// inactivity-based liveness check: a container that's silently computing
	// (no log output but pegging the GPU) counts as "making progress" and is
	// NOT killed. nil → no resource signal (liveness relies on log output).
	ResourceActive func() bool
}

// centralLogLine is what the worker's OWN system logger may record about a node
// run's output (GSEC FX.96, owner decision Q82=A): the line itself is customer
// content and must never reach MOD's central logs, so the worker logs a stable
// digest — "h:" + first 10 hex of sha256 — instead. The digest keeps "which line
// on which task" correlatable without storing the content, and matches
// utils.secret_redaction.hash_for_log on the API side.
//
// The customer's own output still travels to its tenant-scoped store unchanged
// (SendLog -> the tenant-private task:{id}:logs Redis key, and the task's
// result_payload log_tail, both served only through the tenant-gated
// /container-logs endpoints). Only the worker's log line is digested.
func centralLogLine(line string) string {
	sum := sha256.Sum256([]byte(line))
	return "h:" + hex.EncodeToString(sum[:])[:10]
}

// LastCleanup records the most recent cleanup pass.
// Used by the heartbeat to report disk reclaim stats.
type LastCleanup struct {
	At               time.Time
	ReclaimedBytes   uint64
	ReclaimableBytes uint64
}

// GetLastCleanup returns a copy of the last cleanup record.
func (e *Executor) GetLastCleanup() *LastCleanup {
	e.lastCleanupMu.RLock()
	defer e.lastCleanupMu.RUnlock()
	if e.lastCleanup == nil {
		return nil
	}
	// Return a copy to avoid race conditions
	cpy := *e.lastCleanup
	return &cpy
}

// DockerClient returns the underlying Docker client for resource monitoring.
func (e *Executor) DockerClient() *client.Client {
	return e.docker
}

// _inactivityLivenessEnabled gates the inactivity-based container liveness model
// (kill on NO-PROGRESS rather than on wall-clock duration). Default ON; set
// MOD_INACTIVITY_LIVENESS=0 to instantly revert to the legacy fixed-timeout
// behavior without a redeploy.
func _inactivityLivenessEnabled() bool {
	return os.Getenv("MOD_INACTIVITY_LIVENESS") != "0"
}

// Sidecar identity labels. Every container spawned by runSidecarWithOutput
// carries all three, which is what lets PruneStaleContainers ask the daemon
// for exactly our sidecars instead of listing every container on the host and
// inspecting each one — and, more importantly, lets it tell one of ITS OWN
// live transfers apart from an orphan left by a worker that died mid-task.
// The worker/instance pair is deliberately generic — it stamps per-task
// VOLUMES as well as sidecars, so both reapers share one ownership model.
const (
	sidecarLabel         = "com.example.mod-core.sidecar"
	sidecarWorkerLabel   = "com.example.mod-core.worker"
	sidecarInstanceLabel = "com.example.mod-core.instance"

	// taskVolumeLabel marks the ephemeral per-task /data volume created by
	// createVolume, so PruneStaleVolumes can identify its own work without
	// ever pattern-matching a named service volume.
	taskVolumeLabel = "com.example.mod-core.task-volume"

	// keepVolumeLabel is an unconditional opt-out from every reaper, for
	// volumes that must survive being unused for arbitrarily long.
	//
	// The case that motivates it is a model-weight cache. A stopped LLM stack
	// leaves its cache volume dangling by design — that is the whole point of
	// `stop` rather than `teardown` (llm_idle.py: "STOP, NEVER TEAR DOWN"),
	// because re-pulling 12-24GB of weights turns a ~3.5 minute wake into a
	// full re-download. "Unused" is therefore NOT evidence of "abandoned" for
	// these, and no age or ownership heuristic can tell them apart. Stamp the
	// volume and the question stops being a judgement call.
	keepVolumeLabel = "com.example.mod-core.keep"

	// taskContainerLabel marks the main processing container so it can be
	// identified in prune sweeps without being visible to the sidecar reaper.
	taskContainerLabel = "com.example.mod-core.task"
)

// modCoreProjectPrefix is the registry PROJECT the prune policy is scoped to,
// deliberately without a host. See isModCoreImage.
const modCoreProjectPrefix = "modtim/"

// taskVolumePrefix namespaces every per-task data volume this worker creates.
//
// Ownership by OUR OWN naming, rather than by a generic shape. The previous
// name was `<node_kind>_<16hex>`, which is not a claim about ownership at all —
// node kinds are arbitrary words, and any tool on the host that emits a
// hex-suffixed volume would have matched it. This host also runs
// comic-catalog, flowise, lister and defense-logic-twin, so that mattered.
//
// A prefix we own cannot collide, is visible in plain `docker volume ls`, and
// makes the reaper's scope something an operator can verify with grep rather
// than having to trust. Volumes created before this prefix existed simply do
// not match and are therefore never touched — the deliberate trade for not
// having to guess about anything already on disk.
const taskVolumePrefix = "modcore-task-"

// checkpointMountPath is where a node finds its own prior progress. Fixed
// rather than configurable so a node image can hard-code it, and separate from
// /data so nothing that walks the input tree trips over it.
const checkpointMountPath = "/checkpoint"

// checkpointVolumeLabel marks volumes that must OUTLIVE a task, so the stale
// task-volume prune (which is scoped positively to what it created) never
// mistakes one for an orphan.
const checkpointVolumeLabel = "com.example.checkpoint"

// checkpointVolumePrefix is what the manager names these, and what this worker
// will offer up for reaping. Only ever removes what matches — the same
// positive scoping the task-volume prune uses.
const checkpointVolumePrefix = "modcore-ckpt-"

// taskVolumeMinAge keeps the reaper away from a volume that is briefly
// unreferenced mid-task — between the prepare sidecar exiting and the
// processing container starting, the volume really is dangling for a moment.
// The instance label already covers this; the age floor is the backstop for a
// volume whose labels somehow did not survive.
const taskVolumeMinAge = 30 * time.Minute

// defaultSidecarIdleWindow is how long a sidecar may make NO progress before
// it is treated as stalled.
//
// Deliberately an idle window and not a wall-clock cap. A sidecar is `mc`
// moving bytes to or from MinIO, and the payload is whatever the node
// produced — a 20-hour photogrammetry run can emit a point cloud that takes
// hours to upload. A duration cap strict enough to contain a leak would
// destroy exactly that work. The failure mode being defended against is a
// transfer that STOPS MOVING, which is orthogonal to how long it runs.
//
// Two hours is deliberately generous, and is a PLACEHOLDER pending
// measurement. "No progress" is sampled from the client's own counters, and
// `mc` goes genuinely quiet at the tail of a large upload: after the last part
// ships it blocks on CompleteMultipartUpload while MinIO assembles server-side,
// consuming no network, disk, or CPU. That window has not been measured on a
// real multi-hundred-GB output. Killing there would discard hours of completed
// work to reclaim one container, so the window is set far above any plausible
// assembly time rather than tuned for fast leak detection. Leak containment
// does not depend on it being tight: an orphaned sidecar is reclaimed by
// ownership the moment the worker restarts, regardless of this value.
//
// Tighten only once a representative large upload has been timed end to end.
const defaultSidecarIdleWindow = 2 * time.Hour

// sidecarProbeInterval is how often the guard samples progress. Cheap enough
// to run per-sidecar (one stats one-shot) and fine-grained relative to the
// idle window.
const sidecarProbeInterval = 30 * time.Second

// _sidecarIdleWindow is the no-progress window, env-overridable via
// MOD_SIDECAR_IDLE_S for a no-redeploy adjustment.
func _sidecarIdleWindow() time.Duration {
	if v := os.Getenv("MOD_SIDECAR_IDLE_S"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
	}
	return defaultSidecarIdleWindow
}

// sidecarStuckAfter is the LAST-RESORT age backstop, and applies only to
// sidecars whose owner cannot be identified as live: one labeled for a
// different worker (whose own reaper should normally collect it), or a
// pre-label legacy leak. Ownership, not age, is the primary signal — see
// PruneStaleContainers — precisely because an age rule tight enough to catch
// leaks promptly would also kill the multi-hour uploads a long render or
// training run produces. Derived from the guard window so tuning one tunes
// the other, floored at 24h so it can never fire on a transfer the guard
// would still consider alive.
//
// The multiplier is small because the window is already generous; what matters
// is only that the backstop stays comfortably clear of the guard, so a sidecar
// the guard would still be watching can never be reclaimed out from under it.
func sidecarStuckAfter() time.Duration {
	if d := 4 * _sidecarIdleWindow(); d > 24*time.Hour {
		return d
	}
	return 24 * time.Hour
}

// workerIdentity returns this worker's name, matching the WORKER_NAME the
// config package reads. Used as a label so a shared host's reaper never
// touches another worker's in-flight sidecars.
func workerIdentity() string {
	// Embedded mode always runs as the fixed name (config.EmbeddedWorkerName),
	// whatever WORKER_NAME says, so this label cannot drift from the config.
	if strings.EqualFold(strings.TrimSpace(os.Getenv("WORKER_MODE")), "embedded") {
		return "mod-worker-embedded"
	}
	if v := strings.TrimSpace(os.Getenv("WORKER_NAME")); v != "" {
		return v
	}
	return "unknown"
}

// readCappedExecStream reads at most maxBytes off a docker exec/log stream.
// Extracted from containerDataBytes so unit tests can prove the bound with a
// counting reader; behaviour is identical
// to the inline io.ReadAll(io.LimitReader(r, maxBytes)) it replaced (read errors
// were already ignored by the callers).
func readCappedExecStream(r io.Reader, maxBytes int64) []byte {
	b, _ := io.ReadAll(io.LimitReader(r, maxBytes))
	return b
}

// containerDataBytes returns the total byte size of /data inside the container
// (inputs + outputs) via a best-effort `du`. Used as an inactivity progress
// signal: if /data's size changes between probes the job is writing output and
// counts as "making progress" even with no logs and ~0 CPU/GPU (e.g. a long
// disk-bound phase of a multi-day photogrammetry/training run). Best-effort:
// returns ok=false if the container has no shell/du (e.g. distroless) or the
// probe times out — callers then fall back to the log + CPU/GPU signals.
func (e *Executor) containerDataBytes(ctx context.Context, containerID string) (int64, bool) {
	ec, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	cr, err := e.docker.ContainerExecCreate(ec, containerID, container.ExecOptions{
		Cmd:          []string{"sh", "-c", "du -sb /data 2>/dev/null | cut -f1"},
		AttachStdout: true,
		Tty:          true, // raw stream (no stdcopy framing) — simpler to parse
	})
	if err != nil {
		return 0, false
	}
	att, err := e.docker.ContainerExecAttach(ec, cr.ID, container.ExecAttachOptions{Tty: true})
	if err != nil {
		return 0, false
	}
	defer att.Close()
	out := readCappedExecStream(att.Reader, stageOutLogMaxBytes)
	for _, f := range strings.Fields(string(out)) {
		if n, perr := strconv.ParseInt(f, 10, 64); perr == nil {
			return n, true
		}
	}
	return 0, false
}

// killContainer best-effort terminates a container (SIGTERM, grace, SIGKILL).
func (e *Executor) killContainer(containerID, reason string) {
	e.log.Warn("killing container", "reason", reason, "container", containerID)
	kc, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = e.docker.ContainerKill(kc, containerID, "SIGTERM")
	time.Sleep(5 * time.Second)
	_ = e.docker.ContainerKill(kc, containerID, "SIGKILL")
}

// PauseContainer freezes a running task's container (cgroup freezer) so it
// stops consuming CPU/GPU cycles while a human uses the machine. The container
// is marked paused so the inactivity-liveness guard does not kill it.
func (e *Executor) PauseContainer(ctx context.Context, containerID string) error {
	if containerID == "" {
		return nil
	}
	e.pausedContainers.Store(containerID, struct{}{})
	if err := e.docker.ContainerPause(ctx, containerID); err != nil {
		e.pausedContainers.Delete(containerID)
		return err
	}
	e.log.Info("paused container (NOAH yield)", "container", containerID)
	return nil
}

// UnpauseContainer resumes a previously paused task's container.
func (e *Executor) UnpauseContainer(ctx context.Context, containerID string) error {
	if containerID == "" {
		return nil
	}
	err := e.docker.ContainerUnpause(ctx, containerID)
	e.pausedContainers.Delete(containerID)
	if err != nil {
		return err
	}
	e.log.Info("resumed container (NOAH)", "container", containerID)
	return nil
}

// EvictContainer kills a task's container so its run fails and requeues. Clears
// any paused mark first (unpause before kill is unnecessary — SIGKILL applies
// to frozen cgroups — but the mark must not linger).
func (e *Executor) EvictContainer(containerID, reason string) {
	e.pausedContainers.Delete(containerID)
	e.killContainer(containerID, reason)
}

func (e *Executor) isPaused(containerID string) bool {
	_, ok := e.pausedContainers.Load(containerID)
	return ok
}

// New creates an executor connected to the local Docker daemon.
//
// Note: the Harbor registry is not a constructor argument — it is sourced from
// Vault via creds.Harbor.Registry on each task. Only the local (user-container)
// registry is a static argument here.
//
// apiURL is the worker's own reachable API endpoint (e.g. https://example.com
// for a remote worker, or http://api:80 for an embedded worker). It overrides any
// API_URL the server put in container_env, because the server hardcodes the
// Docker-internal `http://api:80` which remote-worker containers cannot resolve.
func New(localRegistry, dockerNetwork, apiURL string, hardwareTags []string, log *slog.Logger) (*Executor, error) {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return nil, fmt.Errorf("docker client: %w", err)
	}

	// Verify Docker is reachable
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := cli.Ping(ctx); err != nil {
		return nil, fmt.Errorf("docker ping: %w", err)
	}

	// #83 + #87: Two isolated networks for user containers.
	//
	//  - mod-user-internal: Internal=true, ZERO outbound. No access to
	//    API / Redis / Vault / Postgres AND no outbound to the
	//    internet. This is the default-deny home for any node whose
	//    AllowedEgress is empty — a compromised node on this network
	//    literally cannot open a TCP socket to anything outside the
	//    bridge.
	//  - mod-user-egress: Internal=false. For nodes that have
	//    explicitly opted in via a non-empty AllowedEgress list, or
	//    for system containers declaring API_KEY / NIM_API_KEY /
	//    NEEDS_INTERNET. Still isolated from MOD internal services
	//    (no DNS entries for API / Redis / Vault / Postgres) but can
	//    reach external hosts.
	//
	// Sidecar containers (minio-client, download/upload helpers) use
	// the main network for MinIO access — that's orchestrated by the
	// worker, not the user's image, so it stays outside this policy.
	userNet := "mod-user-internal"
	_, err = cli.NetworkCreate(context.Background(), userNet, network.CreateOptions{
		Driver:     "bridge",
		Internal:   true, // Zero-outbound default-deny
		EnableIPv6: nil,
		Labels:     map[string]string{"mod.purpose": "user-container-isolation-deny"},
	})
	// FX.91 design item 1 (Q77=A, node-container egress default-deny): fail
	// closed. A failed NetworkCreate must NOT widen user containers onto the
	// main network — _userNetworkAfterCreate keeps the intended isolated
	// name, and requireUserNetwork at the per-task selection site refuses
	// to start user tasks while the network is missing.
	{
		var created bool
		userNet, created = _userNetworkAfterCreate(userNet, err)
		if !created {
			log.Error("could not create internal-only user network mod-user-internal; user containers will fail closed (no fallback to main network)", "error", err)
		}
	}

	egressNet := "mod-user-egress"
	_, err = cli.NetworkCreate(context.Background(), egressNet, network.CreateOptions{
		Driver:     "bridge",
		Internal:   false, // Needs outbound — gated by the AllowedEgress allowlist upstream
		EnableIPv6: nil,
		Labels:     map[string]string{"mod.purpose": "user-container-isolation-egress"},
	})
	// FX.91 design item 1 (Q77=A): same fail-closed treatment as the
	// default-deny home — a failed create keeps mod-user-egress (never
	// main), and requireUserNetwork fails tasks until it exists.
	{
		var created bool
		egressNet, created = _userNetworkAfterCreate(egressNet, err)
		if !created {
			log.Error("could not create egress user network mod-user-egress; AllowedEgress tasks will fail closed (no fallback to main network)", "error", err)
		}
	}

	// Parse pinned images from MOD_WORKER_PINNED_IMAGES.
	// Format: comma-separated glob patterns (e.g., "ghcr.io/modtim/*,registry.local/custom:*")
	pinned := []string{}
	if raw := strings.TrimSpace(os.Getenv("MOD_WORKER_PINNED_IMAGES")); raw != "" {
		for _, pat := range strings.Split(raw, ",") {
			pat = strings.TrimSpace(pat)
			if pat != "" {
				pinned = append(pinned, pat)
			}
		}
	}

	e := &Executor{
		docker:              cli,
		localRegistry:       localRegistry,
		network:             dockerNetwork,
		userNetwork:         userNet,
		egressNetwork:       egressNet,
		apiURL:              strings.TrimRight(strings.TrimSpace(apiURL), "/"),
		egressProxyMode:     egressProxyModeFromEnv(),
		egressProxyImage:    os.Getenv("MOD_EGRESS_PROXY_IMAGE"),
		hardwareTags:        hardwareTags,
		workerName:          workerIdentity(),
		instanceID:          newInstanceID(),
		imageLRU:            make(map[string]int64),
		pinnedImages:        pinned,
		log:                 log,
	}
	// GF.C2 — model-cache selection. The tunables store is wired later by the
	// caller (SetTunables), so the manager starts with a nil tunables pointer
	// and picks it up there; every reader falls back to "no mirror / no other
	// caches" until then, which is exactly the pre-existing behaviour.
	e.modelCache = newModelCacheManager(log, nil, e.dockerTSStatusRunner)
	// FX.80.p2 — per-card tenant scrub. Built here so every executor has one
	// even while the gate is off: the gate is checked at call time, and a
	// missing scrubber is a hard failure (fail-closed), which would otherwise
	// make "gate on" mean "cannot scrub" on any executor built by New().
	e.tenantScrub = newTenantScrubber(e)
	return e, nil
}

// _userNetworkAfterCreate is the FX.91 (Q77=A) fail-closed decision for the
// two user-container networks: an "already exists" error is success, any
// other NetworkCreate failure keeps the intended isolated network name (it
// must NEVER become the main network — that widened user containers onto
// platform services + the internet). Returns (networkName, createdOk).
func _userNetworkAfterCreate(name string, err error) (string, bool) {
	if err == nil || strings.Contains(err.Error(), "already exists") {
		return name, true
	}
	return name, false
}

// egressProxyModeForTask resolves the egress proxy mode with precedence
// server-pushed tunable > local MOD_EGRESS_PROXY env > "enforce", then
// normalises every non-"enforce" value to "off". FX.91 design item 2 (Q77=A):
// there is no audit-only escape hatch — a mode of "off"/"audit"/anything else
// would leave an AllowedEgress task on bare mod-user-egress with unproxied
// outbound access, so the task is refused instead (see requireProxiedEgress).
func (e *Executor) egressProxyModeForTask() string {
	if e.currentEgressProxyMode() == "enforce" {
		return "enforce"
	}
	return "off"
}

// _egressGuard is the FX.91 design item 2 (Q77=A) hard gate: a FromAllowlist
// verdict (a task with a non-empty AllowedEgress) that did NOT engage the
// per-task proxy bridge must fail outright — it must never run on bare
// mod-user-egress, which is unproxied and therefore grants unrestricted
// outbound access. Only "enforce" is honoured; "off" (and any other mode,
// including a report-only "audit") is a refusal. Non-allowlist verdicts
// (platform/main, stack posture, default-deny home) are untouched here —
// design item 3 (marker tasks onto a scoped bridge) is a later task.
func _egressGuard(sel NetworkSelection, proxyMode string) error {
	if !sel.FromAllowlist || sel.Proxied {
		return nil
	}
	return fmt.Errorf("egress default-deny: AllowedEgress task refused in egress proxy mode %q; it would otherwise run on bare mod-user-egress with unproxied outbound access (set worker.egress_proxy_mode=enforce)", proxyMode)
}

// userNetworkProbe is the slice of the docker client the fail-closed user-network
// existence check needs (a *client.Client satisfies it; tests pass a stub).
type userNetworkProbe interface {
	NetworkInspect(ctx context.Context, networkID string, options network.InspectOptions) (network.Inspect, error)
}

// requireUserNetwork is the FX.91 design item 1 (Q77=A) fail-closed check: if
// the network policy picked one of the two user-container networks and that
// network does not exist on this host (its create at worker start-up failed),
// the task must fail rather than be widened onto the main network.
func requireUserNetwork(probe userNetworkProbe, name string) error {
	if _, err := probe.NetworkInspect(context.Background(), name, network.InspectOptions{}); err != nil {
		return fmt.Errorf("egress default-deny network %s unavailable; refusing to run user container on main network: %s", name, err)
	}
	return nil
}

// egressProxyModeFromEnv resolves the egress proxy mode the worker applies to
// allowlist verdicts. Default is "enforce" (opt the tenant's allowlist network
// into the proxy bridge); "off" disables the rewrite so the allowlist network
// stays as-is with no proxy marker.
func egressProxyModeFromEnv() string {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("MOD_EGRESS_PROXY"))) {
	case "off", "disable", "disabled", "false", "0":
		return "off"
	default:
		return "enforce"
	}
}

// egressProxyModeTunableKey is the platform_settings registry key for the
// egress proxy mode (must match CORE/API/services/platform_settings.py's
// register() call and worker_management.py's _WORKER_TUNABLE_KEYS mapping).
const egressProxyModeTunableKey = "worker.egress_proxy_mode"

// currentEgressProxyMode resolves the egress proxy mode with precedence:
// server-pushed tunable (GS.PSR.7) > local MOD_EGRESS_PROXY env > "enforce".
// Read fresh on every use (not cached), so a Dashboard change to the
// platform setting applies within two heartbeats — no worker restart. The
// local env/default half of that precedence is baked into e.egressProxyMode
// once at construction, same as before this landed.
func (e *Executor) currentEgressProxyMode() string {
	if e.tunables != nil {
		if v := e.tunables.String(egressProxyModeTunableKey, "", "", "off", "enforce"); v != "" {
			return v
		}
	}
	return e.egressProxyMode
}

// newInstanceID returns a random per-process identifier. Falls back to a
// constant only if the entropy source fails, which degrades orphan detection
// to the age backstop rather than breaking it.
func newInstanceID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "unknown"
	}
	return hex.EncodeToString(b[:])
}

// sidecarLabels stamps a helper container with this worker's identity.
// ensureAttachableNetwork makes a named docker network exist on this host
// before a container is attached to it by NetworkMode. Used for the shared
// NIM posture network (mod_stack-api-net): the NIM stack declares it external
// and ensureExternalNetworks creates it, but a client task pinned to it may be
// scheduled before that stack, so we ensure it here too. Best-effort and
// idempotent — an "already exists" is success and any other failure is left to
// surface as the specific ContainerCreate/Start error.
//
// FX.91 p5 (this task): when the verdict is an internal-only one
// (internalOnly=true — the gov-undeclared stamp, or the default-deny home), a
// network WE create is created with Internal: true, so the "internal-only"
// claim is actually enforced. An existing network is never deleted or
// recreated (live containers are attached); if it exists without Internal the
// posture is a reported gap, not something we can fix here — see
// ensurePostureNetwork in netpol.go, which records the observation and WARNs
// once. Callers that need egress pass internalOnly=false and are unaffected.
// SetPostureNetNotify wires the hook main calls with the token server's
// NoteNetwork (see the field comment above postureNetNotify). Exported so
// cmd/worker can wire it — the field itself stays package-private.
func (e *Executor) SetPostureNetNotify(fn func(name string)) {
	e.postureNetNotify = fn
}

func (e *Executor) ensureAttachableNetwork(ctx context.Context, name string, internalOnly bool) {
	e.ensurePostureNetwork(ctx, e.docker, name, internalOnly)
	// NODE-RETRY: a posture net created/observed HERE is exactly the net this
	// task's container is about to land on, so the node-token endpoint must
	// learn its gateway IP NOW — the probeLoop in tokenserver.go only re-derives
	// every tokenRebindProbe (5s), which is what left the FIRST attempt's
	// MOD_TASK_TOKEN_URL unreachable (connection refused -> mod_node_auth raises
	// -> container exits 1 -> API requeues -> the retry ~10s later, after a probe
	// round, succeeds). Idempotent: rebind keeps already-live addresses and opens
	// only genuinely new ones (tokenserver.go rebind).
	if e.postureNetNotify != nil {
		e.postureNetNotify(name)
	}
}

func (e *Executor) sidecarLabels() map[string]string {
	return map[string]string{
		sidecarLabel:         "1",
		sidecarWorkerLabel:   e.workerName,
		sidecarInstanceLabel: e.instanceID,
	}
}

// taskContainerLabels stamps the main processing container with identifying
// labels. Unlike sidecarLabels, we do NOT include sidecarLabel so the task
// container is not visible to the sidecar reaper. We DO include the worker
// identity so the orphan sweeper can still identify our work, plus mod.task
// for debugging.
func (e *Executor) taskContainerLabels(taskID int) map[string]string {
	return map[string]string{
		taskContainerLabel:   "1",
		sidecarWorkerLabel:   e.workerName,
		sidecarInstanceLabel: e.instanceID,
		"mod.task":           strconv.Itoa(taskID),
	}
}

// noRegistryPosture is the CE "no registry at all" shape: the API issues an
// EMPTY harbor block (1665l Gap W) when the platform registry env (DOCKER_
// REGISTRY) is unset, so the Go Credentials struct decodes to zero-value
// HarborCredentials. Nothing exists to authenticate against, so this is NOT a
// misconfiguration. A registry missing while creds are present — or creds
// missing while a registry is configured — is the hosted-side host/creds
// mismatch the fail-closed guard below exists to catch, and stays fatal.
func noRegistryPosture(harbor transport.HarborCredentials) bool {
	return strings.TrimSpace(harbor.Registry) == "" &&
		harbor.Username == "" && harbor.Password == ""
}

// harborRegistry extracts the Vault-sourced Harbor registry host from the
// per-task credentials bundle. Returns an error if empty — we fail closed
// rather than fall back to any env var or hardcoded default, because the
// registry must match the Harbor credentials that ship alongside it.
func harborRegistry(harbor transport.HarborCredentials) (string, error) {
	reg := strings.TrimSpace(harbor.Registry)
	reg = strings.TrimRight(reg, "/")
	if reg == "" {
		return "", fmt.Errorf("harbor registry is empty in task credentials (tenant Vault must have harbor_registry set)")
	}
	return reg, nil
}

// harborRegistryForTask is the container-pipeline (Run) view of harborRegistry:
// the identical fail-closed guard, plus the CE carve (1665q Gap W2). When the
// API ships an empty harbor block — no registry AND no creds — there is no
// host to prefix image refs with and nothing to authenticate against: the
// task's own image ref is used as-is, from the local daemon or the default
// public registry, exactly as CE compose names it, so return "" without an
// error. A task that names no image at all and has no registry is a
// misconfiguration and still errors, and every other empty-registry shape
// (creds present, host missing — the hosted-side host/creds mismatch) still
// errors. Callers that must have a Harbor (k8s staging, stack runner) keep
// calling harborRegistry verbatim.
func harborRegistryForTask(payload transport.TaskPayload, harbor transport.HarborCredentials) (string, error) {
	reg, err := harborRegistry(harbor)
	if err == nil || !noRegistryPosture(harbor) || payload.ProcessingImage == "" {
		return reg, err
	}
	return "", nil
}

// sidecarImageRef names the minio-client sidecar. With a Harbor host it lives
// under that host's modtim/ project, same as the processing images (the
// sidecar MUST use the host the Harbor credentials authenticate against,
// otherwise the pull 401s). With no registry configured (CE), CE does not
// build/publish its own minio-client image (not in open-core's VETTABLE
// list), so this renders the EMPTY ref: under the CE no-registry posture
// every staging leg is in process (1665w, ce_inprocess_staging.go) and the
// public upstream mc client is NEVER named or pulled (the Harbor-hosted
// image is itself built FROM it, CORE/DockerContainers/minio-client/,
// which is a Harbor-only artifact). Same entrypoint ("mc") for the Harbor ref.
func sidecarImageRef(harborReg string) string {
	// (1665w: under the CE no-registry posture there is NO sidecar image at all
	// - every staging leg is in process (ce_inprocess_staging.go) and the only
	// volume-shell image the posture names is the alpine tar helper, so this
	// renders the empty ref rather than the public upstream client.)
	if harborReg == "" {
		return ""
	}
	return harborReg + "/modtim/minio-client:latest"
}

// unenforcedEgressReport builds the EgressReport the executor attaches to the
// run when the task's network was an allowlist verdict but the proxy was NOT
// engaged. Mode is "unenforced" and AllowedHosts mirrors the declared
// allowlist, so managers/ops can tell an opted-in allowlist that simply wasn't
// proxied from a plain default-deny verdict (which carries no EgressReport at
// all).
func unenforcedEgressReport(sel NetworkSelection) *transport.EgressReport {
	return &transport.EgressReport{
		Mode:         "unenforced",
		AllowedHosts: sel.EgressHosts,
	}
}

// Result is what the executor returns after a successful run.
type Result struct {
	Outputs  []transport.OutputFile
	LogTail  string
	Duration time.Duration
	// OOMKilled is true when the kernel killed the processing container for
	// exceeding memory. Docker exposes this on State.OOMKilled and nothing
	// used to read it, so an OOM was reported as a generic non-zero exit —
	// indistinguishable from a crash, and invisible to the manager deciding
	// whether to requeue elsewhere.
	OOMKilled bool
	// ErrorDetail carries the failing sidecar's raw, untruncated stderr —
	// calibration-health-fixtures-and-timeouts §3. Populated only on a
	// sidecar failure (download/prepare/upload) so an operator can tell a
	// missing key from a missing bucket from an auth failure from the queue
	// row alone, instead of the mc success banner ("Added myminio
	// successfully.") drowning out the actual error line. Empty on success
	// and on failures that aren't sidecar-shaped (e.g. the processing
	// container itself exiting non-zero, which already reports via LogTail).
	ErrorDetail string
	// Egress summarizes the egress posture observed while running the task,
	// or nil when the run carried no allowlist network. Populated by Run.
	Egress *transport.EgressReport
	// EphemeralShmMB / EphemeralTmpfsMB are the POST-clamp values the worker
	// actually applied to the processing container's /dev/shm and /tmp tmpfs
	// (WS1.38c); EphemeralClamped is true when either requested value was
	// reduced to the worker's ephemeral cap. All zero when the task declared
	// no ephemeral memory. Populated by Run.
	EphemeralShmMB   int64
	EphemeralTmpfsMB int64
	EphemeralClamped bool
	// LockdownReport is the FX.115 p2 machine-readable twin of the "lockdown-report"
	// log line for THIS task's node container (node_lockdown.go planMap): the
	// per-flag Missing/Needed/Applied verdict plus the subject node kind.
	// nil when the task ran no node container (native/k8s/stack paths).
	// Populated by Run off runContainerWithCredsInternal.
	LockdownReport map[string]any
	// ExecutedImageDigest is the docker-digest of the image that was actually
	// used to run the processing container (B119). Populated after the pull
	// step in Run so callers can report it in the audit trail even when a
	// local fallback was used (may differ from the expected digest).
	ExecutedImageDigest string
	// UsageTokens (30.6.11 c11-J3) is the token metering read back out of the
	// processing container's /data/output/result.json (the stack-model-call
	// helper writes {"model":…,"usage":{prompt_tokens,completion_tokens,total_tokens}}).
	// nil — key omitted on the wire — for every task that wrote no usage, so
	// completion bodies are byte-for-byte unchanged for old API consumers.
	// Reading usage NEVER fails a task: a missing file, an over-cap file, a
	// parse error and an out-of-range count all yield nil / a dropped field.
	UsageTokens *transport.UsageTokens
	// VerifyTables / VerifyTruncated (30.6.1 c1-J3d) are the per-table row counts
	// of a restored logical snapshot, produced by logicalVerifyCounts ONLY after
	// logicalRestore returned nil — never on a failed restore, which would count
	// a half-applied DB. Bookkeeping: a failed/timed-out count batch yields no
	// counts and NEVER fails the restore. A nil map means the wire key is omitted
	// (transport.TaskResult carries both with omitempty), so completion bodies
	// stay byte-for-byte identical for every other op and for old API consumers.
	VerifyTables    map[string]int64
	VerifyTruncated bool
}

// resolveProcessingImage selects the right Docker image based on this worker's hardware tags.
// Fallback chain: iterate tags in priority order → cpu variant → default ProcessingImage.
// harborReg must be the Vault-sourced registry (from creds.Harbor.Registry) and is only
// consulted when payload.ProcessingImage is empty.
//
// When the server sends an immutable digest (node-version-pinning) for the selected
// variant, the returned ref is pinned to it ("<repo>@sha256:…"), so the subsequent pull
// AND container run reference the exact same build and a moved tag cannot change what a
// pinned run executes. Absent a digest, the mutable tag ref is returned as before.
func (e *Executor) resolveProcessingImage(payload transport.TaskPayload, harborReg string) string {
	defaultImage := payload.ProcessingImage
	if defaultImage == "" {
		defaultImage = harborReg + "/modtim/" + payload.NodeKind
	}

	// Default (no variants): pin to the top-level ImageDigest when present.
	if len(payload.GPUVariants) == 0 {
		return pinToDigest(defaultImage, payload.ImageDigest)
	}

	// Track which backend key was selected so we can look up its parallel digest.
	imageRef, selected := defaultImage, ""
	for _, tag := range e.hardwareTags {
		key := strings.ToLower(tag)
		if img, ok := payload.GPUVariants[key]; ok && img != "" {
			e.log.Info("using hardware variant image", "tag", tag, "image", img)
			imageRef, selected = img, key
			break
		}
	}
	if selected == "" {
		if img, ok := payload.GPUVariants["cpu"]; ok && img != "" {
			e.log.Warn("no variant for hardware tags, falling back to CPU", "tags", e.hardwareTags, "image", img)
			imageRef, selected = img, "cpu"
		}
	}
	if selected == "" {
		e.log.Info("no matching variant, using default image", "tags", e.hardwareTags, "image", defaultImage)
		return pinToDigest(defaultImage, payload.ImageDigest)
	}
	return pinToDigest(imageRef, payload.GPUVariantDigests[selected])
}

// selectedGPUBackend returns the accelerator backend this worker will actually
// run the task on, derived from payload.GPUVariants ∩ e.hardwareTags using the
// SAME precedence as resolveProcessingImage (hardware tags are priority-ordered;
// the CPU variant is never an accelerator). Returns "" when the task has no
// gpu_variants or the only match is the cpu variant.
//
// gpu-passthrough-embedded-worker: this is the authoritative signal for GPU
// device passthrough. The scheduler routes a task to an accelerator queue purely
// from NodeDefinition.gpu_variants (utils/queue_selector.select_queue_for_node),
// and the task payload's `gpu_required` bool is frequently NULL/false because it
// is only ever hydrated from the workflow-graph node_data (never from the
// NodeDefinition). So a CUDA node dispatched to this worker arrived with
// gpu_required=false and got NO DeviceRequests → torch fell back to CPU. Tying
// the GPU request to the variant we selected makes the worker self-consistent:
// if we chose the cuda/rocm/intel/npu image, we hand the matching device in.
func (e *Executor) selectedGPUBackend(payload transport.TaskPayload) string {
	if len(payload.GPUVariants) == 0 {
		return ""
	}
	for _, tag := range e.hardwareTags {
		key := strings.ToLower(strings.TrimSpace(tag))
		if key == "" || key == "cpu" || key == "default" {
			continue
		}
		if img, ok := payload.GPUVariants[key]; ok && img != "" {
			return key
		}
	}
	return ""
}

// pinToDigest rewrites an image reference to pull by immutable digest when one is
// available, e.g. "reg/modtim/foo:latest" + "sha256:ab…" -> "reg/modtim/foo@sha256:ab…".
// A moved tag can then never change the artifact a pinned run executes; pull and run use
// the same ref. Returns imageRef unchanged when digest is empty (legacy dispatch that
// sends no digest) or when the ref is already digest-pinned.
func pinToDigest(imageRef, digest string) string {
	digest = strings.TrimSpace(digest)
	if digest == "" || strings.Contains(imageRef, "@") {
		return imageRef
	}
	if !strings.HasPrefix(digest, "sha256:") {
		digest = "sha256:" + digest
	}
	// Strip an existing tag: the tag is a ':' AFTER the last '/'. A registry host may
	// embed a ':port' before the first '/', so only look past the final path separator.
	repo := imageRef
	if slash := strings.LastIndex(imageRef, "/"); slash != -1 {
		if colon := strings.LastIndex(imageRef[slash+1:], ":"); colon != -1 {
			repo = imageRef[:slash+1+colon]
		}
	} else if colon := strings.LastIndex(imageRef, ":"); colon != -1 {
		repo = imageRef[:colon]
	}
	return repo + "@" + digest
}

// PrePullImage pulls the processing image in advance (during start_after wait).
// Returns the resolved image name for cache reporting. Picks between
// platform and tenant Harbor creds by image hostname, same as Run().
func (e *Executor) PrePullImage(ctx context.Context, payload transport.TaskPayload, harbor, tenantHarbor transport.HarborCredentials) (string, error) {
	harborReg, err := harborRegistry(harbor)
	if err != nil && payload.ProcessingImage == "" {
		// Only fatal if we would have needed harborReg to build the default image.
		return "", fmt.Errorf("pre-pull: %w", err)
	}
	imageName := e.resolveProcessingImage(payload, harborReg)
	pullCreds := PickRegistryAuth(imageName, harbor, tenantHarbor)
	e.log.Info("pre-pulling processing image",
		"image", imageName,
		"registry_kind", registryKindFor(imageName, harbor, tenantHarbor),
	)
	if err := e.pullImage(ctx, imageName, pullCreds, payload.ImageDigest); err != nil {
		return imageName, fmt.Errorf("pre-pull: %w", err)
	}
	e.log.Info("pre-pull complete", "image", imageName)
	return imageName, nil
}

// Run executes the full container pipeline for a task:
//  1. Create ephemeral volume
//  2. Download input from MinIO (sidecar container)
//  3. Pull processing image
//  4. Run processing container
//  5. Upload outputs to MinIO (sidecar container)
//  6. Cleanup volume + containers
//
// The executor does not decide what to run — it follows the task instructions.
func (e *Executor) Run(ctx context.Context, task *transport.Task, creds *transport.Credentials, cb Callbacks) (res *Result, err error) {
	started := time.Now()
	payload := task.Payload

	// egress is written into only by runContainerWithCreds, and copied onto
	// whatever *Result Run returns by this single defer — so every return
	// path (success, failure, and the early sidecar-error exits) carries the
	// same unenforced-proxy report. Res is left nil only when Run itself
	// returns nil, and the defer skips the assignment in that case.
	var egress *transport.EgressReport
	// ephApplied is written into only by runContainerWithCreds (WS1.38c), and
	// copied onto whatever *Result Run returns by this single defer — every
	// return path (success, non-zero exit, early sidecar-error exits) carries
	// the same post-clamp ephemeral values. Left zero when the task declared
	// no ephemeral memory (applyEphemeralResources never ran).
	var ephApplied ephemeralApplied
	// lockdownOut is written into only by runContainerWithCredsInternal
	// (FX.115 p2) — the per-container lockdown verdict, so a task that FAILED
	// (the common breakage case: enforce makes the node exit non-zero) still
	// reports what WOULD change / what changed. Fields stays nil for paths
	// that never create a node container (stack ops, native, k8s).
	var lockdownOut LockdownReportOut
	// executedImageDigest is the B119 digest of the image that actually ran
	// (pulled OR kept locally). 1900Y1g: it lands in this local — NOT on res
	// — because res (Run's named *Result return) is still nil when the
	// inspect step runs mid-body; the defer below copies it onto whichever
	// Result Run actually returns.
	var executedImageDigest string
	defer func() {
		if res != nil {
			res.Egress = egress
			res.EphemeralShmMB = ephApplied.ShmMB
			res.EphemeralTmpfsMB = ephApplied.TmpfsMB
			res.EphemeralClamped = ephApplied.Clamped
			res.LockdownReport = lockdownOut.Fields
			res.ExecutedImageDigest = executedImageDigest
		}
	}()

	// add-persistent-stacks: a stack op runs the compose-runner (docker compose)
	// instead of the single-container processing path.
	if payload.StackOp != "" {
		return e.runStackOpSerialized(ctx, task, creds, started, cb)
	}

	// sz-build-queue.1 — task_type "build" routes to the build-task handler
	// instead of the generic single-container processing path below (which
	// has no ProcessingImage for a build task and would fail confusingly
	// deep inside image-pull). handleBuildTask is a stub today (see
	// build_task.go) — later jobs (sz-build-queue.3+) fill in the real
	// BuildKit runner.
	if payload.TaskType == "build" {
		return e.handleBuildTask(ctx, task, creds, started, cb)
	}

	// hx.7c (opsx worker-host-exec) — task_type "host-exec" routes to the
	// host-exec handler instead of the container path (no ProcessingImage).
	if payload.TaskType == "host-exec" || hostExecOf(task) {
		return e.handleHostExecTask(ctx, task, creds, started, cb)
	}

	// Worker-side disk hygiene: after a build task (the ImageBuilder node)
	// finishes — success OR failure — fire a light prune (build cache +
	// dangling layers) so build workers don't accrue layer sludge between
	// the threshold sweeps. Deferred + async so it never delays the task
	// result. See diskcleanup.go.
	if strings.Contains(payload.NodeKind, "image_builder") {
		defer e.lightPruneAfterBuild()
	}

	timeout := time.Duration(payload.ContainerTimeout) * time.Second
	if timeout == 0 {
		timeout = 30 * time.Minute
	}
	// workflow-agentic-execution-loop §wizard-test-run-dispatch — sandbox
	// runs override the default timeout with the orchestrator-supplied
	// SandboxTimeoutS (60s default). Hard cap so a wedged container in
	// sandbox mode can't DoS the worker pool.
	if payload.Purpose == "wizard_test_run" && payload.SandboxTimeoutS > 0 {
		timeout = time.Duration(payload.SandboxTimeoutS) * time.Second
	}

	// Create a timeout context for the entire pipeline. Sandbox runs
	// get a tighter outer cap (sandbox timeout + 30s grace for sidecar
	// upload/cleanup) instead of the +10min production grace.
	outerGrace := 10 * time.Minute
	if payload.Purpose == "wizard_test_run" {
		outerGrace = 30 * time.Second
	}
	// Outer cap. For normal runs (when inactivity-liveness is enabled) there is
	// NO wall-clock ceiling by default — a job that keeps making progress (e.g.
	// photogrammetry / training that can legitimately run for DAYS or WEEKS)
	// must be able to run to completion. The ONLY liveness guard is the
	// inactivity check inside runContainerWithCreds (kill when a container stops
	// making progress). An optional MOD_CONTAINER_ABSOLUTE_MAX_S sets a hard
	// ceiling for cost-controlled / multi-tenant deployments (default: none).
	// Sandbox runs always keep their tight hard cap (DoS guard). Whole model is
	// env-flippable (MOD_INACTIVITY_LIVENESS=0) for instant rollback.
	outerCap := timeout + outerGrace
	if _inactivityLivenessEnabled() && payload.Purpose != "wizard_test_run" {
		outerCap = 0 // unlimited by default
		if v := os.Getenv("MOD_CONTAINER_ABSOLUTE_MAX_S"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 {
				outerCap = time.Duration(n) * time.Second
			}
		}
	}
	var pipelineCtx context.Context
	var cancel context.CancelFunc
	if outerCap > 0 {
		pipelineCtx, cancel = context.WithTimeout(ctx, outerCap)
	} else {
		pipelineCtx, cancel = context.WithCancel(ctx) // no deadline — inactivity is the guard
	}
	defer cancel()

	// Harbor registry must come from the per-task credentials bundle (Vault).
	// The sidecar image lives under modtim/ in the same Harbor that hosts
	// processing images, so it MUST use the same host the Harbor credentials
	// authenticate against — otherwise the pull 401s.
	harborReg, err := harborRegistryForTask(payload, creds.Harbor)
	if err != nil {
		return nil, fmt.Errorf("harbor credentials: %w", err)
	}
	minioClientImage := sidecarImageRef(harborReg)
	// 1665v (CE Gap Z, phase 1) — under the CE no-registry posture the
	// staging legs (prepareDataVolume / download / readResultJSON / upload)
	// need only a shell + tar, so they run on the alpine helper
	// checkpoint_transfer.go already uses; the AGPL mc client is NOT
	// pulled at all there (offline it 401s from Hub and blocks CE
	// stranger-boot C6). With a Harbor configured this stays the minio-client
	// ref byte-for-byte.
	cePosture := noRegistryPosture(creds.Harbor)
	ceStagingImage := ceStagingHelper(creds.Harbor, harborReg)
	// (1665w: no minio-client bookkeeping under the posture - the ref is empty there
	// and every leg below is posture-branched; ceStagingImage is the volume shell.)

	// Step 1: Create ephemeral volume
	volName, err := e.createVolume(pipelineCtx, payload.NodeKind)
	if err != nil {
		return nil, fmt.Errorf("create volume: %w", err)
	}
	defer e.cleanupVolume(volName)

	e.log.Info("created ephemeral volume", "volume", volName)

	// Pull minio-client sidecar image. Used for three things downstream:
	//   1. prepareDataVolume (always — needed for ANY processing container
	//      that runs as a non-root USER, which is most wizard-built nodes)
	//   2. downloadSidecar (when there's an input file to fetch)
	//   3. uploadSidecar (when outputs need to land in MinIO)
	// System tasks that skip BOTH download and upload (benchmarks, trivy)
	// historically didn't pull this — but the prep step needs it now, so
	// we pull unconditionally. The image is small (~20MB) and cached after
	// the first task on a fresh worker.
	// 1665v: under the CE posture this pull is SKIPPED — the staging legs run
	// on the alpine helper instead (pulled below), and the mc client only
	// (1665w: there is no lazy pull any more - the scan-staging, cleanup and
	// checkpoint-export legs are in process under the posture, so the mc client
	// is never pulled there at all. The Harbor/SaaS pull is unchanged, verbatim.)
	cePullErr := error(nil)
	if cePosture {
		// (CE: pull the alpine tar helper — the same helper checkpoint_transfer.go
		// already uses — and NOT the AGPL mc client, which offline 401s from
		// Hub and blocked every CE task before a node ran)
		cePullErr = e.retryOp(pipelineCtx, 3, "pull-staging-helper", func() error {
			return e.pullImage(pipelineCtx, ceStagingImage, creds.Harbor, "")
		})
		if cePullErr != nil {
			cePullErr = fmt.Errorf("pull staging helper: %w", cePullErr)
		}
	} else {
		// (Harbor/SaaS: the eager minio-client pull, verbatim — ceStagingImage
		// equals minioClientImage in this posture, so every leg below sees the
		// same image it always did)
		cePullErr = e.retryOp(pipelineCtx, 3, "pull-minio-client", func() error {
			return e.pullImage(pipelineCtx, minioClientImage, creds.Harbor, "")
		})
		if cePullErr != nil {
			cePullErr = fmt.Errorf("pull minio-client: %w", cePullErr)
		}
	}
	if cePullErr != nil {
		return nil, cePullErr
	}
	// (1665w: ceNeedSidecar is GONE - no path under the CE posture needs the
	// minio-client any more: scan-staging, cleanup and checkpoint-export staging all
	// run in process (ce_inprocess_staging.go), and sidecarImageRef renders the empty
	// ref under the posture, so nothing can pull a Hub image there.)

	// Step 1.5: Seed /data with the input/output dirs and chmod 0777 so
	// non-root processing containers can write. Runs unconditionally —
	// failure here is loud and propagates (vs piggy-backing on
	// downloadSidecar, which is skipped on SkipDownload tasks and was
	// failing silently in stage on user-image tasks).
	if err := e.retryOp(pipelineCtx, 3, "prepare-data", func() error {
		// 1665v: the prep leg needs only a shell (mkdir/chmod) — under the
		// CE posture it runs on the alpine helper, so no mc image is involved.
		// The Harbor branch keeps minioClientImage verbatim.
		return e.prepareDataVolume(pipelineCtx, ceStagingImage, volName, creds.MinIO)
	}); err != nil {
		return sidecarErrorResult(err), fmt.Errorf("prepare data volume: %w", err)
	}

	// Step 2: Download input from MinIO via sidecar (skip for export containers
	// and system tasks with skip_download=true)
	// InputFiles is checked too: the server still sets SourceFileLocation from
	// cog[0] today, but this must not depend on that. A task carrying inputs
	// and no single source is a task with inputs.
	if (payload.SourceFileLocation != "" || len(payload.InputFiles) > 0) && !payload.SkipDownload {
		e.log.Info("downloading input",
			"hash", truncHash(payload.SourceFileLocation),
			"bucket", payload.MinioBucket,
			"endpoint", creds.MinIO.Endpoint,
			"has_access", creds.MinIO.AccessKey != "",
			"has_mchost", creds.MinIO.MCHost != "",
		)
		if err := e.retryOp(pipelineCtx, 3, "download", func() error {
			// 1665v: under the CE posture the input leg is in process (minio-go
			// + sha256-verify, then tar into /data/input through the alpine helper).
			// StageNativeInputs reproduces buildDownloadCmd naming exactly: single
			// file input_<nodeKind><ext>, multi <cogId><ext>. Harbor branch verbatim.
			if cePosture {
				return e.ceDownloadInProcess(pipelineCtx, ceStagingImage, volName, creds, payload)
			}
			return e.downloadSidecar(pipelineCtx, minioClientImage, volName, creds, payload)
		}); err != nil {
			return sidecarErrorResult(err), fmt.Errorf("download: %w", err)
		}
	} else {
		e.log.Info("skipping download (no source file)")
	}

	// Step 2.5 (sz-tcp.16b): size-probe the downloaded inputs before the node
	// runs, so tiered credit pricing knows the real family/size instead of the
	// medium-tier fallback. Cached in Redis (24h TTL) keyed by the input digests.
	// A probe failure is NEVER a task failure — SizeProbe returns nil on every
	// error path.
	if doc := SizeProbe(pipelineCtx, task, creds, e.network, e.localRegistry, volName, e.docker); doc != nil {
		e.log.Info("size-probe ok", "task", task.ID, "probes", len(doc.Probes))
	}

	// Step 3: Pull processing image
	// Retry up to 3 times — Harbor may bounce during server update
	processingImage := e.resolveProcessingImage(payload, harborReg)
	pullCreds := PickRegistryAuth(processingImage, creds.Harbor, creds.TenantHarbor)
	e.log.Info("pulling image",
		"image", processingImage,
		"registry_kind", registryKindFor(processingImage, creds.Harbor, creds.TenantHarbor),
	)
	if err := e.retryOp(pipelineCtx, 3, "pull-image", func() error {
		return e.pullImage(pipelineCtx, processingImage, pullCreds, payload.ImageDigest)
	}); err != nil {
		return nil, fmt.Errorf("pull image: %w", err)
	}

	// B119: capture the actual digest of the image that was just pulled
	// (or kept locally) so the result carries what was truly executed.
	// 1900Y1g: this closure runs MID-BODY, while Run's named *Result return
	// is still nil — writing res.ExecutedImageDigest here was a nil-pointer
	// write (SIGSEGV at offset 0x78) that killed the worker right after a
	// prefer-local cache hit. The digest goes to the local and the Run defer
	// copies it onto whichever Result Run actually returns.
	_ = e.retryOp(pipelineCtx, 1, "inspect-image", func() error {
		if d := e.noteExecutedImageDigest(pipelineCtx, processingImage); d != "" {
			executedImageDigest = d
		}
		return nil
	})

	// Step 3.9 (FX.80.p2): scrub the cards this job is about to land on when the
	// card's last tenant differs from this job's tenant (hosted-cloud isolation,
	// owner decision Q63=A). Runs AFTER the image pull and immediately BEFORE the
	// processing container is created, so a refusal happens before anything
	// tenant-facing touches the card. Fail-closed: an error here returns and the
	// API retries/reschedules the task rather than running it on a dirty card.
	if scrubErr := e.scrubForTask(pipelineCtx, task); scrubErr != nil {
		return nil, fmt.Errorf("gpu tenant scrub: %w", scrubErr)
	}

	// Step 4: Run processing container
	// Pass MinIO credentials for containers that need direct MinIO access (zip-download, export)
	e.log.Info("running container", "image", processingImage, "timeout", timeout)
	logTail, exitCode, oomKilled, err := e.runContainerWithCredsLabel(pipelineCtx, task.ID, processingImage, volName, payload, timeout, &creds.MinIO, &creds.Harbor, creds.ModelCacheToken, cb, &egress, &ephApplied, &lockdownOut)
	if err != nil {
		return nil, fmt.Errorf("run container: %w", err)
	}
	if exitCode != 0 {
		// Return the result with logs so the failure report includes container output.
		// An OOM kill is called out explicitly: exit 137 alone is ambiguous, and the
		// manager needs to distinguish "this host ran out of memory" (retry
		// elsewhere) from "this code crashed" (retrying anywhere is pointless).
		msg := fmt.Errorf("container exited with code %d", exitCode)
		if oomKilled {
			msg = fmt.Errorf("container OOM-killed by the kernel (exit %d): "+
				"the host ran out of memory for this task", exitCode)
		}
		return &Result{
			LogTail:          logTail,
			Duration:         time.Since(started),
			OOMKilled:        oomKilled,
			EphemeralShmMB:   ephApplied.ShmMB,
			EphemeralTmpfsMB: ephApplied.TmpfsMB,
			EphemeralClamped: ephApplied.Clamped,
		}, msg
	}
	e.log.Info("container completed successfully")

	// 985b — checkpoint export mirror of Gate 3's hydrate path
	// (HydrateCheckpointVolume runs BEFORE the container; this runs AFTER it
	// succeeds). Both CheckpointExportURL and CheckpointVolume are set
	// together, only for a loaned node run's borrower-side attempt (985c
	// stamps them) — empty means "nothing to export", every task today. Goes
	// through the SAME pre-PUT scan consult the output path uses, so a
	// checkpoint export can never bypass the output-scan posture the result
	// JSON and bucket-upload paths already honour.
	if payload.CheckpointExportURL != "" && payload.CheckpointVolume != "" {
		// 1665w: no sidecar pull here - the export staging leg runs in process
		// under the CE posture (ce_inprocess_staging.go: alpine tar + minio-go
		// PutObject at the same fixed key), on the helper already pulled at
		// volume-create. Harbor posture: ceStagingImage IS minioClientImage.
		ceExportImage := ceStagingImage
		if blocked, reason := e.exportCheckpointWithScan(pipelineCtx, task, creds,
			ceExportImage, payload.CheckpointVolume, payload.CheckpointExportURL); blocked {
			e.log.Warn("checkpoint export refused by output scan",
				"task", task.ID, "reason", reason)
			return &Result{
				LogTail:          logTail,
				Duration:         time.Since(started),
				ErrorDetail:      reason,
				EphemeralShmMB:   ephApplied.ShmMB,
				EphemeralTmpfsMB: ephApplied.TmpfsMB,
				EphemeralClamped: ephApplied.Clamped,
			}, fmt.Errorf("%s", reason)
		}
	}

	// Step 5: Check for JSON result file (input-validator, export containers write result JSON)
	// If found, use its outputs directly instead of uploading files.
	// Use the minio-client image (already pulled) to read the file — alpine:3.19
	// was unreliable on remote workers where it wasn't cached, causing readResultJSON
	// to silently fail and the upload sidecar to capture the raw JSON file as output.
	// 1665v: this leg only cats a file — a shell is enough, so under the CE
	// posture it runs on the alpine helper (already pulled above). Under a
	// Harbor posture ceStagingImage IS minioClientImage: byte-for-byte the
	// old call.
	resultJSON := e.readResultJSON(pipelineCtx, volName, ceStagingImage)
	// 30.6.11 c11-J3 — the SAME helper container + volume this leg already uses
	// (readResultJSONFile, :1780) also reads result.json's token metering, so
	// the stack-model-call node's usage rides the completion body. Read
	// INDEPENDENTLY of resultJSON: stack-model-call writes result.json, which
	// readResultJSON deliberately does not look at (its filename list is
	// validation_result.json / export_result.json), so its outputs still come
	// from the ordinary upload path below while its usage still lands.
	// Never fails a task: nil on every failure mode.
	usage := e.readUsageJSON(pipelineCtx, volName, ceStagingImage)
	if resultJSON != nil {
		// Fortress 3.5 — scan BEFORE the inline result becomes durable. /complete
		// writes task.result_payload + node_run.outputs from exactly this return,
		// so a block must return NO outputs: the verdict is the API's, the refusal
		// is the worker's, and nothing persists on either side. The checkpoint
		// volume is untouched (Gate 3 already ran inside runContainerWithCreds).
		if blocked, reason := e.scanOutputBeforeStore(pipelineCtx, task, resultJSON, false); blocked {
			e.log.Warn("output scan refused the durable result write",
				"task", task.ID, "reason", reason, "outputs", len(resultJSON))
			return &Result{
				LogTail:          logTail,
				Duration:         time.Since(started),
				ErrorDetail:      reason,
				EphemeralShmMB:   ephApplied.ShmMB,
				EphemeralTmpfsMB: ephApplied.TmpfsMB,
				EphemeralClamped: ephApplied.Clamped,
			}, fmt.Errorf("%s", reason)
		}
		e.log.Info("using result JSON outputs", "output_count", len(resultJSON))
		return &Result{
			Outputs:          resultJSON,
			LogTail:          logTail,
			Duration:         time.Since(started),
			EphemeralShmMB:   ephApplied.ShmMB,
			EphemeralTmpfsMB: ephApplied.TmpfsMB,
			EphemeralClamped: ephApplied.Clamped,
			UsageTokens:      usage,
		}, nil
	}

	// Step 6: Upload outputs via sidecar (for containers that write real files to /data/output/)
	// Skip entirely when the task says SkipUpload (benchmarks, trivy scans).
	if payload.SkipUpload {
		e.log.Info("skipping upload (skip_upload=true)")
		return &Result{
			LogTail:          logTail,
			Duration:         time.Since(started),
			EphemeralShmMB:   ephApplied.ShmMB,
			EphemeralTmpfsMB: ephApplied.TmpfsMB,
			EphemeralClamped: ephApplied.Clamped,
			UsageTokens:      usage,
		}, nil
	}
	// Fortress 3.5 — scan BEFORE the durable storage write. uploadSidecar is
	// where the bytes land in the tenant bucket, so this is the only point at
	// which "before durable storage write" is literally satisfiable worker-side
	// (design_fortress_35 §1 step 2). A block refuses the upload: the object is
	// never written (nothing is deleted — ISO 27001 soft-delete posture), the
	// task fails with reason output_scan_blocked, and the API's /failed path
	// fails the NodeRun/WorkflowRun exactly as an ordinary terminal failure.
	// The verdict itself is the API's (services/task_output_scan.apply_task_output_scan
	// via the consult endpoint); the worker never re-derives it in Go.
	// uploadPending=true: the bucket objects do not exist yet, so the API answers
	// from the verdict it would have produced at /complete without registering.
	// The checkpoint volume is untouched: Gate 3 ran earlier, inside
	// runContainerWithCredsInternal, so a refusal keeps every checkpoint.
	//
	// Fortress 3.5 staging pass: the consult used to carry outputs=[] because
	// the bucket objects do not exist yet, so the API could only scan metadata.
	// The worker now ALSO stages the same bytes under the transient
	// `scan-staging/{task_id}/` prefix (same tenant bucket, same creds, same
	// minio-client sidecar, no new env vars) and sends per-output staging_key
	// descriptors, which task_output_scan.py:_validate_staging_key accepts and
	// _scan_staged_bytes fetches. Staging is BEST-EFFORT: a failed stage logs
	// and degrades to the pre-existing outputs=[] metadata-only consult — it
	// must never fail the task, and it is never retried (retryOp would double
	// the sidecar cost on the hot completion path).
	//
	// payload.SkipUpload already returned above, so this site is by definition
	// the upload path; the guard is kept for readability against the brief.
	// The Step-5 inline call (:1194) stays as-is — there are no files there.
	// Cleanup of the staged blobs happens at the END of this same sidecar run
	// (stageOutputsForScan appends `mc rm -r ... || true`), so nothing durable
	// is left behind: these objects are transient bytes referenced by NO DB
	// record, so removing them is not a record delete (ISO 27001 posture).
	// Crash-orphan note: if the sidecar dies mid-run, orphaned scan-staging/
	// objects are unreferenced bytes; the wizard-style API sweep
	// (wizard_test_run_cleanup.py shape) is the follow-up, NOT implemented
	// here.
	//
	// Docker path only: native_staging.go / k8s_staging.go / loan_staging.go
	// staging paths are untouched this pass.
	//
	// ORDERING: cleanup of the staged blobs must happen strictly AFTER the
	// consult below has fetched them. It is a SEPARATE sidecar invocation
	// (cleanupStagedOutputs), deferred from here, so every return path out of
	// Run() — the blocked early-return right after the consult, and the final
	// success return after the real durable upload — only runs it once this
	// function is on its way out, i.e. after the consult call has completed.
	// It must NOT be appended to the staging upload command: that would delete
	// the objects in the same sidecar run that created them, before the
	// consult (an HTTP call made AFTER this sidecar returns) ever fetches them.
	stagedOutputs := []transport.OutputFile{}
	if !payload.SkipUpload {
		// 1665w: no sidecar pull here either - the scan-staging pass and its
		// cleanup run in process under the CE posture (minio-go PutObject/Remove
		// Object over the same scan-staging/{task_id}/ prefix), on the helper
		// already pulled at volume-create. Harbor posture: ceStagingImage IS
		// minioClientImage, so both closures below are the verbatim sidecar pair.
		ceStageImage := ceStagingImage
		ceStageScan := e.stageOutputsForScan
		ceStageClean := func() error {
			return e.cleanupStagedOutputs(pipelineCtx, ceStageImage, volName, creds, task)
		}
		if cePosture {
			ceStageScan = e.ceStageOutputsForScan
			ceStageClean = func() error {
				return ceCleanupStagedScanOutputs(pipelineCtx, task, creds)
			}
		}
		if staged, stageErr := ceStageScan(pipelineCtx, ceStageImage, volName, creds, task); stageErr != nil {
			e.log.Warn("output-scan staging sidecar failed; consult falls back to metadata-only",
				"task", task.ID, "error", stageErr)
		} else {
			stagedOutputs = staged
			defer func() {
				if cleanupErr := ceStageClean(); cleanupErr != nil {
					e.log.Warn("scan-staging cleanup sidecar failed; orphaned blobs need the API-side sweep",
						"task", task.ID, "error", cleanupErr)
				}
			}()
		}
	}
	if blocked, reason := e.scanOutputBeforeStore(pipelineCtx, task,
		stagedOutputs, true); blocked {
		e.log.Warn("output scan refused the durable bucket write",
			"task", task.ID, "reason", reason, "bucket", payload.MinioBucket)
		return &Result{
			LogTail:          logTail,
			Duration:         time.Since(started),
			ErrorDetail:      reason,
			EphemeralShmMB:   ephApplied.ShmMB,
			EphemeralTmpfsMB: ephApplied.TmpfsMB,
			EphemeralClamped: ephApplied.Clamped,
		}, fmt.Errorf("%s", reason)
	}
	// Retry up to 3 times — MinIO may bounce during server update
	e.log.Info("uploading outputs", "bucket", payload.MinioBucket)
	var outputs []transport.OutputFile
	if err := e.retryOp(pipelineCtx, 3, "upload", func() error {
		var uploadErr error
		// 1665v: the durable upload is in process under the CE posture —
		// UploadNativeOutputs renders the SAME <prefix><hash> keys (wizard
		// OutputPrefix included via uploadScriptParamsFromTask) and scanOutputDir
		// enforces the SandboxMaxOutputMB cap against the same CapBytes BEFORE any
		// object is written, so both sidecar guarantees hold. Harbor branch verbatim.
		if cePosture {
			outputs, uploadErr = e.ceUploadInProcess(pipelineCtx, ceStagingImage, volName, creds, task)
		} else {
			outputs, uploadErr = e.uploadSidecar(pipelineCtx, minioClientImage, volName, creds, task)
		}
		return uploadErr
	}); err != nil {
		return sidecarErrorResult(err), fmt.Errorf("upload: %w", err)
	}

	return &Result{
		Outputs:          outputs,
		LogTail:          logTail,
		Duration:         time.Since(started),
		EphemeralShmMB:   ephApplied.ShmMB,
		EphemeralTmpfsMB: ephApplied.TmpfsMB,
		EphemeralClamped: ephApplied.Clamped,
		UsageTokens:      usage,
	}, nil
}

// outputScanModeTunableKey is the platform_settings registry key the API's
// scan helper registers (services/task_output_scan.py:44, group compliance,
// off|shadow|enforce, default shadow). Fortress 3.5 reads the SAME key the
// API owns through the heartbeat tunables map: no worker env var is invented
// (the store's envVar arg stays empty), so an untouched fleet reads the API's
// own default, shadow = the fail-open launch posture.
const outputScanModeTunableKey = "fortress.task_output_scan_mode"

// outputScanPathFmt is the API consult the worker POSTs BEFORE anything
// durable is written: the MinIO bucket upload (Run() Step 6 / uploadSidecar)
// and the inline result-JSON return (Run() Step 5). The endpoint is the API
// slice's job (it wraps services/task_output_scan.apply_task_output_scan,
// whose verdict today only runs INSIDE /complete). A 404 from an API that
// has not landed it yet is a "no verdict": fail-open under shadow/off,
// refused only under enforce. Body: {tenant_id, node_kind, minio_bucket,
// upload_pending, outputs[]}. Response: the verdict dict
// {mode, blocked, exempted?, findings[], error?}.
const outputScanPathFmt = "/workers/tasks/%d/output-scan-check"

// outputScanBlockedReason is the failure reason a refused durable write
// carries, so the manager sees WHY (a scan verdict), not a crash.
const outputScanBlockedReason = "output_scan_blocked"

// outputScanFindingLimit caps the finding codes named in a refusal reason
// (the API's own finding_codes() helper caps at 10 and masks details).
const outputScanFindingLimit = 10

// outputScanHTTPTimeout bounds the single pre-store consult hop. The caller's
// ctx (pipelineCtx) frequently has NO deadline (outerCap defaults to 0 =
// unlimited), so this is the only thing standing between a hung/slow API and
// a task that never finishes. A 404 today (no endpoint yet) resolves in
// milliseconds; this cap only matters once the endpoint exists.
const outputScanHTTPTimeout = 3 * time.Second

// outputScanSetAuth is the auth seam main.go wires with the same
// X-Worker-Name / X-Bootstrap-Token headers every other worker->API call
// carries (internal/transport/api.go:850 setHeaders). It defaults to nil,
// which means the consult is unauthenticated and the API 401s: that is a
// "no verdict", so the posture decides (shadow allows, enforce refuses).
// Nil-by-default is deliberate: the executor never invents a secret.
var outputScanSetAuth func(*http.Request)

// SetOutputScanAuth installs the auth callback the executor calls on every
// output-scan consult request. nil restores the pre-907 behaviour (no auth
// headers, i.e. unauthenticated consult).
func SetOutputScanAuth(fn func(*http.Request)) {
	outputScanSetAuth = fn
}

// outputScanHTTPDo is the HTTP seam (same shape as hostexec_task.go's
// hostExecHTTPDo / checkpoint_transfer.go's checkpointHTTPDo) so a test can
// stand an httptest server for the consult instead of the real API.
var outputScanHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
	client := &http.Client{}
	return client.Do(req)
}

// OutputScanFinding is one verdict finding as the API words it. Only the
// masked fields are decoded; raw detail is never even parsed, so a finding's
// content cannot reach a worker log by accident.
type OutputScanFinding struct {
	Scanner  string `json:"scanner"`
	Category string `json:"category"`
	Severity string `json:"severity"`
}

// OutputScanVerdict is the API's verdict, parsed not interpreted: the worker
// never re-derives block/allow in Go (design_fortress_35 §2 rejects the Go
// scanner port as fork risk), it only reads the API's word.
type OutputScanVerdict struct {
	Mode     string              `json:"mode"`
	Block    bool                `json:"blocked"`
	Exempted bool                `json:"exempted"`
	Error    bool                `json:"error"`
	Findings []OutputScanFinding `json:"findings"`
}

// scanOutputBeforeStore asks the API for a block/allow verdict on this task's
// outputs BEFORE the worker writes anything durable. Returns (blocked, reason)
// with reason "" unless blocked.
//
// Posture is the API's own settings, never a worker-side flag:
//
//	off     - no call at all, allow;
//	shadow  - call, honour the verdict (the API returns blocked=false), and a
//	          verdict-less consult (unreachable / 404 / 401 / 500) also allows;
//	enforce - honour blocked=true, and a verdict-less consult REFUSES,
//	          mirroring task_output_scan.py's scanner-blowup-under-enforce
//	          fail-closed posture.
//
// The checkpoint path is never blocked: Gate 3 (the durable checkpoint volume)
// already ran inside runContainerWithCredsInternal before this call, so a
// refusal keeps every checkpoint and only refuses the RESULT store. Nothing is
// deleted: a refused object is simply never written (ISO 27001 - no hard
// delete on any path here).
func (e *Executor) scanOutputBeforeStore(ctx context.Context, task *transport.Task,
	inlineOutputs []transport.OutputFile, uploadPending bool) (bool, string) {
	// Posture first: off means "do not scan at all" - zero network hops, so an
	// untouched fleet pays nothing on the completion path.
	mode := e.tunables.String(outputScanModeTunableKey, "", "shadow",
		"off", "shadow", "enforce")
	if mode == "off" {
		return false, ""
	}
	base := strings.TrimRight(strings.TrimSpace(e.apiURL), "/")
	if base == "" {
		// No API URL to consult: fail-open unless the tenant chose enforce.
		if mode == "enforce" {
			return true, outputScanBlockedReason + ": no API endpoint to consult"
		}
		return false, ""
	}
	// The consult carries only what the API needs to re-run its own scanner:
	// identity + the output descriptors. No file bytes cross the wire - the
	// API fetches bytes itself (output_safety_scan_async._fetch_bytes), which
	// keeps one definition of the scanner and no second copy of the payload.
	// upload_pending tells the API this is the PRE-store consult (the bucket
	// write has not happened yet), so it answers from the verdict it would
	// have produced at /complete without registering anything.
	// outputs is [] for the file-upload case (the bucket objects do not exist
	// yet) and the inline result JSON's OutputFile list for Step 5.
	// tenant_id/node_kind are what the per-tenant, per-workflow scanner config
	// (compliance_output_scanners, fortress 3.7) resolves. minio_bucket is the
	// object prefix context the async path keys its verdict rows on.
	body, err := json.Marshal(map[string]interface{}{
		"tenant_id":      task.TenantID,
		"node_kind":      task.Payload.NodeKind,
		"minio_bucket":   task.Payload.MinioBucket,
		"upload_pending": uploadPending,
		"outputs":        inlineOutputs,
	})
	if err != nil {
		// A serialisation blow-up is the scanner-blowup shape: fail-closed under
		// enforce, fail-open otherwise (same mirror as the unreachable-API case).
		if mode == "enforce" {
			return true, outputScanBlockedReason + ": verdict request build failed"
		}
		return false, ""
	}
	// The consult must never stall the completion path: pipelineCtx (the
	// caller's ctx) is often unbounded (outerCap defaults to 0 = no deadline),
	// so a hung API would otherwise hang the task forever. Bound this single
	// hop independently of the caller's deadline.
	reqCtx, cancel := context.WithTimeout(ctx, outputScanHTTPTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost,
		base+fmt.Sprintf(outputScanPathFmt, task.ID), bytes.NewReader(body))
	if err != nil {
		if mode == "enforce" {
			return true, outputScanBlockedReason + ": verdict request failed"
		}
		return false, ""
	}
	req.Header.Set("Content-Type", "application/json")
	if outputScanSetAuth != nil {
		outputScanSetAuth(req)
	}
	resp, err := outputScanHTTPDo(ctx, req)
	if err != nil {
		if mode == "enforce" {
			return true, fmt.Sprintf("%s: API unreachable (%v)", outputScanBlockedReason, err)
		}
		return false, ""
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		// 404 = the API has not landed the consult yet; 401 = the auth seam was
		// never wired. Both are "no verdict", and the posture decides.
		if mode == "enforce" {
			return true, fmt.Sprintf("%s: no verdict from API (http %d)",
				outputScanBlockedReason, resp.StatusCode)
		}
		return false, ""
	}
	var verdict OutputScanVerdict
	if err := json.NewDecoder(resp.Body).Decode(&verdict); err != nil {
		if mode == "enforce" {
			return true, outputScanBlockedReason + ": verdict undecodable"
		}
		return false, ""
	}
	// The API is authoritative, and its OWN posture field is what decides
	// fail-open (the brief's "fail-open ONLY when the API reports shadow/off
	// posture"): a verdict that reports shadow/off is never a refusal even if
	// some other writer set blocked, because that block is not the tenant's
	// compliance posture. An absent mode field means the API did not report
	// one, so the worker's own read of the SAME registry key decides - the two
	// are the same setting, so they cannot disagree on a live install.
	posture := verdict.Mode
	if posture == "" {
		posture = mode
	}
	if posture == "shadow" || posture == "off" {
		return false, ""
	}
	if !verdict.Block {
		return false, ""
	}

	codes := make([]string, 0, outputScanFindingLimit)
	for i, f := range verdict.Findings {
		if i >= outputScanFindingLimit {
			break
		}
		if f.Category != "" {
			codes = append(codes, f.Category)
		}
	}
	reason := outputScanBlockedReason
	if len(codes) > 0 {
		joined := strings.Join(codes, ",")
		if len(joined) > 500 {
			joined = joined[:500]
		}
		reason += ": " + joined
	}
	return true, reason
}

// NativeScanConsult is the exported seam 943's native path calls through
// executor.SetNativeScanConsult (wired once in main.go next to
// SetOutputScanAuth): the native backend has no Executor of its own, but the
// posture decision (platform_settings mode, the API consult, the verdict
// parsing) must stay a single implementation, not a second Go copy. This is
// a thin exported wrapper around the unexported scanOutputBeforeStore —
// same tunables, same apiURL, same outputScanSetAuth hook, same refusal
// reason (outputScanBlockedReason) — so the native and Docker paths can
// never disagree on a live install.
func (e *Executor) NativeScanConsult(ctx context.Context, task *transport.Task,
	staged []transport.OutputFile, uploadPending bool) (bool, string) {
	return e.scanOutputBeforeStore(ctx, task, staged, uploadPending)
}

// resultJSONReadTimeout caps the result-file helper. A flat cap is correct
// HERE, unlike the mc sidecars: this container only `cat`s a small JSON file,
// so there is no legitimate long-running case to protect.
const resultJSONReadTimeout = 2 * time.Minute

// readResultJSONFile runs one helper container to cat a single result file and
// returns its trimmed stdout ("" on any failure).
//
// Extracted from readResultJSON so teardown can be deferred per attempt. The
// previous inline version removed the container on the line AFTER an unbounded
// ContainerWait, so a wait that never returned skipped cleanup entirely and
// leaked a running container — the same defect as the mc sidecars, on a path
// that bypasses runSidecarWithOutput and so was missed the first time.
func (e *Executor) readResultJSONFile(ctx context.Context, volName, helperImage, filename string) string {
	cmd := fmt.Sprintf(`cat /data/output/%s 2>/dev/null || echo ""`, filename)

	resp, err := e.docker.ContainerCreate(ctx,
		&container.Config{
			Image:      helperImage,
			Entrypoint: []string{"/bin/sh"},
			Cmd:        []string{"-c", cmd},
			Labels:     e.sidecarLabels(),
		},
		&container.HostConfig{
			Binds: []string{volName + ":/data:ro"},
		},
		nil, nil, "",
	)
	if err != nil {
		return ""
	}

	// Background ctx so teardown still runs when the task ctx is already
	// canceled — a remove on a dead ctx fails, which is its own leak.
	defer func() {
		rmCtx, rmCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer rmCancel()
		e.docker.ContainerRemove(rmCtx, resp.ID, container.RemoveOptions{Force: true})
	}()

	if err := e.docker.ContainerStart(ctx, resp.ID, container.StartOptions{}); err != nil {
		return ""
	}

	waitCtx, waitCancel := context.WithTimeout(ctx, resultJSONReadTimeout)
	defer waitCancel()
	statusCh, errCh := e.docker.ContainerWait(waitCtx, resp.ID, container.WaitConditionNotRunning)
	select {
	case <-errCh:
	case <-statusCh:
	}

	var stdout, stderr bytes.Buffer
	logReader, err := e.docker.ContainerLogs(ctx, resp.ID, container.LogsOptions{ShowStdout: true})
	if err == nil {
		stdcopy.StdCopy(&stdout, &stderr, logReader)
		logReader.Close()
	}
	return strings.TrimSpace(stdout.String())
}

// readResultJSON checks if the container wrote a JSON result file to /data/output/.
// Input-validator writes validation_result.json, export writes export_result.json.
// If found, reads the "outputs" array from the JSON and returns them as OutputFiles.
//
// helperImage must be an image already pulled on this worker (e.g. the
// minio-client sidecar). Using an unpulled image like alpine:3.19 caused
// silent failures on remote workers where that image wasn't cached.
func (e *Executor) readResultJSON(ctx context.Context, volName string, helperImage string) []transport.OutputFile {
	if helperImage == "" {
		helperImage = "alpine:3.20"
	}
	// Try both known result file names
	for _, filename := range []string{"validation_result.json", "export_result.json"} {
		content := e.readResultJSONFile(ctx, volName, helperImage, filename)
		if content == "" {
			continue
		}

		// Parse JSON result
		var result struct {
			Outputs []struct {
				ContentHash   string `json:"content_hash"`
				FileName      string `json:"file_name"`
				FileExtension string `json:"file_extension"`
				SizeBytes     int64  `json:"size_bytes"`
				Location      string `json:"location"`
				FilePath      string `json:"file_path"`
				CogID         int    `json:"cog_id"`
			} `json:"outputs"`
		}

		if err := json.Unmarshal([]byte(content), &result); err != nil {
			e.log.Debug("result JSON parse failed", "file", filename, "error", err)
			continue
		}

		if len(result.Outputs) == 0 {
			continue
		}

		// Convert to transport.OutputFile
		var outputs []transport.OutputFile
		for _, o := range result.Outputs {
			hash := o.ContentHash
			if hash == "" && o.Location != "" {
				// Extract hash from location: minio://bucket/hash
				parts := strings.Split(o.Location, "/")
				hash = parts[len(parts)-1]
			}
			outputs = append(outputs, transport.OutputFile{
				ContentHash:   hash,
				FileName:      o.FileName,
				FileExtension: o.FileExtension,
				SizeBytes:     o.SizeBytes,
			})
		}

		e.log.Info("read result JSON", "file", filename, "outputs", len(outputs))
		return outputs
	}

	return nil
}

// ============================================================================
// 30.6.11 c11-J3 — stack-model-call token usage (30.6.11)
// ============================================================================

const usageJSONMaxBytes = 64 * 1024

// usageTokenMax is the S9 upper bound on a single token count (the same 1e9 the
// API's S8 Pydantic bound enforces); negative / larger values drop the field.
const usageTokenMax = int64(1_000_000_000)

// usageInt is the S9 integer gate: json.Unmarshal decodes a JSON whole number
// into an `any` field as float64, so int64/float64 are both accepted (a
// fractional float is rejected outright - a token count is never a fraction),
// and anything outside 0..1e9 drops THAT field, the rest of the usage still
// riding (the API's "422 for that field only" rule, enforced worker-side so
// the API never sees the bad value). -1 means "no usable value".
func usageInt(v any) int64 {
	switch x := v.(type) {
	case int64:
		if x < 0 || x > usageTokenMax {
			return -1
		}
		return x
	case int:
		if x < 0 || int64(x) > usageTokenMax {
			return -1
		}
		return int64(x)
	case float64:
		if x < 0 || x > float64(int64(usageTokenMax)) {
			return -1
		}
		if x-float64(int64(x)) != 0 {
			return -1
		}
		return int64(x)
	default:
		return -1
	}
}

// usageJSONDoc / usageJSONObj are the decode shape for the result.json the
// stack-model-call entrypoint writes: only model + the four integer fields are
// decoded; every other key (finish_reason, and anything a tenant node plants)
// is never even looked at, so prompt/response text cannot ride the wire.
type usageJSONObj struct {
	PromptTokens     any `json:"prompt_tokens"`
	CompletionTokens any `json:"completion_tokens"`
	TotalTokens      any `json:"total_tokens"`
	Images           any `json:"images"`
}

type usageJSONDoc struct {
	Model string        `json:"model"`
	Usage *usageJSONObj `json:"usage"`
}

// usageModelNameOK is the worker-side twin of the API's S8 model-name rule:
// <=128 chars, [A-Za-z0-9._:/-] only. Hand-rolled (no regexp import) because the
// charset is tiny and a regex would let a tenant-chosen name drive backtracking.
func usageModelNameOK(name string) bool {
	if name == "" || len(name) > 128 {
		return false
	}
	for _, c := range []byte(name) {
		ok := (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
			c == '.' || c == '_' || c == ':' || c == '/' || c == '-'
		if !ok {
			return false
		}
	}
	return true
}

// parseUsageJSON is the pure (no docker, no ctx) half of readUsageJSON, so the
// S9 bounds are unit-testable without a container. The stack-model-call entrypoint
// (CORE/DockerContainers/stack-model-call/entrypoint.py:200-203) writes
// /data/output/result.json as {"model": str, "usage": <raw OpenAI usage>,
// "finish_reason": str}. ONLY the model name and the three token counts (plus an
// optional images int) are read; every other key - including anything a
// tenant-built node plants in result.json - is ignored, so prompt/response text
// can never ride the completion body (S3/S7 trust boundary, worker side).
//
// S9 bounds (the worker-side twin of the API's S8 bounds, enforced HERE):
//   - content over 64 KiB is ignored outright (a tenant node can write an
//     arbitrarily large result.json);
//   - a count that is not an integer in 0..1e9 drops THAT field, the rest still
//     rides (mirrors the API's "422 for that field only" rule);
//   - a model name over 128 chars or outside [A-Za-z0-9._:/-] drops the WHOLE
//     usage - the name is the entry's identity, without it the counts are
//     meaningless and would land on the wrong model row;
//   - a usage with no surviving count at all reports nil (nothing to say).
//
// nil means "no usage to report": the completion body then carries no
// usage_tokens key at all, which is byte-for-byte the old shape.
func parseUsageJSON(content []byte) *transport.UsageTokens {
	if len(content) == 0 || len(content) > usageJSONMaxBytes {
		return nil
	}

	var doc usageJSONDoc
	if err := json.Unmarshal(content, &doc); err != nil {
		return nil
	}
	if doc.Usage == nil || !usageModelNameOK(doc.Model) {
		return nil
	}

	entry := transport.UsageModel{Model: doc.Model}
	p := usageInt(doc.Usage.PromptTokens)
	c := usageInt(doc.Usage.CompletionTokens)
	t := usageInt(doc.Usage.TotalTokens)
	imgs := usageInt(doc.Usage.Images)
	if p >= 0 {
		entry.PromptTokens = p
	}
	if c >= 0 {
		entry.CompletionTokens = c
	}
	if t >= 0 {
		entry.TotalTokens = t
	}
	if imgs >= 0 {
		entry.Images = imgs
	}
	if p < 0 && c < 0 && t < 0 && imgs < 0 {
		// Nothing survived the S9 gate: there is no usage to report.
		return nil
	}

	return &transport.UsageTokens{Models: []transport.UsageModel{entry}}
}

// readUsageJSON reads the processing container's /data/output/result.json on the
// SAME helper container + volume readResultJSON already uses (readResultJSONFile,
// :1780) and returns the token metering for the completion body. It deliberately
// does NOT add result.json to readResultJSON's output filename list: outputs
// semantics are unchanged, so a stack-model-call node still reports its files
// through the ordinary upload path and result.json is never captured as an
// output. Never fails a task - every failure mode returns nil.
func (e *Executor) readUsageJSON(ctx context.Context, volName string, helperImage string) *transport.UsageTokens {
	if helperImage == "" {
		helperImage = "alpine:3.20"
	}
	content := e.readResultJSONFile(ctx, volName, helperImage, "result.json")
	if content == "" {
		return nil
	}
	usage := parseUsageJSON([]byte(content))
	if usage == nil {
		// Counts/bytes only - result.json can sit next to response.txt, so no
		// file content is ever echoed.
		e.log.Debug("result.json usage ignored", "file", "result.json", "bytes", len(content))
		return nil
	}
	m := usage.Models[0]
	e.log.Info("read result.json usage", "prompt_tokens", m.PromptTokens,
		"completion_tokens", m.CompletionTokens, "total_tokens", m.TotalTokens,
		"images", m.Images, "model_chars", len(m.Model))
	return usage
}

func (e *Executor) createVolume(ctx context.Context, dagID string) (string, error) {
	randBytes := make([]byte, 8)
	rand.Read(randBytes)
	name := taskVolumePrefix + sanitizeProject(dagID) + "-" + hex.EncodeToString(randBytes)

	_, err := e.docker.VolumeCreate(ctx, volume.CreateOptions{
		Name: name,
		// Stamped with the same worker/instance identity as sidecars so a
		// volume orphaned by a killed worker can be told apart from one an
		// in-flight task is still writing to. cleanupVolume remains the
		// happy path; these labels exist for when it cannot run.
		Labels: map[string]string{
			taskVolumeLabel:      "1",
			sidecarWorkerLabel:   e.workerName,
			sidecarInstanceLabel: e.instanceID,
		},
	})
	return name, err
}

// ensureCheckpointVolume creates the node run's durable volume if this host has
// not seen it before. Idempotent: a retry on the SAME host finds the existing
// volume and its contents, which is the case that makes checkpointing pay.
//
// A retry on a DIFFERENT host gets an empty one and starts over. That is a real
// limit and an honest one — the alternative is syncing volumes between hosts,
// which is a much larger promise than "your six-hour job survives a reboot".
func (e *Executor) ensureCheckpointVolume(ctx context.Context, name string) error {
	if _, err := e.docker.VolumeInspect(ctx, name); err == nil {
		return nil
	}
	_, err := e.docker.VolumeCreate(ctx, volume.CreateOptions{
		Name: name,
		Labels: map[string]string{
			checkpointVolumeLabel: "1",
			// Belt and braces. The prune is already scoped positively to the
			// task prefix, which a checkpoint volume does not carry — but the
			// keep label is the mechanism that exists for "never reap this",
			// and relying on a name not matching is weaker than saying so.
			"com.example.mod-core.keep": "1",
			sidecarWorkerLabel:              e.workerName,
			sidecarInstanceLabel:            e.instanceID,
		},
	})
	return err
}

// maxResumeCheckpointEnvBytes caps MOD_RESUME_CHECKPOINT. The API caps the blob
// it stamps, but the worker re-checks: a payload that grew past the cap
// (an older API, a hand-edited requeue) must never be able to push the node
// container's env past the kernel's per-argument limit and fail the exec.
const maxResumeCheckpointEnvBytes = 32 * 1024

// sanitizeResumeValue strips the characters that would corrupt an env entry or
// fake a log line: NUL terminates the C string the kernel copies, CR/LF end a
// record in the worker's own log. Everything else passes through — the label is
// the node's own tag, the worker has no opinion about its alphabet.
func sanitizeResumeValue(s string) string {
	b := []byte(s)
	out := make([]byte, 0, len(b))
	for _, c := range b {
		if c == 0 || c == '\n' || c == '\r' {
			continue
		}
		out = append(out, c)
	}
	return string(out)
}

// resumeCheckpointEnv turns the reaper's resume stamp (EXPR 9.3, stamped by the
// API's _stamp_requeue_resume_checkpoint) into the env a node container sees.
// It is deliberately NOT inside the CheckpointVolume branch: a resume point the
// API recorded is useful on a host that never had the volume at all — that is
// the whole point of the stamp being in the payload instead of on disk.
//
// The payload blob is size-capped because env is a hard kernel limit: an
// argv/envp overflow fails the exec, which would turn "restart from where you
// left off" into "the node never starts". Oversize or malformed JSON is skipped
// and reported by SIZE ONLY — checkpoint payloads can carry node state, and the
// log is not the place for it.
func (e *Executor) resumeCheckpointEnv(payload transport.TaskPayload) []string {
	if payload.ResumeCheckpointID <= 0 {
		// No stamp: byte-identical env to the pre-EXPR world. A zero/negative
		// id is not a resume point, and half-stamping a node that has no idea
		// what it is resuming is worse than stamping nothing.
		return nil
	}
	env := []string{"MOD_RESUME_CHECKPOINT_ID=" + strconv.FormatInt(payload.ResumeCheckpointID, 10)}
	if label := sanitizeResumeValue(payload.ResumeCheckpointLabel); label != "" {
		env = append(env, "MOD_RESUME_CHECKPOINT_LABEL="+label)
	}
	// Progress is a pointer so 0.0 stays distinguishable from "not recorded":
	// a node that resumes at the start of a step it had already checkpointed
	// must not get the same env as one whose progress was never measured.
	if payload.ResumeCheckpointProgress != nil {
		env = append(env, "MOD_RESUME_CHECKPOINT_PROGRESS="+strconv.FormatFloat(*payload.ResumeCheckpointProgress, 'f', -1, 64))
	}
	// The blob is only forwarded when it is real JSON and small enough to be
	// env-safe. Compact, not pretty: the node parses it, and every byte of
	// indentation is env the kernel had to copy for nothing.
	if n := len(payload.ResumeCheckpointPayload); n > 0 {
		if !json.Valid(payload.ResumeCheckpointPayload) {
			// Size only, always. The payload is node state; the log is not its
			// place, and a malformed blob is not a resume point, so the node
			// starts from its own beginning — what it would do with no stamp.
			e.log.Warn("resume checkpoint payload is not valid JSON; skipping it",
				"bytes", n)
		} else if n > maxResumeCheckpointEnvBytes {
			// Same rule, different reason: valid, but too big to fit.
			e.log.Warn("resume checkpoint payload exceeds env size cap; skipping it",
				"bytes", n)
		} else {
			var buf bytes.Buffer
			if err := json.Compact(&buf, payload.ResumeCheckpointPayload); err != nil {
				// json.Valid passed above, so this is unreachable in practice;
				// if it ever fires, the node runs without the blob rather than
				// with a half-compacted one.
				e.log.Warn("resume checkpoint payload could not be compacted; skipping it",
					"bytes", n)
			} else {
				env = append(env, "MOD_RESUME_CHECKPOINT="+buf.String())
			}
		}
	}
	return env
}

func (e *Executor) cleanupVolume(name string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := e.docker.VolumeRemove(ctx, name, true); err != nil {
		e.log.Warn("failed to remove volume", "volume", name, "error", err)
	}
}

// prepareDataVolume seeds /data/input and /data/output on the freshly
// created task volume so the downstream processing container (which
// commonly runs as a non-root USER like "mod" via the wizard's
// Dockerfile) can mkdir into them. Without this the entrypoint's
// `mkdir -p /data/output` returns EACCES against the root:root 0755
// /data that Docker hands the bind mount, and the task fails before
// the user script gets a chance to run.
//
// Runs unconditionally after volume create — not piggy-backed on
// downloadSidecar, which is skipped for SkipDownload tasks AND was
// observed in stage to either no-op silently or not propagate its
// chmod (we never figured out which — the previous attempt to do
// this inside downloadSidecar's `&&` chain didn't fix the EACCES the
// user kept seeing in repeated runs). Doing it standalone with
// captured output makes the success/failure visible in the worker
// log feed.
//
// World-writable is safe: the volume is task-scoped and gets nuked
// after upload (cleanupVolume).
func (e *Executor) prepareDataVolume(ctx context.Context, sidecarImage, volName string, minioCreds transport.MinIOCredentials) error {
	cmd := `set -e; ` +
		`mkdir -p /data/input /data/output; ` +
		`chmod 0777 /data /data/input /data/output; ` +
		`echo "prep OK: $(ls -ld /data /data/input /data/output | tr '\n' ' | ')"`
	out, err := e.runSidecarWithOutput(ctx, sidecarImage, volName, cmd, minioCreds)
	if err != nil {
		return fmt.Errorf("prepare /data: %w", err)
	}
	e.log.Info("data volume prepared", "volume", volName, "output", strings.TrimSpace(out))
	return nil
}

// downloadSidecar runs a minio-client container to download the input file.
// Data stays inside the ephemeral volume — never touches the host filesystem.
// Permissions on /data are handled separately by prepareDataVolume; this
// sidecar only does the mc cp.
// buildDownloadCmd renders the mc command that stages a task's inputs.
//
// Pure, so the staging contract can be pinned by a test without a MinIO, a
// Docker daemon or a network. That matters more here than in most places: the
// bug this replaced was invisible precisely because nothing ever asserted what
// the sidecar copied — it staged one file out of 38 and every task still
// reported success.
//
// R-154 follow-up: the tenant-supplied extension (and the NodeKind in the
// single-file name) are allowlisted with the same helpers the native staging
// path uses; anything that would land a destination outside /data/input is
// rejected with a clear error and ZERO downloads staged.
func buildDownloadCmd(payload transport.TaskPayload) (string, error) {
	// mc resolves the `myminio` alias from the MC_HOST_myminio env var (a full
	// scheme://access:secret@host URL, set in runSidecarWithOutput). The old
	// `mc alias set myminio "$MINIO_ENDPOINT" ...` broke on versitygw: the
	// endpoint has no scheme ("Invalid URL versitygw:7070 ... should be
	// scheme://host") and a minted secret can start with '-' (mc's parser reads
	// it as a flag: "flag provided but not defined: -Ne8..."). MC_HOST avoids
	// both — no URL/secret positional args. `true` keeps the `&& mc cp` chain shape.
	alias := `true`

	// Multi-file staging. The single-file path below is unchanged and is still
	// what runs for every task the server does not mark as multi-file.
	//
	// Named by cog id rather than by original filename, deliberately:
	//   - two cogs can share a name across folders, and a destination collision
	//     would silently reduce 38 inputs to fewer, which is the exact class of
	//     bug this change exists to remove;
	//   - filenames come from user uploads and are interpolated into a shell
	//     command here, so a real name is an injection surface;
	//   - each file carries its OWN extension, which is the point. A mixed
	//     capture (DJI writes JPG beside DNG) must not have the first file's
	//     extension applied to all of them.
	//
	// The cost is that the container sees 47.jpg rather than DSC_0042.jpg, so
	// anything reporting per-image findings names a cog id. That is resolvable
	// at display time and is not worth an injection surface.
	if len(payload.InputFiles) > 0 {
		parts := []string{alias}
		for i, f := range payload.InputFiles {
			if f.ContentHash == "" {
				continue
			}
			fx, err := normalizeStagingExtension(f.FileExtension,
				fmt.Sprintf("input_files[%d] (cog %d)", i, f.CogID))
			if err != nil {
				return "", err
			}
			dest := fmt.Sprintf("%d%s", f.CogID, fx)
			if err := assertStagingDestInside("input", dest); err != nil {
				return "", err
			}
			parts = append(parts, fmt.Sprintf(
				`mc cp myminio/%s/%s /data/input/%s`,
				payload.MinioBucket, f.ContentHash, dest,
			))
		}
		return strings.Join(parts, " && "), nil
	}

	ext, err := normalizeStagingExtension(payload.FileExtension, "payload")
	if err != nil {
		return "", err
	}
	dest := fmt.Sprintf("input_%s%s", payload.NodeKind, ext)
	if err := assertStagingDestInside("input", dest); err != nil {
		return "", err
	}
	return fmt.Sprintf(
		`%s && mc cp myminio/%s/%s /data/input/%s`,
		alias, payload.MinioBucket, payload.SourceFileLocation, dest,
	), nil
}

func (e *Executor) downloadSidecar(ctx context.Context, minioImage, volName string, creds *transport.Credentials, payload transport.TaskPayload) error {
	if len(payload.InputFiles) > 0 {
		e.log.Info("staging multiple inputs",
			"count", len(payload.InputFiles), "node_kind", payload.NodeKind)
	} else if payload.FileExtension == "" {
		e.log.Warn("FileExtension is empty — container may not find the input file", "node_kind", payload.NodeKind)
	} else {
		e.log.Info("downloading with extension", "ext", payload.FileExtension, "node_kind", payload.NodeKind)
	}
	// R-154 follow-up: buildDownloadCmd fails closed — an escaping extension
	// or node kind aborts before the sidecar starts, so nothing downloads.
	cmd, err := buildDownloadCmd(payload)
	if err != nil {
		return err
	}
	return e.runSidecarWithEnvCreds(ctx, minioImage, volName, cmd, creds.MinIO)
}

// uploadSidecar runs a minio-client container to upload outputs with hash-based naming.
// Parses the manifest output to return structured OutputFile entries.
//
// workflow-agentic-execution-loop §wizard-test-run-dispatch — when the
// task payload carries Purpose='wizard_test_run' the sidecar:
//
//	(a) prefixes every key with `payload.OutputPrefix` so the outputs land
//	    under `_wizard_test_runs/{session_id}/{iteration_n}/<hash>` and
//	    never mix with user-visible production outputs.
//	(b) enforces a total-output size cap (SandboxMaxOutputMB) by summing
//	    file sizes BEFORE upload and aborting with a clear error if the
//	    sum exceeds the cap.
func (e *Executor) uploadSidecar(ctx context.Context, minioImage, volName string, creds *transport.Credentials, task *transport.Task) ([]transport.OutputFile, error) {
	// Script shared with the K8s stage-out container (upload_script.go, W17).
	cmd := buildUploadScript(uploadScriptParamsFromTask(task))

	output, err := e.runSidecarWithOutput(ctx, minioImage, volName, cmd, creds.MinIO)
	if err != nil {
		return nil, err
	}

	return parseUploadManifest(output), nil
}

// stageOutputsForScan is the Fortress 3.5 PRE-STORE staging pass: the same
// minio-client sidecar, the same tenant creds (creds.MinIO, refreshed by
// credsrefresher) and the same buildUploadScript, but writing to the transient
// `scan-staging/{task_id}/` prefix so the API's pre-store consult can fetch
// REAL bytes (task_output_scan.py _validate_staging_key + _scan_staged_bytes
// via minio_client.MinioClient(task.tenant_id)) instead of scanning metadata
// only. No new credentials, no new env vars, no presigned URLs.
//
// Unlike uploadSidecar this is BEST-EFFORT and never retried: the caller logs
// any error here and proceeds with outputs=[] (the pre-existing metadata-only
// consult). A staging failure must never fail the task, and retrying would
// double the sidecar cost on the hot completion path.
//
// IMPORTANT ORDERING NOTE: this sidecar ONLY uploads — it must never also
// delete the staged objects. The pre-store consult (scanOutputBeforeStore) is
// an HTTP call the CALLER makes after this function returns; if cleanup ran
// in this same sidecar invocation, the staged bytes would already be gone by
// the time the API tried to fetch them. Cleanup is cleanupStagedOutputs, a
// separate sidecar call the caller defers until after the consult.
func (e *Executor) stageOutputsForScan(ctx context.Context, minioImage, volName string, creds *transport.Credentials, task *transport.Task) ([]transport.OutputFile, error) {
	params := stagingUploadParamsFromTask(task)
	// parseUploadManifest reports the manifest key column verbatim in ContentHash
	// (buildUploadScript prints `UPLOAD,<prefix><hash>,...`), so the staging key
	// is that column itself; when a build ever drops the prefix from the column
	// we re-prepend it. Either way the key satisfies
	// task_output_scan.py:_validate_staging_key.
	manifest, err := e.runSidecarWithOutput(ctx, minioImage, volName, buildUploadScript(params), creds.MinIO)
	if err != nil {
		return nil, err
	}
	staged := parseUploadManifest(manifest)
	for i := range staged {
		key := staged[i].ContentHash
		if !strings.HasPrefix(key, params.Prefix) {
			key = params.Prefix + key
		}
		staged[i].StagingKey = key
	}
	return staged, nil
}

// cleanupStagedOutputs removes the transient scan-staging/{task_id}/ prefix in
// a SEPARATE sidecar invocation from the staging upload (see the ordering note
// on stageOutputsForScan) — the caller defers this until after the pre-store
// consult has fetched the staged bytes. Best-effort: `|| true` so a bucket
// with nothing to remove, or a transient MinIO hiccup, never fails the task;
// an error here only means an orphaned transient blob for the API-side sweep
// (wizard_test_run_cleanup.py shape) to pick up later — these objects are
// referenced by NO DB record, so removing them is not a record delete (ISO
// 27001 no-hard-delete posture is about records).
func (e *Executor) cleanupStagedOutputs(ctx context.Context, minioImage, volName string, creds *transport.Credentials, task *transport.Task) error {
	params := stagingUploadParamsFromTask(task)
	return e.runSidecar(ctx, minioImage, volName, stagingCleanupCmd(params), creds.MinIO)
}

// stagingCleanupCmd is the standalone cleanup command for the scan-staging/
// prefix — deliberately NOT appended to the staging upload script, so it only
// ever runs (via cleanupStagedOutputs) after the pre-store consult returns.
func stagingCleanupCmd(params uploadScriptParams) string {
	return `mc rm -r myminio/` + params.Bucket + `/` + params.Prefix + ` || true`
}

// checkpointExportStagingKey is the FIXED (never hash-named) scan-staging key
// for a checkpoint export: unlike buildUploadScript's per-file outputs, there
// is exactly one tar per task, so there is nothing to disambiguate.
// task_output_scan.py:_validate_staging_key only pins the
// `scan-staging/{task.id}/` prefix, so any name under it validates.
func checkpointExportStagingKey(taskID int) string {
	return "scan-staging/" + strconv.Itoa(taskID) + "/checkpoint.tar"
}

// stageCheckpointExportForScan is the checkpoint-export mirror of
// stageOutputsForScan: tar the checkpoint volume (the SAME checkpointHelperImage
// idiom ExportCheckpointVolume uses, checkpoint_transfer.go:115) into a host
// temp dir — never bind-mounted into the task's own /data volume, whose
// /data/output is the real output listing buildUploadScript walks — then `mc
// cp` that tar into the transient `scan-staging/{task_id}/checkpoint.tar` key
// in the tenant bucket via the same minio-client image/creds every other
// staging sidecar in this file uses. BEST-EFFORT like stageOutputsForScan: the
// caller degrades to a metadata-only consult (outputs=[]) on any error here
// and never fails the task for a staging failure alone.
func (e *Executor) stageCheckpointExportForScan(ctx context.Context, minioImage string,
	creds *transport.Credentials, task *transport.Task, checkpointVolume string) (string, int64, error) {
	dir, err := os.MkdirTemp("", "checkpoint-export-")
	if err != nil {
		return "", 0, fmt.Errorf("checkpoint export workspace: %w", err)
	}
	defer os.RemoveAll(dir)

	tarCmd := execCommandContext(ctx, "docker", "run", "--rm",
		"-v", checkpointVolume+":/data:ro",
		"-v", dir+":/out",
		checkpointHelperImage, "tar", "czf", "/out/checkpoint.tar", "-C", "/data", ".")
	var tarStderr bytes.Buffer
	tarCmd.Stderr = &tarStderr
	if err := tarCmd.Run(); err != nil {
		return "", 0, fmt.Errorf("tar checkpoint volume %q: %v: %s",
			checkpointVolume, err, strings.TrimSpace(tarStderr.String()))
	}

	tarPath := filepath.Join(dir, "checkpoint.tar")
	info, err := os.Stat(tarPath)
	if err != nil {
		return "", 0, fmt.Errorf("stat staged checkpoint tar: %w", err)
	}

	key := checkpointExportStagingKey(task.ID)
	cpScript := fmt.Sprintf("mc cp /stage/checkpoint.tar myminio/%s/%s", task.Payload.MinioBucket, key)
	env := []string{
		"MC_HOST_myminio=" + creds.MinIO.MCHost,
		"MINIO_ENDPOINT=" + creds.MinIO.Endpoint,
		"MINIO_ACCESS=" + creds.MinIO.AccessKey,
		"MINIO_SECRET=" + creds.MinIO.SecretKey,
	}
	mcArgs := []string{"run", "--rm", "--network", e.network, "-v", dir + ":/stage:ro"}
	mcArgs = append(mcArgs, mcEnvArgs(env)...)
	mcArgs = append(mcArgs, minioImage, "sh", "-c", cpScript)
	mcCmd := execCommandContext(ctx, "docker", mcArgs...)
	var mcStderr bytes.Buffer
	mcCmd.Stderr = &mcStderr
	if err := mcCmd.Run(); err != nil {
		return "", 0, fmt.Errorf("stage checkpoint tar to %s: %v: %s",
			key, err, strings.TrimSpace(mcStderr.String()))
	}
	return key, info.Size(), nil
}

// mcEnvArgs renders a NAME=VALUE env slice as repeated `-e NAME=VALUE` docker
// CLI flags — these sidecars go through execCommandContext (a plain `docker
// run` child process) rather than the docker SDK's ContainerCreate
// (stageCheckpointExportForScan needs two volumes, which
// runSidecarWithOutput's single-bind signature has no room for), so the env
// has to go on the `docker run` command line instead of a Config.Env slice.
func mcEnvArgs(env []string) []string {
	args := make([]string, 0, len(env)*2)
	for _, kv := range env {
		args = append(args, "-e", kv)
	}
	return args
}

// cleanupCheckpointExportStaging removes the transient scan-staging/{task_id}/
// prefix after the pre-PUT consult has fetched the staged tar — the
// checkpoint-export mirror of cleanupStagedOutputs, same best-effort `|| true`
// posture and same "referenced by no DB record" ISO 27001 reasoning.
func (e *Executor) cleanupCheckpointExportStaging(ctx context.Context, minioImage string,
	creds *transport.Credentials, task *transport.Task) error {
	prefix := "scan-staging/" + strconv.Itoa(task.ID) + "/"
	cmd := fmt.Sprintf("mc rm -r myminio/%s/%s || true", task.Payload.MinioBucket, prefix)
	env := []string{
		"MC_HOST_myminio=" + creds.MinIO.MCHost,
		"MINIO_ENDPOINT=" + creds.MinIO.Endpoint,
		"MINIO_ACCESS=" + creds.MinIO.AccessKey,
		"MINIO_SECRET=" + creds.MinIO.SecretKey,
	}
	rmArgs := []string{"run", "--rm", "--network", e.network}
	rmArgs = append(rmArgs, mcEnvArgs(env)...)
	rmArgs = append(rmArgs, minioImage, "sh", "-c", cmd)
	rmCmd := execCommandContext(ctx, "docker", rmArgs...)
	var stderr bytes.Buffer
	rmCmd.Stderr = &stderr
	if err := rmCmd.Run(); err != nil {
		return fmt.Errorf("%v: %s", err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

// exportCheckpointWithScan is the checkpoint-export mirror of Run() Step 6's
// scanOutputBeforeStore call: stage the checkpoint tar under the same
// transient prefix, consult the SAME output-scan-check endpoint (posture
// `off` skips both hops), and only then PUT. Returns (blocked, reason)
// exactly like scanOutputBeforeStore. Allowed exports report the upload via
// the 985f worker-authenticated endpoint; a report failure never blocks or
// fails the task (log.Warn only — the API's reaper timeout fallback already
// covers a lost report, loan_handback.py:197).
func (e *Executor) exportCheckpointWithScan(ctx context.Context, task *transport.Task,
	creds *transport.Credentials, minioImage, checkpointVolume, exportURL string) (bool, string) {
	// 1665w: under the CE posture the staging hop is in process (ce_inprocess_
	// staging.go: alpine tar of the volume + minio-go PutObject at the same
	// fixed key, cleanup via RemoveObject over the same transient prefix). The
	// posture signal is the volume-shell image the caller passes: Run() hands
	// ceStagingHelper's alpine helper under the posture and the mc ref verbatim
	// otherwise, so the Harbor/SaaS staging pair below stays byte-for-byte.
	// The presigned-PUT HTTP leg (ExportCheckpointVolume, checkpoint_transfer.go)
	// is unchanged either way.
	ceStageExport := e.stageCheckpointExportForScan
	ceCleanExport := func() error {
		return e.cleanupCheckpointExportStaging(ctx, minioImage, creds, task)
	}
	if minioImage == ceStageHelperImage {
		ceStageExport = e.ceStageCheckpointExportForScan
		ceCleanExport = func() error {
			return cleanupNativeStagedOutputs(ctx, task, creds)
		}
	}
	mode := e.tunables.String(outputScanModeTunableKey, "", "shadow",
		"off", "shadow", "enforce")
	var staged []transport.OutputFile
	var byteSize int64
	if mode != "off" {
		if key, size, stageErr := ceStageExport(ctx, minioImage, creds, task, checkpointVolume); stageErr != nil {
			e.log.Warn("checkpoint-export staging sidecar failed; consult falls back to metadata-only",
				"task", task.ID, "error", stageErr)
		} else {
			staged = []transport.OutputFile{{FileName: "checkpoint.tar", StagingKey: key, SizeBytes: size}}
			byteSize = size
			defer func() {
				if cleanupErr := ceCleanExport(); cleanupErr != nil {
					e.log.Warn("checkpoint-export scan-staging cleanup failed; orphaned blob needs the API-side sweep",
						"task", task.ID, "error", cleanupErr)
				}
			}()
		}
	}
	if blocked, reason := e.scanOutputBeforeStore(ctx, task, staged, true); blocked {
		return true, reason
	}
	if err := e.ExportCheckpointVolume(ctx, checkpointVolume, exportURL); err != nil {
		e.log.Warn("checkpoint export PUT failed", "task", task.ID,
			"volume", checkpointVolume, "export_url", redactCheckpointURL(exportURL), "error", err)
		return false, ""
	}
	e.reportCheckpointExportUploaded(ctx, task, byteSize)
	return false, ""
}

// checkpointExportReportPathFmt is the 985f worker-authenticated mirror of
// 985a's platform-admin route (routers/loans.py's
// `/loan/{loan_id}/checkpoint/export-uploaded`) — the worker reports a
// completed checkpoint export PUT here, via the SAME outputScanSetAuth
// bootstrap-token seam, never the /managers/work/... prefix (admin/peer-manager
// auth only).
const checkpointExportReportPathFmt = "/workers/tasks/%d/checkpoint-export-uploaded"

// reportCheckpointExportUploaded posts the 985f report after a successful
// ExportCheckpointVolume PUT. byteSize is 0 when staging was skipped (posture
// off) or failed — the API's byte_size field is Optional, so 0 degrades to
// "no byte count reported" rather than a wrong number. Best-effort: every
// failure path here (including no numeric loan id / no transfer id to report,
// which is expected until 985c stamps CheckpointExportTransferID) is a
// log.Warn only.
func (e *Executor) reportCheckpointExportUploaded(ctx context.Context, task *transport.Task, byteSize int64) {
	if task.Payload.CheckpointExportTransferID == 0 {
		e.log.Warn("checkpoint export uploaded but no transfer id to report",
			"task", task.ID, "loan_id", task.Payload.LoanID)
		return
	}
	loanID, err := strconv.Atoi(strings.TrimSpace(task.Payload.LoanID))
	if err != nil {
		e.log.Warn("checkpoint export uploaded but no numeric loan id to report",
			"task", task.ID, "loan_id", task.Payload.LoanID)
		return
	}
	base := strings.TrimRight(strings.TrimSpace(e.apiURL), "/")
	if base == "" {
		e.log.Warn("checkpoint export uploaded but no API URL to report to", "task", task.ID)
		return
	}
	body, err := json.Marshal(map[string]interface{}{
		"loan_id":     loanID,
		"transfer_id": task.Payload.CheckpointExportTransferID,
		"byte_size":   byteSize,
	})
	if err != nil {
		e.log.Warn("checkpoint export report build failed", "task", task.ID, "error", err)
		return
	}
	reqCtx, cancel := context.WithTimeout(ctx, outputScanHTTPTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost,
		base+fmt.Sprintf(checkpointExportReportPathFmt, task.ID), bytes.NewReader(body))
	if err != nil {
		e.log.Warn("checkpoint export report request build failed", "task", task.ID, "error", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if outputScanSetAuth != nil {
		outputScanSetAuth(req)
	}
	resp, err := outputScanHTTPDo(ctx, req)
	if err != nil {
		e.log.Warn("checkpoint export report POST failed", "task", task.ID, "error", err)
		return
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode >= 400 {
		e.log.Warn("checkpoint export report refused", "task", task.ID, "status", resp.StatusCode)
	}
}

func (e *Executor) runSidecar(ctx context.Context, img, volName, cmd string, minioCreds transport.MinIOCredentials) error {
	_, err := e.runSidecarWithOutput(ctx, img, volName, cmd, minioCreds)
	return err
}

func (e *Executor) runSidecarWithEnvCreds(ctx context.Context, img, volName, cmd string, minioCreds transport.MinIOCredentials) error {
	_, err := e.runSidecarWithOutput(ctx, img, volName, cmd, minioCreds)
	return err
}

func (e *Executor) runSidecarWithOutput(ctx context.Context, img, volName, cmd string, minioCreds transport.MinIOCredentials) (string, error) {
	env := []string{
		"MC_HOST_myminio=" + minioCreds.MCHost,
		"MINIO_ENDPOINT=" + minioCreds.Endpoint,
		"MINIO_ACCESS=" + minioCreds.AccessKey,
		"MINIO_SECRET=" + minioCreds.SecretKey,
	}

	resp, err := e.docker.ContainerCreate(ctx,
		&container.Config{
			Image:      img,
			Entrypoint: []string{"/bin/sh"},
			Cmd:        []string{"-c", cmd},
			Env:        env,
			Labels:     e.sidecarLabels(),
		},
		&container.HostConfig{
			NetworkMode: container.NetworkMode(e.network),
			Binds:       []string{volName + ":/data:rw"},
			AutoRemove:  false, // we read logs before removing
		},
		&network.NetworkingConfig{},
		nil,
		"",
	)
	if err != nil {
		return "", fmt.Errorf("create sidecar: %w", err)
	}
	containerID := resp.ID

	defer func() {
		rmCtx, rmCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer rmCancel()
		e.docker.ContainerRemove(rmCtx, containerID, container.RemoveOptions{Force: true})
	}()

	if err := e.docker.ContainerStart(ctx, containerID, container.StartOptions{}); err != nil {
		return "", fmt.Errorf("start sidecar: %w", err)
	}

	// Progress-based liveness, NOT a wall-clock cap.
	//
	// The task ctx reaching here has no deadline whenever inactivity-liveness
	// is on (see the pipelineCtx branch in Run) — that model watches the main
	// node container, and sidecars were left with nothing. A stalled `mc`
	// therefore parked this wait forever, the deferred ContainerRemove above
	// never fired, and the sidecar stayed RUNNING for the life of the worker:
	// the shape that put 464 stuck sidecars on a dev box in 3.5 minutes and
	// wedged dockerd.
	//
	// The fix is not a duration limit. Sidecars run sequentially with the
	// processing container (Run steps 2-6), so while this one is alive `mc`
	// IS the work — and the payload is whatever the node produced, which for
	// a long render can take hours to move. Killing on elapsed time would
	// destroy exactly that. Killing on NO progress catches the real failure
	// mode without ever touching a transfer that is still moving bytes.
	statusCh, errCh := e.docker.ContainerWait(ctx, containerID, container.WaitConditionNotRunning)
	idleWindow := _sidecarIdleWindow()
	probe := time.NewTicker(sidecarProbeInterval)
	defer probe.Stop()
	lastProgress := int64(-1)
	lastMoved := time.Now()

	for {
		select {
		case err := <-errCh:
			if err != nil {
				return "", fmt.Errorf("wait sidecar: %w", err)
			}
			return e.getContainerLogs(ctx, containerID, 0), nil

		case status := <-statusCh:
			if status.StatusCode != 0 {
				// Full tail (not the old 50-line cap), stdout and stderr kept
				// SEPARATE — calibration-health-fixtures-and-timeouts §3. The old
				// combined "stdout then stderr" ordering meant a success banner
				// like `mc`'s "Added myminio successfully." (stdout) sat in front
				// of the actual failure line (stderr), and whatever downstream
				// display/storage limit existed cut the message off inside the
				// banner — the exact "Added myminio successfully.   \nmc" bug
				// design.md calls out. SidecarExitError keeps both so a caller
				// can surface stderr on its own.
				stdout, stderr := e.getContainerLogsSplit(ctx, containerID, 0)
				return "", &SidecarExitError{ExitCode: int(status.StatusCode), Stdout: stdout, Stderr: stderr}
			}
			return e.getContainerLogs(ctx, containerID, 0), nil

		case <-probe.C:
			n, ok := e.sidecarProgress(ctx, containerID)
			if !ok {
				// Unmeasurable (stats call failed, container already gone).
				// Treat as alive — a probe failure must never be the thing
				// that kills a running transfer.
				continue
			}
			if n != lastProgress {
				lastProgress = n
				lastMoved = time.Now()
				continue
			}
			if time.Since(lastMoved) >= idleWindow {
				reason := fmt.Sprintf("sidecar stalled: no network, disk, or CPU progress for %s",
					idleWindow.Round(time.Second))
				e.killContainer(containerID, reason)
				return "", fmt.Errorf("%s", reason)
			}
		}
	}
}

// sidecarProgress returns a monotonic activity counter for a sidecar: bytes
// moved over the network, bytes read/written to disk, and CPU time consumed.
// A transfer that is doing anything at all advances at least one of these; one
// blocked on a socket that will never answer advances none, and consumes no
// CPU while blocked. ok is false when no reading could be taken, which callers
// must treat as "assume alive".
func (e *Executor) sidecarProgress(ctx context.Context, containerID string) (int64, bool) {
	statsCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	resp, err := e.docker.ContainerStatsOneShot(statsCtx, containerID)
	if err != nil {
		return 0, false
	}
	defer resp.Body.Close()

	var stats container.StatsResponse
	if err := json.NewDecoder(resp.Body).Decode(&stats); err != nil {
		return 0, false
	}

	var total int64
	for _, n := range stats.Networks {
		total += int64(n.RxBytes + n.TxBytes)
	}
	for _, b := range stats.BlkioStats.IoServiceBytesRecursive {
		total += int64(b.Value)
	}
	total += int64(stats.CPUStats.CPUUsage.TotalUsage)
	return total, true
}

// imageHost returns the hostname segment of a container image ref
// ("example.com" from
// "example.com/tenant-1/foo:latest"). For refs with no
// host (bare repo names, sha256 digests on Docker Hub), returns the
// empty string — the caller then treats the image as platform-defaulted.
//
// A "/" in the first path segment that doesn't also contain "." or ":"
// or "localhost" is treated as a Docker Hub namespace, not a hostname
// (matches the Docker daemon's parsing in reference/normalize.go).
func imageHost(imageRef string) string {
	if imageRef == "" {
		return ""
	}
	i := strings.IndexByte(imageRef, '/')
	if i == -1 {
		return ""
	}
	first := imageRef[:i]
	if first == "localhost" {
		return first
	}
	if strings.ContainsAny(first, ".:") {
		return first
	}
	return ""
}

// normalizeHost trims any scheme prefix and trailing slashes from a
// configured registry URL so it can be compared to imageHost output.
// The API sends "https://stage-registry.example.com/" for some tenants;
// imageHost output never has a scheme. Without this the string compare
// always misses and we silently fall back to platform creds.
func normalizeHost(registry string) string {
	registry = strings.TrimSpace(registry)
	for _, prefix := range []string{"https://", "http://"} {
		if strings.HasPrefix(registry, prefix) {
			registry = registry[len(prefix):]
			break
		}
	}
	return strings.TrimRight(registry, "/")
}

// PickRegistryAuth selects the right Harbor credentials for an image
// based on its registry hostname. Tenant images live on the tenant
// Harbor (e.g. example.com) and require the tenant
// robot account; platform images live on the platform Harbor (e.g.
// registry.example.com) and require the platform admin. Sending the
// wrong set produces a 401 because the two Harbors are independent
// auth backends — admin on platform Harbor is not admin on tenant
// Harbor.
//
// Exported so the API/worker contract is testable: an end-to-end test
// can assert that a tenant-1 image picks the tenant creds without
// having to spin up a Docker daemon.
func PickRegistryAuth(imageRef string, platform, tenant transport.HarborCredentials) transport.HarborCredentials {
	if tenant.Username == "" || tenant.Registry == "" {
		return platform
	}
	if imageHost(imageRef) == normalizeHost(tenant.Registry) {
		return tenant
	}
	return platform
}

// registryKindFor returns a short label ("tenant" / "platform" / "none")
// for logging which credentials path the pull is taking. Helps operators
// distinguish "401 with platform creds vs tenant Harbor" from "401
// because tenant Harbor creds are wrong" without re-running with debug
// logging.
func registryKindFor(imageRef string, platform, tenant transport.HarborCredentials) string {
	if tenant.Username != "" && imageHost(imageRef) == normalizeHost(tenant.Registry) {
		return "tenant"
	}
	if platform.Username != "" {
		return "platform"
	}
	return "none"
}

// pullImage fetches the freshest processing image WITHOUT re-downloading it
// when nothing changed.
//
// "Pull only if the cloud image is new" is achieved by `docker pull` itself:
// ImagePull re-resolves the tag, fetches the manifest, and downloads ONLY the
// layers the daemon doesn't already have. An unchanged :latest is a near-
// instant no-op ("image is up to date"); a re-pushed :latest pulls just the
// changed layers.
//
// The key change vs. the old behavior: we NO LONGER force-remove the cached
// image before pulling. That force-remove threw away the layer cache every
// run and re-downloaded the whole image — fine for small images, but it
// re-pulled multi-GB system images (e.g. the ~9GB Containerizer) on every
// task and could hang for minutes on a slow registry, stranding the image
// with no error surfaced. Letting docker's native digest-aware pull do the
// work is both correct (stays fresh) and cheap (no redundant download) — and
// adds no extra registry round-trip that could itself hang.
//
// preferLocalImagesTunableKey is the platform_settings registry key for the
// prefer-local-images override (must match
// CORE/API/services/platform_settings.py's register() call and
// worker_management.py's _WORKER_TUNABLE_KEYS mapping).
const preferLocalImagesTunableKey = "worker.prefer_local_images"

// preferLocalImages resolves the prefer-local-images override with
// precedence: server-pushed tunable (GS.PSR.7) > local
// MOD_WORKER_PREFER_LOCAL_IMAGES env > false. Read fresh on every call, so a
// Dashboard change applies to the next pullImage within two heartbeats — no
// worker restart.
func (e *Executor) preferLocalImages() bool {
	if e.tunables != nil {
		return e.tunables.Bool(preferLocalImagesTunableKey, "MOD_WORKER_PREFER_LOCAL_IMAGES", false)
	}
	return os.Getenv("MOD_WORKER_PREFER_LOCAL_IMAGES") == "1"
}

// MOD_WORKER_PREFER_LOCAL_IMAGES=1 (or the worker.prefer_local_images
// platform setting — see preferLocalImages) is the offline/local-patch
// override: if the image is cached at all it's used as-is, skipping the
// pull entirely. Use case: stage hosts running patched images not (yet)
// published to Harbor.
//
// expectedDigest (B119): when non-empty the prefer-local path compares
// the local image digest against it. A mismatch forces a pull (fresh
// image wins over stale local). A network/registry failure during pull
// falls back to the local copy when preferLocalImages is true (best-effort
// execution).
// imageInspect is the single seam every image-inspect step in the pull path
// goes through. Production (hook nil) is the daemon verbatim; a unit test
// sets Executor.imageInspectHook so the prefer-local cache-hit path — which
// is exactly the path that SIGSEGVed on 1900Y1g — can be driven with no
// daemon at all.
func (e *Executor) imageInspect(ctx context.Context, imageName string) (types.ImageInspect, error) {
	if e.imageInspectHook != nil {
		return e.imageInspectHook(ctx, imageName)
	}
	img, _, err := e.docker.ImageInspectWithRaw(ctx, imageName)
	return img, err
}

// noteExecutedImageDigest is the B119 inspect step Run runs right after the
// pull (or right after a prefer-local cache hit skipped it). It RETURNS the
// digest rather than writing it: Run's named *Result return is still nil at
// that point in the body, so the pre-fix `res.ExecutedImageDigest = ...`
// write was a nil-pointer write (SIGSEGV at offset 0x78, the ExecutedImageDigest
// word of Result) that killed the worker between the "skipping pull" log and
// the container run. Run keeps the value in a local and its existing defer
// copies it onto whichever Result Run actually returns.
func (e *Executor) noteExecutedImageDigest(ctx context.Context, imageName string) string {
	img, err := e.imageInspect(ctx, imageName)
	if err != nil {
		return ""
	}
	return imageDigestForInspect(img)
}

func (e *Executor) pullImage(ctx context.Context, imageName string, harbor transport.HarborCredentials, expectedDigest string) error {
	if reg, err := harborRegistry(harbor); err == nil {
		e.noteHarborHost(reg)
	}
	if e.preferLocalImages() {
		if img, err := e.imageInspect(ctx, imageName); err == nil {
			if expectedDigest == "" {
				// No digest to compare — legacy path, skip pull.
				e.log.Info(
					"prefer-local-images: image cached locally, skipping pull",
					"image", imageName,
				)
				return nil
			}
			// B119: digest-aware local check — compare the local image's
			// docker-digest against the expected one.
			// normalizeDigest on BOTH sides: the registered digest arrives verbatim
			// from NodeDefinition.image_digest / the registry Docker-Content-Digest
			// header, neither of which guarantees the sha256: prefix or lower-case
			// hex, while imageDigestForInspect always returns the canonical form. A
			// raw == makes a same-build image look mismatched forever: the worker
			// re-pulls a multi-GB image on every task and, on a registry blip,
			// silently runs the stale local copy behind a mismatch warning.
			localDigest := imageDigestForInspect(img)
			// B119 follow-up (PIN8 gpu3, finding F-2): `docker save | docker load`
			// drops RepoDigests, so a locally LOADED image carries no registry
			// (manifest) digest at all — imageDigestForInspect then falls back to
			// img.ID, the config digest, which is a hash of a DIFFERENT thing than
			// the per-task expectedDigest (the registry manifest digest the API
			// stamps on NodeDefinition.image_digest). normalizeDigest cannot bridge
			// two different hashes, so the comparison below reports a mismatch for
			// an image that is not stale, merely loaded, and the pull that follows
			// dies `unauthorized` on a registry_kind=none tag -> job red.
			// No RepoDigests means there is NOTHING to compare: with prefer-local
			// ON the local copy is used as-is (the same posture as the legacy
			// empty-expectedDigest path above). A local image that DOES carry a
			// RepoDigest keeps the digest check verbatim below — a real mismatch
			// (stale local copy) still pulls.
			if len(img.RepoDigests) == 0 {
				e.log.Info(
					"prefer-local-images: using local image; no registry digest recorded for a loaded image, digest not comparable",
					"image", imageName, "expected", expectedDigest, "local_id", localDigest,
				)
				return nil
			}
			if normalizeDigest(localDigest) == normalizeDigest(expectedDigest) {
				e.log.Info(
					"prefer-local-images: image cached locally, digest matches, skipping pull",
					"image", imageName,
				)
				return nil
			}
			// Digest mismatch: fall through to pull so the worker gets the
			// correct image version.
			e.log.Info(
				"prefer-local-images: digest mismatch; pulling",
				"expected", expectedDigest, "local", localDigest,
			)
		}
	}

	opts := image.PullOptions{}
	if !e.isLocalRegistryImage(imageName) {
		// The CE no-registry posture (no host AND no creds, 1665q) has nothing
		// to authenticate against — sending an empty auth header makes the
		// daemon try the default public registry and 400. Any other shape,
		// including a configured registry whose creds are empty, keeps the
		// hosted path verbatim so a creds mismatch still 401s loudly.
		if !noRegistryPosture(harbor) {
			encoded, encErr := auth.EncodeRegistryAuth(harbor.Username, harbor.Password)
			if encErr != nil {
				return fmt.Errorf("encode registry auth: %w", encErr)
			}
			opts.RegistryAuth = encoded
		}
	}

	reader, err := e.docker.ImagePull(ctx, imageName, opts)
	if err != nil {
		// B119: if prefer-local is on and the pull fails because of a
		// network/registry error, fall back to the local copy (best-effort).
		// The caller's retryOp will have already retried, so this is the
		// final fallback before the task fails.
		if e.preferLocalImages() && isRegistryNetworkError(err) {
			if img, insErr := e.imageInspect(ctx, imageName); insErr == nil {
				localDig := imageDigestForInspect(img)
				e.log.Warn("prefer-local-images: pull failed (network error), falling back to local image",
					"image", imageName,
					"error", err,
					"expected_digest", expectedDigest,
					"local_digest", localDig,
				)
				return nil
			}
		}
		return err
	}
	// PRODUCT GAP fix (CE node-images diag): the ImagePull body is a JSON
	// message stream; a registry tag-miss (404) is carried IN IT, not in the
	// ImagePull error. The old code discarded the body, so the real cause
	// ("manifest unknown") never reached retryOp's WARN and only surfaced
	// later as a bare "No such image" at CreateContainer. The stream carries
	// no credentials or auth headers — the image ref is safe to log.
	// retryOp keeps its 3 retries on this error; isRegistryNetworkError does
	// not match a tag-miss, so the prefer-local fallback is untouched.
	//
	defer reader.Close()
	if err := pullStreamError(imageName, reader); err != nil {
		return err
	}
	return nil
}

// pullStreamError decodes a docker ImagePull JSON message stream and returns
// the first error it carries, shaped "pull image <ref>: <message>".
// Daemon/registry error spellings covered: {"Err":"..."} (pull stream),
// {"error":"..."} (go-client checkResponseForError shape), and
// {"ErrorDetail":{"Message":"..."}} (newer API errors). A clean progress
// stream (or an empty body) returns nil.
func pullStreamError(imageName string, reader io.Reader) error {
	type pullMsg struct {
		Err       string `json:"Err"`
		Err2      string `json:"error"`
		ErrDetail struct {
			Msg string `json:"Message"`
		} `json:"ErrorDetail"`
	}
	dec := json.NewDecoder(reader)
	for {
		var m pullMsg
		if dec.Decode(&m) != nil {
			// Stream exhausted or garbled tail: any error line already
			// returned; a clean progress stream ends here.
			return nil
		}
		msg := m.ErrDetail.Msg
		if msg == "" {
			msg = m.Err
		}
		if msg == "" {
			msg = m.Err2
		}
		if msg != "" {
			return fmt.Errorf("pull image %s: %s", imageName, msg)
		}
	}
}

// imageDigestForInspect returns the docker-digest string from an image
// inspect result, or the empty string if unavailable.
//
// Prefers the image's RepoDigests (the manifest/registry digest) so the
// value matches what NodeDefinition.image_digest stores (populated by
// manifest_emit.py's _image_digest which reads RepoDigests).  Falls back
// to img.ID (the config digest) when RepoDigests is empty.
//
// Both the returned digest and any expected digest are normalized for
// comparison: sha256: prefix ensured, lowercased.
func imageDigestForInspect(img types.ImageInspect) string {
	// Prefer RepoDigests entries — these carry the manifest digest that
	// NodeDefinition.image_digest is stamped with.
	for _, rd := range img.RepoDigests {
		if idx := strings.Index(rd, "@"); idx >= 0 {
			return strings.ToLower(rd[idx+1:]) // "sha256:..."
		}
	}
	// Fallback to the config digest.
	if img.ID != "" {
		id := strings.ToLower(img.ID)
		if !strings.HasPrefix(id, "sha256:") {
			id = "sha256:" + id
		}
		return id
	}
	return ""
}

// normalizeDigest ensures a digest string has the sha256: prefix and is
// lowercased, so two digests can be compared with ==.
func normalizeDigest(d string) string {
	if d == "" {
		return ""
	}
	d = strings.ToLower(d)
	if !strings.HasPrefix(d, "sha256:") {
		d = "sha256:" + d
	}
	return d
}

// isRegistryNetworkError returns true when err looks like a network /
// registry-connection problem (as opposed to, say, a 401/403/404 auth or
// permissions error). Used by pullImage to decide whether a local-image
// fallback makes sense.
func isRegistryNetworkError(err error) bool {
	msg := err.Error()
	if strings.Contains(msg, "certificate") || strings.Contains(msg, "x509") {
		// TLS cert-trust failure (e.g. x509: certificate signed by unknown
		// authority) is a config problem, not a connectivity blip: never fall
		// back on a pinned digest for it (1859D1 review narrowing).
		return false
	}
	return strings.Contains(msg, "connection") ||
		strings.Contains(msg, "refused") ||
		strings.Contains(msg, "timeout") ||
		strings.Contains(msg, "TLS") ||
		strings.Contains(msg, "network") ||
		strings.Contains(msg, "no such host") ||
		strings.Contains(msg, "EOF") ||
		strings.Contains(msg, "network unreachable")
}

// PullImagePublic pulls an image without registry auth — used for pre-pulling
// images during the profiling window. Harbor credentials aren't available yet
// at this stage (they're per-task), so we rely on Docker's default auth
// (which works for public registries and registries with stored credentials
// in Docker's config.json, which the setup script configures).
//
// GF.C3b: when worker.harbor_proxy_cache is enabled AND a Harbor host has
// been observed from an earlier task's credentials (knownHarborHost), the
// pull is routed through Harbor's proxy-cache first (tagging the result
// back to imageName so callers see the same ref either way), falling back
// to this same bare pull on any proxy error or when no host is known yet —
// see pullPublicImage.
func (e *Executor) PullImagePublic(ctx context.Context, imageName string) error {
	return e.pullPublicImage(ctx, imageName)
}

// isLocalRegistryImage checks if an image name targets the local registry.
func (e *Executor) isLocalRegistryImage(imageName string) bool {
	return e.localRegistry != "" && strings.HasPrefix(imageName, e.localRegistry+"/")
}

// runContainer runs the processing container and returns log tail + exit code.
// runContainer runs a processing container with no credentials and no task ID.
// Used for sidecar containers that don't need orphan-sweep identification.
func (e *Executor) runContainer(ctx context.Context, imageName, volName string, payload transport.TaskPayload, timeout time.Duration, cb Callbacks) (string, int, bool, error) {
	return e.runContainerWithCredsInternal(ctx, 0, imageName, volName, payload, timeout, nil, nil, "", cb, nil, nil, nil)
}

// runContainerWithCreds runs the processing container with optional MinIO
// + Harbor credentials injected. Harbor creds only flow through to
// PLATFORM_TASK containers (see the guarded injection below).
// wasOOMKilled reports whether the kernel killed this container for memory.
//
// The worker only DETECTS and REPORTS — it does not decide what happens next.
// Whether to requeue on another host, fail outright, or record the event as
// calibration data is a manager decision (see overlap_scheduler: "All
// scheduling intelligence lives here. Workers are dumb executors").
//
// Best-effort: an inspect failure returns false rather than guessing, because
// a false OOM claim would send the manager rescheduling work that was never
// memory-starved.
// defaultMemoryLimitBytes is the per-container cap when the payload says
// nothing: a share of host RAM, or 4GB if the host size is unknown.
func defaultMemoryLimitBytes(log *slog.Logger) int64 {
	const fallback = int64(4 * 1024 * 1024 * 1024)

	if env := os.Getenv("MOD_WORKER_DEFAULT_MEMORY_MB"); env != "" {
		if v, err := strconv.ParseInt(env, 10, 64); err == nil && v != 0 {
			return v * 1024 * 1024
		}
	}

	host := hostMemoryBytes()
	if host <= 0 {
		return fallback
	}

	fraction := 0.75
	if env := os.Getenv("MOD_WORKER_MEMORY_FRACTION"); env != "" {
		if v, err := strconv.ParseFloat(env, 64); err == nil && v > 0 && v <= 1 {
			fraction = v
		}
	}

	limit := int64(float64(host) * fraction)
	if limit < fallback {
		// Never go BELOW the historical default on a small host — that would
		// be a regression for anyone the 4GB cap was already fitting.
		limit = fallback
	}
	return limit
}

// nodeShmSizeBytes is the /dev/shm size for node containers. Docker's 64MB
// default breaks ML nodes (PyTorch dataloader workers share tensors via shm).
// MOD_WORKER_NODE_SHM_MB overrides the 2048MB default; <=0 leaves Docker's
// default in place (returns 0). Unparseable values fall back to the default.
func nodeShmSizeBytes() int64 {
	const defaultMB = int64(2048)
	mb := defaultMB
	if env := strings.TrimSpace(os.Getenv("MOD_WORKER_NODE_SHM_MB")); env != "" {
		if v, err := strconv.ParseInt(env, 10, 64); err == nil {
			mb = v
		}
	}
	if mb <= 0 {
		return 0
	}
	return mb * 1024 * 1024
}

func (e *Executor) wasOOMKilled(ctx context.Context, containerID string) bool {
	insCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, err := e.docker.ContainerInspect(insCtx, containerID)
	if err != nil || c.State == nil {
		return false
	}
	return c.State.OOMKilled
}

// defaultIODirEnv returns KEY=VALUE entries pointing a node at the /data
// mount's input/output dirs, for each I/O env var the task did not set.
// Server-set values always win.
func defaultIODirEnv(containerEnv map[string]string) []string {
	defaults := [][2]string{
		{"INPUT_DIR", "/data/input"},
		{"INPUT_PATH", "/data/input"},
		{"OUTPUT_DIR", "/data/output"},
		{"OUTPUT_PATH", "/data/output"},
	}
	out := make([]string, 0, len(defaults))
	for _, kv := range defaults {
		if _, ok := containerEnv[kv[0]]; ok {
			continue
		}
		out = append(out, kv[0]+"="+kv[1])
	}
	return out
}

// applyEgressHandle injects a proxy handle's network, env, and DNS into the
// container being built for a proxied task. It is pure: it only mutates the
// three pointers it is given and returns nothing, so it can be unit-tested
// without Docker.
//
// It swaps the container onto the bridge's private internal network
// (containerNetwork), appends the proxy's task env (proxy URLs, allowlist
// marker) to the container env, and sets the container DNS to the proxy's
// NXDOMAIN-only stub so only allowed hosts resolve.
func applyEgressHandle(h *egressProxyHandle, containerNetwork *string, env *[]string, hostConfig *container.HostConfig) {
	*containerNetwork = h.Network
	*env = append(*env, h.Env...)
	hostConfig.DNS = h.DNS
	// NetworkMode was already derived from the pre-proxy network when
	// hostConfig was built; without this the task would bypass the proxy.
	hostConfig.NetworkMode = container.NetworkMode(h.Network)
}

// credsFileBind returns the read-only bind line for the task's storage-creds
// file at the same container path (GSEC Q21.7), or "" when the file is not on
// this host (no storage creds for the task / refresher never started). A
// missing file must not become a docker-created directory, hence the stat.
func credsFileBind(taskID int) string {
	if taskID <= 0 {
		return ""
	}
	p := credsrefresher.TaskCredsPath(taskID)
	if _, err := os.Stat(p); err != nil {
		return ""
	}
	return p + ":" + p + ":ro"
}

// runContainerWithCredsLabel runs the processing container with labels for
// identification in prune sweeps. This is a wrapper that adds labels before
// calling runContainerWithCredsInternal.
func (e *Executor) runContainerWithCredsLabel(ctx context.Context, taskID int, imageName, volName string, payload transport.TaskPayload, timeout time.Duration, minioCreds *transport.MinIOCredentials, harborCreds *transport.HarborCredentials, modelCacheToken string, cb Callbacks, egressOut **transport.EgressReport, ephOut *ephemeralApplied, lockdownOut *LockdownReportOut) (string, int, bool, error) {
	return e.runContainerWithCredsInternal(ctx, taskID, imageName, volName, payload, timeout, minioCreds, harborCreds, modelCacheToken, cb, egressOut, ephOut, lockdownOut)
}

// runContainerWithCredsInternal runs the processing container with optional
// MinIO and Harbor credentials. taskID is used to label the container so the
// orphan sweeper can identify it as our work without making it visible to the
// sidecar reaper.
func (e *Executor) runContainerWithCredsInternal(ctx context.Context, taskID int, imageName, volName string, payload transport.TaskPayload, timeout time.Duration, minioCreds *transport.MinIOCredentials, harborCreds *transport.HarborCredentials, modelCacheToken string, cb Callbacks, egressOut **transport.EgressReport, ephOut *ephemeralApplied, lockdownOut *LockdownReportOut) (string, int, bool, error) {
	env := make([]string, 0, len(payload.ContainerEnv)+5)
	// API_URL is authoritatively set by the worker — NOT the server.
	// The server cannot know what URL each remote worker's containers can
	// reach (different NATs, VPNs, hostnames, deployments). The worker
	// learned its own APIURL at setup time; that's the only URL we trust.
	// Drop anything the server tried to send for API_URL; we overwrite it.
	for k, v := range payload.ContainerEnv {
		if k == "API_URL" {
			continue
		}
		env = append(env, k+"="+v)
	}
	env = append(env, defaultIODirEnv(payload.ContainerEnv)...)
	env = append(env, gpuAssignmentEnv(payload.GPUAssignment, payload.ContainerEnv)...)
	// GSEC FX.13 (R-059): gate the URL we inject (helpers in internal_tls.go).
	// "require" fails the task BEFORE container creation when the learned
	// API URL is plain http; "report" logs once; "off"/absent is unchanged.
	// caBundleBind is applied at the binds assembly further below in this
	// function ("" ⇒ no extra bind).
	caBundleBind := ""
	if e.apiURL != "" {
		mode := ""
		if e.tunables != nil {
			mode = e.tunables.String(internalTLSModeTunableKey, "", "", "off", "report", "require")
		}
		nodeAPIURL, err := decideNodeAPIURL(e.apiURL, mode)
		if err != nil {
			return "", 0, false, err
		}
		reportPlainAPIURL(e.log, mode, nodeAPIURL)
		env = append(env, "API_URL="+nodeAPIURL)
		// Forward the platform CA bundle into the container ONLY when the
		// worker has one (set + readable on this host), so node images can
		// verify internal TLS endpoints (additive; unset ⇒ nothing new).
		if caPath, bind := internalCABundleMount(); caPath != "" {
			env = append(env, "MOD_INTERNAL_CA_BUNDLE="+caPath)
			caBundleBind = bind
		}
	}
	// GF.C2 — point this node's HF clients at the best measured model cache.
	// Adds HF_ENDPOINT=<chosen> + HF_HUB_DISABLE_XET=1 only when the task's
	// env does not already pin HF_ENDPOINT (an API/operator value wins). A
	// chosen endpoint of "" (local cache or upstream direct) still sets
	// HF_HUB_DISABLE_XET=1 and no HF_ENDPOINT. Fails the task only when the
	// egress tunables seal the public upstream and nothing local is eligible
	// (GY.C8) — a sealed install must not fall through to huggingface.co.
	var modelCacheErr error
	// GF.C6.5 — a download that declares an HF_TOKEN is token-gated: it is
	// never routed through another site's cache, and when the effective
	// endpoint is a MOD model cache the declared (raw) token is swapped for
	// the per-task tenant token (see applyModelCacheEnv).
	_, gatedDownload := payload.ContainerEnv["HF_TOKEN"]
	env, modelCacheErr = e.applyModelCacheEnv(ctx, env, taskModelRepoID(payload), modelCacheToken, gatedDownload)
	if modelCacheErr != nil {
		return "", 0, false, modelCacheErr
	}
	if _, err := os.Stat("/dev/dxg"); err == nil {
		env = append(env, "LD_LIBRARY_PATH=/host-gpu-libs:/usr/lib/wsl/lib:/usr/local/lib:/opt/intel/lib:/opt/rocm/lib:"+os.Getenv("LD_LIBRARY_PATH"))
		env = append(env, "LIBVA_DRIVER_NAME=d3d12")
		env = append(env, "HSA_ENABLE_DXG_DETECTION=1")
		// rocprofiler-register aborts GPU apps under WSL.
		env = append(env, "ROCPROFILER_REGISTER_ENABLED=0")
	}

	// Inject per-task storage credentials if provided (for containers that need
	// direct S3 access). These come straight from the API's /credentials grant —
	// versitygw tenant-scoped creds OR MinIO STS creds — never a worker-side
	// exchange (Gate 2, remove-minio-versitygw).
	if minioCreds != nil {
		endpoint := minioCreds.Endpoint
		if strings.HasPrefix(endpoint, "http://") {
			endpoint = endpoint[7:]
		} else if strings.HasPrefix(endpoint, "https://") {
			endpoint = endpoint[8:]
		}
		env = append(env,
			"MINIO_ENDPOINT="+endpoint,
			"MINIO_ACCESS_KEY="+minioCreds.AccessKey,
			"MINIO_SECRET_KEY="+minioCreds.SecretKey,
		)
		// Only inject a session token when the backend actually issued one.
		// versitygw creds have no STS/session token; injecting an empty
		// MINIO_SESSION_TOKEN="" makes some S3 clients send an empty
		// X-Amz-Security-Token header and fail signing. Omit it entirely when
		// absent so long-lived (versitygw) creds sign cleanly.
		if minioCreds.SessionToken != "" {
			env = append(env, "MINIO_SESSION_TOKEN="+minioCreds.SessionToken)
		}
	}

	// Inject Harbor credentials for PLATFORM_TASK containers only —
	// ImageBuilder needs them to `docker login` before `docker build`
	// can pull a FROM base (`ghcr.io/modtim/base-...`).
	// Guarded on PLATFORM_TASK so tenant containers can't exfiltrate
	// Harbor push credentials: only our own system images (that the
	// API explicitly marks PLATFORM_TASK=1 via the helper) receive
	// them. If Vault hasn't given us creds, silently skip — the build
	// will then 401 like it did before, but with a clearer error from
	// docker itself.
	if _, isPlatform := payload.ContainerEnv["PLATFORM_TASK"]; isPlatform && harborCreds != nil {
		// GSEC FX.16 R-023 — build-DAG stages get their registry
		// creds from claim-time secret_refs (platform Harbor PULL refs
		// for the docker-build stages, the 1-day repo-scoped push
		// robot for the push/smoke/sample-test stages). The durable
		// tenant robot from the /credentials grant must never reach
		// them, so skip the blanket injection for build-DAG tasks
		// entirely (no stage reads HARBOR_* from this injection).
		if !IsBuildDAGStage(payload.ContainerEnv) {
			if harborCreds.Username != "" {
				env = append(env,
					"HARBOR_USERNAME="+harborCreds.Username,
					"HARBOR_PASSWORD="+harborCreds.Password,
				)
				if harborCreds.Registry != "" {
					env = append(env, "HARBOR_REGISTRY="+harborCreds.Registry)
				}
			}
		}
	}

	// Inject Vault AppRole bootstrap + Postgres connection topology
	// for PLATFORM_TASK containers that need to authenticate against
	// the platform Vault (e.g. ml-retrain pulls dynamic Postgres creds
	// + service-account MinIO creds at startup). Same trust gate as
	// the Harbor block above — only platform-marked tasks receive
	// these. The Vault AppRole secret_id is the auth bootstrap, not a
	// long-lived service credential; once the container mints a token
	// it operates with that bounded-TTL token instead.
	if _, isPlatform := payload.ContainerEnv["PLATFORM_TASK"]; isPlatform {
		for _, key := range []string{
			"VAULT_ADDR",
			"VAULT_APPROLE_ROLE_ID",
			"VAULT_APPROLE_SECRET_ID",
			"VAULT_DB_ROLE",
			"POSTGRES_HOST",
			"POSTGRES_PORT",
			"POSTGRES_DB",
		} {
			if v := os.Getenv(key); v != "" {
				if _, alreadySet := payload.ContainerEnv[key]; !alreadySet {
					env = append(env, key+"="+v)
				}
			}
		}
	}

	// #83: User containers default to the isolated user network (no
	// access to API, Redis, Vault, Postgres, or the internet).
	// #87: Egress selection is delegated to the pure-function
	// SelectNetworkMode helper so the policy is testable in isolation.
	// A compromised node with empty AllowedEgress lands on the isolated
	// network by default; only explicit declarations (API_KEY / NIM /
	// NEEDS_INTERNET) or a populated AllowedEgress list grant outbound.
	netSel := SelectNetworkMode(payload, e.network, e.userNetwork, e.egressNetwork)
	// #87: opt the allowlist network into the tenant egress proxy bridge when
	// the worker's MOD_EGRESS_PROXY mode is enforce. ApplyEgressProxyMode only
	// rewrites verdicts that came from the AllowedEgress branch (FromAllowlist);
	// it flips Proxied=true so the proxied path below (D.7c) routes the net
	// through the bridge.
	// FX.91 design item 2 (Q77=A): an AllowedEgress verdict is only ever
	// honoured through the per-task proxy bridge. Any non-"enforce" mode
	// (including a hypothetical "audit"/report-only mode, which would leave
	// the task unproxied on bare mod-user-egress) is refused here instead of
	// running unenforced — the old D.7b "unenforced" report path is gone.
	proxyMode := EffectiveEgressProxyMode(e.egressProxyModeForTask(), payload)
	netSel = ApplyEgressProxyMode(netSel, proxyMode)
	if guard := _egressGuard(netSel, proxyMode); guard != nil {
		return "", -1, false, guard
	}
	containerNetwork := netSel.DockerNetwork
	// When a task is pinned onto the shared NIM posture network by name,
	// the network is declared external by the NIM stack's compose and
	// created by ensureExternalNetworks on that host — but a client task
	// can be scheduled before (or independently of) the NIM stack that
	// owns it, so make the attach robust by ensuring the network exists
	// first. Best-effort and idempotent: mirrors ensureExternalNetworks
	// (inspect, create-if-missing); a failure here is non-fatal and the
	// real, specific error surfaces from ContainerCreate/Start below.
	// FX.91 p5: a gov-undeclared (internal-only) verdict must get an
	// Internal:true network when this worker is the one that creates it; an
	// existing network is never recreated, so a non-internal one is reported
	// as a posture gap (WARN once + heartbeat posture status).
	if containerNetwork == stackAPINetwork {
		e.ensureAttachableNetwork(ctx, containerNetwork, netSel.InternalOnly)
	}
	// FX.91 design item 1 (Q77=A): fail closed. If the chosen network is one
	// of the two user-container networks, it must actually exist on this host.
	// When its NetworkCreate at worker start-up failed, executor.New kept the
	// isolated name (no fallback to main), so a user task here would otherwise
	// get a confusing daemon error or, worse, be re-attached to main by an
	// operator workaround. Refuse it with a clear error instead.
	if containerNetwork == e.userNetwork || containerNetwork == e.egressNetwork {
		if netErr := requireUserNetwork(e.docker, containerNetwork); netErr != nil {
			return "", -1, false, netErr
		}
	}
	env = append(env, netSel.EnvAdditions...)
	e.log.Info("network policy",
		"network", containerNetwork,
		"reason", netSel.Reason,
		"allowed_egress_count", len(payload.AllowedEgress),
		"node_kind", payload.NodeKind,
	)
	if len(payload.AllowedEgress) > 0 {
		// Per-domain audit line — operators can grep for this
		// when investigating suspicious outbound.
		for _, domain := range payload.AllowedEgress {
			e.log.Info("egress allowed", "domain", domain, "node_kind", payload.NodeKind)
		}
	}

	// #84: Resource limits — defaults if not specified in payload
	// Memory allocation.
	//
	// This was a hardcoded 4GB cap on every container, and — exactly like the
	// CPU cap — nothing in the API ever populated payload.MemoryLimitMB, so
	// there was no way to raise it. A 31GB worker ran every task in 4GB.
	//
	// Unlike CPU, memory is NOT fairly shared under pressure: the kernel does
	// not slow everyone down, it kills something. So the default is a
	// FRACTION of host RAM rather than all of it, leaving headroom for the
	// worker process, the Docker daemon, and whatever else shares the box. A
	// single task may now be large without a runaway one taking the host with
	// it.
	//
	// This is safer than it once was: the worker now detects kernel OOM kills
	// (State.OOMKilled) and reports them, and the manager clears the pin so a
	// retry lands on a host with memory instead of looping on the starved one.
	//
	//   payload.MemoryLimitMB > 0   explicit cap, wins
	//   payload.MemoryLimitMB < 0   uncapped (Memory=0), platform only
	//   MOD_WORKER_DEFAULT_MEMORY_MB  operator default
	//   MOD_WORKER_MEMORY_FRACTION    share of host RAM (default 0.75)
	memoryLimit := defaultMemoryLimitBytes(e.log)
	if payload.MemoryLimitMB != 0 {
		memoryLimit = int64(payload.MemoryLimitMB) * 1024 * 1024
	}
	if memoryLimit < 0 {
		memoryLimit = 0 // uncapped
	}
	// CPU allocation.
	//
	// This used to be a hard 2-core cap on EVERY container, with no way to
	// raise it: nothing in the API ever populated payload.CPULimit, so a
	// 22-core worker ran every client task on 2 cores and there was no
	// override. It also made contention calibration meaningless — stressor
	// and node under test were both capped at 2, so on a 22-core box they
	// never competed and a "contention" run was identical to isolation.
	//
	// The default is now the host's core count: one task may use the whole
	// machine, and when several run at once the kernel scheduler shares
	// fairly, which yields better throughput than hard per-task caps.
	// Operators who want a hard cap set MOD_WORKER_DEFAULT_CPU_LIMIT.
	//
	//   payload.CPULimit > 0   explicit cap, wins over everything
	//   payload.CPULimit < 0   uncapped (NanoCPUs=0). Platform dispatcher
	//                          only — user payloads cannot set it, because
	//                          they do not author the task payload.
	cpuLimit := float64(goruntime.NumCPU())
	if env := os.Getenv("MOD_WORKER_DEFAULT_CPU_LIMIT"); env != "" {
		if v, err := strconv.ParseFloat(env, 64); err == nil && v != 0 {
			cpuLimit = v
		}
	}
	if payload.CPULimit != 0 {
		cpuLimit = payload.CPULimit
	}
	nanoCPUs := int64(cpuLimit * 1e9)
	if cpuLimit < 0 {
		nanoCPUs = 0 // uncapped
	}
	pidsLimit := int64(4096) // Fork bomb protection
	if payload.PidsLimit > 0 {
		pidsLimit = payload.PidsLimit
	}

	// GPU / accelerator device passthrough. Benchmark containers (and
	// processing containers that need GPU access) set TARGET_KIND in their
	// container_env. Map each kind to the host devices it needs.
	// GPU / accelerator device passthrough. Only mount devices that actually
	// exist on this host — Docker errors on missing device paths. Log which
	// devices are found vs missing so the operator knows what to install.
	var devices []container.DeviceMapping
	targetKind := strings.ToLower(payload.ContainerEnv["TARGET_KIND"])
	// gpu-passthrough-embedded-worker: when the task carries no explicit
	// TARGET_KIND (benchmark path) but we selected a non-cpu gpu_variant image,
	// treat the selected backend as the target kind so the accelerator device is
	// passed in. Without this, an accelerator-queue task whose payload.gpu_required
	// arrived NULL/false (the common case — see selectedGPUBackend) ran on CPU.
	if targetKind == "" {
		if backend := e.selectedGPUBackend(payload); backend != "" {
			targetKind = backend
			e.log.Info("GPU backend derived from selected image variant",
				"backend", backend, "node_kind", payload.NodeKind,
				"gpu_required_payload", payload.GPURequired)
		}
	}
	gpuRequired := payload.GPURequired || targetKind != ""

	addDeviceIfExists := func(path string) bool {
		if _, err := os.Stat(path); err == nil {
			devices = append(devices, container.DeviceMapping{
				PathOnHost: path, PathInContainer: path, CgroupPermissions: "rwm",
			})
			return true
		}
		e.log.Warn("GPU device not found on host — benchmark will run without hardware access", "path", path, "kind", targetKind)
		return false
	}

	// Expand a directory into one DeviceMapping per child device node.
	// Docker CLI's `--device /dev/dri` does this expansion so each
	// card/renderD gets its own cgroup device-controller allowance; the
	// Docker SDK does NOT reliably do the same when given a directory path.
	// Hosts with multiple GPUs can end up with only one device accessible
	// inside the container (symptom we chased: benchmark for index 1
	// returning 0-scores because Level Zero only enumerated one device).
	// Iterating manually makes SDK behaviour match CLI. Hosts with 1 GPU
	// are unaffected.
	//
	// While enumerating, also collect the set of unique group-owner GIDs
	// of the mounted device nodes. /dev/dri/renderD* is typically
	// root:render 0660 — with cap_drop=ALL (which strips CAP_DAC_OVERRIDE)
	// the container's root can't open it unless the render GID is added
	// as a supplementary group. We add those GIDs to HostConfig.GroupAdd
	// below. Pass numeric, not "render", because the benchmark images
	// don't pre-seed a render group and `--group-add render` would fail.
	gidSet := map[uint32]bool{}
	addDeviceDirContents := func(dir string) int {
		entries, err := os.ReadDir(dir)
		if err != nil {
			e.log.Warn("could not enumerate GPU device dir", "dir", dir, "kind", targetKind, "err", err)
			return 0
		}
		count := 0
		for _, entry := range entries {
			if entry.IsDir() {
				continue
			}
			full := filepath.Join(dir, entry.Name())
			info, err := os.Stat(full)
			if err != nil || info.Mode()&os.ModeDevice == 0 {
				continue
			}
			if addDeviceIfExists(full) {
				count++
				if gid, ok := deviceGID(info); ok {
					gidSet[gid] = true
				}
			}
		}
		return count
	}

	_, isWSL := os.Stat("/dev/dxg")
	isWSL2 := isWSL == nil
	var extraBinds []string

	// Narrow /dev/dri exposure to the specific PCI device matching
	// `kind` + TARGET_INDEX. Previous behaviour mounted every card*
	// and renderD* for ANY GPU kind; measurements on example-host showed
	// each unused DRM node the container opens adds ~260 MiB of pinned
	// THP-shmem (i915 GEM state held by the mere fact that /dev/dri/
	// cardN is bound in, regardless of whether Level Zero / HIP uses
	// it). Mounting only the target device cuts that leak out.
	// Returns ("","") if enumeration fails — callers fall back to
	// the old mount-everything behaviour.
	//
	// Index→device mapping caveat: `index` is the position §14.3's card
	// picker (or a legacy TARGET_INDEX) named, which only means the same
	// physical card here if this host's /sys/class/drm BDF-ascending order
	// hasn't shifted since the card was profiled (e.g. no card added/removed
	// since the last heartbeat). When the API-pinned card carries a PCI bus
	// id (`preferBDF`, rocm/intel have no per-card uuid to use instead), it
	// is matched directly against `byBDF` and wins over the positional
	// lookup — exact and immune to reordering. Empty `preferBDF` (older
	// AcceleratorProfile row, or an APU with no discrete PCI device) falls
	// back to the positional `sorted[index]` guess.
	narrowDRIForKind := func(kind string, index int, preferBDF string) (string, string) {
		validDrivers := map[string]bool{}
		switch kind {
		case "rocm":
			validDrivers["amdgpu"] = true
		case "intel":
			validDrivers["i915"] = true
			validDrivers["xe"] = true
		default:
			return "", ""
		}
		entries, err := os.ReadDir("/sys/class/drm")
		if err != nil {
			return "", ""
		}
		type drmDev struct{ bdf, cardPath, renderPath string }
		byBDF := map[string]*drmDev{}
		for _, entry := range entries {
			name := entry.Name()
			if !strings.HasPrefix(name, "card") {
				continue
			}
			// Reject connector subnames like "card0-HDMI-A-1"
			if _, convErr := strconv.Atoi(strings.TrimPrefix(name, "card")); convErr != nil {
				continue
			}
			driverLink, dErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device/driver"))
			if dErr != nil {
				continue
			}
			driverName := filepath.Base(driverLink)
			if !validDrivers[driverName] {
				continue
			}
			devLink, lErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device"))
			if lErr != nil {
				continue
			}
			bdf := filepath.Base(devLink)
			cardPath := "/dev/dri/" + name
			if _, sErr := os.Stat(cardPath); sErr != nil {
				continue
			}
			if _, ok := byBDF[bdf]; !ok {
				byBDF[bdf] = &drmDev{bdf: bdf, cardPath: cardPath}
			}
		}
		for _, entry := range entries {
			name := entry.Name()
			if !strings.HasPrefix(name, "renderD") {
				continue
			}
			devLink, lErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device"))
			if lErr != nil {
				continue
			}
			bdf := filepath.Base(devLink)
			if d, ok := byBDF[bdf]; ok {
				d.renderPath = "/dev/dri/" + name
			}
		}
		if preferBDF != "" {
			if d, ok := byBDF[preferBDF]; ok {
				return d.cardPath, d.renderPath
			}
			// Fall through to the positional guess — the pinned bus id is
			// no longer present under this driver (unplugged/reordered).
		}
		var sorted []*drmDev
		for _, d := range byBDF {
			sorted = append(sorted, d)
		}
		sort.Slice(sorted, func(i, j int) bool { return sorted[i].bdf < sorted[j].bdf })
		if index < 0 || index >= len(sorted) {
			return "", ""
		}
		return sorted[index].cardPath, sorted[index].renderPath
	}

	addNarrowedDRI := func(kind string) bool {
		// §14.3: an API card pin (GPUAssignment) names the exact host
		// index/indices for this kind — prefer those over the single
		// TARGET_INDEX a benchmark/legacy task sets. A nil assignment, or
		// one with no cards of this kind (older API, or the task's pin is
		// for a different kind), falls back to today's single-index path
		// unchanged.
		idx := 0
		indices := assignedIndicesForKind(payload.GPUAssignment, kind)
		pinned := len(indices) > 0
		if !pinned {
			idxStr := payload.ContainerEnv["TARGET_INDEX"]
			idx, _ = strconv.Atoi(idxStr)
			indices = []int{idx}
		} else {
			idx = indices[0]
		}
		var cardPath, renderPath string
		mounted := 0
		for _, i := range indices {
			busID := assignedPCIBusIDForIndex(payload.GPUAssignment, kind, i)
			cp, rp := narrowDRIForKind(kind, i, busID)
			if cp == "" {
				continue
			}
			addDeviceIfExists(cp)
			if rp != "" {
				addDeviceIfExists(rp)
			}
			if mounted == 0 {
				cardPath, renderPath = cp, rp
			}
			mounted++
		}
		if mounted == 0 {
			e.log.Warn("could not narrow /dev/dri — falling back to mounting all nodes",
				"kind", kind, "target_indices", indices)
			return false
		}
		if pinned {
			// §14.3: tell the in-container device selector which of the
			// just-mounted cards to use (HIP/Level-Zero enumerate only
			// what's present, so "0..mounted-1" always means exactly these
			// cards regardless of their host index/BDF). Only for an
			// API-pinned assignment — the legacy TARGET_INDEX path already
			// has its own node-side env composition (see comment below).
			env = append(env, amdIntelVisibleDevicesEnv(kind, mounted, payload.ContainerEnv)...)
		}
		// Because narrowing made the target device(s) the only one(s) visible
		// of its kind in the container, reset TARGET_INDEX to 0 in the
		// container env so the in-container device selector (HIP_VISIBLE_
		// DEVICES, ONEAPI_DEVICE_SELECTOR, etc. — all composed from
		// TARGET_INDEX by the test modules) asks for "device 0 of the
		// filtered view". Without this override a scheduler TARGET_INDEX>0
		// would point at nothing and the binary would silently return
		// zeros (same bug we saw on example-host's Intel #1 / Arc Pro B60).
		// On hosts where narrowing falls back to mount-all, TARGET_INDEX
		// stays as the scheduler set it and works out of the old
		// device-index-in-full-list logic.
		if payload.ContainerEnv != nil {
			payload.ContainerEnv["TARGET_INDEX"] = "0"
		}
		// The `env` slice was flattened from payload.ContainerEnv earlier
		// in this function (line ~611). Mutating the map doesn't update
		// the slice Docker actually receives, so rewrite the entry in
		// place (or append if somehow missing). First fix the bug the
		// map-only mutation was silently failing to address.
		targetIdxFound := false
		for i, kv := range env {
			if strings.HasPrefix(kv, "TARGET_INDEX=") {
				env[i] = "TARGET_INDEX=0"
				targetIdxFound = true
				break
			}
		}
		if !targetIdxFound {
			env = append(env, "TARGET_INDEX=0")
		}
		e.log.Info("narrowed /dev/dri to target device",
			"kind", kind, "target_index_original", idx, "target_index_in_container", 0,
			"card", cardPath, "render", renderPath)
		return true
	}

	// 1900ARC21 (task 5.5a): vendor-narrowed /dev/dri FALLBACK.
	//
	// addNarrowedDRI above picks the ONE card this task was pinned to; when it
	// cannot (no assignment, an index it cannot resolve) the callers used to
	// fall back to addDeviceDirContents("/dev/dri"), which mounts EVERY render
	// node on the host. On example-host (i915 card0 + Arc Pro B60 card1 + R9000
	// card2) that handed a HIP container the Intel render nodes — the same
	// exposure class that produced stack 36 lane 0's libsycl.so.9.0.0 segfault,
	// and each unused DRM node pins ~260 MiB of THP-shmem.
	//
	// Vendor is read from /sys/class/drm/<name>/device/driver (amdgpu / i915 /
	// xe) — the same source narrowDRIForKind uses — never a hard-coded cardN or
	// renderDNNN, which is this box's today-only numbering.
	//
	// Returns false when the host's cards could not be classified at all, so
	// callers keep the old mount-everything behaviour unchanged (a container
	// without /sys mounted, or WSL2, which has neither /sys/class/drm GPU nodes
	// nor /dev/dri — GPUs there are reached through /dev/dxg + librocdxg).
	addVendorDRI := func(accept func(driverName string) bool) bool {
		drmEntries, err := os.ReadDir("/sys/class/drm")
		if err != nil {
			return false
		}
		// Classify cards by driver, then pair renderD* nodes to the owning card
		// by PCI bus id (BDF) -- exactly how narrowDRIForKind resolves /dev/dri/by-
		// path. renderD*/device/driver is not guaranteed to exist; renderD*/device
		// always resolves to the owning card device dir, whose basename is the BDF
		// that also prefixes /dev/dri/by-path/pci-<BDF>.0-card and .0-render.
		keep := map[string]bool{}
		bdfVendor := map[string]string{}
		for _, entry := range drmEntries {
			name := entry.Name()
			if !strings.HasPrefix(name, "card") {
				continue
			}
			// Reject connector subnames like "card0-HDMI-A-1"
			if _, convErr := strconv.Atoi(strings.TrimPrefix(name, "card")); convErr != nil {
				continue
			}
			driverLink, dErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device/driver"))
			if dErr != nil {
				continue
			}
			vendor := filepath.Base(driverLink)
			devLink, lErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device"))
			if lErr == nil {
				bdfVendor[filepath.Base(devLink)] = vendor
			}
			if accept(vendor) {
				keep[name] = true
			}
		}
		for _, entry := range drmEntries {
			name := entry.Name()
			if !strings.HasPrefix(name, "renderD") {
				continue
			}
			devLink, lErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device"))
			if lErr != nil {
				continue
			}
			if v, ok := bdfVendor[filepath.Base(devLink)]; ok && accept(v) {
				keep[name] = true
			}
		}
		// Nothing classifiable at all (no /sys/class/drm GPU nodes readable —
		// a container without /sys bind, an old kernel): report failure so the
		// caller keeps its old mount-everything behaviour.
		if len(keep) == 0 {
			return false
		}
		driEntries, err := os.ReadDir("/dev/dri")
		if err != nil {
			return false
		}
		count := 0
		for _, entry := range driEntries {
			if entry.IsDir() || !keep[entry.Name()] {
				continue
			}
			full := filepath.Join("/dev/dri", entry.Name())
			info, sErr := os.Stat(full)
			if sErr != nil || info.Mode()&os.ModeDevice == 0 {
				continue
			}
			if addDeviceIfExists(full) {
				count++
				if gid, ok := deviceGID(info); ok {
					gidSet[gid] = true
				}
			}
		}
		e.log.Info("mounted /dev/dri narrowed by driver vendor", "kind", targetKind, "nodes", count)
		return true
	}

	if gpuRequired {
		if isWSL2 {
			addDeviceIfExists("/dev/dxg")
			// WSL2 has NO /dev/dri — GPUs are accessed exclusively through /dev/dxg.
			// wslGpuLibBind probes the root-owned NVIDIA user-mode lib dir
			// (Q21.9): unreadable by a non-root worker ⇒ one warning with the
			// fix and no bind (fall back to the other GPU bridge mounts);
			// absent dir (non-WSL) ⇒ no bind, no log. Never fails the task.
			if b := wslGpuLibBind(); b != "" {
				extraBinds = append(extraBinds, b)
			}
			// Mount individual host GPU driver libs — the host's Level Zero driver
			// knows how to talk to GPUs through DXG. Must NOT mount the entire
			// /usr/lib/x86_64-linux-gnu/ dir (it contains libc which would conflict).
			hostLibDir := "/usr/lib/x86_64-linux-gnu"
			gpuLibs := []string{
				"libze_intel_gpu.so.1", "libigdgmm.so.12", "libigdfcl.so.2",
				"libigc.so.2", "libze_loader.so.1",
			}
			for _, lib := range gpuLibs {
				src := hostLibDir + "/" + lib
				if _, err := os.Stat(src); err == nil {
					extraBinds = append(extraBinds, src+":/host-gpu-libs/"+lib+":ro")
				}
			}
			// librocdxg — the ROCm bridge over /dev/dxg (WSL has no /dev/kfd).
			// Without it a ROCm container reports "HIP error 100: no
			// ROCm-capable device", and the card gets quarantined.
			if lib := findRocdxgLib(); lib != "" {
				extraBinds = append(extraBinds, rocdxgBinds(lib)...)
			} else if targetKind == "rocm" {
				e.log.Warn("WSL2 ROCm task but no librocdxg found on the host; the GPU will be invisible to the container (set MOD_ROCDXG_LIB if it is installed somewhere unusual)")
			}
			e.log.Info("WSL2 detected — mounting GPU bridge libs", "kind", targetKind, "binds", len(extraBinds))
		}
		switch targetKind {
		case "rocm":
			addDeviceIfExists("/dev/kfd")
			// 1900ARC21 (task 5.5a): when the per-card pin cannot resolve, fall
			// back to the AMD vendor's render nodes only — never the i915/XPU
			// nodes that share /dev/dri on a box with iGPU + Arc B60 + R9000
			// (that exposure is what let a HIP binary reach an Intel render
			// node and segfault on libsycl.so.9.0.0, stack 36 lane 0). Mount-
			// all survives only as the last resort, for hosts whose /sys/class/
			// drm view is unavailable (no /sys bind), where the vendor cannot
			// be determined at all.
			if !addNarrowedDRI("rocm") && !addVendorDRI(func(d string) bool { return d == "amdgpu" }) {
				addDeviceDirContents("/dev/dri")
			}
		case "intel":
			if !addNarrowedDRI("intel") {
				// 1900ARC21: same vendor narrowing, mirrored — an Intel task
				// must not carry the amdgpu render nodes either.
				if !addVendorDRI(func(d string) bool { return d == "i915" || d == "xe" }) {
					addDeviceDirContents("/dev/dri")
				}
			}
		case "npu":
			addDeviceDirContents("/dev/accel")
		case "cuda":
			// NVIDIA uses nvidia-container-runtime, not raw device mapping.
		default:
			// 1900ARC21: this branch only runs when gpuRequired is already true
			// (a CPU-only container never gets here — the whole block is inside
			// `if gpuRequired`), but the kind is one we have no device recipe
			// for: an accelerator tag we do not size (tpu, inferentia, habana…)
			// or a legacy gpu_required payload with no TARGET_KIND at all. We
			// cannot know WHICH vendor's card that container wants, so the
			// cross-vendor mount must stay broad — pinning it to one driver
			// would silently blind a container that asks for GPU access
			// generically. What we can still drop is anything in /dev/dri that
			// is not a GPU render/card node we can classify, so narrow to the
			// union of known GPU drivers and keep mount-all only when the host
			// exposes no /sys/class/drm view to classify against.
			if !addVendorDRI(func(d string) bool { return d == "amdgpu" || d == "i915" || d == "xe" }) {
				addDeviceDirContents("/dev/dri")
			}
		}
		if len(devices) > 0 {
			e.log.Info("mounting GPU devices", "kind", targetKind, "devices", len(devices))
		}
	}

	// Turn the GIDs we harvested above (from /dev/dri/renderD*, /dev/kfd,
	// /dev/accel, etc.) into supplementary group entries on the container.
	// Without this the container's root cannot open group-owned device
	// files because cap_drop=ALL strips CAP_DAC_OVERRIDE. Skip GID 0 (root
	// is already primary) and GID 44 (video, typically already allowed
	// for card* nodes which are world-readable on most distros).
	var groupAdd []string
	for gid := range gidSet {
		if gid == 0 {
			continue
		}
		groupAdd = append(groupAdd, strconv.FormatUint(uint64(gid), 10))
	}

	binds := []string{volName + ":/data:rw"}
	binds = append(binds, extraBinds...)
	// GSEC R-059: platform CA bundle, read-only, only when the API_URL
	// block above mounted it ("" ⇒ unchanged).
	if caBundleBind != "" {
		binds = append(binds, caBundleBind)
	}
	// GSEC Q21.7: per-task storage-creds file, read-only at the same path
	// inside the container, so opt-in images can re-read refreshed creds.
	// MINIO_* env stays the bootstrap value; the file carries refreshes.
	// Skipped when the file is absent (no storage creds for this task).
	if b := credsFileBind(taskID); b != "" {
		binds = append(binds, b)
	}

	// Opt-in Docker socket passthrough. Only the API's trusted
	// dispatcher sets NeedsDockerSocket=true, and currently only for
	// the HardwareBenchmark image (which times `docker pull alpine`
	// + `docker run alpine true` for its container_startup_ms
	// score). User task payloads come in with this flag false — a
	// compromised processing image can't self-escalate because it
	// doesn't author the task payload. The bind is read-write
	// because `docker pull` needs to modify the local image cache.
	// Gate 3 — the durable checkpoint volume, if this node run has one.
	//
	// Created here rather than by the manager because the manager has no
	// docker daemon, and NOT removed by cleanupVolume: surviving the attempt is
	// the entire purpose. It is reaped when the node run reaches a terminal
	// state, which the manager decides — a worker that reaped it would destroy
	// progress on every retry, which is the failure this exists to prevent.
	if cp := strings.TrimSpace(payload.CheckpointVolume); cp != "" {
		if err := e.ensureCheckpointVolume(ctx, cp); err != nil {
			// Non-fatal on purpose. Checkpointing is an optimisation; refusing
			// to run the node because its scratch space could not be made
			// would turn a slow recovery into no recovery.
			e.log.Warn("checkpoint volume unavailable; running without it",
				"volume", cp, "err", err)
		} else {
			// GC.MWL.C6 — a retry landing on a DIFFERENT host than the one that
			// wrote this checkpoint has a freshly-created, empty local volume
			// at this point (ensureCheckpointVolume above only finds prior
			// content on the SAME host). CheckpointHydrateURL, when the API set
			// one, is that content's presigned GET — best-effort: a failed or
			// missing hydrate must never block the node from starting cold.
			if hu := strings.TrimSpace(payload.CheckpointHydrateURL); hu != "" {
				if herr := e.HydrateCheckpointVolume(ctx, cp, hu); herr != nil {
					e.log.Warn("checkpoint hydrate failed; starting cold",
						"volume", cp, "err", herr)
				}
			}
			binds = append(binds, cp+":"+checkpointMountPath+":rw")
			env = append(env, "MOD_CHECKPOINT_DIR="+checkpointMountPath)
			e.log.Info("checkpoint volume mounted",
				"volume", cp, "path", checkpointMountPath)
		}
	}

	// EXPR 9.3 — the reaper's resume stamp, if this task is a requeue.
	// Deliberately outside the CheckpointVolume block above: the stamp lives in
	// the payload precisely so it survives onto a host that has never seen the
	// volume, which is the case the volume alone cannot cover.
	env = append(env, e.resumeCheckpointEnv(payload)...)

	if payload.NeedsDockerSocket {
		if bind, sok := e.dockerSocketBind(); sok {
			binds = append(binds, bind)
			e.log.Info("bind-mounting docker socket for docker benchmark task",
				"node_kind", payload.NodeKind, "image", imageName, "bind", bind)
		} else {
			e.log.Warn("needs_docker_socket requested but no docker socket available (raw socket missing, or socket proxy failed to start) — running without one",
				"node_kind", payload.NodeKind, "image", imageName)
		}
	}
	hostConfig := &container.HostConfig{
		NetworkMode: container.NetworkMode(containerNetwork),
		Binds:       binds,
		ShmSize:     nodeShmSizeBytes(),
		// #84: Resource limits
		Resources: container.Resources{
			Memory:    memoryLimit,
			NanoCPUs:  nanoCPUs,
			PidsLimit: &pidsLimit,
			Devices:   devices,
		},
		// #85: Security hardening — drop all capabilities, no privileged mode
		CapDrop:     []string{"ALL"},
		Privileged:  false,
		SecurityOpt: []string{"no-new-privileges"},
		GroupAdd:    groupAdd,
	}

	// D.7c: proxied egress path. When the proxy engaged (ApplyEgressProxyMode
	// flipped netSel.Proxied), bring up the per-task egress bridge and route
	// the container through it. setupEgressProxy returns an error on any
	// failure — that is retryable (owner E1), so surface it and skip the task
	// rather than letting it run without the intended egress posture.
	if netSel.Proxied {
		instanceID := newInstanceID()
		h, err := setupEgressProxy(ctx, e.docker, instanceID, e.egressProxyImage, payload.AllowedEgress, e.egressNetwork, e.sidecarLabels())
		if err != nil {
			return "", -1, false, fmt.Errorf("egress proxy unavailable: %w", err)
		}
		// Teardown runs AFTER the task container is removed below (this defer
		// is registered before the container-remove defer), so the proxy's
		// egress logs are captured for the run's report before it is torn down.
		defer func() {
			tctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
			defer cancel()
			r := h.Teardown(tctx, e.docker)
			if egressOut != nil {
				*egressOut = r
			}
		}()
		applyEgressHandle(h, &containerNetwork, &env, hostConfig)

	}
	// worker-mediated-node-credentials: a token-fetch container reaches the
	// worker's local token endpoint at MOD_TASK_TOKEN_URL, whose default host is
	// `host.docker.internal`. On Linux that name only resolves when the
	// container carries the host-gateway mapping, so add it whenever this task
	// was given a token URL. Harmless if an operator repointed the URL at a
	// fixed service name instead — the extra host simply goes unused.
	if _, ok := payload.ContainerEnv["MOD_TASK_TOKEN_URL"]; ok {
		hostConfig.ExtraHosts = append(hostConfig.ExtraHosts, "host.docker.internal:host-gateway")
	}

	// Layer-3 sandbox hardening. Kill switch: DISABLE_HARDENED_SANDBOX=true
	// on the worker falls back to the pre-hardening profile. Hardening is
	// container-scoped — no effect on the host, other containers, or
	// bind-mounted paths (/data, /var/run/docker.sock where present).
	applyHardenedSandbox(hostConfig, hardenedSandboxEnabled())

	// Node-declared ephemeral memory overrides the worker default ShmSize and
	// the sandbox /tmp tmpfs size; must run after applyHardenedSandbox, which
	// is what creates the /tmp tmpfs entry.
	if payload.ShmSizeMB > 0 || payload.TmpfsMB > 0 {
		ephApplied := applyEphemeralResources(hostConfig, payload.ShmSizeMB, payload.TmpfsMB)
		if ephOut != nil {
			*ephOut = ephApplied
		}
		e.log.Info("node ephemeral resources applied",
			"node_kind", payload.NodeKind,
			"shm_size_mb_requested", payload.ShmSizeMB,
			"tmpfs_mb_requested", payload.TmpfsMB,
			"shm_applied_mb", ephApplied.ShmMB,
			"tmpfs_applied_mb", ephApplied.TmpfsMB,
			"ephemeral_clamped", ephApplied.Clamped,
			"ShmSize", hostConfig.ShmSize,
			"tmp_tmpfs", hostConfig.Tmpfs["/tmp"])
	}

	// Runtime selector — gVisor (runsc) when the payload asks for it
	// and the host has it registered; default Docker runtime otherwise.
	if runtime, reason := SelectRuntime(payload.UseSandboxRuntime); runtime != "" {
		hostConfig.Runtime = runtime
		e.log.Info("runtime selected", "runtime", runtime, "reason", reason, "node_kind", payload.NodeKind)
	} else if payload.UseSandboxRuntime {
		e.log.Info("runtime downgrade", "reason", reason, "node_kind", payload.NodeKind)
	}

	// GPU support — NVIDIA uses Docker's DeviceRequests (requires nvidia-container-toolkit).
	// AMD/Intel/NPU use raw --device mounts (already in hostConfig.Devices above).
	// Only add DeviceRequests for NVIDIA — other vendors don't have a Docker
	// runtime that responds to capabilities: [[gpu]], and it crashes Docker.
	if gpuRequired && (targetKind == "cuda" || targetKind == "") {
		// targetKind=="" means a regular GPU processing container (not a
		// benchmark) that declared gpu_required=true. targetKind=="cuda" is a
		// benchmark OR a node whose selected gpu_variant is cuda (derived above).
		// Default to NVIDIA since that's the most common case; AMD/Intel/NPU
		// processing containers set TARGET_KIND (or a matching gpu_variant) and
		// take the raw --device path above instead. Mirrors how the platform's
		// managed GPU stacks (brand-vlm / NIM) attach the GPU on this host:
		// Driver "nvidia" + Capabilities [["gpu"]] via the nvidia runtime.
		// §14.2: only the cards the API pinned at claim, else every GPU.
		hostConfig.DeviceRequests = []container.DeviceRequest{
			nvidiaDeviceRequest(payload.GPUAssignment),
		}
	}

	// Debug dump so we can see exactly what each benchmark container gets
	// when results look wrong. Redacts env values that look like secrets.
	// Disable with LOG_CONTAINER_CREATE=0.
	if os.Getenv("LOG_CONTAINER_CREATE") != "0" {
		var devPaths []string
		for _, d := range devices {
			devPaths = append(devPaths, d.PathOnHost)
		}
		safeEnv := make([]string, 0, len(env))
		for _, kv := range env {
			parts := strings.SplitN(kv, "=", 2)
			if len(parts) != 2 {
				safeEnv = append(safeEnv, kv)
				continue
			}
			key := strings.ToUpper(parts[0])
			if strings.Contains(key, "KEY") || strings.Contains(key, "TOKEN") ||
				strings.Contains(key, "SECRET") || strings.Contains(key, "PASSWORD") ||
				strings.Contains(key, "HMAC") {
				safeEnv = append(safeEnv, parts[0]+"=<redacted>")
			} else {
				safeEnv = append(safeEnv, kv)
			}
		}
		e.log.Info("container create",
			"image", imageName,
			"target_kind", targetKind,
			"gpu_required", gpuRequired,
			"gpu_required_payload", payload.GPURequired,
			"device_count", len(devices),
			"devices", devPaths,
			"device_requests", hostConfig.DeviceRequests,
			"cap_drop", hostConfig.CapDrop,
			"security_opt", hostConfig.SecurityOpt,
			"privileged", hostConfig.Privileged,
			"readonly_rootfs", hostConfig.ReadonlyRootfs,
			"tmpfs", hostConfig.Tmpfs,
			"group_add", hostConfig.GroupAdd,
			"binds", hostConfig.Binds,
			"env", safeEnv,
		)
	}

	// Labels for the main task container so it can be identified in prune
	// sweeps. Unlike sidecar containers, we do NOT include sidecarLabel
	// (com.example.mod-core.sidecar) so this container is not visible
	// to the sidecar reaper — but we DO include worker identity + mod.task.
	labels := make(map[string]string)
	if taskID > 0 {
		labels = e.taskContainerLabels(taskID)
	}

	e.touchImage(ctx, imageName)

	// GSEC FX.115 p1a — node container lockdown (non-root / cap-drop ALL /
	// no-new-privileges), each behind its OWN platform setting, report-first.
	// The task Config is hoisted out of the create call so that layer can
	// write Config.User; HostConfig was already a variable. Every mode read
	// comes off the heartbeat tunables store (worker.node_lockdown_*), whose
	// shipped default is "report": an untouched fleet logs one structured
	// line per container naming what WOULD change and changes nothing.
	// Nodes listed in worker.node_lockdown_exempt_nodes are skipped and
	// reported exempt (FX.115 p3 replaces that allow-list with a per-node
	// exception row + recorded reason).
	// FX.115 p2 — the SAME verdict is handed to the caller as a map
	// (LockdownReportOut) so it rides the task result to the API, which
	// aggregates per (node kind, flag). A FAILED task's report is what
	// answers "what broke under enforce", so it is filled BEFORE the
	// container is created, not only on success.
	// FX.115 p4 — the runtime knob (hosted gVisor) is the one layer that has
	// to ASK the daemon (docker info) whether runsc exists, so the client is
	// handed to it here; an untouched fleet (worker.node_runtime_gvisor off)
	// never probes at all.
	setDockerClient(e.docker)
	taskConfig := &container.Config{
		Image:  imageName,
		Env:    env,
		Cmd:    payload.Cmd,
		Labels: labels,
	}
	lockdown := applyNodeLockdown(taskConfig, hostConfig, e.tunables, payload)
	e.log.Info("lockdown-report", planFields(payload, lockdown)...)
	if lockdownOut != nil {
		*lockdownOut = LockdownReportOut{Fields: planMap(lockdown)}
	}

	resp, err := e.docker.ContainerCreate(ctx,
		taskConfig,
		hostConfig,
		&network.NetworkingConfig{},
		nil,
		"",
	)
	if err != nil {
		return "", -1, false, fmt.Errorf("create container: %w", err)
	}
	containerID := resp.ID

	// Notify resource monitor about the container ID for CPU/RAM tracking
	if cb.OnContainerCreated != nil {
		cb.OnContainerCreated(containerID)
	}

	defer func() {
		rmCtx, rmCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer rmCancel()
		e.docker.ContainerRemove(rmCtx, containerID, container.RemoveOptions{Force: true})
	}()

	if err := e.docker.ContainerStart(ctx, containerID, container.StartOptions{}); err != nil {
		return "", -1, false, fmt.Errorf("start container: %w", err)
	}

	// Inactivity-liveness progress clock: bumped on every container log line
	// (below) and on CPU/GPU activity (in the wait loop). The container is only
	// killed if this goes stale — never for merely running a long time.
	var lastActivity atomic.Int64
	lastActivity.Store(time.Now().UnixNano())

	// Stream logs. Docker multiplexes stdout/stderr in a single reader;
	// we demux via stdcopy into two io.Pipe pairs so we can scan each
	// stream line-by-line in real time. A collector goroutine batches
	// lines and flushes them via e.OnLogBatch (wired by main.go to
	// transport.SendLog → Redis) so the Dashboard's Logs panel can render
	// live container output while the build runs. Previously this
	// goroutine blocked on stdcopy.StdCopy() until EOF and only logged
	// to the worker's own slog sink, which is why the UI always showed
	// "No logs captured yet" for in-progress tasks.
	logReader, err := e.docker.ContainerLogs(ctx, containerID, container.LogsOptions{
		ShowStdout: true,
		ShowStderr: true,
		Follow:     true,
	})
	if err != nil {
		e.log.Warn("failed to attach to container logs", "error", err)
	} else {
		go func() {
			defer logReader.Close()

			stdoutR, stdoutW := io.Pipe()
			stderrR, stderrW := io.Pipe()

			// Pump: demux the multiplexed docker stream into the pipes.
			// Closes the pipe writers on completion so the scanner
			// goroutines below exit cleanly.
			go func() {
				defer stdoutW.Close()
				defer stderrW.Close()
				stdcopy.StdCopy(stdoutW, stderrW, logReader)
			}()

			// Line channel feeding the batch collector. Buffer size
			// balances backpressure vs. memory for noisy containers.
			lineCh := make(chan string, 256)
			var wg sync.WaitGroup
			scan := func(r io.Reader, stream string) {
				defer wg.Done()
				sc := bufio.NewScanner(r)
				// Some containers emit very long JSON lines; raise the
				// default 64 KiB cap to 1 MiB so Scanner doesn't bail
				// early with bufio.ErrTooLong.
				sc.Buffer(make([]byte, 64*1024), 1024*1024)
				for sc.Scan() {
					line := sc.Text()
					if line == "" {
						continue
					}
					// FX.96 (Q82=A): the node's output line is customer content —
					// the worker's own log gets the digest, the raw line goes only
					// to the tenant-scoped log store via cb.OnLogBatch.
					e.log.Info("container", "stream", stream, "log", centralLogLine(line))
					lineCh <- line
				}
			}
			wg.Add(2)
			go scan(stdoutR, "stdout")
			go scan(stderrR, "stderr")
			go func() { wg.Wait(); close(lineCh) }()

			// Collector: batch lines and flush every 500ms or 32 lines.
			batch := make([]string, 0, 32)
			flush := func() {
				if len(batch) == 0 || cb.OnLogBatch == nil {
					batch = batch[:0]
					return
				}
				// Copy so the callback owns its slice.
				cp := make([]string, len(batch))
				copy(cp, batch)
				cb.OnLogBatch(cp)
				batch = batch[:0]
			}
			ticker := time.NewTicker(500 * time.Millisecond)
			defer ticker.Stop()
			for {
				select {
				case l, ok := <-lineCh:
					if !ok {
						flush()
						return
					}
					lastActivity.Store(time.Now().UnixNano()) // log output = progress
					batch = append(batch, l)
					if len(batch) >= 32 {
						flush()
					}
				case <-ticker.C:
					flush()
				}
			}
		}()
	}

	// `timeout` (the node's container_timeout) is the IDLE window when inactivity-
	// liveness is on; legacy fixed wall-clock cap otherwise. Sandbox runs always
	// keep the hard cap (DoS guard).
	idleWindow := timeout
	if idleWindow <= 0 {
		idleWindow = 30 * time.Minute
	}
	useInactivity := _inactivityLivenessEnabled() && payload.Purpose != "wizard_test_run"

	// 5000-line tail: the Containerizer's per-variant adapter JSON alone is
	// ~250 lines; a smaller tail truncated it and broke artifact extraction.
	if !useInactivity {
		// Legacy / sandbox: hard wall-clock cap.
		timeoutCtx, timeoutCancel := context.WithTimeout(ctx, timeout)
		defer timeoutCancel()
		statusCh, errCh := e.docker.ContainerWait(timeoutCtx, containerID, container.WaitConditionNotRunning)
		select {
		case err := <-errCh:
			if err != nil {
				e.killContainer(containerID, fmt.Sprintf("wait error / hard timeout: %v", err))
				return "", -1, false, fmt.Errorf("container timeout/error: %w", err)
			}
			return "", -1, false, nil
		case status := <-statusCh:
			return e.getContainerLogs(ctx, containerID, 5000), int(status.StatusCode),
				e.wasOOMKilled(ctx, containerID), nil
		}
	}

	// INACTIVITY-based liveness: kill ONLY when the container makes no progress
	// (no log output AND no CPU/GPU activity) for idleWindow — never merely for
	// running a long time. A job that keeps logging or using the GPU (e.g. a
	// multi-hour photogrammetry / training run) runs to completion. The outer
	// pipelineCtx is the generous absolute backstop.
	statusCh, errCh := e.docker.ContainerWait(ctx, containerID, container.WaitConditionNotRunning)
	probe := time.NewTicker(15 * time.Second)
	defer probe.Stop()
	var lastDataBytes int64 = -1
	probeCount := 0
	for {
		select {
		case err := <-errCh:
			if err != nil {
				e.killContainer(containerID, fmt.Sprintf("wait error / absolute cap: %v", err))
				return e.getContainerLogs(ctx, containerID, 5000), -1, false, fmt.Errorf("container wait error: %w", err)
			}
			return "", -1, false, nil
		case status := <-statusCh:
			return e.getContainerLogs(ctx, containerID, 5000), int(status.StatusCode),
				e.wasOOMKilled(ctx, containerID), nil
		case <-probe.C:
			now := time.Now()
			probeCount++
			// A container paused by NOAH yield reports no CPU/GPU and no output
			// growth — that is intentional, not a hang. Treat paused as progress
			// so it is never killed for "no progress" while frozen.
			if e.isPaused(containerID) {
				lastActivity.Store(now.UnixNano())
				continue
			}
			if cb.ResourceActive != nil && cb.ResourceActive() {
				lastActivity.Store(now.UnixNano()) // CPU/GPU busy = progress
			}
			// Output-growth signal, throttled to ~60s so a huge /data tree
			// isn't rescanned every tick. /data size changing = the job is
			// writing output = progress (covers silent, disk-bound phases).
			if probeCount%4 == 0 {
				if sz, ok := e.containerDataBytes(ctx, containerID); ok {
					if lastDataBytes >= 0 && sz != lastDataBytes {
						lastActivity.Store(now.UnixNano())
					}
					lastDataBytes = sz
				}
			}
			idle := now.Sub(time.Unix(0, lastActivity.Load()))
			if idle > idleWindow {
				reason := fmt.Sprintf("no progress (no logs, CPU/GPU, or output growth) for %s", idleWindow.Round(time.Second))
				e.killContainer(containerID, reason)
				return e.getContainerLogs(ctx, containerID, 5000), -1, false, fmt.Errorf("container killed: %s", reason)
			}
		}
	}
}

// sidecarErrorResult extracts a *SidecarExitError's stderr (if err wraps
// one) into a *Result carrying only ErrorDetail. Run()'s sidecar failure
// branches (prepare/download/upload) historically returned a nil *Result —
// main.go then had nothing but err.Error() to report. Returning a non-nil
// Result here lets main.go surface the tool's raw stderr as error_detail
// even though the pipeline never produced any other outputs.
// calibration-health-fixtures-and-timeouts §3.
func sidecarErrorResult(err error) *Result {
	var sidecarErr *SidecarExitError
	if errors.As(err, &sidecarErr) && sidecarErr.Stderr != "" {
		return &Result{ErrorDetail: sidecarErr.Stderr}
	}
	return nil
}

// SidecarExitError is returned by runSidecarWithOutput when the sidecar
// container exits non-zero. It keeps stdout and stderr separate (unlike the
// combined string getContainerLogs returns) so a caller that cares — the
// download/upload/prepare failure paths in Run() — can surface the tool's
// OWN stderr rather than whatever got concatenated in front of it.
// calibration-health-fixtures-and-timeouts §3.
type SidecarExitError struct {
	ExitCode int
	Stdout   string
	Stderr   string
}

// Error keeps the pre-existing combined-message shape ("sidecar exited N:
// <logs>") so every caller that only checked err.Error() (log lines, the
// generic error_message report) is unaffected by this type's introduction.
func (s *SidecarExitError) Error() string {
	return fmt.Sprintf("sidecar exited %d: %s", s.ExitCode, s.Stdout+s.Stderr)
}

// getContainerLogsSplit is getContainerLogs but keeps stdout/stderr apart
// instead of concatenating them. tailLines=0 means the full log.
func (e *Executor) getContainerLogsSplit(ctx context.Context, containerID string, tailLines int) (stdout, stderr string) {
	tail := "all"
	if tailLines > 0 {
		tail = fmt.Sprintf("%d", tailLines)
	}
	reader, err := e.docker.ContainerLogs(ctx, containerID, container.LogsOptions{
		ShowStdout: true,
		ShowStderr: true,
		Tail:       tail,
	})
	if err != nil {
		return "", ""
	}
	defer reader.Close()

	var out, errBuf bytes.Buffer
	stdcopy.StdCopy(&out, &errBuf, reader)
	return out.String(), errBuf.String()
}

func (e *Executor) getContainerLogs(ctx context.Context, containerID string, tailLines int) string {
	tail := "all"
	if tailLines > 0 {
		tail = fmt.Sprintf("%d", tailLines)
	}
	reader, err := e.docker.ContainerLogs(ctx, containerID, container.LogsOptions{
		ShowStdout: true,
		ShowStderr: true,
		Tail:       tail,
	})
	if err != nil {
		return ""
	}
	defer reader.Close()

	// Docker multiplexes stdout/stderr with 8-byte headers per frame.
	// Use stdcopy to demux into clean text.
	var stdout, stderr bytes.Buffer
	stdcopy.StdCopy(&stdout, &stderr, reader)
	combined := stdout.String() + stderr.String()
	if combined == "" {
		// Fallback: raw read if stdcopy produced nothing (e.g., TTY mode)
		b, _ := io.ReadAll(io.LimitReader(reader, stageOutLogMaxBytes))
		return string(b)
	}
	return combined
}

// parseUploadManifest parses CSV manifest lines: UPLOAD,hash,filename,size,extension
func parseUploadManifest(output string) []transport.OutputFile {
	var files []transport.OutputFile
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "UPLOAD,") {
			continue
		}
		parts := strings.SplitN(line, ",", 5)
		if len(parts) < 5 {
			continue
		}
		var size int64
		fmt.Sscanf(parts[3], "%d", &size)
		files = append(files, transport.OutputFile{
			ContentHash:   parts[1],
			FileName:      parts[2],
			SizeBytes:     size,
			FileExtension: parts[4],
		})
	}
	return files
}

// retryOp retries an operation with exponential backoff.
// Used for sidecar and image pull operations that may fail during server updates.
func (e *Executor) retryOp(ctx context.Context, maxAttempts int, label string, fn func() error) error {
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if err := fn(); err != nil {
			lastErr = err
			if attempt < maxAttempts-1 {
				delay := time.Duration(1<<uint(attempt)) * 5 * time.Second // 5s, 10s, 20s
				e.log.Warn("retrying operation", "op", label, "attempt", attempt+1, "delay", delay, "error", err)
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(delay):
				}
			}
		} else {
			if attempt > 0 {
				e.log.Info("retry succeeded", "op", label, "attempts", attempt+1)
			}
			return nil
		}
	}
	return lastErr
}

func truncHash(h string) string {
	if len(h) > 12 {
		return h[:12] + "..."
	}
	return h
}

// Close releases the Docker client.
func (e *Executor) Close() error {
	return e.docker.Close()
}

// ---------------------------------------------------------------------
// Stale-container prune
// ---------------------------------------------------------------------
//
// The Worker spawns one-shot containers for each node it executes
// (sidecars from ghcr.io/modtim/* — minio-client copies,
// build-task transforms, helper utilities). The happy-path teardown is
// a deferred ContainerRemove(Force: true) inside runContainer / runSidecar.
// If the worker process is killed mid-task (SIGKILL, OOM, host reboot)
// the deferred remove never runs and the container leaks into
// `docker ps -a` as a `Created` or cleanly-exited remnant with a
// docker-generated random name.
//
// Without housekeeping these accumulate forever — clutter `docker ps`,
// turn the System Status panel into noise, and on disk-tight stage
// hosts they pile up. start-main-server.sh sweeps them at process
// boot, but any session that runs for days without a restart will
// still leak. This in-process sweep covers that gap.
//
// Policy: prune a container only if it is ours — it carries sidecarLabel, or
// (for pre-label leaks) its Config.Image is under
// `ghcr.io/modtim/`. A non-running one is always safe to
// remove. A `running` one is removed only when it is an ORPHAN: same worker
// name, different instance ID, meaning the process that would have cleaned it
// up is gone. restarting/paused are never touched, and a running sidecar
// belonging to this live process is never touched at any age — it may be a
// legitimate multi-hour upload, and the in-process guard already owns it.
//
// Ownership rather than age is the discriminator on purpose. Age cannot
// separate a leak from a large transfer, so any threshold tight enough to
// reclaim leaks promptly would also kill a long render's upload. Age survives
// only as a last-resort backstop (sidecarStuckAfter, floored at 24h) for
// containers whose owner cannot be identified at all.

// sidecarOwner classifies a labeled sidecar relative to the worker running the
// sweep.
type sidecarOwner int

const (
	// ownerSelf — spawned by THIS live process. Never reclaimed while
	// running: it may be a legitimate multi-hour upload, and the in-process
	// guard already owns its liveness.
	ownerSelf sidecarOwner = iota
	// ownerOrphan — same worker name, different instance ID. A previous
	// incarnation of this worker spawned it and then died (SIGKILL, OOM,
	// host reboot) without tearing it down. Nothing is watching it and
	// nothing ever will, so it is reclaimed regardless of age.
	ownerOrphan
	// ownerForeign — a different worker on a shared host. Its own reaper is
	// responsible; we only fall back to the age backstop.
	ownerForeign
)

// isComposeManaged reports whether a container belongs to a docker compose
// project, which the sweep must never touch at ANY state or age.
//
// Managed stacks (stackrunner.go) are deployed with `docker compose up` and
// torn down with `docker compose down`; force-removing a member out from under
// compose leaves the project in a state its own lifecycle commands cannot
// reconcile. Exited ones matter just as much as running ones — the stack `stop`
// op deliberately leaves containers in place so a later `start` can bring them
// back, and reaping those would silently destroy a stopped stack.
//
// This is the guard that keeps the image-prefix fallback honest: a stack built
// on a mod_core image is otherwise indistinguishable from a leaked sidecar.
func isComposeManaged(labels map[string]string) bool {
	return strings.TrimSpace(labels[composeProjectLabel]) != ""
}

// classifyOwner maps a sidecar's labels to its owner class.
func classifyOwner(labels map[string]string, selfWorker, selfInstance string) sidecarOwner {
	if labels[sidecarWorkerLabel] != selfWorker {
		return ownerForeign
	}
	if labels[sidecarInstanceLabel] == selfInstance {
		return ownerSelf
	}
	return ownerOrphan
}

// shouldPruneLabeled is the policy for containers carrying sidecarLabel.
//
// `running` is no longer an unconditional keep. The original policy skipped it
// to avoid killing another worker's in-flight task on a shared host, but that
// made the reaper blind to the one failure mode that actually accumulates: a
// sidecar whose ContainerWait never returned, which stays RUNNING forever and
// is therefore exactly what the sweep must collect.
//
// The discriminator is ownership rather than age, because age cannot tell a
// leak from a large transfer — a 20-hour render's upload is legitimately
// long-lived, and any threshold tight enough to reclaim leaks promptly would
// also kill it. An orphan, by contrast, is provably abandoned: the process
// that would have reaped it no longer exists.
func shouldPruneLabeled(state string, owner sidecarOwner, runningFor time.Duration) bool {
	switch state {
	case "restarting", "paused":
		return false
	case "running":
		switch owner {
		case ownerSelf:
			return false
		case ownerOrphan:
			return true
		default:
			return runningFor > sidecarStuckAfter()
		}
	}
	// Any non-running state: the container is finished, so it is safe to
	// remove whoever owns it.
	return true
}

// isModCoreImage reports whether an image ref sits in a `mod_core` project of
// SOME registry, rather than one specific registry.
//
// The sidecar image is built from harborRegistry(creds.Harbor), which is
// Vault-sourced PER TENANT — so it is whichever Harbor that tenant is bound to:
// registry.example.com, example.com, or the local tenant
// Harbor a deployment runs itself (ENABLE_TENANT_HARBOR, reachable here as
// tenant-harbor.localhost). Hard-coding one host made the sweep a silent no-op
// on every deployment that is not this one — the leaked sidecars were there,
// the reaper simply could not see them.
//
// Host-agnostic matching is a weaker ownership claim than a specific host, and
// that is precisely why the pass using it is opt-in rather than default.
func isModCoreImage(cfgImage string) bool {
	ref := strings.TrimSpace(cfgImage)
	if ref == "" {
		return false
	}
	if h := imageHost(ref); h != "" {
		ref = ref[len(h)+1:]
	}
	return strings.HasPrefix(ref, modCoreProjectPrefix)
}

// shouldPruneContainer is the full policy for UNLABELED containers — those
// leaked by a worker built before sidecarLabel existed, where the image
// namespace is the only identity signal available. Pure, so it can be
// unit-tested without a Docker client.
func shouldPruneContainer(cfgImage, state string, runningFor time.Duration) bool {
	if !isModCoreImage(cfgImage) {
		return false
	}
	// No labels to reason about, so ownership is unknowable and only the age
	// backstop applies. Treated as foreign for that reason.
	return shouldPruneLabeled(state, ownerForeign, runningFor)
}

// PruneStaleContainers force-removes leaked workflow containers in two passes:
// a label-filtered sweep of this worker's own sidecars, then a fallback sweep
// for unlabeled leaks predating sidecarLabel. Returns the number removed.
// Errors are logged and swallowed — a flaky Docker socket must not crash the
// worker, and a failed first pass must not skip the second.
//
// The fallback still needs Config.Image rather than the ContainerList Image
// field, because the latter is a SHA once the source image has been rebuilt
// under the container, which breaks string-prefix matching. That inspect is
// now taken only for containers whose listed image is a bare digest.
func (e *Executor) PruneStaleContainers(ctx context.Context) int {
	pruned := 0
	seen := make(map[string]bool)

	// Pass 1 — labeled sidecars. The daemon filters server-side, so this
	// returns only containers this worker spawned and costs O(sidecars)
	// rather than O(host). ContainerList already carries State and Created,
	// so this pass needs no per-container inspect at all. The label is the
	// identity, which also means it survives an image retag — the exact case
	// the old inspect existed to handle.
	listCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	labeled, err := e.docker.ContainerList(listCtx, container.ListOptions{
		All:     true,
		Filters: filters.NewArgs(filters.Arg("label", sidecarLabel)),
	})
	cancel()
	if err != nil {
		e.log.Warn("prune stale containers: labeled list failed", "err", err)
	}
	orphans := 0
	for _, c := range labeled {
		seen[c.ID] = true
		if isComposeManaged(c.Labels) {
			continue
		}
		owner := classifyOwner(c.Labels, e.workerName, e.instanceID)
		if !shouldPruneLabeled(c.State, owner, containerAge(c.Created)) {
			continue
		}
		if e.removeContainer(ctx, c.ID) {
			pruned++
			if c.State == "running" && owner == ownerOrphan {
				orphans++
			}
		}
	}
	if orphans > 0 {
		// Worth its own line: a running orphan means a previous incarnation
		// of this worker died mid-task, which is a fact about worker
		// stability, not just disk hygiene.
		e.log.Warn("reclaimed running sidecars orphaned by a previous worker instance",
			"count", orphans, "worker", e.workerName)
	}

	// Pass 2 — unlabeled legacy leaks from workers built before sidecarLabel.
	// OFF BY DEFAULT, for the same reason as the legacy volume pass.
	//
	// Identity here can only be inferred from the image, and that inference is
	// weaker than it first appears. The sidecar registry is Vault-sourced per
	// tenant, so the match cannot be pinned to one host and has to accept a
	// `mod_core` project on ANY registry — including a locally-run tenant
	// Harbor. On a host shared with other systems that is a guess, not proof.
	//
	// Every sidecar created from v1.0.33 onward is labeled, so the default
	// path already covers all new leaks; this pass only reaches containers
	// that predate the label. Turn it on deliberately, once, after looking at
	// what is actually on the host.
	//
	// When it does run: the list-level Image is usually the tag itself, which
	// settles membership with no inspect at all — only a bare digest (the
	// post-rebuild case) needs one — and those inspects run concurrently under
	// a small bound. The original version inspected every container on the
	// host serially and blew its own 30s list budget at ~2000 containers, so
	// it pruned nothing precisely when a leak was underway.
	if os.Getenv("MOD_CONTAINER_REAPER_LEGACY") != "1" {
		if pruned > 0 {
			e.log.Info("pruned stale workflow containers", "count", pruned)
		}
		return pruned
	}

	listCtx2, cancel2 := context.WithTimeout(ctx, 60*time.Second)
	all, err := e.docker.ContainerList(listCtx2, container.ListOptions{All: true})
	cancel2()
	if err != nil {
		e.log.Warn("prune stale containers: list failed", "err", err)
		if pruned > 0 {
			e.log.Info("pruned stale workflow containers", "count", pruned)
		}
		return pruned
	}

	var (
		mu sync.Mutex
		wg sync.WaitGroup
		// byName is appended only from this loop; byInspect only from the
		// goroutines under mu. Keeping them separate is what makes the
		// synchronous path lock-free without racing the concurrent one.
		sem       = make(chan struct{}, 8)
		byName    []string
		byInspect []string
	)
	for _, c := range all {
		if seen[c.ID] {
			continue
		}
		// Never reap a compose project member. This pass matches purely on
		// image namespace, so without this a managed stack running on a
		// mod_core image would be force-removed out from under compose.
		if isComposeManaged(c.Labels) {
			continue
		}
		age := containerAge(c.Created)

		// Unambiguous by name — no inspect needed either way.
		if !strings.HasPrefix(c.Image, "sha256:") {
			if shouldPruneContainer(c.Image, c.State, age) {
				byName = append(byName, c.ID)
			}
			continue
		}

		// Digest image: Config.Image is the only reliable signal.
		wg.Add(1)
		go func(id, state string, age time.Duration) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			inspectCtx, icancel := context.WithTimeout(ctx, 5*time.Second)
			info, err := e.docker.ContainerInspect(inspectCtx, id)
			icancel()
			if err != nil {
				return
			}
			cfgImage := ""
			if info.Config != nil {
				cfgImage = info.Config.Image
			}
			if info.State != nil && info.State.Status != "" {
				state = info.State.Status
			}
			if !shouldPruneContainer(cfgImage, state, age) {
				return
			}
			mu.Lock()
			byInspect = append(byInspect, id)
			mu.Unlock()
		}(c.ID, c.State, age)
	}
	wg.Wait()

	for _, id := range append(byName, byInspect...) {
		if e.removeContainer(ctx, id) {
			pruned++
		}
	}

	if pruned > 0 {
		e.log.Info("pruned stale workflow containers", "count", pruned)
	}
	return pruned
}

// containerAge converts a ContainerList Created stamp (unix seconds) into an
// age. Sidecars are created and started in the same breath, so this tracks
// StartedAt closely enough for an hour-scale staleness threshold.
func containerAge(created int64) time.Duration {
	if created <= 0 {
		return 0
	}
	return time.Since(time.Unix(created, 0))
}

// removeContainer force-removes one container, logging and swallowing errors —
// a flaky Docker socket must not crash the worker. Reports whether it went.
func (e *Executor) removeContainer(ctx context.Context, id string) bool {
	rmCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := e.docker.ContainerRemove(rmCtx, id, container.RemoveOptions{Force: true}); err != nil {
		e.log.Debug("prune: remove failed", "id", truncHash(id), "err", err)
		return false
	}
	return true
}

// ---------------------------------------------------------------------
// Stale task-volume prune
// ---------------------------------------------------------------------
//
// Every task gets an ephemeral /data volume (createVolume) that Run removes
// via `defer e.cleanupVolume`. That defer covers ordinary success and ordinary
// failure. It does NOT cover three cases, and on 2026-08-01 all three were
// live at once, leaving 3,661 orphaned volumes holding ~18GB:
//
//   - the worker is SIGKILLed / OOM-killed / the host reboots, so no defer runs
//   - the pipeline hangs forever, so the defer is never reached
//   - the volume is still referenced by a LEAKED container, so VolumeRemove is
//     refused with "volume is in use" (609 times in the logs). The force flag
//     does not override this, and nothing retried afterwards.
//
// This matters beyond disk. prepareDataVolume chmods /data to 0777, justified
// in-comment by the volume being "task-scoped and nuked after upload" — an
// assumption these orphans break. They hold tenant inputs and staged outputs,
// world-readable, indefinitely, outside MinIO's access controls.
//
// Scoping is positive, never subtractive: a volume is a candidate only if WE
// named it, using a prefix nothing else on the host uses (taskVolumePrefix).
// That is a claim we control, unlike the old `<node_kind>_<16hex>` shape, which
// was a guess that any hex-suffixed volume could satisfy — and this host also
// runs comic-catalog, flowise, lister and defense-logic-twin.
//
// The deliberate trade: volumes created before the prefix existed do not match
// and are never reclaimed automatically. That is the point. Nothing already on
// disk has to be guessed about, so the reaper cannot destroy data it did not
// create. Clearing pre-prefix leaks is a one-time operator job, done by
// reviewing an explicit list — not by teaching the reaper to guess.
//
// `docker volume prune` is NOT an acceptable substitute either — most named
// service volumes (postgres/minio data for projects that merely aren't
// running) report as dangling and would be destroyed. Note that a stopped
// service's volume is dangling by definition, so "dangling" never means
// "abandoned" on a host that runs anything besides us.

// isVolumeKept reports whether a volume is explicitly exempt from reaping.
// Checked before every other rule, in every pass, so a stamped volume is
// unreachable by any heuristic no matter how it is named or how long it has
// sat unused.
func isVolumeKept(labels map[string]string) bool {
	v := strings.TrimSpace(labels[keepVolumeLabel])
	return v != "" && v != "0" && !strings.EqualFold(v, "false")
}

// isTaskVolume reports whether a volume is one this system created. Ownership
// is asserted by OUR prefix, not inferred from a generic shape, so a volume
// belonging to any other tool on the host can never match.
func isTaskVolume(name string) bool {
	return strings.HasPrefix(name, taskVolumePrefix)
}

// shouldPruneVolume is the whole policy, in one place.
//
// Every condition is required. `dangling` must already have been applied by
// the caller — an in-use volume is refused by the daemon anyway, but relying
// on that alone would be sloppy. The keep label wins over everything so a
// model-weight cache is untouchable. Compose owns its own volumes. A volume
// created by THIS live process may belong to a task still running. And the age
// floor covers the moment mid-task when the volume is genuinely unreferenced,
// in case the labels did not survive.
func shouldPruneVolume(name string, labels map[string]string, age time.Duration,
	selfWorker, selfInstance string) bool {

	if !isTaskVolume(name) {
		return false
	}
	if isVolumeKept(labels) || isComposeManaged(labels) {
		return false
	}
	if classifyOwner(labels, selfWorker, selfInstance) == ownerSelf {
		return false
	}
	return age > taskVolumeMinAge
}

// volumeAge parses a VolumeList CreatedAt stamp. An unparseable or missing
// stamp yields 0, which reads as "brand new" and so is never reclaimed —
// failing safe rather than deleting on missing metadata.
func volumeAge(createdAt string) time.Duration {
	if createdAt == "" {
		return 0
	}
	t, err := time.Parse(time.RFC3339, createdAt)
	if err != nil {
		return 0
	}
	return time.Since(t)
}

// PruneStaleVolumes removes orphaned per-task data volumes. Returns the number
// removed. Errors are logged and swallowed. Set MOD_VOLUME_REAPER=0 to disable.
func (e *Executor) PruneStaleVolumes(ctx context.Context) int {
	if os.Getenv("MOD_VOLUME_REAPER") == "0" {
		return 0
	}

	listCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	vols, err := e.docker.VolumeList(listCtx, volume.ListOptions{
		Filters: filters.NewArgs(filters.Arg("dangling", "true")),
	})
	cancel()
	if err != nil {
		e.log.Warn("prune stale volumes: list failed", "err", err)
		return 0
	}

	pruned := 0
	for _, v := range vols.Volumes {
		if v == nil {
			continue
		}
		if !shouldPruneVolume(v.Name, v.Labels, volumeAge(v.CreatedAt), e.workerName, e.instanceID) {
			continue
		}
		if e.removeVolume(ctx, v.Name) {
			pruned++
		}
	}
	if pruned > 0 {
		e.log.Info("pruned stale task volumes", "count", pruned)
	}
	return pruned
}

// OnReapCheckpoints wires the manager query. Mirrors OnFenceStacks: the
// executor owns the docker side, the caller owns the transport.
func (e *Executor) OnReapCheckpoints(fn CheckpointReaper) { e.reapCheckpoints = fn }

// CheckpointReaper is how the executor asks the manager which checkpoint
// volumes may go. A func rather than an import so this package does not depend
// on transport, matching how StackFencer is wired.
type CheckpointReaper func(ctx context.Context, volumes []string) ([]string, error)

// PruneCheckpointVolumes removes checkpoint volumes the manager says are done
// with. Called on the same tick as the stale-task sweep.
//
// The asymmetry with task volumes is deliberate: a task volume is this
// worker's to judge, because it created it for one attempt and nothing else
// can want it. A checkpoint volume belongs to a NODE RUN that may still be
// retried, and only the manager knows. So this asks, and a manager that cannot
// answer means nothing is removed — refusing costs disk, reaping wrongly costs
// a six-hour job.
func (e *Executor) PruneCheckpointVolumes(ctx context.Context) int {
	if e.reapCheckpoints == nil {
		return 0
	}
	listCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	vols, err := e.docker.VolumeList(listCtx, volume.ListOptions{
		Filters: filters.NewArgs(filters.Arg("dangling", "true")),
	})
	if err != nil {
		e.log.Debug("checkpoint prune: cannot list volumes", "err", err)
		return 0
	}

	var candidates []string
	for _, v := range vols.Volumes {
		if v == nil || !strings.HasPrefix(v.Name, checkpointVolumePrefix) {
			continue
		}
		// Dangling is necessary but not sufficient: a node between attempts has
		// no container holding its volume, and that is exactly when it must
		// NOT be removed. The manager settles it.
		candidates = append(candidates, v.Name)
	}
	if len(candidates) == 0 {
		return 0
	}

	askCtx, cancelAsk := context.WithTimeout(ctx, 30*time.Second)
	defer cancelAsk()
	reapable, err := e.reapCheckpoints(askCtx, candidates)
	if err != nil {
		e.log.Debug("checkpoint prune: manager did not answer; keeping all",
			"candidates", len(candidates), "err", err)
		return 0
	}

	pruned := 0
	for _, name := range reapable {
		if !strings.HasPrefix(name, checkpointVolumePrefix) {
			continue // only ever remove what we offered
		}
		if e.removeVolume(ctx, name) {
			pruned++
		}
	}
	if pruned > 0 {
		e.log.Info("reclaimed checkpoint volumes", "count", pruned)
	}
	return pruned
}

// removeVolume removes one volume, logging and swallowing errors. Reports
// whether it went. A still-referenced volume is refused by the daemon, which
// is a free safety net an in-flight task cannot lose to.
func (e *Executor) removeVolume(ctx context.Context, name string) bool {
	rmCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if err := e.docker.VolumeRemove(rmCtx, name, false); err != nil {
		e.log.Debug("prune: volume remove failed", "volume", name, "err", err)
		return false
	}
	return true
}

// StartBackgroundJobs runs an initial PruneStaleContainers pass to
// reclaim leaks from a previous worker session, then loops on a
// 30-minute timer. Exits cleanly when ctx is canceled (the caller's
// SIGINT/SIGTERM context). Idempotent / safe to call once after
// Executor construction.
// applyHostLockdown is the worker-start call for the host egress lockdown
// (owner Tim 11-12: LIVE at CE launch). Guarded by the existing platform
// setting (egress.host_lockdown_workers, delivered through the tunables Store
// with the spec's env fallback): off for every worker the list does not name,
// with ZERO exec. Probe first so an incapable box (no CAP_NET_ADMIN, no nft,
// Docker Desktop) never attempts an apply: it logs a WARNING and keeps the
// heartbeat-visible Status. Only Enforced (or Partial with the OD-H1 opt-in)
// applies the table. Never fatal: a failed apply logs and continues.
func (e *Executor) applyHostLockdown(ctx context.Context) {
	workerName := e.workerName
	if workerName == "" {
	 workerName = workerIdentity()
	}
	// tunables.Store.String: registry key first, env fallback, then default.
	names := ""
	if e.tunables != nil {
		names = e.tunables.String(lockdown.HostLockdownSetting, lockdown.HostLockdownEnv, "")
	}
	acceptWSL := false
	if e.tunables != nil {
		acceptWSL = e.tunables.Bool(lockdown.AcceptWSLSetting, lockdown.AcceptWSLEnv, false)
	}
	if !lockdown.Listed(names, workerName) {
		e.log.Info("host egress lockdown: off", "worker", workerName, "detail", "not named in " + lockdown.HostLockdownSetting)
		return
	}
	// NIT 2 (REV 11-12): every other exec in this file wraps its own
	// context.WithTimeout (executor.go:455, :482, :549, ...), so a hung child
	// can never pin the caller. The lockdown execs (docker info, the nft probe
	// pair, the nft apply) now do too: 10 s, like killContainer's neighbour.
	// PolicyForAPI's own hostname lookup is bounded separately (2 s in wire.go)
	// and runs before the wrap, so the worst-case startup cost stays bounded.
	lc, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	p := lockdown.PolicyForAPI(e.apiURL)
	m := lockdown.DefaultManager()
	st, applied := lockdown.StartupLockdown(m, lc, true, acceptWSL, p)
	if applied {
		e.log.Info("host egress lockdown: enforced", "worker", workerName, "scope", st.Scope, "detail", st.Detail)
		return
	}
	e.log.Warn("host egress lockdown: NOT enforced", "worker", workerName, "host_lockdown", st.HostLockdown, "scope", st.Scope, "detail", st.Detail)
}

func (e *Executor) StartBackgroundJobs(ctx context.Context) {
	go func() {
		// Initial pass at startup. Anything left over from the
		// previous worker process — happy path or crash — clears now.
		// Containers first: a leaked container pins its volume, and
		// VolumeRemove is refused while that reference exists.
		e.PruneStaleContainers(ctx)
		// Owner owner 11-12: the nft egress lockdown must be LIVE at CE launch.
		// The host table is applied once per worker process, at start, when
		// the platform setting names this worker (egress.host_lockdown_workers
		// via the tunables Store; env fallback per the spec). Degrades to a
		// WARNING + the heartbeat-visible Status on a box that cannot lock.
		e.applyHostLockdown(ctx)
		e.PruneEgressNetworks(ctx)
		e.PruneStaleVolumes(ctx)
		e.PruneCheckpointVolumes(ctx)

		ticker := time.NewTicker(30 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				e.PruneStaleContainers(ctx)
				e.PruneEgressNetworks(ctx)
				e.PruneStaleVolumes(ctx)
				e.PruneCheckpointVolumes(ctx)
			}
		}
	}()

	// Worker-side disk-pressure cleanup (diskcleanup.go). The only prior
	// auto-cleanup lived in the API container, which has no docker socket
	// — a permanent no-op. The worker holds the socket and is what actually
	// consumes disk (image pulls + builds), on every host, so cleanup runs
	// here. Threshold-based: build-cache prune over MOD_DISK_AUTO_CLEAN_PCT,
	// image prune over MOD_DISK_DEEP_CLEAN_PCT.
	e.StartDiskCleanup(ctx)

	// 25.5 — worker self-cleanup: report reclaimable docker images / model
	// cache / orphan task dirs, and reclaim only when the API pushes
	// worker.cleanup_reclaim on. See internal/cleanup + cleanup_bridge.go.
	e.startSelfCleanup(ctx)

	// GF.C2 — keep the model-cache scores fresh so the heartbeat reflects
	// current cache health even when no download is in flight.
	if e.modelCache != nil {
		e.modelCache.StartBackgroundRefresh(ctx)
	}
}
