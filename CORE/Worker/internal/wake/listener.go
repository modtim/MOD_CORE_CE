package wake

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	neturl "net/url"
	"strconv"
	"strings"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// FX.165.p4 — direct wake (API side: CORE/API/services/worker_direct_wake.py).
//
// The API POSTs http://<advertise_address>:<port>/wake with an empty body
// (~300ms timeout) right after it commits a task ASSIGNED to this worker, so
// the worker runs its claim pass immediately instead of waiting for the Valkey
// `task:available:<queue>` publish or for its long-poll backstop to expire.
//
// The claim trigger this listener feeds is the worker's OWN claim-now path:
// APITransport.TryClaimNow (internal/transport/api.go:230), the 1-second poll
// DirectTransport.WaitForTask already fires at the top of every wait
// (internal/transport/direct.go:99) to close the pub/sub re-subscription gap.
// POST /wake is a signal only: the body is never read beyond a 1 KiB limit and
// carries no meaning, and the send is non-blocking on a capacity-1 channel, so
// a burst of wakes collapses to one pending signal — one extra claim pass,
// never a stampede on the claim endpoint.
//
// Bind policy: ONLY the addresses this worker advertises to the manager
// (internal/heartbeat/advertise.go — the heartbeat's advertise_address /
// advertise_address_site / advertise_address_cloud). 0.0.0.0 and :: are never
// chosen: a wake endpoint on a wildcard bind is a public endpoint that makes
// this worker poll on demand. On top of the bind, the source IP must itself be
// private (RFC1918 / CGNAT 100.64/10 / ULA / loopback) or the request is
// refused 403 before the trigger is touched. A manager that dials us over a
// tailnet is, by construction, in one of those ranges.
//
// Enable/port come from the platform-settings registry, delivered on every
// heartbeat response as the `tunables` map (GS.PSR.7 — heartbeat.go:1289 →
// internal/tunables.Store). Default OFF: with no `worker.direct_wake_enabled`
// in that map nothing is ever bound. No env var is invented for behaviour.

const (
	 wakePath = "/wake"
	 enabledKey = "worker.direct_wake_enabled"
	 portKey = "worker.direct_wake_port"
	 defaultPort = 8711 // the API's worker.direct_wake_port default
	 maxBodyBytes = 1024
	 claimTimeout = 5 * time.Second // TryClaimNow's own cap
	 handoff = 45 * time.Second // job-loop collection bound
	 recheck = 2 * time.Second // settings re-read cadence
)

// WakeClaimer is the worker's claim-now trigger, asserted structurally so this
// package does not depend on which transport the worker runs. APITransport has
// TryClaimNow; an embedded DirectTransport does not — it is woken by the Valkey
// publish on the same host — so the listener stays inert there.
type WakeClaimer interface {
	 TryClaimNow(ctx context.Context) (*transport.Task, error)
}

var privateRanges = []netip.Prefix{
	 netip.MustParsePrefix("10.0.0.0/8"), // RFC1918
	 netip.MustParsePrefix("172.16.0.0/12"), // RFC1918
	 netip.MustParsePrefix("192.168.0.0/16"), // RFC1918
	 netip.MustParsePrefix("100.64.0.0/10"), // CGNAT / tailnet
	 netip.MustParsePrefix("fc00::/7"), // IPv6 ULA (covers fd00::/8)
	 netip.MustParsePrefix("::1/128"), // IPv6 loopback
}

// IsPrivate reports whether a caller may dial us: RFC1918 private, CGNAT, ULA,
// or loopback. Invalid, unspecified, and any public address are refused.
func IsPrivate(ip netip.Addr) bool {
	 if !ip.IsValid() || ip.IsUnspecified() {
		 return false
	 }
	 if ip.IsLoopback() {
		 return true
	 }
	 for i := 0; i < len(privateRanges); i++ {
		 if privateRanges[i].Contains(ip.Unmap()) {
			 return true
		 }
	 }
	 return false
}

// ChooseAddrs is the bind policy, pure and unit-testable: from candidate
// address strings (the heartbeat's advertise addresses), keep only specified,
// private, de-duplicated ones as "ip:port". An unspecified address ("" / "0" /
// "::" / "0.0.0.0") is never emitted, and a public address never becomes a
// listen target.
func ChooseAddrs(port string, cands []string) []string {
	 if port == "" {
		 port = strconv.Itoa(defaultPort)
	 }
	 out := []string{}
	 for _, c := range cands {
		 ip, err := netip.ParseAddr(strings.TrimSpace(c))
		 if err != nil || ip.IsUnspecified() || !IsPrivate(ip) {
			 continue
		 }
		 joined := net.JoinHostPort(ip.String(), port)
		 if !containsAddr(out, joined) {
			 out = append(out, joined)
		 }
	 }
	 return out
}

func containsAddr(ss []string, want string) bool {
	 for _, s := range ss {
		 if s == want {
			 return true
		 }
	 }
	 return false
}

// SourceIP is the request's own connection address. X-Forwarded-For is never
// consulted: a wake hop is direct, and trusting a header would let a public
// caller present itself as a private one.
func SourceIP(r *http.Request) netip.Addr {
	 host, _, err := net.SplitHostPort(r.RemoteAddr)
	 if err != nil || host == "" {
		 host = r.RemoteAddr
	 }
	 ip, err := netip.ParseAddr(strings.TrimSpace(host))
	 if err != nil {
		 return netip.Addr{}
	 }
	 return ip
}

type Listener struct {
	 log *slog.Logger
	 tun *tunables.Store
	 claimer WakeClaimer
	 atCap func() bool // true = worker already at its concurrency cap

	 APIURL string
	 anchorSite string
	 anchorCloud string

	 Trigger chan struct{} // capacity 1: the claim-trigger channel
	 Claimed chan *transport.Task // wake-won tasks → the job loop

	 srv *http.Server
	 lns []net.Listener // live listeners (settings goroutine only)
	 port string // the port currently bound ("" = off)
	 boundAddrs []string // the addresses actually bound (a tailnet/LAN shift
	 // must rebind even when the port is unchanged, or the API — which dials
	 // the CURRENT advertise address — would knock on nothing)
}

// New builds the listener. Nothing is bound until Start() decides, and Start()
// re-decides on every settings tick, so an operator flipping
// worker.direct_wake_enabled takes effect without restarting the worker.
// atCap (nil = no guard) reports whether the worker is already at its
// concurrency cap; a wake claim pass is skipped there so the wake can never
// push the worker past max_concurrent_tasks.
func New(log *slog.Logger, tun *tunables.Store, apiURL, anchorSite, anchorCloud string,
	 claimer WakeClaimer, atCap func() bool) *Listener {
	 wl := &Listener{
		 log: log, tun: tun, claimer: claimer, atCap: atCap,
		 APIURL: apiURL, anchorSite: anchorSite, anchorCloud: anchorCloud,
		 Trigger: make(chan struct{}, 1),
		 Claimed: make(chan *transport.Task, 1),
	 }
	 mux := http.NewServeMux()
	 mux.HandleFunc(wakePath, func(w http.ResponseWriter, r *http.Request) {
		 wl.handle(w, r)
	 })
	 wl.srv = &http.Server{
		 Handler: mux,
		 ReadTimeout: 2 * time.Second,
		 WriteTimeout: 2 * time.Second,
	 }
	 return wl
}

// handle serves POST /wake. Any other method → 405 (with Allow), a public
// source IP → 403, otherwise a non-blocking trigger send and 204. The body is
// read only up to maxBodyBytes and never interpreted.
func (wl *Listener) handle(w http.ResponseWriter, r *http.Request) {
	 if r.Method != http.MethodPost {
		 w.Header().Set("Allow", http.MethodPost)
		 w.WriteHeader(http.StatusMethodNotAllowed)
		 return
	 }
	 // ONE read, capped at maxBodyBytes: the wake carries no payload, and a
	// second read would let a caller stream an unbounded body into this worker.
	 buf := make([]byte, maxBodyBytes)
	 n, _ := r.Body.Read(buf)
	 _ = n
	 _ = buf

	 ip := SourceIP(r)
	 status, fire := Decide(r.Method, ip)
	 if fire {
		 // Non-blocking send: if a wake is already pending, this one is dropped.
		 if !trySendTrigger(wl.Trigger) {
			 wl.log.Debug("wake dropped: claim pass already pending")
		 }
	 } else if status != http.StatusNoContent {
		 wl.log.Warn("wake refused", "from", ip.String(), "status", status)
	 }
	 w.WriteHeader(status)
}

// Decide is the pure request decision: any method other than POST → 405, a
// public/unspecified source IP → 403, otherwise 204 and "fire" (send the
// claim-trigger signal). Kept separate from handle so it is unit-testable
// without a live ResponseWriter.
func Decide(method string, ip netip.Addr) (status int, fire bool) {
	 if method != "POST" {
		 return http.StatusMethodNotAllowed, false
	 }
	 if !IsPrivate(ip) {
		 return http.StatusForbidden, false
	 }
	 return http.StatusNoContent, true
}

// trySendTrigger is the non-blocking send on the claim-trigger channel.
func trySendTrigger(ch chan struct{}) bool {
	 select {
	 case ch <- struct{}{}:
		 return true
	 default:
		 return false
	 }
}

// Start runs the two loops this listener owns: the settings loop (which binds
// and unbinds as the platform settings say) and the claim consumer. Both exit
// with the worker's context, closing every listener.
func (wl *Listener) Start(ctx context.Context) {
	 go settingsLoop(ctx, wl)
	 go claimLoop(ctx, wl)
}

// NextClaim is the job loop's non-blocking drain of a task a wake claim pass
// won. nil when there is none pending.
func (wl *Listener) NextClaim() *transport.Task {
	 select {
	 case t := <-wl.Claimed:
		 return t
	 default:
		 return nil
	 }
}

// AdvertiseCandidates gathers the addresses this worker reports to the manager
// as advertise_address / advertise_address_site / advertise_address_cloud.
//
// The heartbeat computes them in internal/heartbeat/advertise.go (advertise
//Address / advertiseAddressTo) — those are package-private, so the wake
// listener re-derives them with the SAME route trick documented there: opening
// a UDP socket sends nothing, it only makes the kernel pick the source address
// it would reach that destination from. A LAN worker therefore reports its LAN
// IP, a worker whose route to the manager runs over a tailnet reports its
// tailnet IP, and an anchor it cannot reach contributes "".
func (wl *Listener) AdvertiseCandidates() []string {
	 return []string{
		 advertiseAddressTo(managerHost(wl.APIURL)),
		 advertiseAddressTo(wl.anchorSite),
		 advertiseAddressTo(wl.anchorCloud),
	 }
}

// advertiseAddressTo mirrors heartbeat/advertise.go:advertiseAddressTo — the
// source address the kernel would use to reach `host`. Loopback and the
// unspecified address are dropped here (the heartbeat keeps them because it
// only reports; for a bind target they are exactly what we must never pick).
func advertiseAddressTo(host string) string {
	 if host == "" {
		 return ""
	 }
	 conn, err := net.DialTimeout("udp", net.JoinHostPort(host, "80"), 2*time.Second)
	 if err != nil {
		 return ""
	 }
	 defer conn.Close()
	 sa, ok := conn.LocalAddr().(*net.UDPAddr)
	 if !ok || sa.IP == nil || sa.IP.IsUnspecified() || sa.IP.IsLoopback() {
		 return ""
	 }
	 return sa.IP.String()
}

// managerHost pulls the bare host out of the configured API URL (same shape as
// heartbeat/advertise.go:managerHost).
func managerHost(apiURL string) string {
	 raw := strings.TrimSpace(apiURL)
	 if raw == "" {
		 return ""
	 }
	 if !strings.Contains(raw, "://") {
		 raw = "http://" + raw
	 }
	 u, err := neturl.Parse(raw)
	 if err != nil {
		 return ""
	 }
	 return u.Hostname()
}

// settingsLoop re-reads the enable/port settings every tick and re-binds when
// they change. Default OFF: no `worker.direct_wake_enabled` in the heartbeat's
// tunables map means nothing is ever bound.
func settingsLoop(ctx context.Context, wl *Listener) {
	 for {
		 select {
		 case <-ctx.Done():
			 closeAll(wl)
			 return
		 case <-time.After(recheck):
		 }
		 if wl.claimer == nil {
			 continue // no claim-now path: inert regardless of the setting
		 }
		 on := wl.tun.Bool(enabledKey, "", false)
		 port := strconv.Itoa(wl.tun.Int(portKey, defaultPort))
		 if !on {
			 if wl.port != "" {
				 closeAll(wl)
				 wl.log.Info("wake listener off")
			 }
			 continue
		 }
		 addrs := ChooseAddrs(port, wl.AdvertiseCandidates())
		 if len(addrs) == 0 {
			 // The route to the manager went away (or never resolved). A bind
			 // from an earlier tick would be stale: close it so a wake cannot
			 // land on an address this worker no longer advertises.
			 if wl.port != "" {
				 closeAll(wl)
				 wl.log.Warn("wake listener: no advertise address to bind — off")
			 }
			 continue
		 }
		 if port == wl.port && sameAddrs(addrs, wl.boundAddrs) {
			 continue // already serving exactly these addresses
		 }
		 closeAll(wl)
		 if err := bindAll(ctx, wl, addrs); err != nil {
			 wl.log.Error("wake listener: bind failed", "addrs", addrs, "error", err)
			 continue
		 }
		 wl.port = port
		 wl.log.Info("wake listener listening", "addrs", strings.Join(addrs, ","))
	 }
}

// bindAll opens one listener per address and serves them; a partial failure is
// reported but keeps whatever did bind.
func bindAll(ctx context.Context, wl *Listener, addrs []string) error {
	 var (
		 lns []net.Listener
		 errs []error
		 bound []string
	 )
	 for _, a := range addrs {
		 ln, err := net.Listen("tcp", a)
		 if err != nil {
			 errs = append(errs, fmt.Errorf("%s: %w", a, err))
			 continue
		 }
		 lns = append(lns, ln)
		 bound = append(bound, a)
	 }
	 if len(lns) == 0 {
		 return fmt.Errorf("no bind address succeeded")
	 }
	 wl.lns = lns
	 wl.boundAddrs = bound
	 for _, ln := range lns {
		 go func() {
			 if err := wl.srv.Serve(ln); err != nil && err != http.ErrServerClosed {
				 wl.log.Error("wake listener: serve stopped", "addr", ln.Addr().String(), "error", err)
			 }
		 }()
	 }
	 return nil
}

// closeAll ends the worker's wake listeners (Start/stop symmetry).
func closeAll(wl *Listener) {
	 for _, ln := range wl.lns {
		 _ = ln.Close()
	 }
	 wl.lns = []net.Listener{}
	 wl.boundAddrs = []string{}
	 wl.port = ""
}

// sameAddrs compares two bind sets order-insensitively (the advertise order can
// shuffle between ticks; the set that matters is which addresses are live).
func sameAddrs(a, b []string) bool {
	 if len(a) != len(b) {
		 return false
	 }
	 for i := 0; i < len(a); i++ {
		 if !containsAddr(b, a[i]) {
			 return false
		 }
	 }
	 return true
}

// claimLoop consumes the claim-trigger channel: each pending signal runs ONE
// claim-now pass (the worker's TryClaimNow), and a task it wins is handed to
// the job loop through Claimed.
func claimLoop(ctx context.Context, wl *Listener) {
	 for {
		 select {
		 case <-wl.Trigger:
		 case <-ctx.Done():
			 return
		 }
		 if wl.atCap != nil && wl.atCap() {
			 continue // at capacity: the wake is satisfied by the pending work
		 }
		 reqCtx, cancel := context.WithTimeout(ctx, claimTimeout)
		 task, err := wl.claimer.TryClaimNow(reqCtx)
		 cancel()
		 if err != nil {
			 wl.log.Warn("wake claim pass failed", "error", err)
			 continue
		 }
		 if task == nil {
			 continue // empty queue: the wake still cost one cheap poll
		 }
		 if !handoffTask(ctx, wl, task) {
			 wl.log.Error("wake claim: task not collected by job loop", "task_id", task.ID)
		 }
	 }
}

// handoffTask blocks until the job loop collects the task, up to handoff.
func handoffTask(ctx context.Context, wl *Listener, task *transport.Task) bool {
	 select {
	 case wl.Claimed <- task:
		 return true
	 case <-time.After(handoff):
		 return false
	 case <-ctx.Done():
		 return false
	 }
}
