// Package cleanup implements the worker's self-cleanup planner: a pure,
// unit-testable policy over snapshots of docker objects and directories,
// deciding what is reclaimable (and, when reclaim is explicitly enabled,
// what to actually delete).
//
// Safety model — the plan is a whitelist, never a blacklist:
//
//   - Containers / volumes / networks are candidates only if they carry one
//     of the label keys the worker itself sets (ModLabelKeys). Anything
//     unlabelled is never touched.
//   - Compose-managed objects (a stack, or the worker's own container in the
//     startup compose) are never touched, at any state or age.
//   - Running / restarting / paused containers are never touched.
//   - Objects carrying a task ID that is currently active on this worker are
//     never touched.
//   - Nothing created (or, for images/models, last used) within the grace
//     period is touched.
//   - Images referenced by ANY container (running or stopped), matching a
//     pinned pattern, on the protected list, or inside the grace window are
//     never victims.
//
// The planner returns both RECLAIMABLE (everything the policy would allow)
// and VICTIMS (what the policy would actually delete this pass — images
// only while the docker root is over the high-water mark, model dirs only
// while the cache is over its cap). Deletion itself lives in executor.go,
// behind an interface, and is a no-op entirely unless Config.Reclaim is
// set.
package cleanup

import (
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// ModLabelKeys is the set of label keys the MOD worker sets on the
// containers, volumes and networks it creates (see internal/executor:
// sidecarLabel, taskContainerLabel, taskVolumeLabel, sidecarWorkerLabel,
// the "mod.task" / "mod.job" / "mod.purpose" / "mod.worker" stamps on task
// containers, egress proxies and task networks, and the stack label). An
// object is a candidate for any reclamation only if it carries at least one
// key from this set — and the protection rules below (compose-managed,
// running, active task, grace) still apply on top.
var ModLabelKeys = []string{
	"com.example.mod-core.sidecar",
	"com.example.mod-core.task",
	"com.example.mod-core.task-volume",
	"com.example.mod-core.worker",
	"mod.task",
	"mod.job",
	"mod.purpose",
	"mod.worker",
}

// Protection label keys: presence of any of these disqualifies the object
// regardless of ownership. composeProjectLabel marks every member of a
// `docker compose` project — managed stacks AND the worker's own container
// in the startup compose — which must never be reaped out from under
// compose. keepLabel is the volume opt-out (a stopped stack's model-weight
// volume is dangling BY DESIGN). checkpointLabel marks node-run checkpoint
// volumes whose reaping the manager arbitrates, not the worker.
const (
	composeProjectLabel = "com.docker.compose.project"
	stackLabel          = "mod.techlabs/stack"
	keepLabel           = "com.example.mod-core.keep"
	checkpointLabel     = "com.example.checkpoint"
)

// ReportImageListLimit caps the "what could go" sample the report carries so
// a fleet heartbeat with thousands of stale images stays bounded; the
// reclaimable TOTALS and COUNTS stay exact regardless.
const ReportImageListLimit = 25

// Config is the policy for one pass. All thresholds are safe to override;
// Reclaim is the only behaviour switch and defaults to false (report only).
type Config struct {
	// Reclaim enables actual deletion. false = compute the report and
	// delete nothing, no matter how full the disk is.
	Reclaim bool
	// Image GC triggers only when the docker data-root partition is at or
	// above HighWaterPct used, and reclaims down to LowWaterPct.
	HighWaterPct int
	LowWaterPct  int
	// ModelCacheCapBytes caps the model cache; eviction (LRU by atime)
	// runs only while the cache total is above the cap. <=0 disables.
	// 25.5.s2 adds the second, API-side cap (worker.cleanup_model_cache_cap_gb,
	// GiB, 0 = off): when both caps are on the TIGHTER one gates eviction, so
	// the disk-pressure arm (a small operator-set cap) and the built-in cache
	// size arm can fire independently. In-use dirs are never victims under
	// either cap — the bridge proves "mounted by a live container" via docker
	// mounts (ModelCacheInUse), and anything it cannot prove is additionally
	// protected by the grace window below.
	// normalise clamps a negative cap to 0 (off).
	ModelCacheCapBytes int64
	// ModelCacheCapGb is the GiB form of the API-pushed cap
	// (worker.cleanup_model_cache_cap_gb, 0 = off); normalise folds it into
	// ModelCacheCapBytes as the tighter of the two caps, so the planner
	// proper keeps exactly one cap to compare against.
	// normalise clamps a negative GiB cap to 0 (off).
	ModelCacheCapGb int
	// ModelCacheReportLimit bounds the dry-run "what could go" model sample
	// (same bounded-report discipline as images); <=0 = ReportImageListLimit.
	ModelCacheReportLimit int
	//
	// Grace: nothing created (containers/volumes/networks/task dirs) or
	// last used (images/models) within this window is ever touched.
	Grace time.Duration
	// ImageCountCap: when the number of ELIGIBLE (unprotected) local images
	// exceeds this, the oldest beyond the cap become victims even with no
	// disk pressure — the "too many images" half of the GC trigger. <=0
	// disables it (disk pressure alone gates GC).
	ImageCountCap int
	// PinnedImagePatterns: glob or prefix patterns of image tags that are
	// never reclaimable (the existing MOD_WORKER_PINNED_IMAGES semantics).
	PinnedImagePatterns []string
	// ProtectedImageRefs: exact image tags never reclaimable — the
	// worker's own image, base images in use, etc.
	ProtectedImageRefs []string
}

// defaultPinnedImagePatterns is the always-on protection for MOD Core's own
// infrastructure images. The worker, base, stack and sidecar images live
// under ghcr.io/modtim/ and their lifecycle belongs to the
// manager (pull-on-demand, deep clean) — the worker must never reclaim them
// under disk pressure, no matter how full the root is. The env-var list
// (MOD_WORKER_PINNED_IMAGES, wired additively by the executor bridge)
// extends this set, never replaces it.
var defaultPinnedImagePatterns = []string{
	"ghcr.io/modtim/*",
}

// DefaultConfig is the built-in policy. The reclaim gate is OFF — the worker
// reports reclaimable space every pass but removes nothing until the API
// pushes the worker.cleanup_reclaim tunable on.
func DefaultConfig() Config {
	return Config{
		Reclaim:            false,
		HighWaterPct:       85,
		LowWaterPct:        75,
		ModelCacheCapBytes: 500 << 30, // 500 GiB
		Grace:              24 * time.Hour,
		PinnedImagePatterns: defaultPinnedImagePatterns,
		// Count trigger OFF by default: the API pushes
		// worker.cleanup_image_count_cap when the operator wants a
		// "too many images" trigger alongside disk pressure.
		ImageCountCap: 0,
	}
}

// normalise guards against misconfiguration: an inverted or zeroed watermark
// pair would make image GC fire (or never fire) on garbage input, and a
// non-positive grace would remove the "nothing recent" floor.
func (c Config) normalise() Config {
	if c.Grace <= 0 {
		c.Grace = DefaultConfig().Grace
	}
	if c.HighWaterPct < 0 {
		c.HighWaterPct = 0
	}
	if c.HighWaterPct > 100 {
		c.HighWaterPct = 100
	}
	if c.LowWaterPct < 0 {
		c.LowWaterPct = 0
	}
	if c.LowWaterPct > 100 {
		c.LowWaterPct = 100
	}
	if c.LowWaterPct > c.HighWaterPct {
		c.LowWaterPct = c.HighWaterPct
	}
	if c.ImageCountCap < 0 {
		c.ImageCountCap = 0
	}
	if c.ModelCacheCapGb < 0 {
		c.ModelCacheCapGb = 0
	}
	if c.ModelCacheCapGb > 0 {
		gb := int64(c.ModelCacheCapGb) << 30
		if c.ModelCacheCapBytes <= 0 || gb < c.ModelCacheCapBytes {
			c.ModelCacheCapBytes = gb
		}
	}
	if c.ModelCacheReportLimit < 0 {
		c.ModelCacheReportLimit = 0
	}
	return c
}

// RootUsage is a snapshot of the partition backing the docker data root.
type RootUsage struct {
	Total int64
	Used  int64
}

// Valid reports whether the snapshot is usable (a non-statted platform or a
// failed statfs returns a zero value).
func (r RootUsage) Valid() bool { return r.Total > 0 && r.Used >= 0 }

// UsedPct is used percentage, clamped to [0,100].
func (r RootUsage) UsedPct() int {
	if !r.Valid() {
		return 0
	}
	pct := int(r.Used * 100 / r.Total)
	if pct < 0 {
		pct = 0
	}
	if pct > 100 {
		pct = 100
	}
	return pct
}

// Image is one docker image as the planner sees it. LastUsed is the LRU
// touch time when known; zero means "never touched" and the planner falls
// back to CreatedAt (an image never used is as old as it looks).
type Image struct {
	ID        string
	RepoTags  []string
	Size      int64
	CreatedAt time.Time
	LastUsed  time.Time
}

// lastUsed resolves the LRU timestamp, falling back to creation time.
func (i Image) lastUsed() time.Time {
	if !i.LastUsed.IsZero() {
		return i.LastUsed
	}
	return i.CreatedAt
}

// ImagePlan is the image-GC decision for one pass.
//
//	Reclaimable — total bytes the policy would allow removing (everything
//	               eligible), reported even below the watermark.
//	Victims     — what this pass would actually delete: only non-empty when
//	               Triggered (root usage at/above the high-water mark), in
//	               LRU order, accumulated until the root is back at the
//	               low-water mark.
type ImagePlan struct {
	Triggered   bool
	UsedPct     int
	NeedBytes   int64
	Reclaimable int64
	// ReclaimableCount is the number of eligible images, reported even when
	// the watermark is not triggered.
	ReclaimableCount int
	// ReclaimableImages is the eligible set in LRU order (oldest first) — the
	// dry-run answer to "what could go, and in what order". Capped at
	// ReportImageListLimit entries so a huge fleet's heartbeat payload stays
	// bounded; ReclaimableCount stays exact.
	ReclaimableImages []Image
	// CountTriggered is the second GC trigger: more eligible images than
	// ImageCountCap. Victims then include the oldest images beyond the cap
	// even when the disk is nowhere near full.
	CountTriggered    bool
	Victims           []Image
}

// hasModLabel reports whether labels carry any MOD ownership key.
func hasModLabel(labels map[string]string) bool {
	for _, k := range ModLabelKeys {
		if _, ok := labels[k]; ok {
			return true
		}
	}
	return false
}

// composeManaged reports whether the object belongs to a docker compose
// project — a managed stack, or the worker's own container in the startup
// compose. Such objects are never reclaimed: their lifecycle is owned by
// `docker compose` and by the stack ops, and force-removing a member (even
// a stopped one) leaves the project in a state its own commands cannot
// reconcile.
func composeManaged(labels map[string]string) bool {
	return strings.TrimSpace(labels[composeProjectLabel]) != ""
}

// stackLabeled reports whether the object is part of a persistent stack by
// the stack's own label, independently of compose membership.
func stackLabeled(labels map[string]string) bool {
	return strings.TrimSpace(labels[stackLabel]) != ""
}

// modOwned is the gate every candidate must pass: MOD-labelled, not
// compose-managed, not a stack object.
func modOwned(labels map[string]string) bool {
	if !hasModLabel(labels) {
		return false
	}
	if composeManaged(labels) || stackLabeled(labels) {
		return false
	}
	return true
}

// pinnedImage reports whether any of the image's tags matches a pinned
// pattern. Same semantics as the pre-existing image GC (imagegc.go): a glob
// (filepath.Match) or a plain prefix.
func pinnedImage(tags []string, patterns []string) bool {
	for _, tag := range tags {
		for _, pat := range patterns {
			if pat == "" {
				continue
			}
			if match, err := filepath.Match(pat, tag); err == nil && match {
				return true
			}
			if strings.HasPrefix(tag, pat) {
				return true
			}
		}
	}
	return false
}

// protectedImage reports whether any of the image's tags is on the
// protected list (exact tag match — e.g. the worker's own image ref, or
// base images in use).
func protectedImage(tags []string, protected []string) bool {
	if len(protected) == 0 {
		return false
	}
	set := make(map[string]bool, len(protected))
	for _, p := range protected {
		set[p] = true
	}
	for _, tag := range tags {
		if set[tag] {
			return true
		}
	}
	return false
}

// PlanImages is the pure image-GC policy. inUse maps the image references
// (full shas) of EVERY container — running or stopped — built by the
// caller from the container list; an image is in use when its ID equals or
// is a prefix of any inUse entry (the image-list API returns short IDs, the
// container API returns full shas).
//
// Reclaimable counts every eligible image; Victims respects the watermarks
// (empty unless usage is at/above the high-water mark) and stops once the
// root is back at the low-water mark.
func PlanImages(cfg Config, usage RootUsage, images []Image, inUse map[string]bool, now time.Time) ImagePlan {
	c := cfg.normalise()
	plan := ImagePlan{UsedPct: usage.UsedPct()}
	if usage.Valid() {
		plan.Triggered = usage.UsedPct() >= c.HighWaterPct
		if plan.Triggered {
			target := usage.Total * int64(c.LowWaterPct) / 100
			if plan.NeedBytes = usage.Used - target; plan.NeedBytes < 0 {
				plan.NeedBytes = 0
			}
		}
	}

	eligible := make([]Image, 0, len(images))
	for _, img := range images {
		if img.ID == "" || img.Size <= 0 {
			continue
		}
		if imageInUse(img.ID, inUse) {
			continue
		}
		if pinnedImage(img.RepoTags, c.PinnedImagePatterns) {
			continue
		}
		if protectedImage(img.RepoTags, c.ProtectedImageRefs) {
			continue
		}
		if now.Sub(img.lastUsed()) < c.Grace {
			continue
		}
		eligible = append(eligible, img)
		plan.Reclaimable += img.Size
		plan.ReclaimableCount++
	}

	// Sort oldest-first BEFORE accumulating: list order from the daemon is
	// unspecified, and accumulating unsorted then sorting the chosen subset
	// could stop after taking a newer image ahead of an older one.
	sort.SliceStable(eligible, func(i, j int) bool {
		return eligible[i].lastUsed().Before(eligible[j].lastUsed())
	})

	// The dry-run list: what could go, in the order it would go.
	if n := len(eligible); n > ReportImageListLimit {
		plan.ReclaimableImages = eligible[:ReportImageListLimit]
	} else {
		plan.ReclaimableImages = eligible
	}

	// Two triggers, unioned: disk pressure (reclaim down to the low-water
	// mark) and image-count pressure (keep the cap newest, free the rest
	// oldest-first).
	plan.CountTriggered = c.ImageCountCap > 0 && plan.ReclaimableCount > c.ImageCountCap

	acc := int64(0)
	if plan.Triggered && plan.NeedBytes > 0 {
		for _, img := range eligible {
			plan.Victims = append(plan.Victims, img)
			acc += img.Size
			if acc >= plan.NeedBytes {
				break
			}
		}
	}
	if plan.CountTriggered {
		// Both victim sets are prefixes of the same LRU-sorted list, so the
		// union is the longer prefix.
		keepFrom := plan.ReclaimableCount - c.ImageCountCap
		for len(plan.Victims) < keepFrom {
			plan.Victims = append(plan.Victims, eligible[len(plan.Victims)])
		}
	}
	return plan
}

// imageInUse matches the image's (short) ID against full-sha container
// image references.
func imageInUse(id string, inUse map[string]bool) bool {
	if len(inUse) == 0 {
		return false
	}
	if inUse[id] {
		return true
	}
	for full := range inUse {
		if strings.HasPrefix(full, id) {
			return true
		}
	}
	return false
}

// ModelDir is one model-cache eviction unit (a "models--org--name" dir).
// Atime is the last-used marker when present, else the dir's mtime — the
// same resolution the on-box model-cache GC uses.
type ModelDir struct {
	Path  string
	Size  int64
	Atime time.Time
	InUse bool
}

// ModelPlan is the model-cache eviction decision for one pass.
type ModelPlan struct {
	TotalBytes         int64
	Reclaimable        int64
	ReclaimableCount   int
	Victims            []ModelDir
	ReclaimableModelDirs []ModelDir
	CapTriggered       bool
}

// PlanModelDirs is the pure model-cache policy: LRU by atime, evicting only
// while the cache total is above the cap and only down to it. In-use dirs
// and dirs touched within the grace window are never victims; Reclaimable
// counts every eligible dir regardless of the cap (the report is
// informational even when the cap is not yet exceeded).
func PlanModelDirs(cfg Config, dirs []ModelDir, now time.Time) ModelPlan {
	c := cfg.normalise()
	plan := ModelPlan{TotalBytes: sumModelSizes(dirs)}
	if c.ModelCacheCapBytes <= 0 || plan.TotalBytes <= c.ModelCacheCapBytes {
		return plan
	}

	eligible := make([]ModelDir, 0, len(dirs))
	for _, d := range dirs {
		if d.Size <= 0 || d.InUse {
			continue
		}
		if now.Sub(d.Atime) < c.Grace {
			continue
		}
		eligible = append(eligible, d)
		plan.Reclaimable += d.Size
		plan.ReclaimableCount++
	}

	sort.SliceStable(eligible, func(i, j int) bool {
		return eligible[i].Atime.Before(eligible[j].Atime)
	})
	plan.CapTriggered = true
	// 25.5.s2: the bounded dry-run sample of "what could go" (same discipline
	// as images) so the heartbeat can show model victims even when Reclaim is
	// off.
	limit := c.ModelCacheReportLimit
	if limit <= 0 {
		limit = ReportImageListLimit
	}
	if n := len(eligible); n > limit {
		plan.ReclaimableModelDirs = eligible[:limit]
	} else {
		plan.ReclaimableModelDirs = eligible
	}
	need := plan.TotalBytes - c.ModelCacheCapBytes
	for _, d := range eligible {
		plan.Victims = append(plan.Victims, d)
		need -= d.Size
		if need <= 0 {
			break
		}
	}
	return plan
}

func sumModelSizes(dirs []ModelDir) int64 {
	var total int64
	for _, d := range dirs {
		total += d.Size
	}
	return total
}

// Container is one docker container as the planner sees it.
type Container struct {
	ID        string
	Name      string
	State     string // running / exited / restarting / paused / ...
	// ImageID is the container's image reference (full sha from the list
	// API) — every container, running or stopped, pins its image.
	ImageID   string
	CreatedAt time.Time
	Labels    map[string]string
}

// taskIDOf extracts the task ID stamp when present.
func taskIDOf(labels map[string]string) (string, bool) {
	if t := strings.TrimSpace(labels["mod.task"]); t != "" {
		return t, true
	}
	return "", false
}

// taskActive reports whether the object's task is executing on this worker
// right now. A nil activeTaskIDs map means "the caller cannot say", and the
// planner treats that as ACTIVE — for a deleter, unknown must fail safe.
func taskActive(activeTaskIDs map[string]bool, id string) bool {
	if activeTaskIDs == nil {
		return true
	}
	return activeTaskIDs[id]
}

// PlanContainers selects orphan containers the policy would reclaim:
// MOD-labelled, not compose-managed or stack-labelled, not running /
// restarting / paused, not carrying an active task ID, older than the grace
// window.
func PlanContainers(cfg Config, conts []Container, activeTaskIDs map[string]bool, now time.Time) []Container {
	c := cfg.normalise()
	out := make([]Container, 0, len(conts))
	for _, ct := range conts {
		if !modOwned(ct.Labels) {
			continue
		}
		switch ct.State {
		case "running", "restarting", "paused":
			continue
		}
		if id, ok := taskIDOf(ct.Labels); ok && taskActive(activeTaskIDs, id) {
			continue
		}
		if now.Sub(ct.CreatedAt) < c.Grace {
			continue
		}
		out = append(out, ct)
	}
	return out
}

// Volume is one docker volume as the planner sees it.
type Volume struct {
	Name     string
	CreatedAt time.Time
	Labels   map[string]string
	UsedBy   []string
}

// PlanVolumes selects orphan task volumes the policy would reclaim: MOD-
// labelled task volumes, not keep- or checkpoint-labelled, dangling (no
// container references them), older than the grace window. Volumes are
// named modcore-task-<dagID>-<hex> (no numeric task ID), so the active-task
// check does not apply; the dangling + age + label triple is the ownership
// proof.
func PlanVolumes(cfg Config, vols []Volume, now time.Time) []Volume {
	c := cfg.normalise()
	out := make([]Volume, 0, len(vols))
	for _, v := range vols {
		labels := v.Labels
		if !hasModLabel(labels) {
			continue
		}
		if composeManaged(labels) || stackLabeled(labels) {
			continue
		}
		if labels[keepLabel] != "" || labels[checkpointLabel] != "" {
			continue
		}
		if len(v.UsedBy) != 0 {
			continue
		}
		if now.Sub(v.CreatedAt) < c.Grace {
			continue
		}
		out = append(out, v)
	}
	return out
}

// Network is one docker network as the planner sees it.
type Network struct {
	ID        string
	Name      string
	CreatedAt time.Time
	Labels    map[string]string
	// Attached is the number of containers attached (excluding any of the
	// network's own proxy sidecar — the caller subtracts it).
	Attached int
}

// PlanNetworks selects orphan TASK-SCOPED networks the policy would
// reclaim. The positive scope is the mod.task stamp: shared worker
// infrastructure networks (the user-egress bridge, the NIM posture net —
// mod.purpose-labelled, no mod.task) are LONG-LIVED by design and are never
// candidates, whatever their age. A candidate must additionally carry no
// attached containers and not belong to an active task.
func PlanNetworks(cfg Config, nets []Network, activeTaskIDs map[string]bool, now time.Time) []Network {
	c := cfg.normalise()
	out := make([]Network, 0, len(nets))
	for _, n := range nets {
		if !modOwned(n.Labels) {
			continue
		}
		id, ok := taskIDOf(n.Labels)
		if !ok {
			continue // shared infrastructure network — never a candidate
		}
		if taskActive(activeTaskIDs, id) {
			continue
		}
		if n.Attached > 0 {
			continue
		}
		if now.Sub(n.CreatedAt) < c.Grace {
			continue
		}
		out = append(out, n)
	}
	return out
}

// TaskDir is one orphan task working directory under the worker's own task
// work root. TaskID is the directory name (numeric — the scanner only ever
// surfaces numeric-named entries, which is what makes the identity claim
// safe: "stacks", "outbox", "native" and every other work-root child are
// never shaped like a task ID).
type TaskDir struct {
	Path       string
	TaskID     string
	ModifiedAt time.Time
	Size       int64
}

// PlanTaskDirs selects orphan task dirs the policy would reclaim: not an
// active task, not modified within the grace window. The work-root
// containment itself is enforced by the FS scanner (and re-checked at
// delete time), not here.
func PlanTaskDirs(cfg Config, dirs []TaskDir, activeTaskIDs map[string]bool, now time.Time) []TaskDir {
	c := cfg.normalise()
	out := make([]TaskDir, 0, len(dirs))
	for _, d := range dirs {
		if d.Size <= 0 || d.TaskID == "" {
			continue
		}
		if taskActive(activeTaskIDs, d.TaskID) {
			continue
		}
		if now.Sub(d.ModifiedAt) < c.Grace {
			continue
		}
		out = append(out, d)
	}
	return out
}

// Report is the outcome of one cleanup pass. Reclaimable* counts everything
// the policy would allow; the *Orphans lists are the full reclaimable sets
// for containers / volumes / networks / task dirs; Victims record what the
// policy would delete this pass (watermark/cap-gated); Reclaimed* record
// what was actually deleted (reclaim mode only).
type Report struct {
	At      time.Time
	Reclaim bool
	Usage   RootUsage

	ImageReclaimable   int64
	ModelReclaimable   int64
	ContainerOrphans   []Container
	VolumeOrphans      []Volume
	NetworkOrphans     []Network
	TaskDirOrphans     []TaskDir

	ImageVictims   []Image
	ModelVictims   []ModelDir
	ImageVictBytes int64
	ModelVictBytes int64

	// Reclaimed* are populated only in reclaim mode.
	ReclaimedBytes      int64
	ReclaimedCounts     map[string]int
	ReclaimedImageIDs   []string
	ReclaimedModelDirs  []string
	ReclaimedContainers []string
	ReclaimedVolumes    []string
	ReclaimedNetworks   []string
	ReclaimedTaskDirs   []string

	// imageCount / modelCount are the RECLAIMABLE counts (eligible items)
	// carried from the plans — separate from the watermark-gated victim
	// lists, so the report can say "34 images could go" even below the
	// high-water mark.
	imageCount int
	modelCount int

	// Dry-run visibility (25.5.s1): the eligible images in LRU order (capped
	// at ReportImageListLimit) and whether the image-count trigger fired.
	// A report-only pass answers "reclaimable → exactly what, oldest first"
	// while deleting nothing.
	ReclaimableImages []Image
	CountTriggered    bool

	// Dry-run visibility (25.5.s2): the eligible model dirs in LRU order
	// (capped at ModelCacheReportLimit) and whether the model-cache cap
	// actually fired this pass.
	ReclaimableModelDirs []ModelDir
	// ReclaimableModelCount is the eligible model-dir count (informational).
	ReclaimableModelCount int
	// ModelCapTriggered is true when the model-cache total exceeded the cap.
	ModelCapTriggered bool

	// Dry-run visibility (25.5.s3): the orphan sets in age order (oldest
	// first), capped at ReportImageListLimit, so a report-only pass answers
	// "which exact containers / volumes / networks / task dirs could go"
	// instead of only their counts. Orphans carry no watermark gate - the
	// reclaimable list IS what a reclaim pass removes - so these samples are
	// the dry-run preview of the sweep, listed before anything is deleted.
	ReclaimableContainers []Container
	ReclaimableVolumes    []Volume
	ReclaimableNetworks   []Network
	ReclaimableTaskDirs   []TaskDir

	// 25.5.s4: the pass RESULT — what the pass actually did (mode, start/finish,
	// per-kind reclaimed counts + bytes freed + failures with reasons). Set by
	// Run() before reclaim, filled by reclaim(); nil only on a hand-built Report.
	Res *Result
}

// NewReport seeds a report with the plan results.
func NewReport(cfg Config, usage RootUsage, img ImagePlan, mod ModelPlan,
	containers []Container, volumes []Volume, networks []Network, taskDirs []TaskDir, now time.Time) *Report {
	r := &Report{
		At:                 now,
		Reclaim:            cfg.Reclaim,
		Usage:              usage,
		ImageReclaimable:   img.Reclaimable,
		ModelReclaimable:   mod.Reclaimable,
		ContainerOrphans:   containers,
		VolumeOrphans:      volumes,
		NetworkOrphans:     networks,
		TaskDirOrphans:     taskDirs,
		ImageVictims:       img.Victims,
		ModelVictims:       mod.Victims,
		ReclaimedCounts:    map[string]int{},
	}
	r.imageCount = img.ReclaimableCount
	r.modelCount = mod.ReclaimableCount
	r.ReclaimableImages = img.ReclaimableImages
	r.CountTriggered = img.CountTriggered
	r.ReclaimableModelDirs = mod.ReclaimableModelDirs
	r.ReclaimableModelCount = mod.ReclaimableCount
	r.ModelCapTriggered = mod.CapTriggered
	r.ReclaimableContainers = sortCap(containers, func(x Container) time.Time { return x.CreatedAt })
	r.ReclaimableVolumes = sortCap(volumes, func(x Volume) time.Time { return x.CreatedAt })
	r.ReclaimableNetworks = sortCap(networks, func(x Network) time.Time { return x.CreatedAt })
	r.ReclaimableTaskDirs = sortCap(taskDirs, func(x TaskDir) time.Time { return x.ModifiedAt })
	for _, v := range img.Victims {
		r.ImageVictBytes += v.Size
	}
	for _, d := range mod.Victims {
		r.ModelVictBytes += d.Size
	}
	return r
}

// sortCap orders an orphan sample oldest-first (the order a reclaim pass
// would eat it in) and caps it at ReportImageListLimit, same bounded-report
// discipline as the image/model samples. The FULL sets stay in the *Orphans
// fields and in Counts(); this is only the visible preview. The insertion
// sort is stable, so equal ages keep the snapshot's order.
func sortCap[T any](items []T, at func(T) time.Time) []T {
	ordered := make([]T, len(items))
	for i, x := range items {
		ordered[i] = x
	}
	for i := 1; i < len(ordered); i++ {
		j := i
		for j > 0 && at(ordered[j]).Before(at(ordered[j-1])) {
			ordered[j], ordered[j-1] = ordered[j-1], ordered[j]
			j -= 1
		}
	}
	if len(ordered) > ReportImageListLimit {
		return ordered[:ReportImageListLimit]
	}
	return ordered
}

// TaskDirBytes sums the reclaimable bytes of orphan task dirs.
func TaskDirBytes(dirs []TaskDir) int64 {
	var total int64
	for _, d := range dirs {
		total += d.Size
	}
	return total
}

// ReclaimableBytes is the total the policy would allow removing this pass,
// across every kind (images + model dirs + orphan task dirs). Containers,
// volumes and networks contribute counts, not bytes — the daemon does not
// report their footprint in the list APIs.
func (r *Report) ReclaimableBytes() int64 {
	return r.ImageReclaimable + r.ModelReclaimable + TaskDirBytes(r.TaskDirOrphans)
}

// Counts returns the RECLAIMABLE count by kind — what the policy would
// allow, not what the watermarks happen to gate this pass.
func (r *Report) Counts() map[string]int {
	return map[string]int{
		"images":     r.imageCount,
		"model_dirs": r.modelCount,
		"containers": len(r.ContainerOrphans),
		"volumes":    len(r.VolumeOrphans),
		"networks":   len(r.NetworkOrphans),
		"task_dirs":  len(r.TaskDirOrphans),
	}
}

// VictimCounts returns the count by kind of what the policy would delete
// this pass (the watermark/cap-gated lists).
func (r *Report) VictimCounts() map[string]int {
	return map[string]int{
		"images":     len(r.ImageVictims),
		"model_dirs": len(r.ModelVictims),
		"containers": len(r.ContainerOrphans),
		"volumes":    len(r.VolumeOrphans),
		"networks":   len(r.NetworkOrphans),
		"task_dirs":  len(r.TaskDirOrphans),
	}
}
