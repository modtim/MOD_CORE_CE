package cleanup

import (
	"context"
	"sort"
	"testing"
	"time"
)

// fakeRuntime records every mutation call so tests can assert exactly what
// a pass would (or did) delete.
type fakeRuntime struct {
	containers []Container
	images     []Image
	volumes    []Volume
	networks   []Network
	usage      RootUsage
	usageOK    bool

	removedContainers []string
	removedImages     []string
	removedVolumes    []string
	removedNetworks   []string
}

func (f *fakeRuntime) ListContainers(ctx context.Context) ([]Container, error) {
	return f.containers, nil
}
func (f *fakeRuntime) ListImages(ctx context.Context) ([]Image, error) { return f.images, nil }
func (f *fakeRuntime) ListVolumes(ctx context.Context) ([]Volume, error) {
	return f.volumes, nil
}
func (f *fakeRuntime) ListNetworks(ctx context.Context) ([]Network, error) {
	return f.networks, nil
}
func (f *fakeRuntime) DockerRootUsage(ctx context.Context) (RootUsage, bool) {
	return f.usage, f.usageOK
}
func (f *fakeRuntime) RemoveContainer(ctx context.Context, id string) error {
	f.removedContainers = append(f.removedContainers, id)
	return nil
}
func (f *fakeRuntime) RemoveImage(ctx context.Context, id string) error {
	f.removedImages = append(f.removedImages, id)
	return nil
}
func (f *fakeRuntime) RemoveVolume(ctx context.Context, name string) error {
	f.removedVolumes = append(f.removedVolumes, name)
	return nil
}
func (f *fakeRuntime) RemoveNetwork(ctx context.Context, id string) error {
	f.removedNetworks = append(f.removedNetworks, id)
	return nil
}

// fakeFS is an in-memory filesystem with the same recording discipline.
type fakeFS struct {
	taskDirs []TaskDir
	models   []ModelDir
	removedTaskDirs []string
	removedModels   []string
}

func (f *fakeFS) ListTaskDirs(ctx context.Context) ([]TaskDir, error) { return f.taskDirs, nil }
func (f *fakeFS) ListModelDirs(ctx context.Context, root string) ([]ModelDir, error) {
	if root == "" {
		return nil, nil
	}
	return f.models, nil
}
func (f *fakeFS) RemoveTaskDir(ctx context.Context, path string) error {
	f.removedTaskDirs = append(f.removedTaskDirs, path)
	return nil
}
func (f *fakeFS) RemoveModelDir(ctx context.Context, root, path string) error {
	f.removedModels = append(f.removedModels, path)
	return nil
}

func reclaimCfg() Config {
	c := DefaultConfig()
	c.Reclaim = true
	c.HighWaterPct = 85
	c.LowWaterPct = 75
	c.ModelCacheCapBytes = 10 << 30
	c.Grace = 24 * time.Hour
	return c
}

func TestReportOnlyDeletesNothing(t *testing.T) {
	rt := &fakeRuntime{
		usage:   RootUsage{Total: 100 << 30, Used: 95 << 30}, // over high water
		usageOK: true,
		images: []Image{
			{ID: "img-old", RepoTags: []string{"r/old:1"}, Size: 2 << 30, LastUsed: old(72)},
			{ID: "img-inuse", RepoTags: []string{"r/base:1"}, Size: 2 << 30, LastUsed: old(72)},
		},
		containers: []Container{
			// pins img-inuse
			{ID: "c1", Name: "base", State: "running", ImageID: "img-inuse",
				CreatedAt: old(1), Labels: map[string]string{composeProjectLabel: "startup"}},
			// orphan task container
			{ID: "c2", Name: "orphan", State: "exited", ImageID: "img-old",
				CreatedAt: old(72), Labels: map[string]string{"mod.task": "42"}},
		},
		volumes:  []Volume{{Name: "modcore-task-x", CreatedAt: old(72), Labels: map[string]string{"com.example.mod-core.task-volume": "1"}}},
		networks: []Network{{ID: "n1", Name: "mod-egr-42a", CreatedAt: old(72), Labels: map[string]string{"mod.task": "42"}}},
	}
	fs := &fakeFS{
		taskDirs: []TaskDir{{Path: "/root/42", TaskID: "42", ModifiedAt: old(72), Size: 1 << 20}},
		models:   []ModelDir{{Path: "m--old", Size: 20 << 30, Atime: old(72)}},
	}
	x := New(rt, fs, nil)

	cfg := reclaimCfg()
	cfg.Reclaim = false // REPORT ONLY
	in := Input{
		ActiveTaskIDs: map[string]bool{},
		ModelRoot:     "hub",
	}
	rep := x.Run(context.Background(), cfg, in)

	if rep.Reclaim {
		t.Fatal("report says reclaim=true")
	}
	// Reclaimable numbers populated...
	if rep.ReclaimableBytes() <= 0 {
		t.Fatal("expected nonzero reclaimable bytes in report-only mode")
	}
	if c := rep.Counts(); c["images"] == 0 || c["containers"] == 0 || c["volumes"] == 0 || c["networks"] == 0 || c["task_dirs"] == 0 || c["model_dirs"] == 0 {
		t.Fatalf("counts incomplete in report-only mode: %v", c)
	}
	// ...but ZERO deletions through the runtime or the FS.
	if len(rt.removedContainers) != 0 || len(rt.removedImages) != 0 ||
		len(rt.removedVolumes) != 0 || len(rt.removedNetworks) != 0 ||
		len(fs.removedTaskDirs) != 0 || len(fs.removedModels) != 0 {
		t.Fatalf("report-only mode performed deletions: rt=%v fs=%v", rt, fs)
	}
	if rep.ReclaimedBytes != 0 {
		t.Fatalf("report-only mode reports reclaimed bytes %d", rep.ReclaimedBytes)
	}
	// The in-use image must never appear as reclaimable.
	for _, v := range rep.ImageVictims {
		if v.ID == "img-inuse" {
			t.Fatal("in-use image listed as victim")
		}
	}
}

func TestReclaimRemovesOnlyPlannedObjects(t *testing.T) {
	rt := &fakeRuntime{
		usage:   RootUsage{Total: 100 << 30, Used: 95 << 30},
		usageOK: true,
		images: []Image{
			{ID: "img-old", RepoTags: []string{"r/old:1"}, Size: 40 << 30, LastUsed: old(72)},
			{ID: "img-inuse", RepoTags: []string{"r/base:1"}, Size: 40 << 30, LastUsed: old(72)},
			{ID: "img-pinned", RepoTags: []string{"ghcr.io/modtim/keep:1"}, Size: 40 << 30, LastUsed: old(72)},
		},
		containers: []Container{
			{ID: "c1", Name: "base", State: "running", ImageID: "img-inuse",
				CreatedAt: old(1), Labels: map[string]string{composeProjectLabel: "startup"}},
			{ID: "c2", Name: "orphan", State: "exited", ImageID: "img-old",
				CreatedAt: old(72), Labels: map[string]string{"mod.task": "42"}},
		},
		volumes: []Volume{
			{Name: "modcore-task-x", CreatedAt: old(72), Labels: map[string]string{"com.example.mod-core.task-volume": "1"}},
			{Name: "v-keep", CreatedAt: old(72), Labels: map[string]string{"com.example.mod-core.task-volume": "1", keepLabel: "1"}},
			{Name: "v-user", CreatedAt: old(72), Labels: map[string]string{}},
		},
		networks: []Network{
			{ID: "n1", Name: "mod-egr-42a", CreatedAt: old(72), Labels: map[string]string{"mod.task": "42"}},
			{ID: "n2", Name: "mod-user-egress", CreatedAt: old(999), Labels: map[string]string{"mod.purpose": "egress"}},
		},
	}
	fs := &fakeFS{
		taskDirs: []TaskDir{
			{Path: "/root/42", TaskID: "42", ModifiedAt: old(72), Size: 1 << 20},
			{Path: "/root/77", TaskID: "77", ModifiedAt: old(72), Size: 1 << 20}, // active
		},
		models: []ModelDir{
			{Path: "m--old", Size: 30 << 30, Atime: old(72)},
			{Path: "m--live", Size: 30 << 30, Atime: old(72), InUse: true},
		},
	}
	x := New(rt, fs, nil)

	in := Input{
		ActiveTaskIDs: map[string]bool{"77": true},
		ModelRoot:     "hub",
	}
	rep := x.Run(context.Background(), reclaimCfg(), in)

	// Image: need = 95 - 75 = 20 GB; only img-old eligible → single victim.
	if len(rt.removedImages) != 1 || rt.removedImages[0] != "img-old" {
		t.Fatalf("removed images = %v, want [img-old]", rt.removedImages)
	}
	// Containers/volumes/networks: exactly the planned orphans.
	if len(rt.removedContainers) != 1 || rt.removedContainers[0] != "c2" {
		t.Fatalf("removed containers = %v", rt.removedContainers)
	}
	if len(rt.removedVolumes) != 1 || rt.removedVolumes[0] != "modcore-task-x" {
		t.Fatalf("removed volumes = %v (keep-labelled and user volumes must survive)", rt.removedVolumes)
	}
	if len(rt.removedNetworks) != 1 || rt.removedNetworks[0] != "n1" {
		t.Fatalf("removed networks = %v (shared infra must survive)", rt.removedNetworks)
	}
	// Task dirs: active 77 survives.
	if len(fs.removedTaskDirs) != 1 || fs.removedTaskDirs[0] != "/root/42" {
		t.Fatalf("removed task dirs = %v", fs.removedTaskDirs)
	}
	// Models: cache total 60 GB > 10 GB cap; m--live in-use survives.
	if len(fs.removedModels) != 1 || fs.removedModels[0] != "m--old" {
		t.Fatalf("removed models = %v", fs.removedModels)
	}

	// Reclaimed accounting.
	if rep.ReclaimedBytes != (40<<30)+(30<<30)+(1<<20) {
		t.Fatalf("ReclaimedBytes = %d, want %d", rep.ReclaimedBytes, (40<<30)+(30<<30)+(1<<20))
	}
	got := map[string]int{}
	for k, v := range rep.ReclaimedCounts {
		if v > 0 {
			got[k] = v
		}
	}
	if got["images"] != 1 || got["containers"] != 1 || got["volumes"] != 1 ||
		got["networks"] != 1 || got["task_dirs"] != 1 || got["model_dirs"] != 1 {
		t.Fatalf("ReclaimedCounts = %v", rep.ReclaimedCounts)
	}
	if !sort.StringsAreSorted(rt.removedImages) {
		t.Fatalf("n/a")
	}
}

func TestRunToleratesListingFailures(t *testing.T) {
	// A runtime that errors on every listing still yields a report — and
	// never deletes.
	rt := &failingRuntime{}
	fs := &failingFS{}
	x := New(rt, fs, nil)
	rep := x.Run(context.Background(), reclaimCfg(), Input{ActiveTaskIDs: map[string]bool{}})
	if rep == nil {
		t.Fatal("nil report")
	}
	if rep.ReclaimableBytes() != 0 {
		t.Fatalf("ReclaimableBytes = %d, want 0 on total listing failure", rep.ReclaimableBytes())
	}
}

type failingRuntime struct{ fakeRuntime }

func (failingRuntime) ListContainers(ctx context.Context) ([]Container, error) {
	return nil, context.Canceled
}
func (failingRuntime) ListImages(ctx context.Context) ([]Image, error) {
	return nil, context.Canceled
}
func (failingRuntime) ListVolumes(ctx context.Context) ([]Volume, error) {
	return nil, context.Canceled
}
func (failingRuntime) ListNetworks(ctx context.Context) ([]Network, error) {
	return nil, context.Canceled
}
func (failingRuntime) DockerRootUsage(ctx context.Context) (RootUsage, bool) {
	return RootUsage{}, false
}

type failingFS struct{ fakeFS }

func (failingFS) ListTaskDirs(ctx context.Context) ([]TaskDir, error) {
	return nil, context.Canceled
}
func (failingFS) ListModelDirs(ctx context.Context, root string) ([]ModelDir, error) {
	return nil, context.Canceled
}
