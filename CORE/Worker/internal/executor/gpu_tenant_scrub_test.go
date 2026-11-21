package executor

// FX.80.p2 — cross-tenant VRAM scrub tests. The scrubber's `launch` field is
// the seam, so every test here counts scrub calls and asserts on the card
// keys handed to the helper; no docker client and no GPU are involved.
import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// scrubGate builds an executor whose gate is ON (or OFF) and whose scrubber
// records every launch's card keys. failOn (1-based, 0 = none) makes one
// launch fail — how the fail-closed test drives a broken helper without a
// docker client. The returned closure reads the recorded launches.
func scrubGate(failOn int, gate bool) (*Executor, func() []string) {
	store := tunables.New()
	if gate {
		// The API pushes worker.gpu_tenant_scrub=true on the heartbeat
		// `tunables` map; that is the only way the path turns on.
		// One Apply only: the store's snapshot is replaced wholesale per Apply
		// (see tunables_test.go's TestApply_ClearsPreviousSnapshot), so pushing
		// the gate and the image knob must come in the same map.
		store.Apply(map[string]interface{}{
			tenantScrubTunableKey:      true,
			tenantScrubImageTunableKey: "harbor.example/modtim/base-cuda:1",
		})
	}
	var launched []string
	launch := func(ctx context.Context, cardIDs []string) (int64, int, error) {
		launched = append(launched, strings.Join(cardIDs, ","))
		// Once the failure threshold is reached every later launch fails too,
		// which is what the retry-after-failure test needs: the helper is still
		// broken, so the retry must scrub again AND fail again.
		if failOn > 0 && len(launched) >= failOn {
			return 0, 0, fmt.Errorf("helper could not zero the card")
		}
		// A successful fake scrub reports a plausible byte count so the ring
		// assertions see real numbers, not zeros.
		return int64(8) << 30, 0, nil
	}
	e := &Executor{
		log: logIt(),
		// A CUDA worker: the scrub's helper is a CUDA program, so the hardware
		// half of the gate must be satisfied for these tests to exercise the
		// policy at all. The CPU-only worker case is its own test below.
		hardwareTags: []string{"cuda"},
		tunables:     store,
		tenantScrub:  &VramScrubber{launch: launch},
	}
	return e, func() []string { return launched }
}

func logIt() *slog.Logger {
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
}

// gpuTask is a GPU-required task on the given tenant, pinned to the given
// cuda cards. A card name starting with a digit exercises the host-index
// fallback (no uuid); a name starting with a letter becomes the uuid.
func gpuTask(tenant int, cardNames ...string) *transport.Task {
	var assigned []transport.AssignedGPU
	for _, n := range cardNames {
		if len(n) > 0 && n[0] >= '0' && n[0] <= '9' {
			// No uuid: the ledger key must be the host index as a string.
			assigned = append(assigned, transport.AssignedGPU{Index: atoi(n), Kind: "cuda"})
		} else {
			assigned = append(assigned, transport.AssignedGPU{DeviceUUID: n, Kind: "cuda"})
		}
	}
	payload := transport.TaskPayload{GPURequired: true}
	if len(assigned) > 0 {
		payload.GPUAssignment = &transport.GPUAssignment{Mode: "exclusive", Cards: assigned}
	}
	return &transport.Task{ID: 1, TenantID: tenant, Payload: payload}
}

func atoi(s string) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0
	}
	return n
}

// Same tenant twice on one card: no scrub. The ledger must also still record
// the tenant, so a later different tenant is caught.
func TestScrub_SameTenantNoScrub(t *testing.T) {
	e, launched := scrubGate(0, true)
	ctx := context.Background()
	if err := e.scrubForTask(ctx, gpuTask(7, "GPU-aaa")); err != nil {
		t.Fatalf("first job (fresh card) must not fail: %v", err)
	}
	if n := len(launched()); n != 0 {
		t.Fatalf("fresh card has no predecessor, want 0 scrubs, got %d", n)
	}
	if err := e.scrubForTask(ctx, gpuTask(7, "GPU-aaa")); err != nil {
		t.Fatalf("same tenant repeat must not fail: %v", err)
	}
	if n := len(launched()); n != 0 {
		t.Fatalf("same tenant must not scrub, want 0, got %d", n)
	}
}

// Different tenant on the same card: exactly one scrub, on the right card key.
func TestScrub_DifferentTenantScrubsOnce(t *testing.T) {
	e, launched := scrubGate(0, true)
	ctx := context.Background()
	if err := e.scrubForTask(ctx, gpuTask(7, "GPU-aaa")); err != nil {
		t.Fatalf("first job must not fail: %v", err)
	}
	if err := e.scrubForTask(ctx, gpuTask(9, "GPU-aaa")); err != nil {
		t.Fatalf("tenant switch must not fail: %v", err)
	}
	got := launched()
	if len(got) != 1 {
		t.Fatalf("want exactly 1 scrub, got %d: %v", len(got), got)
	}
	if got[0] != "GPU-aaa" {
		t.Fatalf("scrub must target the pinned card key, got %q", got[0])
	}
}

// Gate off (the default): the ledger is never touched and no scrub ever runs,
// even across a tenant switch. This is the on-prem single-customer case.
func TestScrub_GateOffNeverScrubs(t *testing.T) {
	e, launched := scrubGate(0, false)
	ctx := context.Background()
	if err := e.scrubForTask(ctx, gpuTask(7, "GPU-aaa")); err != nil {
		t.Fatalf("gate off must not fail: %v", err)
	}
	if err := e.scrubForTask(ctx, gpuTask(9, "GPU-aaa")); err != nil {
		t.Fatalf("gate off must not fail on a tenant switch: %v", err)
	}
	if n := len(launched()); n != 0 {
		t.Fatalf("gate off must never scrub, got %d", n)
	}
	// Nothing recorded either: the heartbeat key stays omitted.
	if sc := e.ScrubSummary(); sc != nil {
		t.Fatalf("gate off must report no scrubs, got %d entries", len(sc))
	}
}

// Scrub failure => the job start is refused (fail-closed), and the card keeps
// its old attribution so the retry scrubs again rather than trusting a card
// we could not clean.
func TestScrub_ScrubErrorRefusesJobStart(t *testing.T) {
	e, launched := scrubGate(1, true)
	ctx := context.Background()
	if err := e.scrubForTask(ctx, gpuTask(7, "GPU-aaa")); err != nil {
		t.Fatalf("first job (fresh card) must not fail: %v", err)
	}
	err := e.scrubForTask(ctx, gpuTask(9, "GPU-aaa"))
	if err == nil {
		t.Fatalf("a failed scrub must refuse the job start, not allow it")
	}
	if !strings.Contains(err.Error(), "helper could not zero the card") {
		t.Fatalf("refusal must name the scrub failure, got %v", err)
	}
	// The failed scrub IS recorded (ok=false) so the operator can see the card
	// that refused, and the retry scrubs again because the ledger was untouched.
	if err2 := e.scrubForTask(ctx, gpuTask(9, "GPU-aaa")); err2 == nil {
		t.Fatalf("retry after a failed scrub must scrub again and fail again")
	}
	got := launched()
	if len(got) != 2 {
		t.Fatalf("want a scrub on both attempts, got %d: %v", len(got), got)
	}
}

// Unknown tenant (TenantID == 0, a platform/system task) switching to or from
// a named tenant scrubs: "unknown" may be another tenant's work.
func TestScrub_UnknownTenantSwitchScrubs(t *testing.T) {
	e, launched := scrubGate(0, true)
	ctx := context.Background()
	// unknown first (fresh card, no scrub)
	if err := e.scrubForTask(ctx, gpuTask(0, "GPU-aaa")); err != nil {
		t.Fatalf("unknown first must not fail: %v", err)
	}
	if n := len(launched()); n != 0 {
		t.Fatalf("fresh card wants 0 scrubs, got %d", n)
	}
	// unknown -> tenant 4 scrubs
	if err := e.scrubForTask(ctx, gpuTask(4, "GPU-aaa")); err != nil {
		t.Fatalf("unknown -> tenant must not fail: %v", err)
	}
	if n := len(launched()); n != 1 {
		t.Fatalf("unknown -> tenant must scrub once, got %d", n)
	}
	// tenant 4 -> unknown scrubs
	if err := e.scrubForTask(ctx, gpuTask(0, "GPU-aaa")); err != nil {
		t.Fatalf("tenant -> unknown must not fail: %v", err)
	}
	if n := len(launched()); n != 2 {
		t.Fatalf("tenant -> unknown must scrub once more, got %d", n)
	}
	// unknown -> unknown does NOT scrub: same (unattributed) workload class.
	if err := e.scrubForTask(ctx, gpuTask(0, "GPU-aaa")); err != nil {
		t.Fatalf("unknown -> unknown must not fail: %v", err)
	}
	if n := len(launched()); n != 2 {
		t.Fatalf("unknown -> unknown must not scrub, still %d expected", n)
	}
}

// A task pinned to several cards scrubs each pinned card exactly once, keyed
// by that card's device identity (uuid when reported, host index otherwise).
func TestScrub_MultiCardScrubsEachCard(t *testing.T) {
	e, launched := scrubGate(0, true)
	ctx := context.Background()
	if err := e.scrubForTask(ctx, gpuTask(7, "GPU-aaa", "3")); err != nil {
		t.Fatalf("first job must not fail: %v", err)
	}
	if err := e.scrubForTask(ctx, gpuTask(9, "GPU-aaa", "3")); err != nil {
		t.Fatalf("tenant switch must not fail: %v", err)
	}
	got := launched()
	if len(got) != 2 {
		t.Fatalf("want one scrub per pinned card (2), got %d: %v", len(got), got)
	}
	if got[0] != "GPU-aaa" || got[1] != "3" {
		t.Fatalf("scrubs must target each card key (uuid + host index), got %v", got)
	}
}

// GPU-required but unpinned (older API / no card pin): the container gets
// every GPU, so the scrub must cover the whole host rather than guess a card.
func TestScrub_UnpinnedGPUTaskScrubsAllCards(t *testing.T) {
	e, launched := scrubGate(0, true)
	ctx := context.Background()
	first := gpuTask(7)
	if err := e.scrubForTask(ctx, first); err != nil {
		t.Fatalf("first job must not fail: %v", err)
	}
	if err := e.scrubForTask(ctx, gpuTask(9)); err != nil {
		t.Fatalf("tenant switch must not fail: %v", err)
	}
	got := launched()
	if len(got) != 1 {
		t.Fatalf("unpinned GPU task is one ledger unit, want 1 scrub, got %d: %v", len(got), got)
	}
	if got[0] != tenantScrubUnknownCard {
		t.Fatalf("unpinned scrub must carry the all-cards key, got %q", got[0])
	}
}

// A CPU task touches no card: it must not scrub AND must not be recorded, or
// a later tenant switch would be skipped because the CPU job "cleaned" the
// card.
func TestScrub_CPUNodeNeverScrubs(t *testing.T) {
	e, launched := scrubGate(0, true)
	ctx := context.Background()
	cpu := &transport.Task{ID: 2, TenantID: 7, Payload: transport.TaskPayload{NodeKind: "ffmpeg"}}
	if err := e.scrubForTask(ctx, cpu); err != nil {
		t.Fatalf("cpu task must not fail: %v", err)
	}
	gpu := gpuTask(9, "GPU-aaa")
	if err := e.scrubForTask(ctx, gpu); err != nil {
		t.Fatalf("gpu task on a fresh card must not fail: %v", err)
	}
	if n := len(launched()); n != 0 {
		t.Fatalf("cpu task must not scrub the card, got %d", n)
	}
}

// The in-memory ring is capped: a long tenant-switching shift cannot grow
// memory unboundedly, and the newest scrub is always retained.
func TestScrub_RingBufferCapped(t *testing.T) {
	e, launched := scrubGate(0, true)
	ctx := context.Background()
	total := tenantScrubHistoryCap + 12
	for i := 0; i < total; i++ {
		tenant := (i % 2) + 7
		if err := e.scrubForTask(ctx, gpuTask(tenant, "GPU-aaa")); err != nil {
			t.Fatalf("scrub %d must not fail: %v", i, err)
		}
	}
	got := launched()
	if len(got) != total-1 {
		t.Fatalf("want a scrub on every tenant switch (%d), got %d", total-1, len(got))
	}
	sc := e.ScrubSummary()
	if sc == nil {
		t.Fatalf("scrubs happened, so the summary must not be nil")
	}
	if len(sc) != tenantScrubHistoryCap {
		t.Fatalf("ring must cap at %d, got %d", tenantScrubHistoryCap, len(sc))
	}
	newest := sc[len(sc)-1]
	if newest["card"] != "GPU-aaa" || newest["ok"] != true {
		t.Fatalf("newest entry must be the last scrub, got %v", newest)
	}
}

// A CPU-only worker is a NO-OP even when the operator turned the gate on:
// there is no CUDA context on any of its cards to scrub, and the CUDA helper
// could not run there anyway. No launch, no ledger entry, no error — a GPU
// task misrouted to a CPU box must not be bounced by the scrub.
func TestScrub_CPUWorkerNeverScrubs(t *testing.T) {
	e, launched := scrubGate(0, true)
	e.hardwareTags = []string{"cpu"}
	ctx := context.Background()
	if err := e.scrubForTask(ctx, gpuTask(4, "GPU-aaa")); err != nil {
		t.Fatalf("cpu-only worker must not refuse the job: %v", err)
	}
	if err := e.scrubForTask(ctx, gpuTask(9, "GPU-aaa")); err != nil {
		t.Fatalf("cpu-only worker must not refuse the second tenant either: %v", err)
	}
	if n := len(launched()); n != 0 {
		t.Fatalf("cpu-only worker must never scrub, got %d", n)
	}
	if sc := e.ScrubSummary(); sc != nil {
		t.Fatalf("cpu-only worker must record nothing, got %d", len(sc))
	}
}

// parseScrubBytes reads the helper's counter out of noisy stdout: the last
// bare integer wins, and nothing parseable is 0 (never a fabricated figure).
func TestParseScrubBytes_CounterFromNoisyStdout(t *testing.T) {
	if got := parseScrubBytes("1073741824\n"); got != int64(1073741824) {
		t.Fatalf("plain counter, got %d", got)
	}
	if got := parseScrubBytes("warn: 12\n8589934592\ntorch: done\n"); got != int64(8589934592) {
		t.Fatalf("last bare integer must win, got %d", got)
	}
	if got := parseScrubBytes("no digits here\n"); got != 0 {
		t.Fatalf("unparseable stdout must read as 0, got %d", got)
	}
	if got := parseScrubBytes("free=1073741824 bytes\n"); got != int64(1073741824) {
		t.Fatalf("a counter embedded in text still counts, got %d", got)
	}
}
