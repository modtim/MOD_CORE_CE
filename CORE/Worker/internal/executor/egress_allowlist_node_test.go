package executor

// Node-task egress allowlist enforcement (external-executor-nodes task 7.3):
// a node whose payload has a non-empty AllowedEgress is routed, through the
// default "enforce" egress-proxy mode, onto a private internal bridge behind
// the per-task proxy. This file proves, with no real Docker, that (i) an
// allowlisted host is permitted by the proxy the setup actually configures,
// and (ii) a non-configured host is denied at both the proxy (403 +
// egress_deny) and the task's DNS stub (NXDOMAIN + dns_deny).

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egress"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egressproxy"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"golang.org/x/net/dns/dnsmessage"
)

// nodeFakeResolver is a fixed hostname → addresses map.
type nodeFakeResolver map[string][]netip.Addr

func (f nodeFakeResolver) LookupNetIP(_ context.Context, _, host string) ([]netip.Addr, error) {
	if a, ok := f[host]; ok {
		return a, nil
	}
	return nil, fmt.Errorf("no such host: %s", host)
}

// nodeFakeDialer records every address dialed and routes known IP:port
// targets to real local listeners.
type nodeFakeDialer struct {
	mu     sync.Mutex
	routes map[string]string
	dialed []string
}

func (d *nodeFakeDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	d.mu.Lock()
	d.dialed = append(d.dialed, address)
	real, ok := d.routes[address]
	d.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("node fake dialer: no route for %s", address)
	}
	var nd net.Dialer
	return nd.DialContext(ctx, network, real)
}

func (d *nodeFakeDialer) Dialed() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.dialed...)
}

// nodeLogBuf is a mutex-guarded line buffer for proxy/DNS JSON logs.
type nodeLogBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *nodeLogBuf) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *nodeLogBuf) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// nodeAllowedEgressPayload is the node task under test: no API_KEY, no
// NEEDS_INTERNET, just a populated AllowedEgress allowlist.
func nodeAllowedEgressPayload() transport.TaskPayload {
	return transport.TaskPayload{AllowedEgress: []string{"api.example.com"}}
}

// TestEgressAllowlistNodeWiring proves the Docker-path wiring: the
// AllowedEgress verdict is rewritten into the proxy bridge in the default
// "enforce" mode, the proxy container is scoped to exactly the node's
// allowlist, and the task container is swapped onto the internal bridge with
// proxy env and the NXDOMAIN-only DNS stub.
func TestEgressAllowlistNodeWiring(t *testing.T) {
	_ = stubEgressSubnet(t, nil)
	payload := nodeAllowedEgressPayload()

	sel := SelectNetworkMode(payload, "mod-main", "mod-user-internal", "mod-user-egress")
	if !sel.FromAllowlist || sel.DockerNetwork != "mod-user-egress" {
		t.Fatalf("network selection = %+v, want FromAllowlist on mod-user-egress", sel)
	}
	if !hasStr(sel.EnvAdditions, "MOD_ALLOWED_EGRESS_HOSTS=api.example.com") {
		t.Errorf("selection env = %v", sel.EnvAdditions)
	}

	// Default proxy mode is "enforce" (egressProxyModeFromEnv).
	sel = ApplyEgressProxyMode(sel, "enforce")
	if !sel.Proxied || !hasStr(sel.EnvAdditions, "MOD_EGRESS_ENFORCED=true") {
		t.Fatalf("apply proxy mode: proxied=%v env=%v", sel.Proxied, sel.EnvAdditions)
	}

	f := &fakeEgressDocker{health: types.Healthy}
	h, err := setupEgressProxy(context.Background(), f, "task1234-node", "mod/egress-proxy:1",
		payload.AllowedEgress, "mod-user-egress", nil)
	if err != nil {
		t.Fatalf("setupEgressProxy: %v", err)
	}
	// The proxy is configured with exactly the node's allowlist.
	allow := ""
	for _, e := range f.cfg.Env {
		if v, ok := strings.CutPrefix(e, "MOD_EGRESS_ALLOW="); ok {
			allow = v
		}
	}
	if allow != "api.example.com" {
		t.Fatalf("proxy MOD_EGRESS_ALLOW = %q, want %q", allow, "api.example.com")
	}
	al, err := egress.ParseList(allow)
	if err != nil {
		t.Fatalf("parse the configured allowlist: %v", err)
	}
	mOK, _ := al.Match("api.example.com", 443)
	mBad, _ := al.Match("evil.com", 443)
	if al.Len() != 1 || !mOK || mBad {
		t.Fatalf("parsed allowlist matches wrong hosts (ok=%v bad=%v)", mOK, mBad)
	}

	// The task container is swapped onto the internal bridge with the proxy
	// env and the stub DNS — it has no other route out.
	containerNetwork := sel.DockerNetwork
	env := append([]string{}, sel.EnvAdditions...)
	hc := &container.HostConfig{NetworkMode: container.NetworkMode(containerNetwork)}
	applyEgressHandle(h, &containerNetwork, &env, hc)
	if containerNetwork != h.Network || hc.NetworkMode != container.NetworkMode(h.Network) {
		t.Errorf("task network = %q / %v, want %q", containerNetwork, hc.NetworkMode, h.Network)
	}
	if !hasStr(env, "HTTPS_PROXY=http://mod-egress-proxy:3128") || !hasStr(env, "MOD_EGRESS_ENFORCED=true") {
		t.Errorf("task env = %v", env)
	}
	if len(hc.DNS) != 1 || hc.DNS[0] != h.ProxyIP {
		t.Errorf("task DNS = %v, want [%s]", hc.DNS, h.ProxyIP)
	}
}

// TestEgressAllowlistNodeAllowedHost proves (i): the proxy configured by
// setupEgressProxy permits a CONNECT for an allowlisted host and dials the
// checked IP, with no deny logged.
func TestEgressAllowlistNodeAllowedHost(t *testing.T) {
	up := listenOnce(t)
	dialer := &nodeFakeDialer{routes: map[string]string{"203.0.113.10:80": up}}
	log := &nodeLogBuf{}
	allow, err := egress.ParseList("api.example.com")
	if err != nil {
		t.Fatalf("parse allowlist: %v", err)
	}
	p := &egressproxy.Proxy{
		Allow:    allow,
		Resolver: nodeFakeResolver{"api.example.com": {netip.MustParseAddr("203.0.113.10")}},
		Dialer:   dialer,
		Log:      log,
		TaskID:   "task1234-node",
	}

	resp := proxyConnect(t, p, "api.example.com:80")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT status = %d, want 200", resp.StatusCode)
	}
	if d := dialer.Dialed(); len(d) != 1 || d[0] != "203.0.113.10:80" {
		t.Fatalf("dialed %v, want the checked IP 203.0.113.10:80", d)
	}
	if s := log.String(); strings.Contains(s, `"ev":"egress_deny"`) {
		t.Fatalf("allowed request logged a deny: %s", s)
	}
}

// TestEgressAllowlistNodeDeniedHost proves (ii): a host that is not in the
// node's AllowedEgress is denied — the proxy 403s it without dialing and
// logs egress_deny (surfaced in the run's egress report), and the task's DNS
// stub NXDOMAINs it with a dns_deny line.
func TestEgressAllowlistNodeDeniedHost(t *testing.T) {
	log := &nodeLogBuf{}
	allow, err := egress.ParseList("api.example.com")
	if err != nil {
		t.Fatalf("parse allowlist: %v", err)
	}
	dialer := &nodeFakeDialer{}
	p := &egressproxy.Proxy{
		Allow:    allow,
		Resolver: nodeFakeResolver{"evil.com": {netip.MustParseAddr("203.0.113.66")}},
		Dialer:   dialer,
		Log:      log,
		TaskID:   "task1234-node",
	}

	resp := proxyConnect(t, p, "evil.com:443")
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("CONNECT status = %d, want 403", resp.StatusCode)
	}
	if got := resp.Header.Get("X-Mod-Egress-Deny"); got != "not_allowlisted" {
		t.Fatalf("deny header = %q, want not_allowlisted", got)
	}
	if d := dialer.Dialed(); len(d) != 0 {
		t.Fatalf("denied request dialed upstream: %v", d)
	}

	// The denial reaches the run's egress report through the existing
	// aggregation seam.
	report := aggregateProxyLog(strings.NewReader(log.String()), []string{"api.example.com"})
	if report.Mode != "proxy" || len(report.Denied) != 1 {
		t.Fatalf("report = %+v", report)
	}
	dn := report.Denied[0]
	if dn.Host != "evil.com" || dn.Port != 443 || dn.Reason != "not_allowlisted" || dn.Count != 1 {
		t.Fatalf("denied tuple = %+v", dn)
	}

	// DNS layer: the task's stub resolves nothing and logs dns_deny.
	dnsLog := &nodeLogBuf{}
	stub, err := egressproxy.ListenDNSStub("127.0.0.1:0", dnsLog)
	if err != nil {
		t.Fatalf("listen dns stub: %v", err)
	}
	stub.TaskID = "task1234-node"
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go stub.Serve(ctx)
	defer stub.Close()

	msg := dnsmessage.Message{
		Header: dnsmessage.Header{ID: 7, RecursionDesired: true},
		Questions: []dnsmessage.Question{{
			Name:  dnsmessage.MustNewName("evil.com."),
			Type:  dnsmessage.TypeA,
			Class: dnsmessage.ClassINET,
		}},
	}
	data, _ := msg.Pack()
	conn, err := net.Dial("udp", stub.Addr())
	if err != nil {
		t.Fatalf("dial dns stub: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Write(data); err != nil {
		t.Fatalf("write dns query: %v", err)
	}
	buf := make([]byte, 512)
	n, err := conn.Read(buf)
	if err != nil {
		t.Fatalf("read dns reply: %v", err)
	}
	var reply dnsmessage.Message
	if err := reply.Unpack(buf[:n]); err != nil {
		t.Fatalf("unpack dns reply: %v", err)
	}
	if reply.Header.RCode != dnsmessage.RCodeNameError {
		t.Fatalf("dns rcode = %d, want NXDOMAIN", reply.Header.RCode)
	}
	if s := dnsLog.String(); !strings.Contains(s, `"ev":"dns_deny"`) || !strings.Contains(s, `"name":"evil.com."`) {
		t.Fatalf("dns log missing dns_deny: %s", s)
	}
}

// listenOnce returns a local listener address that accepts (and closes)
// connections, standing in for the allowlisted upstream.
func listenOnce(t *testing.T) string {
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
				io.Copy(io.Discard, c)
			}()
		}
	}()
	return ln.Addr().String()
}

// proxyConnect serves p in a test server and issues one CONNECT, returning
// the response (the client conn is closed before returning).
func proxyConnect(t *testing.T, p *egressproxy.Proxy, target string) *http.Response {
	t.Helper()
	srv := newTestProxyServer(t, p)
	c, err := net.Dial("tcp", srv)
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := fmt.Fprintf(c, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target); err != nil {
		t.Fatalf("write CONNECT: %v", err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(c), &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatalf("read CONNECT response: %v", err)
	}
	return resp
}

// newTestProxyServer runs p behind httptest and returns the address.
func newTestProxyServer(t *testing.T, p *egressproxy.Proxy) string {
	t.Helper()
	srv := httptest.NewServer(p)
	t.Cleanup(srv.Close)
	return srv.Listener.Addr().String()
}
