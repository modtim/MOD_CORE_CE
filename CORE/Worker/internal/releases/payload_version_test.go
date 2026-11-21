package releases

import (
	"os"
	"path/filepath"
	"testing"
)

// The manifest's only job is that its version and its bytes agree. Publishing
// the RUNNING binary's version against the BAKED payload broke that after any
// self-update: the manifest advertised one version and shipped another, so a
// remote worker installed it, reported the older number, saw the newer one
// still advertised, and updated again — for ever, re-downloading every
// heartbeat cycle. Observed live on 2026-08-14.
func TestPayloadVersionReadsWhatShipsBesideTheBinaries(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "VERSION")
	if err := os.WriteFile(path, []byte("v1.0.46\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := PayloadVersion(path)
	if err != nil || got != "v1.0.46" {
		t.Fatalf("PayloadVersion = %q, %v; want v1.0.46", got, err)
	}
}

// Unreadable must mean "do not publish". A release nobody cut beats one that
// cannot be satisfied.
func TestAMissingVersionRefusesRatherThanGuessing(t *testing.T) {
	if _, err := PayloadVersion(filepath.Join(t.TempDir(), "nope")); err == nil {
		t.Fatal("a missing VERSION returned no error; the caller would publish a guess")
	}
}

func TestAnEmptyVersionIsRefused(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "VERSION")
	os.WriteFile(path, []byte("   \n"), 0o644)
	if _, err := PayloadVersion(path); err == nil {
		t.Fatal("an empty VERSION was accepted")
	}
}

// The same placeholders PublishIfNewer refuses, so the caller gets one answer
// rather than two different ones.
func TestPlaceholdersAreRefused(t *testing.T) {
	for _, v := range []string{"dev", "docker", "unknown"} {
		dir := t.TempDir()
		path := filepath.Join(dir, "VERSION")
		os.WriteFile(path, []byte(v), 0o644)
		if _, err := PayloadVersion(path); err == nil {
			t.Errorf("placeholder %q was accepted as a release version", v)
		}
	}
}
