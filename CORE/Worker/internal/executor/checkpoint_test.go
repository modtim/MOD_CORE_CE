package executor

import (
	"testing"
	"time"
)

// Gate 3. A checkpoint volume exists to OUTLIVE the attempt that made it. If the
// stale-volume prune could take one, a retry would find nothing and the node
// would restart from zero — the exact failure checkpointing prevents.
func TestACheckpointVolumeIsNeverPruned(t *testing.T) {
	labels := map[string]string{checkpointVolumeLabel: "1", keepVolumeLabel: "1"}
	// Old, dangling, owned by a dead instance: every condition that would
	// condemn an ordinary task volume.
	if shouldPruneVolume("modcore-ckpt-noderun-42", labels, 72*time.Hour, "w", "i") {
		t.Fatal("a checkpoint volume was judged prunable; a retry would start from zero")
	}
}

// The name alone must be enough, because a volume created before the label
// existed still has to survive.
func TestTheNameAloneKeepsACheckpointVolumeOutOfTheTaskSet(t *testing.T) {
	if isTaskVolume("modcore-ckpt-noderun-42") {
		t.Error("checkpoint volumes must not read as task volumes")
	}
	if !isTaskVolume(taskVolumePrefix + "abc") {
		t.Error("an ordinary task volume stopped matching")
	}
}

// And the ordinary volume must STILL be reapable — a fix that protects
// everything protects nothing, and 3,900 orphans were swept off one host.
func TestAnOrdinaryTaskVolumeIsStillPruned(t *testing.T) {
	if !shouldPruneVolume(taskVolumePrefix+"dead", map[string]string{}, 72*time.Hour, "w", "i") {
		t.Fatal("ordinary task volumes stopped being reclaimable")
	}
}

// The reaper must do NOTHING when it cannot ask. A worker that guesses would
// destroy a six-hour job's progress; refusing only costs disk.
func TestNoReaperWiredMeansNoReaping(t *testing.T) {
	e := &Executor{}
	if got := e.PruneCheckpointVolumes(nil); got != 0 {
		t.Fatalf("pruned %d with no manager query wired; must be 0", got)
	}
}
