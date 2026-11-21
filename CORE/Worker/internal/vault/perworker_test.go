package vault

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// GSEC Q21.10 — the per-worker AppRole login (Q21.8) against a fake Vault:
// login with per-worker creds, the rotation window, revoked secrets, token
// refresh semantics, and concurrent token access. Distinct from q218_test.go
// (which exercises ApplySecretID switch/keep and RetryLogin in isolation):
// here the fake mints a DISTINCT client token per login so tests can assert
// which credential generation a secret read actually used.

// fakeVault emulates /v1/auth/approle/login + KV v2 reads. A login succeeds
// only when the role matches and the secret_id is in `valid` and not denied;
// the minted token encodes the login sequence number ("tok-1", "tok-2", ...).
type fakeVault struct {
	mu         sync.Mutex
	role       string
	valid      map[string]bool
	deny       map[string]bool
	logins     int
	lastRole   string
	lastSecret string
	reads      int
	readToken  string
	read403    bool
	server     *httptest.Server
}

func newFakeVault(t *testing.T, role string, valid, deny map[string]bool) *fakeVault {
	if deny == nil {
		deny = map[string]bool{}
	}
	v := &fakeVault{role: role, valid: valid, deny: deny}
	v.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/v1/auth/approle/login":
			var body struct {
				RoleID   string `json:"role_id"`
				SecretID string `json:"secret_id"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			v.mu.Lock()
			v.logins++
			v.lastRole, v.lastSecret = body.RoleID, body.SecretID
			tok := "tok-" + strconv.Itoa(v.logins)
			ok := body.RoleID == v.role && v.valid[body.SecretID] && !v.deny[body.SecretID]
			v.mu.Unlock()
			if !ok {
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(map[string]any{"errors": []string{"invalid or unknown secret_id"}})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"auth": map[string]any{"client_token": tok, "lease_duration": 3600},
			})
		case strings.HasPrefix(r.URL.Path, "/v1/secret/data/"):
			v.mu.Lock()
			v.reads++
			v.readToken = r.Header.Get("X-Vault-Token")
			boom := v.read403
			v.mu.Unlock()
			if boom {
				w.WriteHeader(http.StatusForbidden)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"data": map[string]any{"data": map[string]string{"user": "w", "pass": "p"}},
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(v.server.Close)
	return v
}

// denySecret simulates the API revoking a secret_id after a rotation ack.
func (v *fakeVault) denySecret(s string) {
	v.mu.Lock()
	v.deny[s] = true
	v.mu.Unlock()
}

func (v *fakeVault) stats() (logins, reads int, lastRole, lastSecret, readToken string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.logins, v.reads, v.lastRole, v.lastSecret, v.readToken
}

func TestPerWorkerLogin_RoleAndSecretMustMatch(t *testing.T) {
	v := newFakeVault(t, "worker-7", map[string]bool{"sw-7": true}, nil)
	c := New(v.server.URL, "worker-7", "sw-7")
	if err := c.Login(); err != nil {
		t.Fatalf("per-worker login: %v", err)
	}
	logins, _, lastRole, lastSecret, _ := v.stats()
	if logins != 1 || lastRole != "worker-7" || lastSecret != "sw-7" {
		t.Fatalf("logins=%d lastRole=%q lastSecret=%q, want one login with the per-worker pair", logins, lastRole, lastSecret)
	}
	if got, err := c.ReadSecret("infrastructure/redis"); err != nil || got["pass"] != "p" {
		t.Fatalf("read: got=%v err=%v, want the secret data", got, err)
	}
	if _, _, _, _, readToken := v.stats(); readToken != "tok-1" {
		t.Fatalf("read used token %q, want tok-1 minted from the per-worker login", readToken)
	}
	// A credential the API never minted for this role must be rejected with a
	// clear error, not a panic or a cached-token read.
	bad := New(v.server.URL, "worker-7", "sw-other")
	if err := bad.Login(); err == nil || !strings.Contains(err.Error(), "HTTP 400") {
		t.Fatalf("wrong secret login err = %v, want a clear HTTP 400 error", err)
	}
}

func TestRotationWindow_OldSecretTokenValidUntilAck(t *testing.T) {
	v := newFakeVault(t, "worker-7", map[string]bool{"old": true, "new": true}, nil)
	c := New(v.server.URL, "worker-7", "old")
	if err := c.Login(); err != nil {
		t.Fatalf("boot login: %v", err)
	}
	// During the window the worker proves nothing yet: reads ride the token
	// minted from the OLD secret, which stays valid until the API revokes it.
	if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
		t.Fatalf("read before ack: %v", err)
	}
	if _, _, _, _, tok := v.stats(); tok != "tok-1" {
		t.Fatalf("pre-ack read used %q, want tok-1 (old secret's token)", tok)
	}
	// Heartbeat delivers the new secret_id: the client probes it and, only on
	// success, switches over — the working token is now the new one.
	if !c.ApplySecretID("new") {
		t.Fatal("valid new secret must switch over")
	}
	if _, _, _, lastSecret, tok := v.stats(); lastSecret != "new" || tok == "" {
		t.Fatalf("post-switch lastSecret=%q readToken=%q", lastSecret, tok)
	}
	if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
		t.Fatalf("read after ack: %v", err)
	}
	if _, _, _, _, tok := v.stats(); tok != "tok-2" {
		t.Fatalf("post-ack read used %q, want tok-2 (new secret's token)", tok)
	}
	// API revokes the old secret now that the worker acked: nothing changes
	// for the worker, and a forced re-login still succeeds (it uses the new
	// secret, not the revoked one).
	v.denySecret("old")
	c.mu.Lock()
	c.expAt = time.Now().Add(-time.Second)
	c.mu.Unlock()
	if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
		t.Fatalf("read after old-secret revocation: %v", err)
	}
	if logins, _, _, lastSecret, _ := v.stats(); logins != 3 || lastSecret != "new" {
		t.Fatalf("post-revocation logins=%d lastSecret=%q, want re-login 3 with the new secret", logins, lastSecret)
	}
}

func TestRevokedSecret_ClearErrorDegradedNoPanic(t *testing.T) {
	v := newFakeVault(t, "worker-7", map[string]bool{"sw-7": true}, map[string]bool{"sw-7": true})
	c := New(v.server.URL, "worker-7", "sw-7")
	var err error
	func() { // a panic here fails the test, which is the "no panic" assertion
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("login with revoked secret panicked: %v", r)
			}
		}()
		err = c.Login()
	}()
	if err == nil || !strings.Contains(err.Error(), "HTTP 400") {
		t.Fatalf("revoked-secret login err = %v, want a clear HTTP 400 error", err)
	}
	if !c.Degraded() {
		t.Fatal("revoked secret at boot must leave the client degraded")
	}
	w := c.Warning()
	if !strings.Contains(w, "degraded") || strings.Contains(w, "sw-7") {
		t.Fatalf("warning %q must name the degraded mode and never the secret", w)
	}
	// Degraded mode: reads fail cleanly (nil map, error), never panic, and the
	// worker keeps running on API-mediated creds.
	got, rerr := c.ReadSecret("infrastructure/redis")
	if rerr == nil || got != nil {
		t.Fatalf("degraded read got=%v err=%v, want nil map + error", got, rerr)
	}
	// The API re-issues the secret (or the revoke was wrong): recovery works.
	v.mu.Lock()
	v.deny["sw-7"] = false
	v.mu.Unlock()
	if err := c.Login(); err != nil {
		t.Fatalf("recovery login: %v", err)
	}
	if c.Degraded() || c.Warning() != "" {
		t.Fatal("recovered login must clear degraded state and warning")
	}
}

func TestTokenExpiryTriggersReLoginNotRenewal(t *testing.T) {
	v := newFakeVault(t, "worker-7", map[string]bool{"sw-7": true}, nil)
	c := New(v.server.URL, "worker-7", "sw-7")
	if err := c.Login(); err != nil {
		t.Fatalf("login: %v", err)
	}
	// Fresh token: a read must NOT log in again. The client has no token-renew
	// endpoint — AppRole refresh means a fresh login.
	if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
		t.Fatalf("read: %v", err)
	}
	if logins, reads, _, _, _ := v.stats(); logins != 1 || reads != 1 {
		t.Fatalf("logins=%d reads=%d, want 1/1 (no renewal churn)", logins, reads)
	}
	// Expired token: the next read re-logs in with the same per-worker creds
	// and uses the freshly minted token.
	c.mu.Lock()
	c.expAt = time.Now().Add(-time.Second)
	c.mu.Unlock()
	if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
		t.Fatalf("read after expiry: %v", err)
	}
	logins, reads, lastRole, lastSecret, tok := v.stats()
	if logins != 2 || reads != 2 || lastRole != "worker-7" || lastSecret != "sw-7" || tok != "tok-2" {
		t.Fatalf("logins=%d reads=%d lastRole=%q lastSecret=%q readToken=%q, want a re-login on the same creds and tok-2",
			logins, reads, lastRole, lastSecret, tok)
	}
}

func TestConcurrentReadsAndReLogins_RaceSafe(t *testing.T) {
	v := newFakeVault(t, "worker-7", map[string]bool{"sw-7": true}, nil)
	c := New(v.server.URL, "worker-7", "sw-7")
	if err := c.Login(); err != nil {
		t.Fatalf("login: %v", err)
	}
	const workers = 8
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
					t.Errorf("concurrent read: %v", err)
					return
				}
			}
		}()
	}
	// Contention partner: force expiry and re-login in a tight loop so the
	// token read (ReadSecret) races the token write (loginWith) — both are
	// mutex-guarded, so -race stays clean.
	for j := 0; j < 100; j++ {
		c.mu.Lock()
		c.expAt = time.Now().Add(-time.Second)
		c.mu.Unlock()
		if err := c.Login(); err != nil {
			t.Fatalf("contended login: %v", err)
		}
	}
	wg.Wait()
	if logins, reads, _, _, _ := v.stats(); logins < 101 || reads != workers*50 {
		t.Fatalf("logins=%d reads=%d, want >=101 logins and %d reads", logins, reads, workers*50)
	}
}
