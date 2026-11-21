package cleanup

import (
	"testing"
	"time"
)

// now is fixed so every age/grace assertion is deterministic.
var now = time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC)

func old(n int) time.Time { return now.Add(-time.Duration(n) * time.Hour) }

func TestPlanImagesWatermarksAndLRUOrder(t *testing.T) {
	// 100 GB root, 90 GB used → 90% ≥ 85% high water. Low water 75% → need
	// to free 15 GB to reach 75 GB used.
	cfg := Config{
		Reclaim:      true,
		HighWaterPct: 85,
		LowWaterPct:  75,
		Grace:        24 * time.Hour,
	}
	// 1 GB images, oldest first in LRU. inUse pins img4.
	images := []Image{
		{ID: "img4", RepoTags: []string{"r/pin:1"}, Size: 1 << 30, LastUsed: old(30)},
		{ID: "img1", RepoTags: []string{"r/a:1"}, Size: 1 << 30, LastUsed: old(60)},
		{ID: "img2", RepoTags: []string{"r/b:1"}, Size: 1 << 30, LastUsed: old(50)},
		{ID: "img3", RepoTags: []string{"r/c:1"}, Size: 1 << 30, LastUsed: old(40)},
	}
	inUse := map[string]bool{"img4-sha": true} // full sha prefix of img4
	plan := PlanImages(cfg, RootUsage{Total: 100 << 30, Used: 90 << 30}, images, inUse, now)

	if !plan.Triggered {
		t.Fatal("expected trigger at 90% used (high 85)")
	}
	if plan.UsedPct != 90 {
		t.Fatalf("UsedPct = %d, want 90", plan.UsedPct)
	}
	// Need = 90GB - 75GB = 15GB... but only 3 eligible 1GB images exist
	// (img4 pinned via in-use). Reclaimable counts all 3.
	if plan.Reclaimable != 3<<30 {
		t.Fatalf("Reclaimable = %d, want %d", plan.Reclaimable, 3<<30)
	}
	// 15 GB need > 3 available → all three become victims, oldest first.
	if len(plan.Victims) != 3 {
		t.Fatalf("Victims = %d, want 3", len(plan.Victims))
	}
	got := []string{plan.Victims[0].ID, plan.Victims[1].ID, plan.Victims[2].ID}
	want := []string{"img1", "img2", "img3"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("LRU order = %v, want %v", got, want)
		}
	}
}

func TestPlanImagesBelowHighWaterNoVictims(t *testing.T) {
	cfg := Config{HighWaterPct: 85, LowWaterPct: 75, Grace: 24 * time.Hour}
	images := []Image{
		{ID: "img1", RepoTags: []string{"r/a:1"}, Size: 1 << 30, LastUsed: old(60)},
	}
	// 50% used < 85% → not triggered, no victims, but still reclaimable.
	plan := PlanImages(cfg, RootUsage{Total: 100 << 30, Used: 50 << 30}, images, nil, now)
	if plan.Triggered {
		t.Fatal("should not trigger below high water")
	}
	if len(plan.Victims) != 0 {
		t.Fatalf("Victims = %d, want 0", len(plan.Victims))
	}
	if plan.Reclaimable != 1<<30 {
		t.Fatalf("Reclaimable = %d, want %d", plan.Reclaimable, 1<<30)
	}
}

func TestPlanImagesProtectedRules(t *testing.T) {
	cfg := Config{HighWaterPct: 1, LowWaterPct: 0, Grace: 24 * time.Hour,
		PinnedImagePatterns:  []string{"ghcr.io/modtim/*"},
		ProtectedImageRefs:   []string{"ghcr.io/modtim/worker:latest"},
	}
	images := []Image{
		{ID: "i-inuse", RepoTags: []string{"x/base:1"}, Size: 1 << 20, LastUsed: old(48)},   // pinned by in-use
		{ID: "i-pat", RepoTags: []string{"ghcr.io/modtim/node:1"}, Size: 1 << 20, LastUsed: old(48)}, // pinned by glob
		{ID: "i-own", RepoTags: []string{"ghcr.io/modtim/worker:latest"}, Size: 1 << 20, LastUsed: old(48)}, // worker own image (protected ref)
		{ID: "i-new", RepoTags: []string{"x/fresh:1"}, Size: 1 << 20, LastUsed: old(2)},    // within grace
		{ID: "i-ok", RepoTags: []string{"x/old:1"}, Size: 1 << 20, LastUsed: old(48)},      // eligible
	}
	inUse := map[string]bool{"i-inuse": true}
	plan := PlanImages(cfg, RootUsage{Total: 10 << 20, Used: 10 << 20}, images, inUse, now)

	// Only i-ok eligible.
	if plan.ReclaimableCount != 1 {
		t.Fatalf("ReclaimableCount = %d, want 1", plan.ReclaimableCount)
	}
	if len(plan.Victims) != 1 || plan.Victims[0].ID != "i-ok" {
		t.Fatalf("Victims = %v, want only [i-ok]", idsOf(plan.Victims))
	}
}

func TestPlanImagesCreatedFallbackWhenNeverTouched(t *testing.T) {
	cfg := Config{HighWaterPct: 1, LowWaterPct: 0, Grace: 24 * time.Hour}
	// LastUsed zero → falls back to CreatedAt (48h ago) → eligible.
	images := []Image{{ID: "i", RepoTags: []string{"x/z:1"}, Size: 1 << 20, CreatedAt: old(48)}}
	plan := PlanImages(cfg, RootUsage{Total: 10 << 20, Used: 10 << 20}, images, nil, now)
	if plan.ReclaimableCount != 1 {
		t.Fatalf("ReclaimableCount = %d, want 1 (created fallback)", plan.ReclaimableCount)
	}
}

func idsOf(images []Image) []string {
	out := make([]string, 0, len(images))
	for _, i := range images {
		out = append(out, i.ID)
	}
	return out
}

func TestPlanModelDirsCapAndLRU(t *testing.T) {
	cfg := Config{ModelCacheCapBytes: 10 << 30, Grace: 24 * time.Hour}
	dirs := []ModelDir{
		{Path: "m-inuse", Size: 4 << 30, Atime: old(60), InUse: true},
		{Path: "m-old", Size: 4 << 30, Atime: old(72)},
		{Path: "m-mid", Size: 4 << 30, Atime: old(48)},
		{Path: "m-new", Size: 4 << 30, Atime: old(2)},
	}
	// Total 16 GB > 10 GB cap. In-use + recent excluded → eligible m-old, m-mid (8 GB).
	plan := PlanModelDirs(cfg, dirs, now)
	if plan.TotalBytes != 16<<30 {
		t.Fatalf("TotalBytes = %d, want %d", plan.TotalBytes, 16<<30)
	}
	if plan.Reclaimable != 8<<30 {
		t.Fatalf("Reclaimable = %d, want %d", plan.Reclaimable, 8<<30)
	}
	// Need 16-10 = 6 GB. LRU: m-old (4GB) first, then m-mid.
	if len(plan.Victims) != 2 {
		t.Fatalf("Victims = %d, want 2", len(plan.Victims))
	}
	if plan.Victims[0].Path != "m-old" || plan.Victims[1].Path != "m-mid" {
		t.Fatalf("victim order = %v,%v want m-old,m-mid", plan.Victims[0].Path, plan.Victims[1].Path)
	}
}

func TestPlanModelDirsUnderCapNoVictims(t *testing.T) {
	cfg := Config{ModelCacheCapBytes: 100 << 30, Grace: 24 * time.Hour}
	dirs := []ModelDir{{Path: "m", Size: 10 << 30, Atime: old(100)}}
	plan := PlanModelDirs(cfg, dirs, now)
	if len(plan.Victims) != 0 {
		t.Fatalf("Victims = %d, want 0 under cap", len(plan.Victims))
	}
	if plan.Reclaimable != 0 {
		t.Fatalf("Reclaimable = %d, want 0 under cap", plan.Reclaimable)
	}
}

func TestPlanModelDirsCapDisabled(t *testing.T) {
	cfg := Config{ModelCacheCapBytes: 0, Grace: 24 * time.Hour}
	dirs := []ModelDir{{Path: "m", Size: 10 << 30, Atime: old(100)}}
	plan := PlanModelDirs(cfg, dirs, now)
	if len(plan.Victims) != 0 {
		t.Fatalf("Victims = %d, want 0 when cap disabled", len(plan.Victims))
	}
}

func TestPlanContainersLabelledOnlyAndStates(t *testing.T) {
	cfg := Config{Grace: 24 * time.Hour}
	// active task 77; task 88 is not active.
	active := map[string]bool{"77": true}
	conts := []Container{
		// no MOD label → never
		{Name: "unlabeled", State: "exited", CreatedAt: old(100), Labels: map[string]string{"other": "x"}},
		// MOD-labelled but compose-managed (stack) → never
		{Name: "stack", State: "exited", CreatedAt: old(100),
			Labels: map[string]string{"mod.task": "88", composeProjectLabel: "mystack"}},
		// MOD-labelled stack label → never
		{Name: "stack2", State: "exited", CreatedAt: old(100),
			Labels: map[string]string{"mod.task": "88", stackLabel: "foo"}},
		// MOD-labelled running → never
		{Name: "running", State: "running", CreatedAt: old(100),
			Labels: map[string]string{"mod.task": "88"}},
		// MOD-labelled, active task → never
		{Name: "active", State: "exited", CreatedAt: old(100),
			Labels: map[string]string{"mod.task": "77"}},
		// MOD-labelled, inactive task, old, exited → eligible
		{Name: "orphan", State: "exited", CreatedAt: old(100),
			Labels: map[string]string{"mod.task": "88"}},
		// MOD-labelled, recent → never
		{Name: "recent", State: "exited", CreatedAt: old(2),
			Labels: map[string]string{"mod.task": "89"}},
	}
	got := PlanContainers(cfg, conts, active, now)
	if len(got) != 1 || got[0].Name != "orphan" {
		t.Fatalf("got %v, want only [orphan]", namesOf(got))
	}
}

func namesOf(c []Container) []string {
	out := make([]string, 0, len(c))
	for _, x := range c {
		out = append(out, x.Name)
	}
	return out
}

func TestPlanVolumesKeepAndCheckpoint(t *testing.T) {
	cfg := Config{Grace: 24 * time.Hour}
	vols := []Volume{
		// unlabelled → never
		{Name: "v-user", CreatedAt: old(100), Labels: map[string]string{}},
		// MOD task volume, keep-labelled → never
		{Name: "v-keep", CreatedAt: old(100), Labels: map[string]string{taskVolLabelKey(): "1", keepLabel: "1"}},
		// checkpoint volume → never
		{Name: "v-ckpt", CreatedAt: old(100), Labels: map[string]string{taskVolLabelKey(): "1", checkpointLabel: "1"}},
		// MOD task volume, dangling, old → eligible
		{Name: "v-orphan", CreatedAt: old(100), Labels: map[string]string{taskVolLabelKey(): "1"}},
		// MOD task volume, dangling, recent → never
		{Name: "v-new", CreatedAt: old(2), Labels: map[string]string{taskVolLabelKey(): "1"}},
	}
	got := PlanVolumes(cfg, vols, now)
	if len(got) != 1 || got[0].Name != "v-orphan" {
		t.Fatalf("got %v, want only [v-orphan]", namesOfVolumes(got))
	}
}

// taskVolLabelKey returns the MOD task-volume label key from ModLabelKeys.
func taskVolLabelKey() string { return "com.example.mod-core.task-volume" }

func namesOfVolumes(v []Volume) []string {
	out := make([]string, 0, len(v))
	for _, x := range v {
		out = append(out, x.Name)
	}
	return out
}

func TestPlanNetworksTaskScopedOnly(t *testing.T) {
	cfg := Config{Grace: 24 * time.Hour}
	active := map[string]bool{"77": true}
	nets := []Network{
		// unlabelled → never
		{Name: "n-user", CreatedAt: old(100), Labels: map[string]string{}},
		// MOD-purpose shared infra (no mod.task) → never
		{Name: "mod-user-egress", CreatedAt: old(500), Labels: map[string]string{"mod.purpose": "user-container-isolation-egress"}},
		// task network, active task → never
		{Name: "mod-egr-77", CreatedAt: old(100), Labels: map[string]string{"mod.task": "77", "mod.worker": "w"}},
		// task network, still has attached containers → never
		{Name: "mod-egr-busy", CreatedAt: old(100), Attached: 2, Labels: map[string]string{"mod.task": "88", "mod.worker": "w"}},
		// task network, old, detached, inactive → eligible
		{Name: "mod-egr-orphan", CreatedAt: old(100), Attached: 0, Labels: map[string]string{"mod.task": "88", "mod.worker": "w"}},
		// task network, recent → never
		{Name: "mod-egr-new", CreatedAt: old(2), Attached: 0, Labels: map[string]string{"mod.task": "89", "mod.worker": "w"}},
	}
	got := PlanNetworks(cfg, nets, active, now)
	if len(got) != 1 || got[0].Name != "mod-egr-orphan" {
		t.Fatalf("got %v, want only [mod-egr-orphan]", namesOfNetworks(got))
	}
}

func namesOfNetworks(n []Network) []string {
	out := make([]string, 0, len(n))
	for _, x := range n {
		out = append(out, x.Name)
	}
	return out
}

func TestPlanTaskDirsActiveAndGrace(t *testing.T) {
	cfg := Config{Grace: 24 * time.Hour}
	active := map[string]bool{"101": true}
	dirs := []TaskDir{
		{Path: "/root/101", TaskID: "101", ModifiedAt: old(100), Size: 1 << 20}, // active → never
		{Path: "/root/102", TaskID: "102", ModifiedAt: old(100), Size: 1 << 20}, // eligible
		{Path: "/root/103", TaskID: "103", ModifiedAt: old(2), Size: 1 << 20},  // recent → never
		{Path: "/root/104", TaskID: "104", ModifiedAt: old(100), Size: 0},     // zero size → never
	}
	got := PlanTaskDirs(cfg, dirs, active, now)
	if len(got) != 1 || got[0].TaskID != "102" {
		t.Fatalf("got %v, want only [102]", taskIDs(got))
	}
}

func taskIDs(d []TaskDir) []string {
	out := make([]string, 0, len(d))
	for _, x := range d {
		out = append(out, x.TaskID)
	}
	return out
}

func TestTaskActiveNilFailsSafe(t *testing.T) {
	// nil activeTaskIDs = "cannot say" → treated as active → never reclaimed.
	conts := []Container{
		{Name: "orphan", State: "exited", CreatedAt: old(100),
			Labels: map[string]string{"mod.task": "88"}},
	}
	cfg := Config{Grace: 24 * time.Hour}
	if got := PlanContainers(cfg, conts, nil, now); len(got) != 0 {
		t.Fatalf("nil active map must fail safe (no reclaim), got %v", namesOf(got))
	}
}

func TestReportReclaimableBytes(t *testing.T) {
	cfg := Config{HighWaterPct: 85, LowWaterPct: 75, Grace: 24 * time.Hour}
	img := ImagePlan{Reclaimable: 3 << 30, ReclaimableCount: 3}
	mod := ModelPlan{Reclaimable: 5 << 30, ReclaimableCount: 2}
	taskDirs := []TaskDir{{Path: "/r/1", TaskID: "1", Size: 1 << 20}}
	rep := NewReport(cfg, RootUsage{Total: 100 << 30, Used: 90 << 30}, img, mod,
		nil, nil, nil, taskDirs, now)
	if rep.ReclaimableBytes() != (3<<30)+(5<<30)+(1<<20) {
		t.Fatalf("ReclaimableBytes = %d", rep.ReclaimableBytes())
	}
	c := rep.Counts()
	if c["images"] != 3 || c["model_dirs"] != 2 || c["task_dirs"] != 1 {
		t.Fatalf("Counts = %v", c)
	}
}
