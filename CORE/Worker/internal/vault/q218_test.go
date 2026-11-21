package vault

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// GSEC Q21.8: the two-secret rotation window and degraded boot.

// loginGate serves AppRole logins and rejects secret_ids in the bad set with
// the 400 Vault emits for an unknown/revoked secret_id. It records the secret
// used by the most recent login so a test can tell which credential the client
// actually presented.
type loginGate struct {
	bad        map[string]bool
	logins     int
	lastSecret string
	server     *httptest.Server
}

func newLoginGate(t *testing.T, bad map[string]bool) *loginGate {
	g := &loginGate{bad: bad}
	g.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/auth/approle/login" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		g.logins++
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		g.lastSecret = body["secret_id"]
		if g.bad[body["secret_id"]] {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(map[string]any{"errors": []string{"invalid or unknown secret_id"}})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"auth": map[string]any{"client_token": "s.ok", "lease_duration": 3600},
		})
	}))
	t.Cleanup(g.server.Close)
	return g
}

func TestApplySecretID_GoodNewSecretSwitchesOver(t *testing.T) {
	g := newLoginGate(t, map[string]bool{})
	c := New(g.server.URL, "worker-7", "old-secret")
	if err := c.Login(); err != nil {
		t.Fatalf("boot login: %v", err)
	}
	if !c.ApplySecretID("new-secret") {
		t.Fatal("a valid new secret_id must switch the client over")
	}
	if c.secretID != "new-secret" {
		t.Errorf("secretID = %q, want the new one", c.secretID)
	}
	// Validate-first means two logins: the boot login and the probe with the
	// new secret; the probe's token is the working one after the switch.
	if g.logins != 2 || g.lastSecret != "new-secret" {
		t.Errorf("logins=%d lastSecret=%q, want 2 and new-secret", g.logins, g.lastSecret)
	}
}

func TestApplySecretID_BadNewSecretKeepsTheOldOne(t *testing.T) {
	g := newLoginGate(t, map[string]bool{"bad-new": true})
	c := New(g.server.URL, "worker-7", "old-secret")
	if err := c.Login(); err != nil {
		t.Fatalf("boot login: %v", err)
	}
	if c.ApplySecretID("bad-new") {
		t.Fatal("a rejected secret_id must not switch anything over")
	}
	if c.secretID != "old-secret" {
		t.Errorf("secretID = %q, want the old one kept", c.secretID)
	}
	if err := c.Login(); err != nil {
		t.Fatalf("login after failed rotation: %v", err)
	}
	if g.lastSecret != "old-secret" {
		t.Errorf("last login used %q, want the old secret", g.lastSecret)
	}
}

func TestDegradedBoot_WarningWhileLoginFailsAndClearsOnRecovery(t *testing.T) {
	g := newLoginGate(t, map[string]bool{"old-secret": true})
	c := New(g.server.URL, "worker-7", "old-secret")
	if err := c.Login(); err == nil {
		t.Fatal("expected boot login failure")
	}
	if !c.Degraded() {
		t.Fatal("failed boot login must mark the client degraded")
	}
	if w := c.Warning(); !strings.Contains(w, "degraded") {
		t.Errorf("warning = %q, want a degraded-mode warning", w)
	}
	// Vault unseals / the secret becomes valid again: the same client recovers
	// and the warning clears. First attempt is immediate, so no sleep needed.
	delete(g.bad, "old-secret")
	if err := c.Login(); err != nil {
		t.Fatalf("recovery login: %v", err)
	}
	if c.Degraded() || c.Warning() != "" {
		t.Fatal("successful login must clear degraded state and warning")
	}
}

func TestRetryLogin_ReturnsNilOnRecoveryAndCtxErrOnCancel(t *testing.T) {
	g := newLoginGate(t, map[string]bool{"old-secret": true})
	c := New(g.server.URL, "worker-7", "old-secret")
	if err := c.Login(); err == nil {
		t.Fatal("expected boot login failure")
	}
	// Always failing: a cancelled ctx ends the loop with ctx.Err (the worker
	// shuts down rather than retrying forever).
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := c.RetryLogin(ctx); err == nil {
		t.Fatal("cancelled retry must return ctx.Err")
	}
	// Recovered Vault: the same client's first retry is immediate (no backoff
	// wait), so the loop returns nil.
	c2 := New(g.server.URL, "worker-7", "old-secret")
	if err := c2.Login(); err == nil {
		t.Fatal("expected boot login failure")
	}
	delete(g.bad, "old-secret")
	c2.retryMin = time.Millisecond
	c2.retryMax = 10 * time.Millisecond
	if err := c2.RetryLogin(context.Background()); err != nil {
		t.Fatalf("retry after recovery: %v", err)
	}
	if c2.Degraded() || c2.Warning() != "" {
		t.Fatal("recovered login must clear degraded state and warning")
	}
}

func TestNewWithEnv_PrefersPerWorkerEnvValues(t *testing.T) {
	t.Setenv("VAULT_APPROLE_ROLE_ID", "env-role")
	t.Setenv("VAULT_APPROLE_SECRET_ID", "env-secret")
	c := NewWithEnv("http://vault:8200", "shared-role", "shared-secret")
	if c.roleID != "env-role" || c.secretID != "env-secret" {
		t.Errorf("got %q/%q, want the per-worker env values", c.roleID, c.secretID)
	}
	// Empty env (a box that predates per-worker rendering) falls back to the
	// shared role passed in.
	t.Setenv("VAULT_APPROLE_ROLE_ID", "")
	t.Setenv("VAULT_APPROLE_SECRET_ID", "")
	c = NewWithEnv("http://vault:8200", "shared-role", "shared-secret")
	if c.roleID != "shared-role" || c.secretID != "shared-secret" {
		t.Errorf("fallback got %q/%q, want the shared role", c.roleID, c.secretID)
	}
}
