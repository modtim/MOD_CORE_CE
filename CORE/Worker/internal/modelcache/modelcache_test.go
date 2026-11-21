package modelcache

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// fixedNow is a deterministic clock for TTL tests.
var fixedNow = time.Date(2026, 9, 29, 12, 0, 0, 0, time.UTC)

// newTestSelecter wires a Selecter onto a fixed clock and the given probes.
func newTestSelecter(probe Prober, relay RelayProbe) *Selecter {
	s := New(probe, relay, defaultCacheTTL)
	s.now = func() time.Time { return fixedNow }
	return s
}

// canned returns a Prober that answers from a per-Endpoint table.
func canned(table map[string]Score) Prober {
	return func(ctx context.Context, c Candidate) Score {
		sc, ok := table[c.ProbeURL]
		if !ok {
			sc = Score{Healthy: false, Error: "no probe data"}
		}
		return sc
	}
}

var (
	relayedNever = func(ctx context.Context, host string) (bool, error) { return false, nil }
)

// cand is shorthand for a test candidate.
func cand(class, endpoint, probeURL string) Candidate {
	return Candidate{Class: class, Endpoint: endpoint, ProbeURL: probeURL}
}

func TestSelect_LocalBeatsEverythingWhenPresent(t *testing.T) {
	s := newTestSelecter(canned(map[string]Score{
		"http://site:8080/": {Healthy: true, LatencyMS: 1},
		"https://hf.co/":    {Healthy: true, LatencyMS: 80},
		"http://other:8080/": {Healthy: true, LatencyMS: 5},
	}), relayedNever)

	ch, _ := s.Select(context.Background(), []Candidate{
		cand(ClassSite, "http://site:8080", "http://site:8080/"),
		cand(ClassUpstream, "", "https://hf.co/"),
		cand(ClassOther, "http://other:8080", "http://other:8080/"),
		cand(ClassLocal, "", ""),
	})
	// Local (already on disk) is cost 0 — it wins no matter how fast the
	// others look, and it means "do not set HF_ENDPOINT" (use the cache).
	if ch.Class != ClassLocal || ch.Endpoint != "" {
		t.Fatalf("local should win, got %+v", ch)
	}
}

func TestSelect_SelectionOrderByMeasuredCost(t *testing.T) {
	cases := []struct {
		name     string
		costs    map[string]Score // per probeURL
		cands    []Candidate
		wantCls  string
		wantEP   string
	}{
		{
			name: "own site LAN beats upstream WAN",
			costs: map[string]Score{
				"http://site:8080/": {Healthy: true, LatencyMS: 2},
				"https://hf.co/":    {Healthy: true, LatencyMS: 90},
			},
			cands: []Candidate{
				cand(ClassSite, "http://site:8080", "http://site:8080/"),
				cand(ClassUpstream, "", "https://hf.co/"),
			},
			wantCls: ClassSite, wantEP: "http://site:8080",
		},
		{
			name: "upstream wins when the only cache is down",
			costs: map[string]Score{
				"http://site:8080/": {Healthy: false, Error: "dial refused"},
				"https://hf.co/":    {Healthy: true, LatencyMS: 90},
			},
			cands: []Candidate{
				cand(ClassSite, "http://site:8080", "http://site:8080/"),
				cand(ClassUpstream, "", "https://hf.co/"),
			},
			wantCls: ClassUpstream, wantEP: "",
		},
		{
			name: "other site chosen only when faster than upstream",
			costs: map[string]Score{
				"http://other:8080/": {Healthy: true, LatencyMS: 40},
				"https://hf.co/":     {Healthy: true, LatencyMS: 90},
			},
			cands: []Candidate{
				cand(ClassOther, "http://other:8080", "http://other:8080/"),
				cand(ClassUpstream, "", "https://hf.co/"),
			},
			wantCls: ClassOther, wantEP: "http://other:8080",
		},
		{
			name: "other site NOT chosen when slower than upstream",
			costs: map[string]Score{
				"http://other:8080/": {Healthy: true, LatencyMS: 200},
				"https://hf.co/":     {Healthy: true, LatencyMS: 90},
			},
			cands: []Candidate{
				cand(ClassOther, "http://other:8080", "http://other:8080/"),
				cand(ClassUpstream, "", "https://hf.co/"),
			},
			wantCls: ClassUpstream, wantEP: "",
		},
		{
			name: "site beats other site on equal cost (locality tie-break)",
			costs: map[string]Score{
				"http://site:8080/":  {Healthy: true, LatencyMS: 30},
				"http://other:8080/": {Healthy: true, LatencyMS: 30},
				"https://hf.co/":     {Healthy: true, LatencyMS: 90},
			},
			cands: []Candidate{
				cand(ClassSite, "http://site:8080", "http://site:8080/"),
				cand(ClassOther, "http://other:8080", "http://other:8080/"),
				cand(ClassUpstream, "", "https://hf.co/"),
			},
			wantCls: ClassSite, wantEP: "http://site:8080",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := newTestSelecter(canned(tc.costs), relayedNever)
			ch, _ := s.Select(context.Background(), tc.cands)
			if ch.Class != tc.wantCls || ch.Endpoint != tc.wantEP {
				t.Fatalf("got {%s %q}, want {%s %q}", ch.Class, ch.Endpoint, tc.wantCls, tc.wantEP)
			}
		})
	}
}

func TestSelect_DERPRelayedExcludedEvenIfFastest(t *testing.T) {
	// The site cache is the fastest candidate by a wide margin but its path
	// is DERP-relayed — it must lose to a slower, direct upstream.
	costs := map[string]Score{
		"http://site:8080/":  {Healthy: true, LatencyMS: 1},
		"http://other:8080/": {Healthy: true, LatencyMS: 5},
		"https://hf.co/":     {Healthy: true, LatencyMS: 90},
	}
	// hostFromURL yields the hostname without its port.
	relay := func(ctx context.Context, host string) (bool, error) {
		if host == "site" {
			return true, nil
		}
		return false, nil
	}
	s := newTestSelecter(canned(costs), relay)
	ch, scores := s.Select(context.Background(), []Candidate{
		cand(ClassSite, "http://site:8080", "http://site:8080/"),
		cand(ClassOther, "http://other:8080", "http://other:8080/"),
		cand(ClassUpstream, "", "https://hf.co/"),
	})
	if ch.Class != ClassOther || ch.Endpoint != "http://other:8080" {
		t.Fatalf("relayed candidate must be excluded; got %+v", ch)
	}
	// The relay bit must also be visible in the reported scores (heartbeat).
	for _, sc := range scores {
		if sc.Endpoint == "http://site:8080" {
			if !sc.Relayed {
				t.Fatalf("site score missing relayed bit: %+v", sc)
			}
		}
	}
}

func TestSelect_ALLRelayedFallsBackToUpstream(t *testing.T) {
	// Every cache is relayed and upstream itself is also relayed: nothing is
	// eligible but upstream is always eligible as the fallback — the answer is
	// "do not set HF_ENDPOINT" (go direct), never a relayed cache.
	costs := map[string]Score{
		"http://site:8080/":  {Healthy: true, LatencyMS: 1},
		"http://other:8080/": {Healthy: true, LatencyMS: 2},
		"https://hf.co/":     {Healthy: true, LatencyMS: 90},
	}
	relay := func(ctx context.Context, host string) (bool, error) { return true, nil }
	s := newTestSelecter(canned(costs), relay)
	ch, _ := s.Select(context.Background(), []Candidate{
		cand(ClassSite, "http://site:8080", "http://site:8080/"),
		cand(ClassOther, "http://other:8080", "http://other:8080/"),
		cand(ClassUpstream, "", "https://hf.co/"),
	})
	if ch.Class != ClassUpstream || ch.Endpoint != "" {
		t.Fatalf("all-relayed must fall back to upstream direct; got %+v", ch)
	}
}

func TestSelect_UnhealthySkipped(t *testing.T) {
	cases := []struct {
		name   string
		costs  map[string]Score
		want   string // class
		wantEP string // endpoint to set ("" = do not set HF_ENDPOINT)
	}{
		{
			name: "site down, other healthy -> other",
			costs: map[string]Score{
				"http://site:8080/":  {Healthy: false, Error: "connection refused"},
				"http://other:8080/": {Healthy: true, LatencyMS: 10},
				"https://hf.co/":     {Healthy: true, LatencyMS: 90},
			},
			want: ClassOther, wantEP: "http://other:8080",
		},
		{
			name: "all caches down, upstream healthy -> upstream",
			costs: map[string]Score{
				"http://site:8080/":  {Healthy: false, Error: "timeout"},
				"http://other:8080/": {Healthy: false, Error: "timeout"},
				"https://hf.co/":     {Healthy: true, LatencyMS: 90},
			},
			want: ClassUpstream, wantEP: "",
		},
		{
			name: "everything down -> still upstream (do not set endpoint)",
			costs: map[string]Score{
				"http://site:8080/":  {Healthy: false, Error: "timeout"},
				"http://other:8080/": {Healthy: false, Error: "timeout"},
				"https://hf.co/":     {Healthy: false, Error: "timeout"},
			},
			want: ClassUpstream, wantEP: "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := newTestSelecter(canned(tc.costs), relayedNever)
			ch, _ := s.Select(context.Background(), []Candidate{
				cand(ClassSite, "http://site:8080", "http://site:8080/"),
				cand(ClassOther, "http://other:8080", "http://other:8080/"),
				cand(ClassUpstream, "", "https://hf.co/"),
			})
			if ch.Class != tc.want || ch.Endpoint != tc.wantEP {
				t.Fatalf("got {%s %q}, want {%s %q}", ch.Class, ch.Endpoint, tc.want, tc.wantEP)
			}
		})
	}
}

func TestSelect_NoCandidatesIsUpstream(t *testing.T) {
	s := newTestSelecter(canned(nil), relayedNever)
	ch, scores := s.Select(context.Background(), nil)
	if ch.Class != ClassUpstream || ch.Endpoint != "" || len(scores) != 0 {
		t.Fatalf("empty candidate set must yield upstream direct; got %+v scores=%d", ch, len(scores))
	}
}

func TestSelect_RelayProbeErrorDoesNotExclude(t *testing.T) {
	// A relay probe that cannot tell (error) must NOT exclude a candidate —
	// we only exclude on a positive relay verdict.
	costs := map[string]Score{
		"http://site:8080/": {Healthy: true, LatencyMS: 2},
		"https://hf.co/":    {Healthy: true, LatencyMS: 90},
	}
	relay := func(ctx context.Context, host string) (bool, error) {
		return false, errors.New("no tailscale state")
	}
	s := newTestSelecter(canned(costs), relay)
	ch, _ := s.Select(context.Background(), []Candidate{
		cand(ClassSite, "http://site:8080", "http://site:8080/"),
		cand(ClassUpstream, "", "https://hf.co/"),
	})
	if ch.Class != ClassSite {
		t.Fatalf("relay probe error must not exclude; got %+v", ch)
	}
}

func TestScores_CachedWithinTTL(t *testing.T) {
	var probes int
	probe := func(ctx context.Context, c Candidate) Score {
		probes++
		return Score{Healthy: true, LatencyMS: 3}
	}
	s := New(probe, nil, defaultCacheTTL)
	s.now = func() time.Time { return fixedNow }
	c := []Candidate{cand(ClassSite, "http://site:8080", "http://site:8080/")}

	s.Select(context.Background(), c)
	s.Select(context.Background(), c)
	if probes != 1 {
		t.Fatalf("expected 1 probe within the TTL, got %d", probes)
	}

	// Advance past the TTL: the next Select must re-probe.
	s.now = func() time.Time { return fixedNow.Add(defaultCacheTTL + time.Second) }
	s.Select(context.Background(), c)
	if probes != 2 {
		t.Fatalf("expected re-probe after the TTL expired, got %d probes", probes)
	}
}

func TestScores_LocalNeverProbed(t *testing.T) {
	var probes int
	probe := func(ctx context.Context, c Candidate) Score {
		probes++
		return Score{Healthy: true, LatencyMS: 1}
	}
	s := newTestSelecter(probe, relayedNever)
	_, scores := s.Select(context.Background(), []Candidate{
		cand(ClassLocal, "", ""),
		cand(ClassSite, "http://site:8080", "http://site:8080/"),
	})
	if probes != 1 {
		t.Fatalf("local must never be probed; probed %d times", probes)
	}
	var localOK bool
	for _, sc := range scores {
		if sc.Class == ClassLocal {
			localOK = true
			if !sc.Healthy || sc.LatencyMS != 0 {
				t.Fatalf("local score must be healthy/0ms: %+v", sc)
			}
		}
	}
	if !localOK {
		t.Fatal("local candidate missing from reported scores")
	}
}

// TestHTTPProber exercises the real HTTP prober against an httptest server:
// a 206 partial read is healthy with a measured latency/throughput; a 404 is
// unhealthy; an unreachable host is unhealthy.
func TestHTTPProber_HappyPathAndErrors(t *testing.T) {
	body := make([]byte, probeBytes)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Range", "bytes 0-255/1000")
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(body[:256])
	}))
	defer srv.Close()

	s := New(nil, nil, defaultCacheTTL)
	probe := NewHTTPProber(s.HTTPClient(), "/")
	sc := probe(context.Background(), cand(ClassSite, "http://site", srv.URL+"/"))
	if !sc.Healthy {
		t.Fatalf("expected healthy probe of %s: %+v", srv.URL, sc)
	}
	if sc.LatencyMS < 0 {
		t.Fatalf("negative latency: %+v", sc)
	}

	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	}))
	defer bad.Close()
	sc = probe(context.Background(), cand(ClassSite, "http://site", bad.URL+"/"))
	if sc.Healthy {
		t.Fatalf("404 must be unhealthy: %+v", sc)
	}

	sc = probe(context.Background(), cand(ClassSite, "http://site", "http://127.0.0.1:1/"))
	if sc.Healthy {
		t.Fatalf("unreachable host must be unhealthy: %+v", sc)
	}

	sc = probe(context.Background(), cand(ClassLocal, "", ""))
	if sc.Healthy {
		t.Fatalf("empty probe url must be unhealthy: %+v", sc)
	}
}

func TestThroughputMBPS(t *testing.T) {
	if got := throughputMBPS(1024*1024, time.Second); got != 1 {
		t.Fatalf("1 MiB / 1s = %v, want 1", got)
	}
	if got := throughputMBPS(0, time.Second); got != 0 {
		t.Fatalf("zero bytes = %v, want 0", got)
	}
	if got := throughputMBPS(1024*1024, 0); got != 0 {
		t.Fatalf("zero elapsed = %v, want 0 (unknown, not infinite)", got)
	}
}
