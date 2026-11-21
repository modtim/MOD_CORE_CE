package executor

// B119 follow-up (prefer-local vs a docker save|load image) - unit proof, no daemon.
//
// A node image shipped to a remote worker via `docker save | docker load` carries
// NO RepoDigests: docker save/load drops them, so imageDigestForInspect falls back
// to img.ID (the config digest). The per-task expectedDigest the API stamps on
// NodeDefinition.image_digest is the REGISTRY manifest digest - a hash of a
// different thing. normalizeDigest cannot bridge two different hashes, so the
// B119 digest check reports a mismatch for an image that is NOT stale, merely
// loaded: the worker logs "digest mismatch; pulling", pulls a registry_kind=none
// tag and dies `unauthorized` (PIN8 gpu3 run 8, finding F-2).
//
// The fix: with prefer-local ON and the tag present locally, a local image that
// carries NO RepoDigest (no registry digest recorded for it) must NOT be forced
// into a pull by a digest mismatch - there is nothing to compare. A local image
// that DOES carry a RepoDigest which differs from expectedDigest is a real
// mismatch (stale local copy) and still pulls. prefer-local OFF is unchanged.

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

const norepoImage = "ghcr.io/modtim/3d-thumbnail-gen:latest"
const registryDigest = "sha256:aaaaaaaaaaaaaaaa"
const loadedConfigID = "sha256:bbbbbbbbbbbbbbbb"

// inspectHookFor reports exactly the scenario's local image state and counts
// the inspect calls (a skipped pull leaves exactly one).
func inspectHookFor(rd []string, id string, calls *int) imageInspectFunc {
	return func(_ context.Context, image string) (types.ImageInspect, error) {
		*calls = *calls + 1
		return types.ImageInspect{ID: id, RepoDigests: rd}, nil
	}
}

// unreachableClientNoRepo can never serve a pull, so a pull ATTEMPT is observable as
// an error while a skipped pull returns nil.
func unreachableClientNoRepo(t *testing.T) *client.Client {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatalf("client construction failed: %v", err)
	}
	return cli
}

// worker is a bare Executor with the prefer-local tunable set as asked, the
// inspect seam installed and the unreachable client.
func worker(t *testing.T, calls *int, preferLocal bool, rd []string, id string) *Executor {
	cli := unreachableClientNoRepo(t)
	store := tunables.New()
	store.Apply(map[string]interface{}{preferLocalImagesTunableKey: preferLocal})
	return &Executor{
		docker:             cli,
		log:              slog.New(slog.NewTextHandler(os.Stderr, nil)),
		tunables:         store,
		// no localRegistry: the tag is not a local-registry image, so the pull
		// path really does try the registry (and fails on the unreachable client).
		imageInspectHook: inspectHookFor(rd, id, calls),
	}
}

// The bug (F-2): the image is loaded locally (docker save|load -> no RepoDigests)
// and the task carries a registry expectedDigest. Pre-fix: the config-ID fallback
// mismatches, the worker pulls and dies `unauthorized` (err != nil -> RED).
// After the fix: no registry digest is recorded for the local image, so nothing
// can be compared - the local copy is used and the pull is skipped.
func TestPreferLocalNoRepoDigestSkipsPull(t *testing.T) {
	calls := 0
	e := worker(t, &calls, true, []string{}, loadedConfigID)
	if err := e.pullImage(context.Background(), norepoImage, transport.HarborCredentials{}, registryDigest); err != nil {
		t.Errorf("loaded image with no RepoDigest must not be forced into a pull, got: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected exactly one local inspect (no pull), got %d", calls)
	}
}

// A real mismatch must still pull: the local image DOES carry a registry digest
// (RepoDigests) and it differs from the expected one = stale local copy.
func TestPreferLocalRepoDigestMismatchStillPulls(t *testing.T) {
	calls := 0
	e := worker(t, &calls, true, []string{norepoImage + "@" + registryDigest}, loadedConfigID)
	if err := e.pullImage(context.Background(), norepoImage, transport.HarborCredentials{}, "sha256:9999999999999999"); err == nil {
		t.Fatal("a local image carrying a different RepoDigest must still pull")
	}
}

// prefer-local OFF: the pull path is unchanged - a digest mismatch is irrelevant,
// the worker always tries the registry.
func TestPreferLocalOffPullPathUnchanged(t *testing.T) {
	calls := 0
	e := worker(t, &calls, false, []string{}, loadedConfigID)
	if err := e.pullImage(context.Background(), norepoImage, transport.HarborCredentials{}, registryDigest); err == nil {
		t.Fatal("prefer-local off must still attempt the pull")
	}
}

// Regression guard: prefer-local ON with no expectedDigest keeps the legacy skip.
func TestPreferLocalNoExpectedDigestStillSkips(t *testing.T) {
	calls := 0
	e := worker(t, &calls, true, []string{}, loadedConfigID)
	if err := e.pullImage(context.Background(), norepoImage, transport.HarborCredentials{}, ""); err != nil {
		t.Errorf("no expected digest must keep the legacy skip, got: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected exactly one local inspect, got %d", calls)
	}
}
