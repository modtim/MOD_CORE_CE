package executor

// sz-tcp.16b tests: cache hit, cache miss -> store, probe error -> nil (no
// panic). Redis is mocked with the executor's own interface-fake pattern
// (egress_proxy_test.go's fake dockerAPI, tokenserver_test.go's RunTokenMinter)
// — no live Redis, no miniredis in the worker's dep set.

import (
	"bytes"
	"context"
	"errors"
	"io"
	"sync"
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/pkg/stdcopy"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const probeDoc = `{"probes": [{"bytes": 1234, "family": "images", "format": "png", "path": "/data/input/a.png", "size_unit_value": 12.0, "width": 4000, "height": 3000, "megapixels": 12.0}]}`

// framedLog is what a non-TTY daemon actually sends: multiplexed frames, so
// sizeProbeStdout's stdcopy demux is exercised, not a raw-string shortcut.
func framedLog(payload string) []byte {
	var framed bytes.Buffer
	_, err := stdcopy.NewStdWriter(&framed, stdcopy.Stdout).Write([]byte(payload))
	if err != nil {
		return []byte{}
	}
	return framed.Bytes()
}

func cannedReader(payload string) io.ReadCloser {
	return io.NopCloser(bytes.NewBufferString(payload))
}

// fakeProbeDocker is a recording dockerProbeAPI.
type fakeProbeDocker struct {
	calls    []string
	logs     []byte   // canned multiplexed log stream
	pullErr  error    // ImagePull error (nil = ok)
	createOK bool     // ContainerCreate succeeds
	binds    []string // Binds the hook asked for
	nonZero   bool    // ContainerWait reports exit code 3
	blockWait        bool // ContainerWait blocks until unblockWait() (async timing tests)
	unblockCh        chan struct{}
	unblockMu        sync.Mutex
	unblockRequested bool
	// captureCreate (sizeprobe_lockdown_test.go) records the exact Config/HostConfig
	// the hook passed, so lockdown guards can be asserted off the real create path.
	captureCreate func(*container.Config, *container.HostConfig)
}

func (f *fakeProbeDocker) ImagePull(_ context.Context, ref string, _ image.PullOptions) (io.ReadCloser, error) {
	f.calls = append(f.calls, "ImagePull "+ref)
	if f.pullErr != nil {
		return nil, f.pullErr
	}
	return cannedReader("{}"), nil
}

func (f *fakeProbeDocker) ContainerCreate(_ context.Context, cfg *container.Config, hc *container.HostConfig, _ *network.NetworkingConfig, _ *ocispec.Platform, _ string) (container.CreateResponse, error) {
	f.calls = append(f.calls, "ContainerCreate")
	if hc != nil {
		f.binds = hc.Binds
	}
	if f.captureCreate != nil {
		f.captureCreate(cfg, hc)
	}
	if !f.createOK {
		return container.CreateResponse{}, errors.New("boom: create failed")
	}
	return container.CreateResponse{ID: "cid-probe"}, nil
}

func (f *fakeProbeDocker) ContainerStart(_ context.Context, _ string, _ container.StartOptions) error {
	f.calls = append(f.calls, "ContainerStart")
	return nil
}

func (f *fakeProbeDocker) ContainerRemove(_ context.Context, _ string, o container.RemoveOptions) error {
	f.calls = append(f.calls, "ContainerRemove")
	if !o.Force {
		return errors.New("expected force remove")
	}
	return nil
}

func (f *fakeProbeDocker) ContainerKill(_ context.Context, _, _ string) error {
	f.calls = append(f.calls, "ContainerKill")
	return nil
}

func (f *fakeProbeDocker) ContainerWait(_ context.Context, _ string, _ container.WaitCondition) (<-chan container.WaitResponse, <-chan error) {
	f.calls = append(f.calls, "ContainerWait")
	code := int64(0)
	if f.nonZero {
		code = 3
	}
	statusCh := make(chan container.WaitResponse)
	errCh := make(chan error)
	if f.blockWait {
		ch := make(chan struct{})
		f.unblockMu.Lock()
		f.unblockCh = ch
		if f.unblockRequested {
			close(ch)
		}
		f.unblockMu.Unlock()
		go func() {
			<-ch
			statusCh <- container.WaitResponse{StatusCode: code}
		}()
	} else {
		go sendWait(statusCh, code)
	}
	return statusCh, errCh
}

func (f *fakeProbeDocker) unblockWait() {
	if !f.blockWait {
		return
	}
	f.unblockMu.Lock()
	f.unblockRequested = true
	if f.unblockCh != nil {
		close(f.unblockCh)
		f.unblockCh = nil
	}
	f.unblockMu.Unlock()
}

func sendWait(ch chan container.WaitResponse, code int64) {
	ch <- container.WaitResponse{StatusCode: code}
}

func (f *fakeProbeDocker) ContainerLogs(_ context.Context, _ string, _ container.LogsOptions) (io.ReadCloser, error) {
	f.calls = append(f.calls, "ContainerLogs")
	return io.NopCloser(bytes.NewBuffer(f.logs)), nil
}

var _ dockerProbeAPI = (*fakeProbeDocker)(nil)

// fakeSizeCache is a recording sizeCacheOps.
type fakeSizeCache struct {
	hits    map[string]string
	missErr error
	lookups []string
	stores  map[string]string
}

func (c *fakeSizeCache) Lookup(_ context.Context, key string) (string, error) {
	c.lookups = append(c.lookups, key)
	if c.missErr != nil {
		return "", c.missErr
	}
	if v, ok := c.hits[key]; ok {
		return v, nil
	}
	return "", nil
}

func (c *fakeSizeCache) Store(_ context.Context, key, val string) error {
	if c.stores == nil {
		c.stores = make(map[string]string)
	}
	c.stores[key] = val
	return nil
}

var _ sizeCacheOps = (*fakeSizeCache)(nil)

func probePayload(hashes []string) transport.TaskPayload {
	var p transport.TaskPayload
	for _, h := range hashes {
		p.InputFiles = append(p.InputFiles, transport.InputFile{ContentHash: h})
	}
	return p
}

func probeTask(p transport.TaskPayload) *transport.Task {
	var task transport.Task
	task.Payload = p
	return &task
}

func probeCreds(registry string) *transport.Credentials {
	return &transport.Credentials{Harbor: transport.HarborCredentials{Registry: registry}}
}

// --- 1. cache hit: the probe container must NOT run -------------------------
func TestSizeProbe_CacheHit(t *testing.T) {
	p := probePayload([]string{"sha256:bbb", "sha256:aaa"})
	key := sizeProbeCacheKey(sizeProbeInputHashes(p))
	if key == "" {
		t.Fatalf("no cache key for a task with inputs")
	}

	var cache fakeSizeCache
	cache.hits = map[string]string{key: probeDoc}
	SetSizeProbeCache(&cache)

	var docker fakeProbeDocker
	doc := SizeProbe(context.Background(), probeTask(p), probeCreds("harbor.example"), "mod-net", "", "vol-1", &docker)
	if doc == nil {
		t.Fatalf("cache hit must return the cached document")
	}
	if doc.Raw != probeDoc {
		t.Fatalf("cached doc not returned verbatim: %s", doc.Raw)
	}
	if len(doc.Probes) != 1 || doc.Probes[0].Family != "images" {
		t.Fatalf("cached doc not decoded: %+v", doc.Probes[0])
	}
	if len(docker.calls) != 0 {
		t.Fatalf("a cache hit must not touch docker: %v", docker.calls)
	}
	if len(cache.lookups) != 1 || cache.lookups[0] != key {
		t.Fatalf("expected exactly one lookup of %s: %v", key, cache.lookups)
	}
}

// --- 2. cache miss -> probe runs, result is stored ---------------------------
func TestSizeProbe_MissStores(t *testing.T) {
	p := probePayload([]string{"sha256:aaa", "sha256:bbb"})
	key := sizeProbeCacheKey(sizeProbeInputHashes(p))

	var cache fakeSizeCache
	SetSizeProbeCache(&cache)

	var docker fakeProbeDocker
	docker.createOK = true
	docker.logs = framedLog(probeDoc)

	doc := SizeProbe(context.Background(), probeTask(p), probeCreds("harbor.example"), "mod-net", "", "vol-1", &docker)
	if doc == nil {
		t.Fatalf("a probe that ran must return a document")
	}
	if doc.Raw != probeDoc {
		t.Fatalf("probe stdout not returned verbatim: %s", doc.Raw)
	}
	if len(cache.stores) != 1 || cache.stores[key] != probeDoc {
		t.Fatalf("expected one store of %s: %v", key, cache.stores)
	}
	if len(docker.binds) != 1 || docker.binds[0] != "vol-1:/data:ro" {
		t.Fatalf("inputs must be mounted read-only: %v", docker.binds)
	}
}

// --- 3. probe failure -> nil, no panic, task path unaffected -----------------
func TestSizeProbe_FailureIsNil(t *testing.T) {
	p := probePayload([]string{"sha256:aaa"})
	task := probeTask(p)
	creds := probeCreds("harbor.example")

	var pullDocker fakeProbeDocker
	pullDocker.pullErr = errors.New("no such image")
	var createDocker fakeProbeDocker
	createDocker.createOK = false
	var exitDocker fakeProbeDocker
	exitDocker.createOK = true
	exitDocker.nonZero = true
	var junkDocker fakeProbeDocker
	junkDocker.createOK = true
	junkDocker.logs = framedLog("not json at all")

	for _, d := range []*fakeProbeDocker{&pullDocker, &createDocker, &exitDocker, &junkDocker} {
		var cache fakeSizeCache
		SetSizeProbeCache(&cache)
		doc := SizeProbe(context.Background(), task, creds, "mod-net", "", "vol-1", d)
		if doc != nil {
			t.Fatalf("a failing probe must return nil, never a document")
		}
		if len(cache.stores) != 0 {
			t.Fatalf("a failing probe must not poison the cache")
		}
	}

	// No cache wired at all: the probe still runs, nothing is stored.
	SetSizeProbeCache(nil)
	var nocacheDocker fakeProbeDocker
	nocacheDocker.createOK = true
	nocacheDocker.logs = framedLog(probeDoc)
	doc := SizeProbe(context.Background(), task, creds, "mod-net", "", "vol-1", &nocacheDocker)
	if doc == nil {
		t.Fatalf("no cache must not disable the probe")
	}
}
