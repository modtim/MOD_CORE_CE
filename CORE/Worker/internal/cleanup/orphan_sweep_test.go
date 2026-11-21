package cleanup

// 25.5.s3: orphan mod.*-labelled task dirs / containers / volumes / networks.
// The sweep policy predates this part (3a9c20433); 25.5.s3 adds the s1/s2-style
// dry-run surface - the orphan sets listed per item (oldest first, capped at
// ReportImageListLimit) so a report-only pass shows EXACTLY what a reclaim pass
// would remove - and re-asserts the safety invariants through the executor fakes:
// unlabelled never touched, live task/stack never touched, grace respected,
// attached network/volume never touched, dry-run removes nothing.

import (
	"context"
	"strconv"
	"testing"
	"time"
)

func itoa(i int) string { return strconv.Itoa(i) }

func sweepCfg() Config {
	c := DefaultConfig()
	c.Grace = 24 * time.Hour
	return c
}

// agoH is relative to the wall clock, because Executor.Run snapshots time.Now()
// (the planner_test `now` constant only pins the pure-planner tests).
func agoH(h int) time.Time { return time.Now().Add(-time.Duration(h) * time.Hour) }

func TestOrphanSampleAgeOrderAndCap(t *testing.T) {
	const n = ReportImageListLimit + 5
	containers := make([]Container, 0, n)
	vols := make([]Volume, 0, n)
	nets := make([]Network, 0, n)
	dirs := make([]TaskDir, 0, n)
	for i := 0; i < n; i++ {
		age := 1000 - i
		containers = append(containers,
			Container{ID: "c" + itoa(i), Name: "mod-egr-" + itoa(i), State: "exited",
				CreatedAt: old(age), Labels: map[string]string{"mod.task": itoa(i)}})
		vols = append(vols,
			Volume{Name: "modcore-task-" + itoa(i), CreatedAt: old(age),
				Labels: map[string]string{"com.example.mod-core.task-volume": "1"}})
		nets = append(nets,
			Network{ID: "n" + itoa(i), Name: "mod-egr-" + itoa(i), CreatedAt: old(age),
				Attached: 0, Labels: map[string]string{"mod.task": itoa(i)}})
		dirs = append(dirs,
			TaskDir{Path: "/root/" + itoa(i), TaskID: itoa(i), ModifiedAt: old(age),
				Size: 1 << 10})
	}
	active := map[string]bool{} // nothing is live this pass
	ctOrphans := PlanContainers(sweepCfg(), containers, active, now)
	volOrphans := PlanVolumes(sweepCfg(), vols, now)
	netOrphans := PlanNetworks(sweepCfg(), nets, active, now)
	dirOrphans := PlanTaskDirs(sweepCfg(), dirs, active, now)
	if len(ctOrphans) != n || len(volOrphans) != n || len(netOrphans) != n || len(dirOrphans) != n {
		t.Fatalf("all %d of each kind must be reclaimable, got %d/%d/%d/%d",
			n, len(ctOrphans), len(volOrphans), len(netOrphans), len(dirOrphans))
	}
	rep := NewReport(sweepCfg(), RootUsage{}, ImagePlan{}, ModelPlan{},
		ctOrphans, volOrphans, netOrphans, dirOrphans, now)
	if len(rep.ReclaimableContainers) != ReportImageListLimit ||
		len(rep.ReclaimableVolumes) != ReportImageListLimit ||
		len(rep.ReclaimableNetworks) != ReportImageListLimit ||
		len(rep.ReclaimableTaskDirs) != ReportImageListLimit {
		t.Fatalf("samples must cap at %d: %d/%d/%d/%d", ReportImageListLimit,
			len(rep.ReclaimableContainers), len(rep.ReclaimableVolumes),
			len(rep.ReclaimableNetworks), len(rep.ReclaimableTaskDirs))
	}
	for i := 1; i < len(rep.ReclaimableContainers); i++ {
		if rep.ReclaimableContainers[i].CreatedAt.Before(
			rep.ReclaimableContainers[i-1].CreatedAt) {
			t.Fatalf("container sample not oldest-first")
		}
	}
	if rep.ReclaimableContainers[0].CreatedAt != old(1000) {
		t.Fatalf("oldest container must lead the sample")
	}
	if rep.Counts()["containers"] != n {
		t.Fatalf("full set must stay counted: %d", rep.Counts()["containers"])
	}
}

// sameStrings asserts an exact removal list (order included).
func sameStrings(got []string, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

// The dry-run preview must name the SAME objects a reclaim pass removes,
// and a report-only pass must still remove nothing.
func TestOrphanDryRunMatchesReclaimAndRemovesNothing(t *testing.T) {
	rt := &fakeRuntime{
		containers: []Container{
			// unlabelled -> never
			{ID: "u1", Name: "user-app", State: "exited", CreatedAt: agoH(100),
				Labels: map[string]string{"other": "x"}},
			// stack member (compose-managed) -> never
			{ID: "s1", Name: "stack-mem", State: "exited", CreatedAt: agoH(100),
				Labels: map[string]string{"mod.task": "9", composeProjectLabel: "modstack1g1"}},
			// live task -> never
			{ID: "a1", Name: "live-task", State: "exited", CreatedAt: agoH(100),
				Labels: map[string]string{"mod.task": "77"}},
			// within grace -> never
			{ID: "g1", Name: "recent-task", State: "exited", CreatedAt: agoH(2),
				Labels: map[string]string{"mod.task": "89"}},
			// the one orphan
			{ID: "o1", Name: "orphan-task", State: "exited", CreatedAt: agoH(100),
				Labels: map[string]string{"mod.task": "88"}},
		},
		volumes: []Volume{
			{Name: "v-attached", CreatedAt: agoH(100), UsedBy: []string{"some-ct"},
				Labels: map[string]string{"com.example.mod-core.task-volume": "1"}},
			{Name: "v-orphan", CreatedAt: agoH(100),
				Labels: map[string]string{"com.example.mod-core.task-volume": "1"}},
		},
		networks: []Network{
			{ID: "n-attached", Name: "mod-egr-88a", CreatedAt: agoH(100), Attached: 1,
				Labels: map[string]string{"mod.task": "88"}},
			{ID: "n-orphan", Name: "mod-egr-88b", CreatedAt: agoH(100), Attached: 0,
				Labels: map[string]string{"mod.task": "88"}},
			{ID: "n-shared", Name: "mod-user-egress", CreatedAt: agoH(500), Attached: 0,
				Labels: map[string]string{"mod.purpose": "user-container-isolation-egress"}},
		},
		usage: RootUsage{}, usageOK: false,
	}
	fs := &fakeFS{
		taskDirs: []TaskDir{
			{Path: "/root/77", TaskID: "77", ModifiedAt: agoH(100), Size: 1 << 20},
			{Path: "/root/88", TaskID: "88", ModifiedAt: agoH(100), Size: 1 << 20},
			{Path: "/root/89", TaskID: "89", ModifiedAt: agoH(2), Size: 1 << 20},
		},
	}
	x := New(rt, fs, nil)
	active := map[string]bool{"77": true}

	dry := sweepCfg()
	rep := x.Run(context.Background(), dry, Input{ActiveTaskIDs: active})
	if len(rt.removedContainers) != 0 || len(rt.removedVolumes) != 0 ||
		len(rt.removedNetworks) != 0 || len(fs.removedTaskDirs) != 0 {
		t.Fatalf("dry run deleted something: rt=%v fs=%v", rt, fs)
	}
	if len(rep.ReclaimableContainers) != 1 || rep.ReclaimableContainers[0].ID != "o1" {
		t.Fatalf("dry-run container sample must be exactly [o1]")
	}
	if len(rep.ReclaimableVolumes) != 1 || rep.ReclaimableVolumes[0].Name != "v-orphan" {
		t.Fatalf("dry-run volume sample must be exactly [v-orphan]")
	}
	if len(rep.ReclaimableNetworks) != 1 || rep.ReclaimableNetworks[0].ID != "n-orphan" {
		t.Fatalf("dry-run network sample must be exactly [n-orphan]")
	}
	if len(rep.ReclaimableTaskDirs) != 1 || rep.ReclaimableTaskDirs[0].TaskID != "88" {
		t.Fatalf("dry-run task-dir sample must be exactly [88]")
	}

	live := sweepCfg()
	live.Reclaim = true
	rt2 := &fakeRuntime{
		containers: rt.containers, volumes: rt.volumes, networks: rt.networks,
		usage: RootUsage{}, usageOK: false,
	}
	fs2 := &fakeFS{taskDirs: fs.taskDirs}
	x2 := New(rt2, fs2, nil)
	rep2 := x2.Run(context.Background(), live, Input{ActiveTaskIDs: active})
	if !sameStrings(rt2.removedContainers, []string{"o1"}) ||
		!sameStrings(rt2.removedVolumes, []string{"v-orphan"}) ||
		!sameStrings(rt2.removedNetworks, []string{"n-orphan"}) ||
		!sameStrings(fs2.removedTaskDirs, []string{"/root/88"}) {
		t.Fatalf("reclaim must remove exactly the dry-run preview: rt=%v fs=%v", rt2, fs2)
	}
	if rep2.ReclaimedCounts["containers"] != 1 || rep2.ReclaimedCounts["volumes"] != 1 ||
		rep2.ReclaimedCounts["networks"] != 1 || rep2.ReclaimedCounts["task_dirs"] != 1 {
		t.Fatalf("reclaimed counts wrong: %v", rep2.ReclaimedCounts)
	}
}
