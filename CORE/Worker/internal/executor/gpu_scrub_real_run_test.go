package executor

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

const (
	realImgGood   = "scrubtest-helper-cap:1"
	realImgBroken = "scrubtest-helper-broken:1"
)

// realExec is a hand-built executor with a REAL docker client and the real
// scrubber wired (newTenantScrubber), gate ON/OFF through the tunables store
// exactly as the API pushes it. prefer_local_images lets the locally built
// scrubtest-helper image skip the registry pull.
func realExec(t *testing.T, gate bool, image string) *Executor {
	t.Helper()
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatalf("docker client: %v", err)
	}
	store := tunables.New()
	store.Apply(map[string]interface{}{
		tenantScrubTunableKey:       gate,
		tenantScrubImageTunableKey:  image,
		preferLocalImagesTunableKey: true,
	})
	e := &Executor{
		log:          slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})),
		docker:       cli,
		hardwareTags: []string{"cuda"},
		tunables:     store,
		workerName:   "scrubtest-worker",
		instanceID:   "scrubtest",
	}
	e.tenantScrub = newTenantScrubber(e)
	return e
}

// setImage changes the helper image mid-test (a Dashboard knob, read fresh).
func setImage(e *Executor, gate bool, image string) {
	e.tunables.Apply(map[string]interface{}{
		tenantScrubTunableKey:       gate,
		tenantScrubImageTunableKey:  image,
		preferLocalImagesTunableKey: true,
	})
}

// runTenant runs one tenant workload container on card 0 (nvidia runtime,
// network none, caps dropped) and returns its stdout and exit code. The
// container is removed before returning.
func runTenant(t *testing.T, e *Executor, name, script string) (string, int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	hc := helperBaseHostConfig()
	hc.DeviceRequests = []container.DeviceRequest{scrubDeviceRequest([]string{"0"})}
	resp, err := e.docker.ContainerCreate(ctx, &container.Config{
		Image:      realImgGood,
		Entrypoint: []string{"/usr/bin/python3", "-c", script},
	}, hc, &network.NetworkingConfig{}, nil, name)
	if err != nil {
		t.Fatalf("create %s: %v", name, err)
	}
	defer e.docker.ContainerRemove(context.Background(), resp.ID, container.RemoveOptions{Force: true})
	if err := e.docker.ContainerStart(ctx, resp.ID, container.StartOptions{}); err != nil {
		t.Fatalf("start %s: %v", name, err)
	}
	st, errCh := e.docker.ContainerWait(ctx, resp.ID, container.WaitConditionNotRunning)
	rc := -1
	select {
	case s := <-st:
		rc = int(s.StatusCode)
	case err := <-errCh:
		t.Fatalf("wait %s: %v", name, err)
	}
	lr, err := e.docker.ContainerLogs(ctx, resp.ID, container.LogsOptions{ShowStdout: true, ShowStderr: true})
	if err != nil {
		t.Fatalf("logs %s: %v", name, err)
	}
	var so, se bytes.Buffer
	_, _ = stdcopy.StdCopy(&so, &se, lr)
	lr.Close()
	out := strings.TrimSpace(so.String())
	if rc != 0 {
		out += " | stderr: " + strings.TrimSpace(se.String())
	}
	t.Logf("tenant %s rc=%d\n%s", name, rc, out)
	return out, rc
}

// realTask is a GPU task of the given tenant pinned to card 0.
func realTask(tenant int) *transport.Task { return gpuTask(tenant, "0") }

// probeTotal parses "PROBE_TOTAL pattern_words=N" out of the probe's stdout.
func probeTotal(out string) int {
	for _, ln := range strings.Split(out, "\n") {
		if strings.HasPrefix(ln, "PROBE_TOTAL pattern_words=") {
			var n int
			fmt.Sscanf(strings.TrimPrefix(ln, "PROBE_TOTAL pattern_words="), "%d", &n)
			return n
		}
	}
	return -1
}

func histOf(e *Executor) []ScrubResult {
	e.tenantScrubMu.Lock()
	defer e.tenantScrubMu.Unlock()
	return append([]ScrubResult(nil), e.tenantScrubHistory...)
}

// Phase 1: CONTROL, gate OFF. A writes, B probes, nothing scrubs. Shows what
// the driver does by itself (pattern_words 0 = driver zeroes new allocations).
func TestRealGPUScrub_P1_ControlGateOff(t *testing.T) {
	realEnabled(t)
	e := realExec(t, false, realImgGood)
	ctx := context.Background()
	if err := e.scrubForTask(ctx, realTask(1)); err != nil {
		t.Fatal(err)
	}
	w, rc := runTenant(t, e, "scrubtest-p1-a", realWriterScript)
	if rc != 0 || !strings.Contains(w, "WRITER") {
		t.Fatalf("writer failed: %s", w)
	}
	if err := e.scrubForTask(ctx, realTask(2)); err != nil {
		t.Fatal(err)
	}
	p, rc := runTenant(t, e, "scrubtest-p1-b", realProbeScript)
	t.Logf("P1 control (no scrub): scrubs run=%d probe pattern_words=%d", len(histOf(e)), probeTotal(p))
	if len(histOf(e)) != 0 || rc != 0 {
		t.Fatalf("gate off must not scrub (history=%d rc=%d)", len(histOf(e)), rc)
	}
}

// Phase 2: scrub ON. A (tenant 1) writes, B (tenant 2) arrives: the executor
// must run the helper (exit 0) BEFORE B's container exists, then B probes.
func TestRealGPUScrub_P2_ScrubOn(t *testing.T) {
	realEnabled(t)
	e := realExec(t, true, realImgGood)
	ctx := context.Background()
	// Tenant A's own first job: with the restart-gap fix this scrubs once
	// (unknown predecessor); without it the card has no predecessor.
	if err := e.scrubForTask(ctx, realTask(1)); err != nil {
		t.Fatal(err)
	}
	pre := len(histOf(e))
	w, rc := runTenant(t, e, "scrubtest-p2-a", realWriterScript)
	if rc != 0 || !strings.Contains(w, "WRITER") {
		t.Fatalf("writer failed: %s", w)
	}
	tStart := time.Now()
	if err := e.scrubForTask(ctx, realTask(2)); err != nil {
		t.Fatalf("scrub refused unexpectedly: %v", err)
	}
	tDone := time.Now()
	h := histOf(e)
	if len(h) != pre+1 {
		t.Fatalf("expected exactly one scrub on the A->B switch, history %d -> %d", pre, len(h))
	}
	last := h[len(h)-1]
	t.Logf("P2 scrub A->B: card=%s from=%s to=%s ok=%v bytes_zeroed=%d took=%s (switch decision+helper %s) BEFORE tenant B container creation",
		last.Card, last.FromTenant, last.ToTenant, last.OK, last.BytesZeroed, time.Duration(last.DurationMS)*time.Millisecond, tDone.Sub(tStart))
	if !last.OK || last.BytesZeroed <= 0 {
		t.Fatalf("scrub did not succeed: %+v", last)
	}
	p, rc := runTenant(t, e, "scrubtest-p2-b", realProbeScript)
	t.Logf("P2 tenant B probe after scrub: pattern_words=%d", probeTotal(p))
	if rc != 0 || probeTotal(p) != 0 {
		t.Fatalf("tenant B saw tenant A's pattern after the scrub (or probe failed): rc=%d total=%d", rc, probeTotal(p))
	}
}

// Phase 3: fail-closed. The helper image's python3 exits 3. The switch must
// be refused (error), tenant B's container must NOT be started, the ledger
// must still say tenant 1, and the failure is recorded ok=false. A retry with
// a good helper then scrubs and admits B.
func TestRealGPUScrub_P3_FailClosed(t *testing.T) {
	realEnabled(t)
	e := realExec(t, true, realImgBroken)
	ctx := context.Background()
	// Establish tenant 1 as the card's last tenant; a possible first-job
	// unknown-predecessor scrub would hit the broken helper too, so use the
	// ledger directly to isolate the A->B switch.
	e.tenantScrubMu.Lock()
	e.tenantScrubLast = map[string]string{"0": tenantLabelOf(realTask(1))}
	e.tenantScrubMu.Unlock()
	err := e.scrubForTask(ctx, realTask(2))
	started := false
	if err == nil {
		started = true // Run would proceed to create B's container
	}
	h := histOf(e)
	t.Logf("P3 broken helper: scrubForTask err=%v tenantB_started=%v history=%d", err, started, len(h))
	if err == nil || started {
		t.Fatalf("job was NOT refused")
	}
	if len(h) != 1 || h[0].OK {
		t.Fatalf("failure not recorded ok=false: %+v", h)
	}
	e.tenantScrubMu.Lock()
	still := e.tenantScrubLast["0"]
	e.tenantScrubMu.Unlock()
	if still != tenantLabelOf(realTask(1)) {
		t.Fatalf("ledger advanced on a failed scrub: %q", still)
	}
	// Retry once the helper is repaired.
	setImage(e, true, realImgGood)
	if err := e.scrubForTask(ctx, realTask(2)); err != nil {
		t.Fatalf("retry with good helper failed: %v", err)
	}
	h = histOf(e)
	t.Logf("P3 retry with good helper: ok=%v bytes_zeroed=%d history=%d", h[len(h)-1].OK, h[len(h)-1].BytesZeroed, len(h))
	if !h[len(h)-1].OK {
		t.Fatalf("retry not ok")
	}
}

// Restart gap, process 1: tenant 1's job on the card (gate ON), pattern left.
func TestRealGPUScrub_GapA_BeforeRestart(t *testing.T) {
	realEnabled(t)
	e := realExec(t, true, realImgGood)
	if err := e.scrubForTask(context.Background(), realTask(1)); err != nil {
		t.Fatal(err)
	}
	w, rc := runTenant(t, e, "scrubtest-gap-a", realWriterScript)
	t.Logf("GapA: process-1 scrubs=%d writer rc=%d %s", len(histOf(e)), rc, w)
}

// Restart gap, process 2 (a fresh executor = restarted worker, empty
// in-memory ledger): tenant 2's FIRST job on the same card. Logs how many
// scrubs the executor ran.
func TestRealGPUScrub_GapB_AfterRestart(t *testing.T) {
	realEnabled(t)
	e := realExec(t, true, realImgGood)
	if err := e.scrubForTask(context.Background(), realTask(2)); err != nil {
		t.Fatal(err)
	}
	n := len(histOf(e))
	p, _ := runTenant(t, e, "scrubtest-gap-b", realProbeScript)
	t.Logf("GapB: scrubs run before tenant 2's first job after restart=%d probe pattern_words=%d", n, probeTotal(p))
	if os.Getenv("SCRUBTEST_EXPECT_GAP") == "1" && n != 0 {
		t.Fatalf("gap expected but %d scrubs ran", n)
	}
	if os.Getenv("SCRUBTEST_EXPECT_GAP") == "0" && n != 1 {
		t.Fatalf("fix expected one scrub, got %d", n)
	}
}
