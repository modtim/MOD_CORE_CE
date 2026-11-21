package monitor

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/image"
)

func TestDirSize(t *testing.T) {
	// Create a temp directory tree
	tmpDir, err := os.MkdirTemp("", "dirsize-test")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	// Create files with known sizes
	files := map[string]int64{
		"file1.txt": 1024,
		"file2.txt": 2048,
	}
	for name, size := range files {
		path := filepath.Join(tmpDir, name)
		if err := os.WriteFile(path, make([]byte, size), 0644); err != nil {
			t.Fatalf("failed to write file %s: %v", name, err)
		}
	}

	// Test dirSize on the exact path
	result := dirSize(tmpDir)
	expected := int64(1024 + 2048)
	if result != expected {
		t.Errorf("dirSize(%s) = %d, want %d", tmpDir, result, expected)
	}
}

func TestDirSizeWithGlob(t *testing.T) {
	// Create temp directories matching a pattern
	tmpBase, err := os.MkdirTemp("", "dirsize-glob-*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpBase)

	// Create a few subdirectories with files
	for i := 0; i < 3; i++ {
		subdir := filepath.Join(tmpBase, "test", "dir"+string(rune('a'+i)))
		if err := os.MkdirAll(subdir, 0755); err != nil {
			t.Fatalf("failed to create subdir: %v", err)
		}
		path := filepath.Join(subdir, "data.txt")
		if err := os.WriteFile(path, make([]byte, 100), 0644); err != nil {
			t.Fatalf("failed to write file: %v", err)
		}
	}

	// Use glob pattern to match test/*
	result := dirSize(filepath.Join(tmpBase, "test", "*"))
	expected := int64(300) // 3 dirs * 100 bytes
	if result != expected {
		t.Errorf("dirSize(glob) = %d, want %d", result, expected)
	}
}

type fakeDiskUsage struct {
	images   []*image.Summary
	buildC   []*types.BuildCache
	layersSz int64
}

func (f *fakeDiskUsage) DiskUsage(ctx context.Context, opts types.DiskUsageOptions) (types.DiskUsage, error) {
	return types.DiskUsage{
		LayersSize: f.layersSz,
		Images:     f.images,
		BuildCache: f.buildC,
	}, nil
}

func TestCollectDiskUsage(t *testing.T) {
	ctx := context.Background()

	// Test with nil du (no docker client)
	result := CollectDiskUsage(ctx, nil)
	if result.ImagesBytes != 0 || result.ReclaimableBytes != 0 {
		t.Errorf("with nil du, images_bytes=%d, reclaimable_bytes=%d, want 0", result.ImagesBytes, result.ReclaimableBytes)
	}

	// Test with fake du
	fake := &fakeDiskUsage{
		layersSz: 1000000,
		images: []*image.Summary{
			{Size: 100000, Containers: 1},
			{Size: 50000, Containers: 0},
		},
		buildC: []*types.BuildCache{
			{Size: 20000, InUse: true},
			{Size: 30000, InUse: false},
		},
	}

	result = CollectDiskUsage(ctx, fake)

	// ImagesBytes should equal LayersSize when non-zero
	if result.ImagesBytes != 1000000 {
		t.Errorf("ImagesBytes = %d, want 1000000 (LayersSize)", result.ImagesBytes)
	}

	// Reclaimable = image with 0 containers + build cache not in use
	expectedReclaimable := int64(50000 + 30000) // 50000 + 30000
	if result.ReclaimableBytes != expectedReclaimable {
		t.Errorf("ReclaimableBytes = %d, want %d", result.ReclaimableBytes, expectedReclaimable)
	}
}

func TestFormatResourceSnapshotDiskUsage(t *testing.T) {
	diskUsageMu.Lock()
	diskUsageOk = false
	diskUsageMu.Unlock()

	sys := SystemSnapshot{DiskFreeGB: 100, DiskTotalGB: 500}
	gpu := GPUSnapshot{Type: "none"}

	// Before SetDiskUsage, fields should be omitted
	snap := FormatResourceSnapshot(sys, gpu, nil, 1, 0)
	if _, ok := snap["images_bytes"]; ok {
		t.Error("images_bytes present before SetDiskUsage, should be omitted")
	}

	// After SetDiskUsage, fields should be included
	SetDiskUsage(DiskUsage{
		ImagesBytes:      1000,
		ModelCacheBytes:  2000,
		WorkdirsBytes:    3000,
		ReclaimableBytes: 4000,
	})

	snap = FormatResourceSnapshot(sys, gpu, nil, 1, 0)
	if images, ok := snap["images_bytes"]; !ok || images != int64(1000) {
		t.Errorf("images_bytes = %v, ok=%v, want 1000", images, ok)
	}
	if cache, ok := snap["model_cache_bytes"]; !ok || cache != int64(2000) {
		t.Errorf("model_cache_bytes = %v, ok=%v, want 2000", cache, ok)
	}
	if dirs, ok := snap["workdirs_bytes"]; !ok || dirs != int64(3000) {
		t.Errorf("workdirs_bytes = %v, ok=%v, want 3000", dirs, ok)
	}
	if reclaim, ok := snap["reclaimable_bytes"]; !ok || reclaim != int64(4000) {
		t.Errorf("reclaimable_bytes = %v, ok=%v, want 4000", reclaim, ok)
	}
}
