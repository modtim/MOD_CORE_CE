package main

// GSEC Q21.8 — the clean-restart recovery contract. A sealed-Vault boot leaves
// the worker with NO bootstrap token (degraded); after recovery the worker
// must NOT patch cfg in place (data race + transports captured the token by
// value). It confirms the token is now available and requests a clean restart,
// and the post-recovery boot builds a transport that carries the NEW token.

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/vault"
)

// sealedVault is a mock Vault that is sealed (rejects AppRole login) until the
// test calls Unseal, at which point it serves both a successful login and the
// embedded-worker bootstrap secret.
type sealedVault struct {
	seal    atomic.Bool
	secret  string
	server  *httptest.Server
	lastTok *atomic.Value
}

func newSealedVault(t *testing.T, secret string) *sealedVault {
	v := &sealedVault{secret: secret}
	v.seal.Store(true)
	v.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/auth/approle/login":
			if v.seal.Load() {
				w.WriteHeader(http.StatusServiceUnavailable)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"auth": map[string]any{"client_token": "ok", "lease_duration": 3600},
			})
		case "/v1/secret/data/infrastructure/embedded-worker":
			if v.seal.Load() || v.secret == "" {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"data": map[string]any{"data": map[string]any{"bootstrap_token": v.secret}},
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(v.server.Close)
	return v
}

func (v *sealedVault) Unseal() { v.seal.Store(false) }

func (v *sealedVault) URL() string { return v.server.URL }

// After recovery the worker confirms the token is now available (read-only) and
// requests the clean restart; the post-recovery boot then loads the SAME token
// from Vault, and the transport built from it carries the NEW token on its
// request headers — never a stale empty one.
func TestRecoveryRestartThenNewTransportCarriesRecoveredToken(t *testing.T) {
	resetRestartState()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	v := newSealedVault(t, "recovered-bootstrap-token")

	// Sealed-Vault boot: login fails, token stays empty, client is degraded.
	cfg := &config.Config{}
	vc := vault.New(v.URL(), "worker-7", "sid")
	if err := vc.Login(); err == nil {
		t.Fatal("sealed boot login must fail")
	}
	if !vc.Degraded() {
		t.Fatal("sealed boot must leave the client degraded")
	}
	loadBootstrapTokenFromVault(vc, cfg, log)
	if cfg.BootstrapToken != "" {
		t.Fatalf("sealed boot must leave the token empty, got %q", cfg.BootstrapToken)
	}

	// Vault unseals. Recovery: RetryLogin recovers; the read-only confirm must
	// see the token, and the clean restart must be requested exactly once.
	v.Unseal()
	if err := vc.RetryLogin(t.Context()); err != nil {
		t.Fatalf("recovery login: %v", err)
	}
	if got := readBootstrapTokenFromVault(vc); got != "recovered-bootstrap-token" {
		t.Fatalf("recovery read = %q, want the recovered token", got)
	}
	// The confirm must be read-only: cfg must still be empty after it.
	if cfg.BootstrapToken != "" {
		t.Fatal("recovery confirm must not write cfg.BootstrapToken (data race)")
	}
	var stops atomic.Int64
	requestGracefulRestart(func() { stops.Add(1) }, log, "bootstrap token recovered")
	if stops.Load() != 1 || restartExitCode() != exitCodeRestartForRecovery {
		t.Fatalf("recovery must request a clean restart once, stops=%d code=%d",
			stops.Load(), restartExitCode())
	}

	// Post-recovery boot: a fresh process re-runs the token read and builds the
	// transport from the recovered value. The transport carries the NEW token.
	freshCfg := &config.Config{}
	if got := loadBootstrapTokenFromVault(vc, freshCfg, log); got != "recovered-bootstrap-token" {
		t.Fatalf("post-recovery boot load = %q, want the recovered token", got)
	}
	tp := transport.NewAPITransport("http://api.test", "worker-7", freshCfg.BootstrapToken, []string{"CPU"}, log)
	var gotTok atomic.Value
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotTok.Store(r.Header.Get("X-Bootstrap-Token"))
		_ = json.NewEncoder(w).Encode(map[string]any{}) // no task pending
	}))
	t.Cleanup(api.Close)
	tp.SetBaseURL(api.URL)
	if _, err := tp.TryClaimNow(t.Context()); err != nil {
		t.Fatalf("try claim: %v", err)
	}
	if got, _ := gotTok.Load().(string); got != "recovered-bootstrap-token" {
		t.Fatalf("post-recovery transport X-Bootstrap-Token = %q, want the NEW token", got)
	}
}
