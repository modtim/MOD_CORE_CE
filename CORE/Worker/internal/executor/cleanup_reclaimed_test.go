package executor

// 25.5.s4 — "reclaimable → reclaimed". The dry-run lists in CleanupSummary
// say what COULD go; these tests pin the other half: after a pass the worker
// reports what DID go (per kind: count + bytes freed where known), which
// deletes refused and why, and the mode/timestamps of the last pass.

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/cleanup"
)

func oldAt(n int) time.Time {
	return time.Now().Add(-time.Duration(n) * time.Hour)
}

func passCfg(reclaim bool) cleanup.Config {
	c := cleanup.DefaultConfig()
	c.Reclaim = reclaim
	c.HighWaterPct = 85
	c.LowWaterPct = 75
	c.ModelCacheCapBytes = 10 << 30
	c.Grace = 24 * time.Hour
	return c
}

// reclaimTestRuntime is a recording docker surface; failingIDs makes the
// matching Remove* call refuse, so the pass must record the failure and
// carry on with everything else.
type reclaimTestRuntime struct {
	failingImages  []string
	failingVolume  string
	seenImages     []string
	seenContainers []string
	seenVolumes    []string
	seenNetworks   []string
}

func (f *reclaimTestRuntime) ListContainers(ctx context.Context) ([]cleanup.Container, error) {
	return []cleanup.Container{
		{ID: "c-orphan", Name: "orphan", State: "exited", ImageID: "img-old",
			CreatedAt: oldAt(72), Labels: map[string]string{"mod.task": "42"}},
	}, nil
}
func (f *reclaimTestRuntime) ListImages(ctx context.Context) ([]cleanup.Image, error) {
	return []cleanup.Image{
		{ID: "img-old", RepoTags: []string{"r/old:1"}, Size: 10 << 30, LastUsed: oldAt(72)},
		{ID: "img-stubborn", RepoTags: []string{"r/stubborn:1"}, Size: 20 << 30, LastUsed: oldAt(72)},
	}, nil
}
func (f *reclaimTestRuntime) ListVolumes(ctx context.Context) ([]cleanup.Volume, error) {
	return []cleanup.Volume{
		{Name: "modcore-task-x", CreatedAt: oldAt(72),
			Labels: map[string]string{"com.example.mod-core.task-volume": "1"}},
	}, nil
}
func (f *reclaimTestRuntime) ListNetworks(ctx context.Context) ([]cleanup.Network, error) {
	return []cleanup.Network{
		{ID: "n1", Name: "mod-egr-42a", CreatedAt: oldAt(72),
			Labels: map[string]string{"mod.task": "42"}, Attached: 0},
	}, nil
}
func (f *reclaimTestRuntime) DockerRootUsage(ctx context.Context) (cleanup.RootUsage, bool) {
	return cleanup.RootUsage{Total: 100 << 30, Used: 99 << 30}, true
}
func (f *reclaimTestRuntime) RemoveContainer(ctx context.Context, id string) error {
	f.seenContainers = append(f.seenContainers, id)
	return nil
}
func (f *reclaimTestRuntime) RemoveImage(ctx context.Context, id string) error {
	f.seenImages = append(f.seenImages, id)
	for _, bad := range f.failingImages {
		if bad == id {
			return errors.New("client error: 409 image is in use")
		}
	}
	return nil
}
func (f *reclaimTestRuntime) RemoveVolume(ctx context.Context, name string) error {
	f.seenVolumes = append(f.seenVolumes, name)
	if name == f.failingVolume {
		return errors.New("client error: 500 volume is mounted")
	}
	return nil
}
func (f *reclaimTestRuntime) RemoveNetwork(ctx context.Context, id string) error {
	f.seenNetworks = append(f.seenNetworks, id)
	return nil
}

type reclaimTestFS struct {
	failingTaskDir string
	seenTaskDirs   []string
	seenModels     []string
}

func (f *reclaimTestFS) ListTaskDirs(ctx context.Context) ([]cleanup.TaskDir, error) {
	return []cleanup.TaskDir{
		{Path: "/root/42", TaskID: "42", ModifiedAt: oldAt(72), Size: 1 << 20},
	}, nil
}
func (f *reclaimTestFS) RemoveTaskDir(ctx context.Context, path string) error {
	f.seenTaskDirs = append(f.seenTaskDirs, path)
	if path == f.failingTaskDir {
		return errors.New("fs error: permission denied")
	}
	return nil
}
func (f *reclaimTestFS) ListModelDirs(ctx context.Context, root string) ([]cleanup.ModelDir, error) {
	if root == "" {
		return nil, nil
	}
	return []cleanup.ModelDir{
		{Path: "m--old", Size: 30 << 30, Atime: oldAt(72)},
	}, nil
}
func (f *reclaimTestFS) RemoveModelDir(ctx context.Context, root, path string) error {
	f.seenModels = append(f.seenModels, path)
	return nil
}

// TestDryRunPassReportsZeroReclaimed: a report-only pass must land a
// closed result (mode dry_run, zero counts, zero bytes, no failures) even
// though plenty is reclaimable — and must delete nothing.
func TestDryRunPassReportsZeroReclaimed(t *testing.T) {
	rt := &reclaimTestRuntime{}
	fs := &reclaimTestFS{}
	eng := cleanup.New(rt, fs, nil)
	rep := eng.Run(context.Background(), passCfg(false), cleanup.Input{
		ActiveTaskIDs: map[string]bool{},
		ModelRoot:     "hub",
	})
	if rep.Res == nil {
		t.Fatal("dry-run pass has no Result")
	}
	if rep.Res.Mode != "dry_run" {
		t.Fatalf("mode = %s, want dry_run", rep.Res.Mode)
	}
	if rep.Res.Started.IsZero() || rep.Res.Finished.Before(rep.Res.Started) {
		t.Fatal("pass timestamps missing")
	}
	if len(rep.Res.Counts) != 0 || len(rep.Res.Failures) != 0 ||
		rep.Res.ReclaimedBytes != 0 {
		t.Fatalf("dry_run result not empty: counts=%v failures=%v bytes=%d",
			rep.Res.Counts, rep.Res.Failures, rep.Res.ReclaimedBytes)
	}
	if len(rt.seenImages) != 0 || len(rt.seenContainers) != 0 ||
		len(rt.seenVolumes) != 0 || len(rt.seenNetworks) != 0 ||
		len(fs.seenTaskDirs) != 0 || len(fs.seenModels) != 0 {
		t.Fatal("dry_run pass deleted something")
	}
	// The reclaimable side is still reported, so the operator sees what a
	// later reclaim pass would remove.
	if rep.Counts()["images"] == 0 {
		t.Fatal("dry_run pass reports no reclaimable images")
	}
}

// TestReclaimPassCountsMatchPlan: every planned object that the runtime
// accepted is counted per kind, bytes freed land only on the kinds whose
// snapshot knows a size (images/model_dirs/task_dirs), and the pass is
// stamped mode=reclaim with start/finish.
func TestReclaimPassCountsMatchPlan(t *testing.T) {
	rt := &reclaimTestRuntime{}
	fs := &reclaimTestFS{}
	eng := cleanup.New(rt, fs, nil)
	rep := eng.Run(context.Background(), passCfg(true), cleanup.Input{
		ActiveTaskIDs: map[string]bool{},
		ModelRoot:     "hub",
	})
	res := rep.Res
	if res == nil || res.Mode != "reclaim" || res.Started.IsZero() ||
		res.Finished.Before(res.Started) {
		t.Fatalf("result missing/mis-stamped: %+v", res)
	}
	// need = 99-75 = 24 GB; LRU order eats img-old (10) then img-stubborn
	// (20) — both planned and (here) both accepted.
	if res.Counts["images"] != 2 || res.Counts["containers"] != 1 ||
		res.Counts["networks"] != 1 || res.Counts["task_dirs"] != 1 ||
		res.Counts["model_dirs"] != 1 {
		t.Fatalf("counts = %v", res.Counts)
	}
	if res.BytesFreed["images"] != 30<<30 ||
		res.BytesFreed["model_dirs"] != 30<<30 ||
		res.BytesFreed["task_dirs"] != 1<<20 {
		t.Fatalf("bytes_freed = %v", res.BytesFreed)
	}
	if _, ok := res.BytesFreed["containers"]; ok {
		t.Fatal("containers carry no size — must not appear in bytes_freed")
	}
	if res.ReclaimedBytes != (30<<30)+(30<<30)+(1<<20) {
		t.Fatalf("ReclaimedBytes = %d", res.ReclaimedBytes)
	}
	if len(rt.seenImages) != 2 || len(rt.seenContainers) != 1 ||
		len(rt.seenNetworks) != 1 || len(fs.seenTaskDirs) != 1 ||
		len(fs.seenModels) != 1 {
		t.Fatal("runtime call counts wrong")
	}
}

// TestDeleteFailureRecordedAndPassContinues: one refused delete (volume)
// is recorded with kind+ref+reason, does NOT abort the rest of the pass,
// and its bytes/counts stay out of the reclaimed accounting.
func TestDeleteFailureRecordedAndPassContinues(t *testing.T) {
	rt := &reclaimTestRuntime{failingVolume: "modcore-task-x"}
	fs := &reclaimTestFS{}
	eng := cleanup.New(rt, fs, nil)
	rep := eng.Run(context.Background(), passCfg(true), cleanup.Input{
		ActiveTaskIDs: map[string]bool{},
		ModelRoot:     "hub",
	})
	res := rep.Res
	if len(res.Failures) != 1 {
		t.Fatalf("failures = %v, want exactly one", res.Failures)
	}
	f := res.Failures[0]
	if f.Kind != "volumes" || f.Ref != "modcore-task-x" || f.Reason == "" {
		t.Fatalf("failure = %+v", f)
	}
	if _, ok := res.Counts["volumes"]; ok {
		t.Fatal("failed volume counted as reclaimed")
	}
	// The rest of the pass still ran.
	if res.Counts["containers"] != 1 || res.Counts["networks"] != 1 ||
		res.Counts["task_dirs"] != 1 || res.Counts["model_dirs"] != 1 {
		t.Fatalf("pass aborted after failure: counts=%v", res.Counts)
	}
	if len(rt.seenNetworks) != 1 || len(fs.seenTaskDirs) != 1 {
		t.Fatal("kinds after the failed one never ran")
	}
}

// TestHeartbeatExposesLastCleanup: the bridge adds the `last_cleanup` key
// (mode + timestamps + per-kind counts/bytes + failures) to the heartbeat
// summary without dropping any of the pre-existing keys.
func TestHeartbeatExposesLastCleanup(t *testing.T) {
	rt := &reclaimTestRuntime{failingVolume: "modcore-task-x"}
	fs := &reclaimTestFS{}
	eng := cleanup.New(rt, fs, nil)
	rep := eng.Run(context.Background(), passCfg(true), cleanup.Input{
		ActiveTaskIDs: map[string]bool{},
		ModelRoot:     "hub",
	})
	e := &Executor{}
	e.storeCleanupReport(rep)
	cs := e.CleanupSummary()
	if cs == nil {
		t.Fatal("no summary after a pass")
	}
	lc, ok := cs["last_cleanup"]
	if !ok {
		t.Fatal("last_cleanup key missing")
	}
	lcm := lc.(map[string]interface{})
	lm := lcm["mode"]
	if lm == nil {
		t.Fatal("last_cleanup.mode missing")
	}
	if lm != "reclaim" {
		t.Fatalf("last_cleanup.mode = %v", lm)
	}
	if lcm["failures"] == nil {
		t.Fatal("failures key missing")
	}
	// Pre-existing keys survive an additive change.
	for _, k := range []string{"last_run", "mode", "reclaimable_bytes",
		"reclaimed_bytes", "counts", "reclaimed_counts"} {
		if _, ok := cs[k]; !ok {
			t.Fatalf("pre-existing key %s dropped", k)
		}
	}
	// A hand-built Report (no Result) omits the key rather than lying.
	bare := cleanup.NewReport(cleanup.DefaultConfig(), cleanup.RootUsage{},
		cleanup.ImagePlan{}, cleanup.ModelPlan{}, nil, nil, nil, nil, time.Now())
	if cleanupPassSummary(bare) != nil {
		t.Fatal("nil Result must omit last_cleanup")
	}
}
