package vault

// The vault package had no test file.
//
// It is the worker's whole credential path: AppRole login, KV v2 reads, and
// dynamic database credentials. The tests below use httptest rather than
// mocks, so the JSON shapes are the real ones Vault emits.
//
// Two of them pin a behaviour that is arguably wrong. See
// TestTokenLifetime_ShortLeasesAreExpiredOnArrival.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// vaultStub serves AppRole login + KV v2 reads, counting logins so a test can
// tell a cached token from a re-authentication.
type vaultStub struct {
	logins   atomic.Int32
	reads    atomic.Int32
	leaseSec int
	server   *httptest.Server
	lastPath string
	lastTok  string
}

func newVaultStub(t *testing.T, leaseSec int) *vaultStub {
	t.Helper()
	s := &vaultStub{leaseSec: leaseSec}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/auth/approle/login", func(w http.ResponseWriter, r *http.Request) {
		s.logins.Add(1)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"auth": map[string]any{
				"client_token":   "s.token-value",
				"lease_duration": s.leaseSec,
			},
		})
	})
	mux.HandleFunc("/v1/secret/data/", func(w http.ResponseWriter, r *http.Request) {
		s.reads.Add(1)
		s.lastPath = r.URL.Path
		s.lastTok = r.Header.Get("X-Vault-Token")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"data": map[string]any{
				"data": map[string]string{"password": "hunter2", "user": "modtech"},
			},
		})
	})
	mux.HandleFunc("/v1/database/creds/", func(w http.ResponseWriter, r *http.Request) {
		s.lastPath = r.URL.Path
		_ = json.NewEncoder(w).Encode(map[string]any{
			"lease_id":       "database/creds/api-readwrite/abc",
			"lease_duration": 3600,
			"data":           map[string]string{"username": "v-approle-x", "password": "pw"},
		})
	})
	s.server = httptest.NewServer(mux)
	t.Cleanup(s.server.Close)
	return s
}

func TestNew_TrimsTrailingSlash(t *testing.T) {
	// Every path is concatenated directly onto addr, so a trailing slash
	// produces `//v1/auth/...`, which Vault rejects.
	c := New("http://vault:8200/", "role", "secret")
	if strings.HasSuffix(c.addr, "/") {
		t.Errorf("addr kept its trailing slash: %q", c.addr)
	}
}

func TestReadSecret_UnwrapsKVv2AndReusesTheToken(t *testing.T) {
	// A one-hour lease is comfortably outside the 5-minute refresh window, so
	// the second read must not re-authenticate.
	s := newVaultStub(t, 3600)
	c := New(s.server.URL, "role", "secret")

	got, err := c.ReadSecret("infrastructure/redis")
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	// KV v2 nests the user's pairs under data.data — returning the outer map
	// would hand callers metadata instead of the secret.
	if got["password"] != "hunter2" {
		t.Errorf("got %v, want the inner data map", got)
	}
	if s.lastTok != "s.token-value" {
		t.Errorf("token header = %q, want the logged-in token", s.lastTok)
	}

	if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
		t.Fatalf("second read: %v", err)
	}
	if n := s.logins.Load(); n != 1 {
		t.Errorf("logged in %d times for two reads, want 1 — the token cache is not holding", n)
	}
}

func TestTokenLifetime_ShortLeasesAreExpiredOnArrival(t *testing.T) {
	// PINS CURRENT BEHAVIOUR, WHICH IS PROBABLY A DEFECT.
	//
	// Login computes `expAt = now + lease - 5m` and ensureToken re-logins once
	// `now > expAt`. So any lease of 5 minutes or less is already expired when
	// it is stored, and EVERY read re-authenticates. Vault AppRole TTLs of
	// 5m are ordinary in tightened deployments.
	//
	// Not fixed here: changing the refresh margin alters the credential path
	// for every worker, and the right shape (clamp the margin to a fraction of
	// the lease, the way the API side rotates at 50% TTL) is a decision rather
	// than a patch.
	s := newVaultStub(t, 300) // exactly the 5-minute margin
	c := New(s.server.URL, "role", "secret")

	for i := 0; i < 3; i++ {
		if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
			t.Fatalf("read %d: %v", i, err)
		}
	}
	if n := s.logins.Load(); n != 3 {
		t.Errorf("logins=%d; expected 3, i.e. one per read. If this now fails, "+
			"the refresh margin was fixed and this test should assert the new "+
			"behaviour rather than be deleted", n)
	}
}

func TestTokenLifetime_AnInfiniteLeaseReadsAsAlwaysExpired(t *testing.T) {
	// PINS CURRENT BEHAVIOUR, WHICH IS AN INVERSION.
	//
	// `lease_duration: 0` means non-expiring in Vault (root and some periodic
	// tokens). Here it produces `expAt = now - 5m`, so the longest-lived token
	// obtainable is treated as the most expired, and the worker re-authenticates
	// on every single secret read against a token that never needed refreshing.
	s := newVaultStub(t, 0)
	c := New(s.server.URL, "role", "secret")

	for i := 0; i < 2; i++ {
		if _, err := c.ReadSecret("infrastructure/redis"); err != nil {
			t.Fatalf("read %d: %v", i, err)
		}
	}
	if n := s.logins.Load(); n != 2 {
		t.Errorf("logins=%d, want 2 — a zero lease currently re-logins every read", n)
	}
}

func TestGetInfraSecret_ComposesTheInfrastructurePath(t *testing.T) {
	s := newVaultStub(t, 3600)
	c := New(s.server.URL, "role", "secret")
	if _, err := c.GetInfraSecret("redis"); err != nil {
		t.Fatalf("GetInfraSecret: %v", err)
	}
	if want := "/v1/secret/data/infrastructure/redis"; s.lastPath != want {
		t.Errorf("path = %q, want %q", s.lastPath, want)
	}
}

func TestReadSecret_NonOKNamesThePathAndStatus(t *testing.T) {
	// An operator reading this line needs both: which secret, and whether it
	// was a permission problem or a missing path.
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/auth/approle/login", func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"auth": map[string]any{"client_token": "t", "lease_duration": 3600},
		})
	})
	mux.HandleFunc("/v1/secret/data/", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"errors":["permission denied"]}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	c := New(srv.URL, "role", "secret")
	_, err := c.ReadSecret("infrastructure/redis")
	if err == nil {
		t.Fatal("a 403 must be an error, not an empty secret map")
	}
	for _, want := range []string{"infrastructure/redis", "403", "permission denied"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q should mention %q", err, want)
		}
	}
}

func TestLogin_NonOKIsAnError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/auth/approle/login", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"errors":["invalid role or secret id"]}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	c := New(srv.URL, "bad", "bad")
	err := c.Login()
	if err == nil {
		t.Fatal("a rejected AppRole login must be an error")
	}
	if !strings.Contains(err.Error(), "invalid role or secret id") {
		t.Errorf("error should carry Vault's reason, got: %v", err)
	}
	// A failed login must leave no usable token behind.
	if c.token != "" {
		t.Errorf("token set after a failed login: %q", c.token)
	}
}

func TestReadDatabaseCreds_ReturnsLeaseAndCredentials(t *testing.T) {
	s := newVaultStub(t, 3600)
	c := New(s.server.URL, "role", "secret")

	creds, err := c.ReadDatabaseCreds("api-readwrite")
	if err != nil {
		t.Fatalf("ReadDatabaseCreds: %v", err)
	}
	if creds.Username != "v-approle-x" || creds.Password != "pw" {
		t.Errorf("credentials not extracted: %+v", creds)
	}
	// The lease id and duration are what a renewer needs; dropping either
	// makes the credential unrenewable and it dies mid-run.
	if creds.LeaseID == "" || creds.LeaseDuration != 3600 {
		t.Errorf("lease not extracted: %+v", creds)
	}
	if want := "/v1/database/creds/api-readwrite"; s.lastPath != want {
		t.Errorf("path = %q, want %q", s.lastPath, want)
	}
}
