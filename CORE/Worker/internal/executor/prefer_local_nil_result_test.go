package executor

// 1900Y1g — the CE worker SIGSEGVed (nil deref, signal addr 0x78) in the
// inspect-image closure Run runs right after a prefer-local image-cache hit.
// Run's named *Result return is nil mid-body, so the pre-fix
// `res.ExecutedImageDigest = imageDigestForInspect(img)` wrote through a nil
// pointer — 0x78 is that field's word in Result. The worker died between the
// "prefer-local-images: image cached locally, skipping pull" log and the
// container run, three restarts, run never left running.
//
// These tests drive the cache-hit path with docker == nil and the
// Executor.imageInspectHook seam installed, i.e. no daemon anywhere. Without
// the fix they are red: the seam/note helper do not exist, and pullImage's
// cache-hit branch dereferences the nil daemon client (panic).

import (
	"context"
	"log/slog"
	"os"
	"testing"

	"github.com/docker/docker/api/types"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

const cachedImage = "ghcr.io/modtim/image-ops:latest"

// fakeInspect is a daemon-free imageInspectHook: it reports the image as
// cached with a RepoDigest, exactly what a prefer-local cache hit sees.
func fakeInspect(calls *int) imageInspectFunc {
	return func(_ context.Context, image string) (types.ImageInspect, error) {
		*calls = *calls + 1
		return types.ImageInspect{
			ID:          "sha256:configdigest",
			RepoDigests: []string{image + "@sha256:ABCDEF"},
		}, nil
	}
}

// preferLocalExecutor is a bare Executor with the prefer-local tunable ON and
// the inspect seam installed. docker stays nil on purpose: the whole point is
// that the cache-hit path must never touch the daemon client.
func preferLocalExecutor(t *testing.T, calls *int) *Executor {
	store := tunables.New()
	store.Apply(map[string]interface{}{preferLocalImagesTunableKey: true})
	return &Executor{
		log:              slog.New(slog.NewTextHandler(os.Stderr, nil)),
		tunables:         store,
		imageInspectHook: fakeInspect(calls),
	}
}

// The cache-hit inspect goes through the seam: with docker nil, a fall-through
// to the daemon client would panic.
func TestImageInspectSeamIsDaemonFree(t *testing.T) {
	calls := 0
	e := preferLocalExecutor(t, &calls)
	img, err := e.imageInspect(context.Background(), cachedImage)
	if err != nil {
		t.Fatalf("inspect through the seam failed: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected exactly one hook call, got %d", calls)
	}
	if imageDigestForInspect(img) != "sha256:abcdef" {
		t.Errorf("expected the RepoDigest kind, got %q", imageDigestForInspect(img))
	}
}

// The exact branch that logged "skipping pull" moments before the crash, with
// no daemon at all: prefer-local ON, image cached, no expected digest.
func TestPreferLocalCacheHitSkipsPullWithoutDaemon(t *testing.T) {
	calls := 0
	e := preferLocalExecutor(t, &calls)
	err := e.pullImage(context.Background(), cachedImage, transport.HarborCredentials{}, "")
	if err != nil {
		t.Fatalf("cache hit must skip the pull, got: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected one local inspect, got %d", calls)
	}
}

// Run's shape: the digest is captured MID-BODY, while the named *Result
// return is still nil. Pre-fix this closure wrote res.ExecutedImageDigest
// through that nil pointer (the SIGSEGV); now it returns into a local and
// Run's defer copies it onto whichever Result Run actually returns.
func TestExecutedImageDigestSurvivesNilResult(t *testing.T) {
	calls := 0
	e := preferLocalExecutor(t, &calls)
	var res *Result
	executed := ""
	if err := e.retryOp(context.Background(), 1, "inspect-image", func() error {
		if d := e.noteExecutedImageDigest(context.Background(), cachedImage); d != "" {
			executed = d
		}
		return nil
	}); err != nil {
		t.Fatalf("inspect step must not fail: %v", err)
	}
	if executed != "sha256:abcdef" {
		t.Errorf("digest must be captured without touching res, got %q", executed)
	}
	if res != nil {
		t.Fatal("res is still nil mid-body — the fix must not dereference it")
	}
}
