package executor

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/cleanup"
)

// Megaplan 25.5 — worker self-cleanup. The planner + executor live in
// internal/cleanup (pure policy + a small Docker/FS interface); this bridge
// is the only piece that knows the worker: it gathers the live snapshot
// (active task IDs, image LRU, in-use model dirs), resolves the policy from
// the API-pushed tunables, runs the periodic goroutine, and exposes the
// latest report to the heartbeat.
//
// The reclaim gate defaults to OFF (report only). The API flips it live via
// the worker.cleanup_reclaim platform setting, delivered on the heartbeat
// `tunables` map (GS.PSR.7) — no env var, no worker restart.

const (
	cleanupReclaimTunableKey         = "worker.cleanup_reclaim"
	cleanupHighWaterTunableKey       = "worker.cleanup_image_high_water_pct"
	cleanupLowWaterTunableKey        = "worker.cleanup_image_low_water_pct"
	cleanupModelCapTunableKey        = "worker.cleanup_model_cache_max_gb"
	cleanupGraceTunableKey           = "worker.cleanup_grace_hours"
	cleanupImageCountCapTunableKey   = "worker.cleanup_image_count_cap"
	cleanupModelCacheCapGbTunableKey = "worker.cleanup_model_cache_cap_gb"
	cleanupTick                      = 30 * time.Minute
	cleanupSettle                    = 2 * time.Minute
	cleanupPassTimeout               = 25 * time.Minute
)

// cleanupWorkRoot is the worker's own task work root — where native
// backends keep their per-task scratch dirs (initNativeBackend /
// initWslcSession use the same preference: /var/lib/mod-worker, falling
// back to the system temp dir).
func cleanupWorkRoot() string {
	const root = "/var/lib/mod-worker"
	if _, err := os.Stat(root); err == nil {
		return root
	}
	return os.TempDir()
}

// cleanupConfig resolves the self-cleanup policy from the server-pushed
// tunables, falling back to the built-in defaults (reclaim OFF). The
// pinned-image set is the built-in default (MOD Core's own
// ghcr.io/modtim/* infrastructure images) extended by the
// existing MOD_WORKER_PINNED_IMAGES list the executor already applies to
// image GC (same semantics, same source) so the two paths can never
// disagree about what is protected.
func (e *Executor) cleanupConfig() cleanup.Config {
	cfg := cleanup.DefaultConfig()
	if len(e.pinnedImages) > 0 {
		cfg.PinnedImagePatterns = append(append([]string{}, cfg.PinnedImagePatterns...), e.pinnedImages...)
	}
	if e.tunables != nil {
		cfg.Reclaim = e.tunables.Bool(cleanupReclaimTunableKey, "", false)
		cfg.HighWaterPct = e.tunables.Int(cleanupHighWaterTunableKey, cfg.HighWaterPct)
		cfg.LowWaterPct = e.tunables.Int(cleanupLowWaterTunableKey, cfg.LowWaterPct)
		if gb := e.tunables.Int(cleanupModelCapTunableKey, -1); gb >= 0 {
			cfg.ModelCacheCapBytes = int64(gb) << 30
		}
		if h := e.tunables.Int(cleanupGraceTunableKey, -1); h >= 0 {
			cfg.Grace = time.Duration(h) * time.Hour
		}
		if cap := e.tunables.Int(cleanupImageCountCapTunableKey, 0); cap > 0 {
			cfg.ImageCountCap = cap
		}
		// 25.5.s2: the API-side model-cache cap (GiB, 0 = off). normalise()
		// folds it into ModelCacheCapBytes as the tighter of the two, so the
		// built-in 500 GiB default is never lowered by a 0 / unset value.
		if gb := e.tunables.Int(cleanupModelCacheCapGbTunableKey, -1); gb >= 0 {
			cfg.ModelCacheCapGb = gb
		}
	}
	return cfg
}

// SetActiveTaskIDsProvider wires the live set of executing task IDs into the
// self-cleanup bridge. Called once at startup by the worker (the heartbeat
// already tracks active tasks); nil-safe — without a provider the sweep
// conservatively treats every task ID as active and reclaims nothing.
func (e *Executor) SetActiveTaskIDsProvider(fn func() []int) {
	e.cleanupMu.Lock()
	defer e.cleanupMu.Unlock()
	e.cleanupActiveTaskIDs = fn
}

func (e *Executor) activeTaskIDSet() map[string]bool {
	e.cleanupMu.RLock()
	fn := e.cleanupActiveTaskIDs
	e.cleanupMu.RUnlock()
	if fn == nil {
		// No provider wired: nil means "cannot say" and the planners treat
		// every stamped task as active (the safe default for a deleter).
		return nil
	}
	out := map[string]bool{}
	for _, id := range fn() {
		out[strconv.Itoa(id)] = true
	}
	return out
}

// startSelfCleanup launches the periodic self-cleanup goroutine. Called from
// StartBackgroundJobs. No-op without a docker client (k8s-mode workers
// have no local docker objects to reclaim). Report-only unless the
// worker.cleanup_reclaim tunable is pushed on.
func (e *Executor) startSelfCleanup(ctx context.Context) {
	if e.docker == nil {
		e.log.Debug("self-cleanup: no docker client; not started")
		return
	}
	rt := cleanup.NewDockerRuntime(e.docker)
	fs := cleanup.NewOSFS(cleanupWorkRoot(), e.log)
	eng := cleanup.New(rt, fs, e.log)

	e.log.Info("self-cleanup: enabled",
		"mode", "report-only until worker.cleanup_reclaim is pushed",
		"tick", cleanupTick.String(),
		"work_root", cleanupWorkRoot())

	go func() {
		pass := func() {
			passCtx, cancel := context.WithTimeout(ctx, cleanupPassTimeout)
			defer cancel()
			cfg := e.cleanupConfig()
			modelRoot := modelCacheHubRoot()
			var modelInUse map[string]bool
			if dirs, err := fs.ListModelDirs(passCtx, modelRoot); err == nil && len(dirs) > 0 {
				paths := make([]string, 0, len(dirs))
				for _, d := range dirs {
					paths = append(paths, d.Path)
				}
				modelInUse = e.ModelCacheInUse(passCtx, paths)
			}
			rep := eng.Run(passCtx, cfg, cleanup.Input{
				ActiveTaskIDs: e.activeTaskIDSet(),
				ImageLastUsed: e.ImageLastUsedSnapshot(),
				ModelInUse:    modelInUse,
				ModelRoot:     modelRoot,
			})
			e.storeCleanupReport(rep)
		}

		select {
		case <-ctx.Done():
			return
		case <-time.After(cleanupSettle):
		}
		pass() // initial sweep after settle
		ticker := time.NewTicker(cleanupTick)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				pass()
			}
		}
	}()
}

func (e *Executor) storeCleanupReport(rep *cleanup.Report) {
	e.cleanupMu.Lock()
	defer e.cleanupMu.Unlock()
	e.cleanupLastReport = rep
	// Keep the pre-existing simple record in lock-step (GetLastCleanup).
	e.lastCleanup = &LastCleanup{
		At:               rep.At,
		ReclaimedBytes:   uint64(rep.ReclaimedBytes),
		ReclaimableBytes: uint64(rep.ReclaimableBytes()),
	}
}

// CleanupSummary shapes the latest self-cleanup report for the heartbeat
// `cleanup` key. nil when no pass has run yet (or the worker has no docker
// client), so the manager reads "not reported" rather than a stale zero.
func (e *Executor) CleanupSummary() map[string]interface{} {
	e.cleanupMu.RLock()
	rep := e.cleanupLastReport
	e.cleanupMu.RUnlock()
	if rep == nil {
		return nil
	}
	mode := "report"
	if rep.Reclaim {
		mode = "reclaim"
	}
	out := map[string]interface{}{
		"last_run":          rep.At.UTC().Format(time.RFC3339),
		"mode":              mode,
		"reclaimable_bytes": rep.ReclaimableBytes(),
		"reclaimed_bytes":   rep.ReclaimedBytes,
		"counts":            rep.Counts(),
		"reclaimed_counts":  rep.ReclaimedCounts,
	}
	if rep.Usage.Valid() {
		out["docker_root_used_pct"] = rep.Usage.UsedPct()
	}
	// Sample of what is reclaimable, so the operator can see exactly what a
	// reclaim pass would remove before flipping the flag on.
	if len(rep.ImageVictims) > 0 {
		tags := make([]string, 0, len(rep.ImageVictims))
		for _, v := range rep.ImageVictims {
			tags = append(tags, fmt.Sprint(v.RepoTags))
		}
		out["image_victims"] = tags
	}
	// 25.5.s1 dry-run surface: the eligible list in LRU order (planner caps
	// it), so reclaimable → reclaimed is visible per image before anything
	// is ever deleted, plus which trigger fired.
	if len(rep.ReclaimableImages) > 0 {
		l := make([]map[string]interface{}, 0, len(rep.ReclaimableImages))
		for _, v := range rep.ReclaimableImages {
			l = append(l, map[string]interface{}{
				"id": v.ID, "tags": v.RepoTags, "size_bytes": v.Size,
			})
		}
		out["reclaimable_images"] = l
	}
	if rep.CountTriggered {
		out["count_triggered"] = true
	}
	// 25.5.s2 model dry-run surface: the eligible model dirs in LRU order
	// (planner caps the list), the victims this pass, and whether the cap
	// fired — so "what could go" is visible before any eviction.
	if len(rep.ModelVictims) > 0 {
		vs := make([]string, 0, len(rep.ModelVictims))
		for _, v := range rep.ModelVictims {
			vs = append(vs, v.Path)
		}
		out["model_victims"] = vs
	}
	if len(rep.ReclaimableModelDirs) > 0 {
		ms := make([]map[string]interface{}, 0, len(rep.ReclaimableModelDirs))
		for _, v := range rep.ReclaimableModelDirs {
			ms = append(ms, map[string]interface{}{
				"path": v.Path, "size_bytes": v.Size,
				"atime": v.Atime.UTC().Format(time.RFC3339),
			})
		}
		out["reclaimable_model_dirs"] = ms
	}
	if rep.ReclaimableModelCount > 0 {
		out["reclaimable_model_count"] = rep.ReclaimableModelCount
	}
	if rep.ModelCapTriggered {
		out["model_cap_triggered"] = true
	}
	// 25.5.s3 orphan dry-run surface: the EXACT containers / volumes / networks
	// / task dirs a reclaim pass would remove (age order, capped by the planner),
	// so "reclaimable -> reclaimed" is visible per object before anything goes.
	if len(rep.ReclaimableContainers) > 0 {
		cs := make([]map[string]interface{}, 0, len(rep.ReclaimableContainers))
		for _, v := range rep.ReclaimableContainers {
			cs = append(cs, map[string]interface{}{
				"id": v.ID, "name": v.Name, "state": v.State,
				"created": v.CreatedAt.UTC().Format(time.RFC3339),
			})
		}
		out["reclaimable_containers"] = cs
	}
	if len(rep.ReclaimableVolumes) > 0 {
		vs := make([]map[string]interface{}, 0, len(rep.ReclaimableVolumes))
		for _, v := range rep.ReclaimableVolumes {
			vs = append(vs, map[string]interface{}{
				"name":    v.Name,
				"created": v.CreatedAt.UTC().Format(time.RFC3339),
			})
		}
		out["reclaimable_volumes"] = vs
	}
	if len(rep.ReclaimableNetworks) > 0 {
		ns := make([]map[string]interface{}, 0, len(rep.ReclaimableNetworks))
		for _, v := range rep.ReclaimableNetworks {
			ns = append(ns, map[string]interface{}{
				"id": v.ID, "name": v.Name,
				"created": v.CreatedAt.UTC().Format(time.RFC3339),
			})
		}
		out["reclaimable_networks"] = ns
	}
	if len(rep.ReclaimableTaskDirs) > 0 {
		ds := make([]map[string]interface{}, 0, len(rep.ReclaimableTaskDirs))
		for _, v := range rep.ReclaimableTaskDirs {
			ds = append(ds, map[string]interface{}{
				"path": v.Path, "task_id": v.TaskID, "size_bytes": v.Size,
				"modified": v.ModifiedAt.UTC().Format(time.RFC3339),
			})
		}
		out["reclaimable_task_dirs"] = ds
	}
	// 25.5.s4: what the LAST pass actually did (mode, start/finish, per-kind
	// reclaimed counts + bytes freed + failures with reasons) — the other half
	// of "reclaimable → reclaimed". Omitted only when no result is attached.
	if lc := cleanupPassSummary(rep); lc != nil {
		out["last_cleanup"] = lc
	}
	return out
}

// cleanupPassSummary shapes Report.Res for the heartbeat `cleanup` key.
func cleanupPassSummary(rep *cleanup.Report) map[string]interface{} {
	res := rep.Res
	if res == nil {
		return nil
	}
	fails := make([]map[string]interface{}, 0, len(res.Failures))
	for _, f := range res.Failures {
		fails = append(fails, map[string]interface{}{
			"kind": f.Kind, "ref": f.Ref, "reason": f.Reason,
		})
	}
	finished := res.Finished
	if finished.IsZero() {
		finished = res.Started
	}
	return map[string]interface{}{
		"mode":        res.Mode,
		"started":     res.Started.UTC().Format(time.RFC3339),
		"finished":    finished.UTC().Format(time.RFC3339),
		"counts":      res.Counts,
		"bytes_freed": res.BytesFreed,
		"bytes_total": res.ReclaimedBytes,
		"failures":    fails,
	}
}

// ModelCacheInUse reports which of the given model-cache dirs are currently
// mounted by a running container — directly (a narrow bind of that one dir)
// or indirectly (a bind of the shared cache root or hub, which is how
// stacks normally mount it). Same "ask docker, don't trust a manual
// ref-count" approach as inUseModelDirs in modelcache_gc.go.
//
// Fail-safe: when the container list cannot be read, EVERY path is reported
// in use — the self-cleanup must never evict weights it cannot prove are
// not mounted.
func (e *Executor) ModelCacheInUse(ctx context.Context, paths []string) map[string]bool {
	inUse := make(map[string]bool, len(paths))
	if len(paths) == 0 {
		return inUse
	}
	if e.docker == nil {
		for _, p := range paths {
			inUse[p] = true
		}
		return inUse
	}
	containers, err := e.docker.ContainerList(ctx, container.ListOptions{})
	if err != nil {
		for _, p := range paths {
			inUse[p] = true
		}
		return inUse
	}
	for _, c := range containers {
		for _, m := range c.Mounts {
			src := filepath.Clean(m.Source)
			if src == "" {
				continue
			}
			for _, p := range paths {
				p = filepath.Clean(p)
				if src == p ||
					strings.HasPrefix(p, src+string(os.PathSeparator)) ||
					strings.HasPrefix(src, p+string(os.PathSeparator)) {
					inUse[p] = true
				}
			}
		}
	}
	return inUse
}

// ImageLastUsedSnapshot returns a copy of the persisted image LRU (image ID
// → last container start), for the self-cleanup planner.
func (e *Executor) ImageLastUsedSnapshot() map[string]time.Time {
	e.imageLRUMu.Lock()
	defer e.imageLRUMu.Unlock()
	out := make(map[string]time.Time, len(e.imageLRU))
	for k, v := range e.imageLRU {
		out[k] = time.Unix(0, v)
	}
	return out
}
