package executor

import (
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// node-version-pinning: pinToDigest is the pure rewrite that turns a mutable tag
// reference into an immutable digest reference. These cases nail down the tag
// stripping (which must not mistake a registry ':port' for a tag) and the
// legacy passthrough that keeps un-pinned dispatch working unchanged.
func TestPinToDigest(t *testing.T) {
	const d = "sha256:abc123"
	tests := []struct {
		name   string
		ref    string
		digest string
		want   string
	}{
		{"empty digest is passthrough", "reg/modtim/foo:latest", "", "reg/modtim/foo:latest"},
		{"tag replaced by digest", "reg/modtim/foo:latest", d, "reg/modtim/foo@" + d},
		{"bare sha gets sha256 prefix", "reg/modtim/foo:latest", "abc123", "reg/modtim/foo@" + d},
		{"host port is not a tag", "reg.io:5000/modtim/foo:latest", d, "reg.io:5000/modtim/foo@" + d},
		{"host port, no tag", "reg.io:5000/modtim/foo", d, "reg.io:5000/modtim/foo@" + d},
		{"no registry host, has tag", "foo:latest", d, "foo@" + d},
		{"already digest-pinned is left alone", "reg/modtim/foo@sha256:deadbeef", d, "reg/modtim/foo@sha256:deadbeef"},
		{"repo without tag or host", "foo", d, "foo@" + d},
	}
	for _, tt := range tests {
		if got := pinToDigest(tt.ref, tt.digest); got != tt.want {
			t.Errorf("%s: pinToDigest(%q,%q)=%q, want %q", tt.name, tt.ref, tt.digest, got, tt.want)
		}
	}
}

// The default (no-variant) path pins to the top-level ImageDigest when present,
// and returns the plain tag when it is absent (legacy server).
func TestResolveProcessingImage_DefaultDigestPin(t *testing.T) {
	e := testExecutor([]string{"cpu"}, "ghcr.io")

	pinned := transport.TaskPayload{
		ProcessingImage: "reg/modtim/foo:latest",
		ImageDigest:     "sha256:deadbeef",
	}
	if got, want := e.resolveProcessingImage(pinned, "ghcr.io"), "reg/modtim/foo@sha256:deadbeef"; got != want {
		t.Errorf("pinned default: got %q, want %q", got, want)
	}

	legacy := transport.TaskPayload{ProcessingImage: "reg/modtim/foo:latest"}
	if got, want := e.resolveProcessingImage(legacy, "ghcr.io"), "reg/modtim/foo:latest"; got != want {
		t.Errorf("legacy default: got %q, want %q", got, want)
	}
}

// When a hardware variant is selected, the worker pins to that variant's digest
// from the parallel GPUVariantDigests map — and falls back to the tag when the
// map has no entry for the selected backend (a partially-populated map degrades
// gracefully rather than mispinning).
func TestResolveProcessingImage_VariantDigestPin(t *testing.T) {
	payload := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants:     map[string]string{"cuda": "img:cuda", "cpu": "img:cpu"},
		GPUVariantDigests: map[string]string{
			"cuda": "sha256:cuda11",
			// no "cpu" entry on purpose
		},
	}

	// cuda worker → pinned to the cuda digest
	eCuda := testExecutor([]string{"cuda"}, "ghcr.io")
	if got, want := eCuda.resolveProcessingImage(payload, "ghcr.io"), "img@sha256:cuda11"; got != want {
		t.Errorf("cuda variant: got %q, want %q", got, want)
	}

	// cpu worker → selected variant has no digest entry → tag ref, unpinned
	eCPU := testExecutor([]string{"cpu"}, "ghcr.io")
	if got, want := eCPU.resolveProcessingImage(payload, "ghcr.io"), "img:cpu"; got != want {
		t.Errorf("cpu variant (no digest): got %q, want %q", got, want)
	}
}
