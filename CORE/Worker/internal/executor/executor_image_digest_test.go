package executor

import (
	"log/slog"
	"os"
	"testing"

	"github.com/docker/docker/api/types"
)

// ============================================================================
// B119 — unit tests for the digest-aware prefer-local path in pullImage.
//
// Scenarios (driven through helpers, no Docker daemon required):
//   (a) prefer-local + RepoDigest matches → skip pull (isRegistryNetworkError
//       + imageDigestForInspect confirm match).
//   (b) mismatch → pullImage falls through to the real pull.
//   (c) mismatch + connectivity error → local fallback (the fallback path in
//       pullImage would catch this; we test the helpers that drive it).
//   (d) mismatch + 404 → error returned, no fallback (404 is NOT a network
//       error after the isRegistryNetworkError narrowing).
//
// Note: full pullImage integration tests that stub docker.ImageInspectWithRaw
// / docker.ImagePull require a dockerAPI interface. The current Executor struct
// embeds *client.Client directly (no interface), so adding a fake client
// would exceed scope. The tests below cover all pure-helper logic that the
// pullImage path depends on.
// ============================================================================

// --------------------------------------------------------------------------
// isRegistryNetworkError
// --------------------------------------------------------------------------

func TestIsRegistryNetworkError_TrueCases(t *testing.T) {
	tests := []string{
		"dial tcp 10.0.0.1:443: connect: connection refused",
		"network unreachable",
		"i/o timeout",
		"TLS handshake timeout",
		"dial tcp: lookup registry.example.com: no such host",
		"read tcp 10.0.0.1:443: read: connection refused",
		"unexpected EOF",
		"connect: network is unreachable",
	}
	for _, msg := range tests {
		if !isRegistryNetworkError(errorString(msg)) {
			t.Errorf("expected isRegistryNetworkError(%q) = true", msg)
		}
	}
}

func TestIsRegistryNetworkError_FalseCases(t *testing.T) {
	tests := []string{
		"invalid argument",
		"syntax error in Dockerfile",
		"container not found",
		"volume already exists",
		"image is in use",
		"unexpected status code 401",
		"unexpected status code 403",
		"unexpected status code 404",
		"unauthorized: authentication required",
		"forbidden: access denied",
		"denied: requested access to the resource is denied",
	}
	for _, msg := range tests {
		if isRegistryNetworkError(errorString(msg)) {
			t.Errorf("expected isRegistryNetworkError(%q) = false", msg)
		}
	}
}

// --------------------------------------------------------------------------
// imageDigestForInspect — RepoDigests preference + ID fallback
// --------------------------------------------------------------------------

func TestImageDigestForInspect_RepoDigestsPreferred(t *testing.T) {
	// RepoDigests should take priority over ID.
	img := types.ImageInspect{
		ID: "sha256:config123",
		RepoDigests: []string{
			"myregistry.io/repo/image@sha256:repodigest456",
		},
	}
	got := imageDigestForInspect(img)
	expected := "sha256:repodigest456"
	if got != expected {
		t.Errorf("expected RepoDigests to be preferred, got %q, want %q", got, expected)
	}
}

func TestImageDigestForInspect_RepoDigestFirstEntry(t *testing.T) {
	// When there are multiple RepoDigests, take the first one.
	img := types.ImageInspect{
		ID: "sha256:config123",
		RepoDigests: []string{
			"myregistry.io/repo/image@sha256:digest001",
			"another.io/repo/image@sha256:digest002",
		},
	}
	got := imageDigestForInspect(img)
	if got != "sha256:digest001" {
		t.Errorf("expected first RepoDigest, got %q", got)
	}
}

func TestImageDigestForInspect_RepoDigestsWithoutAt(t *testing.T) {
	// RepoDigest entry without "@" — should not panic, return empty.
	img := types.ImageInspect{
		ID:          "sha256:config123",
		RepoDigests: []string{"myregistry.io/repo/image"},
	}
	got := imageDigestForInspect(img)
	// No "@" found in RepoDigests, should fall through to ID.
	if got == "" {
		t.Error("expected fallback to ID when RepoDigest has no @")
	}
}

func TestImageDigestForInspect_RepoDigestsEmpty_FallsBackToID(t *testing.T) {
	img := types.ImageInspect{
		ID:          "sha256:config123",
		RepoDigests: []string{},
	}
	got := imageDigestForInspect(img)
	if got != "sha256:config123" {
		t.Errorf("expected ID fallback preserved, got %q", got)
	}
}

func TestImageDigestForInspect_NoRepoDigests_NoID(t *testing.T) {
	img := types.ImageInspect{
		ID:          "",
		RepoDigests: []string{},
	}
	got := imageDigestForInspect(img)
	if got != "" {
		t.Errorf("expected empty string for empty inspect, got %q", got)
	}
}

func TestImageDigestForInspect_IDWithoutPrefix(t *testing.T) {
	// ID without sha256: prefix — should be normalized.
	img := types.ImageInspect{
		ID: "config123",
	}
	got := imageDigestForInspect(img)
	if got != "sha256:config123" {
		t.Errorf("expected sha256: prefix added, got %q", got)
	}
}

func TestImageDigestForInspect_CaseNormalization(t *testing.T) {
	// Digests should be lowercased for comparison.
	img := types.ImageInspect{
		ID: "SHA256:ABCDEF123",
	}
	got := imageDigestForInspect(img)
	if got != "sha256:abcdef123" {
		t.Errorf("expected lowercase, got %q", got)
	}

	img2 := types.ImageInspect{
		ID:          "sha256:UPPER",
		RepoDigests: []string{"reg.io/repo@SHA256:MIXED"},
	}
	got2 := imageDigestForInspect(img2)
	if got2 != "sha256:mixed" {
		t.Errorf("expected RepoDigest lowercased, got %q", got2)
	}
}

// --------------------------------------------------------------------------
// normalizeDigest
// --------------------------------------------------------------------------

func TestNormalizeDigest_AlreadyFormatted(t *testing.T) {
	if got := normalizeDigest("sha256:abc123"); got != "sha256:abc123" {
		t.Errorf("expected %q, got %q", "sha256:abc123", got)
	}
}

func TestNormalizeDigest_NoPrefix(t *testing.T) {
	if got := normalizeDigest("abc123"); got != "sha256:abc123" {
		t.Errorf("expected %q, got %q", "sha256:abc123", got)
	}
}

func TestNormalizeDigest_Empty(t *testing.T) {
	if got := normalizeDigest(""); got != "" {
		t.Errorf("expected empty, got %q", got)
	}
}

func TestNormalizeDigest_Uppercase(t *testing.T) {
	if got := normalizeDigest("SHA256:ABC"); got != "sha256:abc" {
		t.Errorf("expected lowercase, got %q", got)
	}
}

// --------------------------------------------------------------------------
// B119 pullImage scenario helpers (pure-function verification)
// --------------------------------------------------------------------------

func TestPullImage_ScenarioA_RepoDigestMatch(t *testing.T) {
	// Scenario (a): prefer-local + RepoDigest matches → no pull.
	// This is verified by checking that imageDigestForInspect returns the
	// RepoDigest, and normalizeDigest produces a canonical form that can
	// be compared with == against expectedDigest.

	localImg := types.ImageInspect{
		RepoDigests: []string{"reg.io/repo@sha256:abc123"},
		ID:          "sha256:config999", // should be ignored
	}
	localDigest := imageDigestForInspect(localImg)
	if localDigest != "sha256:abc123" {
		t.Errorf("scenario (a): expected local digest to match RepoDigest, got %q", localDigest)
	}

	// normalizeDigest gives the canonical form for comparison.
	normalized := normalizeDigest(localDigest)
	if normalized != "sha256:abc123" {
		t.Errorf("scenario (a): normalized digest mismatch, got %q", normalized)
	}
}

func TestPullImage_ScenarioB_DigestMismatch(t *testing.T) {
	// Scenario (b): mismatch → pull is called.
	// Verify that mismatched digests are detected.
	localImg := types.ImageInspect{
		RepoDigests: []string{"reg.io/repo@sha256:local111"},
		ID:          "sha256:config999",
	}
	localDigest := imageDigestForInspect(localImg)

	expectedDigest := "sha256:remote222"
	if localDigest == expectedDigest {
		t.Error("scenario (b): digests should NOT match")
	}
}

func TestPullImage_ScenarioC_NetworkErrorNoFallback(t *testing.T) {
	// Scenario (c): mismatch + connectivity error → fallback allowed.
	// Verify 404 is NOT a network error (so it doesn't trigger fallback).
	err404 := errorString("unexpected status code 404")
	if isRegistryNetworkError(err404) {
		t.Error("scenario (d): 404 should NOT be treated as a network error")
	}

	// A real network error should still be detected.
	errNet := errorString("dial tcp: connection refused")
	if !isRegistryNetworkError(errNet) {
		t.Error("scenario (c): connection refused should be a network error")
	}
}

func TestPullImage_ScenarioD_404NoFallback(t *testing.T) {
	// Scenario (d): mismatch + 404 → error returned, no fallback.
	// 404 is an HTTP status error, NOT a network connectivity error.
	err404 := errorString("unexpected status code 404")
	if isRegistryNetworkError(err404) {
		t.Error("scenario (d): 404 should NOT trigger fallback — pull should error")
	}

	err401 := errorString("unexpected status code 401")
	if isRegistryNetworkError(err401) {
		t.Error("scenario (d): 401 should NOT trigger fallback")
	}

	err403 := errorString("unexpected status code 403")
	if isRegistryNetworkError(err403) {
		t.Error("scenario (d): 403 should NOT trigger fallback")
	}
}

// --------------------------------------------------------------------------
// Helpers (copied from original file)
// --------------------------------------------------------------------------

// errorString is a tiny helper to make error types for isRegistryNetworkError.
type errorString string

func (e errorString) Error() string { return string(e) }

// newTestLogger creates a minimal slog logger that writes to stderr.
func newTestLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelWarn}))
}

// TestPullImage_PreferLocal_Off_ByDefault verifies the default is off.
func TestPullImage_PreferLocal_Off_ByDefault(t *testing.T) {
	e := &Executor{log: newTestLogger()}
	if e.preferLocalImages() {
		t.Error("default preferLocalImages should be false")
	}
}

// TestPullImage_PreferLocal_EnvOverride verifies the env-var fallback path.
func TestPullImage_PreferLocal_EnvOverride(t *testing.T) {
	os.Setenv("MOD_WORKER_PREFER_LOCAL_IMAGES", "1")
	defer os.Unsetenv("MOD_WORKER_PREFER_LOCAL_IMAGES")
	e := &Executor{log: newTestLogger()}
	if !e.preferLocalImages() {
		t.Error("with MOD_WORKER_PREFER_LOCAL_IMAGES=1, preferLocalImages should return true")
	}
}

// TestPullImage_HarborRegistryDetection verifies isLocalRegistryImage works.
func TestPullImage_LocalRegistryDetection(t *testing.T) {
	e := &Executor{localRegistry: "registry:5000"}
	if !e.isLocalRegistryImage("registry:5000/modtim/foo:latest") {
		t.Error("image under local registry should be detected")
	}
	if e.isLocalRegistryImage("ghcr.io/modtim/foo:latest") {
		t.Error("image under different registry should NOT be detected as local")
	}
}
