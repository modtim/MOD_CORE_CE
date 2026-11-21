package cleanup

import (
	"context"
	"fmt"
	"log/slog"
	"time"
)

// Input is the per-pass snapshot the executor gathers before planning:
// everything about WHAT IS LIVE right now, as opposed to Config which is
// WHAT IS ALLOWED. Both are supplied by the caller (the worker's executor
// bridge), which owns access to the docker client, the image-LRU store and
// the active-task tracking.
type Input struct {
	// ActiveTaskIDs is the set of task IDs this worker is executing right
	// now (string form of the queue task ID).
	ActiveTaskIDs map[string]bool
	// ImageLastUsed is the worker's persisted image LRU, keyed by image ID
	// (short or full — matching is prefix-based).
	ImageLastUsed map[string]time.Time
	// ModelInUse marks model-cache dirs currently mounted by a live
	// container.
	ModelInUse map[string]bool
	// ModelRoot is the model-cache "hub" root (children are the
	// models--org--name eviction units). "" disables model eviction.
	ModelRoot string
}

// Runtime is the small docker surface the cleanup executor needs. The
// production implementation (runtime.go) adapts the worker's docker client;
// tests supply a fake that records calls.
type Runtime interface {
	ListContainers(ctx context.Context) ([]Container, error)
	ListImages(ctx context.Context) ([]Image, error)
	ListVolumes(ctx context.Context) ([]Volume, error)
	ListNetworks(ctx context.Context) ([]Network, error)
	// DockerRootUsage snapshots the partition backing the docker data root.
	// ok=false means "could not stat" — the planner then reports no
	// trigger, never deletes images.
	DockerRootUsage(ctx context.Context) (RootUsage, bool)

	RemoveContainer(ctx context.Context, id string) error
	RemoveImage(ctx context.Context, id string) error
	RemoveVolume(ctx context.Context, name string) error
	RemoveNetwork(ctx context.Context, id string) error
}

// FS is the filesystem surface: orphan task dirs under the worker's own
// task work root, and model-cache eviction units under ModelRoot.
// Implementations MUST refuse paths that resolve (after symlink
// evaluation) outside their root.
type FS interface {
	ListTaskDirs(ctx context.Context) ([]TaskDir, error)
	RemoveTaskDir(ctx context.Context, path string) error
	ListModelDirs(ctx context.Context, root string) ([]ModelDir, error)
	RemoveModelDir(ctx context.Context, root, path string) error
}

// Executor runs one cleanup pass: gather, plan, (optionally) reclaim,
// report. It holds no state between passes — the caller stores the
// returned Report for the heartbeat.
type Executor struct {
	rt  Runtime
	fs  FS
	log *slog.Logger
}

// Result is what a pass ACTUALLY did, as opposed to the Report's plan
// fields which say what it could do. 25.5.s4 closes the "reclaimable →
// reclaimed" gap: after a pass the heartbeat can say per kind how many
// objects went, how many bytes that freed (only kinds with a known size:
// images, model dirs, task dirs), and which deletes failed and why — a
// failure never aborts the rest of the pass, it just stays in the
// reclaimable list. Mode is "dry_run" (report-only: zero reclaimed by
// construction) or "reclaim".
type Result struct {
	Mode     string
	Started  time.Time
	Finished time.Time

	Counts map[string]int
	// BytesFreed is per-kind bytes freed, only for kinds whose size the
	// snapshot knows (containers/volumes/networks carry no size in the
	// list APIs, so they are counted, not measured).
	BytesFreed     map[string]int64
	ReclaimedBytes int64
	Failures       []Failure
}

// Failure is one delete that did not go, with the reason, so the operator
// sees "3 of 4 gone, this one refused because ..." instead of silence.
type Failure struct {
	Kind   string
	Ref    string
	Reason string
}

// newResult seeds a Result for one pass under cfg.
func newResult(cfg Config, started time.Time) *Result {
	 mode := "dry_run"
	 if cfg.Reclaim {
	  mode = "reclaim"
	 }
	 return &Result{
	  Mode: mode,
		Started:  started,
		Counts:   map[string]int{},
		BytesFreed: map[string]int64{},
	}
}

// note records one successful delete in the pass result.
func note(res *Result, kind string, bytes int64) {
	res.Counts[kind]++
	if bytes > 0 {
		res.BytesFreed[kind] += bytes
	}
}

// noteFailure records one refused delete (capped, so a pathological pass
// can never grow the heartbeat payload without bound).
func noteFailure(res *Result, kind, ref string, err error) {
	if len(res.Failures) >= resultFailureLimit {
		return
	}
	res.Failures = append(res.Failures, Failure{Kind: kind, Ref: ref, Reason: fmt.Sprint(err)})
}

const resultFailureLimit = 25

// New builds an Executor. rt and fs must be non-nil; a nil log becomes the
// default logger.
func New(rt Runtime, fs FS, log *slog.Logger) *Executor {
	if log == nil {
		log = slog.Default()
	}
	return &Executor{rt: rt, fs: fs, log: log}
}

// Run performs one pass under cfg. It never fails the worker on a partial
// listing: a kind that cannot be enumerated is reported empty for that
// pass (and, in reclaim mode, simply not deleted). The report is always
// returned — even when nothing is reclaimable, so last_run advances.
func (x *Executor) Run(ctx context.Context, cfg Config, in Input) *Report {
	now := time.Now()
	cfg = cfg.normalise()

	// Containers: the container list feeds both the orphan plan and the
	// image in-use set. Plan the orphans FIRST, because an image referenced
	// ONLY by a container this pass reclaims is itself reclaimable — reclaim
	// below removes the orphan containers before their images, and the
	// daemon still refuses an image remove while any surviving container
	// references it (a container appearing between snapshot and delete
	// keeps its image safe for free).
	containers, cerr := x.rt.ListContainers(ctx)
	if cerr != nil {
		x.log.Warn("cleanup: container list failed; skipping containers + image in-use this pass", "err", cerr)
		containers = nil
	}
	ctOrphans := PlanContainers(cfg, containers, in.ActiveTaskIDs, now)
	orphanImageIDs := make(map[string]bool, len(ctOrphans))
	for _, ct := range ctOrphans {
		if ct.ImageID != "" {
			orphanImageIDs[ct.ImageID] = true
		}
	}
	inUseImages := make(map[string]bool, len(containers))
	for _, ct := range containers {
		if ct.ImageID == "" || orphanImageIDs[ct.ImageID] {
			continue
		}
		inUseImages[ct.ImageID] = true
	}

	usage, usageOK := x.rt.DockerRootUsage(ctx)
	if !usageOK {
		x.log.Debug("cleanup: docker root usage unavailable; image GC will report, not trigger")
	}

	images, ierr := x.rt.ListImages(ctx)
	if ierr != nil {
		x.log.Warn("cleanup: image list failed; skipping image GC this pass", "err", ierr)
		images = nil
	}
	for i := range images {
		if t, ok := in.ImageLastUsed[images[i].ID]; ok {
			images[i].LastUsed = t
		}
	}

	volumes, verr := x.rt.ListVolumes(ctx)
	if verr != nil {
		x.log.Warn("cleanup: volume list failed; skipping volume plan this pass", "err", verr)
		volumes = nil
	}

	networks, nerr := x.rt.ListNetworks(ctx)
	if nerr != nil {
		x.log.Warn("cleanup: network list failed; skipping network plan this pass", "err", nerr)
		networks = nil
	}

	modelDirs := []ModelDir{}
	if in.ModelRoot != "" {
		md, merr := x.fs.ListModelDirs(ctx, in.ModelRoot)
		if merr != nil {
			x.log.Warn("cleanup: model dir list failed; skipping model GC this pass", "err", merr)
		} else {
			for i := range md {
				if in.ModelInUse != nil && in.ModelInUse[md[i].Path] {
					md[i].InUse = true
				}
			}
			modelDirs = md
		}
	}

	taskDirs := []TaskDir{}
	td, terr := x.fs.ListTaskDirs(ctx)
	if terr != nil {
		x.log.Warn("cleanup: task dir list failed; skipping task dir plan this pass", "err", terr)
	} else {
		taskDirs = td
	}

	// in.ActiveTaskIDs may be nil — "cannot say" — in which case the
	// planners treat every stamped task as active (fail-safe for a deleter).
	imgPlan := PlanImages(cfg, usage, images, inUseImages, now)
	modPlan := PlanModelDirs(cfg, modelDirs, now)
	volOrphans := PlanVolumes(cfg, volumes, now)
	netOrphans := PlanNetworks(cfg, networks, in.ActiveTaskIDs, now)
	dirOrphans := PlanTaskDirs(cfg, taskDirs, in.ActiveTaskIDs, now)

	rep := NewReport(cfg, usage, imgPlan, modPlan, ctOrphans, volOrphans, netOrphans, dirOrphans, now)

	// 25.5.s4: the pass result travels on the report so the caller can
	// report what was ACTUALLY reclaimed (mode + counts + bytes + failures)
	// after the pass, not just what was reclaimable.
	rep.Res = newResult(cfg, now)

	x.log.Info("cleanup pass",
		"mode", modeName(cfg),
		"docker_root_used_pct", usage.UsedPct(),
		"reclaimable_bytes", rep.ReclaimableBytes(),
		"counts", rep.Counts(),
	)

	if !cfg.Reclaim {
		// REPORT ONLY: zero deletions, by construction — no Remove* call
		// below ever executes. The result still closes, so the heartbeat can
		// say "last pass: dry_run, 0 reclaimed".
		rep.Res.Finished = time.Now()
		return rep
	}

	x.reclaim(ctx, in.ModelRoot, rep)
	rep.Res.Finished = time.Now()
	return rep
}

func modeName(cfg Config) string {
	if cfg.Reclaim {
		return "reclaim"
	}
	return "report"
}

// reclaim executes the plan's deletions, recording what actually went.
// Each kind is independent: one daemon error never blocks the others, and a
// delete that fails simply stays in the reclaimable list for the next pass.
// The per-kind accounting lands on rep.Res (25.5.s4) so the heartbeat can
// report reclaimed counts/bytes/failures, not just the plan.
func (x *Executor) reclaim(ctx context.Context, modelRoot string, rep *Report) {
	// Defensive: a Report built without Run (tests, replay) has no Result.
	if rep.Res == nil {
		rep.Res = newResult(Config{Reclaim: true}, time.Now())
	}
	res := rep.Res

	// Orphan containers FIRST: a victim image can still be referenced by an
	// orphan container (only this pass's orphan set was exempted from the
	// in-use set), and the daemon refuses to remove an image a container
	// still references — so the containers that pin the victims must go
	// first.
	for _, ct := range rep.ContainerOrphans {
		rm, cancel := context.WithTimeout(ctx, 30*time.Second)
		err := x.rt.RemoveContainer(rm, ct.ID)
		cancel()
		if err != nil {
			x.log.Warn("cleanup: container remove failed; keeping it", "id", ct.ID, "err", err)
			noteFailure(res, "containers", ct.ID, err)
			continue
		}
		note(res, "containers", 0)
		rep.ReclaimedCounts["containers"]++
		rep.ReclaimedContainers = append(rep.ReclaimedContainers, ct.ID)
		x.log.Info("cleanup: orphan container reclaimed", "id", ct.ID, "name", ct.Name)
	}

	for _, img := range rep.ImageVictims {
		rm, cancel := context.WithTimeout(ctx, 5*time.Minute)
		err := x.rt.RemoveImage(rm, img.ID)
		cancel()
		if err != nil {
			x.log.Warn("cleanup: image remove failed; keeping it", "id", img.ID, "err", err)
			noteFailure(res, "images", img.ID, err)
			continue
		}
		note(res, "images", img.Size)
		res.ReclaimedBytes += img.Size
		rep.ReclaimedBytes += img.Size
		rep.ReclaimedCounts["images"]++
		rep.ReclaimedImageIDs = append(rep.ReclaimedImageIDs, img.ID)
		x.log.Info("cleanup: image reclaimed", "id", img.ID, "tags", img.RepoTags, "mb", img.Size/(1024*1024))
	}

	for _, d := range rep.ModelVictims {
		rm, cancel := context.WithTimeout(ctx, 10*time.Minute)
		err := x.fs.RemoveModelDir(rm, modelRoot, d.Path)
		cancel()
		if err != nil {
			x.log.Warn("cleanup: model dir remove failed; keeping it", "path", d.Path, "err", err)
			noteFailure(res, "model_dirs", d.Path, err)
			continue
		}
		note(res, "model_dirs", d.Size)
		res.ReclaimedBytes += d.Size
		rep.ReclaimedBytes += d.Size
		rep.ReclaimedCounts["model_dirs"]++
		rep.ReclaimedModelDirs = append(rep.ReclaimedModelDirs, d.Path)
		x.log.Info("cleanup: model dir reclaimed", "path", d.Path, "gb", d.Size/(1<<30))
	}

	for _, v := range rep.VolumeOrphans {
		rm, cancel := context.WithTimeout(ctx, 30*time.Second)
		err := x.rt.RemoveVolume(rm, v.Name)
		// Volumes carry no size in the list API — counted, not measured.
		cancel()
		if err != nil {
			x.log.Warn("cleanup: volume remove failed; keeping it", "volume", v.Name, "err", err)
			noteFailure(res, "volumes", v.Name, err)
			continue
		}
		note(res, "volumes", 0)
		rep.ReclaimedCounts["volumes"]++
		rep.ReclaimedVolumes = append(rep.ReclaimedVolumes, v.Name)
		x.log.Info("cleanup: orphan volume reclaimed", "volume", v.Name)
	}

	for _, n := range rep.NetworkOrphans {
		rm, cancel := context.WithTimeout(ctx, 30*time.Second)
		err := x.rt.RemoveNetwork(rm, n.ID)
		// Networks carry no size in the list API — counted, not measured.
		cancel()
		if err != nil {
			x.log.Warn("cleanup: network remove failed; keeping it", "id", n.ID, "err", err)
			noteFailure(res, "networks", n.ID, err)
			continue
		}
		note(res, "networks", 0)
		rep.ReclaimedCounts["networks"]++
		rep.ReclaimedNetworks = append(rep.ReclaimedNetworks, n.ID)
		x.log.Info("cleanup: orphan network reclaimed", "id", n.ID, "name", n.Name)
	}

	for _, d := range rep.TaskDirOrphans {
		rm, cancel := context.WithTimeout(ctx, 5*time.Minute)
		err := x.fs.RemoveTaskDir(rm, d.Path)
		cancel()
		if err != nil {
			x.log.Warn("cleanup: task dir remove failed; keeping it", "path", d.Path, "err", err)
			noteFailure(res, "task_dirs", d.Path, err)
			continue
		}
		note(res, "task_dirs", d.Size)
		res.ReclaimedBytes += d.Size
		rep.ReclaimedBytes += d.Size
		rep.ReclaimedCounts["task_dirs"]++
		rep.ReclaimedTaskDirs = append(rep.ReclaimedTaskDirs, d.Path)
		x.log.Info("cleanup: orphan task dir reclaimed", "path", d.Path, "mb", d.Size/(1024*1024))
	}
}
