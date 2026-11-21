package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// --- nonce registry: issue / validate / expire ---

func TestNonceRegistry_IssueAndLookup(t *testing.T) {
	r := NewNonceRegistry()
	id := TaskIdentity{TaskID: 42, TenantID: 7, RunID: 99}

	nonce, err := r.Issue(id)
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	if len(nonce) != 64 { // 32 random bytes, hex-encoded
		t.Errorf("nonce should be 64 hex chars; got %d (%q)", len(nonce), nonce)
	}

	got, ok := r.Lookup(nonce)
	if !ok {
		t.Fatal("freshly issued nonce should validate")
	}
	if got != id {
		t.Errorf("Lookup returned %+v, want %+v", got, id)
	}
}

func TestNonceRegistry_IssueIsUnique(t *testing.T) {
	r := NewNonceRegistry()
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		n, err := r.Issue(TaskIdentity{TaskID: i})
		if err != nil {
			t.Fatalf("Issue: %v", err)
		}
		if seen[n] {
			t.Fatalf("duplicate nonce issued: %q", n)
		}
		seen[n] = true
	}
}

func TestNonceRegistry_UnknownNonceRejected(t *testing.T) {
	r := NewNonceRegistry()
	if _, ok := r.Lookup("nope-never-issued"); ok {
		t.Error("unknown nonce must not validate")
	}
}

func TestNonceRegistry_RevokeInvalidates(t *testing.T) {
	r := NewNonceRegistry()
	nonce, _ := r.Issue(TaskIdentity{TaskID: 1})
	r.Revoke(nonce)
	if _, ok := r.Lookup(nonce); ok {
		t.Error("revoked nonce must not validate")
	}
	// Idempotent — a second revoke must not panic.
	r.Revoke(nonce)
}

func TestNonceRegistry_Expiry(t *testing.T) {
	r := NewNonceRegistry()
	// Drive time by hand so the test is deterministic and instant.
	now := time.Unix(1_000_000, 0)
	r.now = func() time.Time { return now }
	r.ttl = time.Minute

	nonce, _ := r.Issue(TaskIdentity{TaskID: 1})
	if _, ok := r.Lookup(nonce); !ok {
		t.Fatal("nonce should be valid before expiry")
	}

	now = now.Add(time.Minute + time.Second) // past TTL
	if _, ok := r.Lookup(nonce); ok {
		t.Error("expired nonce must not validate")
	}
	if r.Len() != 0 {
		t.Errorf("expired nonce should be evicted on read; %d entries remain", r.Len())
	}
}

// --- token endpoint HTTP behaviour ---

type stubMinter struct {
	token string
	exp   int64
	err   error
	calls int
}

func (s *stubMinter) MintRunToken(ctx context.Context, taskID int) (string, int64, error) {
	s.calls++
	if s.err != nil {
		return "", 0, s.err
	}
	return s.token, s.exp, nil
}

func newTestServer(m RunTokenMinter) *TokenServer {
	return &TokenServer{
		registry:    NewNonceRegistry(),
		minter:      m,
		log:         slog.New(slog.NewTextHandler(io.Discard, nil)),
		bind:        bindAuto,
		port:        defaultTokenPort,
		failLimiter: *newFailLimiter(),
	}
}

// stubGateways is a fake dockerNetworkGateway keyed by network name.
type stubGateways struct {
	gws   map[string]string
	calls []string
}

func (s *stubGateways) NetworkGateway(ctx context.Context, name string) (net.IP, error) {
	s.calls = append(s.calls, name)
	ip, ok := s.gws[name]
	if !ok {
		return nil, fmt.Errorf("network %q not found", name)
	}
	return net.ParseIP(ip), nil
}

func TestTokenServer_ValidNonceMintsToken(t *testing.T) {
	m := &stubMinter{token: "jwt-abc", exp: 1_700_000_900}
	ts := newTestServer(m)
	nonce, _ := ts.IssueNonce(TaskIdentity{TaskID: 55, TenantID: 3, RunID: 9})

	req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	req.Header.Set(nonceHeader, nonce)
	rec := httptest.NewRecorder()
	ts.handle(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("valid nonce should return 200; got %d (%s)", rec.Code, rec.Body.String())
	}
	var out struct {
		Token string `json:"token"`
		Exp   int64  `json:"exp"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if out.Token != "jwt-abc" || out.Exp != 1_700_000_900 {
		t.Errorf("unexpected body: %+v", out)
	}
	if m.calls != 1 {
		t.Errorf("mint should be called exactly once; got %d", m.calls)
	}
}

func TestTokenServer_UnknownNonce401(t *testing.T) {
	m := &stubMinter{token: "should-not-be-minted"}
	ts := newTestServer(m)

	req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	req.Header.Set(nonceHeader, "bogus")
	rec := httptest.NewRecorder()
	ts.handle(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unknown nonce should return 401; got %d", rec.Code)
	}
	if m.calls != 0 {
		t.Error("mint must NOT be called for an unknown nonce")
	}
}

func TestTokenServer_RevokedNonce401(t *testing.T) {
	ts := newTestServer(&stubMinter{token: "x"})
	nonce, _ := ts.IssueNonce(TaskIdentity{TaskID: 1})
	ts.RevokeNonce(nonce) // task finished

	req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	req.Header.Set(nonceHeader, nonce)
	rec := httptest.NewRecorder()
	ts.handle(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("revoked nonce should return 401; got %d", rec.Code)
	}
}

func TestTokenServer_MissingNonce401(t *testing.T) {
	ts := newTestServer(&stubMinter{token: "x"})
	req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	rec := httptest.NewRecorder()
	ts.handle(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("missing nonce header should return 401; got %d", rec.Code)
	}
}

func TestTokenServer_NonPostRejected(t *testing.T) {
	ts := newTestServer(&stubMinter{token: "x"})
	nonce, _ := ts.IssueNonce(TaskIdentity{TaskID: 1})
	req := httptest.NewRequest(http.MethodGet, tokenPath, nil)
	req.Header.Set(nonceHeader, nonce)
	rec := httptest.NewRecorder()
	ts.handle(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET should return 405; got %d", rec.Code)
	}
}

func TestTokenServer_MintFailure502(t *testing.T) {
	m := &stubMinter{err: errors.New("api down")}
	ts := newTestServer(m)
	nonce, _ := ts.IssueNonce(TaskIdentity{TaskID: 1})

	req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	req.Header.Set(nonceHeader, nonce)
	rec := httptest.NewRecorder()
	ts.handle(rec, req)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("upstream mint failure should surface as 502; got %d", rec.Code)
	}
}

func TestNewTokenServer_URLShape(t *testing.T) {
	// Env-driven overrides produce the advertised MOD_TASK_TOKEN_URL.
	t.Setenv("MOD_TASK_TOKEN_HOST", "worker.internal")
	t.Setenv("MOD_TASK_TOKEN_PORT", "12345")
	ts := NewTokenServer(&stubMinter{}, slog.New(slog.NewTextHandler(io.Discard, nil)), bindAuto, "12345", "")
	defer ts.Close()

	want := "http://worker.internal:12345/run-token"
	if ts.ContainerURL() != want {
		t.Errorf("ContainerURL = %q, want %q", ts.ContainerURL(), want)
	}
}

// --- R-161: bind choice is never 0.0.0.0 ---

// assertNarrowBind fails if any address in addrs is a wildcard bind.
func assertNarrowBind(t *testing.T, addrs []string) {
	t.Helper()
	for _, a := range addrs {
		if a == "0.0.0.0" || strings.HasPrefix(a, "0.0.0.0:") || a == "::" || strings.HasPrefix(a, "[::]") {
			t.Errorf("bind address %q is wildcard; expected a narrow per-backend set", a)
		}
	}
}

func TestTokenServer_AutoBind_DockerBackend(t *testing.T) {
	// Remote/datacenter docker worker: callers are the task containers on the
	// main network + the mod-user-*/stack-posture bridges. The narrow set is
	// 127.0.0.1 plus each resolvable gateway IP — never 0.0.0.0.
	gw := &stubGateways{gws: map[string]string{
		"mod_modtex-network": "198.51.100.1",
		"mod-user-egress":    "198.51.100.1",
		"mod-user-internal":  "198.51.100.1",
		// mod_stack-api-net not created yet — must not fail the bind.
	}}
	ts := NewTokenServer(&stubMinter{}, slog.New(slog.NewTextHandler(io.Discard, nil)), bindAuto, "19179", "mod_modtex-network")
	ts.SetDockerGateways(gw)
	defer ts.Close()

	addrs, pending := ts.bindAddrs()
	assertNarrowBind(t, addrs)
	if !pending {
		t.Error("expected pending=true while the stack-posture net is missing")
	}
	for _, want := range []string{
		"127.0.0.1:19179",
		"198.51.100.1:19179",
		"198.51.100.1:19179",
		"198.51.100.1:19179",
	} {
		if !containsHostPort(addrs, want) {
			t.Errorf("bind set missing %q; got %v", want, addrs)
		}
	}

	// The stack-posture net appears later: the set grows without a wildcard.
	gw.gws["mod_stack-api-net"] = "198.51.100.1"
	addrs, pending = ts.bindAddrs()
	assertNarrowBind(t, addrs)
	if pending {
		t.Error("expected pending=false once all networks resolve")
	}
	if !containsHostPort(addrs, "198.51.100.1:19179") {
		t.Errorf("bind set missing stack-posture gateway; got %v", addrs)
	}
}

func TestTokenServer_AutoBind_EmbeddedAddsLocalIP(t *testing.T) {
	gw := &stubGateways{gws: map[string]string{
		"mod_modtex-network": "198.51.100.1",
		"mod-user-egress":    "198.51.100.1",
		"mod-user-internal":  "198.51.100.1",
		"mod_stack-api-net":  "198.51.100.1",
	}}
	ts := NewTokenServer(&stubMinter{}, slog.New(slog.NewTextHandler(io.Discard, nil)), bindAuto, "19179", "mod_modtex-network")
	ts.SetDockerGateways(gw)
	ts.SetEmbedded(true) // worker's own veth-internal IP must be bound
	defer ts.Close()

	addrs, pending := ts.bindAddrs()
	assertNarrowBind(t, addrs)
	if pending {
		t.Error("no network should be pending")
	}
	// The worker container's own IP (non-loopback unicast) is in the set.
	if ip := localNonLoopbackIP(); ip != nil {
		if !containsHostPort(addrs, net.JoinHostPort(ip.String(), "19179")) {
			t.Errorf("embedded worker must bind its own IP %s; got %v", ip, addrs)
		}
	}
}

func TestTokenServer_AutoBind_NoDockerStillBindsLoopback(t *testing.T) {
	// Docker client unavailable: the narrow set still covers loopback and the
	// endpoint still comes up rather than refusing every task.
	ts := NewTokenServer(&stubMinter{}, slog.New(slog.NewTextHandler(io.Discard, nil)), bindAuto, "19179", "mod_modtex-network")
	ts.SetDockerGateways(&stubGateways{gws: map[string]string{}})
	defer ts.Close()

	addrs, _ := ts.bindAddrs()
	assertNarrowBind(t, addrs)
	if !containsHostPort(addrs, "127.0.0.1:19179") {
		t.Errorf("loopback must always be bound; got %v", addrs)
	}
}

func TestTokenServer_ExplicitOverrideForcesBind(t *testing.T) {
	// The config override replaces the derived set verbatim — e.g. "::" for a
	// k8s worker whose task Pods are remote.
	ts := NewTokenServer(&stubMinter{}, slog.New(slog.NewTextHandler(io.Discard, nil)), "::", "19179", "")
	defer ts.Close()

	addrs, pending := ts.bindAddrs()
	if pending {
		t.Error("explicit override must not leave pending gateways")
	}
	if len(addrs) != 1 || addrs[0] != "[::]:19179" {
		t.Errorf("override bind = %v, want [::]:19179", addrs)
	}
}

// --- R-161: per-source-IP rate limiting on failed nonce attempts ---

func TestTokenServer_RateLimitsRepeatedFailures(t *testing.T) {
	ts := newTestServer(&stubMinter{token: "x"})
	// Small budget for a fast test.
	ts.failLimiter.limit = 3

	for i := 0; i < 3; i++ {
		req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
		rec := httptest.NewRecorder()
		ts.handle(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("attempt %d: expected 401, got %d", i+1, rec.Code)
		}
	}
	// Budget exhausted — the same source is now limited to 429.
	req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	rec := httptest.NewRecorder()
	ts.handle(rec, req)
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("4th failed attempt should be 429; got %d", rec.Code)
	}
}

func TestTokenServer_RateLimitWindowExpires(t *testing.T) {
	ts := newTestServer(&stubMinter{token: "x"})
	ts.failLimiter.limit = 1
	now := time.Unix(1_000_000, 0)
	ts.failLimiter.now = func() time.Time { return now }

	doReq := func() int {
		req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
		rec := httptest.NewRecorder()
		ts.handle(rec, req)
		return rec.Code
	}
	if c := doReq(); c != http.StatusUnauthorized {
		t.Fatalf("first failure: expected 401, got %d", c)
	}
	if c := doReq(); c != http.StatusTooManyRequests {
		t.Fatalf("second failure: expected 429, got %d", c)
	}
	// After the window elapses the budget resets.
	now = now.Add(nonceFailWindow + time.Second)
	if c := doReq(); c != http.StatusUnauthorized {
		t.Fatalf("post-window failure: expected 401, got %d", c)
	}
}

func TestTokenServer_SuccessDoesNotCountAsFailure(t *testing.T) {
	ts := newTestServer(&stubMinter{token: "ok"})
	ts.failLimiter.limit = 1
	nonce, _ := ts.IssueNonce(TaskIdentity{TaskID: 1})

	req := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	req.Header.Set(nonceHeader, nonce)
	rec := httptest.NewRecorder()
	ts.handle(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("valid nonce should return 200; got %d", rec.Code)
	}
	// A subsequent failure must still be 401 (not 429): successes don't count.
	req2 := httptest.NewRequest(http.MethodPost, tokenPath, nil)
	rec2 := httptest.NewRecorder()
	ts.handle(rec2, req2)
	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("failure after success should be 401; got %d", rec2.Code)
	}
}

// --- R-161: a failed bind must never take down the endpoint or the task path ---

func TestTokenServer_BindFailureNeverBlocksTheTaskPath(t *testing.T) {
	// The probe loop re-derives the same set every tokenRebindProbe round.
	// Listening again on an address we already hold fails EADDRINUSE, so the
	// old rebind reported "no bind address succeeded" and closed the listener
	// that WAS serving - the CE stranger-boot C6 flood that starved the
	// executor's docker calls and left the node container unlaunched.
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	ts := NewTokenServer(&stubMinter{}, log, bindAuto, "19179", "")
	defer ts.Close()
	ts.Start()
	if !containsHostPort(ts.currentAddrs(), "127.0.0.1:19179") {
		t.Fatal("loopback should be live after Start; auto bind always includes it")
	}

	// An all-overlap round is a no-op: no self-EADDRINUSE, no lost listener.
	if err := ts.rebind([]string{"127.0.0.1:19179"}); err != nil {
		t.Errorf("all-overlap rebind must be a no-op, got: %v", err)
	}
	if !containsHostPort(ts.currentAddrs(), "127.0.0.1:19179") {
		t.Error("the live listener must survive a rebind round")
	}

	// One unbindable address in the set must not fail the endpoint either.
	if err := ts.rebind([]string{"127.0.0.1:19179", "127.0.0.1:not-a-port"}); err != nil {
		t.Errorf("partial bind must not fail the endpoint, got: %v", err)
	}
	if !containsHostPort(ts.currentAddrs(), "127.0.0.1:19179") {
		t.Error("a failed address must not drop the listener that works")
	}

	// Nothing bindable at all: Start logs and returns - the worker keeps
	// draining tasks, nodes fall back to the transitional API_KEY.
	dead := NewTokenServer(&stubMinter{}, log, "::", "not-a-port", "")
	dead.Start()
	if len(dead.currentAddrs()) != 0 {
		t.Error("an unbindable endpoint must simply have no listener")
	}
}
