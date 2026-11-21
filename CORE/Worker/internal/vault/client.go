package vault

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Client provides Vault AppRole authentication and KV v2 secret reading.
// Datacenter and embedded workers use this to fetch credentials for Redis,
// Keycloak, Harbor, etc. Remote workers skip Vault entirely — the API
// proxies credentials for them.
type Client struct {
	addr     string // e.g. http://vault:8200
	roleID   string
	secretID string

	httpClient *http.Client

	mu    sync.Mutex
	token string
	expAt time.Time

	// GSEC Q21.8 — degraded boot. A failed login no longer kills the worker:
	// the boot caller keeps running on API-mediated creds while a background
	// loop retries with backoff from retryMin up to retryMax (defaults 10s →
	// 5m; tests shrink them). `degraded` is true while no login has succeeded,
	// and `warning` carries the heartbeat-visible message for that state.
	retryMin time.Duration
	retryMax time.Duration
	degraded atomic.Bool
	warning  atomic.Value

	// lastLoginErr is the most recent login failure, kept for the warning text.
	lastLoginErr error
}

// New creates a Vault client. Call Login() before reading secrets.
func New(addr, roleID, secretID string) *Client {
	return &Client{
		addr:     strings.TrimRight(addr, "/"),
		roleID:   roleID,
		secretID: secretID,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
		retryMin: 10 * time.Second,
		retryMax: 5 * time.Minute,
	}
}

// NewWithEnv is the per-worker credential source (GSEC Q21.8): the env values
// the install script writes to /etc/mod-worker/worker.env
// (VAULT_APPROLE_ROLE_ID / VAULT_APPROLE_SECRET_ID — the names already used
// by worker_install.sh and internal/config) win over the shared platform role
// passed in; the shared role stays as the fallback for workers that predate
// per-worker rendering.
func NewWithEnv(addr, fallbackRoleID, fallbackSecretID string) *Client {
	roleID, secretID := fallbackRoleID, fallbackSecretID
	if v := os.Getenv("VAULT_APPROLE_ROLE_ID"); v != "" {
		roleID = v
	}
	if v := os.Getenv("VAULT_APPROLE_SECRET_ID"); v != "" {
		secretID = v
	}
	return New(addr, roleID, secretID)
}

// Login authenticates via AppRole and caches the token.
// Automatically called by ReadSecret if the token is expired.
// A failure marks the client degraded (GSEC Q21.8): callers must not exit on
// it — the worker runs on API-mediated creds while RetryLogin recovers the
// login in the background.
func (c *Client) Login() error {
	err := c.loginWith(c.roleID, c.secretID)
	if err != nil {
		c.degraded.Store(true)
		c.warning.Store("vault: login failed, running degraded on API-mediated creds: " + err.Error())
		return err
	}
	c.degraded.Store(false)
	c.warning.Store("")
	return nil
}

func (c *Client) loginWith(roleID, secretID string) error {
	body, err := json.Marshal(map[string]string{
		"role_id":   roleID,
		"secret_id": secretID,
	})
	if err != nil {
		return err
	}

	resp, err := c.httpClient.Post(
		c.addr+"/v1/auth/approle/login",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return fmt.Errorf("vault approle login: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("vault approle login: HTTP %d: %s", resp.StatusCode, truncate(b, 500))
	}

	var result struct {
		Auth struct {
			ClientToken   string `json:"client_token"`
			LeaseDuration int    `json:"lease_duration"`
		} `json:"auth"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return fmt.Errorf("vault approle login decode: %w", err)
	}

	c.mu.Lock()
	c.token = result.Auth.ClientToken
	// Refresh 5 minutes before expiry
	c.expAt = time.Now().Add(time.Duration(result.Auth.LeaseDuration)*time.Second - 5*time.Minute)
	c.mu.Unlock()

	return nil
}

// ReadSecret reads a KV v2 secret at the given path.
// Auto-refreshes the Vault token if expired.
// Returns the inner data map (the user-written key/value pairs).
func (c *Client) ReadSecret(path string) (map[string]string, error) {
	if err := c.ensureToken(); err != nil {
		return nil, err
	}

	req, err := http.NewRequest("GET", c.addr+"/v1/secret/data/"+path, nil)
	if err != nil {
		return nil, err
	}

	c.mu.Lock()
	req.Header.Set("X-Vault-Token", c.token)
	c.mu.Unlock()

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("vault read %s: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("vault read %s: HTTP %d: %s", path, resp.StatusCode, truncate(b, 500))
	}

	var result struct {
		Data struct {
			Data map[string]string `json:"data"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("vault read %s decode: %w", path, err)
	}

	return result.Data.Data, nil
}

// GetInfraSecret reads a secret from the infrastructure path in Vault.
// Example: GetInfraSecret("redis") reads "infrastructure/redis".
func (c *Client) GetInfraSecret(name string) (map[string]string, error) {
	return c.ReadSecret("infrastructure/" + name)
}

// DatabaseCreds holds short-lived credentials from Vault's database secrets engine.
type DatabaseCreds struct {
	Username      string
	Password      string
	LeaseID       string
	LeaseDuration int // seconds
}

// ReadDatabaseCreds requests dynamic credentials from the database secrets engine.
// Example: ReadDatabaseCreds("api-readwrite") calls GET /v1/database/creds/api-readwrite.
func (c *Client) ReadDatabaseCreds(role string) (*DatabaseCreds, error) {
	if err := c.ensureToken(); err != nil {
		return nil, err
	}

	req, err := http.NewRequest("GET", c.addr+"/v1/database/creds/"+role, nil)
	if err != nil {
		return nil, err
	}

	c.mu.Lock()
	req.Header.Set("X-Vault-Token", c.token)
	c.mu.Unlock()

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("vault database creds %s: %w", role, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("vault database creds %s: HTTP %d: %s", role, resp.StatusCode, truncate(b, 500))
	}

	var result struct {
		LeaseID       string `json:"lease_id"`
		LeaseDuration int    `json:"lease_duration"`
		Data          struct {
			Username string `json:"username"`
			Password string `json:"password"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("vault database creds %s decode: %w", role, err)
	}

	return &DatabaseCreds{
		Username:      result.Data.Username,
		Password:      result.Data.Password,
		LeaseID:       result.LeaseID,
		LeaseDuration: result.LeaseDuration,
	}, nil
}

// ApplySecretID implements the worker half of the Q21.8 rotation window: the
// heartbeat delivers a new secret_id for this worker's AppRole, and the old
// one stays valid until the worker proves the new one works. The new login is
// attempted FIRST; only on success do we switch over (drop the old secret and
// re-login to replace the token minted from it). On failure the old secret
// stays in place — rotation is non-destructive by construction, and the API
// revokes the old secret_id only after the worker acks a successful switch.
// Returns true when the switch happened.
func (c *Client) ApplySecretID(newSecretID string) bool {
	if newSecretID == "" || newSecretID == c.secretID {
		return false
	}
	if err := c.loginWith(c.roleID, newSecretID); err != nil {
		return false
	}
	c.mu.Lock()
	c.secretID = newSecretID
	c.mu.Unlock()
	return true
}

// Degraded reports whether this client has never logged in successfully
// (GSEC Q21.8 degraded boot).
func (c *Client) Degraded() bool { return c.degraded.Load() }

// Warning returns the heartbeat-visible degraded-mode message, or "" when the
// login is healthy. Never contains the secret itself.
func (c *Client) Warning() string {
	if w, ok := c.warning.Load().(string); ok {
		return w
	}
	return ""
}

// RetryLogin runs the degraded-boot recovery loop: while degraded, retry the
// AppRole login with exponential backoff from retryMin (default 10s) up to
// retryMax (default 5m). The first attempt is immediate so a Vault that was
// briefly sealed or unreachable at boot recovers without waiting a full
// window. Returns nil when a login succeeds, ctx.Err() when cancelled.
func (c *Client) RetryLogin(ctx context.Context) error {
	backoff := c.retryMin
	for {
		if err := c.Login(); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
		if backoff < c.retryMax {
			backoff *= 2
			if backoff > c.retryMax {
				backoff = c.retryMax
			}
		}
	}
}

func (c *Client) ensureToken() error {
	c.mu.Lock()
	expired := c.token == "" || time.Now().After(c.expAt)
	c.mu.Unlock()

	if expired {
		return c.Login()
	}
	return nil
}

func truncate(b []byte, n int) string {
	s := string(b)
	if len(s) > n {
		return s[:n]
	}
	return s
}
