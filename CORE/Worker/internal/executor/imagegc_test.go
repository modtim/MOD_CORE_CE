package executor

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func mkImg(id string, size int64, created time.Time) imageInfo {
	return imageInfo{ID: id, RepoTags: []string{id + ":latest"}, Size: size, Created: created}
}

func TestSelectLRUVictims(t *testing.T) {
	now := time.Now()

	t.Run("in-use skipped", func(t *testing.T) {
		images := []imageInfo{mkImg("a", 100, now.Add(-48*time.Hour))}
		inUse := map[string]bool{"a": true}
		got := selectLRUVictims(images, inUse, nil, nil, 100, now, 24*time.Hour)
		if len(got) != 0 {
			t.Fatalf("expected 0 victims for in-use image, got %d", len(got))
		}
	})

	t.Run("pinned glob and prefix skipped", func(t *testing.T) {
		images := []imageInfo{
			{ID: "p1", RepoTags: []string{"ghcr.io/modtim/worker:v1"}, Size: 100, Created: now.Add(-48 * time.Hour)},
			{ID: "p2", RepoTags: []string{"my-registry.local/image-foo:latest"}, Size: 100, Created: now.Add(-48 * time.Hour)},
			{ID: "p3", RepoTags: []string{"unrelated/other:latest"}, Size: 100, Created: now.Add(-48 * time.Hour)},
		}
		pinned := []string{"ghcr.io/modtim/*", "my-registry.local/image-"}
		got := selectLRUVictims(images, nil, pinned, nil, 1000, now, 24*time.Hour)
		if len(got) != 1 || got[0].ID != "p3" {
			t.Fatalf("expected only p3 as victim, got %+v", got)
		}
	})

	t.Run("recently used skipped", func(t *testing.T) {
		images := []imageInfo{mkImg("r1", 100, now.Add(-96*time.Hour))}
		lastUsed := map[string]time.Time{"r1": now.Add(-1 * time.Hour)}
		got := selectLRUVictims(images, nil, nil, lastUsed, 100, now, 24*time.Hour)
		if len(got) != 0 {
			t.Fatalf("expected recently-used image (< minAge) to be skipped, got %d victims", len(got))
		}
	})

	t.Run("oldest-first order with unsorted input", func(t *testing.T) {
		// Deliberately fed in an order that is NOT oldest-first, to catch
		// the input-order-dependent accumulation bug.
		images := []imageInfo{
			mkImg("newer", 100, now.Add(-48*time.Hour)),
			mkImg("oldest", 100, now.Add(-96*time.Hour)),
			mkImg("mid", 100, now.Add(-72*time.Hour)),
		}
		lastUsed := map[string]time.Time{
			"newer":  now.Add(-48 * time.Hour),
			"oldest": now.Add(-96 * time.Hour),
			"mid":    now.Add(-72 * time.Hour),
		}
		got := selectLRUVictims(images, nil, nil, lastUsed, 1000, now, 24*time.Hour)
		if len(got) != 3 {
			t.Fatalf("expected 3 victims, got %d", len(got))
		}
		wantOrder := []string{"oldest", "mid", "newer"}
		for i, id := range wantOrder {
			if got[i].ID != id {
				t.Fatalf("victim order[%d] = %s, want %s (full: %+v)", i, got[i].ID, id, got)
			}
		}
	})

	t.Run("stops once needBytes covered, picking oldest not first-seen", func(t *testing.T) {
		// Input order is newest, oldest, mid (i.e. NOT sorted). Only 2 of
		// 3 images (600 of 1500 bytes) are needed: the correct choice is
		// the two OLDEST (oldest, mid), not whichever came first in the
		// unsorted input (newer, oldest).
		images := []imageInfo{
			mkImg("newer", 500, now.Add(-48*time.Hour)),
			mkImg("oldest", 500, now.Add(-96*time.Hour)),
			mkImg("mid", 500, now.Add(-72*time.Hour)),
		}
		lastUsed := map[string]time.Time{
			"newer":  now.Add(-48 * time.Hour),
			"oldest": now.Add(-96 * time.Hour),
			"mid":    now.Add(-72 * time.Hour),
		}
		got := selectLRUVictims(images, nil, nil, lastUsed, 600, now, 24*time.Hour)
		if len(got) != 2 {
			t.Fatalf("expected exactly 2 victims to cover 600 bytes, got %d: %+v", len(got), got)
		}
		gotIDs := map[string]bool{got[0].ID: true, got[1].ID: true}
		if !gotIDs["oldest"] || !gotIDs["mid"] {
			t.Fatalf("expected victims {oldest, mid}, got %+v", got)
		}
	})

	t.Run("Created fallback when never touched", func(t *testing.T) {
		images := []imageInfo{mkImg("untouched", 100, now.Add(-48*time.Hour))}
		got := selectLRUVictims(images, nil, nil, nil, 100, now, 24*time.Hour)
		if len(got) != 1 || got[0].ID != "untouched" {
			t.Fatalf("expected Created-time fallback to select the image, got %+v", got)
		}
	})

	t.Run("empty input", func(t *testing.T) {
		got := selectLRUVictims(nil, nil, nil, nil, 100, now, 24*time.Hour)
		if len(got) != 0 {
			t.Fatalf("expected no victims for empty input, got %d", len(got))
		}
	})
}

func TestImageLRUStoreRoundTrip(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(stateDirEnv, dir)

	e1 := &Executor{imageLRU: make(map[string]int64), log: slog.Default()}
	e1.touchImage(context.Background(), "sha256:abc123")

	path := filepath.Join(dir, "image-last-used.json")
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("expected LRU store file at %s: %v", path, err)
	}

	e2 := &Executor{imageLRU: make(map[string]int64), log: slog.Default()}
	e2.loadImageLRU()

	ts, ok := e2.imageLRU["sha256:abc123"]
	if !ok {
		t.Fatalf("expected loaded LRU map to contain touched image, got %+v", e2.imageLRU)
	}
	if ts == 0 {
		t.Fatalf("expected non-zero timestamp for touched image")
	}
}
