package cleanup

// 25.5.s2 — model-cache size-cap invariants: the API-pushed GiB cap
// (worker.cleanup_model_cache_cap_gb) folds into the single planner cap as
// the TIGHTER of the two arms; eviction is LRU (oldest first); a dir in use
// by a live container is never a victim and never removed; anything touched
// inside the grace window is never a victim; cap 0 = off (nothing evicted,
// nothing reported); a dry-run pass reports the victims while deleting
// nothing; and the OS FS adapter refuses any path that resolves outside the
// model root (symlink escape).

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func TestModelCapGbFoldsIntoThePlannerCap(t *testing.T) {
	// Built-in 500 GiB cache arm vs an operator-set 10 GiB arm: the tighter
	// one gates eviction.
	c := DefaultConfig()
	c.ModelCacheCapGb = 10
	n := c.normalise()
	if n.ModelCacheCapBytes != 10<<30 {
		t.Fatalf("cap bytes = %d, want %d (tighter of 500GiB and 10GiB)", n.ModelCacheCapBytes, 10<<30)
	}
	// cap_gb 0 = off: it must never lower the built-in 500 GiB arm.
	off := DefaultConfig()
	if off.normalise().ModelCacheCapBytes != 500<<30 {
	 t.Fatalf("cap_gb 0 must leave the built-in cap, got %d", off.normalise().ModelCacheCapBytes)
	}
	// A negative GiB cap clamps to 0 (off), never to a negative cap.
	neg := DefaultConfig()
	neg.ModelCacheCapGb = -5
	if neg.normalise().ModelCacheCapGb != 0 || neg.normalise().ModelCacheCapBytes != 500<<30 {
	 t.Fatalf("negative cap_gb must clamp to off, got %d/%d", neg.normalise().ModelCacheCapGb, neg.normalise().ModelCacheCapBytes)
	}
	// The GiB arm alone (no built-in) still gates: a bare Config has no cap.
	bare := Config{ModelCacheCapGb: 4}
	if bare.normalise().ModelCacheCapBytes != 4<<30 {
	 t.Fatalf("bare cap_gb 4 must set the cap, got %d", bare.normalise().ModelCacheCapBytes)
	}
}

func TestModelCapEvictsOldestInUseAndGraceNever(t *testing.T) {
	// 16 GiB total, 10 GiB cap → need 6 GiB. m-inuse (in use by a live
	// container) and m-new (touched 2h ago, inside the 24h grace) are
	// excluded; the two eligible dirs are exactly enough, oldest first.
	cfg := Config{ModelCacheCapGb: 10, Grace: 24 * time.Hour}
	dirs := []ModelDir{
		{Path: "models--m-new", Size: 4 << 30, Atime: old(2)},
		{Path: "models--m-mid", Size: 4 << 30, Atime: old(48)},
		{Path: "models--m-inuse", Size: 4 << 30, Atime: old(60), InUse: true},
		{Path: "models--m-old", Size: 4 << 30, Atime: old(72)},
	}
	plan := PlanModelDirs(cfg, dirs, now)
	if !plan.CapTriggered {
	 t.Fatal("16GiB over a 10GiB cap must trigger the cap")
	}
	if len(plan.Victims) != 2 {
	 t.Fatalf("Victims = %d, want 2", len(plan.Victims))
	}
	if plan.Victims[0].Path != "models--m-old" || plan.Victims[1].Path != "models--m-mid" {
	 t.Fatalf("victim order = %s,%s want m-old,m-mid", plan.Victims[0].Path, plan.Victims[1].Path)
	}
	for _, v := range plan.Victims {
	 if v.Path == "models--m-inuse" || v.Path == "models--m-new" {
		 t.Fatalf("protected dir %s must never be a victim", v.Path)
	 }
	}
	// The dry-run sample is the eligible set in LRU order, oldest first.
	if plan.ReclaimableCount != 2 || len(plan.ReclaimableModelDirs) != 2 {
	 t.Fatalf("reclaimable = %d/%d, want 2/2", plan.ReclaimableCount, len(plan.ReclaimableModelDirs))
	}
	if plan.ReclaimableModelDirs[0].Path != "models--m-old" {
	 t.Fatalf("sample order = %s want m-old first", plan.ReclaimableModelDirs[0].Path)
	}
}

func TestModelCapZeroIsOff(t *testing.T) {
	// cap_gb 0 (the platform-setting default) must not evict, not trigger,
	// and not report a sample — the built-in 500 GiB arm still governs, and
	// 16 GiB is far below it.
	c := DefaultConfig()
	c.ModelCacheCapGb = 0
	dirs := []ModelDir{{Path: "models--m", Size: 16 << 30, Atime: old(100)}}
	plan := PlanModelDirs(c.normalise(), dirs, now)
	if len(plan.Victims) != 0 || plan.CapTriggered || len(plan.ReclaimableModelDirs) != 0 {
	 t.Fatalf("cap off must evict/report nothing: victims=%d triggered=%v sample=%d",
	 len(plan.Victims), plan.CapTriggered, len(plan.ReclaimableModelDirs))
	}
}

func TestModelCapSampleIsBounded(t *testing.T) {
	// 30 eligible dirs over the default sample bound (ReportImageListLimit =
	// 25): the heartbeat payload stays bounded while the count stays exact.
	cfg := Config{ModelCacheCapBytes: 1 << 30, Grace: 24 * time.Hour}
	dirs := make([]ModelDir, 0, 30)
 for i := 0; i < 30; i++ {
		dirs = append(dirs, ModelDir{Path: "models--m" + strconv.Itoa(i), Size: 1 << 28, Atime: old(100 + i)})
	}
	plan := PlanModelDirs(cfg, dirs, now)
	if plan.ReclaimableCount != 30 {
	 t.Fatalf("reclaimable count = %d, want 30", plan.ReclaimableCount)
	}
	if len(plan.ReclaimableModelDirs) != ReportImageListLimit {
	 t.Fatalf("sample size = %d, want %d", len(plan.ReclaimableModelDirs), ReportImageListLimit)
	}
	// ModelCacheReportLimit overrides the bound; <=0 falls back to it.
	tight := cfg
	tight.ModelCacheReportLimit = 3
	p2 := PlanModelDirs(tight, dirs, now)
	if len(p2.ReclaimableModelDirs) != 3 {
	 t.Fatalf("sample size = %d, want 3", len(p2.ReclaimableModelDirs))
	}
	neg := cfg
	neg.ModelCacheReportLimit = -1
	if n := len(PlanModelDirs(neg, dirs, now).ReclaimableModelDirs); n != ReportImageListLimit {
	 t.Fatalf("negative limit must fall back, got %d", n)
	}
}

// modelPass runs one pass against the fake FS so removals are recorded.
// `recent` dirs are stamped with the real clock because the executor plans
// against time.Now(), not the fixed planner-test clock.
func TestModelDryRunReportsVictimsButDeletesNothing(t *testing.T) {
	f := &fakeFS{
		// 16 GiB over a 10 GiB cap: m-old (oldest) then m-mid are the victims;
		// m-inuse is mounted by a live container, m-new was just touched.
		// m-outside is not models--prefixed so the fake lister still sees it
		// only through the executor, which never plans it.
	 models: []ModelDir{
			{Path: "models--m-old", Size: 4 << 30, Atime: old(72)},
			{Path: "models--m-mid", Size: 4 << 30, Atime: old(48)},
			{Path: "models--m-inuse", Size: 4 << 30, Atime: old(60)},
			{Path: "models--m-new", Size: 4 << 30, Atime: time.Now()},
	 },
	}
	 rt := &fakeRuntime{usage: RootUsage{Total: 100 << 30, Used: 10 << 30}, usageOK: true}
	x := New(rt, f, nil)
	 cfg := DefaultConfig()
	 cfg.ModelCacheCapGb = 10
	 cfg.Reclaim = false // DRY RUN
	 in := Input{ModelRoot: "hub", ModelInUse: map[string]bool{"models--m-inuse": true}}
	 rep := x.Run(context.Background(), cfg, in)
	 if !rep.ModelCapTriggered {
	 t.Fatal("16GiB over a 10GiB cap must report the cap as triggered")
	 }
	if len(rep.ModelVictims) != 2 {
		t.Fatalf("victims = %d, want 2", len(rep.ModelVictims))
	}
	if rep.ModelVictims[0].Path != "models--m-old" {
		t.Fatalf("first victim = %s want models--m-old", rep.ModelVictims[0].Path)
	}
	 for _, v := range rep.ModelVictims {
	 if v.Path == "models--m-inuse" || v.Path == "models--m-new" {
		 t.Fatalf("protected dir %s must never be a victim", v.Path)
	 }
	 }
	 if rep.Counts()["model_dirs"] != 2 || rep.ReclaimableModelCount != 2 {
	 t.Fatalf("reclaimable model count = %d/%d, want 2/2",
	 rep.Counts()["model_dirs"], rep.ReclaimableModelCount)
	 }
	 if len(f.removedModels) != 0 || rep.ReclaimedBytes != 0 {
	 t.Fatalf("dry run performed deletions: %v reclaimed=%d", f.removedModels, rep.ReclaimedBytes)
	 }
	if len(rep.ReclaimableModelDirs) != 2 {
		t.Fatalf("dry-run sample size = %d, want 2", len(rep.ReclaimableModelDirs))
	}
	if rep.ReclaimableModelDirs[0].Path != "models--m-old" {
		t.Fatalf("dry-run sample first = %s want models--m-old", rep.ReclaimableModelDirs[0].Path)
	}
}

func TestModelCapReclaimRemovesOnlyEligibleOldestFirst(t *testing.T) {
	// Same snapshot as the dry run, reclaim ON: exactly the two eligible dirs
	// go, oldest first, and the in-use dir is never handed to RemoveModelDir.
	f := &fakeFS{
	 models: []ModelDir{
			{Path: "models--m-old", Size: 4 << 30, Atime: old(72)},
			{Path: "models--m-mid", Size: 4 << 30, Atime: old(48)},
			{Path: "models--m-inuse", Size: 4 << 30, Atime: old(60)},
			{Path: "models--m-new", Size: 4 << 30, Atime: time.Now()},
	 },
	}
	 rt := &fakeRuntime{usage: RootUsage{Total: 100 << 30, Used: 10 << 30}, usageOK: true}
	x := New(rt, f, nil)
	 cfg := DefaultConfig()
	 cfg.ModelCacheCapGb = 10
	 cfg.Reclaim = true
	 in := Input{ModelRoot: "hub", ModelInUse: map[string]bool{"models--m-inuse": true}}
	 rep := x.Run(context.Background(), cfg, in)
	 if len(f.removedModels) != 2 || f.removedModels[0] != "models--m-old" || f.removedModels[1] != "models--m-mid" {
	 t.Fatalf("removed = %v, want [models--m-old models--m-mid]", f.removedModels)
	 }
	 for _, p := range f.removedModels {
	 if p == "models--m-inuse" || p == "models--m-new" {
		 t.Fatalf("protected dir %s must never be removed", p)
	 }
	 }
	 if rep.ReclaimedCounts["model_dirs"] != 2 || rep.ReclaimedBytes != 8<<30 {
	 t.Fatalf("reclaimed = %d/%d bytes, want 2/%d",
	 rep.ReclaimedCounts["model_dirs"], rep.ReclaimedBytes, 8<<30)
	 }
	 if rep.Counts()["model_dirs"] != 2 {
	 t.Fatalf("reclaimable model count = %d, want 2", rep.Counts()["model_dirs"])
	 }
}

func TestModelRootSymlinkEscapeIsRefused(t *testing.T) {
	// A real temp root with a real sibling outside it, plus a models-- dir
	// that is a symlink escaping the root: the OS FS adapter must refuse to
	// remove it (safeWithinRoot evaluates symlinks) and the outside dir must
	// still exist.
	 base, err := mkTempDir()
	 if err != nil {
	 t.Fatalf("temp root: %v", err)
	 }
	 root := filepath.Join(base, "hub")
	 outside := filepath.Join(base, "outside-models")
	 if mkDir(root) || mkDir(outside) {
	 t.Fatal("temp scaffolding failed")
	 }
	 if mkFile(filepath.Join(outside, "weight")) {
	 t.Fatal("outside marker write failed")
	 }
	 // The escape: a models-- entry inside root pointing at the outside dir.
	 if err := os.Symlink(outside, filepath.Join(root, "models--evil")); err != nil {
	 t.Fatalf("symlink plant: %v", err)
	 }
	 fs := NewOSFS("", nil)
	 err = fs.RemoveModelDir(context.Background(), root, filepath.Join(root, "models--evil"))
	 if err != os.ErrPermission {
	 t.Fatalf("escape refused with %v, want permission denied", err)
	 }
	 if _, err := os.Stat(filepath.Join(outside, "weight")); err != nil {
	 t.Fatalf("outside dir was touched: %v", err)
	 }
	 // Listing never reports the escape as an eviction unit either.
	 dirs, err := fs.ListModelDirs(context.Background(), root)
	 if err != nil {
	 t.Fatalf("list: %v", err)
	 }
	 for _, d := range dirs {
	 if d.Path == filepath.Join(root, "models--evil") {
		 t.Fatal("escape listed as an eviction unit")
	 }
	 }
		if err := fs.RemoveModelDir(context.Background(), "", "anything"); err != os.ErrPermission {
	 t.Fatalf("empty root must refuse, got %v", err)
	 }
}

func mkTempDir() (string, error) {
	 d, err := os.MkdirTemp("", "mod-model-root")
	 if err != nil {
	 return "", err
	 }
	 return d, nil
}

func mkDir(p string) bool {
		err := os.Mkdir(p, 0o755)
	 return err != nil
}

func mkFile(p string) bool {
	return os.WriteFile(p, []byte("x"), 0o644) != nil
}
