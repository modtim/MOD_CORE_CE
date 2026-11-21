package egressproxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egress"
)

type fakeResolver map[string][]netip.Addr

func (f fakeResolver) LookupNetIP(_ context.Context, _, host string) ([]netip.Addr, error) {
	if a, ok := f[host]; ok {
		return a, nil
	}
	return nil, errors.New("no such host")
}

// fakeDialer records every address the proxy dials and redirects known
// checked IP:port targets to a real local listener.
type fakeDialer struct {
	mu     sync.Mutex
	routes map[string]string
	dialed []string
}

func (d *fakeDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	d.mu.Lock()
	d.dialed = append(d.dialed, address)
	real, ok := d.routes[address]
	d.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("fake dialer: no route for %s", address)
	}
	var nd net.Dialer
	return nd.DialContext(ctx, network, real)
}

func (d *fakeDialer) Dialed() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.dialed...)
}

type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuf) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func (s *syncBuf) lines(t *testing.T) []LogLine {
	t.Helper()
	var out []LogLine
	for _, l := range strings.Split(strings.TrimSpace(s.String()), "\n") {
		if l == "" {
			continue
		}
		var ll LogLine
		if err := json.Unmarshal([]byte(l), &ll); err != nil {
			t.Fatalf("log line is not JSON: %q: %v", l, err)
		}
		out = append(out, ll)
	}
	return out
}

// waitLog polls until a log line with the given reason appears.
func (s *syncBuf) waitLog(t *testing.T, reason string) LogLine {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		for _, l := range s.lines(t) {
			if l.Reason == reason {
				return l
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("no log line with reason %q; log=%q", reason, s.String())
	return LogLine{}
}

type harness struct {
	proxy  *Proxy
	addr   string
	log    *syncBuf
	dialer *fakeDialer
}

func newHarness(t *testing.T, allow []string, res fakeResolver, routes map[string]string) *harness {
	t.Helper()
	al, err := egress.Parse(allow)
	if err != nil {
		t.Fatalf("parse allowlist: %v", err)
	}
	h := &harness{log: &syncBuf{}, dialer: &fakeDialer{routes: routes}}
	h.proxy = &Proxy{Allow: al, Resolver: res, Dialer: h.dialer, Log: h.log, TaskID: "task-1"}
	srv := httptest.NewServer(h.proxy)
	t.Cleanup(srv.Close)
	h.addr = srv.Listener.Addr().String()
	return h
}

// connect issues a CONNECT and returns the conn, a reader positioned after
// the response headers, and the response.
func (h *harness) connect(t *testing.T, target string) (net.Conn, *bufio.Reader, *http.Response) {
	t.Helper()
	c, err := net.Dial("tcp", h.addr)
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	t.Cleanup(func() { c.Close() })
	_ = c.SetDeadline(time.Now().Add(10 * time.Second))
	fmt.Fprintf(c, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target)
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatalf("read CONNECT response: %v", err)
	}
	return c, br, resp
}

// bufConn reads through the bufio.Reader that consumed the CONNECT reply.
type bufConn struct {
	net.Conn
	r *bufio.Reader
}

func (b *bufConn) Read(p []byte) (int, error) { return b.r.Read(p) }

func expectDeny(t *testing.T, h *harness, resp *http.Response, reason string) {
	t.Helper()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", resp.StatusCode)
	}
	if got := resp.Header.Get("X-Mod-Egress-Deny"); got != reason {
		t.Fatalf("deny header = %q, want %q", got, reason)
	}
	h.log.waitLog(t, reason)
	if len(h.dialer.Dialed()) != 0 {
		t.Fatalf("denied request dialed upstream: %v", h.dialer.Dialed())
	}
}

func tlsUpstream(t *testing.T, body string) *httptest.Server {
	t.Helper()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func echoUpstream(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				io.Copy(c, c)
			}()
		}
	}()
	return ln.Addr().String()
}

func TestProxyConnectAllowedTLS(t *testing.T) {
	up := tlsUpstream(t, "hello-tls")
	h := newHarness(t, []string{"api.example.com"},
		fakeResolver{"api.example.com": {netip.MustParseAddr("203.0.113.10")}},
		map[string]string{"203.0.113.10:443": up.Listener.Addr().String()})

	c, br, resp := h.connect(t, "api.example.com:443")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT status = %d, want 200", resp.StatusCode)
	}
	tc := tls.Client(&bufConn{Conn: c, r: br}, &tls.Config{ServerName: "api.example.com", InsecureSkipVerify: true})
	if err := tc.Handshake(); err != nil {
		t.Fatalf("TLS through tunnel: %v", err)
	}
	req, _ := http.NewRequest(http.MethodGet, "https://api.example.com/", nil)
	if err := req.Write(tc); err != nil {
		t.Fatal(err)
	}
	r2, err := http.ReadResponse(bufio.NewReader(tc), req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(r2.Body)
	if string(b) != "hello-tls" {
		t.Fatalf("body = %q", b)
	}
	if d := h.dialer.Dialed(); len(d) != 1 || d[0] != "203.0.113.10:443" {
		t.Fatalf("dialed %v, want the checked IP 203.0.113.10:443", d)
	}
	for _, l := range h.log.lines(t) {
		if l.Ev == "egress_deny" {
			t.Fatalf("allowed request logged a deny: %+v", l)
		}
	}
}

func TestProxyDeniedHost(t *testing.T) {
	h := newHarness(t, []string{"api.example.com"},
		fakeResolver{"evil.com": {netip.MustParseAddr("203.0.113.66")}}, nil)
	_, _, resp := h.connect(t, "evil.com:443")
	expectDeny(t, h, resp, ReasonNotAllowlisted)
}

func TestProxyDeniedPort(t *testing.T) {
	h := newHarness(t, []string{"api.example.com"},
		fakeResolver{"api.example.com": {netip.MustParseAddr("203.0.113.10")}}, nil)
	_, _, resp := h.connect(t, "api.example.com:8443")
	expectDeny(t, h, resp, ReasonNotAllowlisted)
}

func TestProxyWildcardToPrivateDenied(t *testing.T) {
	h := newHarness(t, []string{"*.internal.net"},
		fakeResolver{"secret.internal.net": {netip.MustParseAddr("10.1.2.3")}}, nil)
	_, _, resp := h.connect(t, "secret.internal.net:443")
	expectDeny(t, h, resp, ReasonPrivateWildcard)
}

func TestProxyExactToPrivateAllowed(t *testing.T) {
	h := newHarness(t, []string{"internal.host"},
		fakeResolver{"internal.host": {netip.MustParseAddr("10.2.3.4")}},
		map[string]string{"10.2.3.4:80": echoUpstream(t)})
	c, br, resp := h.connect(t, "internal.host:80")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT status = %d, want 200", resp.StatusCode)
	}
	if _, err := io.WriteString(c, "PING"); err != nil {
		t.Fatal(err)
	}
	if err := c.(*net.TCPConn).CloseWrite(); err != nil {
		t.Fatal(err)
	}
	b, err := io.ReadAll(br)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "PING" {
		t.Fatalf("echo = %q", b)
	}
	if d := h.dialer.Dialed(); len(d) != 1 || d[0] != "10.2.3.4:80" {
		t.Fatalf("dialed %v, want 10.2.3.4:80", d)
	}
}

func TestProxyMetadataIPDenied(t *testing.T) {
	h := newHarness(t, []string{"169.254.169.254"},
		fakeResolver{"169.254.169.254": {netip.MustParseAddr("169.254.169.254")}}, nil)
	_, _, resp := h.connect(t, "169.254.169.254:443")
	expectDeny(t, h, resp, ReasonMetadataIP)
}

// TestProxySNIMismatchDenied: a wildcard-allowed CONNECT host with a sibling
// SNI (domain fronting) is torn down after the 200 and logged.
func TestProxySNIMismatchDenied(t *testing.T) {
	up := tlsUpstream(t, "hello")
	var reached sync.Mutex
	hits := 0
	up.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached.Lock()
		hits++
		reached.Unlock()
	})
	h := newHarness(t, []string{"*.example.com"},
		fakeResolver{"api.example.com": {netip.MustParseAddr("203.0.113.10")}},
		map[string]string{"203.0.113.10:443": up.Listener.Addr().String()})

	c, br, resp := h.connect(t, "api.example.com:443")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT status = %d, want 200", resp.StatusCode)
	}
	tc := tls.Client(&bufConn{Conn: c, r: br}, &tls.Config{ServerName: "evil.example.com", InsecureSkipVerify: true})
	if err := tc.Handshake(); err == nil {
		t.Fatal("TLS handshake with mismatched SNI succeeded; want tunnel torn down")
	}
	l := h.log.waitLog(t, ReasonSNIMismatch)
	if l.SNI != "evil.example.com" || l.Host != "api.example.com" || l.Port != 443 || l.IP != "203.0.113.10" {
		t.Fatalf("log line = %+v", l)
	}
	reached.Lock()
	defer reached.Unlock()
	if hits != 0 {
		t.Fatalf("upstream served %d requests after SNI mismatch", hits)
	}
}

func proxyClient(h *harness) *http.Client {
	u, _ := url.Parse("http://" + h.addr)
	return &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(u)}, Timeout: 10 * time.Second}
}

func TestProxyHTTPForwardAbsoluteForm(t *testing.T) {
	var mu sync.Mutex
	var gotAuth, gotHost string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotAuth, gotHost = r.Header.Get("Proxy-Authorization"), r.Host
		mu.Unlock()
		io.WriteString(w, r.Method+" "+r.URL.RequestURI())
	}))
	t.Cleanup(up.Close)
	h := newHarness(t, []string{"cdn.example.com"},
		fakeResolver{"cdn.example.com": {netip.MustParseAddr("203.0.113.20")}},
		map[string]string{"203.0.113.20:80": up.Listener.Addr().String()})

	req, _ := http.NewRequest(http.MethodGet, "http://cdn.example.com/path?q=1", nil)
	req.Header.Set("Proxy-Authorization", "Basic c2VjcmV0")
	resp, err := proxyClient(h).Do(req)
	if err != nil {
		t.Fatalf("forward request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	b, _ := io.ReadAll(resp.Body)
	if string(b) != "GET /path?q=1" {
		t.Fatalf("body = %q", b)
	}
	mu.Lock()
	defer mu.Unlock()
	if gotAuth != "" {
		t.Fatalf("hop-by-hop Proxy-Authorization forwarded upstream: %q", gotAuth)
	}
	if gotHost != "cdn.example.com" {
		t.Fatalf("upstream Host = %q", gotHost)
	}
	if d := h.dialer.Dialed(); len(d) != 1 || d[0] != "203.0.113.20:80" {
		t.Fatalf("dialed %v", d)
	}
	for _, l := range h.log.lines(t) {
		if l.Ev == "egress_deny" {
			t.Fatalf("allowed request logged a deny: %+v", l)
		}
	}
}

func TestProxyLogLineFormat(t *testing.T) {
	h := newHarness(t, []string{"api.example.com"},
		fakeResolver{"bad.com": {netip.MustParseAddr("10.1.1.1")}}, nil)
	_, _, resp := h.connect(t, "bad.com:443")
	expectDeny(t, h, resp, ReasonNotAllowlisted)

	lines := h.log.lines(t)
	if len(lines) != 1 {
		t.Fatalf("line count = %d", len(lines))
	}
	l := lines[0]
	if l.Ev != "egress_deny" {
		t.Errorf("ev = %q, want egress_deny", l.Ev)
	}
	if l.Method != "CONNECT" {
		t.Errorf("method = %q, want CONNECT", l.Method)
	}
	if l.Host != "bad.com" {
		t.Errorf("host = %q, want bad.com", l.Host)
	}
	if l.Port != 443 {
		t.Errorf("port = %d", l.Port)
	}
	if l.Reason != ReasonNotAllowlisted {
		t.Errorf("reason = %q", l.Reason)
	}
	if l.TaskID != "task-1" {
		t.Errorf("task_id = %q", l.TaskID)
	}
	if l.IP != "" {
		t.Errorf("ip = %q, want empty (no resolved IP)", l.IP)
	}
	if _, err := time.Parse(time.RFC3339Nano, l.TS); err != nil {
		t.Errorf("ts = %q is not RFC3339Nano", l.TS)
	}
}

func TestProxyIPLiteralAllowed(t *testing.T) {
	h := newHarness(t, []string{"203.0.113.30"},
		fakeResolver{"203.0.113.30": {netip.MustParseAddr("203.0.113.30")}},
		map[string]string{"203.0.113.30:443": echoUpstream(t)})
	c, br, resp := h.connect(t, "203.0.113.30:443")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT status = %d, want 200", resp.StatusCode)
	}
	if _, err := io.WriteString(c, "PING"); err != nil {
		t.Fatal(err)
	}
	if err := c.(*net.TCPConn).CloseWrite(); err != nil {
		t.Fatal(err)
	}
	b, err := io.ReadAll(br)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "PING" {
		t.Fatalf("echo = %q", b)
	}
	if d := h.dialer.Dialed(); len(d) != 1 || d[0] != "203.0.113.30:443" {
		t.Fatalf("dialed %v", d)
	}
}

func TestProxyHTTPForwardDenied(t *testing.T) {
	h := newHarness(t, []string{"cdn.example.com"},
		fakeResolver{"evil.com": {netip.MustParseAddr("203.0.113.66")}}, nil)
	resp, err := proxyClient(h).Get("http://evil.com/x")
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	expectDeny(t, h, resp, ReasonNotAllowlisted)
	if l := h.log.lines(t)[0]; l.Method != "GET" || l.Host != "evil.com" || l.Port != 80 {
		t.Fatalf("log line = %+v", l)
	}
}

// TestProxyAllowLineLogged: an allowed CONNECT writes exactly one allow line,
// even when the same tunnel is established twice (deduplicated per host:port).
func TestProxyAllowLineLogged(t *testing.T) {
	h := newHarness(t, []string{"api.example.com"},
		fakeResolver{"api.example.com": {netip.MustParseAddr("203.0.113.10")}},
		map[string]string{"203.0.113.10:443": echoUpstream(t)})

	if _, _, resp := h.connect(t, "api.example.com:443"); resp.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT status = %d, want 200", resp.StatusCode)
	}
	if _, _, resp := h.connect(t, "api.example.com:443"); resp.StatusCode != http.StatusOK {
		t.Fatalf("second CONNECT status = %d, want 200", resp.StatusCode)
	}

	allowLines := h.log.lines(t)
	if len(allowLines) != 1 {
		t.Fatalf("allow line count = %d, want 1 (deduped per host:port); lines=%+v", len(allowLines), allowLines)
	}
	l := allowLines[0]
	if l.Ev != "egress_allow" {
		t.Errorf("ev = %q, want egress_allow", l.Ev)
	}
	if l.Method != "CONNECT" || l.Host != "api.example.com" || l.Port != 443 {
		t.Errorf("log line = %+v", l)
	}
	if l.Reason != "" {
		t.Errorf("reason = %q, want empty", l.Reason)
	}
}

// holdUpstream accepts TCP connections and holds each one open until the proxy
// half-closes it. It counts accepted-but-not-yet-closed connections.
type holdUpstream struct {
	addr   string
	mu     sync.Mutex
	active int
	max    int
}

func newHoldUpstream(t *testing.T) *holdUpstream {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	u := &holdUpstream{addr: ln.Addr().String()}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			u.mu.Lock()
			u.active++
			if u.active > u.max {
				u.max = u.active
			}
			u.mu.Unlock()
			go func() {
				defer c.Close()
				_, _ = io.Copy(io.Discard, c)
				u.mu.Lock()
				u.active--
				u.mu.Unlock()
			}()
		}
	}()
	return u
}

func (u *holdUpstream) stats() (active, max int) {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.active, u.max
}

// tunnelStatus issues a CONNECT and returns the conn (caller must close it) and
// the status code. It never calls t.Fatal, so it is safe in worker goroutines.
func tunnelStatus(addr, target string) (net.Conn, int, error) {
	c, err := net.Dial("tcp", addr)
	if err != nil {
		return nil, 0, err
	}
	_ = c.SetDeadline(time.Now().Add(10 * time.Second))
	fmt.Fprintf(c, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target)
	resp, err := http.ReadResponse(bufio.NewReader(c), &http.Request{Method: http.MethodConnect})
	if err != nil {
		c.Close()
		return nil, 0, err
	}
	resp.Body.Close()
	return c, resp.StatusCode, nil
}

// newBoundedHarness: a.example.com and b.example.com route to the holding
// upstream; c.example.com resolves but has no route (dial always fails).
func newBoundedHarness(t *testing.T, maxTunnels int) (*harness, *holdUpstream) {
	t.Helper()
	up := newHoldUpstream(t)
	h := newHarness(t, []string{"a.example.com", "b.example.com", "c.example.com"},
		fakeResolver{
			"a.example.com": {netip.MustParseAddr("203.0.113.10")},
			"b.example.com": {netip.MustParseAddr("203.0.113.11")},
			"c.example.com": {netip.MustParseAddr("203.0.113.12")},
		},
		map[string]string{"203.0.113.10:80": up.addr, "203.0.113.11:80": up.addr})
	h.proxy.MaxTunnels = maxTunnels
	return h, up
}

// waitSlotsFree polls until every tunnel slot has been released.
func waitSlotsFree(t *testing.T, h *harness) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for len(h.proxy.sem) != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("slots still held: len(sem)=%d, want 0", len(h.proxy.sem))
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// TestProxyTunnelBoundRejectsOverflow: with MaxTunnels=2 the 3rd concurrent
// CONNECT is refused with 503 and never dials; closing one tunnel frees
// exactly one slot; after all tunnels close every slot is released.
func TestProxyTunnelBoundRejectsOverflow(t *testing.T) {
	h, up := newBoundedHarness(t, 2)
	const target = "a.example.com:80"
	var held []net.Conn
	defer func() {
		for _, c := range held {
			c.Close()
		}
	}()
	for i := 0; i < 2; i++ {
		c, code, err := tunnelStatus(h.addr, target)
		if err != nil {
			t.Fatalf("tunnel %d: %v", i, err)
		}
		held = append(held, c)
		if code != http.StatusOK {
			t.Fatalf("tunnel %d: status %d, want 200", i, code)
		}
	}
	c3, code, err := tunnelStatus(h.addr, target)
	if err != nil {
		t.Fatalf("3rd tunnel: %v", err)
	}
	c3.Close()
	if code != http.StatusServiceUnavailable {
		t.Fatalf("3rd tunnel: status %d, want 503 (MaxTunnels=2)", code)
	}
	if _, max := up.stats(); max != 2 {
		t.Fatalf("upstream max concurrent = %d, want 2", max)
	}
	held[0].Close()
	deadline := time.Now().Add(5 * time.Second)
	for {
		c4, code, err := tunnelStatus(h.addr, target)
		if err == nil {
			c4.Close()
		}
		if err == nil && code == http.StatusOK {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no slot freed after closing a tunnel: status=%d err=%v", code, err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	held[1].Close()
	waitSlotsFree(t, h)
	if _, max := up.stats(); max > 2 {
		t.Fatalf("upstream max concurrent = %d, want <= 2", max)
	}
}

// TestProxyTunnelSlotReleasedOnDialFailure: a tunnel whose upstream dial fails
// returns its slot; with MaxTunnels=1 the next tunnel still succeeds.
func TestProxyTunnelSlotReleasedOnDialFailure(t *testing.T) {
	h, _ := newBoundedHarness(t, 1)
	c, code, err := tunnelStatus(h.addr, "c.example.com:80")
	if err != nil {
		t.Fatalf("dial-fail tunnel: %v", err)
	}
	c.Close()
	if code != http.StatusBadGateway {
		t.Fatalf("dial-fail tunnel: status %d, want 502", code)
	}
	waitSlotsFree(t, h)
	c2, code2, err := tunnelStatus(h.addr, "a.example.com:80")
	if err != nil {
		t.Fatalf("follow-up tunnel: %v", err)
	}
	c2.Close()
	if code2 != http.StatusOK {
		t.Fatalf("follow-up tunnel after dial failure: status %d, want 200 (slot leaked?)", code2)
	}
	waitSlotsFree(t, h)
}

// TestProxyTunnelStressBound: 50 goroutines open and close tunnels against
// MaxTunnels=4. Upstream never sees more than 4 concurrent tunnels and every
// slot is free at the end.
func TestProxyTunnelStressBound(t *testing.T) {
	const n = 4
	h, up := newBoundedHarness(t, n)
	var (
		wg       sync.WaitGroup
		mu       sync.Mutex
		ok, busy int
		errs     []string
	)
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, code, err := tunnelStatus(h.addr, "a.example.com:80")
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err != nil:
				errs = append(errs, err.Error())
			case code == http.StatusOK:
				ok++
			case code == http.StatusServiceUnavailable:
				busy++
			default:
				errs = append(errs, fmt.Sprintf("status %d", code))
			}
			if c != nil {
				c.Close()
			}
		}()
	}
	wg.Wait()
	if len(errs) > 0 {
		t.Fatalf("unexpected results: %v", errs)
	}
	if ok == 0 {
		t.Fatalf("no tunnel succeeded (busy=%d)", busy)
	}
	waitSlotsFree(t, h)
	if active, max := up.stats(); active != 0 || max > n {
		t.Fatalf("upstream active=%d max=%d, want active=0 max<=%d (ok=%d busy=%d)", active, max, n, ok, busy)
	}
}
