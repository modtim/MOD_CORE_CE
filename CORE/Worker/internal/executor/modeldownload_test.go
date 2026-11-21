package executor

import (
	"os"
	"path/filepath"
	"testing"
)

func writeBlob(t *testing.T, dir, name string, size int) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), make([]byte, size), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestRepoFromCacheDirSplitsOnlyTheOrgSeparator(t *testing.T) {
	// Model names contain hyphens as a matter of course. Splitting on every
	// "--" would turn granite-3.3-2b-instruct-GGUF into nonsense, and the repo
	// is what the manager matches against the stack spec to find the total.
	got := repoFromCacheDir("models--ibm-granite--granite-3.3-2b-instruct-GGUF")
	if want := "ibm-granite/granite-3.3-2b-instruct-GGUF"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestAbsentCacheReportsNilNotEmpty(t *testing.T) {
	// nil means "I cannot observe"; empty means "nothing is downloading".
	// Conflating them makes a worker with no managed AI look like one whose
	// download just finished, and the bar would vanish on hosts that never
	// had one.
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", filepath.Join(t.TempDir(), "nope"))
	if got := ScanModelDownloads(); got != nil {
		t.Fatalf("absent cache must report nil, got %#v", got)
	}
}

func TestPresentCacheWithNoPartialsReportsEmpty(t *testing.T) {
	root := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", root)
	writeBlob(t, filepath.Join(root, "hub", "models--org--m", "blobs"), "abc123", 10)

	got := ScanModelDownloads()
	if got == nil {
		t.Fatal("a readable cache must report an observation, not nil")
	}
	if len(got) != 0 {
		t.Fatalf("a completed blob is not a download in flight: %#v", got)
	}
}

func TestPartialDownloadIsReportedWithItsSize(t *testing.T) {
	root := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", root)
	blobs := filepath.Join(root, "hub", "models--ibm-granite--granite-3.3-2b-instruct-GGUF", "blobs")
	writeBlob(t, blobs, "deadbeef.downloadInProgress", 4096)

	got := ScanModelDownloads()
	if len(got) != 1 {
		t.Fatalf("want 1 download, got %#v", got)
	}
	if got[0].Repo != "ibm-granite/granite-3.3-2b-instruct-GGUF" {
		t.Errorf("repo = %q", got[0].Repo)
	}
	if got[0].Bytes != 4096 {
		t.Errorf("bytes = %d, want 4096", got[0].Bytes)
	}
}

func TestBothPartialSuffixesCount(t *testing.T) {
	// llama.cpp writes .downloadInProgress, huggingface_hub writes .incomplete,
	// and both tools write into this same cache. Understanding only one makes
	// the bar disappear depending which rung is pulling.
	root := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", root)
	writeBlob(t, filepath.Join(root, "hub", "models--org--a", "blobs"), "x.downloadInProgress", 100)
	writeBlob(t, filepath.Join(root, "hub", "models--org--b", "blobs"), "y.incomplete", 200)

	got := ScanModelDownloads()
	if len(got) != 2 {
		t.Fatalf("want both partial styles reported, got %#v", got)
	}
}
