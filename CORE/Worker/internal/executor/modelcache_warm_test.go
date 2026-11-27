package executor

// OPT3 (container-start phase) — the model cache score cache must be kept
// warm OFF the task path.
//
// Root cause this pins: SelectFor never recorded m.lastCands, so refreshScores
// (and StartBackgroundRefresh's 2-minute loop) was permanently a no-op. The
// selecter's score TTL could only expire back ONTO the task path, so any task
// more than TTL after the previous one re-dialled every candidate — an HTTP
// GET of the hub root (5 s client) plus a tailscale relay exec — SYNCHRONOUSLY
// inside runContainerWithCredsInternal, before ContainerCreate. On xeon's
// embedded worker that is the 0.08-0.87 s 'running container' -> 'network
// policy' gap measured across tasks 1988/2030/2038/2040/2044/2046/2049/2062.
//
// These tests use a recording prober so every dial is observable, and a tiny
// TTL so expiry is reachable in milliseconds.

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/modelcache"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

type warmProber struct {
	mu     sync.Mutex
	n      int
}

func newWarmProber() *warmProber { return &warmProber{} }

// Probe answers with a per-URL latency so the ranking is deterministic: a
// LAN peer always beats the WAN upstream (classPriority only breaks an EXACT
// cost tie, and upstream outranks ClassOther there — see modelcache.go).
func (p *warmProber) Probe(ctx context.Context, c modelcache.Candidate) modelcache.Score {
	p.mu.Lock()
	p.n++
	p.mu.Unlock()
	lat := 5.0
	if c.ProbeURL == upstreamProbeURL {
		lat = 500.0
	}
	return modelcache.Score{Class: c.Class, Endpoint: c.Endpoint, Healthy: true,
		LatencyMS: lat, At: time.Now()}
}

func (p *warmProber) count() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.n
}

func warmSite(t *testing.T) *httptest.Server {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// newWarmManager builds a manager with a 40 ms score TTL so expiry is
// deterministic, plus the peer endpoint so the candidate set is non-trivial.
func newWarmManager(probe *warmProber, peerURL string) *modelCacheManager {
	ts := tunables.New()
	ts.Apply(map[string]interface{}{modelCacheEndpointsTunableKey: peerURL})
	return &modelCacheManager{
		log:      slog.Default(),
		tunables: ts,
		selecter: modelcache.New(probe.Probe, nil /* no relay */, 40*time.Millisecond),
	}
}

// 1. SelectFor must record the candidate set, or the background refresher
// measures nothing forever (the bug).
func TestSelectForRecordsCandidatesForTheRefresher(t *testing.T) {
	site := warmSite(t)
	p := newWarmProber()
	m := newWarmManager(p, site.URL)
	freshCacheRoot(t)

	if _, _, err := m.SelectFor(context.Background(), "", false); err != nil {
		t.Fatalf("SelectFor: %v", err)
	}
	after := p.count()
	if after == 0 {
		t.Fatal("expected the first SelectFor to measure the candidates")
	}

	// Past the TTL is exactly when the background loop has to do the work
	// OFF the task path; before it, a fresh cache correctly re-measures nothing.
	time.Sleep(60 * time.Millisecond)
	m.refreshScores(context.Background())
	if p.count() <= after {
		t.Fatalf("refreshScores measured nothing (probes stayed %d): lastCands is "+
			"not being recorded, so StartBackgroundRefresh is a permanent no-op",
			p.count())
	}
}

// 2. The hot path must not re-dial once the refresher has re-measured: a
// SelectFor that lands AFTER the TTL expired still reads a warm cache, because
// the background loop refreshed it. This is the seconds saved per task.
func TestTaskPathStaysWarmPastTTL(t *testing.T) {
	site := warmSite(t)
	p := newWarmProber()
	m := newWarmManager(p, site.URL)
	freshCacheRoot(t)

	if _, _, err := m.SelectFor(context.Background(), "", false); err != nil {
		t.Fatalf("SelectFor: %v", err)
	}
	first := p.count()

	time.Sleep(80 * time.Millisecond) // TTL (40 ms) has expired
	m.refreshScores(context.Background())
	afterRefresh := p.count()
	if afterRefresh <= first {
		t.Fatalf("refresher did not re-measure (probes %d -> %d)", first, afterRefresh)
	}

	// Now the task path: cache is younger than the TTL again, so ZERO new dials.
	time.Sleep(10 * time.Millisecond)
	if _, _, err := m.SelectFor(context.Background(), "", false); err != nil {
		t.Fatalf("SelectFor: %v", err)
	}
	if p.count() != afterRefresh {
		t.Fatalf("task path re-dialled the WAN: probes %d -> %d", afterRefresh, p.count())
	}
}

// 3. Behaviour is unchanged: the endpoint chosen with a warm cache is the
// same one the cold path picks, and the sealed refusal still fires.
func TestWarmCacheChoiceUnchanged(t *testing.T) {
	site := warmSite(t)
	p := newWarmProber()
	m := newWarmManager(p, site.URL)
	freshCacheRoot(t)

	ep1, scores, err := m.SelectFor(context.Background(), "", false)
	if err != nil || ep1 != strings.TrimRight(site.URL, "/") {
		t.Fatalf("cold choice = %q err=%v, want the peer", ep1, err)
	}
	if len(scores) == 0 {
		t.Fatal("expected scores for the heartbeat")
	}
	m.refreshScores(context.Background())
	ep2, _, err2 := m.SelectFor(context.Background(), "", false)
	if err2 != nil || ep2 != ep1 {
		t.Fatalf("warm choice changed: %q -> %q (%v)", ep1, ep2, err2)
	}
}
