// Gateway is the declared-egress gateway (C10): an http.Handler forward proxy
// that takes its allowlist from a keyed bearer token minted by the MOD API
// (services/egress_gateway_token.py) instead of from a per-task allowlist file.
// The gateway re-validates the token WITHOUT calling back into MOD: it
// recomputes the HMAC from the same platform secret.
//
// Wire format (must match the Python minter byte-for-byte):
//
//	claims = {"v": 1, "task_id": str, "tenant_id": int,
//	          "hosts": [sorted, deduped str], "exp": int unix seconds}
//	canonical = json.dumps(claims, sort_keys=True, separators=(",", ":"),
//	                        ensure_ascii=True)   # UTF-8, no spaces
//	token = b64url_nopad(canonical) + "." + b64url_nopad(HMAC_SHA256(key, canonical))
//
// Verification order mirrors the Python verify(): shape, signature
// (constant-time), payload re-canonicalization (rejects reordered/duplicate
// keys), version, expiry (exclusive: now >= exp rejects).
//
// The token is NEVER logged: allow/deny lines carry task_id, tenant_id and the
// host only. The allow/deny decision and the CONNECT/HTTP plumbing are
// delegated to the existing Proxy in proxy.go, which gets a per-request
// Allowlist built from the token's hosts claim.
package egressproxy

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egress"
)

type Gateway struct {
	// Key is the HMAC-SHA256 key (HKDF-SHA256 of AUDIT_HMAC_KEY in Python).
	// Never logged.
	Key []byte
	// Log receives one JSON line per allow/deny; nil → discarded.
	Log io.Writer
	// Now injects the clock so tests can pin expiry. nil → time.Now.
	Now func() time.Time
	// Resolver/Dialer override name resolution and dialing; nil → defaults.
	Resolver Resolver
	Dialer   Dialer
	// MaxTunnels overrides the concurrency limit; 0 → MaxTunnels.
	MaxTunnels int
}

// Claims are the verified token claims.
type Claims struct {
	Exp      int      `json:"exp"`
	Hosts    []string `json:"hosts"`
	TaskID   string   `json:"task_id"`
	TenantID int      `json:"tenant_id"`
	V        int      `json:"v"`
}

// tokenReject is a token-verification failure. The reason is logged nowhere
// near the token; the token itself is never part of any response body.
type tokenReject struct {
	Reason string
}

func (t *tokenReject) Error() string { return "egress token rejected: " + t.Reason }

// tenantLog wraps the Proxy's Log writer so every allow/deny line the Proxy
// emits also carries tenant_id (Proxy.LogLine has no such field and proxy.go
// must not change). Proxy writes exactly one JSON line per Write call, so the
// tenant field is spliced in right after the leading '{'. The token never
// reaches this writer.
type tenantLog struct {
	Inner    io.Writer
	TenantID int
}

func (l *tenantLog) Write(p []byte) (int, error) {
	if l.Inner == nil {
		return len(p), nil
	}
	b := p
	if len(b) > 0 && b[0] == '{' {
		var out []byte
		out = append(out, byte('{'))
		out = append(out, []byte(fmt.Sprintf("\"tenant_id\":%d,", l.TenantID))...)
		out = append(out, b[1:]...)
		b = out
	}
	return l.Inner.Write(b)
}

// canonicalJSON renders claims exactly like Python's
// json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True):
// keys in ASCII order (exp, hosts, task_id, tenant_id, v), no spaces,
// non-ASCII escaped as \uXXXX. Non-ASCII anywhere in the claims is rejected
// outright rather than transcoded: the minter's hostname grammar is ASCII, so
// a token carrying non-ASCII must not verify.
func canonicalJSON(c Claims) ([]byte, error) {
	if !isASCII(c.TaskID) {
		return nil, errors.New("non-ASCII task_id")
	}
	var b strings.Builder
	b.WriteString(`{"exp":`)
	b.WriteString(strconv.Itoa(c.Exp))
	b.WriteString(`,"hosts":[`)
	for i, h := range c.Hosts {
		if i > 0 {
			b.WriteByte(',')
		}
		if !isASCII(h) {
			return nil, errors.New("non-ASCII host")
		}
		b.WriteString(jsonEscape(h))
	}
	b.WriteString(`],"task_id":`)
	b.WriteString(jsonEscape(c.TaskID))
	b.WriteString(`,"tenant_id":`)
	b.WriteString(strconv.Itoa(c.TenantID))
	b.WriteString(`,"v":`)
	b.WriteString(strconv.Itoa(c.V))
	b.WriteByte('}')
	return []byte(b.String()), nil
}

// jsonEscape quotes a string the way Python's json.dumps does for ASCII text.
func jsonEscape(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, ch := range s {
		switch ch {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\b':
			b.WriteString(`\b`)
		case '\t':
			b.WriteString(`\t`)
		case '\n':
			b.WriteString(`\n`)
		case '\f':
			b.WriteString(`\f`)
		case '\r':
			b.WriteString(`\r`)
		default:
			if ch < 0x20 || ch == 0x7f {
				b.WriteString(fmt.Sprintf("\\u%04x", ch))
			} else {
				b.WriteRune(ch)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

func isASCII(s string) bool {
	for _, ch := range s {
		if ch > 0x7f {
			return false
		}
	}
	return true
}

// b64urlNoPadDecode accepts the Python minter's base64url-without-padding
// alphabet.
func b64urlNoPadDecode(s string) ([]byte, error) {
	if s == "" {
		return nil, errors.New("empty b64 part")
	}
	pad := strings.Repeat("=", (4-len(s)%4)%4)
	b, err := base64.URLEncoding.DecodeString(s + pad)
	if err != nil {
		return nil, errors.New("bad b64 part")
	}
	return b, nil
}

// hmacSHA256 is the raw (not hex) HMAC-SHA256 tag, matching Python's
// hmac.new(key, canonical, hashlib.sha256).digest().
func hmacSHA256(key, msg []byte) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(msg)
	return mac.Sum(nil)
}

// bearerToken extracts the declared-egress token from the request:
// Proxy-Authorization: Bearer <tok>, falling back to Authorization. The token
// value is never logged or echoed.
func bearerToken(r *http.Request) string {
	v := bearerOf(r.Header.Get("Proxy-Authorization"))
	if v == "" {
		v = bearerOf(r.Header.Get("Authorization"))
	}
	return v
}

func bearerOf(h string) string {
	if !strings.HasPrefix(h, "Bearer ") {
		return ""
	}
	return strings.TrimSpace(h[len("Bearer "):])
}

// verifyToken checks shape, signature (constant-time), payload
// re-canonicalization, version and expiry, in the same order as the Python
// verify_egress_token.
func verifyToken(key []byte, token string, now time.Time) (*Claims, error) {
	dot := strings.IndexByte(token, '.')
	if dot < 1 || dot == len(token)-1 {
		return nil, &tokenReject{Reason: "malformed"}
	}
	payload, err := b64urlNoPadDecode(token[:dot])
	sig, err2 := b64urlNoPadDecode(token[dot+1:])
	if err != nil || err2 != nil {
		return nil, &tokenReject{Reason: "malformed"}
	}
	if len(sig) != 32 {
		return nil, &tokenReject{Reason: "bad_signature"}
	}
	expected := hmacSHA256(key, payload)
	if !hmac.Equal(sig, expected) {
		return nil, &tokenReject{Reason: "bad_signature"}
	}
	var c Claims
	if err := json.Unmarshal(payload, &c); err != nil {
		return nil, &tokenReject{Reason: "malformed"}
	}
	canon, err := canonicalJSON(c)
	if err != nil {
		return nil, &tokenReject{Reason: "malformed"}
	}
	if !bytesEqual(canon, payload) {
		return nil, &tokenReject{Reason: "non_canonical"}
	}
	if c.V != 1 {
		return nil, &tokenReject{Reason: "unsupported_version"}
	}
	if c.Hosts == nil || len(c.Hosts) == 0 {
		return nil, &tokenReject{Reason: "malformed"}
	}
	if c.TaskID == "" {
		return nil, &tokenReject{Reason: "malformed"}
	}
	if now.Unix() >= int64(c.Exp) {
		return nil, &tokenReject{Reason: "expired"}
	}
	return &c, nil
}

// bytesEqual compares two byte slices.
func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// ServeHTTP implements http.Handler. A missing/bad/expired token is a 407 with
// a Proxy-Authenticate: Bearer challenge; a valid token delegates the request
// (CONNECT or absolute-form HTTP) to the existing Proxy, which owns the
// allow/deny decision, the upstream dial and the log lines.
func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	tok := bearerToken(r)
	if tok == "" {
		g.denyToken(w, r, "missing_token")
		return
	}
	now := time.Now()
	if g.Now != nil {
		now = g.Now()
	}
	c, err := verifyToken(g.Key, tok, now)
	if err != nil {
		var tr *tokenReject
		if errors.As(err, &tr) {
			g.denyToken(w, r, tr.Reason)
		} else {
			g.denyToken(w, r, "malformed")
		}
		return
	}
	al, err := egress.Parse(c.Hosts)
	if err != nil {
		g.denyToken(w, r, "bad_allowlist")
		return
	}
	// Per-request Proxy: the token's hosts are the allowlist, and the log
	// writer stamps tenant_id onto every line proxy.go emits.
	p := &Proxy{
		Allow:      al,
		Resolver:   g.Resolver,
		Dialer:     g.Dialer,
		Log:        &tenantLog{Inner: g.Log, TenantID: c.TenantID},
		TaskID:     c.TaskID,
		MaxTunnels: g.MaxTunnels,
	}
	p.ServeHTTP(w, r)
}

// denyToken answers 407 with a Bearer challenge. The reason is a fixed
// vocabulary; the token itself never appears in the response or any log line.
func (g *Gateway) denyToken(w http.ResponseWriter, r *http.Request, reason string) {
	if g.Log != nil {
		var line LogLine
		line.TS = time.Now().UTC().Format(time.RFC3339Nano)
		line.Ev = "egress_token_deny"
		line.Method = r.Method
		line.Host = normalizeHost(r.Host)
		line.Reason = reason
		if b, err := json.Marshal(line); err == nil {
			_, _ = g.Log.Write(append(b, '\n'))
		}
	}
	w.Header().Set("Proxy-Authenticate", "Bearer")
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Mod-Egress-Token", reason)
	w.WriteHeader(http.StatusProxyAuthRequired)
	_, _ = io.WriteString(w, "egress gateway: token rejected\n")
}
