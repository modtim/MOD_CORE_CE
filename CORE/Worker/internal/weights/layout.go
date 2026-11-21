// Package weights lays out model weight files on disk in the exact
// HuggingFace hub cache format so that stack containers mounting the
// cache (with HF_HUB_OFFLINE=1) find the files as if huggingface_hub
// had downloaded them.
//
// Layout produced:
//
//	<cacheRoot>/models--<org>--<name>/
//	  blobs/<sha256>                 actual file contents
//	  snapshots/<revision>/<path>    relative symlink -> ../../blobs/<sha256>
//	  refs/<refName>                 the revision the ref points to
package weights

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Ref describes a set of weight files to materialize for a repo.
// JSON tags match the API side's payload shape.
type Ref struct {
	ArtifactRef string `json:"artifact_ref"`
	RepoID      string `json:"repo_id"`
	Revision    string `json:"revision"`
	Files       []File `json:"files"`
}

// File is one file in a Ref.
type File struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// RepoDir returns <cacheRoot>/models--<org>--<name> for a repoID of the
// form "org/name". Both parts must be non-empty and must not contain
// path separators or traversal.
func RepoDir(cacheRoot, repoID string) (string, error) {
	org, name, ok := strings.Cut(repoID, "/")
	if !ok || org == "" || name == "" {
		return "", fmt.Errorf("weights: invalid repo ID %q: want %q", repoID, "org/name")
	}
	if err := checkRepoPart(org); err != nil {
		return "", err
	}
	if err := checkRepoPart(name); err != nil {
		return "", err
	}
	return filepath.Join(cacheRoot, "models--"+org+"--"+name), nil
}

func checkRepoPart(part string) error {
	if strings.ContainsAny(part, `/\`) || part == "." || part == ".." {
		return fmt.Errorf("weights: invalid repo ID part %q", part)
	}
	return nil
}

// BlobPath returns <repoDir>/blobs/<sha256>. Callers must validate
// sha256 as 64 lowercase hex before calling.
func BlobPath(repoDir, sha256 string) string {
	return filepath.Join(repoDir, "blobs", sha256)
}

// validSHA256 reports whether s is exactly 64 lowercase hex characters.
func validSHA256(s string) bool {
	if len(s) != 64 {
		return false
	}
	_, err := hex.DecodeString(s)
	if err != nil {
		return false
	}
	return strings.ToLower(s) == s
}

// SnapshotPath returns <repoDir>/snapshots/<revision>/<relPath>.
// Absolute paths, ".." segments, empty paths, and revisions containing
// "/" or ".." are rejected.
func SnapshotPath(repoDir, revision, relPath string) (string, error) {
	if relPath == "" {
		return "", errors.New("weights: snapshot path is empty")
	}
	if filepath.IsAbs(relPath) {
		return "", fmt.Errorf("weights: snapshot path %q must be relative", relPath)
	}
	if revision == "" {
		return "", errors.New("weights: revision is empty")
	}
	if strings.ContainsAny(revision, `/`) || revision == ".." {
		return "", fmt.Errorf("weights: invalid revision %q", revision)
	}
	for _, seg := range strings.Split(relPath, "/") {
		if seg == ".." {
			return "", fmt.Errorf("weights: snapshot path %q contains a %q segment", relPath, "..")
		}
	}
	return filepath.Join(repoDir, "snapshots", revision, filepath.FromSlash(relPath)), nil
}

// blobValid reports whether the file at path exists and its sha256
// matches the expected hex digest. The file is streamed in chunks, not
// read into memory whole.
func blobValid(path, want string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return false
	}
	return hex.EncodeToString(h.Sum(nil)) == want
}

// writeAtomic writes r to dst as a temp file in the same directory,
// verifying the sha256 digest while streaming. On mismatch the temp
// file is removed and an error mentioning both hashes is returned. On
// match the temp file is fsynced and renamed over dst.
func writeAtomic(dst string, r io.Reader, want string) error {
	if !validSHA256(want) {
		return fmt.Errorf("weights: invalid sha256 %q for %s", want, dst)
	}
	dir := filepath.Dir(dst)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".weights-tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	cleanup := func() {
		tmp.Close()
		os.Remove(tmpName)
	}
	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(tmp, h), r); err != nil {
		cleanup()
		return err
	}
	actual := hex.EncodeToString(h.Sum(nil))
	if actual != want {
		cleanup()
		return fmt.Errorf("weights: sha256 mismatch for %s: expected %s, got %s", dst, want, actual)
	}
	if err := tmp.Sync(); err != nil {
		cleanup()
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Chmod(tmpName, 0o644); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Rename(tmpName, dst); err != nil {
		os.Remove(tmpName)
		return err
	}
	return nil
}

// Link creates the relative symlink
// snapshots/<revision>/<path> -> ../../blobs/<sha256>, adding one extra
// "../" per directory level in the path. Any existing wrong link at
// the target is replaced.
func Link(repoDir, revision string, f File) error {
	if !validSHA256(f.SHA256) {
		return fmt.Errorf("weights: invalid sha256 %q for file %q", f.SHA256, f.Path)
	}
	snapPath, err := SnapshotPath(repoDir, revision, f.Path)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(snapPath), 0o755); err != nil {
		return err
	}
	// The link sits at snapshots/<revision>/<path>, so its directory is
	// two levels (snapshots/<revision>) plus one per directory level in
	// <path> below the repo dir; that many "../" reach blobs/.
	depth := strings.Count(f.Path, "/")
	rel := strings.Repeat("../", depth+2) + "blobs/" + f.SHA256
	if existing, err := os.Readlink(snapPath); err == nil && existing == rel {
		return nil
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("weights: inspect %s: %w", snapPath, err)
	}
	// Remove whatever is there (wrong link, regular file, stale blob copy).
	if err := os.RemoveAll(snapPath); err != nil {
		return err
	}
	if err := os.Symlink(rel, snapPath); err != nil {
		return err
	}
	_ = os.Chmod(filepath.Dir(snapPath), 0o755)
	return nil
}

// WriteRef writes <repoDir>/refs/<refName> containing the revision with
// no trailing newline, as huggingface_hub does.
func WriteRef(repoDir, refName, revision string) error {
	refPath := filepath.Join(repoDir, "refs", refName)
	if filepath.IsAbs(refName) || strings.ContainsAny(refName, `/\`) {
		return fmt.Errorf("weights: invalid ref name %q", refName)
	}
	if err := os.MkdirAll(filepath.Dir(refPath), 0o755); err != nil {
		return err
	}
	return os.WriteFile(refPath, []byte(revision), 0o644)
}
