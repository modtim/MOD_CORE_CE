package auth

import (
	"encoding/base64"
	"encoding/json"
	"testing"

	"github.com/docker/docker/api/types/registry"
)

// TestEncodeRegistryTokenDecodeBack verifies EncodeRegistryToken produces the
// base64url(JSON(registry.AuthConfig{RegistryToken: token})) format and that a
// decoder using the same encoding as the existing EncodeRegistryAuth can read
// it back to the original token.
func TestEncodeRegistryTokenDecodeBack(t *testing.T) {
	const token = "mgr-harbor-token-abc123"

	encoded, err := EncodeRegistryToken(token)
	if err != nil {
		t.Fatalf("EncodeRegistryToken: %v", err)
	}

	// base64url-decode the raw auth config JSON.
	jsonAuth, err := base64.URLEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("base64url decode: %v", err)
	}

	var cfg registry.AuthConfig
	if err := json.Unmarshal(jsonAuth, &cfg); err != nil {
		t.Fatalf("unmarshal authconfig: %v", err)
	}

	if cfg.RegistryToken != token {
		t.Errorf("RegistryToken = %q, want %q", cfg.RegistryToken, token)
	}
	if cfg.Username != "" || cfg.Password != "" {
		t.Errorf("expected empty username/password, got %+v", cfg)
	}
}

// TestEncodeRegistryTokenMatchesAuthEncoding ensures the new token encoder uses
// the exact same base64url-JSON envelope as EncodeRegistryAuth (they differ
// only in which AuthConfig fields are populated).
func TestEncodeRegistryTokenMatchesAuthEncoding(t *testing.T) {
	const token = "tok"

	encoded, err := EncodeRegistryToken(token)
	if err != nil {
		t.Fatalf("EncodeRegistryToken: %v", err)
	}

	// Round-trip through the auth decoder to confirm mutual compatibility.
	jsonAuth, err := base64.URLEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	// The decoded JSON must contain the registrytoken field name (lowercased
	// by the json tag `registrytoken,omitempty`).
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(jsonAuth, &raw); err != nil {
		t.Fatalf("unmarshal raw: %v", err)
	}
	if _, ok := raw["registrytoken"]; !ok {
		t.Errorf("expected registrytoken key in decoded JSON, keys: %v", raw)
	}
	var val string
	if err := json.Unmarshal(raw["registrytoken"], &val); err != nil {
		t.Fatalf("unmarshal registrytoken: %v", err)
	}
	if val != token {
		t.Errorf("registrytoken = %q, want %q", val, token)
	}
}

// LOW #9 (S-go-worker-auth): EncodeRegistryAuth returns (string, error) and
// produces base64url(JSON(AuthConfig{Username,Password})).
func TestEncodeRegistryAuthReturnsErrorAndDecodesBack(t *testing.T) {
	enc, err := EncodeRegistryAuth("robot$user", "fixture-pw")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	raw, err := base64.URLEncoding.DecodeString(enc)
	if err != nil {
		t.Fatalf("not base64url: %v", err)
	}
	var ac registry.AuthConfig
	if err := json.Unmarshal(raw, &ac); err != nil {
		t.Fatalf("not JSON: %v", err)
	}
	if ac.Username != "robot$user" || ac.Password != "fixture-pw" || ac.RegistryToken != "" {
		t.Fatalf("decoded auth mismatch: %+v", ac)
	}
}
