package monitor

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/docker/docker/api/types"
)

// DiskUsage captures disk space usage for various MOD-related directories.
type DiskUsage struct {
	ImagesBytes      int64
	ModelCacheBytes  int64
	WorkdirsBytes    int64
	ReclaimableBytes int64
	At               time.Time
}

var (
	diskUsageMu sync.RWMutex
	diskUsage   DiskUsage
	diskUsageOk bool
)

// SetDiskUsage updates the cached disk usage snapshot.
func SetDiskUsage(du DiskUsage) {
	diskUsageMu.Lock()
	defer diskUsageMu.Unlock()
	diskUsage = du
	diskUsageOk = true
}

// LatestDiskUsage returns the most recent disk usage snapshot and whether it's available.
func LatestDiskUsage() (DiskUsage, bool) {
	diskUsageMu.RLock()
	defer diskUsageMu.RUnlock()
	return diskUsage, diskUsageOk
}

// reclaimedBytes is a cumulative, since-process-start counter of bytes
// actually freed by disk cleanup (image GC + model-cache GC — see
// executor/imagegc.go and executor/modelcache_gc.go). Reported next to
// reclaimable_bytes in the heartbeat so "reported as reclaimable" can be
// checked against "was actually reclaimed" (megaplan 25.5/Q.3) instead of
// trusting that a reclaim attempt fired at all. A plain atomic counter, not
// folded into the DiskUsage snapshot struct, because CollectDiskUsage
// recomputes that struct wholesale on every tick and would otherwise reset
// this to zero each time SetDiskUsage runs.
var reclaimedBytes atomic.Uint64

// AddReclaimedBytes adds n to the cumulative reclaimed-bytes counter.
func AddReclaimedBytes(n uint64) {
	reclaimedBytes.Add(n)
}

// TotalReclaimedBytes returns the cumulative reclaimed-bytes counter.
func TotalReclaimedBytes() uint64 {
	return reclaimedBytes.Load()
}

// ImageDiskUsager is the interface that Docker client satisfies for disk usage.
type ImageDiskUsager interface {
	DiskUsage(ctx context.Context, opts types.DiskUsageOptions) (types.DiskUsage, error)
}

// CollectDiskUsage gathers disk usage statistics.
// du may be nil → image fields are 0.
func CollectDiskUsage(ctx context.Context, du ImageDiskUsager) DiskUsage {
	result := DiskUsage{At: time.Now()}

	// Images bytes: sum Size of all images (count SharedSize once)
	if du != nil {
		duResp, err := du.DiskUsage(ctx, types.DiskUsageOptions{})
		if err == nil {
			// Use LayersSize if available (total unique layers), else sum individual image sizes
			if duResp.LayersSize > 0 {
				result.ImagesBytes = int64(duResp.LayersSize)
			} else {
				for _, img := range duResp.Images {
					result.ImagesBytes += int64(img.Size)
				}
			}
			// Reclaimable: images with no containers + build cache not in use
			for _, img := range duResp.Images {
				if img.Containers == 0 {
					result.ReclaimableBytes += int64(img.Size)
				}
			}
			for _, bc := range duResp.BuildCache {
				if !bc.InUse {
					result.ReclaimableBytes += int64(bc.Size)
				}
			}
		}
	}

	// Model cache: directory size of HF cache path
	// Default matches executor.hfCachePath
	hfCachePath := "/var/lib/mod/hf-cache"
	if env := os.Getenv("MANAGED_AI_HF_CACHE_PATH"); env != "" {
		hfCachePath = env
	}
	result.ModelCacheBytes = dirSize(hfCachePath)

	// Workdirs: sum of all modstack-* temp directories
	result.WorkdirsBytes = dirSize(filepath.Join(os.TempDir(), "modstack-*"))

	return result
}

// dirSize walks a directory tree and sums file sizes.
// Globs are supported (filepath.Glob pattern).
// Errors are ignored (files may be removed during walk).
func dirSize(pattern string) int64 {
	total := int64(0)
	matches, _ := filepath.Glob(pattern)
	for _, dir := range matches {
		filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return nil // Ignore errors, continue walking
			}
			if d.Type().IsRegular() {
				info, _ := d.Info()
				total += info.Size()
			}
			return nil
		})
	}
	return total
}

// StartDiskUsageLoop runs disk usage collection in a background goroutine.
// It collects immediately on start, then at the specified interval.
// If MOD_WORKER_DISK_USAGE_DISABLED=true, it skips collection entirely.
func StartDiskUsageLoop(ctx context.Context, du ImageDiskUsager, every time.Duration) {
	if os.Getenv("MOD_WORKER_DISK_USAGE_DISABLED") == "true" {
		return
	}

	// Collect immediately on start
	go func() {
		result := CollectDiskUsage(ctx, du)
		SetDiskUsage(result)

		ticker := time.NewTicker(every)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				result := CollectDiskUsage(ctx, du)
				SetDiskUsage(result)
			}
		}
	}()
}
