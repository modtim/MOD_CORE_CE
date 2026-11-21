package heartbeat

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/capabilities"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/mesh"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/mining"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/modelcache"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/monitor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tlsconf"
)

// workerID stores the server-assigned worker id. 0 = unknown yet.
// Never overwritten once set, even if the server sends 0 (older API).
var workerID atomic.Int64

// heartbeatHTTPBudget caps ONE heartbeat POST (sendAPI's http.Client budget),
// DERIVED from the beat it must fit inside: interval - heartbeatHTTPMargin,
// floored at heartbeatHTTPFloor, always strictly under the interval. The beat
// is cfg.HeartbeatInterval, default 15 s at config/config.go:156 — NOT 30 s:
// every shipped worker config writes heartbeat_interval: 15s
// (configs/worker.example.yaml:26, scripts/setup-worker.sh:1511, API/worker_setup.sh:1548).
// The budget MUST stay under it or beats overlap: Run() calls h.send(ctx)
// synchronously on each time.NewTicker(h.cfg.HeartbeatInterval) tick
// (heartbeat.go:990), so a beat longer than the interval leaves one worker
// holding two live heartbeat POSTs at once.
// At the shipped 15 s beat the budget is 13 s: no longer the old hardcoded
// 10 s, which was 9x tighter than the transport's 90 s budget
// (transport/api.go:85) and fenced workers on a 12 s API hiccup — countsAsLostContact
// (fence.go) reads a client timeout as genuine unreachability, which advances
// the fence clock. 13 s also stays under the 30 s minFenceLeaseSeconds floor
// (fence.go:67), so it cannot outlive the lease by itself.
// heartbeatHTTPMargin and heartbeatHTTPFloor are vars, not consts, so tests can
// shrink the budget without sleeping for the real numbers; production never
// writes either.
var heartbeatHTTPMargin = 2 * time.Second
var heartbeatHTTPFloor = 13 * time.Second

func heartbeatHTTPBudget(interval time.Duration) time.Duration {
	if interval <= 0 {
		return heartbeatHTTPFloor // cfg not populated (tests, older callers)
	}
	budget := interval - heartbeatHTTPMargin
	if budget < heartbeatHTTPFloor && heartbeatHTTPFloor < interval {
		budget = heartbeatHTTPFloor // short-but-real beat: keep the floor
	}
	if budget >= interval {
		budget = interval / 2 // beat shorter than the floor: stay under the beat
	}
	if budget <= 0 {
		budget = 1 * time.Second // sub-second beats are not supported
	}
	return budget
}

// WorkerID returns the server-assigned worker id, or 0 if the worker has not
// yet received it from the server (i.e. no heartbeat has been answered yet).
func WorkerID() int64 { return workerID.Load() }

// adoptWorkerID applies the "learn once, never overwritten" rule for a
// worker_id decoded from a heartbeat response: it only stores id when id > 0
// and no id has been learned yet (an older API sending 0/absent never clobbers
// a known id). Returns true iff this call adopted (stored) the id, so the
// caller can log the transition exactly once.
func adoptWorkerID(id int64) bool {
	return id > 0 && workerID.CompareAndSwap(0, id)
}

// ServerStateReceiver is called after each heartbeat with the server's coordination signals.
type ServerStateReceiver func(updating, drain, updateAvail bool, latestVersion string)

// CancelTasksReceiver is called after each heartbeat with the IDs of tasks the
// server has expired/failed/cancelled for this worker. The worker HARD-kills
// the matching in-flight container so a user Cancel actually terminates the run
// instead of letting it run to completion (hard-cancel with cascade).
type CancelTasksReceiver func(taskIDs []int)

// HostedStacksProvider returns the compose projects this host is actually
// running, sampled fresh on each heartbeat.
//
// A func rather than a direct executor dependency because heartbeat must not
// import executor — executor already imports transport, and the wiring is done
// in main.go alongside the other receivers.
//
// Each entry is {project, running, total, services}. The manager treats
// running > 0 as "hosted here"; a project whose containers have all exited is
// reported with running == 0 so the manager can tell "this host never had it"
// from "this host has it and it is down".
type HostedStacksProvider func() []map[string]interface{}

// HostBoundPortsProvider returns the per-protocol set of HOST ports bound on
// this worker right now (gw-stack-networking GW.2): {"tcp": [8080, 20000]}.
// nil means "could not say" (no provider, or the daemon query failed) — the
// field is then omitted entirely so the manager keeps its last reading rather
// than reading "no ports bound" off a worker it cannot ask. An EMPTY (non-nil)
// map is a real answer — "looked, nothing is bound" — and is sent as
// host_bound_ports: {} so the manager clears its last reading (a stopped
// container's port must not stay blocked forever).
type HostBoundPortsProvider func() map[string][]int

// HostBoundPortsByProjectProvider returns the same ports attributed to their
// compose project: {project: {"tcp": [8080]}}. Same nil/empty contract as
// HostBoundPortsProvider. Lets the manager exclude a redeploying stack's OWN
// running containers (compose stops them before the new ones bind) from its
// fixed-port conflict check.
type HostBoundPortsByProjectProvider func() map[string]map[string][]int

// ModelDownloadsProvider returns weight downloads in flight on this host.
// Nil means "this worker cannot observe"; an empty slice means "nothing is
// downloading" — opposite claims, and the manager renders them differently.
type ModelDownloadsProvider func() []map[string]interface{}

// CachedModelsProvider returns repos already fully present on this host, so
// the manager can distinguish "about to download" from "already here".
type CachedModelsProvider func() []string

// ModelCacheScoresProvider returns the model-cache manager's latest measured
// scores (GF.C2): one entry per candidate (local/site/other/upstream) this
// worker considered for its last HF_ENDPOINT choice. nil/empty means "no
// measurement yet" (no download has run since this worker started) — never
// "every cache is unhealthy" — so the field is omitted rather than sent empty.
type ModelCacheScoresProvider func() []modelcache.Score

// CredsWarningsProvider returns per-task storage-creds refresh warnings
// (GSEC Q21.7): one entry per live task whose creds refresher is currently
// failing (API down, 5xx, Vault sealed, network). nil/empty → the worker
// reports no creds warnings, so the field contributes nothing to `warnings`.
type CredsWarningsProvider func() []string

// GPUWarningsProvider returns worker-wide GPU health warnings, e.g. an NVML
// "Driver/library version mismatch" (1131): unattended-upgrades bumped the
// nvidia userspace package while the old kernel module is still loaded, so
// nvidia-persistenced cannot start until the host reboots. nil/empty → the
// worker reports no GPU warnings, so a healthy worker's beat is
// byte-identical to before.
type GPUWarningsProvider func() []string

// VaultWarningsProvider returns the degraded-boot Vault warning (GSEC Q21.8):
// non-empty only while this worker has never logged in to Vault successfully,
// meaning it is running on API-mediated creds while the login-retry loop works
// in the background. Empty → contributes nothing to `warnings`.
type VaultWarningsProvider func() string

// VaultDeliveryReceiver is called with the optional `vault` object from each
// heartbeat response (GSEC Q21.8). The API sends it only when the admin
// setting worker.approle_per_worker_enforced is not off; today it carries
// masked status only, and the worker accepts the optional role_id/secret_id
// fields when present, ignoring them otherwise. The receiver validates a
// delivered secret_id against Vault before discarding the old one (the
// two-secret window lives in internal/vault).
type VaultDeliveryReceiver func(*VaultDelivery)

// VaultDelivery is the heartbeat `vault` object shape.
type VaultDelivery struct {
	Status                 string `json:"status"`
	RoleID                 string `json:"role_id,omitempty"`
	SecretID               string `json:"secret_id,omitempty"`
	PrevSecretIDValidUntil string `json:"prev_secret_id_valid_until,omitempty"`
}

// CleanupSummaryProvider returns the worker's latest self-cleanup report
// (megaplan 25.5) shaped for the heartbeat `cleanup` key: reclaimable_bytes,
// reclaimed_bytes, counts by kind, last_run, mode. nil means "no pass has
// run yet" (or the worker predates self-cleanup / has no docker client), so
// the `cleanup` key is omitted rather than sent empty — same omit-don't-fake
// contract as the other providers.
type CleanupSummaryProvider func() map[string]interface{}

// GpuScrubsProvider returns the worker's recent cross-tenant VRAM scrubs
// (FX.80.p2) shaped for the heartbeat `gpu_scrubs` key: a list of
// {card, from_tenant, to_tenant, bytes_zeroed, duration_ms, ok, at}, newest
// last, capped by the worker's ring. nil means "nothing scrubbed yet" (gate
// off, or no tenant switch on a shared card) — the key is then omitted, so
// "no data" is never read as "scrubbing is on and found nothing to do".
type GpuScrubsProvider func() []map[string]interface{}

// NetpolStatusProvider reports what this worker observed about the internal-only
// posture network a FX.91 p5 gov-stamped node is parked on: {network, internal,
// created_by_worker, egress_gap, warned, observations, observed_at}. nil means
// "this worker has not checked the posture yet", so the `netpol` key is omitted
// rather than sent empty — same omit-don't-fake contract as the other providers.
// The `egress_gap` bool is the point: a posture net that exists WITHOUT
// Internal:true still has egress, and the worker cannot fix it (live containers
// are attached), so the manager must be able to see it per box.
type NetpolStatusProvider func() map[string]interface{}

// DiskFloorProvider reports the worker's pre-claim free-disk gate (MAC.1.s13b,
// design 4.6): {state ("above_floor"|"below_floor"|"unknown"), free_gb, needed_gb,
// work_root}. The manager reads it to explain a worker that heartbeats green but
// refuses claims — disk pressure, not a bug. nil means "no probe has run yet"
// (the worker predates the gate, or the first poll round has not finished), so
// the `disk_floor` key is omitted rather than sent as a guess.
type DiskFloorProvider func() map[string]interface{}

// FileSecretsProvider reports whether this worker can materialize stack file
// secrets (13.5b / GX.S10b): true for a host-process worker, and for a
// containerized worker only when its secrets root is a verified tmpfs same-path
// host bind the daemon can see. The manager's placement gate treats the
// reported bool as authoritative, so the value must be HONEST — a container
// without the bind must report false, or the API will place a file-secret
// stack on a box that then refuses it. A func (not a direct executor
// dependency) because heartbeat must not import executor; main.go wires the
// executor's verified check in.
type FileSecretsProvider func() bool

// TunablesReceiver is called with the raw `tunables` map from each heartbeat
// response (GS.PSR.7). nil/empty means the server sent no live overrides
// this beat — the receiver (internal/tunables.Store.Apply) clears back to
// "nothing set" in that case, same as any other beat with no tunables key.
type TunablesReceiver func(map[string]interface{})

// ImagePrepullReceiver is called when the heartbeat response includes images
// that should be pre-pulled. The worker pulls them in the background so they're
// cached before any task claims them. This fires during the profiling window
// (new worker join) and whenever the API sees pending tasks with uncached images.
type ImagePrepullReceiver func(images []string)

// Status represents the worker's current state.
type Status string

const (
	StatusIdle     Status = "idle"
	StatusBusy     Status = "busy"
	StatusDraining Status = "draining"
)

// Heartbeat sends periodic health signals so the manager knows we're alive.
// Direct mode: writes to Redis hash + calls API.
// Remote mode: calls API only.
type Heartbeat struct {
	cfg     *config.Config
	profile capabilities.Profile
	// System-OS telemetry (kernel/distro/package_count/air_gapped). Detected
	// once at agent start; static for the lifetime of the agent process.
	// API uses these to evaluate calibration staleness against the baseline_*
	// snapshot taken at last calibration completion.
	sysState capabilities.SystemState
	version  string       // agent binary version
	status   atomic.Value // Status
	jobs     atomic.Int64 // active job count

	// Server-pushed concurrency cap. Refreshed from each heartbeat
	// response's `max_concurrent_tasks` field. 0 = unlimited (matches
	// the API's `worker_licenses.max_concurrent_tasks` default).
	// Read by the main task-claim loop via MaxConcurrentTasks() to
	// gate WaitForTask calls.
	maxConcurrent atomic.Int64

	// NOAH opportunistic-harvest state. availability is reported up so the
	// server can release pre-placed work and feed the scheduler timeline;
	// onNoah delivers server-pushed settings to the availability evaluator.
	// onNoahMode (24.5.s2) delivers the SERVER-side lend/pause pin — the same
	// three words the local UI's POST /noah/mode accepts, so the Dashboard
	// control and this machine's own tray switch drive one verdict.
	noahAvail atomic.Value // noahReport
	// noahHandback (24.5.s3) holds the drain/hand-back report — see ReportNoahHandback.
	noahHandback atomic.Value // noahHandback
	onNoah    NoahSettingsReceiver
	onNoahMode NoahModeReceiver

	rdb *redis.Client // nil in remote mode
	log *slog.Logger

	// Resource monitoring for heartbeat snapshots
	sysColl *monitor.SystemCollector

	// Per-task snapshots from active monitors, keyed by task ID
	taskSnapsMu sync.RWMutex
	taskSnaps   map[int]monitor.TaskSnapshot

	// activeTasks is the set of task IDs this worker is ACTUALLY executing.
	//
	// Distinct from taskSnaps, which the resource monitor populates on its own
	// cadence: a task claimed a moment ago may not have been sampled yet, and
	// treating "unsampled" as "not running" would drop a live claim. This set
	// is maintained where the job counter is, so it changes exactly when work
	// starts and stops.
	activeTasks   map[int]bool
	activeTasksMu sync.RWMutex

	// Callback to push server coordination signals to transport/main loop
	onServerState    ServerStateReceiver
	onPrepullReady   ImagePrepullReceiver
	onCancelTasks    CancelTasksReceiver
	onTunables       TunablesReceiver
	hostedStacks     HostedStacksProvider
	stackLoop        bool // worker-controlled stack start is on (review2 L6)
	hostBoundPorts   HostBoundPortsProvider
	hostBoundByProj  HostBoundPortsByProjectProvider
	siteMesh         SiteMesh
	modelDownloads   ModelDownloadsProvider
	cachedModels     CachedModelsProvider
	modelCacheScores ModelCacheScoresProvider
	credsWarnings    CredsWarningsProvider
	vaultWarnings    VaultWarningsProvider
	gpuWarnings      GPUWarningsProvider
	vaultDelivery    VaultDeliveryReceiver
	cleanupSummary   CleanupSummaryProvider
	// gpuScrubs reports the worker's recent cross-tenant VRAM scrubs (FX.80.p2,
	// hosted-cloud isolation). nil = "nothing scrubbed yet" (gate off, or no
	// tenant switch on a shared card), which omits the key rather than sending
	// an empty list — "no data" must not read as "scrubbing found nothing to
	// do". FX.80.p3 ingests it on the API side; nothing here changes the API.
	gpuScrubs GpuScrubsProvider
	// netpolStatus reports the posture observed on the internal-only posture
	// network (FX.91 p5). nil = never checked, which omits the `netpol` key.
	netpolStatus NetpolStatusProvider
	// diskFloor reports the pre-claim free-disk gate reading (MAC.1.s13b).
	// nil = no probe has run yet, which omits the `disk_floor` key.
	diskFloor DiskFloorProvider
	// fileSecrets reports whether THIS worker can actually hand stack file
	// secrets to the docker daemon (13.5b). A host-process worker always can;
	// a containerized worker can only if its secrets root is a verified tmpfs
	// same-path host bind. Nil = "cannot say" (the capability is then reported
	// as before), which keeps an older binary's heartbeat unchanged.
	fileSecrets FileSecretsProvider

	// miningTelemetry reports per-task GPU-utilisation mining-signature
	// counters (FX.135 p4a).  nil = no telemetry wired, so the key is
	// omitted — an older binary must not be read as "miner detected".
	miningTelemetry func() map[string]interface{}

	// stack-ha-reconciliation §3 — self-fencing. See fence.go.
	fenceState

	// add-multi-region-mesh — manager failover. `curURL` is the manager this
	// worker currently talks to (starts at cfg.APIURL). Each heartbeat response
	// carries the approved-manager roster; if `curURL` goes unreachable for
	// `failoverThreshold` consecutive heartbeats, the worker rotates to the next
	// roster entry and pushes it to the transport (so task-claim/report follow).
	failMu   sync.Mutex
	curURL   string
	roster   []string
	hbFails  int
	onRotate func(newURL string)

	// mdns-name-discovery §3 — field-networking site-anchor re-home. onRehome
	// re-runs the site-anchor bring-up when the manager's mesh_rehome epoch
	// changes; rehomeEpoch is the last epoch we acted on, persisted to
	// rehomeStatePath so a restart does not re-home when the address has not
	// moved. onRehome nil ⇒ the worker reads mesh_rehome and does nothing, which
	// is correct while the manager's flag is off (it sends a null directive).
	rehomeMu        sync.Mutex
	onRehome        RehomeReceiver
	rehomeEpoch     string
	rehomeStatePath string

	// lastFetchAt is the UnixNano timestamp of the last time the main task
	// fetch/claim loop in cmd/worker completed a pass (see
	// MarkFetchLoopIteration). Seeded in New() so FetchLoopAge is always
	// meaningful, even before the job loop has run once. Reported on every
	// heartbeat (`last_fetch_at` / `fetch_loop_age_s`) and polled by the
	// in-process stall watchdog in cmd/worker/main.go — a "green but silent"
	// worker keeps heartbeating (this is a separate goroutine) while its
	// fetch loop is deadlocked or wedged on a blocking call, so PENDING
	// tasks it's eligible for sit forever with nothing detecting it.
	lastFetchAt atomic.Int64

	// nowFn is the clock MarkFetchLoopIteration/FetchLoopAge read. nil means
	// time.Now; tests substitute a fake clock (see fetch_loop_test.go) to
	// simulate a stall without a real sleep.
	nowFn func() time.Time

	// lastHBAt/lastHBOk record the outcome of the most recent heartbeat send
	// attempt (see noteHeartbeat), for LastHeartbeat() — read by the
	// localui status page ("connected" / last-heartbeat display). Separate
	// from lastFetchAt: that tracks the task-claim loop, this tracks
	// whether we can actually talk to the manager.
	lastHBAt atomic.Int64
	lastHBOk atomic.Bool

	// FX.62 p3a1 — the outcome of the LAST self-update cycle attempted by THIS
	// process, reported as the heartbeat's `last_update_error`. nil = no cycle
	// attempted (key omitted); pointer to "" = a cycle completed cleanly;
	// pointer to text = that cycle failed. The manager's rolling campaign
	// (services/worker_update_rolling.py) reads this key as its failure signal,
	// so nil and "" are different facts and must stay different on the wire.
	// See update_report.go for the full contract.
	updateErr atomic.Pointer[string]
}

func (h *Heartbeat) now() time.Time {
	if h.nowFn != nil {
		return h.nowFn()
	}
	return time.Now()
}

const failoverThreshold = 3 // consecutive heartbeat failures before rotating

// New creates a heartbeat sender.
func New(cfg *config.Config, profile capabilities.Profile, version string, rdb *redis.Client, log *slog.Logger) *Heartbeat {
	h := &Heartbeat{
		cfg:         cfg,
		profile:     profile,
		sysState:    capabilities.DetectSystemState(),
		version:     version,
		rdb:         rdb,
		log:         log,
		sysColl:     monitor.NewSystemCollector(),
		taskSnaps:   make(map[int]monitor.TaskSnapshot),
		activeTasks: make(map[int]bool),
		curURL:      cfg.APIURL,
	}
	h.status.Store(StatusIdle)
	h.lastFetchAt.Store(time.Now().UnixNano())
	return h
}

// MarkFetchLoopIteration records that the main task fetch/claim loop just
// completed a pass. Called from cmd/worker/main.go's job loop — both at each
// full pass and at each spin of its inner capacity/NOAH gate loops, so a
// worker that is legitimately busy or withheld is never confused with one
// that is stuck.
func (h *Heartbeat) MarkFetchLoopIteration() {
	h.lastFetchAt.Store(h.now().UnixNano())
}

// FetchLoopAge returns how long it has been since the fetch loop last
// completed a pass (or since process start, if the job loop hasn't run yet).
func (h *Heartbeat) FetchLoopAge() time.Duration {
	last := h.lastFetchAt.Load()
	if last == 0 {
		return 0
	}
	return h.now().Sub(time.Unix(0, last))
}

// OnHostedStacks registers the provider whose result is reported as the
// observed half of stack reconciliation. Unset means the worker reports
// nothing, which the manager reads as "this worker cannot observe" rather than
// "this worker hosts nothing" — see the omitempty on hosted_stacks.
func (h *Heartbeat) OnHostedStacks(fn HostedStacksProvider) {
	h.hostedStacks = fn
}

// SetStackLoop advertises stack_loop=true on every beat when this worker runs
// the worker-controlled stack start loop (MOD_STACK_WORKER_START). The manager
// trusts an EMPTY hosted_stacks ("I looked and host nothing") only from such a
// worker (review2 L6). Off: the key is never sent (paid default).
func (h *Heartbeat) SetStackLoop(on bool) {
	h.stackLoop = on
}

// OnHostBoundPorts registers the provider whose result is reported as
// `host_bound_ports` on every beat (gw-stack-networking GW.2). See
// HostBoundPortsProvider for the nil contract.
func (h *Heartbeat) OnHostBoundPorts(fn HostBoundPortsProvider) {
	h.hostBoundPorts = fn
}

// OnHostBoundPortsByProject registers the provider whose result is reported
// as `host_bound_ports_by_project` on every beat — the same bindings
// attributed to their compose project, for the manager's self-exclusion of a
// redeploying stack. See HostBoundPortsByProjectProvider for the contract.
func (h *Heartbeat) OnHostBoundPortsByProject(fn HostBoundPortsByProjectProvider) {
	h.hostBoundByProj = fn
}

// OnCredsWarnings registers the provider whose result is merged into the
// `warnings` field on every beat (GSEC Q21.7). Unset → no creds warnings.
func (h *Heartbeat) OnCredsWarnings(fn CredsWarningsProvider) {
	h.credsWarnings = fn
}

// OnVaultWarnings registers the provider whose result is merged into the
// `warnings` field on every beat (GSEC Q21.8 degraded boot). Unset → no vault
// warnings, so a healthy worker's beat is byte-identical to before.
func (h *Heartbeat) OnVaultWarnings(fn VaultWarningsProvider) {
	h.vaultWarnings = fn
}

// OnGPUWarnings registers the provider whose result is merged into the
// `warnings` field on every beat (1131, GPU driver/library mismatch
// detection). Unset → no GPU warnings, so a healthy worker's beat is
// byte-identical to before.
func (h *Heartbeat) OnGPUWarnings(fn GPUWarningsProvider) {
	h.gpuWarnings = fn
}

// OnVaultDelivery registers the callback that receives the optional `vault`
// object from every heartbeat response (GSEC Q21.8), mirroring OnTunables:
// called on every successfully-decoded beat, with nil when the field is absent.
func (h *Heartbeat) OnVaultDelivery(fn VaultDeliveryReceiver) {
	h.vaultDelivery = fn
}

// appendWarning merges one warning into the beat's `warnings` field, creating
// the slice on first use. Empty warnings contribute nothing.
func appendWarning(data map[string]interface{}, w string) {
	if w == "" {
		return
	}
	var existing []string
	if cur, ok := data["warnings"].([]string); ok {
		existing = cur
	}
	data["warnings"] = append(existing, w)
}

// applyWarnings merges the creds (Q21.7) and vault (Q21.8) provider warnings
// into the beat's `warnings` field. Empty results contribute nothing, so a
// healthy worker's beat is byte-identical to before.
func (h *Heartbeat) applyWarnings(data map[string]interface{}) {
	if h.credsWarnings != nil {
		for _, w := range h.credsWarnings() {
			appendWarning(data, w)
		}
	}
	if h.vaultWarnings != nil {
		appendWarning(data, h.vaultWarnings())
	}
	if h.gpuWarnings != nil {
		for _, w := range h.gpuWarnings() {
			appendWarning(data, w)
		}
	}
}

// OnModelDownloads registers the provider for in-flight weight downloads, so
// a first-run model pull is reportable as a number instead of being inferred
// from a stack that is up but not answering.
// SiteMesh keeps the worker on the site tailnet (see internal/mesh): its
// status rides every heartbeat as `site_mesh`, and the manager's `mesh_site`
// directive is handed back to it.
type SiteMesh interface {
	Report() *mesh.Status
	Apply(*mesh.Directive)
}

// OnSiteMesh registers the site-tailnet reconciler. Unset → the worker neither
// reports site_mesh nor acts on mesh_site (older behaviour).
func (h *Heartbeat) OnSiteMesh(s SiteMesh) { h.siteMesh = s }

func (h *Heartbeat) OnModelDownloads(fn ModelDownloadsProvider) {
	h.modelDownloads = fn
}

// OnCachedModels registers the provider for already-downloaded weights.
func (h *Heartbeat) OnCachedModels(fn CachedModelsProvider) {
	h.cachedModels = fn
}

// OnModelCacheScores registers the provider for the model-cache manager's
// latest measured scores (GF.C2). Unset means the worker predates model-cache
// selection and reports nothing, same reasoning as the other providers above.
func (h *Heartbeat) OnModelCacheScores(fn ModelCacheScoresProvider) {
	h.modelCacheScores = fn
}

// OnCleanupSummary registers the provider whose result is reported as
// `cleanup` on every beat (megaplan 25.5). Unset → the key is omitted; a
// nil provider result (no pass run yet) is also omitted — "not reported yet"
// must not be read as "nothing is reclaimable".
func (h *Heartbeat) OnCleanupSummary(fn CleanupSummaryProvider) {
	h.cleanupSummary = fn
}

// OnGpuScrubs registers the provider whose result is reported as the
// `gpu_scrubs` key on every beat (FX.80.p2). Unset → the key is omitted; a
// nil provider result (nothing scrubbed yet) is also omitted.
func (h *Heartbeat) OnGpuScrubs(fn GpuScrubsProvider) {
	h.gpuScrubs = fn
}

// OnNetpolStatus registers the provider whose result is reported as the
// `netpol` key on every beat (FX.91 p5). Unset → the key is omitted; a nil
// provider result (posture never checked on this box) is also omitted.
func (h *Heartbeat) OnNetpolStatus(fn NetpolStatusProvider) {
	h.netpolStatus = fn
}

// OnDiskFloor registers the provider for the pre-claim free-disk gate reading
// (MAC.1.s13b, design 4.6). Unset → the key is omitted; a nil provider result
// (no probe run yet) is also omitted — "not probed yet" must not read as
// "the volume is above the floor".
func (h *Heartbeat) OnDiskFloor(fn DiskFloorProvider) {
	h.diskFloor = fn
}

// OnFileSecrets registers the provider whose result is reported as the
// `file_secrets` capability on every beat (13.5b / GX.S10b). Unset → the
// capability is reported true, matching the pre-verification behaviour for a
// binary that never wired the check; a wired provider reports the honest
// (containerized ⇒ verified-bind) value so the API's placement gate stops
// placing file-secret stacks on a box that would refuse them.
func (h *Heartbeat) OnFileSecrets(fn FileSecretsProvider) {
	h.fileSecrets = fn
}

// OnMiningTelemetry registers the worker's mining-signature counter so its
// pre-computed GPU-utilisation telemetry is reported as the `mining_telemetry`
// key on every beat (FX.135 p4a).  Unset → the key is omitted so the API
// falls back to its own sweep over TaskResourceSample rows.
func (h *Heartbeat) OnMiningTelemetry(fn func() *mining.Counter) {
	h.miningTelemetry = func() map[string]interface{} {
		c := fn()
		if c == nil {
			return nil
		}
		s := c.Snapshot()
		return map[string]interface{}{
			"sustained_gpu_seconds":   s.SustainedGPUSeconds,
			"sustained_gpu_minutes":   s.SustainedMinutes,
			"gpu_utilization_max_pct": s.GPUUtilMaxPct,
			"gpu_utilization_mean_pct": s.GPUUtilMeanPct,
			"total_samples":           s.TotalSamples,
			"idle_high_util_samples":  s.IdleHighUtilSamples,
			"sustained_high_pct":      s.SustainedHighPct,
		}
	}
}

// hostExecStatusProvider is the package-level seam (993a) mirroring
// cmd/worker's hostExecWorkerIDProvider pattern: hostexec_wire.go sets it
// once at boot; heartbeat.go reads it per beat without importing cmd/worker.
// nil = never wired (older binary) -> the `host_exec` key is omitted.
//
// Unlike hostExecWorkerIDProvider (only ever written before main's job loop
// starts, or from a single-goroutine test), this one IS written concurrently
// with reads in production: main.go starts `go hb.Run(ctx)` (which calls
// send(), which calls hostExecReport() every beat) BEFORE
// startHostExecWiring's goroutine resolves the worker_id and calls
// SetHostExecStatus (hx.18 — wiring waits for the first id-carrying beat).
// hostExecStatusMu guards the var so that write is never a data race against
// the heartbeat loop's reads.
var (
	hostExecStatusMu       sync.RWMutex
	hostExecStatusProvider func() map[string]interface{}
)

// SetHostExecStatus registers the provider whose result is reported as the
// `host_exec` key on every beat (993a). cmd/worker/hostexec_wire.go calls
// this from wireHostExec so heartbeat.go never imports cmd/worker.
func SetHostExecStatus(fn func() map[string]interface{}) {
	hostExecStatusMu.Lock()
	hostExecStatusProvider = fn
	hostExecStatusMu.Unlock()
}

// hostExecReport returns the wired provider's availability map, or nil when
// no provider is wired — nil means "omit the key": an older binary must not
// be read as either available or unavailable. A panic or hang inside the
// provider must not take the heartbeat loop down with it: the provider call
// is recovered (a panic reports as "not wired this beat", same as nil); it
// is also cheap by construction (ComputeStatus does at most a socket stat
// and a geteuid syscall, no network/disk I/O), so no timeout wrapper here.
func hostExecReport() (report map[string]interface{}) {
	hostExecStatusMu.RLock()
	fn := hostExecStatusProvider
	hostExecStatusMu.RUnlock()
	if fn == nil {
		return nil
	}
	defer func() {
		if r := recover(); r != nil {
			report = nil
		}
	}()
	return fn()
}

// fileSecretsCapable returns the honest `file_secrets` capability: the wired
// provider's answer when present, true otherwise (pre-verification behaviour
// for a binary that never wired the check — the presence of the key still
// signals "this release understands file secrets").
func (h *Heartbeat) fileSecretsCapable() bool {
	if h.fileSecrets != nil {
		return h.fileSecrets()
	}
	return true
}

// secretRefreshCapable is the honest `secret_refresh` capability: a constant
// true. Refreshing a stack's secret files in place (the c8-W1 refresh_secret_files
// StackOp in executor/stackrunner.go) is a property of THIS BINARY — it is compiled
// in or it isn't — so there is no provider to query and nothing to probe per box
// (unlike file_secrets, whose value is the live-verified placement answer). The
// API's c8-A refresh gate reads the key: present → refresh in place; ABSENT →
// older binary → redeploy_required. There is deliberately no agent_version
// fallback: absence means not capable.
func secretRefreshCapable() bool { return true }

// OnServerState registers a callback that receives server coordination signals from each heartbeat.
func (h *Heartbeat) OnServerState(fn ServerStateReceiver) {
	h.onServerState = fn
}

// OnCancelTasks registers a callback that receives, from each heartbeat, the
// IDs of this worker's tasks the server has expired/failed/cancelled so the
// worker can hard-kill their containers.
func (h *Heartbeat) OnCancelTasks(fn CancelTasksReceiver) {
	h.onCancelTasks = fn
}

// OnTunables registers a callback that receives the raw `tunables` map from
// every heartbeat response (GS.PSR.7). Wired to a *tunables.Store's Apply
// method so server-pushed worker config is live within two heartbeats
// without a restart.
func (h *Heartbeat) OnTunables(fn TunablesReceiver) {
	h.onTunables = fn
}

// OnManagerRotate registers a callback fired when the worker fails over to a new
// manager — wired to the transport's SetBaseURL so task-claim/report follow.
func (h *Heartbeat) OnManagerRotate(fn func(newURL string)) {
	h.onRotate = fn
}

// OnFetchLoopMarker hands the transport a way to refresh this heartbeat's
// fetch-loop liveness stamp (BUG.B83). The job loop in cmd/worker cannot be
// the only thing that touches lastFetchAt: an idle EMBEDDED worker spends all
// its time inside DirectTransport.WaitForTask's own pub/sub/ticker loop, so
// control never returns to main() and the job loop's marks never fire even
// though the transport is polling /workers/tasks/poll every ~30s. The
// watchdog read a stale stamp on a healthy worker, force-exited it every
// ~11 minutes, and orphaned the claim the API had just handed out.
//
// setter is the transport's SetFetchLoopMarker; this heartbeat passes a
// closure over itself so the transport can never reach or corrupt the
// atomic — it only ever asks it to mark. Called once at wiring time, before
// any goroutine that polls exists.
func (h *Heartbeat) OnFetchLoopMarker(setter func(func())) {
	setter(func() { h.MarkFetchLoopIteration() })
}

// currentURL returns the manager this worker is currently talking to.
func (h *Heartbeat) currentURL() string {
	h.failMu.Lock()
	defer h.failMu.Unlock()
	return h.curURL
}

// noteHeartbeat updates the failover state after a heartbeat attempt. On success
// it stores the latest roster and clears the failure counter. On repeated failure
// it rotates to the next approved manager in the roster (if any) and notifies the
// transport. Returns the (possibly new) current URL.
func (h *Heartbeat) noteHeartbeat(ok bool, roster []string) {
	h.lastHBAt.Store(h.now().UnixNano())
	h.lastHBOk.Store(ok)
	h.failMu.Lock()
	if ok {
		h.hbFails = 0
		if roster != nil {
			h.roster = roster
		}
		h.failMu.Unlock()
		return
	}
	h.hbFails++
	if h.hbFails < failoverThreshold {
		h.failMu.Unlock()
		return
	}
	// Rotate to the next reachable manager. Candidates ALWAYS include cfg.APIURL —
	// the worker's own configured, trusted URL (for the embedded worker that's the
	// internal http://api:80) — so a stale or entirely-unreachable roster can never
	// strand the worker. Round-robin to the next distinct candidate after the
	// current one, cycling back through cfg.APIURL. Previously the roster was the
	// only source, so once the worker rotated onto a dead peer it could never
	// return to a manager it could actually reach — the ~3h embedded-worker outage
	// (roster was dev-api→127.0.0.1 + a no-route IP; the reachable http://api:80
	// was never retried until a manual restart reset curURL to cfg.APIURL).
	cands := h.rotationCandidatesLocked()
	next := nextAfter(cands, h.curURL)
	if next == "" || next == strings.TrimRight(strings.TrimSpace(h.curURL), "/") {
		h.failMu.Unlock() // only candidate is the current (dead) one — keep retrying it
		return
	}
	old := h.curURL
	h.curURL = next
	h.hbFails = 0
	cb := h.onRotate
	h.failMu.Unlock()
	h.log.Warn("manager failover: rotating", "from", old, "to", next)
	if cb != nil {
		cb(next)
	}
}

// rotationCandidatesLocked returns the failover candidates: cfg.APIURL first
// (always present as the trusted fallback), then the approved roster — normalised
// (trimmed, no trailing slash) and de-duplicated. Caller must hold failMu.
func (h *Heartbeat) rotationCandidatesLocked() []string {
	seen := map[string]bool{}
	var out []string
	add := func(u string) {
		u = strings.TrimRight(strings.TrimSpace(u), "/")
		if u != "" && !seen[u] {
			seen[u] = true
			out = append(out, u)
		}
	}
	add(h.cfg.APIURL)
	for _, u := range h.roster {
		add(u)
	}
	return out
}

// nextAfter returns the candidate immediately after cur (round-robin). If cur is
// not in the list (e.g. the worker is stuck on a URL dropped from the roster), it
// returns the first candidate — which is cfg.APIURL — so recovery is guaranteed.
func nextAfter(cands []string, cur string) string {
	if len(cands) == 0 {
		return ""
	}
	curN := strings.TrimRight(strings.TrimSpace(cur), "/")
	idx := -1
	for i, u := range cands {
		if u == curN {
			idx = i
			break
		}
	}
	return cands[(idx+1)%len(cands)]
}

// OnPrepullReady registers a callback that fires when the heartbeat response
// includes images to pre-pull. The worker calls this in a background goroutine.
func (h *Heartbeat) OnPrepullReady(fn ImagePrepullReceiver) {
	h.onPrepullReady = fn
}

// SetStatus updates the worker status reported in heartbeats.
func (h *Heartbeat) SetStatus(s Status)    { h.status.Store(s) }
func (h *Heartbeat) SetActiveJobs(n int64) { h.jobs.Store(n) }

// Status returns the worker status last set by SetStatus (StatusIdle if
// none set yet). Read-only accessor for callers outside this package (e.g.
// the localui status page) that must not call SetStatus themselves.
func (h *Heartbeat) Status() Status {
	s, _ := h.status.Load().(Status)
	if s == "" {
		return StatusIdle
	}
	return s
}

// ActiveJobs returns the current active job count last set by SetActiveJobs.
func (h *Heartbeat) ActiveJobs() int64 { return h.jobs.Load() }

// ActiveTaskIDs returns the set of task IDs this worker is currently
// executing (see AddActiveTask/RemoveActiveTask), never nil.
func (h *Heartbeat) ActiveTaskIDs() []int { return h.activeTaskIDs() }

// LastHeartbeat returns the time of the most recently attempted heartbeat
// send and whether that attempt succeeded. Zero time + false if no attempt
// has completed yet (process just started).
func (h *Heartbeat) LastHeartbeat() (t time.Time, ok bool) {
	ns := h.lastHBAt.Load()
	if ns == 0 {
		return time.Time{}, false
	}
	return time.Unix(0, ns), h.lastHBOk.Load()
}

// NoahSettings is the server-pushed opportunistic-harvest configuration.
type NoahSettings struct {
	Enabled         bool   `json:"enabled"`
	SustainedQuietS int    `json:"sustained_quiet_s"`
	PauseGraceS     int    `json:"pause_grace_s"`
	PluggedInOnly   bool   `json:"plugged_in_only"`
	ScheduleStart   string `json:"schedule_start"`
	ScheduleEnd     string `json:"schedule_end"`
	BatchAppetite   int    `json:"batch_appetite"`
}

// NoahSettingsReceiver is called with settings pushed in a heartbeat response.
type NoahSettingsReceiver func(NoahSettings)

// NoahModeReceiver is called with the server-pushed lend/pause pin (24.5.s2).
// The wire value is availability's own vocabulary already validated on the
// server (lend | pause | none); "" is what the gate clears the pin with.
type NoahModeReceiver func(string)

// noahHandback is the 24.5.s3 drain/hand-back report: when the worker stopped
// lending because a human returned (or because the operator pinned "pause"),
// at is the moment the drain began and tasksRequeued counts the in-flight
// harvest tasks the worker gave back to the manager (requeued, retry charge
// not spent) instead of letting them run on a machine someone is using.
type noahHandback struct {
	at            time.Time
	tasksRequeued int
}

type noahReport struct {
	enabled   bool
	available bool
	reason    string
	nextFree  time.Time
}

// OnNoahSettings registers a callback for server-pushed NOAH settings.
func (h *Heartbeat) OnNoahSettings(fn NoahSettingsReceiver) { h.onNoah = fn }

// OnNoahMode registers a callback for the server-pushed lend/pause pin
// (24.5.s2). The gate hands it to the SAME LocalOverride its local UI writes,
// so "lend this machine" from the Dashboard and from the tray are one switch.
func (h *Heartbeat) OnNoahMode(fn NoahModeReceiver) { h.onNoahMode = fn }

// SetAvailability records the worker's current NOAH availability for
// reporting. nextFree is the learned-availability prediction of when this
// machine will next be free (WS3.24/OD-3, local-only history); pass the
// zero time.Time when unknown, and it is simply omitted from the heartbeat.
func (h *Heartbeat) SetAvailability(enabled, available bool, reason string, nextFree time.Time) {
	h.noahAvail.Store(noahReport{enabled: enabled, available: available, reason: reason, nextFree: nextFree})
}

// ReportNoahHandback (24.5.s3) records that this worker just handed the
// machine back to its human: at is when the drain began (the first task given
// back), tasksRequeued how many in-flight harvest tasks were checkpointed/
// requeued rather than allowed to keep running. The counter is CUMULATIVE for
// the current drain — the gate keeps calling this on every beat while the
// drain is in progress so a task that finishes its grace after the drain
// began is still counted. Passing at == zero time.Time clears the report
// (drain over, nothing to say), which is what omits the key from the
// heartbeat entirely.
func (h *Heartbeat) ReportNoahHandback(at time.Time, tasksRequeued int) {
	if at.IsZero() {
		h.noahHandback.Store(noahHandback{})
		return
	}
	h.noahHandback.Store(noahHandback{at: at, tasksRequeued: tasksRequeued})
}

// NoahHandback returns the current drain/hand-back report. ok is false when
// this worker has never handed work back (or since the report was cleared).
func (h *Heartbeat) NoahHandback() (at time.Time, tasksRequeued int, ok bool) {
	r, ok := h.noahHandback.Load().(noahHandback)
	if !ok || r.at.IsZero() {
		return time.Time{}, 0, false
	}
	return r.at, r.tasksRequeued, true
}

// NoahAvailability returns the most recently reported NOAH availability
// state (see SetAvailability) — read by the localui NOAH settings page.
// Zero values (enabled=false, available=false, reason="") if NOAH has never
// sampled yet (e.g. it's disabled and setAvailable's first "disabled" call
// hasn't landed).
func (h *Heartbeat) NoahAvailability() (enabled, available bool, reason string, nextFree time.Time) {
	r, _ := h.noahAvail.Load().(noahReport)
	return r.enabled, r.available, r.reason, r.nextFree
}

// SumTaskUsage returns the summed CPU% and VRAM(MB) of this worker's own task
// containers, for external-load attribution (host-wide minus own).
func (h *Heartbeat) SumTaskUsage() (cpuPercent, vramMB float64) {
	h.taskSnapsMu.RLock()
	defer h.taskSnapsMu.RUnlock()
	for _, s := range h.taskSnaps {
		cpuPercent += s.CPUPercent
		vramMB += s.VRAMMB
	}
	return
}

// MaxConcurrentTasks returns the most-recent server-pushed concurrency
// cap (0 = unlimited). Updated atomically on each heartbeat response.
// Safe to call from any goroutine.
func (h *Heartbeat) MaxConcurrentTasks() int64 { return h.maxConcurrent.Load() }

// AddActiveTask records that this worker has begun executing a task.
//
// The manager renews a claim only for tasks reported here. Without it renewal
// is keyed on OWNERSHIP — "this worker claimed it" — so a task handed out but
// never received is renewed for ever by the claimant's own heartbeat, the
// reaper can never fire, and the work is wedged until a human intervenes.
func (h *Heartbeat) AddActiveTask(taskID int) {
	h.activeTasksMu.Lock()
	defer h.activeTasksMu.Unlock()
	h.activeTasks[taskID] = true
}

// RemoveActiveTask records that execution has finished, whatever the outcome.
func (h *Heartbeat) RemoveActiveTask(taskID int) {
	h.activeTasksMu.Lock()
	defer h.activeTasksMu.Unlock()
	delete(h.activeTasks, taskID)
}

// activeTaskIDs returns the executing set, never nil.
func (h *Heartbeat) activeTaskIDs() []int {
	h.activeTasksMu.RLock()
	defer h.activeTasksMu.RUnlock()
	out := make([]int, 0, len(h.activeTasks))
	for id := range h.activeTasks {
		out = append(out, id)
	}
	return out
}

// UpdateTaskSnapshot updates the per-task resource snapshot for a running task.
func (h *Heartbeat) UpdateTaskSnapshot(taskID int, snap monitor.TaskSnapshot) {
	h.taskSnapsMu.Lock()
	defer h.taskSnapsMu.Unlock()
	h.taskSnaps[taskID] = snap
}

// RemoveTaskSnapshot removes a task's snapshot when it completes.
func (h *Heartbeat) RemoveTaskSnapshot(taskID int) {
	h.taskSnapsMu.Lock()
	defer h.taskSnapsMu.Unlock()
	delete(h.taskSnaps, taskID)
}

// Run sends heartbeats at the configured interval until ctx is done.
func (h *Heartbeat) Run(ctx context.Context) {
	ticker := time.NewTicker(h.cfg.HeartbeatInterval)
	defer ticker.Stop()

	// Send one immediately
	h.send(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			h.send(ctx)
		}
	}
}

func (h *Heartbeat) send(ctx context.Context) {
	// Build queues as comma-separated string (API expects string, not array)
	queueStr := ""
	for i, q := range h.cfg.Queues {
		if i > 0 {
			queueStr += ","
		}
		queueStr += q
	}

	hostname, _ := os.Hostname()

	// Collect real-time resource snapshot
	sys := h.sysColl.Sample()
	gpu := monitor.SampleGPU(h.profile.GPUType)

	// FX.135 p4a1b — idle-host feed.  Every beat already reads whole-card GPU
	// utilisation; when this worker is executing nothing, that reading IS an
	// idle-host sample, which is what the manager's mining signature looks for
	// (a card pinned near 100% with no MOD task on it).  Unwired Feed is a
	// no-op, so a binary that never wires the counter reports nothing new.
	if h.profile.GPUType != "" && h.profile.GPUType != "none" && h.jobs.Load() == 0 {
		mining.Feed(mining.Sample{
			Timestamp: h.now(),
			GPUUtil:   gpu.UtilizationPct,
			Phase:     "idle",
		})
	}

	h.taskSnapsMu.RLock()
	taskSnaps := make(map[int]monitor.TaskSnapshot, len(h.taskSnaps))
	for k, v := range h.taskSnaps {
		taskSnaps[k] = v
	}
	h.taskSnapsMu.RUnlock()

	activeContainers := int(h.jobs.Load())
	resourceSnapshot := monitor.FormatResourceSnapshot(sys, gpu, taskSnaps, activeContainers, 0)

	// 13.5b / GX.S10b — the file_secrets capability must be HONEST: true only
	// when this worker can actually hand its stack secret files to the docker
	// daemon. A host-process worker always can; a containerized worker can
	// only if its secrets root is a verified tmpfs same-path host bind. The
	// manager's placement gate trusts this bool, so reporting true on a
	// container without the bind would place a file-secret stack on a box that
	// then refuses it. When no provider is wired (older binary) we report true,
	// preserving the pre-verification behaviour.
	fileSecretsCapable := h.fileSecretsCapable()

	data := map[string]interface{}{
		"worker_name":    h.cfg.WorkerName,
		"hostname":       hostname,
		"status":         h.status.Load(),
		"os":             h.profile.OS,
		"arch":           h.profile.Arch,
		"gpu_type":       h.profile.GPUType,
		"gpu_name":       h.profile.GPUName,
		"has_gpu":        h.profile.GPUType != "" && h.profile.GPUType != "none",
		"gpu_vram_mb":    int(h.profile.VRAMGB * 1024),
		"ram_gb":         int(h.profile.RAMGB),
		"cpu_cores":      h.profile.CPUCores,
		"hardware_tags":  h.profile.HardwareTags,
		"accelerators":   h.profile.Accelerators,
		"native_apps":    h.profile.NativeApps,
		"cloud_provider": h.profile.CloudProvider,
		"instance_type":  h.profile.InstanceType,
		"active_jobs":    h.jobs.Load(),
		// ALWAYS present, and non-nil even when empty. Absent means "this
		// worker cannot say" and the manager must keep renewing everything, or
		// a rolling upgrade would expire the claims of every worker still on
		// the old binary. An EMPTY list is the meaningful signal: "I am
		// executing nothing", which is exactly when a stale claim should die.
		"active_task_ids": h.activeTaskIDs(),
		"queues":          queueStr,
		"mode":            string(h.cfg.Mode),
		"agent_version":   h.version,
		// GX.S10b / 13.5b — this worker release materializes stack file secrets
		// and storage files (MOD_SECRETS_DIR / MOD_FILES_DIR). The presence of
		// the key is the "this binary understands file secrets" signal (always
		// present here); the VALUE is the honest capability computed above —
		// true only when the daemon can actually see the worker's secrets root
		// (a containerized worker needs the verified same-path bind), which is
		// the "can I place a file-secret stack on THIS box" signal the API's
		// placement gate trusts, complementing the agent_version gate.
		"file_secrets":      fileSecretsCapable,
		// 30.6.8 / c8-W2 — this binary implements the c8-W1 `refresh_secret_files`
		// StackOp (executor/stackrunner.go), so it can refresh a stack's secret
		// files IN PLACE, with no redeploy and no restart. The key's presence is
		// the "this binary can refresh" signal and its value is a constant true:
		// refresh is a property of the binary, not of config, so there is no
		// provider and nothing to probe (unlike file_secrets, which is honest
		// per-box). The API's c8-A refresh gate reads it: key present → refresh;
		// key ABSENT → older binary → mark redeploy_required. There is
		// deliberately no agent_version fallback here — absence means not capable.
		"secret_refresh":    secretRefreshCapable(),
		"timestamp":         time.Now().UTC().Format(time.RFC3339),
		"resource_snapshot": resourceSnapshot,
		"system_state":      h.sysState,
		// last-completed pass of the task fetch/claim loop — see
		// MarkFetchLoopIteration. Always present (seeded at process start),
		// so the API's stall watchdog can tell "heartbeating but not
		// fetching" apart from "worker predates this field" only by absence,
		// never by a wrong number.
		"last_fetch_at":    time.Unix(0, h.lastFetchAt.Load()).UTC().Format(time.RFC3339),
		"fetch_loop_age_s": h.FetchLoopAge().Seconds(),
		// GY.C2b — this build's FIPS 140-3 crypto mode, reported so the
		// manager's crypto-status view can show it per worker. Report-only:
		// the provider itself is a build-time / GODEBUG concern (the
		// Dockerfile's GOFIPS140 arg or GODEBUG=fips140=on|only), never
		// switched here. ALWAYS present — an older binary omits the block
		// and the manager keeps it "unknown", never reading absence as "off".
		"crypto": cryptoReport(),
		// FX.123.s2 — this box's full-disk-encryption state, reported so the
		// manager's disk-encryption view (CORE/API) can gate compliance work
		// on it. Report-only: nothing here locks/unlocks a volume. Tri-state
		// and NEVER a guess — a probe that fails, times out, answers
		// something unrecognised, or cannot see the answer at all (a WSL
		// worker: its BitLocker state belongs to the Windows host) reports
		// "unknown", which the manager carries the previous reading forward
		// exactly like an older binary that omits the block. Cached, so the
		// beat costs one subprocess per ten minutes, not one per beat.
		"disk_encryption": diskEncReport(),
		// MAC.1.s12 — the honest native-confinement posture (see
		// internal/heartbeat/native_confinement.go): darwin native workers report
		// layer="unsupported" because macOS has no Pdeathsig, no CAP_CHOWN gate,
		// and sandbox-exec is deprecated and unused. Never a claim of confinement
		// this OS does not have.
		"native_confinement": nativeConfinementReport(),
	}

	// GP.17c — wslc capability. Only populated on native Windows workers, so
	// omit it when nil and linux/darwin heartbeats stay byte-identical.
	if h.profile.Wslc != nil {
		data["wslc"] = h.profile.Wslc
	}

	// Where this worker thinks the manager can reach it. Omitted when it
	// cannot be determined, so the manager reads "unknown" rather than a
	// wrong answer — and a worker that predates this field is not mistaken
	// for one claiming to be unreachable.
	if addr := advertiseAddress(h.cfg.APIURL); addr != "" {
		data["advertise_address"] = addr
	}

	// dual-anchor-mesh-federation §2.3 — per-anchor advertise addresses + which
	// anchors this worker currently reaches. The manager's heartbeat registry
	// turns this into site-first / cloud-fallback resolution. Each is a
	// CANDIDATE (route-derived), verified at the point of use by the manager.
	// Omitted when no route exists so an anchor the worker cannot reach is not
	// mistaken for a reachable one, and a single-anchor worker still sends the
	// plain advertise_address above.
	reach := map[string]bool{}
	if site := advertiseAddressTo(h.cfg.SiteAnchorHost); site != "" {
		data["advertise_address_site"] = site
		reach["site"] = true
	}
	if cloud := advertiseAddressTo(h.cfg.CloudAnchorHost); cloud != "" {
		data["advertise_address_cloud"] = cloud
		reach["cloud"] = true
	}
	if len(reach) > 0 {
		data["anchor_reachable"] = reach
	}

	// Site tailnet status: lets the manager record this worker's site address
	// and decide when to hand it a fresh preauth key. Omitted until the first
	// reconcile has run, so "not reported yet" is never read as "needs a key".
	if h.siteMesh != nil {
		if st := h.siteMesh.Report(); st != nil {
			data["site_mesh"] = st
		}
	}

	// Foreign-tailscaled trap (2026-09-28 incident): a stock tailscaled owning
	// the kernel tailscale0 adds a ts-input DROP rule that blackholes mesh
	// TCP on this worker's own tun. Detection is cached ~5 min inside mesh;
	// the field is omitted entirely when clean.
	if w := mesh.ForeignTailscaled(); len(w) > 0 {
		data["warnings"] = w
	}
	h.applyWarnings(data)

	// NOAH availability. Sent only when NOAH is enabled on this worker, so a
	// non-NOAH worker is never mistaken for one that is "unavailable" and its
	// pre-placed work is never released.
	if r, ok := h.noahAvail.Load().(noahReport); ok && r.enabled {
		data["noah_available"] = r.available
		data["noah_reason"] = r.reason
		if !r.nextFree.IsZero() {
			data["noah_next_free_at"] = r.nextFree.Truncate(time.Hour).Format(time.RFC3339)
		}
	}

	// 24.5.s3 — NOAH drain/hand-back. Omitted until the worker actually hands
	// work back, so a manager that has never seen this key sees no change.
	// The manager uses it to tell "the machine went quiet, nothing to do"
	// apart from "the machine went quiet AND we had to give N running jobs
	// back to it" — the latter is the only case where harvest work was
	// interrupted and must be requeued rather than counted as a failure.
	if at, n, ok := h.NoahHandback(); ok {
	 data["noah_handback"] = map[string]interface{}{
	 "at": at.Format(time.RFC3339),
	 "tasks_requeued": n,
	 }
	}

	// Observed stacks. Omitted entirely when this worker has no provider, so a
	// worker that predates this field is not mistaken for one reporting an
	// empty host — the manager must not conclude "hosts nothing" from "said
	// nothing", or a rolling upgrade would look like a fleet-wide stack outage.
	if h.hostedStacks != nil {
		// A nil answer is "I could not look" (main.go returns nil when the
		// Docker query fails) and is omitted: sending [] would read as "this
		// worker hosts nothing" and the worker-controlled start loop would
		// fully redeploy every stack pinned here (review M4/L5, 11-20).
		if observed := h.hostedStacks(); observed != nil {
			data["hosted_stacks"] = observed
		}
	}
	if h.stackLoop {
		data["stack_loop"] = true
	}

	// Host ports bound on this worker (gw-stack-networking GW.2). Omitted
	// when the provider is absent or answered nil — "cannot say" must not be
	// read as "nothing bound" (a daemon hiccup would otherwise clear the
	// manager's last reading of every port on the host). An EMPTY map is a
	// real answer and MUST be sent as {}: once the last publishing container
	// stops, the manager needs the explicit empty set to release the port it
	// still remembers from the previous beat.
	if h.hostBoundPorts != nil {
		if b := h.hostBoundPorts(); b != nil {
			data["host_bound_ports"] = b
		}
	}
	// The per-project attribution of the same bindings (self-exclusion for a
	// redeploy: the manager drops the redeploying stack's OWN project ports
	// from its fixed-port conflict check). Same nil contract: absent or nil
	// → the field is omitted, and the manager treats it as "cannot tell".
	if h.hostBoundByProj != nil {
		if bp := h.hostBoundByProj(); bp != nil {
			data["host_bound_ports_by_project"] = bp
		}
	}

	// Weight downloads in flight. Omitted when this worker has no provider so
	// an older worker is not read as "nothing downloading" — same reasoning as
	// hosted_stacks above.
	if h.modelDownloads != nil {
		dl := h.modelDownloads()
		if dl == nil {
			dl = []map[string]interface{}{}
		}
		data["model_downloads"] = dl
	}

	// Weights already on disk. Lets the manager say "this starts in seconds"
	// instead of leaving a blank panel while it waits to see whether bytes
	// begin arriving.
	if h.cachedModels != nil {
		cm := h.cachedModels()
		if cm == nil {
			cm = []string{}
		}
		data["cached_models"] = cm
	}

	// Model-cache selection scores (GF.C2): the manager's latest measurement of
	// each candidate cache this worker considered for HF_ENDPOINT. Omitted
	// entirely when there is no provider or no measurement yet, so an absent
	// key reads as "no data" and never "every cache is down" — see
	// ModelCacheScoresProvider.
	if h.modelCacheScores != nil {
		if sc := h.modelCacheScores(); len(sc) > 0 {
			data["model_cache_scores"] = sc
		}
	}

	// Host-exec availability (993a): {available, mode, code, message} as
	// computed locally by hostexec.ComputeStatus. Omitted when no provider
	// is wired — an older binary must not read as available or unavailable.
	if hx := hostExecReport(); hx != nil {
		data["host_exec"] = hx
	}

	// Self-cleanup report (megaplan 25.5): what this worker would reclaim
	// and what it actually reclaimed. Omitted until the first pass has run,
	// so "not reported yet" is never read as "nothing reclaimable".
	if h.cleanupSummary != nil {
		if cs := h.cleanupSummary(); cs != nil {
			data["cleanup"] = cs
		}
	}

	// MAC.1.s13b (design 4.6): the pre-claim free-disk gate reading —
	// {state, free_gb, needed_gb, work_root}. Omitted until the first probe,
	// so the manager never reads "the volume is fine" for a worker that has
	// not measured yet; when it IS present and says below_floor, it is the
	// answer to "why is this green worker claiming nothing?".
	if h.diskFloor != nil {
		if df := h.diskFloor(); df != nil {
			data["disk_floor"] = df
		}
	}

	// Cross-tenant VRAM scrubs (FX.80.p2): the last N scrubs this worker ran,
	// each {card, from_tenant, to_tenant, bytes_zeroed, duration_ms, ok, at}.
	// Omitted until the first scrub, so an on-prem fleet (gate off) sends
	// nothing and the manager keeps no stale reading.
	if h.gpuScrubs != nil {
		if sc := h.gpuScrubs(); sc != nil {
			data["gpu_scrubs"] = sc
		}
	}

	// FX.91 p5 posture: what this worker saw on the internal-only posture
	// network. Omitted until the first stamped task has been placed, so "not
	// reported" is never read as "the posture is enforced". An egress_gap true
	// is the honest signal that this box's posture net still has egress and the
	// worker cannot fix it without dropping live containers.
	if h.netpolStatus != nil {
		if np := h.netpolStatus(); np != nil {
			data["netpol"] = np
		}
	}

	// Fence state, so an outage is attributable to fencing rather than to a
	// container crash or an operator action. Omitted entirely until this
	// worker has fenced at least once.
	if fr := h.fenceReport(); fr != nil {
	 data["fence"] = fr
	}

	// FX.62 p3a1 — the outcome of the LAST self-update cycle this process
	// attempted, as the manager's rolling-campaign failure signal
	// (hardware_specs['last_update_error']). Tri-state, same contract as the
	// FX.141 host_bound_ports fix: NO cycle attempted -> key OMITTED (nothing
	// to say; the manager must not read a worker that never drained as a
	// failed upgrade); a cycle that FAILED -> the reason; a cycle that
	// COMPLETED -> the empty string, an explicit "the cycle ended and did not
	// fail", which is what clears the manager's stale reading of a previous
	// failure. See update_report.go.
	if ue := h.updateErrorReport(); ue != nil {
	 data["last_update_error"] = *ue
	}

	// FX.135 p4a — worker-side mining-signature telemetry.  Omitted when no
	// counter is wired so the API falls back to its own sweep over
	// TaskResourceSample rows.
	if h.miningTelemetry != nil {
		if mt := h.miningTelemetry(); mt != nil {
			data["mining_telemetry"] = mt
		}
	}

	// Redis heartbeat (direct mode only)
	if h.rdb != nil {
		if err := h.sendRedis(ctx, data); err != nil {
			h.log.Warn("redis heartbeat failed", "error", err)
		}
	}

	// API heartbeat (all modes). Track success/failure for manager failover.
	roster, resp, err := h.sendAPI(ctx, data)
	if err != nil {
		h.log.Warn("api heartbeat failed", "error", err, "manager", h.currentURL())
	}
	// §3 — the lease is measured in heartbeat success, so evaluate it on the
	// same tick. A separate timer would be a second clock to fall out of step
	// with the thing it is timing. applyHeartbeatResult classifies err/resp so
	// a manager HOST that is reachable but whose API process is down/restarting
	// (any HTTP status, or a refused/reset TCP connection) counts as contact —
	// only a genuine network-level unreachability advances the fence clock.
	h.applyHeartbeatResult(time.Now(), resp, err)

	h.noteHeartbeat(err == nil, roster)
}

func (h *Heartbeat) sendRedis(ctx context.Context, data map[string]interface{}) error {
	key := fmt.Sprintf("worker:%s:heartbeat", h.cfg.WorkerName)
	jsonData, err := json.Marshal(data)
	if err != nil {
		return err
	}
	return h.rdb.Set(ctx, key, jsonData, 60*time.Second).Err()
}

// sendAPI posts one heartbeat. The returned *http.Response is non-nil
// whenever the manager HOST answered with an HTTP response of ANY status —
// including a 502/503/504 while its API process restarts — which the caller
// (applyHeartbeatResult / countsAsLostContact) uses to distinguish "manager
// API down" from "manager host unreachable" for fencing. Only client.Do's
// transport-level error (dial/DNS/TLS/timeout, or a refused/reset TCP
// connection) leaves it nil; local errors (marshal, malformed URL) never
// reach that far and are not fencing-relevant.
func (h *Heartbeat) sendAPI(ctx context.Context, data map[string]interface{}) ([]string, *http.Response, error) {
	body, err := json.Marshal(data)
	if err != nil {
		return nil, nil, err
	}

	// API is single-mounted under /v1 only (no unprefixed backward-compat) —
	// curURL itself stays bare (advertiseAddress and the failover roster both
	// expect a plain host:port/URL), so the prefix is added here, at the one
	// place this package builds an HTTP request.
	url := h.currentURL() + "/v1/workers/heartbeat"
	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Worker-Name", h.cfg.WorkerName)
	if h.cfg.BootstrapToken != "" {
		req.Header.Set("X-Bootstrap-Token", h.cfg.BootstrapToken)
	}

	client := &http.Client{Timeout: heartbeatHTTPBudget(h.cfg.HeartbeatInterval)}
	// GSEC R-059: heartbeat POSTs to the API — trust the platform internal
	// CA when MOD_INTERNAL_CA_BUNDLE names a readable bundle (unset ⇒
	// client untouched, byte-identical behaviour).
	tlsconf.SetClientTLS(client)
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return nil, resp, fmt.Errorf("heartbeat API: HTTP %d", resp.StatusCode)
	}

	// Parse response for server-pushed config and coordination signals
	var hbResp struct {
		DockerRegistry  string   `json:"docker_registry"`
		ServerUpdating  bool     `json:"server_updating"`
		Drain           bool     `json:"drain"`
		UpdateAvailable bool     `json:"update_available"`
		LatestVersion   string   `json:"latest_version"`
		ImagesToPrepull []string `json:"images_to_prepull"`
		MeshManagers    []string `json:"mesh_managers"`
		// mdns-name-discovery §3 — site-anchor re-home directive. nil (JSON null)
		// when the manager's flag is off; the worker then does nothing.
		MeshRehome *MeshRehome `json:"mesh_rehome"`
		// Site-tailnet membership directive (login server + epoch, and a
		// single-use key only when our last site_mesh report needed one).
		MeshSite           *mesh.Directive `json:"mesh_site"`
		MaxConcurrentTasks int             `json:"max_concurrent_tasks"`
		// Tasks the server has expired/failed/cancelled for this worker —
		// hard-kill their containers so a user Cancel actually terminates the run.
		TasksToCancel []int `json:"tasks_to_cancel"`
		// §3.4 — derived by the manager from its OWN grace period, so the two
		// cannot drift apart. The worker never configures this itself: if it
		// did, an operator could set a lease longer than the manager's grace
		// and reintroduce the split-brain window fencing exists to close.
		StackFenceLeaseS int `json:"stack_fence_lease_s"`
		// 2400BUG-1028h J1 — the projects the manager has confirmed THIS worker
		// still owns placement for, sent on the beat that re-established contact
		// (produced by the API's recontact fast path, J2). Absent/empty ⇒ the
		// worker resumes nothing: placement is the manager's decision, never the
		// worker's, and there is no placement epoch anywhere to compare against.
		StackResumeProjects []string `json:"stack_resume_projects"`
		// BUG-1028h J3b — the CLAMPED per-worker spread of the pushed fence
		// lease. The manager owns the bound (lease*(1+pct/100) <= grace -
		// margin, stack_observation.py fence_jitter_push_pct) so the spread
		// can only ever make a worker fence LATER, never past the re-placement
		// headroom. Absent decodes to 0 = no spread, the pre-J3 behaviour.
		StackFenceJitterPct int `json:"stack_fence_jitter_pct"`
		// Server-assigned worker id. Present when the API knows the mapping
		// from this worker_name to its persisted worker row; 0 or absent when
		// the API version predates this field.
		WorkerID int64 `json:"worker_id"`

		// NOAH settings pushed live; nil when the server sends none.
		Noah *NoahSettings `json:"noah"`

		// 24.5.s2 — the server-side lend/pause pin (lend|pause|none). The API
		// sends it every beat (never omits it) so a restarted worker cannot
		// silently keep a human's pause; nil here is only for an older binary's
		// manager, in which case we simply keep the local pin.
		NoahMode string `json:"noah_mode"`

		// GS.PSR.7 — platform_settings-registry-backed worker tunables.
		// Absent/empty when the operator has not set any of them; existing
		// readers then fall back to their local env/default, unchanged.
		Tunables map[string]interface{} `json:"tunables"`

		// GSEC Q21.8 — optional per-worker Vault delivery. Absent (nil) when
		// worker.approle_per_worker_enforced is off; today it carries masked
		// status only, and the secret fields are honoured when present.
		Vault *VaultDelivery `json:"vault"`
	}
	var roster []string
	if err := json.NewDecoder(resp.Body).Decode(&hbResp); err == nil {
		roster = hbResp.MeshManagers

		// Adopt the server-assigned worker id. Only set when the server
		// sends > 0 AND we have not yet learned one (never overwrite with
		// an older API's 0). Log once at Info when first learned.
		if adoptWorkerID(hbResp.WorkerID) {
			h.log.Info("learned server-assigned worker id", "worker_id", hbResp.WorkerID)
		}
		if hbResp.DockerRegistry != "" && h.cfg.DockerRegistry == "" {
			h.cfg.DockerRegistry = hbResp.DockerRegistry
			h.log.Info("received docker_registry from server", "registry", hbResp.DockerRegistry)
		}

		// Server-pushed concurrency cap. The atomic store makes this
		// visible to the main task-claim loop on its next poll, so a
		// platform admin's slider change in the Dashboard takes effect
		// within one heartbeat interval (default 15s — config/config.go:156).
		h.maxConcurrent.Store(int64(hbResp.MaxConcurrentTasks))

		if hbResp.Noah != nil && h.onNoah != nil {
			h.onNoah(*hbResp.Noah)
		}

		// 24.5.s2 — the lend/pause pin. Applied on EVERY successfully-decoded
		// beat, including when the key is absent (NoahMode == ""), because the
		// API sends "none" explicitly to mean "no pin": an operator who cleared
		// the pin (or a worker that just restarted) must not keep a stale pause
		// from a previous snapshot. Same reasoning as the Tunables block above.
		if h.onNoahMode != nil {
			h.onNoahMode(hbResp.NoahMode)
		}

		// GS.PSR.7 — apply on every successfully-decoded heartbeat, including
		// when Tunables is nil/empty, so an operator clearing a platform
		// setting (row goes back to source=env/default) actually clears the
		// worker's override instead of it lingering from a stale snapshot.
		if h.onTunables != nil {
			h.onTunables(hbResp.Tunables)
		}

		// GSEC Q21.8 — hand the optional `vault` object to the rotation
		// receiver on every successfully-decoded beat, nil when absent.
		if h.vaultDelivery != nil {
			h.vaultDelivery(hbResp.Vault)
		}

		// §3.4 — adopt the manager's lease. Only applied when the manager
		// actually sent one; a zero means an older manager that does not know
		// about fencing, and falling back to the built-in default is safer
		// than treating zero as "fencing disabled".
		if hbResp.StackFenceLeaseS > 0 {
			h.SetFenceLease(hbResp.StackFenceLeaseS)
		}

		// BUG-1028h J3b — adopt the manager's CLAMPED per-worker spread. Applied
		// on EVERY successfully-decoded beat, including 0, so an operator who
		// cleared the registry setting (or an older manager that never sends
		// the key, which decodes to 0) clears a stale spread instead of it
		// lingering from a previous snapshot — same reasoning as the Tunables
		// block below. SetFenceJitterPct clamps to [0,25] again on this side:
		// a malformed push must never extend the lease past the manager's
		// headroom, and a worker that accepted a bigger spread than the
		// manager accounted for would fence after re-placement (split-brain).
		h.SetFenceJitterPct(hbResp.StackFenceJitterPct)

		// 2400BUG-1028h J1 — stash the manager's placement confirmation for the
		// beat applyHeartbeatResult is about to apply. Stashed, not acted on,
		// here: the fence decision itself lives in fence.go and is driven by the
		// same tick, so the confirmation can never be read a beat stale.
		h.stashResumeCandidates(hbResp.StackResumeProjects)

		// Push server coordination signals to transport/main loop
		if h.onServerState != nil {
			h.onServerState(hbResp.ServerUpdating, hbResp.Drain, hbResp.UpdateAvailable, hbResp.LatestVersion)
		}

		// Pre-pull images the server says we'll need (pending tasks in the
		// queue, sidecar images, benchmark containers). Fires during the
		// profiling window on new worker join and whenever the API sees
		// uncached images for pending tasks.
		if len(hbResp.ImagesToPrepull) > 0 && h.onPrepullReady != nil {
			h.onPrepullReady(hbResp.ImagesToPrepull)
		}

		// Hard-cancel: the server told us these tasks are dead (expired/
		// failed/cancelled). Kill their containers now so a user Cancel
		// terminates the in-flight run instead of letting it run out.
		if len(hbResp.TasksToCancel) > 0 && h.onCancelTasks != nil {
			h.onCancelTasks(hbResp.TasksToCancel)
		}

		// mdns-name-discovery §3 — re-home the site anchor if the manager says
		// its address moved. No-op when the field is absent (flag off), when no
		// executor is wired, or when the epoch is unchanged.
		h.handleMeshRehome(hbResp.MeshRehome)

		// Keep the site tailnet joined: reconciles in the background, so a
		// slow docker/tailscale call never delays the next heartbeat.
		if hbResp.MeshSite != nil && h.siteMesh != nil {
			h.siteMesh.Apply(hbResp.MeshSite)
		}
	}

	return roster, resp, nil
}
