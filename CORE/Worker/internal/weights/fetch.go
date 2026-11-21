// fetch.go downloads weight files from an OCI registry (via oras-go)
// into the HF-cache layout produced by layout.go.
package weights

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/registry"
	"oras.land/oras-go/v2/registry/remote"
	"oras.land/oras-go/v2/registry/remote/auth"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// Fetch materializes every ref from the registry named by its
// ref.ArtifactRef into the HF-cache layout under cacheRoot. It stops at
// the first error and returns false; credentials never appear in the
// returned error.
func Fetch(ctx context.Context, cacheRoot string, refs []Ref, creds transport.HarborCredentials) (bool, error) {
	for _, ref := range refs {
		repo, err := remote.NewRepository(ref.ArtifactRef)
		if err != nil {
			return false, fmt.Errorf("weights: repo %s: %w", ref.RepoID, err)
		}
		if isLoopbackRegistry(ref.ArtifactRef) {
			repo.PlainHTTP = true
		}
		repo.Client = &auth.Client{
			Client: http.DefaultClient,
			Cache:  auth.NewCache(),
			Credential: auth.StaticCredential(registryHost(creds.Registry, ref.ArtifactRef),
				auth.Credential{Username: creds.Username, Password: creds.Password}),
		}
		if err := fetchFrom(ctx, repo, refTag(ref.ArtifactRef), cacheRoot, ref); err != nil {
			return false, fmt.Errorf("weights: repo %s: %w", ref.RepoID, err)
		}
	}
	return true, nil
}

// fetchFrom resolves tag on target, downloads the layer whose
// org.opencontainers.image.title annotation matches each ref.Files
// entry, verifies its digest, links it under snapshots/<revision> and
// records refs/<revision>.
func fetchFrom(ctx context.Context, target oras.ReadOnlyTarget, tag string, cacheRoot string, ref Ref) error {
	manifestDesc, err := target.Resolve(ctx, tag)
	if err != nil {
		return fmt.Errorf("resolve %s: %w", tag, err)
	}
	data, err := content.FetchAll(ctx, target, manifestDesc)
	if err != nil {
		return fmt.Errorf("fetch manifest %s: %w", manifestDesc.Digest, err)
	}
	var manifest ocispec.Manifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return fmt.Errorf("parse manifest %s: %w", manifestDesc.Digest, err)
	}
	repoDir, err := RepoDir(cacheRoot, ref.RepoID)
	if err != nil {
		return err
	}
	for _, f := range ref.Files {
		if err := fetchFile(ctx, target, manifest, repoDir, ref, f); err != nil {
			return err
		}
	}
	return WriteRef(repoDir, ref.Revision, ref.Revision)
}

// fetchFile downloads (if needed) and links one ref file.
func fetchFile(ctx context.Context, target oras.ReadOnlyTarget, manifest ocispec.Manifest, repoDir string, ref Ref, f File) error {
	layer, ok := findLayer(manifest.Layers, f.Path)
	if !ok {
		return fmt.Errorf("file %q: not present in manifest", f.Path)
	}
	want := "sha256:" + f.SHA256
	if layer.Digest.String() != want {
		return fmt.Errorf("file %q: digest mismatch: manifest has %s, ref has %s", f.Path, layer.Digest, want)
	}
	blob := BlobPath(repoDir, f.SHA256)
	if !blobValid(blob, f.SHA256) {
		rc, err := target.Fetch(ctx, layer)
		if err != nil {
			return fmt.Errorf("file %q: fetch %s: %w", f.Path, layer.Digest, err)
		}
		defer rc.Close()
		if err := writeAtomic(blob, rc, f.SHA256); err != nil {
			return fmt.Errorf("file %q: %w", f.Path, err)
		}
	}
	if err := Link(repoDir, ref.Revision, f); err != nil {
		return fmt.Errorf("file %q: %w", f.Path, err)
	}
	return nil
}

// findLayer returns the layer annotated with org.opencontainers.image.title == path.
func findLayer(layers []ocispec.Descriptor, path string) (ocispec.Descriptor, bool) {
	for _, l := range layers {
		if l.Annotations[ocispec.AnnotationTitle] == path {
			return l, true
		}
	}
	return ocispec.Descriptor{}, false
}

// registryHost returns the host credentials are scoped to: creds.Registry
// if set, otherwise the host part of the artifact ref.
func registryHost(credsRegistry, artifactRef string) string {
	h := credsRegistry
	if h == "" {
		h = artifactRef
	}
	h = strings.TrimPrefix(strings.TrimPrefix(h, "https://"), "http://")
	if i := strings.Index(h, "/"); i >= 0 {
		h = h[:i]
	}
	return h
}

// isLoopbackRegistry reports whether the artifact ref points at a
// localhost registry, which always speaks plain HTTP.
func isLoopbackRegistry(artifactRef string) bool {
	h := registryHost("", artifactRef)
	return h == "localhost" || h == "127.0.0.1"
}

// refTag extracts the tag (or digest) of a full artifact reference,
// defaulting to "latest".
func refTag(artifactRef string) string {
	r, err := registry.ParseReference(artifactRef)
	if err != nil {
		return "latest"
	}
	ref := r.Reference
	if ref == "" {
		return "latest"
	}
	// Drop a trailing digest ("tag@sha256:..." → keep the tag, or a bare
	// digest → the digest itself).
	if i := strings.Index(ref, "@"); i >= 0 {
		ref = ref[:i]
	}
	if ref == "" {
		return "latest"
	}
	return ref
}
