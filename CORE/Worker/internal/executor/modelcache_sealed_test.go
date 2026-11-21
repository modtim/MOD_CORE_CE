package executor

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/modelcache"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// GY.C8 — the worker's model cache must not probe or select the public HF
// upstream when the egress tunables seal it. The tests inject a recording
// prober so any dial of upstreamProbeURL is observable, plus httptest
// servers for the LAN candidates so the local/peer path is exercised for
// real; absent tunables must reproduce today's (unsealed) behaviour exactly.

// recordingProber records every ProbeURL it is asked to measure and answers
// from a per-ProbeURL table (absent = unhealthy).
type recordingProber struct {
	mu     sync.Mutex
	probed []string
	table  map[string]modelcache.Score
}

func newRecordingProber(table map[string]modelcache.Score) *recordingProber {
	return &recordingProber{table: table}
}

func (p *recordingProber) Probe(ctx context.Context, c modelcache.Candidate) modelcache.Score {
	p.mu.Lock()
	p.probed = append(p.probed, c.ProbeURL)
	p.mu.Unlock()
	sc, ok := p.table[c.ProbeURL]
	if !ok {
		sc = modelcache.Score{Class: c.Class, Endpoint: c.Endpoint, Healthy: false, Error: "no probe data"}
	}
	sc.Class = c.Class
	sc.Endpoint = c.Endpoint
	sc.At = time.Now()
	return sc
}

func (p *recordingProber) wasProbed(url string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, u := range p.probed {
		if u == url {
			return true
		}
	}
	return false
}

func (p *recordingProber) count() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.probed)
}

func (p *recordingProber) all() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.probed...)
}

// newSealedTestManager builds a manager with the given server-pushed tunables
// (nil map = nothing pushed, i.e. the unsealed defaults) and the given prober.
func newSealedTestManager(probe modelcache.Prober, tun map[string]interface{}) *modelCacheManager {
	ts := tunables.New()
	if tun != nil {
		ts.Apply(tun)
	}
	return &modelCacheManager{
		log:      slog.Default(),
		tunables: ts,
		selecter: modelcache.New(probe, nil /* no relay probe */, time.Minute),
	}
}

// freshCacheRoot points the local-repo check at an empty temp cache so
// repoIsLocal is false deterministically.
func freshCacheRoot(t *testing.T) {
	t.Helper()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", t.TempDir())
}

func okSiteServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestAllowPublicUpstream(t *testing.T) {
	cases := []struct {
		name   string
		sealed bool
		dl     bool
		want   bool
	}{
		{"unsealed defaults", false, true, true},
		{"sealed master wins", true, true, false},
		{"model downloads off", false, false, false},
		{"both off", true, false, false},
	}
	for _, tc := range cases {
		if got := allowPublicUpstream(tc.sealed, tc.dl); got != tc.want {
			t.Errorf("%s: allowPublicUpstream(%v, %v) = %v, want %v", tc.name, tc.sealed, tc.dl, got, tc.want)
		}
	}
}

// Sealed + a healthy LAN mirror: the mirror is chosen and the public
// upstream is NEVER probed.
func TestSealed_SiteMirrorChosen_NoUpstreamProbe(t *testing.T) {
	site := okSiteServer(t)
	probe := newRecordingProber(map[string]modelcache.Score{
		site.URL + modelcacheProbePath: {Healthy: true, LatencyMS: 3},
	})
	m := newSealedTestManager(probe.Probe, map[string]interface{}{
		egressSealedModeTunableKey:    true,
		modelMirrorEndpointTunableKey: site.URL,
	})
	freshCacheRoot(t)

	endpoint, _, err := m.SelectFor(context.Background(), "", false)
	if err != nil {
		t.Fatalf("SelectFor: %v", err)
	}
	if endpoint != strings.TrimRight(site.URL, "/") {
		t.Fatalf("endpoint = %q, want the site mirror %q", endpoint, site.URL)
	}
	if probe.wasProbed(upstreamProbeURL) {
		t.Fatalf("sealed: the public upstream %s was probed (probed: %v)", upstreamProbeURL, probe.all())
	}
}

// Sealed + a healthy other-site (peer) cache via worker.model_cache_endpoints:
// the peer is eligible and chosen; upstream is never probed.
func TestSealed_PeerEndpointChosen_NoUpstreamProbe(t *testing.T) {
	peer := okSiteServer(t)
	probe := newRecordingProber(map[string]modelcache.Score{
		peer.URL + modelcacheProbePath: {Healthy: true, LatencyMS: 5},
	})
	m := newSealedTestManager(probe.Probe, map[string]interface{}{
		egressSealedModeTunableKey:    true,
		modelCacheEndpointsTunableKey: peer.URL,
	})
	freshCacheRoot(t)

	endpoint, _, err := m.SelectFor(context.Background(), "", false)
	if err != nil {
		t.Fatalf("SelectFor: %v", err)
	}
	if endpoint != strings.TrimRight(peer.URL, "/") {
		t.Fatalf("endpoint = %q, want the peer %q", endpoint, peer.URL)
	}
	if probe.wasProbed(upstreamProbeURL) {
		t.Fatalf("sealed: the public upstream %s was probed (probed: %v)", upstreamProbeURL, probe.all())
	}
}

// Sealed + nothing local/mirrored: the selection FAILS, names both egress
// settings with their live values, and issues zero probes.
func TestSealed_NoLocal_FailsNamingSettings(t *testing.T) {
	probe := newRecordingProber(nil)
	m := newSealedTestManager(probe.Probe, map[string]interface{}{
		egressSealedModeTunableKey: true,
	})
	freshCacheRoot(t)

	_, _, err := m.SelectFor(context.Background(), "org/weights-model", false)
	if !errors.Is(err, ErrModelCacheSealed) {
		t.Fatalf("err = %v, want ErrModelCacheSealed", err)
	}
	for _, want := range []string{
		"egress.sealed_mode=true",
		"egress.model_download_enabled=true",
		"org/weights-model",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not name %q", err.Error(), want)
		}
	}
	if probe.count() != 0 {
		t.Fatalf("sealed: %d probe(s) issued, want none (probed: %v)", probe.count(), probe.all())
	}
}

// egress.model_download_enabled=false (sealed master off) seals the upstream
// the same way: no probe, and the refusal names THAT setting.
func TestModelDownloadDisabled_NoUpstreamProbeAndFails(t *testing.T) {
	probe := newRecordingProber(nil)
	m := newSealedTestManager(probe.Probe, map[string]interface{}{
		egressModelDownloadTunableKey: false,
	})
	freshCacheRoot(t)

	_, _, err := m.SelectFor(context.Background(), "", false)
	if !errors.Is(err, ErrModelCacheSealed) {
		t.Fatalf("err = %v, want ErrModelCacheSealed", err)
	}
	if !strings.Contains(err.Error(), "egress.model_download_enabled=false") {
		t.Errorf("error %q does not name egress.model_download_enabled=false", err.Error())
	}
	if probe.count() != 0 {
		t.Fatalf("model downloads disabled: %d probe(s) issued, want none (probed: %v)", probe.count(), probe.all())
	}
}

// Sealed + the repo is already on disk: the local candidate wins at cost 0 —
// no error, no endpoint, and the selecter never probes the local candidate.
func TestSealed_LocalRepoWins_NoProbes(t *testing.T) {
	root := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", root)
	if err := os.MkdirAll(filepath.Join(root, "models--org--weights-model"), 0o755); err != nil {
		t.Fatalf("mkcache: %v", err)
	}

	probe := newRecordingProber(nil)
	m := newSealedTestManager(probe.Probe, map[string]interface{}{
		egressSealedModeTunableKey: true,
	})

	endpoint, _, err := m.SelectFor(context.Background(), "org/weights-model", false)
	if err != nil {
		t.Fatalf("a local repo must still be selectable when sealed, got: %v", err)
	}
	if endpoint != "" {
		t.Fatalf("endpoint = %q, want empty (serve from the local cache)", endpoint)
	}
	if probe.count() != 0 {
		t.Fatalf("local candidate must not be probed, got %d (probed: %v)", probe.count(), probe.all())
	}
}

// Absent tunables = unsealed defaults: upstream IS probed (its real WAN cost
// is measured) and chosen when nothing else is available — exactly today's
// behaviour, endpoint "" (set no HF_ENDPOINT), no error.
func TestUnsealed_AbsentTunables_CurrentBehaviour(t *testing.T) {
	probe := newRecordingProber(nil) // upstream probe answers unhealthy
	m := newSealedTestManager(probe.Probe, nil)
	freshCacheRoot(t)

	endpoint, _, err := m.SelectFor(context.Background(), "", false)
	if err != nil {
		t.Fatalf("unsealed must never error, got: %v", err)
	}
	if endpoint != "" {
		t.Fatalf("endpoint = %q, want empty (upstream direct)", endpoint)
	}
	if !probe.wasProbed(upstreamProbeURL) {
		t.Fatalf("unsealed: upstream should be probed (current behaviour), probed: %v", probe.all())
	}
}

// Sealed + healthy mirror, production wiring (the REAL HTTP prober): the
// mirror is probed via the httptest server and chosen; there is no upstream
// candidate for the prober to dial, so huggingface.co is never reached.
func TestSealed_RealProber_MirrorOnly(t *testing.T) {
	site := okSiteServer(t)
	ts := tunables.New()
	ts.Apply(map[string]interface{}{
		egressSealedModeTunableKey:    true,
		modelMirrorEndpointTunableKey: site.URL,
	})
	m := newModelCacheManager(slog.Default(), ts, nil /* no site mesh */)
	freshCacheRoot(t)

	endpoint, _, err := m.SelectFor(context.Background(), "", false)
	if err != nil {
		t.Fatalf("SelectFor: %v", err)
	}
	if endpoint != strings.TrimRight(site.URL, "/") {
		t.Fatalf("endpoint = %q, want the mirror %q", endpoint, site.URL)
	}
}

// The background refresher re-measures a candidate set captured BEFORE the
// tunables sealed the install; the stale upstream entry must not be re-probed.
func TestSealed_RefreshDoesNotReProbeStaleUpstream(t *testing.T) {
	probe := newRecordingProber(map[string]modelcache.Score{
		"http://site:8080/": {Healthy: true, LatencyMS: 2},
		upstreamProbeURL:    {Healthy: true, LatencyMS: 90},
	})
	m := newSealedTestManager(probe.Probe, map[string]interface{}{
		egressSealedModeTunableKey: true,
	})
	m.lastCands = []modelcache.Candidate{
		{Class: modelcache.ClassSite, Endpoint: "http://site:8080", ProbeURL: "http://site:8080/"},
		{Class: modelcache.ClassUpstream, Endpoint: "", ProbeURL: upstreamProbeURL},
	}

	m.refreshScores(context.Background())

	if probe.wasProbed(upstreamProbeURL) {
		t.Fatalf("sealed refresh re-probed the public upstream (probed: %v)", probe.all())
	}
	if !probe.wasProbed("http://site:8080/") {
		t.Fatalf("the (non-public) site candidate should still be re-measured (probed: %v)", probe.all())
	}
}

func TestFilterPublicUpstream(t *testing.T) {
	cands := []modelcache.Candidate{
		{Class: modelcache.ClassLocal, Endpoint: ""},
		{Class: modelcache.ClassSite, Endpoint: "http://s:8080", ProbeURL: "http://s:8080/"},
		{Class: modelcache.ClassUpstream, Endpoint: "", ProbeURL: upstreamProbeURL},
		{Class: modelcache.ClassOther, Endpoint: "http://o:8080", ProbeURL: "http://o:8080/"},
	}
	out := filterPublicUpstream(cands)
	if len(out) != 3 {
		t.Fatalf("len(out) = %d, want 3", len(out))
	}
	for _, c := range out {
		if c.Class == modelcache.ClassUpstream {
			t.Fatalf("public upstream survived the filter: %+v", c)
		}
	}
}
