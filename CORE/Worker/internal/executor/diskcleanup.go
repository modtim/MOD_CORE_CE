package executor

import (
	"context"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/filters"
)

// Worker-side Docker disk reclamation.
//
// Background: the only auto-cleanup that existed was the API's
// services/docker_disk_monitor.py — an APScheduler job inside the API
// container. That container has no docker socket, no docker CLI, and no
// view of /var/lib/docker, so the monitor silently no-op'd every tick
// (disk_usage('/var/lib/docker') → None → return). Meanwhile the worker
// is the process that actually fills the disk (it pulls every node image
// and runs every build), it holds the docker socket, and it runs on EVERY
// host — embedded + remote. So disk cleanup belongs here.
//
// Two triggers, both routed through runPrune (TryLock-serialized so a
// threshold sweep and a post-build prune can't stack):
//
//   1. Threshold sweep (StartDiskCleanup): a 15-minute loop reads the
//      docker data-root partition's used-%. Over MOD_DISK_AUTO_CLEAN_PCT
//      (default 85) it prunes build cache + dangling layers (always safe).
//      Over MOD_DISK_DEEP_CLEAN_PCT (default 95) it also prunes all unused
//      images (re-pulled on demand) — matches the old `docker image
//      prune -af` emergency behaviour.
//
//   2. Post-build light prune (lightPruneAfterBuild): after every
//      ImageBuilder task, regardless of disk-%, prune build cache +
//      dangling intermediate layers so a build worker doesn't accrue
//      layer sludge between sweeps. Never touches tagged images.
//
// Disable entirely with MOD_DISK_CLEANUP_DISABLED=true.

type pruneLevel int

const (
	pruneNone  pruneLevel = iota
	pruneLight            // build cache + dangling images (always safe)
	pruneDeep             // light + all unused images (emergency reclaim)
)

func (p pruneLevel) String() string {
	switch p {
	case pruneLight:
		return "light"
	case pruneDeep:
		return "deep"
	default:
		return "none"
	}
}

// diskCleanupAction is the pure threshold policy — extracted so it can be
// unit-tested without a Docker client or a real filesystem.
func diskCleanupAction(usedPct, autoPct, deepPct int) pruneLevel {
	if usedPct >= deepPct {
		return pruneDeep
	}
	if usedPct >= autoPct {
		return pruneLight
	}
	return pruneNone
}

func diskCleanupDisabled() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("MOD_DISK_CLEANUP_DISABLED")))
	return v == "1" || v == "true" || v == "yes"
}

// envPctClamped reads an integer percentage env var, clamped to [0,100],
// falling back to def on absence / parse failure.
func envPctClamped(name string, def int) int {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return def
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	if v < 0 {
		return 0
	}
	if v > 100 {
		return 100
	}
	return v
}

// dockerDiskUsedPct returns percent-used for the partition holding the
// docker data root. Inside a containerized worker, statfs("/") passes
// through overlayfs to the host filesystem backing /var/lib/docker (the
// same trick monitor.readDiskFree relies on for heartbeat DiskFreeGB);
// when the daemon reports a stat-able DockerRootDir, prefer it. Returns
// (pct, true) on success, (0, false) when the partition can't be statted
// (e.g. a platform where statfs isn't implemented).
func (e *Executor) dockerDiskUsedPct(ctx context.Context) (int, bool) {
	path := "/"
	infoCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	if info, err := e.docker.Info(infoCtx); err == nil && info.DockerRootDir != "" {
		if _, serr := os.Stat(info.DockerRootDir); serr == nil {
			path = info.DockerRootDir
		}
	}
	cancel()
	return statfsUsedPct(path)
}

// pruneBuildCache drops all reclaimable build cache. Always safe — cache
// is regenerated on demand. Returns bytes reclaimed.
func (e *Executor) pruneBuildCache(ctx context.Context) uint64 {
	cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	rep, err := e.docker.BuildCachePrune(cctx, types.BuildCachePruneOptions{All: true})
	if err != nil {
		e.log.Warn("disk cleanup: build cache prune failed", "err", err)
		return 0
	}
	if rep == nil {
		return 0
	}
	if rep.SpaceReclaimed > 0 {
		e.log.Info("disk cleanup: build cache pruned",
			"reclaimed_mb", rep.SpaceReclaimed/(1024*1024))
	}
	return rep.SpaceReclaimed
}

// pruneImages removes images. danglingOnly=true → only untagged
// intermediate layers (always safe — never removes a node image that
// could be reused). danglingOnly=false → every image not used by a
// container (deep clean; a stopped-task image is re-pulled on next use).
func (e *Executor) pruneImages(ctx context.Context, danglingOnly bool) uint64 {
	cctx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	args := filters.NewArgs()
	if danglingOnly {
		args.Add("dangling", "true")
	} else {
		args.Add("dangling", "false")
	}
	rep, err := e.docker.ImagesPrune(cctx, args)
	if err != nil {
		e.log.Warn("disk cleanup: image prune failed", "err", err, "dangling_only", danglingOnly)
		return 0
	}
	if rep.SpaceReclaimed > 0 {
		e.log.Info("disk cleanup: images pruned",
			"reclaimed_mb", rep.SpaceReclaimed/(1024*1024),
			"removed", len(rep.ImagesDeleted),
			"dangling_only", danglingOnly)
	}
	return rep.SpaceReclaimed
}

// runPrune executes a prune at the given level. TryLock-serialized: if a
// prune is already running (e.g. a threshold sweep overlapping a
// post-build prune), this call is skipped rather than stacking a second
// concurrent prune. Errors inside each step are logged + swallowed.
//
// autoPct is used for deep pruning to calculate how much space to reclaim.
func (e *Executor) runPrune(ctx context.Context, level pruneLevel, reason string, autoPct int) {
	if level == pruneNone {
		return
	}
	if !e.pruneMu.TryLock() {
		e.log.Debug("disk cleanup: prune already in progress — skipping", "reason", reason)
		return
	}
	defer e.pruneMu.Unlock()

	e.log.Info("disk cleanup: starting prune", "reason", reason, "level", level.String(), "auto_pct", autoPct)
	var total uint64
	total += e.pruneBuildCache(ctx)
	total += e.pruneImages(ctx, true) // dangling layers — always safe
	if level == pruneDeep {
		// For deep prune, use LRU GC to reclaim enough space to get under autoPct
		total += e.runLRUPrune(ctx, autoPct)
		// Images go first (above); only once those are gone do we touch model
		// weights, which are typically much larger and far costlier to
		// re-fetch (see modelcache_gc.go). 0 → evictModelCache reads its own
		// MOD_WORKER_MIN_FREE_GB threshold rather than reusing the image-GC
		// disk-%, since the two are different units (bytes vs. GB-floor).
		total += e.evictModelCache(ctx, 0)
	}
	e.log.Info("disk cleanup: prune complete",
		"reason", reason, "level", level.String(),
		"reclaimed_mb", total/(1024*1024))
}

// runLRUPrune performs LRU-based image garbage collection to reclaim enough
// space to get the data root under autoPct. Uses a sentinel value to walk the
// whole eligible list since we can't easily predict how much space we need
// without statfs.
func (e *Executor) runLRUPrune(ctx context.Context, autoPct int) uint64 {
	// Get current disk usage
	path := "/"
	if info, err := e.docker.Info(ctx); err == nil && info.DockerRootDir != "" {
		if _, serr := os.Stat(info.DockerRootDir); serr == nil {
			path = info.DockerRootDir
		}
	}

	total, available, ok := getDiskSpace(path)
	if !ok {
		e.log.Warn("disk cleanup: cannot stat docker data root for LRU GC", "path", path)
		// Fall back to large sentinel value
		return e.lruImageGC(ctx, 100*1024*1024*1024) // 100GB sentinel
	}

	// Calculate current usage percentage
	currentPct := int((total - available) * 100 / total)
	if currentPct < autoPct {
		// Already under threshold, no need to prune
		e.log.Debug("disk cleanup: already under auto-pct for LRU GC",
			"current_pct", currentPct, "auto_pct", autoPct)
		return 0
	}

	// Calculate how much we need to free to get under autoPct
	// We want: (total - available - toFree) / total <= autoPct/100
	// So: toFree >= total - available - (total * autoPct / 100)
	targetAvailable := total * uint64(autoPct) / 100
	currentAvailable := available
	needBytes := int64(currentAvailable - targetAvailable)
	if needBytes <= 0 {
		return 0
	}

	e.log.Info("disk cleanup: LRU GC will reclaim space",
		"current_pct", currentPct,
		"auto_pct", autoPct,
		"need_bytes", needBytes,
		"available_gb", currentAvailable/(1024*1024*1024))

	// Use a large sentinel if the calculated need is small,
	// to ensure we walk the full eligible list
	if needBytes < 100*1024*1024 {
		needBytes = 100 * 1024 * 1024 // 100MB minimum
	}

	return e.lruImageGC(ctx, needBytes)
}

// lightPruneAfterBuild fires a light prune in the background after a build
// task. Uses its own context (not the task's, which may be canceled on
// return) and runs async so it never delays the task result.
func (e *Executor) lightPruneAfterBuild() {
	if diskCleanupDisabled() {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Minute)
		defer cancel()
		e.runPrune(ctx, pruneLight, "post-build", envPctClamped("MOD_DISK_AUTO_CLEAN_PCT", 85))
	}()
}

// StartDiskCleanup launches the threshold sweep loop. Idempotent-safe to
// call once at startup (from StartBackgroundJobs). Exits when ctx is
// canceled. No-op when MOD_DISK_CLEANUP_DISABLED is set.
func (e *Executor) StartDiskCleanup(ctx context.Context) {
	if diskCleanupDisabled() {
		e.log.Info("disk cleanup: disabled via MOD_DISK_CLEANUP_DISABLED")
		return
	}
	auto := envPctClamped("MOD_DISK_AUTO_CLEAN_PCT", 85)
	deep := envPctClamped("MOD_DISK_DEEP_CLEAN_PCT", 95)
	if deep < auto {
		deep = auto
	}
	e.log.Info("disk cleanup: enabled", "auto_clean_pct", auto, "deep_clean_pct", deep)

	check := func() {
		pct, ok := e.dockerDiskUsedPct(ctx)
		if !ok {
			e.log.Debug("disk cleanup: cannot stat docker data root — skipping tick")
			return
		}
		action := diskCleanupAction(pct, auto, deep)
		if action == pruneNone {
			e.log.Debug("disk cleanup: below threshold", "used_pct", pct, "auto_pct", auto)
			return
		}
		if action == pruneDeep {
			e.log.Warn("disk cleanup: disk pressure HIGH — deep prune",
				"used_pct", pct, "deep_pct", deep)
		} else {
			e.log.Info("disk cleanup: over auto-clean threshold",
				"used_pct", pct, "auto_pct", auto)
		}
		e.runPrune(ctx, action, "threshold-"+action.String(), auto)
	}

	go func() {
		// Small settle delay so the initial container-prune pass and image
		// pulls aren't racing the first disk check at boot.
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Minute):
		}
		check() // initial sweep
		ticker := time.NewTicker(15 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				check()
			}
		}
	}()
}
