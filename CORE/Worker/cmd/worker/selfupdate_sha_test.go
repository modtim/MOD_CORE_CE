package main

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// GSEC R-035: selfUpdate must refuse a downloaded worker binary that does not
// match the SHA-256 published in the release manifest, and must refuse when no
// digest is published at all — an unverified download never replaces the
// running binary.

func shaOf(content []byte) string {
	sum := sha256.Sum256(content)
	return hex.EncodeToString(sum[:])
}

func writeTempBinary(t *testing.T, content []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "mod-worker-update")
	if err := os.WriteFile(path, content, 0755); err != nil {
		t.Fatalf("write temp binary: %v", err)
	}
	return path
}

func TestVerifyDownloadSHA256Match(t *testing.T) {
	content := []byte("fake-binary-bytes")
	path := writeTempBinary(t, content)
	if err := verifyDownloadSHA256(path, shaOf(content)); err != nil {
		t.Fatalf("expected nil for matching digest, got: %v", err)
	}
}

func TestVerifyDownloadSHA256MatchIsCaseInsensitive(t *testing.T) {
	content := []byte("fake-binary-bytes")
	path := writeTempBinary(t, content)
	if err := verifyDownloadSHA256(path, strings.ToUpper(shaOf(content))); err != nil {
		t.Fatalf("expected nil for uppercase digest, got: %v", err)
	}
}

func TestVerifyDownloadSHA256Mismatch(t *testing.T) {
	path := writeTempBinary(t, []byte("tampered-binary-bytes"))
	err := verifyDownloadSHA256(path, shaOf([]byte("different-content")))
	if err == nil {
		t.Fatal("expected error for mismatched digest, got nil")
	}
	if !strings.Contains(err.Error(), "mismatch") {
		t.Fatalf("expected mismatch error, got: %v", err)
	}
}

func TestVerifyDownloadSHA256EmptyExpectedRefuses(t *testing.T) {
	path := writeTempBinary(t, []byte("fake-binary-bytes"))
	if err := verifyDownloadSHA256(path, ""); err == nil {
		t.Fatal("expected error for empty expected digest, got nil")
	}
	if err := verifyDownloadSHA256(path, "   "); err == nil {
		t.Fatal("expected error for blank expected digest, got nil")
	}
}
