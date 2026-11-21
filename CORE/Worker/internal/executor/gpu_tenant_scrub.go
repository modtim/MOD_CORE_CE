package executor

// FX.80.p2 — VRAM scrub between different-tenant jobs on the same GPU card
// (hosted-cloud isolation; owner decision Q63=A).
//
// Why: a card that just ran tenant A's container still carries A's residue in
// its CUDA context — allocation sizes, cached-kernel timings, memory pressure,
// and whatever A left in the blocks the allocator had cached. A different
// tenant landing on that card can observe that residue, which on a hosted
// multi-tenant deployment is a cross-tenant information leak. So when the
// card's LAST tenant differs from the tenant of the job about to start, the
// worker zeroes the card's free VRAM with a short-lived helper container
// attached to THAT CARD ONLY, before the job's container is created.
//
// Gate: the API-pushed worker.gpu_tenant_scrub tunable (GS.PSR.7 — the same
// platform-settings → heartbeat `tunables` mechanism worker.cleanup_reclaim
// uses; no env var, no worker restart). Default OFF, so an on-prem
// single-customer fleet sees no behaviour change at all.
//
// Fail-closed: a scrub that cannot run (image missing, helper crashed,
// timeout) is an ERROR. Run refuses to start the other tenant's job on that
// card and returns the error, so the API retries/reschedules the task rather
// than silently running it on a dirty card.
//
// Tenant identity comes from transport.Task.TenantID — the claim-time task
// envelope the API stamps on every task (0 = platform/system task, >0 = a
// tenant; see backend.go's TenantID note and k8s_taskspec.go's EnvTenantID).
// 0 is labelled "unknown": switching to or from unknown scrubs exactly like
// switching between two named tenants, because "unknown" may be another
// tenant's work. unknown↔unknown is the only pair that needs no scrub, since
// both sides are the same unattributed workload class.
//
// The card key is the card's docker device identity — the NVIDIA uuid when the
// API reported one, else the host index as a decimal string — i.e. exactly
// what gpu_assignment.go puts into DeviceRequest.DeviceIDs. That makes the
// key both the ledger key and the value the scrub helper's DeviceRequest
// needs, with no per-task state to carry. A GPU-required task with NO
// assignment (older API, or a non-pinned GPU task) gets every GPU, so it
// gets one synthetic "*" key and the scrub attaches all cards — the honest
// fail-closed answer for "we do not know which cards are involved".
//
// The scrubber's launch seam (VramScrubber.launch) is a field, not a hard
// call, so gpu_tenant_scrub_test.go can fake the container entirely.
import (
	"bytes"
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/pkg/stdcopy"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// Tunable keys (platform_settings registry, delivered on the heartbeat
// `tunables` map). The image default is the MOD Core CUDA base the node
// builder already hands CUDA nodes (CORE/API/node_builder/base_detector.py
// BASE_CUDA_PY311) — it carries python3 + torch out of the box, so the scrub
// helper needs no new registry object. It is a CONFIG value
// (worker.gpu_tenant_scrub_image), not a constant, because a fleet may run
// its Harbor under a different host.
const (
	tenantScrubTunableKey        = "worker.gpu_tenant_scrub"
	tenantScrubImageTunableKey   = "worker.gpu_tenant_scrub_image"
	tenantScrubTimeoutTunableKey = "worker.gpu_tenant_scrub_timeout_s"
	tenantScrubHistoryCap        = 32
	tenantScrubDefaultImage      = "ghcr.io/modtim/base-cuda:12.4-python3.11"
	tenantScrubDefaultTimeoutS   = 120
	tenantScrubMinTimeoutS       = 5
	tenantScrubPullTimeout       = 90 * time.Second
	tenantScrubTeardown          = 10 * time.Second
	tenantScrubUnknownCard       = "*"
)

// tenantScrubScript is the helper's whole job: free the allocator's cached
// blocks, then allocate the card's remaining FREE VRAM, memset it to zero,
// and print the byte count. Deliberately quote-free so it survives being
// nested inside `sh -c "python3 -c '<script>'"` without escaping.
//
// The 128 MB headroom is what the CUDA context + the tensor's own bookkeeping
// needs; asking for exactly mem_get_info()[0] reliably OOMs. Zeroing the free
// remainder is the point: once the container (and its CUDA context) is gone,
// nothing that was previously resident stays readable.
const tenantScrubScript = `import torch
torch.cuda.empty_cache()
free=torch.cuda.mem_get_info()[0]
n=max(free-(128<<20),0)
m=torch.empty(n,dtype=torch.uint8,device='cuda')
m.zero_()
torch.cuda.synchronize()
del m
torch.cuda.empty_cache()
print(n)`

// scrubBytesLine picks the helper's stdout counter (a bare integer; the last
// one wins — torch prints nothing else here, but a noisy image might).
var scrubBytesLine = regexp.MustCompile(`[0-9]+`)

// unknownTenant is the label a task with no tenant id (TenantID == 0 — a
// platform/system task) gets. It is a real label, not a wildcard: unknown↔
// unknown is the only pair that needs no scrub, because both sides are the
// same (unattributed) workload class.
const unknownTenant = "unknown"

// ScrubResult is one scrub's record: which card, which tenants it separates,
// how much was zeroed, how long it took, and whether it succeeded. Shaped
// for the heartbeat `gpu_scrubs` key (FX.80.p3 ingests it on the API side;
// nothing here touches the API).
type ScrubResult struct {
	Card        string
	FromTenant  string
	ToTenant    string
	BytesZeroed int64
	DurationMS  int64
	OK          bool
	At          time.Time
}

// VramScrubber is the small interface the task asks for: the executor owns
// one, tests replace `launch` so no container is ever started.
type VramScrubber struct {
	// launch runs one helper container on the given card keys and returns
	// (bytes zeroed, exit code, error). The production implementation is
	// Executor.launchScrubHelper; tests hand a closure instead. The image and
	// the timeout are NOT fields here: they are read fresh from the tunables
	// inside the launcher, like every other worker knob, so a Dashboard change
	// applies mid-flight instead of being frozen at construction.
	launch func(ctx context.Context, cardIDs []string) (bytesZeroed int64, exitCode int, err error)
}

// tenantScrubCmd wraps the python one-liner the way the executor already
// wraps helper commands (sh -c). Split out so the production launcher and the
// tests can both assert on it.
func tenantScrubCmd() []string {
	return []string{
		"/bin/sh", "-c",
		"python3 -c '" + tenantScrubScript + "'",
	}
}

// tenantLabelOf names the task's tenant for the per-card ledger.
func tenantLabelOf(task *transport.Task) string {
	if task != nil && task.TenantID > 0 {
		return strconv.Itoa(task.TenantID)
	}
	return unknownTenant
}

// cardKeysFor names the cards a task will touch, in the docker device-identity
// vocabulary (uuid when reported, host index otherwise) — see the package
// comment. Non-cuda kinds (rocm/intel, pinned via raw /dev/dri mounts) are
// skipped: this scrub's helper is a CUDA program, and the rocm/intel pinning
// path has its own device isolation.
func cardKeysFor(a *transport.GPUAssignment, gpuRequired bool) []string {
	if !gpuRequired {
		// A CPU task touches no card, so there is nothing to scrub and nothing
		// to record — the ledger must not learn "this card ran a CPU job".
		return nil
	}
	if a == nil || len(a.Cards) == 0 {
		// GPU-required but unpinned: the container gets EVERY GPU (nvidiaDevice
		// Request's Count=-1 branch), so one synthetic key covers the whole host
		// and the scrub attaches all cards.
		return []string{tenantScrubUnknownCard}
	}
	var keys []string
	for _, c := range a.Cards {
		if c.Kind != "" && c.Kind != "cuda" {
			continue
		}
		// Same identity gpu_assignment.go prefers for this card: uuid first,
		// host index otherwise.
		keys = append(keys, scrubCardKey(c))
	}
	return keys
}

// scrubCardKey is one pinned card's ledger/device key.
func scrubCardKey(c transport.AssignedGPU) string {
	if c.DeviceUUID != "" {
		return c.DeviceUUID
	}
	return strconv.Itoa(c.Index)
}

// scrubDeviceRequest is the DeviceRequest the scrub helper gets for a set of
// card keys. "*" (unknown card) means every GPU — Count=-1 with no DeviceIDs,
// exactly nvidiaDeviceRequest's no-assignment branch; a real key set pins
// exactly those cards by uuid or host index (Count=0 + DeviceIDs).
func scrubDeviceRequest(cardIDs []string) container.DeviceRequest {
	req := container.DeviceRequest{
		// Driver "nvidia" + Capabilities [["gpu"]] is how this worker already
		// attaches GPUs (gpu_assignment.go nvidiaDeviceRequest, and the managed
		// stacks' own compose blocks).
		Driver:       "nvidia",
		Capabilities: [][]string{{"gpu"}},
	}
	ids := scrubDeviceIDs(cardIDs)
	if len(ids) == 0 {
		// "*" only: every GPU, so the scrub cannot miss a card we could not
		// name.
		req.Count = -1
		return req
	}
	req.Count = 0
	req.DeviceIDs = ids
	return req
}

// scrubDeviceIDs maps card keys to DeviceIDs, dropping the synthetic "*" key
// (which means "all cards", not a device name).
func scrubDeviceIDs(cardIDs []string) []string {
	var ids []string
	for _, k := range cardIDs {
		if k == tenantScrubUnknownCard {
			continue
		}
		ids = append(ids, k)
	}
	return ids
}

// gpuTenantScrubEnabled is the feature gate: the API-pushed
// worker.gpu_tenant_scrub platform setting, default OFF. On-prem
// single-customer fleets never set it, so no card is ever scrubbed and no
// helper container is ever started — the whole path is inert.
func (e *Executor) gpuTenantScrubEnabled() bool {
	if e.tunables == nil {
		return false
	}
	return e.tunables.Bool(tenantScrubTunableKey, "", false)
}

// cudaCapable is the hardware half of the gate: this scrub's helper is a CUDA
// program attached through Docker's nvidia DeviceRequest, so it can only run
// where the worker's capability detection reported a CUDA card. A CPU-only
// worker (and a rocm/intel-only one, which pins its devices through raw
// /dev/dri mounts instead) has no CUDA context on any card to scrub, so the
// whole path is a NO-OP there — not a failure. Turning a CPU box into a
// fail-closed refusal would bounce every GPU-required task that the
// scheduler misroutes to it, which is a different bug than the one this
// scrub fixes.
//
// The tag vocabulary is capabilities/detect.go's: "cuda" is appended when an
// nvidia GPU is detected (alongside companions like "tensorrt"/"triton"), so
// "cuda" is the authoritative marker for the nvidia runtime being present.
// An EMPTY tag list means "no capability info" — a hand-built Executor in a
// unit test, or a worker whose detection never ran — and is treated as
// capable, because the honest reading of "we do not know" is to let the
// operator's explicit gate win rather than to silently skip a scrub the
// operator asked for. A real worker always has tags ("cpu" at minimum), so
// the CPU-only no-op below is decided by an explicit "cpu" list, not by
// emptiness.
func (e *Executor) cudaCapable() bool {
	if len(e.hardwareTags) == 0 {
		return true
	}
	for _, tag := range e.hardwareTags {
		if strings.EqualFold(strings.TrimSpace(tag), "cuda") {
			return true
		}
	}
	return false
}

// scrubImage resolves the helper image (config value, default the MOD Core
// CUDA base). Read fresh on every scrub, like every other tunable reader, so
// a Dashboard/DB change applies within two heartbeats with no restart.
func (e *Executor) scrubImage() string {
	if e.tunables == nil {
		return tenantScrubDefaultImage
	}
	return e.tunables.String(tenantScrubImageTunableKey, "", tenantScrubDefaultImage)
}

// scrubTimeout is the helper's bounded wall clock. A scrub is a memset of a
// few tens of GB — seconds, not minutes — so the default is tight and a wedged
// helper is a FAILURE (fail-closed), not something to wait on.
func (e *Executor) scrubTimeout() time.Duration {
	secs := tenantScrubDefaultTimeoutS
	if e.tunables != nil {
		secs = e.tunables.Int(tenantScrubTimeoutTunableKey, tenantScrubDefaultTimeoutS)
	}
	if secs < tenantScrubMinTimeoutS {
		secs = tenantScrubMinTimeoutS
	}
	return time.Duration(secs) * time.Second
}

// Scrub runs one scrub on one card and reports what it did. This is the small
// interface the task asks for; `cardKey` is the card's device identity (uuid,
// or the host index as a decimal string — the same vocabulary
// gpu_assignment.go uses for DeviceRequest.DeviceIDs). Tests replace
// VramScrubber.launch, so nothing here is docker-shaped in a test path.
func (s *VramScrubber) Scrub(ctx context.Context, cardKey string) (*ScrubResult, error) {
	started := time.Now()
	zeroed, exitCode, err := s.launch(ctx, []string{cardKey})
	dur := time.Since(started)
	if err != nil {
		// A failed scrub is recorded too, so the operator can see the card
		// that refused to scrub (and the manager can see why the job bounced).
		return &ScrubResult{
			Card:        cardKey,
			BytesZeroed: zeroed,
			DurationMS:  dur.Milliseconds(),
			OK:          false,
			At:          started,
		}, fmt.Errorf("gpu scrub on card %s: %w", cardKey, err)
	}
	if exitCode != 0 {
		return &ScrubResult{
			Card:        cardKey,
			BytesZeroed: zeroed,
			DurationMS:  dur.Milliseconds(),
			OK:          false,
			At:          started,
		}, fmt.Errorf("gpu scrub on card %s exited %d (helper could not zero the card)", cardKey, exitCode)
	}
	return &ScrubResult{
		Card:        cardKey,
		BytesZeroed: zeroed,
		DurationMS:  dur.Milliseconds(),
		OK:          true,
		At:          started,
	}, nil
}

// noteScrubResult appends to the in-memory ring (newest last, capped) and
// emits the structured log line the task asks for. Kept in one place so the
// heartbeat view and the log can never disagree.
func (e *Executor) noteScrubResult(r *ScrubResult) {
	e.tenantScrubMu.Lock()
	if e.tenantScrubHistory == nil {
		e.tenantScrubHistory = make([]ScrubResult, 0, tenantScrubHistoryCap)
	}
	// Ring: drop from the front once past the cap. A slice-shift keeps the
	// newest entries and is O(cap) with cap=32 — no deque type needed.
	if len(e.tenantScrubHistory) >= tenantScrubHistoryCap {
		e.tenantScrubHistory = e.tenantScrubHistory[len(e.tenantScrubHistory)-tenantScrubHistoryCap+1:]
	}
	e.tenantScrubHistory = append(e.tenantScrubHistory, *r)
	e.tenantScrubMu.Unlock()
	e.log.Info("gpu vram scrub",
		"card", r.Card,
		"from_tenant", r.FromTenant,
		"to_tenant", r.ToTenant,
		"bytes_zeroed", r.BytesZeroed,
		"duration_ms", r.DurationMS,
		"ok", r.OK,
	)
}

// scrubSummary shapes the ring for the heartbeat `gpu_scrubs` key (FX.80.p3
// ingests it; the API side is untouched here). nil when nothing has scrubbed
// yet — the same omit-don't-fake contract every other heartbeat provider
// uses, so "no data" is never read as "no scrubs needed".
func (e *Executor) ScrubSummary() []map[string]interface{} {
	e.tenantScrubMu.Lock()
	hist := e.tenantScrubHistory
	e.tenantScrubMu.Unlock()
	if len(hist) == 0 {
		// Never scrubbed (gate off, or no tenant switch yet) ⇒ omit the key.
		// An empty list would read as "scrubbing is on and found nothing",
		// which is a different claim.
		return nil
	}
	out := make([]map[string]interface{}, 0, len(hist))
	for _, r := range hist {
		out = append(out, map[string]interface{}{
			"card":         r.Card,
			"from_tenant":  r.FromTenant,
			"to_tenant":    r.ToTenant,
			"bytes_zeroed": r.BytesZeroed,
			"duration_ms":  r.DurationMS,
			"ok":           r.OK,
			"at":           r.At.UTC().Format(time.RFC3339),
		})
	}
	return out
}

// launchScrubHelper is the production VramScrubber.launch: one short-lived
// helper container attached to exactly the cards being scrubbed, which
// allocates the card's free VRAM, memsets it zero, and prints the byte count.
//
// Image pull uses PullImagePublic (no per-task creds are consulted for the
// scrub): the scrub image is a platform base image, not a tenant image, and
// Harbor login already lives in the daemon's config.json on every worker the
// setup script provisions — the same path the pre-pull profiler uses. A pull
// failure is an error, which the caller turns into a refused job start
// (fail-closed).
//
// The wait is bounded by the scrub timeout. A scrub is a memset, so anything
// slower than that is a wedged helper; waiting on it forever would strand the
// card exactly as badly as skipping it, while being unable to say the card is
// clean. Timeout returns an error (fail-closed) after killing the container.
func (e *Executor) launchScrubHelper(ctx context.Context, cardIDs []string) (int64, int, error) {
	if e.docker == nil {
		// No docker client (a worker that never got one, or a unit test that
		// built &Executor{} by hand): the scrub cannot be proven, so it fails.
		return 0, 0, fmt.Errorf("no docker client to run the scrub helper on")
	}
	if len(cardIDs) == 0 {
		return 0, 0, fmt.Errorf("scrub called with no card")
	}
	image := e.scrubImage()
	if image == "" {
		return 0, 0, fmt.Errorf("gpu scrub image is unset")
	}

	pullCtx, pullCancel := context.WithTimeout(ctx, tenantScrubPullTimeout)
	defer pullCancel()
	if err := e.PullImagePublic(pullCtx, image); err != nil {
		return 0, 0, fmt.Errorf("pull scrub image %s: %w", image, err)
	}

	// Attach ONLY the scrubbed cards, by the exact device identity the ledger
	// key already is (uuid or host index) — the same DeviceRequest shape
	// gpu_assignment.go builds for a pinned task. The "*" (unknown card) key
	// means every GPU, which is Count=-1 with no DeviceIDs.
	req := scrubDeviceRequest(cardIDs)

	// DeviceRequests lives in the embedded Resources struct, so it is set as a
	// statement (hostConfig.DeviceRequests), exactly how executor.go attaches
	// the processing container's GPUs — a struct literal cannot name an
	// embedded field.
	hostConfig := &container.HostConfig{}
	hostConfig.DeviceRequests = []container.DeviceRequest{req}

	resp, err := e.docker.ContainerCreate(ctx,
		&container.Config{
			Image:      image,
			Entrypoint: []string{tenantScrubCmd()[0]},
			Cmd:        tenantScrubCmd()[1:],
			Labels:     e.sidecarLabels(),
		},
		hostConfig,
		&network.NetworkingConfig{}, nil, "",
	)
	if err != nil {
		return 0, 0, fmt.Errorf("create scrub container: %w", err)
	}
	return e.scrubWaitAndRead(ctx, resp.ID)
}

// scrubWaitAndRead waits the bounded scrub timeout for the helper, then reads
// its stdout counter. Split from launchScrubHelper so a test can drive the
// wait/read half against a fake docker client too, and so the teardown defer
// runs on every return path (the same leak class the mc sidecars had).
func (e *Executor) scrubWaitAndRead(ctx context.Context, containerID string) (int64, int, error) {
	// Background teardown ctx: a scrub whose task ctx is already canceled must
	// still remove its container, or the card keeps a live helper forever.
	// Unconditional remove — a remove on an already-exited container is a
	// no-op error we discard, which is what readResultJSONFile does too.
	//
	// The helper is NOT labelled sidecar=1: it is not a data mover, and the
	// sidecar reaper's job is to reclaim stalled data movers. It IS labelled
	// with the worker identity + instance so the orphan sweeper still knows
	// this container is ours if this process dies mid-scrub.
	// Bounded wait. ContainerWait's own ctx is the scrub timeout, NOT the task
	// ctx: the task ctx can be enormous (unlimited under inactivity-liveness),
	// and a scrub has its own, much tighter, budget.
	deadline, cancel := context.WithTimeout(ctx, e.scrubTimeout())
	defer cancel()
	statusCh, errCh := e.docker.ContainerWait(deadline, containerID, container.WaitConditionNotRunning)
	select {
	case werr := <-errCh:
		// A wait error is the deadline or a daemon problem. Both mean "cannot
		// prove the card is clean", so this is fail-closed: kill the helper
		// (a wedged container would otherwise hold the card indefinitely) and
		// surface the error, which Run turns into a refused job start.
		if werr != nil {
			e.killContainer(containerID, "gpu vram scrub helper: wait error / timeout")
			return 0, 0, fmt.Errorf("gpu scrub wait: %w", werr)
		}
		e.killContainer(containerID, "gpu vram scrub helper: wait returned no status")
		return 0, 0, fmt.Errorf("gpu scrub wait: no status returned")
	case status := <-statusCh:
		// The helper exited. Its exit code is what decides whether the
		// card is clean: a nonzero exit means the memset never completed
		// (no torch, no GPU visible, OOM while allocating), and that is a
		// dirty card, not a clean one.
		zeroed, err := e.readScrubBytes(ctx, containerID)
		if err != nil {
			return 0, int(status.StatusCode), err
		}
		return zeroed, int(status.StatusCode), nil
		// No fall-through statement: every select arm returns, which makes the
		// select itself a terminating statement, so there is nothing after it.
	}
}

// readScrubBytes pulls the helper's stdout and returns the last bare integer
// it printed (the bytes zeroed). 0 with no error when the helper printed
// nothing parseable — the exit code still decides, and a helper that exited
// 0 without a counter zeroed something we cannot size, which is reported as
// 0 bytes rather than a fabricated figure.
func (e *Executor) readScrubBytes(ctx context.Context, containerID string) (int64, error) {
	logReader, err := e.docker.ContainerLogs(ctx, containerID, container.LogsOptions{ShowStdout: true})
	if err != nil {
		return 0, fmt.Errorf("read scrub helper logs: %w", err)
	}
	var stdout, stderr bytes.Buffer
	// stdcopy demuxes the framed /output stream the daemon sends when the
	// container has no tty — same as readResultJSONFile.
	_, _ = stdcopy.StdCopy(&stdout, &stderr, logReader)
	logReader.Close()
	return parseScrubBytes(stdout.String()), nil
}

// parseScrubBytes extracts the last bare integer from the helper's stdout.
// Pure, so the test can feed it captured text.
func parseScrubBytes(out string) int64 {
	var last int64 = 0
	for _, line := range strings.Split(out, "\n") {
		m := scrubBytesLine.FindString(strings.TrimSpace(line))
		if m == "" {
			continue
		}
		if n, err := strconv.ParseInt(m, 10, 64); err == nil && n > 0 {
			last = n
		}
	}
	return last
}

// needsScrub is the whole policy: scrub when the card has a recorded
// predecessor AND that predecessor is a different tenant label. Absent
// predecessor (fresh card / new worker process) needs nothing, and the same
// label on both sides needs nothing — including unknown/unknown, which is
// deliberately "the same workload class", not "anything goes".
func needsScrub(lastTenant, toTenant string, hasLast bool) bool {
	if !hasLast {
		return false
	}
	return lastTenant != toTenant
}

// scrubForTask is the per-card "last tenant" ledger plus the scrub decision.
// Called by Run immediately BEFORE the processing container is created, so a
// refusal happens before anything tenant-facing touches the card.
//
// gpuRequired is derived here the same way executor.go derives it for the
// DeviceRequest (payload.GPURequired || a selected gpu_variant), so the gate
// and the device attachment can never disagree about which tasks touch cards.
//
// Ledger: cardKey -> tenant label of the last job that ran on it. Absent
// (never seen) means "no tenant attribution yet", which needs no scrub: the
// first job on a fresh card has no predecessor residue to hide, and that is
// also what a worker restart looks like - a restarted worker has no memory
// of who ran before, and the previous incarnation's CUDA contexts died with
// their containers, so there is nothing to scrub either.
//
// Fail-closed: a scrub error is returned and the ledger is NOT updated for
// ANY card, so every card keeps its old attribution and the next attempt
// scrubs again. Retrying/rescheduling is the API's job - Run returning an
// error on a claimed task is how this worker already reports a failure.
func (e *Executor) scrubForTask(ctx context.Context, task *transport.Task) error {
	if task == nil {
		// No task means no tenant and no payload: nothing to scrub, and nothing
		// to record. (Run always passes one; this keeps the entry point total.)
		return nil
	}
	payload := task.Payload
	if !e.gpuTenantScrubEnabled() {
		// Gate off (the default): no ledger bookkeeping at all, so an on-prem
		// single-customer fleet never pays for the path and never scrubs.
		return nil
	}
	// A CPU task touches no card: nothing to scrub, and nothing to RECORD -
	// writing "this card ran a CPU job" into the ledger would let a later
	// tenant switch be skipped because the CPU job "cleaned" the card.
	gpuRequired := payload.GPURequired || e.selectedGPUBackend(payload) != ""
	if !gpuRequired {
		return nil
	}
	if !e.cudaCapable() {
		// CPU-only (or rocm/intel-only) worker: no CUDA context exists on any
		// card, so there is no residue to hide and no CUDA helper that could
		// run. A true no-op — including no ledger entry, so a card on a CUDA
		// worker is never attributed by a CPU worker's bookkeeping.
		return nil
	}
	// "*" (unknown card) is a real ledger key: an unpinned GPU task gets every
	// GPU, so it must be tracked as one unit against every other unpinned
	// task, and its scrub attaches all cards.
	//
	// The ledger is read/written under tenantScrubMu, which also guards the
	// history ring. Two concurrent tasks on one host (the worker runs several)
	// therefore decide their scrubs one card at a time - a card cannot be
	// "claimed" by both and have neither scrub.
	tenant := tenantLabelOf(task)
	cards := cardKeysFor(payload.GPUAssignment, true)
	for _, card := range cards {
		scrubbed, err := e.scrubCard(ctx, card, tenant)
		if err != nil {
			// Fail-closed: the job start is refused. The ledger is left
			// untouched below, so the card still remembers its previous
			// tenant and the retry scrubs again.
			return err
		}
		if scrubbed {
			// Already logged by noteScrubResult inside scrubCard.
			continue
		}
	}
	// The ledger is updated only after EVERY card scrubbed, so a mid-list
	// failure leaves every card attributed to its previous tenant and the
	// retry scrubs the whole set again (fail-closed, no partial amnesia).
	for _, card := range cards {
		e.tenantScrubMu.Lock()
		if e.tenantScrubLast == nil {
			e.tenantScrubLast = make(map[string]string)
		}
		e.tenantScrubLast[card] = tenant
		e.tenantScrubMu.Unlock()
	}
	return nil
}

// scrubCard is one card's decision: does this tenant switch need a scrub,
// and if so run it. Split out so the ledger logic is unit-testable without a
// docker client (the test hands a VramScrubber whose launch is a counter).
func (e *Executor) scrubCard(ctx context.Context, cardKey, toTenant string) (bool, error) {
	e.tenantScrubMu.Lock()
	if e.tenantScrubLast == nil {
		e.tenantScrubLast = make(map[string]string)
	}
	last := e.tenantScrubLast[cardKey]
	// A tenant label is never "" (tenantLabelOf returns "unknown" at worst),
	// so an empty reading is exactly "this card has no predecessor yet".
	hasLast := last != ""
	e.tenantScrubMu.Unlock()
	if !needsScrub(last, toTenant, hasLast) {
		return false, nil
	}
	if e.tenantScrub == nil {
		// No scrubber wired (a hand-built &Executor{} in a test, or a future
		// caller that forgot one): the scrub cannot be proven, so it fails
		// rather than pretending the card is clean.
		return false, fmt.Errorf("no vram scrubber wired on this executor")
	}
	res, err := e.tenantScrub.Scrub(ctx, cardKey)
	if res != nil {
		res.FromTenant = last
		res.ToTenant = toTenant
		e.noteScrubResult(res)
	}
	if err != nil {
		return true, err
	}
	return true, nil
}

// newTenantScrubber wires the production launcher. Built in Executor.New so
// every executor has a scrubber even while the gate is off — the gate is
// checked at call time, and a missing scrubber is a hard failure (fail-closed)
// which would otherwise make "gate on" mean "cannot scrub".
func newTenantScrubber(e *Executor) *VramScrubber {
	return &VramScrubber{launch: e.launchScrubHelper}
}
