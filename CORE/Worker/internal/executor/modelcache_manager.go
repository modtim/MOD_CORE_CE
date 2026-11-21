package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types/container"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/modelcache"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/weights"
	"gopkg.in/yaml.v3"
)

// GF.C2 — per-download model-cache selection + HF_ENDPOINT injection.
//
// The worker picks which model-cache source a download should resolve through
// by MEASURING the candidates it can actually reach, rather than trusting one
// hard-wired mirror:
//
//   - local    — the repo is already fully present in this host's HF cache
//     (weights.RepoDir exists). Cost 0; wins outright and means "set no
//     HF_ENDPOINT" (the cache serves it).
//   - site     — the platform's own model mirror, delivered as the
//     worker.model_mirror_endpoint tunable (GF.C0b.9, the same value the API
//     already writes into managed stacks). LAN on a same-site worker.
//   - upstream — the public hub. ALWAYS eligible unless sealed (GY.C8: the
//     egress.sealed_mode / egress.model_download_enabled tunables from the
//     API) — when sealed it is never probed and never selected, and a
//     download with no local candidate fails with the settings named.
//     Unsealed, choosing it means "set no HF_ENDPOINT" and let huggingface_hub
//     go direct. This is the health-checked fallback when every cache is down
//     or relayed.
//   - other    — another site's cache. Only ever chosen when its measured cost
//     beats upstream. (Exposed via the worker.model_cache_endpoints tunable;
//     the API does not populate it yet, so it is inert until then.)
//
// A candidate whose path is DERP-relayed is never selected no matter how fast
// it looks, and an unhealthy one is skipped. The decision is cached for a few
// minutes (modelcache default TTL) so a burst of downloads does not re-probe on
// every one. The latest scores ride the heartbeat as `model_cache_scores`
// (additive; the API's heartbeat schema has no extra="forbid", so it is simply
// ignored server-side today).
//
// Injection (see injectHFEndpoint / injectHFEndpointMap / applyModelCacheEnv):
// HF_ENDPOINT=<chosen> and HF_HUB_DISABLE_XET=1 are added to node containers
// and stack services ONLY when the spec does not already set HF_ENDPOINT — a
// value the API or an operator already pinned always wins, so the worker never
// double-injects or overrides it.
//
// GF.C6.5 — tenant model-cache token swap: whenever the EFFECTIVE endpoint
// (an API/operator-pinned HF_ENDPOINT when present, else the chosen one) is
// one of the known MOD model-cache endpoints, the container's HF_TOKEN is
// set-or-replaced with the per-task tenant token from the /credentials grant
// (transport.Credentials.ModelCacheToken). The cache verifies that token,
// resolves the tenant's REAL HF token itself, and stamps the download with
// tenant attribution — the tenant's raw HF token never ships to a container.
// When the effective endpoint is the public upstream or local, HF_TOKEN is
// left untouched so the tenant's own token passes through to huggingface.co
// as-is. A gated download (container/stack declares an HF_TOKEN) is never
// routed through another site's cache — the other-site candidates are
// dropped from the selection for it (cross-site gated guard).

// Tunable keys (GS.PSR.7). model_mirror_endpoint is the pre-existing GF.C0b.9
// key; model_cache_endpoints is the additive channel for other-site caches.
const (
	modelCacheEndpointsTunableKey = "worker.model_cache_endpoints"
)

// GY.C8 — egress tunables that seal the public model upstream (the same
// platform settings the API gates its own egress with, utils/egress_policy.go).
// Both are read fresh on every selection; absent (the worker binary predates
// this, or the admin never set them) means unsealed — exactly the current
// behaviour.
const (
	egressSealedModeTunableKey    = "egress.sealed_mode"
	egressModelDownloadTunableKey = "egress.model_download_enabled"
)

// allowPublicUpstream is the pure sealed-mode decision: the public model
// upstream (huggingface.co) may be probed and selected as a model-cache
// candidate only when the install is NOT sealed AND the model-download
// category is enabled.
func allowPublicUpstream(sealed, modelDownloadEnabled bool) bool {
	return !sealed && modelDownloadEnabled
}

// ErrModelCacheSealed marks the refusal returned when the public upstream is
// disabled by the egress tunables and no local model-cache candidate is
// available. Task/deploy paths use errors.Is to fail loudly (the wrapped
// message names the settings in effect) instead of falling through to the
// public hub.
var ErrModelCacheSealed = errors.New("model cache: public model upstream disabled by egress settings")

// modelCacheRefresh is how often the background refresher re-measures the last
// known candidates. It is decoupled from individual downloads: the download
// path reuses a still-fresh cache, and this loop keeps the heartbeat scores
// honest so a cache that goes down between downloads is still reported as
// down (and the next download falls back to upstream).
const modelCacheRefresh = 2 * time.Minute

// modelCacheManager ties the modelcache selecter to this executor: it builds
// the candidate set, drives the (injectable) probes, and caches the latest
// choice + scores for the env-injection helpers and the heartbeat.
type modelCacheManager struct {
	log      *slog.Logger
	tunables *tunables.Store
	selecter *modelcache.Selecter
	relay    modelcache.RelayProbe
	tsRunner tsStatusRunner // nil → relay probe reports "not relayed" (cannot tell)

	mu         sync.Mutex
	lastCands  []modelcache.Candidate
	lastChoice modelcache.Choice
	lastScores []modelcache.Score
	lastAt     time.Time
}

// tsStatusRunner returns the raw `tailscale status --json` output from the
// site-mesh container, or an error when the site client is not running. It is
// a func (not an interface) so the relay probe can be fully exercised in tests
// without a docker daemon.
type tsStatusRunner func(ctx context.Context) ([]byte, error)

// newModelCacheManager wires a manager. The HTTP prober uses the selecter's
// shared client; the relay probe is backed by the site-mesh tailscale status
// (tsRunner, which may be nil — then the probe reports "not relayed", since
// uncertainty never excludes a candidate, only a positive tailnet-but-offline
// verdict does).
func newModelCacheManager(log *slog.Logger, tunables *tunables.Store, tsRunner tsStatusRunner) *modelCacheManager {
	prober := modelcache.NewHTTPProber(nil /* default client */, modelcacheProbePath)
	m := &modelCacheManager{
		log:      log,
		tunables: tunables,
		relay:    tailscaleRelayProbe(tsRunner),
		tsRunner: tsRunner,
	}
	m.selecter = modelcache.New(prober, m.relay, 0 /* default TTL */)
	return m
}

// modelcacheProbePath is the small path probed on each cache endpoint. "/" is
// the common denominator: the platform mirror and the hub both answer the root
// fast, and a deeper repo path would 404 on a cache that has not seen that
// repo.
const modelcacheProbePath = "/"

// SelectFor resolves the best endpoint for a download of repoID ("" when the
// caller has no specific repo). It returns the HF_ENDPOINT value to inject (""
// = set nothing: local cache or upstream direct) and the latest scores for the
// heartbeat. Unsealed it never errors: the worst outcome is the upstream
// choice. Sealed (GY.C8) it errors — ErrModelCacheSealed, wrapped with the
// offending settings — whenever the choice is the public upstream, because
// "set nothing" would send the download to huggingface.co.
//
// gated marks a download that declares an HF_TOKEN (a tenant's raw token, i.e.
// a token-requiring repo): for those, another site's cache is NEVER a
// candidate — gated bytes must never be pulled cross-site (GF.C6.5).
func (m *modelCacheManager) SelectFor(ctx context.Context, repoID string, gated bool) (string, []modelcache.Score, error) {
	ch, scores := m.choose(ctx, m.candidates(repoID, gated))
	if !m.publicUpstreamAllowed() && ch.Class == modelcache.ClassUpstream {
		// Choosing upstream = "set no HF_ENDPOINT" = go direct to
		// huggingface.co. A sealed install must fail loudly instead.
		return "", scores, m.sealedUpstreamError(repoID)
	}
	m.mu.Lock()
	m.lastChoice = ch
	m.lastScores = scores
	m.lastAt = time.Now()
	m.mu.Unlock()
	return ch.Endpoint, scores, nil
}

// sealedUpstreamError builds the refusal, naming the exact egress settings in
// effect so the operator knows which switch to move.
func (m *modelCacheManager) sealedUpstreamError(repoID string) error {
	sealed, dl := false, true
	if m.tunables != nil {
		sealed = m.tunables.Bool(egressSealedModeTunableKey, "", false)
		dl = m.tunables.Bool(egressModelDownloadTunableKey, "", true)
	}
	what := "the requested model"
	if repoID != "" {
		what = fmt.Sprintf("repo %q", repoID)
	}
	return fmt.Errorf("%w: %s=%t, %s=%t — %s is not available from the local model cache",
		ErrModelCacheSealed,
		egressSealedModeTunableKey, sealed,
		egressModelDownloadTunableKey, dl,
		what)
}

// publicUpstreamAllowed reads the two egress tunables fresh from the store so
// a mid-run flip applies on the next selection. Absent values mean the
// registry defaults (sealed off, model downloads on) — unsealed.
func (m *modelCacheManager) publicUpstreamAllowed() bool {
	sealed, dl := false, true
	if m.tunables != nil {
		sealed = m.tunables.Bool(egressSealedModeTunableKey, "", false)
		dl = m.tunables.Bool(egressModelDownloadTunableKey, "", true)
	}
	return allowPublicUpstream(sealed, dl)
}

// choose builds the candidate set and asks the selecter. Split out so
// refreshScores can re-measure the same set on a timer.
func (m *modelCacheManager) choose(ctx context.Context, cands []modelcache.Candidate) (modelcache.Choice, []modelcache.Score) {
	if len(cands) == 0 {
		// Nothing to measure — upstream direct is the only sane answer.
		return modelcache.Choice{Endpoint: "", Class: modelcache.ClassUpstream, Reason: "no candidates"}, nil
	}
	return m.selecter.Select(ctx, cands)
}

// candidates assembles the candidate set for a download. Order does not matter
// to the selecter (it ranks by measured cost) but we emit local, site, other,
// upstream to keep the reported scores readable. A gated download (repoID,
// gated) drops the other-site candidates entirely — the cross-site gated
// guard (GF.C6.5): a token-requiring repo's bytes must never be pulled
// through another site's cache, so only the home-site mirror, the local
// cache, and the public upstream (with the tenant's own token) may serve it.
func (m *modelCacheManager) candidates(repoID string, gated bool) []modelcache.Candidate {
	var cands []modelcache.Candidate
	if repoID != "" {
		if m.repoIsLocal(repoID) {
			cands = append(cands, modelcache.Candidate{Class: modelcache.ClassLocal, Endpoint: ""})
		}
	}
	if ep := m.siteEndpoint(); ep != "" {
		cands = append(cands, modelcache.Candidate{Class: modelcache.ClassSite, Endpoint: ep, ProbeURL: ep + modelcacheProbePath})
	}
	if !gated {
		for _, ep := range m.otherEndpoints() {
			cands = append(cands, modelcache.Candidate{Class: modelcache.ClassOther, Endpoint: ep, ProbeURL: ep + modelcacheProbePath})
		}
	}
	// Upstream is always a candidate so its real cost is measured and it is
	// the guaranteed fallback. Its ProbeURL is the hub root (measured), but
	// choosing it still means "set no HF_ENDPOINT". When sealed (GY.C8) it is
	// omitted entirely — never probed, never selectable.
	if m.publicUpstreamAllowed() {
		cands = append(cands, modelcache.Candidate{Class: modelcache.ClassUpstream, Endpoint: "", ProbeURL: upstreamProbeURL})
	}
	return cands
}

// filterPublicUpstream drops the public-upstream candidate from a candidate
// set. The background refresher re-measures a set captured before the egress
// tunables sealed the install, and a stale upstream entry must not be
// re-probed.
func filterPublicUpstream(cands []modelcache.Candidate) []modelcache.Candidate {
	out := make([]modelcache.Candidate, 0, len(cands))
	for _, c := range cands {
		if c.Class != modelcache.ClassUpstream {
			out = append(out, c)
		}
	}
	return out
}

// upstreamProbeURL is the public hub root, probed to measure upstream's real
// WAN cost. Choosing upstream still sets NO HF_ENDPOINT (the huggingface_hub
// default is huggingface.co).
const upstreamProbeURL = "https://huggingface.co/"

// siteEndpoint is the platform mirror endpoint (GF.C0b.9), read fresh from the
// server-pushed tunable. "" when the mirror is not in play.
func (m *modelCacheManager) siteEndpoint() string {
	if m.tunables != nil {
		if v := m.tunables.String(modelMirrorEndpointTunableKey, "", ""); v != "" {
			return strings.TrimRight(strings.TrimSpace(v), "/")
		}
	}
	return ""
}

// otherEndpoints returns the additional (other-site) cache endpoints from the
// worker.model_cache_endpoints tunable: a comma- or space-separated list of
// base URLs. Empty/absent (the API does not populate this key yet) returns
// nothing.
func (m *modelCacheManager) otherEndpoints() []string {
	if m.tunables == nil {
		return nil
	}
	raw := m.tunables.String(modelCacheEndpointsTunableKey, "", "")
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	var out []string
	for _, p := range strings.FieldsFunc(raw, func(r rune) bool { return r == ',' || r == ' ' || r == '\t' || r == '\n' }) {
		p = strings.TrimRight(p, "/")
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

// modEndpoints returns every endpoint that is a MOD model cache (the platform
// mirror + the other-site caches), ready for membership tests. The API writes
// this same mirror value into managed stacks (the model_cache_endpoint the
// worker sees pinned as HF_ENDPOINT), so a pinned endpoint in this set means
// "a MOD cache" and the tenant-token swap applies; a pinned endpoint NOT in
// this set is an operator's own cache and is left alone.
func (m *modelCacheManager) modEndpoints() []string {
	var out []string
	if ep := m.siteEndpoint(); ep != "" {
		out = append(out, ep)
	}
	out = append(out, m.otherEndpoints()...)
	return out
}

// isModCacheEndpoint reports whether endpoint is one of the known MOD
// model-cache endpoints. Exact match after trimming whitespace and trailing
// slashes — deliberately no prefix matching, so an operator-pinned
// third-party endpoint can never be confused with a MOD cache.
func isModCacheEndpoint(endpoint string, modEndpoints []string) bool {
	ep := strings.TrimRight(strings.TrimSpace(endpoint), "/")
	if ep == "" {
		return false
	}
	for _, mep := range modEndpoints {
		if strings.TrimRight(strings.TrimSpace(mep), "/") == ep {
			return true
		}
	}
	return false
}

// repoIsLocal reports whether repoID is already fully present in this host's
// HF cache. Cheap: a single Stat of the cache directory. A missing/empty repoID
// is never "local".
func (m *modelCacheManager) repoIsLocal(repoID string) bool {
	if repoID == "" {
		return false
	}
	dir, err := weights.RepoDir(hfCachePath(), repoID)
	if err != nil {
		return false
	}
	_, err = os.Stat(dir)
	return err == nil
}

// refreshScores re-measures the last known candidate set on a timer so the
// heartbeat reflects current health even when no download is in flight. It is
// a no-op until the first SelectFor has run (there is no set to measure yet).
func (m *modelCacheManager) refreshScores(ctx context.Context) {
	m.mu.Lock()
	cands := m.lastCands
	m.mu.Unlock()
	if len(cands) == 0 {
		return
	}
	if !m.publicUpstreamAllowed() {
		cands = filterPublicUpstream(cands)
	}
	ch, scores := m.selecter.Select(ctx, cands)
	m.mu.Lock()
	m.lastChoice = ch
	m.lastScores = scores
	m.lastAt = time.Now()
	m.mu.Unlock()
}

// StartBackgroundRefresh launches the periodic re-measurement loop. Safe to
// call once; it exits when ctx is done.
func (m *modelCacheManager) StartBackgroundRefresh(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(modelCacheRefresh)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				m.refreshScores(ctx)
			}
		}
	}()
}

// Snapshot returns the latest choice and scores for the heartbeat. scores is
// nil until the first measurement, so the heartbeat can omit the field rather
// than report an empty (and misleading) set.
func (m *modelCacheManager) Snapshot() (modelcache.Choice, []modelcache.Score) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.lastChoice, m.lastScores
}

// tailscaleRelayProbe returns a RelayProbe that classifies a target host as
// relayed or not using the site-mesh tailscale status. The rule (documented in
// tailscaleRelayProbeDirect): a target is "not relayed" (direct) when it is a
// known ONLINE tailnet peer — the path is a direct mesh link — or when it is
// not a tailnet address at all (a LAN/DNS/public name the host reaches on its
// own, which is never a DERP hop). A target that IS a tailnet address but not
// an online peer would have to ride the DERP relay, so it is classified
// relayed. When the status cannot be read (no site client), the probe reports
// not-relayed: uncertainty never excludes a candidate, only a positive
// tailnet-but-offline verdict does.
func tailscaleRelayProbe(run tsStatusRunner) modelcache.RelayProbe {
	return func(ctx context.Context, host string) (bool, error) {
		if run == nil || strings.TrimSpace(host) == "" {
			return false, nil
		}
		out, err := run(ctx)
		if err != nil {
			// Cannot read the mesh status → cannot tell → treat as direct.
			return false, nil
		}
		peers, perr := parseTSStatusPeers(out)
		if perr != nil {
			return false, nil
		}
		return classifyTSRelay(host, peers), nil
	}
}

// tsPeer is the subset of a `tailscale status --json` peer entry we need.
type tsPeer struct {
	TailAddr     string `json:"TailAddr"`
	Online       bool   `json:"Online"`
	LegacyOnline bool   `json:"online"`
}

func (p tsPeer) isOnline() bool { return p.Online || p.LegacyOnline }

// parseTSStatusPeers extracts the peer map (ip → {TailAddr, Online}) from a
// `tailscale status --json` document.
func parseTSStatusPeers(data []byte) (map[string]tsPeer, error) {
	var doc struct {
		Peers map[string]tsPeer `json:"Peers"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	if doc.Peers == nil {
		return map[string]tsPeer{}, nil
	}
	return doc.Peers, nil
}

// classifyTSRelay decides whether host must ride a DERP relay given the known
// peer set.
func classifyTSRelay(host string, peers map[string]tsPeer) bool {
	h := strings.TrimSpace(host)
	if h == "" {
		return false
	}
	// A tailnet address (100.64.0.0/10) that is a known ONLINE peer is a
	// direct mesh link.
	if ip := strings.Split(h, ":")[0]; isTailnetIP(ip) {
		if p, ok := peers[ip]; ok && p.isOnline() {
			return false // direct
		}
		// A tailnet address that is not an online peer cannot be reached
		// directly — it would relay. Classify as relayed.
		return true
	}
	// Not a tailnet address: LAN / DNS / public — the host reaches it on its
	// own plane, never a DERP hop.
	return false
}

// isTailnetIP reports whether s is an IPv4 address in the Tailscale CGNAT
// range 100.64.0.0/10 (the Tailscale CGNAT block).
func isTailnetIP(s string) bool {
	parts := strings.Split(s, ".")
	if len(parts) != 4 {
		return false
	}
	var a, b int
	for i, p := range parts {
		var n int
		n, _ = strconv.Atoi(p)
		if i == 0 {
			a = n
		}
		if i == 1 {
			b = n
		}
	}
	return a == 100 && b&0xfe == 64
}

// dockerTSStatusRunner is the production tsStatusRunner: it shells out to
// `tailscale status --json` inside the site-mesh container (mod-mesh-site),
// the same userspace client mesh/managed.go drives. Returns an error when the
// container is not running (the relay probe then reports "not relayed").
func (e *Executor) dockerTSStatusRunner(ctx context.Context) ([]byte, error) {
	if e == nil || e.docker == nil {
		return nil, context.Canceled
	}
	ec, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	cr, err := e.docker.ContainerExecCreate(ec, meshSiteContainerName, container.ExecOptions{
		Cmd:          []string{"tailscale", "--socket=" + meshSiteSocket, "status", "--json"},
		AttachStdout: true,
		Tty:          true,
	})
	if err != nil {
		return nil, err
	}
	att, err := e.docker.ContainerExecAttach(ec, cr.ID, container.ExecAttachOptions{Tty: true})
	if err != nil {
		return nil, err
	}
	defer att.Close()
	return io.ReadAll(io.LimitReader(att.Reader, 1<<20))
}

// meshSiteContainerName / meshSiteSocket mirror mesh.Managed's site client so
// the relay probe reads the SAME tailscale state the worker uses for the site
// mesh. (They are duplicated here rather than imported to avoid an
// executor→mesh dependency cycle; keep them in sync with mesh/managed.go.)
const (
	meshSiteContainerName = "mod-mesh-site"
	meshSiteSocket        = "/var/run/tailscale/tailscaled.sock"
)

// taskModelRepoID returns the first weight ref's repo id in a task payload, or
// "" when the task carries no weights. For a node container, this is the best
// signal available for whether the model it needs is already on disk (local) —
// a node that downloads a model carries it in its weights refs the same way a
// stack does. "" makes SelectFor skip the local candidate and just measure the
// caches, which is the correct (if slightly more conservative) answer.
func taskModelRepoID(payload transport.TaskPayload) string {
	if len(payload.Weights) == 0 {
		return ""
	}
	return strings.TrimSpace(payload.Weights[0].RepoID)
}

// applyModelCacheEnv injects the model-cache env into a node container's env
// list ([]string of "K=V"). It resolves the best endpoint for repoID ("" when
// unknown; gated per GF.C6.5 when the container declares an HF_TOKEN), then
// appends HF_ENDPOINT=<chosen> and HF_HUB_DISABLE_XET=1 ONLY when HF_ENDPOINT
// is not already present in the list — a value the API or an operator already
// pinned always wins — and swaps HF_TOKEN for the per-task tenant
// model-cache token when the effective endpoint is a MOD model cache
// (see injectHFEndpointIntoList). A nil/empty repoID and an unreachable
// manager degrade to "set nothing" (upstream direct). The only error is the
// sealed refusal (ErrModelCacheSealed, GY.C8): the public upstream disabled
// by the egress tunables with nothing local eligible.
func (e *Executor) applyModelCacheEnv(ctx context.Context, env []string, repoID string, modelCacheToken string, gated bool) ([]string, error) {
	if e == nil || e.modelCache == nil {
		return env, nil
	}
	endpoint, _, err := e.modelCache.SelectFor(ctx, repoID, gated)
	if err != nil {
		return env, err
	}
	return injectHFEndpointIntoList(env, endpoint, e.modelCache.modEndpoints(), modelCacheToken), nil
}

// injectHFEndpointIntoList appends HF_ENDPOINT=<endpoint> (when non-empty) and
// HF_HUB_DISABLE_XET=1 to a "K=V" env list, but never touches an existing
// HF_ENDPOINT entry (spec/API value wins). HF_HUB_DISABLE_XET is added only
// when absent, so an operator's explicit setting is preserved.
//
// GF.C6.5 — when the effective endpoint (an existing pinned HF_ENDPOINT wins
// over the chosen one) is a known MOD model-cache endpoint AND a non-empty
// modelCacheToken was granted, HF_TOKEN is set-or-replaced with it: the cache
// verifies the tenant token, fetches the tenant's real HF token itself, and
// attributes the download to that tenant. Any other effective endpoint
// (public upstream, local, an operator's own cache) leaves HF_TOKEN exactly
// as it was — the tenant's own token passes through untouched.
func injectHFEndpointIntoList(env []string, endpoint string, modEndpoints []string, modelCacheToken string) []string {
	have := make(map[string]bool, len(env))
	pinned := ""
	tokenIdx := -1
	for i, kv := range env {
		k, v, ok := cutEnvKV(kv)
		if !ok {
			continue
		}
		have[k] = true
		if k == "HF_ENDPOINT" {
			pinned = v
		}
		if k == "HF_TOKEN" {
			tokenIdx = i
		}
	}
	if endpoint != "" && !have["HF_ENDPOINT"] {
		env = append(env, "HF_ENDPOINT="+endpoint)
	}
	if !have["HF_HUB_DISABLE_XET"] {
		env = append(env, "HF_HUB_DISABLE_XET=1")
	}
	// Effective endpoint = the pinned one (which wins, above) when present,
	// else the chosen one.
	effective := endpoint
	if pinned != "" {
		effective = pinned
	}
	if modelCacheToken != "" && isModCacheEndpoint(effective, modEndpoints) {
		if tokenIdx >= 0 {
			env[tokenIdx] = "HF_TOKEN=" + modelCacheToken
		} else {
			env = append(env, "HF_TOKEN="+modelCacheToken)
		}
	}
	return env
}

// injectHFEndpointMap is the map[string]string variant for backends that build
// env as a map (K8s). Same "never override an existing HF_ENDPOINT" rule and
// the same GF.C6.5 tenant-token swap as injectHFEndpointIntoList.
func injectHFEndpointMap(env map[string]string, endpoint string, modEndpoints []string, modelCacheToken string) map[string]string {
	if env == nil {
		env = map[string]string{}
	}
	if endpoint != "" {
		if _, ok := env["HF_ENDPOINT"]; !ok {
			env["HF_ENDPOINT"] = endpoint
		}
	}
	if _, ok := env["HF_HUB_DISABLE_XET"]; !ok {
		env["HF_HUB_DISABLE_XET"] = "1"
	}
	if modelCacheToken != "" {
		effective := endpoint
		if p := env["HF_ENDPOINT"]; p != "" {
			effective = p
		}
		if isModCacheEndpoint(effective, modEndpoints) {
			env["HF_TOKEN"] = modelCacheToken
		}
	}
	return env
}

// ModelCacheScores returns the model-cache manager's latest measured scores
// for the heartbeat (GF.C2, see modelCacheManager.Snapshot). nil when there is
// no manager or nothing has been measured yet — a download/deploy always runs
// SelectFor first, so a live worker fills this in on its first task.
func (e *Executor) ModelCacheScores() []modelcache.Score {
	if e == nil || e.modelCache == nil {
		return nil
	}
	_, scores := e.modelCache.Snapshot()
	return scores
}

// applyModelCacheEnvToCompose injects HF_ENDPOINT=<chosen> + HF_HUB_DISABLE_XET=1
// into every service of a compose spec that does not already set HF_ENDPOINT,
// and applies the GF.C6.5 tenant-token swap (see injectHFEndpointCompose)
// per service. The endpoint is resolved from repoID (the first weight ref's
// repo, or "" for a spec with no weights); gated marks a spec in which ANY
// service declares an HF_TOKEN, which drops the other-site candidates from
// the selection (cross-site gated guard). An existing HF_ENDPOINT (any
// service) is never overridden; a spec with no services is returned
// unchanged. The error is the sealed refusal (ErrModelCacheSealed, GY.C8)
// when the public upstream is disabled by the egress tunables and nothing
// local is eligible.
func (e *Executor) applyModelCacheEnvToCompose(ctx context.Context, composeYAML string, repoID string, modelCacheToken string, gated bool) (string, error) {
	if e == nil || e.modelCache == nil {
		return composeYAML, nil
	}
	endpoint, _, err := e.modelCache.SelectFor(ctx, repoID, gated)
	if err != nil {
		return composeYAML, err
	}
	return injectHFEndpointCompose(composeYAML, endpoint, e.modelCache.modEndpoints(), modelCacheToken)
}

// injectHFEndpointCompose is the pure compose-YAML form of
// applyModelCacheEnvToCompose, split out so the injection is unit-testable
// without an executor. When endpoint is "" it only adds HF_HUB_DISABLE_XET=1
// (upstream-direct / local still benefit from XET being disabled for the
// large-repo path). Per service, the GF.C6.5 tenant-token swap applies when
// that service's EFFECTIVE endpoint (its own pinned HF_ENDPOINT when present,
// else the chosen one) is a known MOD model-cache endpoint: HF_TOKEN is
// set-or-replaced with modelCacheToken; every other service keeps HF_TOKEN
// exactly as it was.
func injectHFEndpointCompose(composeYAML string, endpoint string, modEndpoints []string, modelCacheToken string) (string, error) {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(composeYAML), &root); err != nil {
		return composeYAML, fmt.Errorf("injectHFEndpointCompose: parse compose: %w", err)
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return composeYAML, nil
	}
	doc := root.Content[0]
	svcs := mapGet(doc, "services")
	if svcs == nil || svcs.Kind != yaml.MappingNode {
		return composeYAML, nil
	}

	changed := false
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		svc := svcs.Content[i+1]
		if svc.Kind != yaml.MappingNode {
			continue
		}
		envNode := mapGet(svc, "environment")
		if envNode == nil {
			envNode = mapping() // null/absent → fresh mapping
			svc.Content = append(svc.Content, scalar("environment"), envNode)
		}
		// Never override an operator/API-pinned HF_ENDPOINT in this service.
		pinned := envVarValue(envNode, "HF_ENDPOINT")
		if pinned != "" {
			// Still ensure HF_HUB_DISABLE_XET is present.
			if setEnvVarIfAbsent(envNode, "HF_HUB_DISABLE_XET", "1") {
				changed = true
			}
		} else {
			if endpoint != "" && setEnvVarIfAbsent(envNode, "HF_ENDPOINT", endpoint) {
				changed = true
			}
			if setEnvVarIfAbsent(envNode, "HF_HUB_DISABLE_XET", "1") {
				changed = true
			}
		}
		// GF.C6.5 — tenant-token swap for this service's effective endpoint.
		effective := pinned
		if effective == "" {
			effective = endpoint
		}
		if modelCacheToken != "" && isModCacheEndpoint(effective, modEndpoints) {
			if setEnvVar(envNode, "HF_TOKEN", modelCacheToken) {
				changed = true
			}
		}
	}

	if !changed {
		return composeYAML, nil
	}
	out, err := yaml.Marshal(&root)
	if err != nil {
		return composeYAML, fmt.Errorf("injectHFEndpointCompose: re-marshal: %w", err)
	}
	return string(out), nil
}

// hasEnvKey reports whether an `environment` node (mapping or "K=V" list)
// already contains key.
func hasEnvKey(env *yaml.Node, key string) bool {
	if env == nil {
		return false
	}
	switch env.Kind {
	case yaml.MappingNode:
		for i := 0; i+1 < len(env.Content); i += 2 {
			if env.Content[i].Value == key {
				return true
			}
		}
	case yaml.SequenceNode:
		for _, e := range env.Content {
			if e.Kind != yaml.ScalarNode {
				continue
			}
			if k, _, ok := cutEnvKV(e.Value); ok && k == key {
				return true
			}
		}
	}
	return false
}

// envVarValue returns the value of key inside an `environment` node (mapping or
// "K=V" list), or "" when the key is absent. A mapping whose value node is
// null (compose `K:` with no value) counts as present-but-empty.
func envVarValue(env *yaml.Node, key string) string {
	if env == nil {
		return ""
	}
	switch env.Kind {
	case yaml.MappingNode:
		for i := 0; i+1 < len(env.Content); i += 2 {
			if env.Content[i].Value == key {
				return env.Content[i+1].Value
			}
		}
	case yaml.SequenceNode:
		for _, e := range env.Content {
			if e.Kind != yaml.ScalarNode {
				continue
			}
			if k, v, ok := cutEnvKV(e.Value); ok && k == key {
				return v
			}
		}
	}
	return ""
}

// composeDeclaresHFToken reports whether ANY service in a compose spec
// declares an HF_TOKEN — in `environment` (map or "K=V" list) or in an
// `x-mod.secrets` block (the stored-spec form, pre-resolution). That is the
// "gated download" marker for the cross-site guard (GF.C6.5): a
// token-requiring repo is never routed through another site's cache. A
// spec that does not parse is never reported as declaring one.
func composeDeclaresHFToken(composeYAML string) bool {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(composeYAML), &root); err != nil {
		return false
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return false
	}
	svcs := mapGet(root.Content[0], "services")
	if svcs == nil || svcs.Kind != yaml.MappingNode {
		return false
	}
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		svc := svcs.Content[i+1]
		if svc.Kind != yaml.MappingNode {
			continue
		}
		if hasEnvKey(mapGet(svc, "environment"), "HF_TOKEN") {
			return true
		}
		xmod := mapGet(svc, "x-mod")
		if xmod != nil {
			if hasEnvKey(mapGet(xmod, "secrets"), "HF_TOKEN") {
				return true
			}
		}
	}
	return false
}
