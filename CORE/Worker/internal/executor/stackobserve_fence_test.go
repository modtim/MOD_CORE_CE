package executor

import "testing"

// Fencing exists to prevent two live copies of a stack. For a class the
// manager already re-places onto another host without asking, a second copy is
// by definition acceptable — so fencing it buys nothing and costs availability.
//
// The concrete cost, 2026-08-13: a model server was stopped three times in one
// session because the API takes longer to restart than the 120s fence lease.
// Nothing was protected; the weights are a cache and the stack holds no state.
func TestFenceableFollowsTheReplaceableClasses(t *testing.T) {
	cases := []struct {
		class     string
		fenceable bool
		why       string
	}{
		{"stateless", false, "nothing to lose; the manager re-places it freely"},
		{"cache_backed", false, "weights are a cache — a cold start costs time, not correctness"},
		{"snapshot_restore", true, "holds data with an RPO; two writers lose it"},
		{"external", true, "MOD's only job is not running two primaries"},
		{"", true, "unknown might be a database — fence the unknown case"},
		{"  cache_backed  ", false, "whitespace in a label must not change the verdict"},
		{"nonsense", true, "an unrecognised class is not a licence to keep serving"},
	}
	for _, c := range cases {
		got := StackObservation{Project: "p", Running: 1, HAClass: c.class}.Fenceable()
		if got != c.fenceable {
			t.Errorf("HAClass=%q Fenceable()=%v, want %v — %s",
				c.class, got, c.fenceable, c.why)
		}
	}
}

// The two lists must not drift: this mirrors REPLACEABLE_HA_CLASSES in
// services/stack_reconciler.py, and a class the manager re-places while the
// worker fences it is an outage on every partition.
func TestReplaceableSetMatchesTheManagers(t *testing.T) {
	want := map[string]bool{"stateless": true, "cache_backed": true}
	if len(replaceableHAClasses) != len(want) {
		t.Fatalf("replaceableHAClasses = %v, want %v", replaceableHAClasses, want)
	}
	for k := range want {
		if !replaceableHAClasses[k] {
			t.Errorf("%q missing from replaceableHAClasses", k)
		}
	}
}

// A stack that is not serving is skipped before the class is ever consulted.
func TestAStoppedStackIsNotFencedRegardlessOfClass(t *testing.T) {
	if (StackObservation{Project: "p", Running: 0, HAClass: "external"}).Healthy() {
		t.Error("a project with zero running containers must not read as hosted")
	}
}
