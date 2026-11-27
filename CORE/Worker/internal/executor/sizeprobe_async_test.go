package executor

// OPT-SIZE-PROBE-OFF-CRITICAL-PATH tests: parity of the async wrapper with the
// old synchronous call (same cache key, same JSON, no task failure), and no
// goroutine leak when the task context is cancelled/finished.

import (
	"context"
	"errors"
	"runtime"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// TestAsyncSizeProbeParityWithSync: for identical inputs/cache/docker, the
// async wrapper returns exactly the same doc, key and store as the old direct
// SizeProbe call, and the probe starts off the caller's path (Start returns
// before the docker call completes).
func TestAsyncSizeProbeParityWithSync(t *testing.T) {
	p := probePayload([]string{"sha256:bbb", "sha256:aaa"})
	key := sizeProbeCacheKey(sizeProbeInputHashes(p))
	if key == "" {
		t.Fatalf("expected a cache key for the parity task")
	}

	var syncCache, asyncCache fakeSizeCache
	SetSizeProbeCache(&syncCache)
	syncDocker := fakeProbeDocker{createOK: true, logs: framedLog(probeDoc)}
	syncDoc := SizeProbe(context.Background(), probeTask(p), probeCreds("harbor.example"), "mod-net", "", "vol-1", &syncDocker)
	SetSizeProbeCache(&asyncCache)
	asyncDocker := fakeProbeDocker{createOK: true, logs: framedLog(probeDoc)}

	// A docker fake whose ContainerWait blocks proves Start is non-blocking:
	// the probe goroutine cannot have completed before Start returns.
	asyncDocker.blockWait = true
	start := time.Now()
	probe := StartAsyncSizeProbe(context.Background(), 42, probeTask(p), probeCreds("harbor.example"), "mod-net", "", "vol-1", &asyncDocker)
	if elapsed := time.Since(start); elapsed > 100*time.Millisecond {
		t.Fatalf("StartAsyncSizeProbe blocked the task path for %v", elapsed)
	}
	if syncDoc == nil {
		t.Fatalf("sync baseline probe returned nil")
	}
	asyncDocker.unblockWait()
	var gotInfo []string
	asyncDoc := FinishSizeProbe(probe, func(msg string, kv ...interface{}) { gotInfo = append(gotInfo, msg) })

	if asyncDoc == nil || asyncDoc.Raw != syncDoc.Raw {
		t.Fatalf("async JSON differs from sync: %+v vs %+v", asyncDoc, syncDoc)
	}
	if len(asyncDoc.Probes) != len(syncDoc.Probes) || asyncDoc.Probes[0].Family != syncDoc.Probes[0].Family {
		t.Fatalf("async decoded doc differs from sync: %+v", asyncDoc.Probes)
	}
	if len(syncCache.stores) != 1 || len(asyncCache.stores) != 1 {
		t.Fatalf("cache store count differs: sync=%d async=%d", len(syncCache.stores), len(asyncCache.stores))
	}
	for k, v := range syncCache.stores {
		if asyncCache.stores[k] != v {
			t.Fatalf("cache JSON differs for %s: sync=%q async=%q", k, v, asyncCache.stores[k])
		}
	}
	if len(syncDocker.calls) != len(asyncDocker.calls) {
		t.Fatalf("docker call sequence differs: %v vs %v", syncDocker.calls, asyncDocker.calls)
	}
	if len(gotInfo) != 1 || gotInfo[0] != "size-probe ok" {
		t.Fatalf("finish log not emitted once: %v", gotInfo)
	}
}

// TestAsyncSizeProbeNeverFailsTask: pull error, create error, non-zero exit,
// junk stdout, cache error, nil docker and no registry all yield a nil probe
// and no cache poison; Finish returns quietly on the nil result.
func TestAsyncSizeProbeNeverFailsTask(t *testing.T) {
	p := probePayload([]string{"sha256:aaa"})
	cases := []*fakeProbeDocker{
		{pullErr: errors.New("no such image")},
		{createOK: false},
		{createOK: true, nonZero: true},
		{createOK: true, logs: framedLog("not json at all")},
		{createOK: true, logs: framedLog(probeDoc)},
	}
	for i, d := range cases {
		var cache fakeSizeCache
		if i == 4 {
			cache.missErr = errors.New("redis down")
		}
		SetSizeProbeCache(&cache)
		probe := StartAsyncSizeProbe(context.Background(), i, probeTask(p), probeCreds("harbor.example"), "mod-net", "", "vol-1", d)
		if doc := FinishSizeProbe(probe, nil); i < 4 && doc != nil {
			t.Fatalf("case %d: a failing probe must return nil, got %+v", i, doc)
		} else if i == 4 && doc == nil {
			t.Fatalf("case 4: a cache error must not turn a successful probe into nil")
		}
		if i < 4 && len(cache.stores) != 0 {
			t.Fatalf("case %d: a failing probe must not poison the cache", i)
		}
	}
	// nil docker
	probe := StartAsyncSizeProbe(context.Background(), 0, probeTask(p), probeCreds("harbor.example"), "mod-net", "", "vol-1", nil)
	if doc := FinishSizeProbe(probe, nil); doc != nil {
		t.Fatalf("nil docker must return nil")
	}
	// no registry (CE posture): the probe skips before pulling anything
	probe = StartAsyncSizeProbe(context.Background(), 0, probeTask(p), probeCreds(""), "mod-net", "", "vol-1", &fakeProbeDocker{createOK: true, logs: framedLog(probeDoc)})
	if doc := FinishSizeProbe(probe, nil); doc != nil {
		t.Fatalf("CE posture must skip the probe")
	}
	// no inputs
	probe = StartAsyncSizeProbe(context.Background(), 0, probeTask(transport.TaskPayload{}), probeCreds("harbor.example"), "mod-net", "", "vol-1", &fakeProbeDocker{createOK: true, logs: framedLog(probeDoc)})
	if doc := FinishSizeProbe(probe, nil); doc != nil {
		t.Fatalf("a task with no inputs must skip the probe")
	}
}

// TestAsyncSizeProbeCancelNoGoroutineLeak: cancellation of the task ctx
// reaches the probe goroutine; it must not outlive the task's finish. The
// goroutine count before/after is compared with a tolerated delta.
func TestAsyncSizeProbeCancelNoGoroutineLeak(t *testing.T) {
	p := probePayload([]string{"sha256:aaa"})
	var cache fakeSizeCache
	SetSizeProbeCache(&cache)
	d := fakeProbeDocker{createOK: true, logs: framedLog(probeDoc), blockWait: true}
	ctx, cancel := context.WithCancel(context.Background())
	probe := StartAsyncSizeProbe(ctx, 7, probeTask(p), probeCreds("harbor.example"), "mod-net", "", "vol-1", &d)
	before := runtime.NumGoroutine()
	cancel()
	// The blocked wait fake is unblocked so the goroutine can drain its channel.
	d.unblockWait()
	_ = FinishSizeProbe(probe, nil)
	time.Sleep(20 * time.Millisecond)
	after := runtime.NumGoroutine()
	if after > before+2 {
		t.Fatalf("probe goroutine leak: before=%d after=%d", before, after)
	}
	if probe.done == nil {
		t.Fatalf("probe done channel missing")
	}
}
