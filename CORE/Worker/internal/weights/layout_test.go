package weights

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func sumOf(t *testing.T, s string) string {
	t.Helper()
	d := sha256.Sum256([]byte(s))
	return hex.EncodeToString(d[:])
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLayoutRepoDir(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	tests := []struct {
		name    string
		repoID  string
		want    string
		wantErr bool
	}{
		{"good", "org/model", filepath.Join(tmp, "models--org--model"), false},
		{"missing slash", "orgmodel", "", true},
		{"empty org", "/model", "", true},
		{"empty name", "org/", "", true},
		{"trailing slash", "org/model/", "", true},
		{"extra slash", "a/b/c", "", true},
		{"dotdot org", "../model", "", true},
		{"dotdot name", "org/..", "", true},
		{"dot name", "org/.", "", true},
		{"backslash name", "org/m\\x", "", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := RepoDir(tmp, tt.repoID)
			if (err != nil) != tt.wantErr {
				t.Fatalf("RepoDir(%q) error = %v, wantErr %v", tt.repoID, err, tt.wantErr)
			}
			if !tt.wantErr && got != tt.want {
				t.Fatalf("RepoDir = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestLayoutTraversalRejection(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	repoDir, err := RepoDir(tmp, "org/model")
	if err != nil {
		t.Fatal(err)
	}
	badPaths := []string{
		"",
		"/etc/passwd",
		"../blobs/x",
		"a/../../blobs/x",
	}
	for _, p := range badPaths {
		if _, err := SnapshotPath(repoDir, "main", p); err == nil {
			t.Errorf("SnapshotPath(%q) = no error, want error", p)
		}
	}
	badRevisions := []string{"", "a/b", "..", "main/.."}
	for _, r := range badRevisions {
		if _, err := SnapshotPath(repoDir, r, "model.safetensors"); err == nil {
			t.Errorf("SnapshotPath(revision=%q) = no error, want error", r)
		}
	}
	// A good nested path must resolve inside the repo dir.
	got, err := SnapshotPath(repoDir, "v1", "sub/model.safetensors")
	if err != nil {
		t.Fatalf("SnapshotPath good case: %v", err)
	}
	want := filepath.Join(repoDir, "snapshots", "v1", "sub", "model.safetensors")
	if got != want {
		t.Fatalf("SnapshotPath = %q, want %q", got, want)
	}
}

func TestLayoutWriteAtomic(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	content := "some weights payload"
	sum := sumOf(t, content)

	dst := filepath.Join(tmp, "nested", "model.safetensors")
	if err := writeAtomic(dst, strings.NewReader(content), sum); err != nil {
		t.Fatalf("writeAtomic success: %v", err)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != content {
		t.Fatalf("dst content = %q, want %q", got, content)
	}
	if entries, _ := os.ReadDir(filepath.Dir(dst)); len(entries) != 1 {
		t.Fatalf("dir has %d entries, want exactly dst (no temp files)", len(entries))
	}

	// Mismatch: no dst, no temp file behind.
	badDst := filepath.Join(tmp, "bad", "model.safetensors")
	if err := writeAtomic(badDst, strings.NewReader(content), sumOf(t, "other content")); err == nil {
		t.Fatal("writeAtomic mismatch: want error, got nil")
	} else if !strings.Contains(err.Error(), sum) || !strings.Contains(err.Error(), sumOf(t, content)) {
		t.Fatalf("mismatch error should mention expected and actual hash: %v", err)
	}
	if _, err := os.Stat(badDst); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("dst should not exist after mismatch, stat err = %v", err)
	}
	if entries, _ := os.ReadDir(filepath.Dir(badDst)); len(entries) != 0 {
		t.Fatalf("temp files left behind after mismatch: %v", entries)
	}
}

func TestLayoutBlobValid(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	content := "blob bytes"
	sum := sumOf(t, content)
	blob := filepath.Join(tmp, "b.bin")
	writeFile(t, blob, content)
	if !blobValid(blob, sum) {
		t.Error("blobValid = false for matching blob, want true")
	}
	if blobValid(blob, sumOf(t, "tampered")) {
		t.Error("blobValid = true for wrong hash, want false")
	}
	if blobValid(filepath.Join(tmp, "missing"), sum) {
		t.Error("blobValid = true for missing file, want false")
	}
}

func TestLayoutLink(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	repoDir, err := RepoDir(tmp, "org/model")
	if err != nil {
		t.Fatal(err)
	}
	content := "nested blob content"
	sum := sumOf(t, content)
	writeFile(t, BlobPath(repoDir, sum), content)

	f := File{Path: "sub/dir/model.safetensors", SHA256: sum}
	if err := Link(repoDir, "v1", f); err != nil {
		t.Fatalf("Link: %v", err)
	}
	snapPath, err := SnapshotPath(repoDir, "v1", f.Path)
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(snapPath) // follows the symlink
	if err != nil {
		t.Fatalf("ReadFile through symlink: %v", err)
	}
	if string(got) != content {
		t.Fatalf("content = %q, want %q", got, content)
	}
	target, err := os.Readlink(snapPath)
	if err != nil {
		t.Fatal(err)
	}
	// Link dir snapshots/v1/sub/dir is 4 levels below the repo dir
	// (dir, sub, v1, snapshots), so the target needs 4 "../".
	wantTarget := "../../../../blobs/" + sum
	if target != wantTarget {
		t.Fatalf("Readlink = %q, want relative %q", target, wantTarget)
	}
	if filepath.IsAbs(target) {
		t.Fatalf("link target %q must be relative", target)
	}

	// Idempotent re-run.
	if err := Link(repoDir, "v1", f); err != nil {
		t.Fatalf("second Link: %v", err)
	}
	if target2, err := os.Readlink(snapPath); err != nil || target2 != wantTarget {
		t.Fatalf("after re-run: target = %q err = %v, want %q", target2, err, wantTarget)
	}

	// A wrong pre-existing link is replaced.
	other := File{Path: f.Path, SHA256: sumOf(t, "other")}
	writeFile(t, BlobPath(repoDir, other.SHA256), "other")
	if err := Link(repoDir, "v1", other); err != nil {
		t.Fatalf("Link replace: %v", err)
	}
	if got, err := os.ReadFile(snapPath); err != nil || string(got) != "other" {
		t.Fatalf("after replace: content = %q err = %v, want %q", got, err, "other")
	}
}

func TestLayoutWriteRef(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	repoDir, err := RepoDir(tmp, "org/model")
	if err != nil {
		t.Fatal(err)
	}
	revision := "abc123deadbeef"
	if err := WriteRef(repoDir, "main", revision); err != nil {
		t.Fatalf("WriteRef: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(repoDir, "refs", "main"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != revision {
		t.Fatalf("ref content = %q, want %q (no trailing newline)", got, revision)
	}
}
