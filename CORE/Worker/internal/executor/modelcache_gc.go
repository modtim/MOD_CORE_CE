package executor

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"gopkg.in/yaml.v3"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/monitor"
)

// Model-cache LRU eviction under a free-space guard (megaplan 25.5 / Q.3).
//
// The shared HF-style model cache (hfCachePath()/hub, "models--org--name"
// directories) grows unbounded: every managed-AI stack rung that has ever
// been served leaves its weights behind. imagegc.go already reclaims image
// layers; this file does the same for model weights, which are typically far
// larger per unit and the more valuable thing to reclaim first is NOT what
// applies here — models are evicted only once free space is already tight,
// deliberately after image GC (see diskcleanup.go), because a re-download is
// far more expensive (minutes, gigabytes over the network) than a re-pull of
// a cached image layer.
//
// Interlock with the platform model mirror (mod-model-cache, GF.C0b.9).
// When the platform setting model_cache_enabled is on, every managed-AI
// stack's HF clients are pointed at the LAN mirror `models.<BASE_DOMAIN>`
// (API: managed_ai_stacks.model_cache_endpoint, delivered to this worker as
// the worker.model_mirror_endpoint tunable — see modelMirrorEndpoint below).
// That mirror runs its OWN LRU GC (mod-model-cache/gc.py), so two GCs now
// touch the same set of weights, and a naive per-box LRU would make the
// wrong choice: it evicts what it happens to have used least, not what is
// cheapest to get back. The fix is per-object SOURCE, recorded in a small
// sidecar file (modelSourceMarker) inside each cache directory at fetch
// time:
//
//   - "mirror" — the weights arrived (or are re-fetchable) through the
//     platform's own cache path: either the worker materialized them from
//     the platform's pinned-digest weight registry (prepareStackWeights in
//     stack_weights.go), or a stack that resolves HF through the mirror
//     shares this cache (touchModelCacheForStack in stackrunner.go marks
//     the shared cache, since a mirror-routed re-fetch of ANY repo on this
//     box goes over the LAN, and any WAN cost lands on the mirror's box,
//     not this one).
//   - absent/"wan" — fetched straight from the upstream hub; re-fetching
//     after eviction is a cold WAN download of possibly gigabytes.
//
// Eviction order is therefore: mirror-sourced first, WAN-sourced later,
// oldest-used first WITHIN each class (selectModelVictims). In-use
// protection (inUseModelDirs, built from the live ContainerList exactly as
// for images) and the recent-use guard are unchanged and apply to BOTH
// classes — a mirror-sourced weight is never evicted while a container
// still mounts it, because the cheap re-fetch is useless to a live server.
//
// The two GCs are thus non-contradictory by construction: whichever copy
// evicts first, the other side's re-fetch of that object is a cheap,
// digest-pinned platform pull — the worst case is an extra LAN transfer,
// never a cold WAN download triggered by a sibling GC's decision. A box
// whose stacks never route through the mirror carries no "mirror" markers
// and behaves exactly as before this landed.
const (
	defaultModelCacheMinFreeGB = 20.0
	modelCacheHeadroomGB       = 5.0
	modelRecentUseAge          = 1 * time.Hour
	modelTouchMarker           = ".mod-last-used"

	// modelSourceMarker is the per-directory sidecar recording where an
	// object's bytes are cheap to re-fetch from. The HF cache layout itself
	// (blobs/ + snapshots/, written by llama.cpp / huggingface_hub inside
	// stack containers) carries no source metadata, hence the sidecar.
	modelSourceMarker = ".mod-source"
	// modelSourceMirror marks "re-fetchable cheaply via the platform's own
	// cache path (LAN mirror / pinned-digest platform registry)".
	modelSourceMirror = "mirror"
	// modelSourceWAN is the implicit class: no marker, or an unrecognised
	// marker value, means upstream-hub-sourced. Conservative on purpose —
	// a stale or tampered marker degrades to "treat as expensive", which
	// only delays an eviction, never causes a wrong one.
	modelSourceWAN = "wan"
)

// modelMirrorEndpointTunableKey is the platform_settings registry key the
// API pushes (GS.PSR.7) when the platform model mirror is live — the
// worker.model_mirror_endpoint entry of the heartbeat `tunables` map,
// populated by the API from managed_ai_stacks.model_cache_endpoint().
const modelMirrorEndpointTunableKey = "worker.model_mirror_endpoint"

// modelCacheFreeGB reports free space (in GB) for the filesystem backing the
// model cache. A package var so tests can inject a deterministic sequence
// instead of statting the real disk (see modelcache_gc_test.go).
var modelCacheFreeGB = defaultModelCacheFreeGBFn

func defaultModelCacheFreeGBFn() (float64, bool) {
	_, available, ok := getDiskSpace(hfCachePath())
	if !ok {
		return 0, false
	}
	return float64(available) / (1024 * 1024 * 1024), true
}

// modelCacheHubRoot is the one directory whose immediate children are
// eviction units — matches ScanCachedModels' enumeration in modeldownload.go.
func modelCacheHubRoot() string {
	return filepath.Join(hfCachePath(), "hub")
}

// modelMirrorEndpoint reports the platform model-mirror HF endpoint the
// given stack's services resolve through, or "" when the mirror is not in
// play for it. Precedence: the server-pushed worker.model_mirror_endpoint
// tunable (GS.PSR.7 — the API populates it from
// managed_ai_stacks.model_cache_endpoint() exactly while
// model_cache_enabled is on) > the HF_ENDPOINT the API already wrote into
// the stack's compose env via _apply_model_cache_endpoint (the fallback for
// a manager that predates the tunable). Read fresh on every stack deploy,
// never cached — a Dashboard toggle of model_cache_enabled must change the
// source stamping within two heartbeats without a worker restart.
func modelMirrorEndpoint(e *Executor, composeYAML string) string {
	if e != nil && e.tunables != nil {
		if v := e.tunables.String(modelMirrorEndpointTunableKey, "", ""); v != "" {
			return v
		}
	}
	return composeEnvValue(composeYAML, "HF_ENDPOINT")
}

// composeEnvValue returns the first value of env key across all services'
// `environment` blocks (mapping or "K=V" list form), or "" when absent or
// the spec is not a services mapping. Read-only; never mutates the doc.
func composeEnvValue(composeYAML string, key string) string {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(composeYAML), &root); err != nil {
		return ""
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return ""
	}
	svcs := mapGet(root.Content[0], "services")
	if svcs == nil || svcs.Kind != yaml.MappingNode {
		return ""
	}
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		svc := svcs.Content[i+1]
		if svc.Kind != yaml.MappingNode {
			continue
		}
		envNode := mapGet(svc, "environment")
		if envNode == nil {
			continue
		}
		switch envNode.Kind {
		case yaml.MappingNode:
			for j := 0; j+1 < len(envNode.Content); j += 2 {
				if envNode.Content[j].Value == key {
					return strings.TrimSpace(envNode.Content[j+1].Value)
				}
			}
		case yaml.SequenceNode:
			for _, ent := range envNode.Content {
				if ent.Kind != yaml.ScalarNode {
					continue
				}
				if k, v, ok := cutEnvKV(ent.Value); ok && k == key {
					return strings.TrimSpace(v)
				}
			}
		}
	}
	return ""
}

// markModelSource records where the weights under path are cheap to
// re-fetch from (see modelSourceMarker). Best-effort like touchModel:
// marker I/O must never fail the caller's fetch, and a missing marker
// simply means "treat as WAN-sourced" at eviction time.
func markModelSource(path, source string) {
	_ = os.MkdirAll(path, 0o755)
	_ = os.WriteFile(filepath.Join(path, modelSourceMarker), []byte(source+"\n"), 0o644)
}

// modelSourceOf reads path's source marker. Returns modelSourceWAN for an
// absent or unrecognised marker (the conservative default, see the marker
// constant above).
func modelSourceOf(path string) string {
	data, err := os.ReadFile(filepath.Join(path, modelSourceMarker))
	if err != nil {
		return modelSourceWAN
	}
	switch strings.TrimSpace(string(data)) {
	case modelSourceMirror:
		return modelSourceMirror
	default:
		return modelSourceWAN
	}
}

// modelDirInfo is one evictable unit: a "models--org--name" directory.
type modelDirInfo struct {
	Path     string // absolute, cleaned
	Size     int64
	LastUsed time.Time
	Source   string // modelSourceMirror or modelSourceWAN (see modelSourceOf)
}

// touchModel records that path was just used by writing/updating a marker
// file inside it. Called wherever a task or stack resolves/mounts a model
// (see touchModelCacheForStack in stackrunner.go). Best-effort: a failure
// here must never fail the caller's task.
func touchModel(path string) {
	marker := filepath.Join(path, modelTouchMarker)
	now := time.Now()
	if err := os.Chtimes(marker, now, now); err == nil {
		return
	}
	f, err := os.OpenFile(marker, os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	_ = f.Close()
}

// listModelDirs enumerates eviction units under root. LastUsed prefers the
// touchModel marker file; falls back to the directory's own mtime when no
// marker exists (never touched, or touched before this feature shipped).
// Source is read from the .mod-source sidecar (see modelSourceOf).
func listModelDirs(root string) ([]modelDirInfo, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var out []modelDirInfo
	for _, e := range entries {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), "models--") {
			continue
		}
		path := filepath.Clean(filepath.Join(root, e.Name()))
		info, err := e.Info()
		if err != nil {
			continue
		}
		lastUsed := info.ModTime()
		if mi, err := os.Stat(filepath.Join(path, modelTouchMarker)); err == nil {
			lastUsed = mi.ModTime()
		}
		out = append(out, modelDirInfo{
			Path:     path,
			Size:     dirSizeBytes(path),
			LastUsed: lastUsed,
			Source:   modelSourceOf(path),
		})
	}
	return out, nil
}

// dirSizeBytes sums regular-file sizes under path. Errors mid-walk are
// ignored (a file may be removed concurrently); best-effort like the
// equivalent helper in internal/monitor/diskusage.go.
func dirSizeBytes(path string) int64 {
	var total int64
	_ = filepath.WalkDir(path, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return nil //nolint:nilerr
		}
		if d.Type().IsRegular() {
			if info, err := d.Info(); err == nil {
				total += info.Size()
			}
		}
		return nil
	})
	return total
}

// safeWithinRoot refuses to operate on anything that resolves (after
// symlink evaluation) outside root — a symlink escape must never turn a
// cache-cleanup into an arbitrary-path delete.
func safeWithinRoot(root, path string) (string, error) {
	cleanRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", err
	}
	real = filepath.Clean(real)
	cleanRoot = filepath.Clean(cleanRoot)
	if real != cleanRoot && !strings.HasPrefix(real, cleanRoot+string(os.PathSeparator)) {
		return "", os.ErrPermission
	}
	return real, nil
}

// selectModelVictims returns dirs eligible for eviction — not in use, not
// used within modelRecentUseAge — ordered for the mirror interlock:
// mirror-sourced objects first, then WAN-sourced, oldest-used first WITHIN
// each class (see the file header for why the class beats raw LRU). The
// in-use and recent-use guards apply to BOTH classes — a mirror-sourced
// weight is never evicted while a container still mounts it. Pure (no
// docker, no filesystem) so the ordering and guards are unit-testable
// without a real cache directory or daemon.
func selectModelVictims(dirs []modelDirInfo, inUse map[string]bool, now time.Time) []modelDirInfo {
	eligible := make([]modelDirInfo, 0, len(dirs))
	for _, d := range dirs {
		if inUse[d.Path] {
			continue
		}
		if now.Sub(d.LastUsed) < modelRecentUseAge {
			continue
		}
		eligible = append(eligible, d)
	}
	sort.SliceStable(eligible, func(i, j int) bool {
		if (eligible[i].Source == modelSourceMirror) != (eligible[j].Source == modelSourceMirror) {
			return eligible[i].Source == modelSourceMirror
		}
		return eligible[i].LastUsed.Before(eligible[j].LastUsed)
	})
	return eligible
}

// inUseModelDirs marks every model dir a currently-running container has
// mounted — directly (a narrow bind of that one model dir) or indirectly (a
// bind of the shared cache root or hub itself, which is how stacks normally
// mount it — see modelCacheRoots in stackrunner.go). This is the same
// "ask docker, don't trust a manual ref-count" approach imagegc.go uses for
// images (inUse built from ContainerList), so a task ending (container gone)
// self-heals the protection with no separate decrement path to forget.
func (e *Executor) inUseModelDirs(ctx context.Context, dirs []modelDirInfo) (map[string]bool, error) {
	inUse := make(map[string]bool, len(dirs))
	if e.docker == nil {
		return inUse, nil
	}
	containers, err := e.docker.ContainerList(ctx, container.ListOptions{})
	if err != nil {
		return nil, err
	}
	for _, c := range containers {
		for _, m := range c.Mounts {
			src := filepath.Clean(m.Source)
			if src == "" {
				continue
			}
			for _, d := range dirs {
				if src == d.Path ||
					strings.HasPrefix(d.Path, src+string(os.PathSeparator)) ||
					strings.HasPrefix(src, d.Path+string(os.PathSeparator)) {
					inUse[d.Path] = true
				}
			}
		}
	}
	return inUse, nil
}

// removeModelDir deletes one model directory after re-verifying it resolves
// inside root (defence against a symlink planted/changed between listing and
// deletion).
func (e *Executor) removeModelDir(root string, d modelDirInfo) error {
	real, err := safeWithinRoot(root, d.Path)
	if err != nil {
		return err
	}
	return os.RemoveAll(real)
}

// envFloatOrDefault reads a float env var, falling back to def on absence or
// parse failure. Values <= 0 also fall back — a non-positive free-space
// floor is nonsensical and almost certainly a misconfigured env.
func envFloatOrDefault(name string, def float64) float64 {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return def
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil || v <= 0 {
		return def
	}
	return v
}

// evictModelCache deletes least-recently-used model directories while free
// space is under minFreeGB (<=0 reads MOD_WORKER_MIN_FREE_GB, default 20),
// stopping once free space reaches minFreeGB+modelCacheHeadroomGB or nothing
// is left to evict. Never evicts an in-use, recently-used (<1h), or
// symlink-escaping directory. Returns bytes reclaimed.
func (e *Executor) evictModelCache(ctx context.Context, minFreeGB float64) uint64 {
	if minFreeGB <= 0 {
		minFreeGB = envFloatOrDefault("MOD_WORKER_MIN_FREE_GB", defaultModelCacheMinFreeGB)
	}
	free, ok := modelCacheFreeGB()
	if !ok {
		e.log.Debug("model cache gc: cannot determine free space — skipping")
		return 0
	}
	if free >= minFreeGB {
		return 0
	}

	root := modelCacheHubRoot()
	dirs, err := listModelDirs(root)
	if err != nil || len(dirs) == 0 {
		return 0
	}

	inUse, err := e.inUseModelDirs(ctx, dirs)
	if err != nil {
		e.log.Warn("model cache gc: could not enumerate in-use models, refusing to evict", "err", err)
		return 0
	}

	eligible := selectModelVictims(dirs, inUse, time.Now())

	target := minFreeGB + modelCacheHeadroomGB
	var reclaimed uint64
	for _, d := range eligible {
		if f, ok := modelCacheFreeGB(); ok && f >= target {
			break
		}
		if err := e.removeModelDir(root, d); err != nil {
			e.log.Warn("model cache gc: failed to remove model dir", "path", d.Path, "err", err)
			continue
		}
		reclaimed += uint64(d.Size)
		monitor.AddReclaimedBytes(uint64(d.Size))
		e.log.Info("model cache gc: evicted model",
			"path", d.Path, "size_mb", d.Size/(1024*1024), "source", d.Source,
			"last_used", d.LastUsed.Format(time.RFC3339))
	}
	if reclaimed == 0 {
		e.log.Warn("model cache gc: free space low but nothing evictable",
			"free_gb", free, "min_free_gb", minFreeGB, "candidates", len(dirs))
	}
	return reclaimed
}

// touchModelCacheForStack updates the last-used marker on every cached model
// when the given compose spec bind-mounts the shared model cache (or an
// ancestor/descendant of it) — see modelCacheRoots in stackrunner.go. Models
// are shared across stacks/rungs, and a compose spec names a host directory,
// not a specific repo, so there is no way to know which one model this
// service will load without parsing container-level config; touching all of
// them is the conservative choice — it can only delay an eviction, never
// cause a wrongful one.
//
// Mirror interlock (GF.C0b.9): while this box's stacks resolve HF through
// the platform mirror (modelMirrorEndpoint != ""), every model in that
// shared cache is ALSO stamped mirror-sourced. The stamp is deliberately
// coarse — the same "touch all" conservatism as the last-used marker: when
// the mirror is on, EVERY managed stack on this box has its HF clients
// pointed at it (managed_ai_stacks._apply_model_cache_endpoint), so any
// repo a stack may (re)fetch costs this box a LAN pull, not a WAN download,
// and the GC may evict any of them first. Weights the worker materialized
// itself (stack_weights.go, already stamped mirror at fetch time) are
// re-stamped harmlessly. A "mirror" stamp outlives the mirror being turned
// off — a stale stamp only skews the eviction ORDER (cheap class first),
// never the in-use/recent-use guards, and is re-confirmed on the next stack
// deploy while the mirror is on again.
func (e *Executor) touchModelCacheForStack(composeYAML string) {
	roots := modelCacheRoots(composeYAML)
	if len(roots) == 0 {
		return
	}
	hub := modelCacheHubRoot()
	shared := false
	for _, r := range roots {
		rc := filepath.Clean(r)
		if rc == hub || strings.HasPrefix(hub, rc+string(os.PathSeparator)) || strings.HasPrefix(rc, hub+string(os.PathSeparator)) {
			shared = true
			break
		}
	}
	if !shared {
		return
	}
	mirror := modelMirrorEndpoint(e, composeYAML) != ""
	dirs, err := listModelDirs(hub)
	if err != nil {
		return
	}
	for _, d := range dirs {
		touchModel(d.Path)
		if mirror && d.Source != modelSourceMirror {
			markModelSource(d.Path, modelSourceMirror)
		}
	}
}
