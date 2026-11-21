// Package modelcache picks, per download, which model-cache source a worker's
// HF clients should resolve through (GF.C2).
//
// The worker does not trust a single hard-wired mirror: it MEASURES a set of
// candidates — its own on-disk cache ("local"), its site's cache(s)
// ("site"), the upstream hub ("upstream"), and other sites' caches
// ("other") — with a cheap health + latency/throughput probe, caches the
// result for a few minutes, and points HF downloads at the lowest measured
// cost. The ordering the measurement naturally produces is exactly the one we
// want: local (0) → own-site (LAN) → upstream (WAN) → another site only when
// it is in fact faster than upstream. A candidate whose path is DERP-relayed
// is never chosen, no matter how fast it looks, and an unhealthy candidate is
// skipped. When nothing is eligible the answer is upstream — i.e. do NOT set
// HF_ENDPOINT at all, and let huggingface_hub go direct.
//
// The probe and the relay check are injectable (Prober / RelayProbe) so the
// selection and its exclusion rules are unit-testable without a network or a
// tailscale client.
package modelcache

import (
	"context"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

// Candidate classes. The class is a label for reporting and tie-breaking; the
// actual decision is the measured cost. Endpoint is what gets set as
// HF_ENDPOINT when the candidate wins; an empty Endpoint means "do not set it"
// (local relies on the on-disk cache, upstream is the huggingface_hub default).
const (
	ClassLocal    = "local"
	ClassSite     = "site"
	ClassUpstream = "upstream"
	ClassOther    = "other"
)

// defaultCacheTTL is how long a measured score stays authoritative before the
// next probe refreshes it. A few minutes: long enough that a burst of
// downloads does not re-probe on every one, short enough that a cache going
// down is noticed within one or two download cycles.
const defaultCacheTTL = 5 * time.Minute

// Candidate is one source HF traffic could be pointed at.
type Candidate struct {
	// Class is one of ClassLocal/ClassSite/ClassUpstream/ClassOther.
	Class string
	// Endpoint is the value to set as HF_ENDPOINT when this candidate wins.
	// Empty for local (use the on-disk cache) and upstream (the hub default —
	// setting it would be redundant and must be avoided).
	Endpoint string
	// ProbeURL is what the prober dials to measure health/latency. Empty for
	// local (no network — the score is synthetic). For upstream it is the
	// public hub root so its real WAN latency is measured even though choosing
	// it still means "don't set HF_ENDPOINT".
	ProbeURL string
}

// Score is the measured state of one candidate.
type Score struct {
	Class    string `json:"class"`
	Endpoint string `json:"endpoint,omitempty"`
	// Healthy is true when the probe succeeded. A local candidate is always
	// healthy (the disk is not a network).
	Healthy bool `json:"healthy"`
	// LatencyMS is the round-trip of the probe; 0 for local.
	LatencyMS float64 `json:"latency_ms"`
	// ThroughputMBPS is a best-effort throughput estimate of the probe read;
	// 0 when it could not be measured (treated as "unknown", not "fast").
	ThroughputMBPS float64 `json:"throughput_mbps,omitempty"`
	// Relayed means the path to this candidate went over a DERP relay (mesh
	// hairpin) rather than a direct connection. Such a path is never selected
	// while a non-relayed alternative exists, regardless of raw numbers.
	Relayed bool `json:"relayed,omitempty"`
	// Error names a probe failure (never a secret); empty when healthy.
	Error string `json:"error,omitempty"`
	// At is when the score was last measured.
	At time.Time `json:"at"`
}

// Prober measures one candidate. The returned Score must carry Class,
// Endpoint, Healthy, LatencyMS and (where relevant) ThroughputMBPS. The
// Relayed bit is owned by the RelayProbe, not the prober — a prober that does
// not understand tailscale simply leaves it false and the relay check fills
// it in. Implementations are responsible for applying their own timeout; the
// caller will not wait forever.
type Prober func(ctx context.Context, c Candidate) Score

// RelayProbe reports whether the path to host (a hostname or IP) is currently
// DERP-relayed. It is the authoritative source of Score.Relayed, consulted to
// stamp the exclusion on top of the prober's measurement. An error must be
// read as "cannot tell", i.e. NOT relayed — we do not exclude a candidate on
// uncertainty, only on a positive relay verdict.
type RelayProbe func(ctx context.Context, host string) (bool, error)

// Selecter holds a short-lived cache of measured scores and the probes it
// uses. It is safe for concurrent use. The zero value is not usable — use New.
type Selecter struct {
	probe   Prober
	relay   RelayProbe
	ttl     time.Duration
	now     func() time.Time
	httpClt *http.Client

	mu    sync.Mutex
	cache map[string]Score
}

// New returns a Selecter that uses probe for measurement and relay for the
// DERP exclusion (either may be nil — nil probe means "everything unhealthy",
// which collapses to the upstream fallback; nil relay means "never treat
// anything as relayed"). ttl <= 0 uses defaultCacheTTL.
func New(probe Prober, relay RelayProbe, ttl time.Duration) *Selecter {
	if ttl <= 0 {
		ttl = defaultCacheTTL
	}
	return &Selecter{
		probe: probe,
		relay: relay,
		ttl:   ttl,
		now:   time.Now,
		// A probe must never stall a download. This is the outer bound; a
		// Prober may set a tighter per-request deadline.
		httpClt: &http.Client{Timeout: 5 * time.Second},
		cache:   make(map[string]Score),
	}
}

// HTTPClient returns the shared probe client (used by the default HTTP prober
// in the executor, and by tests that point it at an httptest server).
func (s *Selecter) HTTPClient() *http.Client { return s.httpClt }

// cacheKey namespaces a score by what it describes so a local and an upstream
// (both Endpoint=="") never collide, and the same endpoint under two classes
// keeps distinct rows.
func cacheKey(c Candidate) string {
	return c.Class + "\x00" + c.Endpoint + "\x00" + c.ProbeURL
}

// scores returns a fresh Score for each candidate, reusing the cache while it
// is younger than the TTL. Local candidates are never probed (synthetic
// perfect score). Everything else goes through s.probe; s.relay then fills the
// authoritative Relayed bit from the target host.
func (s *Selecter) scores(ctx context.Context, cands []Candidate) map[string]Score {
	now := s.now()
	out := make(map[string]Score, len(cands))
	for _, c := range cands {
		k := cacheKey(c)
		if c.Class == ClassLocal {
			out[k] = Score{Class: c.Class, Endpoint: c.Endpoint, Healthy: true, LatencyMS: 0, At: now}
			continue
		}
		s.mu.Lock()
		cached, ok := s.cache[k]
		stale := !ok || now.Sub(cached.At) > s.ttl
		s.mu.Unlock()
		if !stale {
			out[k] = cached
			continue
		}
		sc := s.measure(ctx, c)
		// The cache clock is owned by the Selecter, not the prober: a prober
		// that forgets to set At (or sets a stale one) must not make the TTL
		// read as expired on the very next call.
		sc.At = now
		s.mu.Lock()
		s.cache[k] = sc
		s.mu.Unlock()
		out[k] = sc
	}
	return out
}

// measure runs the probe, then lets the relay probe decide the authoritative
// Relayed bit for the target host.
func (s *Selecter) measure(ctx context.Context, c Candidate) Score {
	if s.probe == nil {
		return Score{Class: c.Class, Endpoint: c.Endpoint, Healthy: false, Error: "no prober", At: s.now()}
	}
	sc := s.probe(ctx, c)
	sc.Class = c.Class
	if sc.Endpoint == "" {
		sc.Endpoint = c.Endpoint
	}
	if s.relay != nil && c.ProbeURL != "" {
		if host := hostFromURL(c.ProbeURL); host != "" {
			if r, err := s.relay(ctx, host); err == nil {
				sc.Relayed = r
			}
			// On error we keep the (false) prober value — uncertainty does
			// not exclude a candidate.
		}
	}
	return sc
}

// Choice is the result of Select.
type Choice struct {
	// Endpoint is what to set as HF_ENDPOINT. Empty means "do not set it" —
	// either local (use the on-disk cache) or upstream (the hub default).
	Endpoint string
	// Class is the class of the candidate that won (one of the Class* consts).
	Class string
	// Reason is a short, human-readable explanation for logs/heartbeat.
	Reason string
}

// Select picks the best candidate and returns it along with the latest score
// for every candidate (for the heartbeat). It never returns an error: the
// worst case is the upstream choice (do not set HF_ENDPOINT), which is always
// a valid, working configuration.
//
// Eligibility and cost:
//   - local (when the caller includes it — i.e. the repo is already on disk)
//     is always eligible at cost 0 and therefore wins outright: no endpoint is
//     set and the download is served from the local cache.
//   - any other candidate is eligible only when it is healthy AND not
//     DERP-relayed. A relayed path is never chosen no matter how fast it looks,
//     and an unhealthy one is skipped.
//   - upstream is ALWAYS eligible: it is the fallback. Choosing it means "do
//     not set HF_ENDPOINT" (let huggingface_hub go direct). Its cost is its
//     measured latency when healthy and +Inf when it could not be probed, so a
//     healthy non-relayed cache still wins over an unreachable hub, yet a hub
//     that is merely slower than a cache is correctly preferred.
//
// Among the eligible, the lowest measured cost wins; an exact cost tie breaks
// toward the more local class (locality / no egress cost).
func (s *Selecter) Select(ctx context.Context, cands []Candidate) (Choice, []Score) {
	scores := s.scores(ctx, cands)

	// All scores, in candidate order, for reporting.
	all := make([]Score, 0, len(cands))
	pool := make([]scored, 0, len(cands))
	for _, c := range cands {
		sc := scores[cacheKey(c)]
		all = append(all, sc)
		if c.Class == ClassUpstream {
			pool = append(pool, scored{c: c, sc: sc}) // always eligible
			continue
		}
		if !sc.Healthy || sc.Relayed {
			continue
		}
		pool = append(pool, scored{c: c, sc: sc})
	}

	if len(pool) == 0 {
		// No upstream candidate was offered and nothing else is eligible:
		// the only safe action is "set nothing" (go direct).
		return upstreamChoice(cands), all
	}

	sort.SliceStable(pool, func(i, j int) bool {
		ri, rj := rank(pool[i].c, pool[i].sc), rank(pool[j].c, pool[j].sc)
		if ri != rj {
			return ri < rj
		}
		return classPriority(pool[i].c.Class) < classPriority(pool[j].c.Class)
	})
	best := pool[0]
	reason := "lowest measured cost"
	switch {
	case best.c.Class == ClassLocal:
		reason = "already local"
	case best.c.Class == ClassUpstream && (!best.sc.Healthy || len(pool) == 1):
		reason = "no healthy non-relayed cache — using upstream"
	}
	return Choice{Endpoint: best.c.Endpoint, Class: best.c.Class, Reason: reason}, all
}

type scored struct {
	c  Candidate
	sc Score
}

// rank is the measured cost: lower is better. local is always 0. An unhealthy
// non-upstream candidate is +Inf (it would not be in the pool, but this keeps
// the function total). upstream uses its measured latency when healthy and +Inf
// when it could not be probed, so an unreachable hub still loses to a healthy
// cache but remains the fallback when nothing else is usable. Among the
// healthy, a higher measured throughput nudges the rank down by a fraction of
// a millisecond — enough to break a tie, never enough to overcome latency.
func rank(c Candidate, sc Score) float64 {
	if c.Class == ClassLocal {
		return 0
	}
	if !sc.Healthy {
		return math.Inf(1)
	}
	lat := sc.LatencyMS
	if lat < 0 {
		lat = 0
	}
	return lat - sc.ThroughputMBPS*1e-6
}

// classPriority is a tie-breaker for equal measured cost: prefer a local
// source, then our own site, then upstream, then another site — locality and
// no-egress-cost bias on an exact tie.
func classPriority(class string) int {
	switch class {
	case ClassLocal:
		return 0
	case ClassSite:
		return 1
	case ClassUpstream:
		return 2
	case ClassOther:
		return 3
	default:
		return 4
	}
}

// upstreamChoice returns the "do not set HF_ENDPOINT" choice. It names the
// upstream class regardless of whether an upstream candidate was offered, since
// "go direct to the hub" is the same action either way.
func upstreamChoice(cands []Candidate) Choice {
	return Choice{Endpoint: "", Class: ClassUpstream, Reason: "no healthy non-relayed cache — using upstream"}
}

// hostFromURL extracts a dialable host (hostname or IP) from a URL, or "" when
// it cannot be parsed.
func hostFromURL(u string) string {
	parsed, err := url.Parse(strings.TrimSpace(u))
	if err != nil || parsed.Host == "" {
		return ""
	}
	return parsed.Hostname()
}
