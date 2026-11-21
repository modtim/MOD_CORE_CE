package executor

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// A killed download must be cleaned automatically; a LIVE one must never be.
func TestSweepStaleDownloads(t *testing.T) {
	cache := t.TempDir()
	spec := "services:\n  llm:\n    volumes:\n      - " + cache + ":/root/.cache/huggingface\n"
	repo := filepath.Join(cache, "hub", "models--x--y", "blobs")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(repo, "abc.incomplete")
	lock := filepath.Join(cache, "hub", "x.lock")
	for _, p := range []string{stale, lock} {
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		old := time.Now().Add(-2 * staleDownloadAge)
		if err := os.Chtimes(p, old, old); err != nil {
			t.Fatal(err)
		}
	}
	e := &Executor{}
	if got := e.sweepStaleDownloads(spec); len(got) != 2 {
		t.Fatalf("abandoned artefacts not cleaned: removed %d, want 2 (%v)", len(got), got)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatal("stale .incomplete survived")
	}

	// Now a download that IS in flight: nothing may be touched, including the
	// old lock beside it — another stack on this host may own that fetch.
	if err := os.WriteFile(stale, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(lock, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-2 * staleDownloadAge)
	_ = os.Chtimes(lock, old, old)
	if got := e.sweepStaleDownloads(spec); len(got) != 0 {
		t.Fatalf("swept during a live download: %v", got)
	}
	if _, err := os.Stat(lock); err != nil {
		t.Fatal("old lock removed while a download was active")
	}
}

// A spec with no model-cache bind must be a no-op, not a panic.
func TestSweepIgnoresNonCacheMounts(t *testing.T) {
	e := &Executor{}
	if got := e.sweepStaleDownloads("services:\n  a:\n    volumes:\n      - /tmp/x:/data\n"); len(got) != 0 {
		t.Fatalf("swept an unrelated mount: %v", got)
	}
	if got := e.sweepStaleDownloads("not: [valid"); len(got) != 0 {
		t.Fatalf("unparseable spec should be a no-op: %v", got)
	}
}
