package executor

// OPT3 (container-start phase) — the image-LRU persistence failure must be
// latched, not retried per task.
//
// Root cause this pins: touchImage runs once per task right before
// ContainerCreate (executor.go, 'network policy' -> create window), and it
// calls saveImageLRU, which mkdirs the state dir and rewrites the whole map.
// On the embedded worker the default state dir is NOT writable
// (/var/lib/mod-worker/.cache: permission denied), so EVERY task paid a
// failing mkdir and logged a WARN — the 'image gc: cannot create state dir'
// line seen on every xeon task. The in-memory LRU still updates, so GC
// ordering never depended on the write; only the noise and the syscall did.
//
// ENOTDIR (a regular file as the parent) fails for root as well, so the test
// is deterministic inside the golang container.

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
)

func unwritableStateDir(t *testing.T) string {
	root := t.TempDir()
	blocker := filepath.Join(root, "blocker")
	if err := os.WriteFile(blocker, []byte("not a dir"), 0644); err != nil {
		t.Fatalf("write blocker: %v", err)
	}
	return filepath.Join(blocker, "mod-worker") // mkdir: ENOTDIR
}

func TestImageLRUPersistFailureLatches(t *testing.T) {
	dir := unwritableStateDir(t)
	t.Setenv(stateDirEnv, dir)

	e := &Executor{imageLRU: make(map[string]int64), log: slog.Default()}
	e.touchImage(context.Background(), "sha256:aaa111")
	if !e.imageLRUPersistBroken {
		t.Fatal("first failing mkdir must latch imageLRUPersistBroken")
	}

	// The in-memory LRU must still record the touch (GC ordering unchanged).
	if _, ok := e.imageLRU["sha256:aaa111"]; !ok {
		t.Fatalf("in-memory LRU must still update; got %+v", e.imageLRU)
	}

	// A second task must not touch the filesystem at all: the flag short-circuits
	// before mkdir. Prove it by making the dir name valid afterwards — if the
	// latch works, no file appears.
	good := t.TempDir()
	t.Setenv(stateDirEnv, good)
	e.touchImage(context.Background(), "sha256:bbb222")
	if _, err := os.Stat(filepath.Join(good, "image-last-used.json")); err == nil {
		t.Fatal("latched executor still wrote to disk; the per-task mkdir must be gone")
	}
	if _, ok := e.imageLRU["sha256:bbb222"]; !ok {
		t.Fatal("latched executor must still update the in-memory LRU")
	}
}

func TestImageLRUPersistUnchangedWhenWritable(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(stateDirEnv, dir)

	e1 := &Executor{imageLRU: make(map[string]int64), log: slog.Default()}
	e1.touchImage(context.Background(), "sha256:ccc333")
	if e1.imageLRUPersistBroken {
		t.Fatal("a writable state dir must never latch")
	}
	path := filepath.Join(dir, "image-last-used.json")
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("expected the LRU store file at %s: %v", path, err)
	}

	e2 := &Executor{imageLRU: make(map[string]int64), log: slog.Default()}
	e2.loadImageLRU()
	ts, ok := e2.imageLRU["sha256:ccc333"]
	if !ok || ts == 0 {
		t.Fatalf("round-trip must still work; got %+v", e2.imageLRU)
	}
}
