package executor

import (
	"context"
	"errors"
	"log/slog"
	"testing"
)

// TestRewriteToProxyTable ports CORE/API/tests/test_harbor_proxy_cache.py's
// test_rewrite_to_proxy_table (+ the standalone left-untouched cases) so
// both the Python source of truth (services/harbor_proxy_cache.py's
// rewrite_to_proxy) and this Go port agree case-for-case.
func TestRewriteToProxyTable(t *testing.T) {
	const harborHost = "registry.example.com"

	cases := []struct {
		name     string
		imageRef string
		expected string
	}{
		{"bare official image", "nginx:1", harborHost + "/proxy-dockerhub/library/nginx:1"},
		{"bare official image, no tag", "nginx", harborHost + "/proxy-dockerhub/library/nginx"},
		{"dockerhub namespaced", "bitnami/nginx:1.27", harborHost + "/proxy-dockerhub/bitnami/nginx:1.27"},
		{"nvcr.io", "nvcr.io/nim/x:1", harborHost + "/proxy-nvcr/nim/x:1"},
		{"ghcr.io", "ghcr.io/o/r:t", harborHost + "/proxy-ghcr/o/r:t"},
		{"ghcr.io nested path", "ghcr.io/org/sub/repo:t", harborHost + "/proxy-ghcr/org/sub/repo:t"},
		{"already under harbor host is idempotent", harborHost + "/proxy-dockerhub/library/nginx:1", harborHost + "/proxy-dockerhub/library/nginx:1"},
		{"digest-pinned ref is untouched", "nvcr.io/nim/x@sha256:abcdef0123456789", "nvcr.io/nim/x@sha256:abcdef0123456789"},
		{"unknown registry is untouched", "quay.io/some/thing:1", "quay.io/some/thing:1"},
		{"empty ref", "", ""},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := RewriteToProxy(tc.imageRef, harborHost)
			if got != tc.expected {
				t.Errorf("RewriteToProxy(%q, %q) = %q, want %q", tc.imageRef, harborHost, got, tc.expected)
			}
		})
	}
}

// TestRewriteToProxyNoHarborHostIsANoop ports
// test_rewrite_to_proxy_no_harbor_host_is_a_noop.
func TestRewriteToProxyNoHarborHostIsANoop(t *testing.T) {
	got := RewriteToProxy("nginx:1", "")
	if got != "nginx:1" {
		t.Errorf("RewriteToProxy(%q, \"\") = %q, want %q", "nginx:1", got, "nginx:1")
	}
}

// --- pullWithProxyCache: proxy-then-fallback decision, no real docker -----

// fakePuller is the imagePuller test double: no docker daemon, just
// scripted results per call so the decision logic (rewrite -> pull ->
// tag-back -> fallback) can be asserted precisely.
type fakePuller struct {
	pullErr map[string]error // ref -> error to return from Pull
	tagErr  error            // error to return from every Tag call

	pulls []string    // refs Pull was called with, in order
	tags  [][2]string // [source, target] pairs Tag was called with
}

func (f *fakePuller) Pull(_ context.Context, ref string) error {
	f.pulls = append(f.pulls, ref)
	if f.pullErr != nil {
		if err, ok := f.pullErr[ref]; ok {
			return err
		}
	}
	return nil
}

func (f *fakePuller) Tag(_ context.Context, source, target string) error {
	f.tags = append(f.tags, [2]string{source, target})
	return f.tagErr
}

func TestPullWithProxyCache_DisabledIsPlainPull(t *testing.T) {
	f := &fakePuller{}
	err := pullWithProxyCache(context.Background(), f, slog.Default(), "nginx:1", "registry.example.com", false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(f.pulls) != 1 || f.pulls[0] != "nginx:1" {
		t.Errorf("pulls = %v, want [\"nginx:1\"]", f.pulls)
	}
	if len(f.tags) != 0 {
		t.Errorf("tags = %v, want none (disabled)", f.tags)
	}
}

func TestPullWithProxyCache_NoHarborHostIsPlainPull(t *testing.T) {
	f := &fakePuller{}
	err := pullWithProxyCache(context.Background(), f, slog.Default(), "nginx:1", "", true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(f.pulls) != 1 || f.pulls[0] != "nginx:1" {
		t.Errorf("pulls = %v, want [\"nginx:1\"]", f.pulls)
	}
}

func TestPullWithProxyCache_NothingToRewriteIsPlainPull(t *testing.T) {
	f := &fakePuller{}
	err := pullWithProxyCache(context.Background(), f, slog.Default(), "quay.io/some/thing:1", "registry.example.com", true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(f.pulls) != 1 || f.pulls[0] != "quay.io/some/thing:1" {
		t.Errorf("pulls = %v, want the unrewritten ref pulled once", f.pulls)
	}
	if len(f.tags) != 0 {
		t.Errorf("tags = %v, want none (nothing was rewritten)", f.tags)
	}
}

func TestPullWithProxyCache_SuccessTagsBackToOriginalRef(t *testing.T) {
	f := &fakePuller{}
	imageRef := "nginx:1"
	harborHost := "registry.example.com"
	err := pullWithProxyCache(context.Background(), f, slog.Default(), imageRef, harborHost, true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	wantProxyRef := harborHost + "/proxy-dockerhub/library/nginx:1"
	if len(f.pulls) != 1 || f.pulls[0] != wantProxyRef {
		t.Errorf("pulls = %v, want [%q] (only the proxy ref, no fallback)", f.pulls, wantProxyRef)
	}
	if len(f.tags) != 1 || f.tags[0] != [2]string{wantProxyRef, imageRef} {
		t.Errorf("tags = %v, want [[%q %q]] (tag proxy ref back to original)", f.tags, wantProxyRef, imageRef)
	}
}

func TestPullWithProxyCache_ProxyPullErrorFallsBackToOriginalRef(t *testing.T) {
	imageRef := "nginx:1"
	harborHost := "registry.example.com"
	proxyRef := harborHost + "/proxy-dockerhub/library/nginx:1"
	f := &fakePuller{pullErr: map[string]error{proxyRef: errors.New("proxy unreachable")}}

	err := pullWithProxyCache(context.Background(), f, slog.Default(), imageRef, harborHost, true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(f.pulls) != 2 || f.pulls[0] != proxyRef || f.pulls[1] != imageRef {
		t.Errorf("pulls = %v, want [proxy ref, then original ref]", f.pulls)
	}
	if len(f.tags) != 0 {
		t.Errorf("tags = %v, want none (proxy pull never succeeded)", f.tags)
	}
}

func TestPullWithProxyCache_TagBackErrorFallsBackToOriginalRef(t *testing.T) {
	imageRef := "nginx:1"
	harborHost := "registry.example.com"
	proxyRef := harborHost + "/proxy-dockerhub/library/nginx:1"
	f := &fakePuller{tagErr: errors.New("daemon busy")}

	err := pullWithProxyCache(context.Background(), f, slog.Default(), imageRef, harborHost, true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(f.pulls) != 2 || f.pulls[0] != proxyRef || f.pulls[1] != imageRef {
		t.Errorf("pulls = %v, want [proxy ref, then original ref] (tag-back failed)", f.pulls)
	}
}

func TestPullWithProxyCache_BothPullsFailReturnsLastError(t *testing.T) {
	imageRef := "nginx:1"
	harborHost := "registry.example.com"
	proxyRef := harborHost + "/proxy-dockerhub/library/nginx:1"
	wantErr := errors.New("upstream also down")
	f := &fakePuller{pullErr: map[string]error{
		proxyRef: errors.New("proxy unreachable"),
		imageRef: wantErr,
	}}

	err := pullWithProxyCache(context.Background(), f, slog.Default(), imageRef, harborHost, true)
	if !errors.Is(err, wantErr) {
		t.Errorf("err = %v, want %v", err, wantErr)
	}
}
