// fetch_test.go tests fetchFrom against oras-go's in-memory store, no network.
package weights

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/content/memory"
)

const testMediaType = "application/vnd.mod.weights.file"

// countingStore wraps the memory store and counts Fetch calls per digest.
type countingStore struct {
	*memory.Store
	fetches map[string]int
}

func (c *countingStore) Fetch(ctx context.Context, desc ocispec.Descriptor) (io.ReadCloser, error) {
	c.fetches[desc.Digest.String()]++
	return c.Store.Fetch(ctx, desc)
}

type testFile struct {
	path string
	data []byte
}

func shaHex(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func fileList(files []testFile) []File {
	out := make([]File, 0, len(files))
	for _, f := range files {
		out = append(out, File{Path: f.path, SHA256: shaHex(f.data), Size: int64(len(f.data))})
	}
	return out
}

// pushTestRepo pushes a manifest with one titled layer per file, tagged
// with tag, and returns the layer descriptor per path.
func pushTestRepo(t *testing.T, store *memory.Store, tag string, files []testFile) map[string]ocispec.Descriptor {
	t.Helper()
	ctx := context.Background()
	byPath := make(map[string]ocispec.Descriptor, len(files))
	layers := make([]ocispec.Descriptor, 0, len(files))
	for _, f := range files {
		desc := content.NewDescriptorFromBytes(testMediaType, f.data)
		desc.Annotations = map[string]string{ocispec.AnnotationTitle: f.path}
		if err := store.Push(ctx, desc, bytes.NewReader(f.data)); err != nil {
			t.Fatalf("push blob %s: %v", f.path, err)
		}
		byPath[f.path] = desc
		layers = append(layers, desc)
	}
	data, err := json.Marshal(ocispec.Manifest{MediaType: ocispec.MediaTypeImageManifest, Layers: layers})
	if err != nil {
		t.Fatalf("marshal manifest: %v", err)
	}
	manDesc := content.NewDescriptorFromBytes(ocispec.MediaTypeImageManifest, data)
	if err := store.Push(ctx, manDesc, bytes.NewReader(data)); err != nil {
		t.Fatalf("push manifest: %v", err)
	}
	if err := store.Tag(ctx, manDesc, tag); err != nil {
		t.Fatalf("tag manifest: %v", err)
	}
	return byPath
}

func testRef(repoID, tag string, files []testFile) Ref {
	return Ref{ArtifactRef: "localhost:5000/hf/" + repoID + ":" + tag, RepoID: repoID, Revision: "main", Files: fileList(files)}
}

func repoDirOf(t *testing.T, cacheRoot string) string {
	t.Helper()
	d, err := RepoDir(cacheRoot, "org/model")
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestFetchHappyPath(t *testing.T) {
	files := []testFile{
		{path: "weights.bin", data: []byte("big-weights")},
		{path: "sub/a.bin", data: []byte("nested-file")},
	}
	store := memory.New()
	pushTestRepo(t, store, "v1", files)
	ref := testRef("org/model", "v1", files)
	cacheRoot := t.TempDir()
	if err := fetchFrom(context.Background(), store, "v1", cacheRoot, ref); err != nil {
		t.Fatalf("fetchFrom: %v", err)
	}
	repoDir := repoDirOf(t, cacheRoot)
	for _, f := range files {
		snap, err := SnapshotPath(repoDir, "main", f.path)
		if err != nil {
			t.Fatal(err)
		}
		got, err := os.ReadFile(snap)
		if err != nil {
			t.Fatalf("read snapshot %s: %v", f.path, err)
		}
		if !bytes.Equal(got, f.data) {
			t.Fatalf("snapshot %s = %q, want %q", f.path, got, f.data)
		}
	}
	refFile, err := os.ReadFile(filepath.Join(repoDir, "refs", "main"))
	if err != nil {
		t.Fatalf("read refs/main: %v", err)
	}
	if string(refFile) != "main" {
		t.Fatalf("refs/main = %q, want %q", refFile, "main")
	}
}

func TestFetchSecondRunSkips(t *testing.T) {
	files := []testFile{{path: "w.bin", data: []byte("weights")}}
	store := memory.New()
	byPath := pushTestRepo(t, store, "v1", files)
	ref := testRef("org/model", "v1", files)
	ctx := context.Background()
	cacheRoot := t.TempDir()

	if err := fetchFrom(ctx, store, "v1", cacheRoot, ref); err != nil {
		t.Fatalf("first fetchFrom: %v", err)
	}
	first := &countingStore{Store: store, fetches: map[string]int{}}
	if err := fetchFrom(ctx, first, "v1", cacheRoot, ref); err != nil {
		t.Fatalf("second fetchFrom: %v", err)
	}
	for path, desc := range byPath {
		if n := first.fetches[desc.Digest.String()]; n != 0 {
			t.Fatalf("second run fetched blob for %s: %d times, want 0", path, n)
		}
	}
	if len(first.fetches) == 0 {
		t.Fatal("second run made no fetch calls at all; expected at least the manifest")
	}
}

func TestFetchFileMissingFromManifest(t *testing.T) {
	present := []testFile{{path: "w.bin", data: []byte("weights")}}
	store := memory.New()
	pushTestRepo(t, store, "v1", present)
	missing := []testFile{
		{path: "w.bin", data: []byte("weights")},
		{path: "ghost.bin", data: []byte("never-pushed")},
	}
	ref := testRef("org/model", "v1", missing)
	err := fetchFrom(context.Background(), store, "v1", t.TempDir(), ref)
	if err == nil || !strings.Contains(err.Error(), "not present in manifest") {
		t.Fatalf("err = %v, want 'not present in manifest'", err)
	}
}

func TestFetchDigestMismatch(t *testing.T) {
	files := []testFile{{path: "w.bin", data: []byte("weights")}}
	store := memory.New()
	pushTestRepo(t, store, "v1", files)
	ref := testRef("org/model", "v1", files)
	ref.Files[0].SHA256 = shaHex([]byte("other-content"))
	cacheRoot := t.TempDir()
	err := fetchFrom(context.Background(), store, "v1", cacheRoot, ref)
	if err == nil || !strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("err = %v, want 'digest mismatch'", err)
	}
	blob := filepath.Join(repoDirOf(t, cacheRoot), "blobs", ref.Files[0].SHA256)
	if _, err := os.Stat(blob); !os.IsNotExist(err) {
		t.Fatalf("blob %s exists after digest mismatch (stat err = %v)", blob, err)
	}
}

func TestFetchRefetchesCorruptedBlob(t *testing.T) {
	files := []testFile{{path: "w.bin", data: []byte("weights")}}
	store := memory.New()
	byPath := pushTestRepo(t, store, "v1", files)
	ref := testRef("org/model", "v1", files)
	ctx := context.Background()
	cacheRoot := t.TempDir()
	if err := fetchFrom(ctx, store, "v1", cacheRoot, ref); err != nil {
		t.Fatalf("first fetchFrom: %v", err)
	}
	blob := filepath.Join(repoDirOf(t, cacheRoot), "blobs", ref.Files[0].SHA256)
	if err := os.WriteFile(blob, []byte("corrupted"), 0o644); err != nil {
		t.Fatal(err)
	}
	first := &countingStore{Store: store, fetches: map[string]int{}}
	if err := fetchFrom(ctx, first, "v1", cacheRoot, ref); err != nil {
		t.Fatalf("refetch: %v", err)
	}
	desc := byPath["w.bin"]
	if n := first.fetches[desc.Digest.String()]; n != 1 {
		t.Fatalf("blob fetch count after corruption = %d, want 1", n)
	}
	if !blobValid(blob, ref.Files[0].SHA256) {
		t.Fatal("blob still invalid after re-fetch")
	}
}
