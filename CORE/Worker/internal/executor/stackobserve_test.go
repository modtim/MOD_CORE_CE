package executor

import (
	"context"
	"strings"
	"testing"
)

// The manager only ever knew the outcome of the last deploy task it dispatched,
// which says nothing about the hours since. Measured on the dev deployment: two
// vLLM stacks exited 2026-07-28 and the database still said `running` two days
// later, holding 15 of 16 GB of VRAM out of the node-task pool the whole time.
//
// The distinction these cover is the one that failed there: a container that
// EXISTS is not a container that is SERVING. `docker ps -a` showed both dead
// stacks; counting them as present is what let the manager go on believing.

func TestHealthy_RunningContainerCountsAsHosted(t *testing.T) {
	o := StackObservation{Project: "modstack10g0", Running: 1, Total: 1}
	if !o.Healthy() {
		t.Fatal("a project with a running container is not hosted")
	}
}

func TestHealthy_AllExitedIsNotHosted(t *testing.T) {
	// The exact dev-box state: containers present, none running.
	o := StackObservation{Project: "modstack10g0", Running: 0, Total: 2}
	if o.Healthy() {
		t.Fatal("a project whose containers have all exited reported as hosted; " +
			"this is the bug that held 15GB of VRAM for two days")
	}
}

func TestHealthy_PartiallyUpStillCountsButIsDistinguishable(t *testing.T) {
	// One of two services up: something is serving, so it is hosted — but the
	// manager must be able to see it is degraded, which is why Total is
	// reported rather than just a boolean.
	o := StackObservation{Project: "modstack11g0", Running: 1, Total: 2}
	if !o.Healthy() {
		t.Fatal("a partially-running project should still count as hosted")
	}
	if o.Running == o.Total {
		t.Fatal("Total must expose the degraded state, not be flattened away")
	}
}

func TestHealthy_EmptyProjectIsNotHosted(t *testing.T) {
	if (StackObservation{Project: "modstack12g0"}).Healthy() {
		t.Fatal("a project with no containers at all reported as hosted")
	}
}

// A host runs compose projects MOD did not deploy. The dev box's first report
// contained `mod` (31 of 35 containers — MOD Core itself), `deploy` and
// `vclick3d`. Every one would have surfaced as an orphan stack in the drift
// report, burying the real signal.
func TestManagedProjectPrefix_ExcludesForeignProjects(t *testing.T) {
	for _, p := range []string{"mod", "deploy", "vclick3d", "", "my-app"} {
		if strings.HasPrefix(p, managedProjectPrefix) {
			t.Fatalf("foreign compose project %q would be reported as a stack", p)
		}
	}
}

// §3.8a — the fence enumeration must not be O(every container ever run here).
//
// Measured on the dev worker 2026-08-01: 2232 containers, 46 running. Listing
// all of them exceeded the fencer's deadline, so the fence failed and (before
// §3.7) never retried, and the manager re-placed stacks that had never
// stopped. The regression would be silent — it only shows up as a fence timing
// out during a real partition.

// NOTE on the assertions below: `Filters.ExactMatch` is the wrong tool here and
// reads like the right one. It answers "would this value pass the filter", and
// returns TRUE when the key is absent entirely — so it cannot distinguish "no
// status filter" from "status=running". `Get` is what reports presence.
func TestStackListOptions_FencingAsksOnlyForRunningContainers(t *testing.T) {
	opts := stackListOptions(true)
	if opts.All {
		t.Fatal("fencing enumerated stopped containers too; this is the query " +
			"that timed out on a host with 2200 dead containers")
	}
	status := opts.Filters.Get("status")
	if len(status) != 1 || status[0] != "running" {
		t.Fatalf("no daemon-side status filter (%v); the host's history is back "+
			"on the fence's critical path", status)
	}
}

func TestStackListOptions_ObservationStillSeesStoppedContainers(t *testing.T) {
	// The opposite requirement, and the reason this is a parameter rather than
	// a blanket change: a project whose containers have all exited is the case
	// observation most needs to report. Narrowing this to running would
	// recreate the original 15GB-of-VRAM bug.
	opts := stackListOptions(false)
	if !opts.All {
		t.Fatal("observation stopped seeing exited containers; a dead stack " +
			"would become invisible rather than reported as down")
	}
	if len(opts.Filters.Get("status")) != 0 {
		t.Fatal("observation filtered to running containers; Total can no " +
			"longer distinguish 'partially up' from 'entirely gone'")
	}
}

func TestStackListOptions_BothScopeToComposeProjects(t *testing.T) {
	for _, runningOnly := range []bool{true, false} {
		labels := stackListOptions(runningOnly).Filters.Get("label")
		found := false
		for _, l := range labels {
			if l == composeProjectLabel {
				found = true
			}
		}
		if !found {
			t.Fatalf("runningOnly=%v dropped the compose-project label filter (%v)",
				runningOnly, labels)
		}
	}
}

func TestManagedProjectPrefix_KeepsEveryManagedForm(t *testing.T) {
	// The two producers: the default in _project_name, and the blue/green
	// rename in the migrate path.
	for _, p := range []string{"modstack1g0", "modstack10g0", "modstack99g7"} {
		if !strings.HasPrefix(p, managedProjectPrefix) {
			t.Fatalf("managed project %q would be dropped from observation", p)
		}
	}
}

// Per-stack serving VRAM: the parse is the fiddly, GPU-free-testable part.
// This is the ONLY per-stack VRAM measurement the platform has — it feeds
// StackProfile.serving_vram_gb — so its edge cases matter.

func TestParseComputeApps_SumsPerPID(t *testing.T) {
	// Two processes, plus a second row for one PID (multi-GPU process) that
	// must accumulate rather than overwrite.
	csv := "1234, 2048\n5678, 512\n1234, 1024\n"
	got := parseComputeApps(csv)
	if got[1234] != 3072 {
		t.Fatalf("pid 1234: want 3072 MiB summed, got %d", got[1234])
	}
	if got[5678] != 512 {
		t.Fatalf("pid 5678: want 512 MiB, got %d", got[5678])
	}
}

func TestParseComputeApps_SkipsUnreadableRows(t *testing.T) {
	// A driver emits [N/A] / [Not Supported] for processes it cannot
	// introspect. Those are UNKNOWN, not zero — skipping is the only honest
	// reading, and counting them as 0 would understate a stack's footprint.
	csv := "1234, [N/A]\n5678, [Not Supported]\n\n9999, 4096\ngarbage line\n"
	got := parseComputeApps(csv)
	if _, present := got[1234]; present {
		t.Fatal("an [N/A] memory row was counted; unknown must not read as 0")
	}
	if _, present := got[5678]; present {
		t.Fatal("a [Not Supported] memory row was counted")
	}
	if got[9999] != 4096 {
		t.Fatalf("the one valid row was lost: want 4096, got %d", got[9999])
	}
	if len(got) != 1 {
		t.Fatalf("expected exactly one attributable pid, got %d: %v", len(got), got)
	}
}

func TestParseComputeApps_NoGPUProcessesIsEmpty(t *testing.T) {
	if got := parseComputeApps(""); len(got) != 0 {
		t.Fatalf("empty compute-apps output must yield no pids, got %v", got)
	}
}

func TestSampleStackVRAM_NoGPULeavesUnmeasured(t *testing.T) {
	// When nvidia-smi is absent (gpuMemQuery reports !ok), every project stays
	// at VramGB == 0 (unmeasured) and nothing is fabricated. Restores the real
	// query afterwards so other tests are unaffected.
	orig := gpuMemQuery
	defer func() { gpuMemQuery = orig }()
	gpuMemQuery = func(ctx context.Context) (string, bool) { return "", false }

	byProject := map[string]*StackObservation{
		"modstack10g0": {Project: "modstack10g0", Running: 1, Total: 1},
	}
	running := map[string][]string{"modstack10g0": {"container-abc"}}

	(&Executor{}).sampleStackVRAM(context.Background(), byProject, running)

	if byProject["modstack10g0"].VramGB != 0 {
		t.Fatalf("no-GPU host fabricated a VRAM figure: %v",
			byProject["modstack10g0"].VramGB)
	}
}
