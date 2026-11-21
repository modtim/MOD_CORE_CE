package cleanup

// FIX-CE-UNLIMITED-WORKER-CLEANUP-MODEL-CACHE-MAX-GB: the CE default of
// worker.cleanup_model_cache_max_gb is the sentinel 0 ("no model-cache
// eviction"). executor/cleanup_bridge.go maps the delivered tunable to
// ModelCacheCapBytes = gb<<30, so a delivered 0 gives a cap of 0 bytes. The
// planner must read that as eviction OFF (PlanModelDirs returns early on
// cap <= 0), never as "cap 0 bytes = delete everything". These tests pin that
// reading and keep the positive-cap LRU path honest as the control.

import (
	"testing"
	"time"
)

func TestModelCacheMaxGbZeroNeverEvicts(t *testing.T) {
	// 900 GiB of cold, eligible weights: well above the paid 500 GiB arm.
	dirs := []ModelDir{
		{Path: "models--a", Size: 300 << 30, Atime: old(300)},
		{Path: "models--b", Size: 300 << 30, Atime: old(200)},
		{Path: "models--c", Size: 300 << 30, Atime: old(100)},
	}

	// What CE delivers: the cap is 0 bytes. normalise must keep it at 0 (it
	// must not fold the built-in 500 GiB back in).
	off := Config{ModelCacheCapBytes: 0, Grace: 24 * time.Hour}
	if n := off.normalise(); n.ModelCacheCapBytes != 0 {
		t.Fatalf("delivered 0 cap must stay 0 (eviction off), got %d bytes", n.ModelCacheCapBytes)
	}
	plan := PlanModelDirs(off, dirs, now)
	if plan.CapTriggered || len(plan.Victims) != 0 || plan.ReclaimableCount != 0 {
		t.Fatalf("cap 0 must evict nothing: triggered=%v victims=%d reclaimable=%d",
			plan.CapTriggered, len(plan.Victims), plan.ReclaimableCount)
	}
	if len(plan.ReclaimableModelDirs) != 0 {
		t.Fatalf("cap 0 must report no sample, got %d", len(plan.ReclaimableModelDirs))
	}
}

func TestModelCacheMaxGbPositiveCapStillEvictsOldestFirst(t *testing.T) {
	// Positive control on the SAME dirs: a 500 GiB cap over 900 GiB must
	// trigger, and the oldest cold dir goes first.
	dirs := []ModelDir{
		{Path: "models--a", Size: 300 << 30, Atime: old(300)},
		{Path: "models--b", Size: 300 << 30, Atime: old(200)},
		{Path: "models--c", Size: 300 << 30, Atime: old(100)},
	}
	on := Config{ModelCacheCapBytes: 500 << 30, Grace: 24 * time.Hour}
	plan := PlanModelDirs(on, dirs, now)
	if !plan.CapTriggered || len(plan.Victims) == 0 {
		t.Fatalf("900 GiB over a 500 GiB cap must evict: triggered=%v victims=%d",
			plan.CapTriggered, len(plan.Victims))
	}
	if plan.Victims[0].Path != "models--a" {
		t.Fatalf("first victim = %s, want models--a (oldest)", plan.Victims[0].Path)
	}
}
