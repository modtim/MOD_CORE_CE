package executor

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types/network"
)

// worker-mediated-node-credentials (Group 5) — the worker's local token
// endpoint and the per-task nonce registry that guards it.
//
// A node container no longer receives a static, long-lived API credential.
// Instead the worker injects two env vars at launch (see cmd/worker/main.go):
//
//	MOD_TASK_TOKEN_URL — absolute URL of THIS endpoint, reachable on the task
//	                     network. Its presence is also the network-capability
//	                     marker (see netpol.go) and the node-auth helper's
//	                     signal to use worker-vended tokens.
//	MOD_TASK_NONCE     — an opaque single-task secret. Never an API credential;
//	                     it only lets the container ask the worker for tokens
//	                     scoped to its own task, and it is void once the task
//	                     ends.
//
// The node POSTs MOD_TASK_NONCE to MOD_TASK_TOKEN_URL; the worker validates it
// against a live task it launched, mints a fresh run_task token from the API
// over its authenticated worker channel, and returns it. The worker never signs
// tokens itself.
//
// REACHABILITY / BIND CHOICE (R-161 — narrow bind, per backend):
//
// Who calls this endpoint? Exactly the node containers THIS worker launched
// (the executor injects MOD_TASK_TOKEN_URL at spawn, cmd/worker/main.go). No
// sidecars, no remote callers, no host process. So the listener must only be
// reachable from those containers' networks — never 0.0.0.0:
//
//   - docker / wslc / native backends (all callers are local to the worker):
//     a NARROW default bind is derived in Start():
//   - every docker bridge network the token-fetch containers may land on
//     (main network = cfg docker_network, plus the mod-user-egress,
//     mod-user-internal and mod_stack-api-net postures, see netpol.go) —
//     bound to each network's gateway IP, the address
//     `host.docker.internal:host-gateway` (added by runContainerWithCreds)
//     actually resolves to;
//   - 127.0.0.1, for embedded workers: the worker container itself is on
//     the shared compose network, and MOD_TASK_TOKEN_HOST (compose
//     container name, e.g. my_mod_worker) resolves to the worker's
//     container IP, so the worker's own veth-internal IP must be bound
//     (the Docker client resolves the daemon's bridge IP, not ours).
//     Networks not yet inspectable at startup (e.g. the stack-posture net,
//     created on demand) are retried lazily: Start() returns the listener on
//     the best-effort set, and a short probe loop re-inspects the remaining
//     names and rebinds the listener (stop → listen → swap) when one appears.
//     Rebind is safe: nonces are validated by the registry, so a request that
//     arrives on the new listener is no weaker than one on the old.
//   - k8s backend: task Pods are remote to the worker (they use the in-cluster
//     {release}-worker Service, see MOD_TASK_TOKEN_HOST in the chart), so the
//     endpoint must be reachable on the pod/veth interface — narrow is
//     impossible and the bind WIDENS to 0.0.0.0, logged at startup. Nonce +
//     rate limit below still apply, and the k8s path adds NetworkPolicy at
//     the pod side (see k8s-backend.md).
//
// Advertised URL: http://<host>:<port>/run-token. <host> defaults to
// `host.docker.internal` and both host and port are env-overridable
// (MOD_TASK_TOKEN_HOST / MOD_TASK_TOKEN_PORT) for embedded workers. The bind
// itself is overridable through worker config (TokenServerBind,
// `token_server_bind` in the worker YAML) — default "auto" = the narrow set
// above; an explicit host such as "::" forces exactly that, e.g. for the
// k8s case.
const (
	defaultTokenPort = "9179"
	defaultTokenHost = "host.docker.internal"
	tokenPath        = "/run-token"
	nonceHeader      = "X-Mod-Task-Nonce"

	// nonceTTL is a backstop lifetime. A nonce is normally revoked the instant
	// its task finishes (RevokeNonce, deferred in executeTask); this cap only
	// guards against a leaked entry outliving its task forever.
	nonceTTL = 24 * time.Hour

	// mintTimeout bounds the upstream API mint call so a stuck API cannot pin a
	// node's HTTP request open indefinitely.
	mintTimeout = 30 * time.Second

	// --- R-161 narrow bind ---

	// bindAuto selects the per-backend narrow bind derived in Start() (see the
	// package doc). It is the DEFAULT; any other value of TokenServerBind is
	// used verbatim as the listen host.
	bindAuto = "auto"

	// tokenRebindProbe is how often the background probe checks for
	// not-yet-created bridge networks (auto mode only).
	// tokenProbeMaxRounds bounds that probe: a host whose bridge-network
	// gateways never become inspectable (a dind sandbox, a daemon that hides
	// every network's IPAM gateway) would otherwise re-inspect and re-listen
	// every tokenRebindProbe for the whole life of the worker — each round
	// costs four NetworkInspect round-trips on the shared docker socket plus
	// a full stop→listen→swap, and logs "no bind address succeeded". That
	// storm starves the executor's own docker calls, which is what left a
	// 1-node Image Resize run sitting in `running` for 10+ minutes with the
	// node container never launched (CE stranger-boot 1900Y1b C6). After the
	// cap the endpoint keeps whatever addresses DID bind (127.0.0.1 plus, for
	// an embedded worker, its own veth-internal IP — both of which a node on
	// the compose network reaches) and stops probing; a later round that
	// resolves everything still rebinds once and returns.
	// 24 rounds x 5s = a 2-minute window, which covers a network that is
	// created shortly after boot (the stack-posture net, on demand) without
	// leaving an operator chasing a log line.
	tokenRebindProbe = 5 * time.Second

	tokenProbeMaxRounds = 24

	// --- R-161 rate limiting on failed nonce attempts (per source IP) ---

	// nonceFailLimit: after this many failed nonce validations from one
	// source IP, further attempts are rejected with 429 for nonceFailWindow.
	nonceFailLimit  = 20
	nonceFailWindow = time.Minute
)

// tokenBridgeNetworks are the docker networks a token-fetching node container
// can land on (netpol.go: the egress posture, the internal-only default, and
// the on-demand stack-API posture). Start() binds the gateway IP of every one
// of these that is inspectable, and retries the rest in the background until
// they appear (mod_stack-api-net is created on demand). A var, not a const
// slice, so tests can substitute a stub set.
var tokenBridgeNetworks = []string{
	"mod-user-egress",
	"mod-user-internal",
	"mod_stack-api-net",
}

// TaskIdentity is the immutable identity a nonce is bound to. TaskID addresses
// the API mint endpoint; TenantID/RunID are carried for logging and
// defence-in-depth.
type TaskIdentity struct {
	TaskID   int
	TenantID int
	RunID    int
}

// RunTokenMinter obtains a fresh run_task token for a task over the worker's
// already-authenticated API channel. transport.Transport satisfies this.
type RunTokenMinter interface {
	MintRunToken(ctx context.Context, taskID int) (token string, exp int64, err error)
}

// nonceEntry is a live nonce → task binding.
type nonceEntry struct {
	id      TaskIdentity
	expires time.Time
}

// NonceRegistry is a concurrency-safe map of live per-task nonces.
type NonceRegistry struct {
	mu  sync.RWMutex
	m   map[string]nonceEntry
	ttl time.Duration
	now func() time.Time // swappable for tests
}

// NewNonceRegistry returns an empty registry.
func NewNonceRegistry() *NonceRegistry {
	return &NonceRegistry{
		m:   make(map[string]nonceEntry),
		ttl: nonceTTL,
		now: time.Now,
	}
}

// Issue mints a cryptographically-random 256-bit nonce bound to id and stores
// it. The nonce is returned as hex.
func (r *NonceRegistry) Issue(id TaskIdentity) (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("generate task nonce: %w", err)
	}
	nonce := hex.EncodeToString(b[:])
	r.mu.Lock()
	r.m[nonce] = nonceEntry{id: id, expires: r.now().Add(r.ttl)}
	r.mu.Unlock()
	return nonce, nil
}

// Lookup returns the task identity bound to a nonce, or ok=false if the nonce is
// unknown or has expired. An expired nonce is evicted on read.
func (r *NonceRegistry) Lookup(nonce string) (TaskIdentity, bool) {
	r.mu.RLock()
	e, ok := r.m[nonce]
	r.mu.RUnlock()
	if !ok {
		return TaskIdentity{}, false
	}
	if r.now().After(e.expires) {
		r.Revoke(nonce)
		return TaskIdentity{}, false
	}
	return e.id, true
}

// Revoke deletes a nonce. Idempotent — safe to call more than once.
func (r *NonceRegistry) Revoke(nonce string) {
	r.mu.Lock()
	delete(r.m, nonce)
	r.mu.Unlock()
}

// Len reports how many nonces are currently live (test/observability helper).
func (r *NonceRegistry) Len() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.m)
}

// dockerNetworkGateway resolves a docker network's gateway IP (the address a
// container's `host.docker.internal:host-gateway` ExtraHost points at). An
// interface — not *client.Client — so tests stub it; the production adapter is
// dockerGatewaySource.
type dockerNetworkGateway interface {
	NetworkGateway(ctx context.Context, name string) (net.IP, error)
}

// TokenServer is the worker's local run_task-token endpoint.
type TokenServer struct {
	registry *NonceRegistry
	minter   RunTokenMinter
	log      *slog.Logger

	// bind is the listen-host override: bindAuto (default) derives the
	// per-backend narrow bind in Start(); any other value is used verbatim.
	bind string
	// port is the listen port (MOD_TASK_TOKEN_PORT).
	port string
	// containerURL is the advertised MOD_TASK_TOKEN_URL.
	containerURL string
	// mainNetwork is the docker network cfg docker_network resolves to;
	// "host" or "" means the worker's own host networking.
	mainNetwork string
	// gateways inspects docker bridge networks for their gateway IP.
	// nil disables auto-derivation (tests, k8s-only workers).
	gateways dockerNetworkGateway
	// embedded marks a worker that is itself a container on the shared
	// compose network — the worker's own veth-internal IP must be bound so
	// MOD_TASK_TOKEN_HOST (the container name) resolves for sibling nodes.
	embedded bool

	srv  *http.Server
	lnMu sync.Mutex
	lns  []net.Listener // live listeners (one per bind address, auto mode)
	// lnAddrs is parallel to lns: the listen address (as requested, e.g.
	// "127.0.0.1:9179") each live listener was opened on. rebind needs the
	// REQUESTED form, not Addr().String() ("[::]:9179" vs "0.0.0.0:9179"),
	// to tell an already-live address from a new one without tripping our
	// own EADDRINUSE.
	lnAddrs []string

	failLimiter failLimiter // R-161 per-source-IP nonce-failure rate limit
}

// NewTokenServer builds the endpoint (does not start listening). Bind, port
// and advertised host are configurable — see the package doc above.
// bind: "auto" (default) = per-backend narrow bind; otherwise the listen
// host verbatim (e.g. "::"). mainNetwork is cfg docker_network.
func NewTokenServer(minter RunTokenMinter, log *slog.Logger, bind, port, mainNetwork string) *TokenServer {
	if bind == "" {
		bind = bindAuto
	}
	if port == "" {
		port = os.Getenv("MOD_TASK_TOKEN_PORT")
	}
	if port == "" {
		port = defaultTokenPort
	}
	host := os.Getenv("MOD_TASK_TOKEN_HOST")
	if host == "" {
		host = defaultTokenHost
	}

	ts := &TokenServer{
		registry:     NewNonceRegistry(),
		minter:       minter,
		log:          log,
		bind:         bind,
		port:         port,
		containerURL: fmt.Sprintf("http://%s%s", net.JoinHostPort(host, port), tokenPath),
		mainNetwork:  mainNetwork,
		failLimiter:  *newFailLimiter(),
	}

	mux := http.NewServeMux()
	mux.HandleFunc(tokenPath, ts.handle)
	ts.srv = &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	return ts
}

// ContainerURL is the value to inject as MOD_TASK_TOKEN_URL.
func (ts *TokenServer) ContainerURL() string { return ts.containerURL }

// IssueNonce mints a per-task nonce for id.
func (ts *TokenServer) IssueNonce(id TaskIdentity) (string, error) {
	return ts.registry.Issue(id)
}

// RevokeNonce invalidates a per-task nonce (called when the task finishes).
func (ts *TokenServer) RevokeNonce(nonce string) { ts.registry.Revoke(nonce) }

// SetDockerGateways installs the docker bridge-gateway inspector used by
// auto bind derivation. Called by main once the executor (and its docker
// client) exists; a nil argument keeps auto-derivation off.
func (ts *TokenServer) SetDockerGateways(g dockerNetworkGateway) { ts.gateways = g }

// SetEmbedded marks this worker as an embedded (compose) container.
func (ts *TokenServer) SetEmbedded(embedded bool) { ts.embedded = embedded }

// bindAddrs returns the listen addresses for the current config. In auto
// mode the set is derived: main-network gateway + every token bridge-network
// gateway found so far + 127.0.0.1 + (embedded) the worker's own veth-internal
// IP. Addresses that are not yet resolvable are omitted; Start() keeps
// probing for them.
func (ts *TokenServer) bindAddrs() ([]string, bool) {
	// bool: still-resolving (auto mode, incomplete set)
	if ts.bind != bindAuto {
		return []string{net.JoinHostPort(ts.bind, ts.port)}, false
	}
	addrs := []string{net.JoinHostPort("127.0.0.1", ts.port)}
	pending := false
	names := append([]string{}, tokenBridgeNetworks...)
	if ts.mainNetwork != "" && ts.mainNetwork != "host" {
		names = append([]string{ts.mainNetwork}, names...)
	}
	if ts.gateways != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		for _, name := range names {
			ip, err := ts.gateways.NetworkGateway(ctx, name)
			if err != nil || ip == nil {
				pending = true // not created yet / docker unavailable — keep probing
				continue
			}
			if !ip.IsLoopback() && !containsHostPort(addrs, net.JoinHostPort(ip.String(), ts.port)) {
				addrs = append(addrs, net.JoinHostPort(ip.String(), ts.port))
			}
		}
		cancel()
	}
	if ts.embedded {
		if ip := localNonLoopbackIP(); ip != nil {
			if !containsHostPort(addrs, net.JoinHostPort(ip.String(), ts.port)) {
				addrs = append(addrs, net.JoinHostPort(ip.String(), ts.port))
			}
		}
	}
	return addrs, pending
}

// rebind binds the new address set and serves it, replacing the currently
// live listeners (old listeners are closed, which ends their Serve
// goroutines with ErrServerClosed). http.Server tracks every listener it is
// Served on, so concurrent Serve on the set is supported and Shutdown/Close
// reach all of them.
func (ts *TokenServer) rebind(addrs []string) error {
	// The live set is read and swapped under lnMu for the whole call: Close()
	// (process exit) can otherwise race with the probe loop's read of it.
	// Nothing in here blocks - net.Listen is immediate and the Serve goroutines
	// this function starts never take the lock - so holding it across the
	// listens cannot stall anything.
	ts.lnMu.Lock()
	defer ts.lnMu.Unlock()
	// R-161 self-conflict (the CE stranger-boot C6 log flood): the probe loop
	// calls this every tokenRebindProbe with a set that OVERLAPS the live one,
	// because the addresses that already bound successfully (127.0.0.1, plus an
	// embedded worker's own veth-internal IP) are re-derived every round.
	// Listening on an address we are already listening on fails EADDRINUSE, so
	// an all-overlap round used to report "no bind address succeeded" for every
	// address, forever, and a round that did find one new gateway closed the
	// still-live old listeners while its own duplicate listens failed -
	// silently dropping the addresses that were working. So a rebind must:
	//   * keep an address that is already live, never re-listen on it;
	//   * open only genuinely new addresses;
	//   * never discard the listeners that are serving just because some OTHER
	//     address in the set could not be bound.
	// The live addresses are tracked in their REQUESTED form (lnAddrs): Addr()
	// renders "0.0.0.0:9179" as "[::]:9179", which reads as a different address
	// and makes every round look like a full rebind.
	kept := make([]net.Listener, 0, len(addrs))
	keptAddrs := make([]string, 0, len(addrs))
	dropped := make([]net.Listener, 0, len(ts.lns))
	for i, a := range ts.lnAddrs {
		if i >= len(ts.lns) {
			continue
		}
		if containsHostPort(addrs, a) {
			kept = append(kept, ts.lns[i])
			keptAddrs = append(keptAddrs, a)
		} else {
			dropped = append(dropped, ts.lns[i])
		}
	}

	fresh := make([]net.Listener, 0, len(addrs))
	freshAddrs := make([]string, 0, len(addrs))
	var errs []error
	for _, a := range addrs {
		if containsHostPort(keptAddrs, a) {
			continue // already live: re-listening would EADDRINUSE on ourselves
		}
		ln, err := net.Listen("tcp", a)
		if err != nil {
			// One unbindable address must never take down the listeners that
			// do work (nor the ones already live).
			errs = append(errs, fmt.Errorf("%s: %w", a, err))
			continue
		}
		fresh = append(fresh, ln)
		freshAddrs = append(freshAddrs, a)
	}

	// Nothing new to open and nothing to retire: the live set already IS the
	// requested set - a no-op round. No close, no listen, no log line, so the
	// probe loop cannot flood the worker log (nor hammer the docker socket
	// through bindAddrs) while it waits for networks that never appear.
	if len(fresh) == 0 && len(dropped) == 0 {
		if len(kept) == 0 {
			// Nothing was live and nothing could be opened: the whole set
			// failed to bind. The worker still takes work (Start logs this and
			// nodes fall back to the transitional API_KEY), but the endpoint
			// has no listener.
			return fmt.Errorf("no bind address succeeded: %v", errs)
		}
		return nil
	}

	// Retire the addresses that are no longer wanted, then adopt kept+fresh.
	// A listener that failed to open is simply absent from fresh - the kept
	// ones stay live, which is the point: a partial bind never turns into
	// "no listener at all".
	for _, ln := range dropped {
		_ = ln.Close() // ends its Serve goroutine with ErrServerClosed
	}
	ts.lns = append(kept, fresh...)
	ts.lnAddrs = append(keptAddrs, freshAddrs...)

	// Only the freshly opened listeners get a Serve goroutine; the kept ones
	// are already being served by the goroutine opened at their own bind.
	for _, ln := range fresh {
		ln := ln
		go func() {
			if err := ts.srv.Serve(ln); err != nil && err != http.ErrServerClosed && !errors.Is(err, net.ErrClosed) {
				ts.log.Error("node token endpoint: serve stopped", "addr", ln.Addr().String(), "error", err)
			}
		}()
	}
	// Addr logging for the whole set:
	j := make([]string, 0, len(ts.lnAddrs))
	for _, a := range ts.lnAddrs {
		j = append(j, a)
	}
	ts.log.Info("node token endpoint listening",
		"addrs", strings.Join(j, ","), "container_url", ts.containerURL)
	if len(errs) > 0 {
		ts.log.Warn("node token endpoint: some bind addresses failed", "errors", fmt.Sprint(errs))
	}
	return nil
}

// Start begins serving. Auto mode derives the narrow bind, starts a probe
// loop for still-pending network gateways, and widens to nothing beyond the
// derived set (0.0.0.0 is never chosen automatically). A bind failure is
// logged and non-fatal: nodes then fall back to the transitional API_KEY and
// the worker keeps taking work rather than refusing every task over a
// token-endpoint fault.
func (ts *TokenServer) Start() {
	addrs, pending := ts.bindAddrs()
	if len(addrs) == 0 {
		ts.log.Error("node token endpoint: no bind address derivable — nodes fall back to API_KEY")
		return
	}
	if err := ts.rebind(addrs); err != nil {
		ts.log.Error("node token endpoint: listen failed — nodes fall back to API_KEY",
			"addrs", addrs, "error", err)
		return
	}
	if ts.bind == bindAuto && pending && ts.gateways != nil {
		go ts.probeLoop()
	}
}

// probeLoop re-derives the bind set while auto mode still has unresolved
// network gateways (e.g. the stack-posture net, created on demand). It stops
// once the set is complete, the server is closed, or tokenProbeMaxRounds
// rounds have run - a host whose gateways never become inspectable (a dind
// sandbox, a daemon that hides every network's IPAM gateway) must not spend
// the worker's whole life re-inspecting four networks and re-listening every
// tokenRebindProbe on the shared docker socket. Whatever DID bind stays live
// and serving, so capping the probe costs a late-created network its gateway
// address, nothing else.
func (ts *TokenServer) probeLoop() {
	t := time.NewTicker(tokenRebindProbe)
	defer t.Stop()
	for round := 0; round < tokenProbeMaxRounds; round++ {
		// Drain the tick (the ticker's channel is a sink, not a source).
		_ = <-t.C
		ts.lnMu.Lock()
		closed := len(ts.lns) == 0
		ts.lnMu.Unlock()
		if closed {
			return
		}
		addrs, pending := ts.bindAddrs()
		if !pending {
			return
		}
		if cur := ts.currentAddrs(); !addrsEqual(cur, addrs) {
			if err := ts.rebind(addrs); err != nil {
				ts.log.Warn("node token endpoint: rebind failed", "error", err)
			}
		}
	}
}

// currentAddrs reports the live listener addresses in their REQUESTED form
// (parallel to lns, see rebind) so the probe loop's set comparison is
// apples-to-apples with what bindAddrs derives.
func (ts *TokenServer) currentAddrs() []string {
	ts.lnMu.Lock()
	defer ts.lnMu.Unlock()
	return append([]string{}, ts.lnAddrs...)
}

// NODE-RETRY: NoteNetwork is the executor's hook (wired by main.go to
// Executor.postureNetNotify) called the instant a posture network a task is
// about to attach to is created/observed on this host — mod_stack-api-net in
// particular is created ON DEMAND (executor.go ensureAttachableNetwork), so
// its gateway IP is invisible to bindAddrs until that moment. The auto-bind
// set is otherwise refreshed only every tokenRebindProbe (5s) by probeLoop, so
// without this hook the FIRST node container scheduled onto the new net gets a
// connection-refused on MOD_TASK_TOKEN_URL: mod_node_auth._fetch_from_worker
// raises, the container exits 1 (executor.go "container exited with code %d"),
// the API requeues, and the retry ~10s later — after a probe round rebound the
// gateway — succeeds. That is exactly the cold-start double-execute seen in CE
// run 19 (tasks 67/68). Rebind is idempotent (already-live addresses are kept,
// only genuinely new ones opened), so calling this on every task is safe.
func (ts *TokenServer) NoteNetwork(name string) {
	if ts.bind != bindAuto || ts.gateways == nil {
		return // explicit bind / no docker: nothing to re-derive
	}
	addrs, _ := ts.bindAddrs()
	if err := ts.rebind(addrs); err != nil {
		ts.log.Warn("node token endpoint: rebind after posture-network note",
			"network", name, "error", err)
	}
}

// Close shuts the endpoint down (stops the probe loop with it).
func (ts *TokenServer) Close() error {
	ts.lnMu.Lock()
	for _, ln := range ts.lns {
		_ = ln.Close()
	}
	ts.lns = nil
	ts.lnAddrs = nil
	ts.lnMu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return ts.srv.Shutdown(ctx)
}

func containsHostPort(addrs []string, hp string) bool {
	for _, a := range addrs {
		if a == hp {
			return true
		}
	}
	return false
}

func addrsEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	set := make(map[string]bool, len(a))
	for _, x := range a {
		set[x] = true
	}
	for _, y := range b {
		if !set[y] {
			return false
		}
	}
	return true
}

// localNonLoopbackIP returns the first non-loopback unicast IPv4 address on
// this host (the worker container's veth-internal IP when embedded). Used
// only in auto bind mode; returns nil when none is found.
func localNonLoopbackIP() net.IP {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return nil
	}
	for _, a := range addrs {
		ipnet, ok := a.(*net.IPNet)
		if !ok || ipnet.IP.IsLoopback() {
			continue
		}
		ip := ipnet.IP.To4()
		if ip == nil {
			continue
		}
		return ip
	}
	return nil
}

// dockerGatewaySource adapts a docker client to dockerNetworkGateway.
type dockerGatewaySource struct {
	client DockerGatewayClient
}

// DockerGatewayClient is the slice of the docker client the token server
// needs (main passes the executor's client).
type DockerGatewayClient interface {
	NetworkInspect(ctx context.Context, networkID string, options network.InspectOptions) (network.Inspect, error)
}

// NewDockerGateways adapts the executor's docker client to the
// dockerNetworkGateway the auto bind derivation needs.
func NewDockerGateways(c DockerGatewayClient) dockerNetworkGateway {
	return &dockerGatewaySource{client: c}
}

func (g *dockerGatewaySource) NetworkGateway(ctx context.Context, name string) (net.IP, error) {
	res, err := g.client.NetworkInspect(ctx, name, network.InspectOptions{})
	if err != nil {
		return nil, err
	}
	for _, gw := range res.IPAM.Config {
		if gw.Gateway != "" {
			ip := net.ParseIP(gw.Gateway)
			if ip != nil {
				return ip, nil
			}
		}
	}
	return nil, fmt.Errorf("network %q has no gateway configured", name)
}

// failLimiter is a per-source-IP rate limiter over FAILED nonce attempts
// (R-161): once a source exceeds nonceFailLimit failures inside a rolling
// nonceFailWindow, its attempts are rejected with 429 until the window
// clears. Entries are evicted on read once their window has passed. now is
// swappable for tests.
type failLimiter struct {
	mu       sync.Mutex
	failures map[string]int
	window   time.Duration
	limit    int
	now      func() time.Time
	// firstSeen records when each source's current failure burst started.
	firstSeen map[string]time.Time
}

func newFailLimiter() *failLimiter {
	return &failLimiter{
		failures:  make(map[string]int),
		firstSeen: make(map[string]time.Time),
		window:    nonceFailWindow,
		limit:     nonceFailLimit,
		now:       time.Now,
	}
}

// isLimited reports whether src has used up its failure budget (limit or
// more failures inside the current window).
func (f *failLimiter) isLimited(src string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.evictLocked(src)
	return f.failures[src] >= f.limit
}

// recordFail counts one failed attempt for src, starting (or keeping) the
// burst window. A new window begins after window of quiet.
func (f *failLimiter) recordFail(src string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.evictLocked(src)
	f.failures[src]++
	if f.firstSeen[src].IsZero() {
		f.firstSeen[src] = f.now()
	}
}

// evictLocked drops src's entry when its window has fully elapsed.
func (f *failLimiter) evictLocked(src string) {
	first, ok := f.firstSeen[src]
	if !ok {
		return
	}
	if f.now().Sub(first) >= f.window {
		delete(f.failures, src)
		delete(f.firstSeen, src)
	}
}

// sourceIP extracts the request source address for rate limiting: the
// connection IP, unwrapped from X-Forwarded-For only when the connection
// itself is loopback (a local proxy). Never trusts the header otherwise.
func sourceIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || host == "" {
		host = r.RemoteAddr
	}
	if h := net.ParseIP(host); h != nil && h.IsLoopback() {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			if first := net.ParseIP(strings.TrimSpace(strings.Split(xff, ",")[0])); first != nil {
				return first.String()
			}
		}
	}
	return host
}

// handle serves POST {MOD_TASK_TOKEN_URL}. It validates the presented nonce
// against a live task, mints a fresh run_task token from the API, and returns
// {"token","exp"}. 401 on a missing/unknown/expired nonce; 429 once the
// source IP's failed-attempt budget is exhausted.
func (ts *TokenServer) handle(w http.ResponseWriter, r *http.Request) {
	src := sourceIP(r)
	if ts.failLimiter.isLimited(src) {
		writeTokenError(w, http.StatusTooManyRequests, "too many failed attempts; slow down")
		return
	}

	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeTokenError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	nonce := r.Header.Get(nonceHeader)
	if nonce == "" {
		ts.failLimiter.recordFail(src)
		writeTokenError(w, http.StatusUnauthorized, "missing nonce")
		return
	}

	id, ok := ts.registry.Lookup(nonce)
	if !ok {
		// Unknown / expired / task no longer live — never distinguish which,
		// to avoid handing a probe any signal about valid nonces.
		ts.failLimiter.recordFail(src)
		writeTokenError(w, http.StatusUnauthorized, "unknown or expired nonce")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), mintTimeout)
	defer cancel()

	token, exp, err := ts.minter.MintRunToken(ctx, id.TaskID)
	if err != nil {
		ts.log.Warn("node token mint failed",
			"task_id", id.TaskID, "tenant_id", id.TenantID, "run_id", id.RunID, "error", err)
		writeTokenError(w, http.StatusBadGateway, "token mint failed")
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]interface{}{"token": token, "exp": exp})
}

func writeTokenError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}
