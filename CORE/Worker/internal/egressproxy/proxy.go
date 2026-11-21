// Package egressproxy is the per-task egress proxy (GB.EGR.D.x). It enforces
// an egress.Allowlist for proxy-aware clients: HTTP CONNECT tunnels and
// absolute-form plain-HTTP forwarding. It never intercepts TLS (no MITM); on
// port 443 it only peeks the ClientHello SNI to reject domain fronting.
//
// Security properties:
//   - A hostname is resolved exactly once per request and the proxy dials the
//     IP it checked (no second resolution, so DNS rebinding cannot swap the
//     target after the check).
//   - Owner answer E3: an exact allowlisted hostname may resolve to a
//     private/LAN address; a wildcard match never may.
//   - Metadata addresses (169.254/16, fe80::/10, fd00:ec2::254) are always
//     denied, as are loopback, unspecified and multicast addresses.
//   - A CONNECT to an IP literal is allowed only by an IP/CIDR rule (MatchIP).
package egressproxy

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egress"
)

const (
	// ConnectTimeout bounds resolve + dial of the upstream.
	ConnectTimeout = 15 * time.Second
	// IdleTimeout closes a tunnel with no traffic in either direction.
	IdleTimeout = 5 * time.Minute
	// MaxTunnels is the default concurrent tunnel / forward limit.
	MaxTunnels = 256
	// sniPeekTimeout bounds how long the client may take to send its
	// ClientHello after the 200 reply on :443.
	sniPeekTimeout = 15 * time.Second
	// maxTLSRecord is the largest TLS plaintext record we accept to peek.
	maxTLSRecord = 16384 + 2048
)

// Deny reasons (stable strings, part of the log format).
const (
	ReasonNotAllowlisted  = "not_allowlisted"
	ReasonBadTarget       = "bad_target"
	ReasonResolveFailed   = "resolve_failed"
	ReasonMetadataIP      = "metadata_ip"
	ReasonForbiddenIP     = "forbidden_ip"
	ReasonPrivateWildcard = "private_ip_wildcard"
	ReasonSNIMismatch     = "sni_mismatch"
	ReasonBadClientHello  = "bad_client_hello"
	ReasonBadScheme       = "unsupported_scheme"
)

// Resolver resolves hostnames. *net.Resolver satisfies it.
type Resolver interface {
	LookupNetIP(ctx context.Context, network, host string) ([]netip.Addr, error)
}

// Dialer dials upstream connections. *net.Dialer satisfies it.
type Dialer interface {
	DialContext(ctx context.Context, network, address string) (net.Conn, error)
}

// Proxy is an allowlist-enforcing forward proxy. It implements http.Handler.
type Proxy struct {
	Allow    *egress.Allowlist
	Resolver Resolver  // nil → net.DefaultResolver
	Dialer   Dialer    // nil → &net.Dialer{}
	Log      io.Writer // JSON lines; nil → discarded
	// TaskID is stamped on every log line when set.
	TaskID string
	// MaxTunnels overrides the concurrency limit; 0 → MaxTunnels.
	MaxTunnels int

	once   sync.Once
	sem    chan struct{}
	logMu  sync.Mutex
	nowFn  func() time.Time
	idleTO time.Duration
	// allowed tracks host:port pairs seen in a successful CONNECT or
	// forwarded request, so the teardown report can list allowed hosts.
	// Guarded by logMu; capped at allowedCap entries so a busy tunnel
	// cannot flood it.
	allowed map[string]struct{}
}

// allowedCap bounds the distinct allowed host:port entries retained for the
// life of the Proxy.
const allowedCap = 256

// LogLine is the JSON shape written to Proxy.Log for every denial.
type LogLine struct {
	TS     string `json:"ts"`
	Ev     string `json:"ev"`
	TaskID string `json:"task_id,omitempty"`
	Method string `json:"method"`
	Host   string `json:"host"`
	Port   int    `json:"port,omitempty"`
	IP     string `json:"ip,omitempty"`
	SNI    string `json:"sni,omitempty"`
	Reason string `json:"reason,omitempty"`
}

type denyError struct {
	reason string
	ip     string
}

func (d *denyError) Error() string { return "egress denied: " + d.reason }

func (p *Proxy) init() {
	p.once.Do(func() {
		n := p.MaxTunnels
		if n <= 0 {
			n = MaxTunnels
		}
		p.sem = make(chan struct{}, n)
		if p.nowFn == nil {
			p.nowFn = time.Now
		}
		if p.idleTO <= 0 {
			p.idleTO = IdleTimeout
		}
		if p.allowed == nil {
			p.allowed = make(map[string]struct{})
		}
	})
}

func (p *Proxy) resolver() Resolver {
	if p.Resolver != nil {
		return p.Resolver
	}
	return net.DefaultResolver
}

func (p *Proxy) dialer() Dialer {
	if p.Dialer != nil {
		return p.Dialer
	}
	return &net.Dialer{}
}

func (p *Proxy) logDeny(method, host string, port int, ip, sni, reason string) {
	if p.Log == nil {
		return
	}
	line := LogLine{
		TS:     p.nowFn().UTC().Format(time.RFC3339Nano),
		Ev:     "egress_deny",
		TaskID: p.TaskID,
		Method: method,
		Host:   host,
		Port:   port,
		IP:     ip,
		SNI:    sni,
		Reason: reason,
	}
	b, err := json.Marshal(line)
	if err != nil {
		return
	}
	b = append(b, '\n')
	p.logMu.Lock()
	defer p.logMu.Unlock()
	_, _ = p.Log.Write(b)
}

// logAllow writes an egress_allow line for a host:port the first time it is
// seen in a successful CONNECT or forwarded request during the life of the
// Proxy. It is deduplicated so a busy tunnel cannot flood stdout.
func (p *Proxy) logAllow(method, host string, port int, ip string) {
	if p.Log == nil {
		return
	}
	key := host + ":" + strconv.Itoa(port)
	p.logMu.Lock()
	defer p.logMu.Unlock()
	if _, ok := p.allowed[key]; ok {
		return
	}
	if len(p.allowed) >= allowedCap {
		return
	}
	p.allowed[key] = struct{}{}
	line := LogLine{
		TS:     p.nowFn().UTC().Format(time.RFC3339Nano),
		Ev:     "egress_allow",
		TaskID: p.TaskID,
		Method: method,
		Host:   host,
		Port:   port,
		IP:     ip,
	}
	b, err := json.Marshal(line)
	if err != nil {
		return
	}
	b = append(b, '\n')
	_, _ = p.Log.Write(b)
}

func (p *Proxy) deny(w http.ResponseWriter, method, host string, port int, ip, reason string) {
	p.logDeny(method, host, port, ip, "", reason)
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Mod-Egress-Deny", reason)
	w.WriteHeader(http.StatusForbidden)
	_, _ = io.WriteString(w, "egress denied: "+reason+"\n")
}

// ServeHTTP implements http.Handler.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	p.init()
	if !p.acquire() {
		http.Error(w, "egress proxy: too many concurrent connections", http.StatusServiceUnavailable)
		return
	}
	// Function-level defer: the slot is held for the whole tunnel / forward
	// (handleConnect and splice run synchronously below) and released once.
	defer p.release()
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r)
		return
	}
	p.handleForward(w, r)
}

// acquire takes one tunnel slot without blocking; false when all MaxTunnels
// slots are in use. Every true return must be paired with exactly one release.
func (p *Proxy) acquire() bool {
	select {
	case p.sem <- struct{}{}:
		return true
	default:
		return false
	}
}

// release frees a slot taken by a successful acquire.
func (p *Proxy) release() { <-p.sem }

func normalizeHost(h string) string {
	h = strings.TrimSuffix(strings.TrimPrefix(h, "["), "]")
	return strings.TrimSuffix(strings.ToLower(strings.TrimSpace(h)), ".")
}

// forbiddenAlways reports addresses no rule can unlock.
func forbiddenAlways(ip netip.Addr) (bool, string) {
	if !ip.IsValid() {
		return true, ReasonForbiddenIP
	}
	if egress.IsMetadata(ip) {
		return true, ReasonMetadataIP
	}
	if ip.IsLoopback() || ip.IsUnspecified() || ip.IsMulticast() {
		return true, ReasonForbiddenIP
	}
	return false, ""
}

// checkTarget applies the allowlist and address policy, resolving a hostname
// once. It returns the single IP that must be dialed.
func (p *Proxy) checkTarget(ctx context.Context, host string, port int) (netip.Addr, error) {
	if host == "" || port < 1 || port > 65535 {
		return netip.Addr{}, &denyError{reason: ReasonBadTarget}
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		if ip.Zone() != "" {
			return netip.Addr{}, &denyError{reason: ReasonBadTarget}
		}
		ip = ip.Unmap()
		if bad, why := forbiddenAlways(ip); bad {
			return netip.Addr{}, &denyError{reason: why, ip: ip.String()}
		}
		if !p.Allow.MatchIP(ip, port) {
			return netip.Addr{}, &denyError{reason: ReasonNotAllowlisted, ip: ip.String()}
		}
		return ip, nil
	}
	ok, exact := p.Allow.Match(host, port)
	if !ok {
		return netip.Addr{}, &denyError{reason: ReasonNotAllowlisted}
	}
	addrs, err := p.resolver().LookupNetIP(ctx, "ip", host)
	if err != nil || len(addrs) == 0 {
		return netip.Addr{}, &denyError{reason: ReasonResolveFailed}
	}
	firstReason, firstIP := "", ""
	for _, a := range addrs {
		a = a.Unmap()
		reason := ""
		if bad, why := forbiddenAlways(a); bad {
			reason = why
		} else if !exact && egress.IsPrivate(a) {
			reason = ReasonPrivateWildcard
		}
		if reason == "" {
			return a, nil
		}
		if firstReason == "" {
			firstReason, firstIP = reason, a.String()
		}
	}
	return netip.Addr{}, &denyError{reason: firstReason, ip: firstIP}
}

func (p *Proxy) dialChecked(ctx context.Context, ip netip.Addr, port int) (net.Conn, error) {
	return p.dialer().DialContext(ctx, "tcp", netip.AddrPortFrom(ip, uint16(port)).String())
}

func (p *Proxy) handleConnect(w http.ResponseWriter, r *http.Request) {
	rawHost, rawPort, err := net.SplitHostPort(r.Host)
	host := normalizeHost(rawHost)
	port, perr := strconv.Atoi(rawPort)
	if err != nil || perr != nil {
		p.deny(w, r.Method, normalizeHost(r.Host), 0, "", ReasonBadTarget)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), ConnectTimeout)
	defer cancel()
	ip, err := p.checkTarget(ctx, host, port)
	if err != nil {
		var de *denyError
		if errors.As(err, &de) {
			p.deny(w, r.Method, host, port, de.ip, de.reason)
			return
		}
		p.deny(w, r.Method, host, port, "", ReasonBadTarget)
		return
	}
	upstream, err := p.dialChecked(ctx, ip, port)
	if err != nil {
		http.Error(w, "egress proxy: upstream dial failed", http.StatusBadGateway)
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		upstream.Close()
		http.Error(w, "egress proxy: hijack unsupported", http.StatusInternalServerError)
		return
	}
	client, brw, err := hj.Hijack()
	if err != nil {
		upstream.Close()
		return
	}
	defer client.Close()
	defer upstream.Close()
	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		return
	}
	p.logAllow(r.Method, host, port, ip.String())
	var clientR io.Reader = brw.Reader
	if port == 443 {
		_ = client.SetReadDeadline(time.Now().Add(sniPeekTimeout))
		peeked, sni, perr := peekClientHello(brw.Reader)
		_ = client.SetReadDeadline(time.Time{})
		if perr != nil {
			p.logDeny(r.Method, host, port, ip.String(), "", ReasonBadClientHello)
			return
		}
		if sni != "" && !sniAllowed(host, sni) {
			p.logDeny(r.Method, host, port, ip.String(), sni, ReasonSNIMismatch)
			return
		}
		if len(peeked) > 0 {
			if _, err := upstream.Write(peeked); err != nil {
				return
			}
		}
	}
	p.splice(client, clientR, upstream)
}

// sniAllowed: the SNI must equal the CONNECT host. A sibling host under the
// same wildcard rule is still a mismatch (domain fronting).
func sniAllowed(connectHost, sni string) bool {
	return normalizeHost(sni) == connectHost
}

// splice copies both directions until either side closes or the tunnel has
// been idle (no bytes in either direction) for the idle timeout.
func (p *Proxy) splice(client net.Conn, clientR io.Reader, upstream net.Conn) {
	var last atomic.Int64
	last.Store(time.Now().UnixNano())
	idle := p.idleTO
	done := make(chan struct{}, 2)
	pump := func(dst net.Conn, src net.Conn, r io.Reader) {
		defer func() { done <- struct{}{} }()
		buf := make([]byte, 32*1024)
		for {
			_ = src.SetReadDeadline(time.Now().Add(idle))
			n, err := r.Read(buf)
			if n > 0 {
				last.Store(time.Now().UnixNano())
				if _, werr := dst.Write(buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				var ne net.Error
				if errors.As(err, &ne) && ne.Timeout() &&
					time.Since(time.Unix(0, last.Load())) < idle {
					continue
				}
				if tc, ok := dst.(interface{ CloseWrite() error }); ok && err == io.EOF {
					_ = tc.CloseWrite()
					return
				}
				return
			}
		}
	}
	go pump(upstream, client, clientR)
	go pump(client, upstream, upstream)
	<-done
	// Give the other direction a chance to drain after a half-close, bounded
	// by the idle timeout, then tear both down.
	t := time.NewTimer(idle)
	select {
	case <-done:
	case <-t.C:
	}
	t.Stop()
	_ = client.Close()
	_ = upstream.Close()
}

// peekClientHello reads the first TLS record if the stream starts with a TLS
// handshake. It returns the bytes consumed (to forward upstream) and the SNI
// host_name ("" if absent or not TLS). A handshake record that cannot be
// parsed is an error (fail closed).
func peekClientHello(br *bufio.Reader) ([]byte, string, error) {
	first, err := br.Peek(1)
	if err != nil {
		if err == io.EOF {
			return nil, "", nil
		}
		return nil, "", err
	}
	if first[0] != 0x16 {
		return nil, "", nil
	}
	hdr := make([]byte, 5)
	if _, err := io.ReadFull(br, hdr); err != nil {
		return nil, "", err
	}
	n := int(binary.BigEndian.Uint16(hdr[3:5]))
	if n == 0 || n > maxTLSRecord {
		return nil, "", errors.New("bad tls record length")
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(br, body); err != nil {
		return nil, "", err
	}
	sni, err := parseClientHelloSNI(body)
	if err != nil {
		return nil, "", err
	}
	return append(hdr, body...), sni, nil
}

type reader struct {
	b   []byte
	bad bool
}

func (r *reader) take(n int) []byte {
	if r.bad || n < 0 || len(r.b) < n {
		r.bad = true
		return nil
	}
	v := r.b[:n]
	r.b = r.b[n:]
	return v
}

func (r *reader) u8() int {
	v := r.take(1)
	if v == nil {
		return 0
	}
	return int(v[0])
}

func (r *reader) u16() int {
	v := r.take(2)
	if v == nil {
		return 0
	}
	return int(binary.BigEndian.Uint16(v))
}

func (r *reader) u24() int {
	v := r.take(3)
	if v == nil {
		return 0
	}
	return int(v[0])<<16 | int(v[1])<<8 | int(v[2])
}

// parseClientHelloSNI parses a handshake record body holding a ClientHello.
func parseClientHelloSNI(body []byte) (string, error) {
	r := &reader{b: body}
	if r.u8() != 1 {
		return "", errors.New("not a client hello")
	}
	hl := r.u24()
	if r.bad {
		return "", errors.New("truncated handshake")
	}
	if hl > len(r.b) {
		return "", errors.New("client hello spans records")
	}
	h := &reader{b: r.take(hl)}
	h.take(2)  // legacy_version
	h.take(32) // random
	h.take(h.u8())
	h.take(h.u16())
	h.take(h.u8())
	if h.bad {
		return "", errors.New("truncated client hello")
	}
	if len(h.b) == 0 {
		return "", nil
	}
	ext := &reader{b: h.take(h.u16())}
	if h.bad {
		return "", errors.New("truncated extensions")
	}
	sni := ""
	seen := false
	for len(ext.b) > 0 {
		typ := ext.u16()
		data := ext.take(ext.u16())
		if ext.bad {
			return "", errors.New("truncated extension")
		}
		if typ != 0 {
			continue
		}
		if seen {
			return "", errors.New("duplicate server_name")
		}
		seen = true
		sr := &reader{b: data}
		list := &reader{b: sr.take(sr.u16())}
		if sr.bad {
			return "", errors.New("bad server_name")
		}
		for len(list.b) > 0 {
			nt := list.u8()
			name := list.take(list.u16())
			if list.bad {
				return "", errors.New("bad server_name entry")
			}
			if nt == 0 {
				if sni != "" {
					return "", errors.New("multiple host_name entries")
				}
				if len(name) == 0 {
					return "", errors.New("empty host_name")
				}
				sni = string(name)
			}
		}
	}
	return sni, nil
}

var hopHeaders = []string{
	"Connection", "Proxy-Connection", "Keep-Alive", "Proxy-Authenticate",
	"Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade",
}

func stripHop(h http.Header) {
	for _, v := range h.Values("Connection") {
		for _, f := range strings.Split(v, ",") {
			if f = strings.TrimSpace(f); f != "" {
				h.Del(f)
			}
		}
	}
	for _, k := range hopHeaders {
		h.Del(k)
	}
}

func (p *Proxy) handleForward(w http.ResponseWriter, r *http.Request) {
	if !r.URL.IsAbs() || r.URL.Host == "" {
		http.Error(w, "egress proxy: absolute-form request required", http.StatusBadRequest)
		return
	}
	host := normalizeHost(r.URL.Hostname())
	port := 80
	if ps := r.URL.Port(); ps != "" {
		n, err := strconv.Atoi(ps)
		if err != nil {
			p.deny(w, r.Method, host, 0, "", ReasonBadTarget)
			return
		}
		port = n
	}
	if !strings.EqualFold(r.URL.Scheme, "http") {
		p.deny(w, r.Method, host, port, "", ReasonBadScheme)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), ConnectTimeout)
	ip, err := p.checkTarget(ctx, host, port)
	cancel()
	if err != nil {
		var de *denyError
		if errors.As(err, &de) {
			p.deny(w, r.Method, host, port, de.ip, de.reason)
			return
		}
		p.deny(w, r.Method, host, port, "", ReasonBadTarget)
		return
	}
	target := netip.AddrPortFrom(ip, uint16(port)).String()
	tr := &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			dctx, dcancel := context.WithTimeout(ctx, ConnectTimeout)
			defer dcancel()
			return p.dialer().DialContext(dctx, "tcp", target)
		},
		DisableKeepAlives:     true,
		DisableCompression:    true,
		ResponseHeaderTimeout: p.idleTO,
		IdleConnTimeout:       p.idleTO,
	}
	defer tr.CloseIdleConnections()

	out := r.Clone(r.Context())
	out.RequestURI = ""
	out.Header = r.Header.Clone()
	stripHop(out.Header)
	if r.ContentLength == 0 {
		out.Body = nil
	}
	resp, err := tr.RoundTrip(out)
	if err != nil {
		http.Error(w, fmt.Sprintf("egress proxy: upstream error: %v", err), http.StatusBadGateway)
		return
	}
	p.logAllow(r.Method, host, port, ip.String())
	defer resp.Body.Close()
	stripHop(resp.Header)
	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}
