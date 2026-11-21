package executor

import (
	"context"
	"io"
	"log/slog"
	"strings"

	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/client"
)

// harborProxyCacheTunableKey is the platform_settings registry key for the
// harbor-proxy-cache pull override (GF.C3b) — must match
// CORE/API/services/platform_settings.py's register() call and
// worker_management.py's _WORKER_TUNABLE_KEYS mapping.
const harborProxyCacheTunableKey = "worker.harbor_proxy_cache"

// proxyUpstreams mirrors CORE/API/services/harbor_proxy_cache.py's
// UPSTREAMS table EXACTLY: upstream registry host -> Harbor proxy-cache
// project name. This is the Go port (GF.C3b) of that Python source of
// truth; there is no codegen linking the two, so keep them in sync by hand
// (and by the shared table test in harbor_proxy_test.go / the Python
// tests/test_harbor_proxy_cache.py rewrite_to_proxy cases).
var proxyUpstreams = map[string]string{
	"docker.io": "proxy-dockerhub",
	"nvcr.io":   "proxy-nvcr",
	"ghcr.io":   "proxy-ghcr",
}

// RewriteToProxy rewrites a bare/upstream image ref to pull through
// Harbor's proxy-cache. Ports
// CORE/API/services/harbor_proxy_cache.py's rewrite_to_proxy EXACTLY (same
// cases, same order, same left-untouched rules):
//
//	"nginx:1"             -> "<harborHost>/proxy-dockerhub/library/nginx:1"
//	"bitnami/nginx:1"     -> "<harborHost>/proxy-dockerhub/bitnami/nginx:1"
//	"nvcr.io/nim/x:1"     -> "<harborHost>/proxy-nvcr/nim/x:1"
//	"ghcr.io/o/r:t"       -> "<harborHost>/proxy-ghcr/o/r:t"
//
// Left completely untouched (returned verbatim):
//   - a ref already under harborHost (idempotent — rewriting a rewritten
//     ref would double-prefix it).
//   - a digest-pinned ref ("...@sha256:...") — already exact and
//     registry-agnostic.
//   - any host not in proxyUpstreams (nothing to rewrite it to).
//   - an empty ref, or an empty harborHost.
//
// Pure function — no network, no Harbor client, no docker.
func RewriteToProxy(imageRef, harborHost string) string {
	ref := strings.TrimSpace(imageRef)
	if ref == "" {
		return ref
	}

	hostPrefix := strings.TrimRight(strings.TrimSpace(harborHost), "/")
	if hostPrefix != "" && (ref == hostPrefix || strings.HasPrefix(ref, hostPrefix+"/")) {
		return ref
	}

	if strings.Contains(ref, "@sha256:") {
		return ref
	}

	firstSeg, sep, remainder := partitionOnce(ref, "/")
	var host, rest string
	if sep && (strings.Contains(firstSeg, ".") || strings.Contains(firstSeg, ":") || firstSeg == "localhost") {
		host = firstSeg
		rest = remainder
	} else {
		host = "docker.io"
		rest = ref
	}

	project, ok := proxyUpstreams[host]
	if !ok {
		return ref
	}

	if host == "docker.io" && !strings.Contains(rest, "/") {
		// Docker Hub's own convention for unqualified "official" images.
		rest = "library/" + rest
	}

	if hostPrefix == "" {
		return ref
	}

	return hostPrefix + "/" + project + "/" + rest
}

// partitionOnce mirrors Python's str.partition(sep): splits s on the first
// occurrence of sep, returning (before, found, after). If sep is absent,
// returns (s, false, "").
func partitionOnce(s, sep string) (before string, found bool, after string) {
	i := strings.Index(s, sep)
	if i == -1 {
		return s, false, ""
	}
	return s[:i], true, s[i+len(sep):]
}

// imagePuller is the seam pullWithProxyCache tests against, narrowing the
// docker client to just the two calls the proxy-cache flow needs. Lets the
// rewrite/pull/tag-back/fallback decision be unit tested without a real
// docker daemon (see harbor_proxy_test.go).
type imagePuller interface {
	Pull(ctx context.Context, ref string) error
	Tag(ctx context.Context, source, target string) error
}

// dockerImagePuller adapts *client.Client to imagePuller for real pulls.
type dockerImagePuller struct {
	docker *client.Client
}

func (p dockerImagePuller) Pull(ctx context.Context, ref string) error {
	reader, err := p.docker.ImagePull(ctx, ref, image.PullOptions{})
	if err != nil {
		return err
	}
	defer reader.Close()
	io.Copy(io.Discard, reader)
	return nil
}

func (p dockerImagePuller) Tag(ctx context.Context, source, target string) error {
	return p.docker.ImageTag(ctx, source, target)
}

// pullWithProxyCache implements the GF.C3b decision: when harbor-proxy-cache
// is enabled and a Harbor host is known, try pulling the rewritten
// proxy-cache ref first and `docker tag` it back to the original ref
// (ImageTag) so compose/containers that reference the original image name
// still find it. ANY proxy error — the rewritten pull failing, or the
// tag-back failing — logs one line and falls back to pulling the original
// ref: the proxy cache is always best-effort, never a hard dependency for
// the pull to succeed.
//
// When disabled, when no Harbor host is known yet, or when the ref has
// nothing to rewrite to (RewriteToProxy is a no-op for it), this is just a
// plain pull of imageRef — no proxy involved.
func pullWithProxyCache(ctx context.Context, puller imagePuller, log *slog.Logger, imageRef, harborHost string, enabled bool) error {
	if !enabled || harborHost == "" {
		return puller.Pull(ctx, imageRef)
	}

	rewritten := RewriteToProxy(imageRef, harborHost)
	if rewritten == imageRef {
		return puller.Pull(ctx, imageRef)
	}

	if err := puller.Pull(ctx, rewritten); err != nil {
		if log != nil {
			log.Warn("harbor-proxy-cache: proxy pull failed, falling back to upstream ref",
				"image", imageRef, "proxy_ref", rewritten, "error", err)
		}
		return puller.Pull(ctx, imageRef)
	}

	if err := puller.Tag(ctx, rewritten, imageRef); err != nil {
		if log != nil {
			log.Warn("harbor-proxy-cache: tag-back failed, falling back to upstream ref",
				"image", imageRef, "proxy_ref", rewritten, "error", err)
		}
		return puller.Pull(ctx, imageRef)
	}

	return nil
}

// noteHarborHost records the platform Harbor registry host seen on a
// task's credentials, so a later PullImagePublic call (GF.C3b) — which
// runs before any task creds exist — still has a host to rewrite public
// image refs against once at least one task has run. Overwrites on every
// call: the platform Harbor host does not change between tasks, so the
// last-seen value is always current. A no-op for an empty host.
func (e *Executor) noteHarborHost(host string) {
	if host == "" {
		return
	}
	e.harborHostMu.Lock()
	e.harborHost = host
	e.harborHostMu.Unlock()
}

// knownHarborHost returns the most recently seen platform Harbor registry
// host, or "" if no task has supplied one yet.
func (e *Executor) knownHarborHost() string {
	e.harborHostMu.RLock()
	defer e.harborHostMu.RUnlock()
	return e.harborHost
}

// harborProxyCacheEnabled reads the worker.harbor_proxy_cache tunable the
// same way preferLocalImages reads worker.prefer_local_images: server-
// pushed value only (see tunables.Store.Bool) — this is new behaviour, so
// deliberately no env var fallback (an operator toggles it in the
// Dashboard, not by editing worker env files).
func (e *Executor) harborProxyCacheEnabled() bool {
	if e.tunables == nil {
		return false
	}
	return e.tunables.Bool(harborProxyCacheTunableKey, "", false)
}

// pullPublicImage pulls a public (non-Harbor-credentialed) image ref,
// honoring MOD_WORKER_PREFER_LOCAL_IMAGES / worker.prefer_local_images
// FIRST — same precedence as pullImage: if the image is already cached
// locally and prefer-local is on, no pull at all — then routing through
// Harbor's proxy-cache when worker.harbor_proxy_cache is enabled and a
// Harbor host is known (pullWithProxyCache), falling back to a bare pull
// of the original ref otherwise.
func (e *Executor) pullPublicImage(ctx context.Context, imageRef string) error {
	if e.preferLocalImages() {
		if _, _, err := e.docker.ImageInspectWithRaw(ctx, imageRef); err == nil {
			e.log.Info(
				"prefer-local-images: image cached locally, skipping pull",
				"image", imageRef,
			)
			return nil
		}
	}
	return pullWithProxyCache(ctx, dockerImagePuller{e.docker}, e.log, imageRef, e.knownHarborHost(), e.harborProxyCacheEnabled())
}
