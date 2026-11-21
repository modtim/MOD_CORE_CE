package cleanup

// 25.5.s1 — LRU image GC invariants, exercised through the fake docker
// runtime (no daemon touched): protected images (pinned patterns, exact
// protected refs, anything referenced by a container in ANY state, anything
// inside the grace window) are never victims and never removed; victims are
// in LRU order; a dry-run (report-only) pass deletes nothing even when the
// count trigger fires; the reclaimable list is reported oldest-first.

import (
	"context"
	"testing"
	"time"
)

func TestCountCapTriggerWithoutDiskPressure(t *testing.T) {
	// Disk at 10% — nowhere near the watermark — but 5 eligible images over
	// a cap of 2: the 3 oldest become victims, the 2 newest stay. Input is
	// deliberately unsorted to prove the LRU sort runs before accumulating.
	cfg := Config{HighWaterPct: 85, LowWaterPct: 75, Grace: 24 * time.Hour,
		Reclaim: true, ImageCountCap: 2}
	images := []Image{
		{ID: "i5", RepoTags: []string{"r/e:1"}, Size: 1 << 20, LastUsed: old(40)},
		{ID: "i1", RepoTags: []string{"r/a:1"}, Size: 1 << 20, LastUsed: old(200)},
		{ID: "i4", RepoTags: []string{"r/d:1"}, Size: 1 << 20, LastUsed: old(50)},
		{ID: "i2", RepoTags: []string{"r/b:1"}, Size: 1 << 20, LastUsed: old(150)},
		{ID: "i3", RepoTags: []string{"r/c:1"}, Size: 1 << 20, LastUsed: old(100)},
	}
	plan := PlanImages(cfg, RootUsage{Total: 100 << 30, Used: 10 << 30}, images, nil, now)
	if plan.Triggered {
		t.Fatal("disk at 10% must not trigger the watermark")
	}
	if !plan.CountTriggered {
		t.Fatal("5 eligible over cap 2 must trigger the count gate")
	}
	if len(plan.Victims) != 3 {
		t.Fatalf("Victims = %d, want 3", len(plan.Victims))
	}
	want := []string{"i1", "i2", "i3"}
	for i := range want {
		if plan.Victims[i].ID != want[i] {
			t.Fatalf("cap victims = %v, want %v (LRU order)", idsOf(plan.Victims), want)
		}
	}
}

func TestCapZeroDisablesCountTrigger(t *testing.T) {
	cfg := Config{HighWaterPct: 85, LowWaterPct: 75, Grace: 24 * time.Hour,
		Reclaim: true, ImageCountCap: 0}
	images := []Image{
		{ID: "i1", RepoTags: []string{"r/a:1"}, Size: 1 << 20, LastUsed: old(200)},
		{ID: "i2", RepoTags: []string{"r/b:1"}, Size: 1 << 20, LastUsed: old(150)},
	}
	plan := PlanImages(cfg, RootUsage{Total: 100 << 30, Used: 10 << 30}, images, nil, now)
	if plan.CountTriggered || len(plan.Victims) != 0 {
		t.Fatalf("cap 0 must disable the count trigger: triggered=%v victims=%d",
			plan.CountTriggered, len(plan.Victims))
	}
	if plan.ReclaimableCount != 2 {
		t.Fatalf("ReclaimableCount = %d, want 2", plan.ReclaimableCount)
	}
}

func TestReclaimableListIsLRUOrderedDryRun(t *testing.T) {
	// Report-only (Reclaim false): the list answers "what could go" without
	// any deletion, even with the count trigger firing.
	cfg := Config{HighWaterPct: 85, LowWaterPct: 75, Grace: 24 * time.Hour,
		Reclaim: false, ImageCountCap: 1}
	images := []Image{
		{ID: "n1", RepoTags: []string{"r/new:1"}, Size: 1 << 20, LastUsed: old(30)},
		{ID: "o1", RepoTags: []string{"r/old:1"}, Size: 1 << 20, LastUsed: old(300)},
		{ID: "m1", RepoTags: []string{"r/mid:1"}, Size: 1 << 20, LastUsed: old(90)},
	}
	plan := PlanImages(cfg, RootUsage{Total: 100 << 30, Used: 10 << 30}, images, nil, now)
	if len(plan.ReclaimableImages) != 3 {
		t.Fatalf("ReclaimableImages = %d, want 3", len(plan.ReclaimableImages))
	}
	want := []string{"o1", "m1", "n1"}
	for i := range want {
		if plan.ReclaimableImages[i].ID != want[i] {
			t.Fatalf("reclaimable list = %v, want %v (LRU order)",
				idsOf(plan.ReclaimableImages), want)
		}
	}
}

func TestProtectedImagesNeverRemovedThroughRuntime(t *testing.T) {
	// A STOPPED container still pins its image; a compose/stack-labelled
	// container pins its image; pinned + protected refs are never victims.
	// Only the truly orphan image is removed, and only in reclaim mode.
	cfg := Config{HighWaterPct: 1, LowWaterPct: 0, Grace: 24 * time.Hour,
		Reclaim: true, ImageCountCap: 0,
		PinnedImagePatterns: []string{"ghcr.io/modtim/*"},
		ProtectedImageRefs:  []string{"ghcr.io/modtim/worker:latest"}}
	// fakeRuntime + fakeFS come from executor_test.go (same package).
	rt := &fakeRuntime{
		images: []Image{
			{ID: "img-stopped", RepoTags: []string{"r/stopped:1"}, Size: 1 << 20, LastUsed: old(72)},
			{ID: "img-stack", RepoTags: []string{"r/stack:1"}, Size: 1 << 20, LastUsed: old(72)},
			{ID: "img-harbor", RepoTags: []string{"ghcr.io/modtim/node:1"}, Size: 1 << 20, LastUsed: old(72)},
			{ID: "img-own", RepoTags: []string{"ghcr.io/modtim/worker:latest"}, Size: 1 << 20, LastUsed: old(72)},
			{ID: "img-orphan", RepoTags: []string{"r/orphan:1"}, Size: 1 << 20, LastUsed: old(72)},
		},
		containers: []Container{
			// stopped, no labels — still pins img-stopped (any state counts)
			{ID: "c1", Name: "stopped", State: "exited", ImageID: "img-stopped",
				Labels: map[string]string{}},
			// persistent stack member pins img-stack
			{ID: "c2", Name: "stack-member", State: "running", ImageID: "img-stack",
				Labels: map[string]string{stackLabel: "my-stack"}},
		},
		usage: RootUsage{Total: 10 << 20, Used: 10 << 20},
		usageOK: true,
	}
	fs := &fakeFS{}
	x := New(rt, fs, nil)
	// No active-task provider needed: images are the subject here.
	x.Run(context.Background(), cfg, Input{ActiveTaskIDs: map[string]bool{}})

	if len(rt.removedImages) != 1 || rt.removedImages[0] != "img-orphan" {
		t.Fatalf("removed images = %v, want exactly [img-orphan]", rt.removedImages)
	}
}

func TestDryRunCapTriggerDeletesNothing(t *testing.T) {
	// Report-only + count trigger: victims are REPORTED (reclaimable →
	// reclaimed visibility) but zero RemoveImage calls happen.
	cfg := Config{HighWaterPct: 85, LowWaterPct: 75, Grace: 24 * time.Hour,
		Reclaim: false, ImageCountCap: 1}
	rt := &fakeRuntime{
		images: []Image{
			{ID: "a-old", RepoTags: []string{"r/a:1"}, Size: 1 << 20, LastUsed: old(200)},
			{ID: "b-mid", RepoTags: []string{"r/b:1"}, Size: 1 << 20, LastUsed: old(100)},
			{ID: "c-new", RepoTags: []string{"r/c:1"}, Size: 1 << 20, LastUsed: old(40)},
		},
		// no containers at all — every image is unprotected
		containers: []Container{},
		usage: RootUsage{Total: 100 << 30, Used: 10 << 30}, // no disk pressure
		usageOK: true,
	}
	fs := &fakeFS{}
	x := New(rt, fs, nil)
	rep := x.Run(context.Background(), cfg, Input{ActiveTaskIDs: map[string]bool{}})

	if len(rt.removedImages) != 0 {
		t.Fatalf("report-only pass removed %d images; want 0", len(rt.removedImages))
	}
	if !rep.CountTriggered {
		t.Fatal("count trigger must be reported in dry-run mode")
	}
	if rep.imageCount != 3 {
		t.Fatalf("imageCount = %d, want 3", rep.imageCount)
	}
	if len(rep.ReclaimableImages) != 3 || rep.ReclaimableImages[0].ID != "a-old" {
			t.Fatalf("dry-run reclaimable list = %v, want oldest-first [a-old b-mid c-new]",
			idsOf(rep.ReclaimableImages))
	}
	// Victims (what a reclaim pass WOULD delete) respect the cap: keep 1
	// newest, free 2 oldest.
	if len(rep.ImageVictims) != 2 || rep.ImageVictims[0].ID != "a-old" ||
		rep.ImageVictims[1].ID != "b-mid" {
		t.Fatalf("dry-run victims = %v, want [a-old b-mid]", idsOf(rep.ImageVictims))
	}
}

// 25.5.s4 - reclaimable -> reclaimed accounting round-trip: per-kind reclaimed
// counts sum to the reclaimed object total, per-kind bytes freed sum to the
// reclaimed bytes, protected/pinned/in-use images appear in NEITHER the
// reclaimable list nor the reclaimed list, and the same fixture run report-only
// yields identical reclaimable numbers with zero reclaimed.
func TestAccountingReclaimableReclaimed(t *testing.T) {
	images := []Image{
		{ID: "img-a", RepoTags: []string{"r/a:1"}, Size: 4 << 30, LastUsed: old(100)},
		{ID: "img-b", RepoTags: []string{"r/b:1"}, Size: 2 << 30, LastUsed: old(300)},
		{ID: "img-inuse", RepoTags: []string{"r/inuse:1"}, Size: 8 << 30, LastUsed: old(300)},
		{ID: "img-pinned", RepoTags: []string{"ghcr.io/modtim/node:1"}, Size: 8 << 30, LastUsed: old(300)},
		{ID: "img-protected", RepoTags: []string{"ghcr.io/modtim/worker:latest"}, Size: 8 << 30, LastUsed: old(300)},
	}
	containers := []Container{
		{ID: "c-live", Name: "live", State: "running", ImageID: "img-inuse",
			Labels: map[string]string{"mod.worker": "1"}},
		{ID: "c-dead", Name: "dead", State: "exited", ImageID: "img-a",
			Labels: map[string]string{"mod.task": "42"}},
	}
	vols := []Volume{
		{Name: "modcore-task-x", CreatedAt: old(72), UsedBy: []string{},
			Labels: map[string]string{"com.example.mod-core.task-volume": "1"}},
	}
	nets := []Network{
		{ID: "n-task", Name: "mod-egr-42a", CreatedAt: old(72), Attached: 0,
			Labels: map[string]string{"mod.task": "42"}},
	}
	mk := func() *fakeRuntime {
		return &fakeRuntime{
			images: images, containers: containers, volumes: vols, networks: nets,
			usage: RootUsage{Total: 100 << 30, Used: 95 << 30}, usageOK: true,
		}
	}
	mkFS := func() *fakeFS {
		return &fakeFS{
			taskDirs: []TaskDir{{Path: "/root/42", TaskID: "42", ModifiedAt: old(72), Size: 1 << 30}},
			models: []ModelDir{
				{Path: "m--old", Size: 6 << 30, Atime: old(72)},
				{Path: "m--live", Size: 6 << 30, Atime: old(72), InUse: true},
			},
		}
	}
	cfg := Config{HighWaterPct: 85, LowWaterPct: 75, Grace: 24 * time.Hour,
		Reclaim: true, ImageCountCap: 0, ModelCacheCapBytes: 10 << 30,
		PinnedImagePatterns: []string{"ghcr.io/modtim/*"},
		ProtectedImageRefs: []string{"ghcr.io/modtim/worker:latest"}}
	in := Input{ActiveTaskIDs: map[string]bool{"42": false}, ModelRoot: "hub"}
	wantBytes := (int64(6) << 30) + (int64(6) << 30) + (int64(1) << 30)

	// PASS 1: reclaim.
	rt := mk()
	fs := mkFS()
	rep := New(rt, fs, nil).Run(context.Background(), cfg, in)

	// PLAN side: only the two eligible images; protected ones excluded.
	if rep.imageCount != 2 || rep.ReclaimableBytes() != wantBytes {
		t.Fatalf("reclaimable side: imageCount=%d bytes=%d, want 2 and %d",
			rep.imageCount, rep.ReclaimableBytes(), wantBytes)
	}
	for _, img := range rep.ReclaimableImages {
		if img.ID == "img-inuse" || img.ID == "img-pinned" || img.ID == "img-protected" {
			t.Fatalf("protected image %s in the reclaimable list", img.ID)
		}
	}

	// RECLAIMED side: per-kind counts sum to the reclaimed object total.
	var countTotal int
	for _, n := range rep.ReclaimedCounts {
		countTotal += n
	}
	objs := len(rep.ReclaimedImageIDs) + len(rep.ReclaimedModelDirs) +
		len(rep.ReclaimedContainers) + len(rep.ReclaimedVolumes) +
		len(rep.ReclaimedNetworks) + len(rep.ReclaimedTaskDirs)
	if countTotal != objs {
		t.Fatalf("per-kind reclaimed counts sum %d != reclaimed objects %d", countTotal, objs)
	}
	if countTotal != 7 {
		t.Fatalf("reclaimed objects = %d, want 7 (2 images, 1 model, 1 ct, 1 vol, 1 net, 1 dir)", countTotal)
	}
	// per-kind bytes freed sum to the reclaimed bytes (sized kinds only).
	var bytesTotal int64
	for _, b := range rep.Res.BytesFreed {
		bytesTotal += b
	}
	if bytesTotal != rep.ReclaimedBytes {
		t.Fatalf("BytesFreed sums %d != ReclaimedBytes %d", bytesTotal, rep.ReclaimedBytes)
	}
	if rep.ReclaimedBytes != wantBytes {
		t.Fatalf("ReclaimedBytes = %d, want %d", rep.ReclaimedBytes, wantBytes)
	}
	for _, id := range rep.ReclaimedImageIDs {
		if id == "img-inuse" || id == "img-pinned" || id == "img-protected" {
			t.Fatalf("protected image %s reclaimed", id)
		}
	}
	if len(rt.removedImages) != 2 || len(rt.removedContainers) != 1 ||
		len(rt.removedVolumes) != 1 || len(rt.removedNetworks) != 1 ||
		len(fs.removedModels) != 1 || len(fs.removedTaskDirs) != 1 {
		t.Fatalf("runtime removals = %v %v %v %v fs %v %v",
		rt.removedImages, rt.removedContainers, rt.removedVolumes,
		rt.removedNetworks, fs.removedModels, fs.removedTaskDirs)
	}

	// PASS 2: same fixture, report-only.
	dryCfg := cfg
	dryCfg.Reclaim = false
	rt2 := mk()
	fs2 := mkFS()
	dry := New(rt2, fs2, nil).Run(context.Background(), dryCfg, in)
	if dry.ReclaimableBytes() != rep.ReclaimableBytes() || dry.imageCount != rep.imageCount {
		t.Fatalf("dry-run reclaimable side differs: bytes %d vs %d, images %d vs %d",
		dry.ReclaimableBytes(), rep.ReclaimableBytes(), dry.imageCount, rep.imageCount)
	}
	if dry.ReclaimedBytes != 0 || len(dry.ReclaimedImageIDs) != 0 ||
		len(rt2.removedImages) != 0 || len(fs2.removedModels) != 0 {
		t.Fatalf("dry-run reclaimed nonzero: bytes %d ids %v", dry.ReclaimedBytes, dry.ReclaimedImageIDs)
	}
	if dry.Res == nil || dry.Res.Mode != "dry_run" {
		t.Fatalf("dry-run result mode wrong: nil=%v", dry.Res == nil)
	}
}
