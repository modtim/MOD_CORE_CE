package executor

// B119 (prefer-local digest normalisation) - unit proof, no daemon.
//
// The expected digest reaches pullImage verbatim: NodeDefinition.image_digest
// is written from the registry Docker-Content-Digest header
// (node_scanner.resolve_image_digest) or from manifest_emit docker-inspect Id
// fallback, and crud.record_node_version stores it after only a strip().
// None of those sources guarantees the sha256: prefix or lower-case hex,
// while imageDigestForInspect always returns the canonical
// "sha256:<lower hex>" form. The pre-fix comparison was a raw
// localDigest == expectedDigest, so a SAME-build image whose registered
// digest is bare or upper-case compares unequal forever: the worker logs
// "digest mismatch; pulling", re-downloads a multi-GB image on every single
// task, and on a registry blip falls back to the local copy behind a
// mismatch warning - the prefer-local cache hit B119 exists to protect never
// happens.

// The fix compares normalizeDigest(localDigest) == normalizeDigest(expectedDigest).

// No daemon anywhere: the Executor docker client is a real client pointed at
// an unreachable URL, so a pull attempt fails with a connection error instead
// of panicking. The observable difference between the old and the new verdict
// is the inspect count: a skipped pull leaves exactly ONE local inspect; a
// pull attempt adds the fallback inspect (2).

import (
	"context"
	"log/slog"
	"os"
	"testing"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const b119CachedImage = "ghcr.io/modtim/taskmergenode:latest"
const b119CanonicalDigest = "sha256:abcdef0123456789"

// cachedInspect reports the image as cached with the canonical RepoDigest -
// exactly what a prefer-local cache hit sees on a live host.
func cachedInspect(calls *int) imageInspectFunc {
	return func(_ context.Context, image string) (types.ImageInspect, error) {
		*calls = *calls + 1
		return types.ImageInspect{
			ID:          "sha256:configdigest",
			RepoDigests: []string{image + "@" + b119CanonicalDigest},
		}, nil
	}
}

// unreachableClient is a real docker client that can never serve a pull, so a
// pull attempt fails with a connection error rather than crashing the test.
func unreachableClient(t *testing.T) *client.Client {
	// unreachableClient is a real docker client that can never serve a pull:
	// the unit-test container has no daemon socket, so a pull attempt fails with
	// a connection error (which isRegistryNetworkError accepts) instead of
	// dereferencing a nil client.
	cli, err := client.NewClientWithOpts(client.WithHost(noDaemonHost), client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatalf("client construction failed: %v", err)
	}
	return cli
}

// preferLocal is a bare Executor with the prefer-local tunable ON, the
// inspect seam installed and the unreachable client.
func preferLocal(t *testing.T, calls *int) *Executor {
	cli := unreachableClient(t)
	store := tunables.New()
	store.Apply(map[string]interface{}{preferLocalImagesTunableKey: true})
	return &Executor{
		docker:             cli,
		log:              slog.New(slog.NewTextHandler(os.Stderr, nil)),
		tunables:         store,
		imageInspectHook: cachedInspect(calls),
	}
}

// The canonical form already matched before the fix - regression guard:
// normalising must not break the case that used to work.
func TestPreferLocalCanonicalDigestStillSkipsPull(t *testing.T) {
	calls := 0
	e := preferLocal(t, &calls)
	if err := e.pullImage(context.Background(), b119CachedImage, transport.HarborCredentials{}, b119CanonicalDigest); err != nil {
		t.Fatalf("canonical match must skip the pull, got: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected exactly one local inspect, got %d", calls)
	}
}

// The bug, form 1: bare hex (manifest_emit docker-inspect Id fallback and
// hand-registered NodeVersion rows). Same build, different spelling.
// Pre-fix: raw == is false, so the pull is attempted and pullImage errors.
func TestPreferLocalBareHexDigestSkipsPull(t *testing.T) {
	calls := 0
	e := preferLocal(t, &calls)
	if err := e.pullImage(context.Background(), b119CachedImage, transport.HarborCredentials{}, "abcdef0123456789"); err != nil {
		t.Fatalf("bare-hex form of the same digest must match and skip the pull, got: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected exactly one local inspect, got %d", calls)
	}
}

// The bug, form 2: upper-case hex (Docker-Content-Digest is not guaranteed
// lower-case). Same build, different spelling.
func TestPreferLocalUpperCaseDigestSkipsPull(t *testing.T) {
	calls := 0
	e := preferLocal(t, &calls)
	if err := e.pullImage(context.Background(), b119CachedImage, transport.HarborCredentials{}, "SHA256:ABCDEF0123456789"); err != nil {
		t.Fatalf("upper-case form of the same digest must match and skip the pull, got: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected exactly one local inspect, got %d", calls)
	}
}

// Normalising must not swallow a real mismatch: a different digest still
// forces the pull (here: the pull fails, which is the observable attempt).
func TestPreferLocalRealMismatchStillPulls(t *testing.T) {
	calls := 0
	e := preferLocal(t, &calls)
	// The unreachable client makes the pull fail; what matters is that the
	// verdict was "mismatch, fall through" (err != nil), never a cache hit.
	if err := e.pullImage(context.Background(), b119CachedImage, transport.HarborCredentials{}, "sha256:9999990000000001"); err == nil {
		t.Fatal("a different digest must not be treated as a cache hit")
	}
	if calls < 1 {
		t.Errorf("expected at least one local inspect, got %d", calls)
	}
}
