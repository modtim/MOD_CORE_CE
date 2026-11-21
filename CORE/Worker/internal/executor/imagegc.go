package executor

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/image"
)

// imageLastUsed tracks the last time each image was used (when a container
// started from it). Protected by imageLRUMu, persisted to disk.
type imageLastUsed struct {
	mu   sync.RWMutex
	data map[string]time.Time
}

// imageInfo holds metadata about a docker image for GC decisions.
type imageInfo struct {
	ID       string
	RepoTags []string
	Size     int64
	Created  time.Time
}

const (
	defaultImageGCMinAgeHours = 24
	stateDirEnv               = "MOD_WORKER_STATE_DIR"
)

// touchImage records last-use for the image identified by ref (a name or
// tag, e.g. as used in ContainerCreate). imageLRU is keyed by image ID (see
// the field doc on Executor.imageLRU), so ref is resolved via ImageInspect
// before recording — storing raw refs here would silently desync the map
// from the ID-keyed lookups in selectLRUVictims/lruImageGC, making every
// touch a no-op for GC ordering (previously the case for this function).
func (e *Executor) touchImage(ctx context.Context, ref string) {
	id := ref
	if e.docker != nil {
		if info, _, err := e.docker.ImageInspectWithRaw(ctx, ref); err == nil && info.ID != "" {
			id = info.ID
		}
	}
	e.imageLRUMu.Lock()
	defer e.imageLRUMu.Unlock()
	if e.imageLRU == nil {
		// Early call before executor init; ignore
		return
	}
	e.imageLRU[id] = time.Now().UnixNano()
	e.saveImageLRU()
}

// saveImageLRU persists the LRU map to disk. Best-effort; errors are logged.
func (e *Executor) saveImageLRU() {
	if e.imageLRU == nil {
		return
	}
	// Convert to JSON-serializable map
	data := make(map[string]int64)
	for k, v := range e.imageLRU {
		data[k] = v
	}
	jsonData, err := json.Marshal(data)
	if err != nil {
		e.log.Warn("image gc: failed to marshal LRU data", "err", err)
		return
	}
	dir := os.Getenv(stateDirEnv)
	if dir == "" {
		if home, err := os.UserCacheDir(); err == nil {
			dir = filepath.Join(home, "mod-worker")
		} else {
			e.log.Warn("image gc: cannot determine cache dir, skipping persistence", "err", err)
			return
		}
	}
	if err := os.MkdirAll(dir, 0755); err != nil {
		e.log.Warn("image gc: cannot create state dir", "dir", dir, "err", err)
		return
	}
	path := filepath.Join(dir, "image-last-used.json")
	if err := os.WriteFile(path, jsonData, 0644); err != nil {
		e.log.Warn("image gc: failed to write LRU file", "path", path, "err", err)
	}
}

// loadImageLRU loads the LRU map from disk. Best-effort; returns empty map on error.
func (e *Executor) loadImageLRU() {
	e.imageLRUMu.Lock()
	defer e.imageLRUMu.Unlock()
	if e.imageLRU == nil {
		e.imageLRU = make(map[string]int64)
		return
	}
	dir := os.Getenv(stateDirEnv)
	if dir == "" {
		if home, err := os.UserCacheDir(); err == nil {
			dir = filepath.Join(home, "mod-worker")
		}
	}
	if dir == "" {
		return
	}
	path := filepath.Join(dir, "image-last-used.json")
	data, err := os.ReadFile(path)
	if err != nil {
		// File may not exist yet; this is fine
		return
	}
	var loaded map[string]int64
	if err := json.Unmarshal(data, &loaded); err != nil {
		e.log.Warn("image gc: failed to unmarshal LRU data", "err", err)
		return
	}
	// Convert loaded timestamps to time.Time
	for k, v := range loaded {
		e.imageLRU[k] = v
	}
}

// selectLRUVictims returns the images to prune to reclaim needBytes.
// Excludes images in inUse, images matching pinned patterns, and images
// used within minAge (default 24h).
//
// Sorts by last used (oldest first), then by created time as fallback.
// Returns images to remove, in pruning order.
func selectLRUVictims(
	images []imageInfo,
	inUse map[string]bool,
	pinned []string,
	lastUsed map[string]time.Time,
	needBytes int64,
	now time.Time,
	minAge time.Duration,
) []imageInfo {
	if minAge == 0 {
		minAge = 24 * time.Hour
	}

	eligible := make([]imageInfo, 0, len(images))
	for _, img := range images {
		// Skip images in use by any container (running or stopped)
		if inUse[img.ID] {
			continue
		}

		// Skip images matching any pinned pattern
		if isPinned(img.RepoTags, pinned) {
			continue
		}

		// Skip images used within minAge
		lastUse := lastUsed[img.ID]
		if lastUse.IsZero() {
			// Fallback to creation time if never touched
			lastUse = img.Created
		}
		if now.Sub(lastUse) < minAge {
			continue
		}

		eligible = append(eligible, img)
	}

	// Sort oldest-first BEFORE accumulating. Docker's ImageList order is
	// unspecified, so accumulating in input order and sorting only the
	// already-chosen subset (the previous approach) could stop after
	// picking newer images ahead of older ones that appeared later in the
	// list — silently defeating "oldest first" whenever the input wasn't
	// already sorted.
	sortByLastUsed(eligible, lastUsed)

	victims := make([]imageInfo, 0, len(eligible))
	bytesReclaimed := int64(0)
	for _, img := range eligible {
		victims = append(victims, img)
		bytesReclaimed += img.Size

		if bytesReclaimed >= needBytes {
			break
		}
	}

	return victims
}

// sortByLastUsed sorts images by last used time (oldest first), falling back
// to created time if never touched.
func sortByLastUsed(images []imageInfo, lastUsed map[string]time.Time) {
	sort.Slice(images, func(i, j int) bool {
		lui := lastUsed[images[i].ID]
		if lui.IsZero() {
			lui = images[i].Created
		}
		luj := lastUsed[images[j].ID]
		if luj.IsZero() {
			luj = images[j].Created
		}
		return lui.Before(luj)
	})
}

// isPinned checks if any image tag matches any pinned pattern.
// Patterns can be globs (e.g., "ghcr.io/modtim/*") or
// simple prefixes (e.g., "my-registry.local/image-").
func isPinned(tags []string, pinned []string) bool {
	for _, tag := range tags {
		for _, pat := range pinned {
			// Try glob pattern match first
			if match, _ := globMatch(pat, tag); match {
				return true
			}
			// Fall back to prefix match
			if len(tag) >= len(pat) && tag[:len(pat)] == pat {
				return true
			}
		}
	}
	return false
}

// globMatch is a simple glob pattern matcher for common patterns.
// Supports * for any characters and ? for single character.
func globMatch(pattern, text string) (bool, error) {
	// Use filepath.Match which handles * and ? patterns
	return filepath.Match(pattern, text)
}

// lruImageGC performs LRU-based image garbage collection to reclaim needBytes.
// Returns the number of bytes reclaimed.
func (e *Executor) lruImageGC(ctx context.Context, needBytes int64) uint64 {
	e.log.Info("image gc: starting LRU-based garbage collection", "need_bytes", needBytes)

	// Load persisted LRU data
	e.loadImageLRU()

	// Get current time and min age from env
	now := time.Now()
	minAge := 24 * time.Hour
	if raw := os.Getenv("MOD_WORKER_IMAGE_GC_MIN_AGE_HOURS"); raw != "" {
		if v, err := time.ParseDuration(raw + "h"); err == nil {
			minAge = v
		}
	}

	// Get all containers (including stopped) to find in-use images
	containers, err := e.docker.ContainerList(ctx, container.ListOptions{All: true})
	if err != nil {
		e.log.Warn("image gc: failed to list containers", "err", err)
		return 0
	}

	// Build set of in-use image IDs
	inUse := make(map[string]bool)
	for _, c := range containers {
		if c.ImageID != "" {
			inUse[c.ImageID] = true
		}
	}

	// Get all images
	images, err := e.docker.ImageList(ctx, image.ListOptions{})
	if err != nil {
		e.log.Warn("image gc: failed to list images", "err", err)
		return 0
	}

	// Build image info list
	imageInfos := make([]imageInfo, 0, len(images))
	for _, img := range images {
		repoTags := make([]string, 0, len(img.RepoTags))
		for _, tag := range img.RepoTags {
			if tag != "<none>:<none>" && tag != "<none>" {
				repoTags = append(repoTags, tag)
			}
		}
		// Use image ID as key
		id := img.ID
		if id == "" {
			continue
		}
		imageInfos = append(imageInfos, imageInfo{
			ID:       id,
			RepoTags: repoTags,
			Size:     img.Size,
			Created:  time.Unix(img.Created, 0),
		})
	}

	// Get last used times from in-memory LRU
	e.imageLRUMu.RLock()
	lastUsed := make(map[string]time.Time)
	for k, v := range e.imageLRU {
		lastUsed[k] = time.Unix(0, v)
	}
	e.imageLRUMu.RUnlock()

	// Select victims
	victims := selectLRUVictims(
		imageInfos,
		inUse,
		e.pinnedImages,
		lastUsed,
		needBytes,
		now,
		minAge,
	)

	// Remove victims
	var totalReclaimed uint64
	for _, v := range victims {
		e.log.Info("image gc: removing image",
			"id", v.ID,
			"tags", v.RepoTags,
			"size_mb", v.Size/(1024*1024),
			"last_used", lastUsed[v.ID].Format(time.RFC3339))

		// Try to remove with pruning children (this is what docker rmi --force does)
		_, err := e.docker.ImageRemove(ctx, v.ID, image.RemoveOptions{Force: false})
		if err != nil {
			e.log.Warn("image gc: failed to remove image",
				"id", v.ID,
				"err", err)
			continue
		}
		totalReclaimed += uint64(v.Size)
	}

	e.log.Info("image gc: garbage collection complete",
		"victims", len(victims),
		"reclaimed_mb", totalReclaimed/(1024*1024))

	return totalReclaimed
}
