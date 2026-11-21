package executor

import (
	"fmt"
	"testing"

	"gopkg.in/yaml.v3"
)

// GF.C2 — stack-service HF_ENDPOINT/HF_HUB_DISABLE_XET injection
// (injectHFEndpointCompose, wired into stackrunner.go's deploy path via
// applyModelCacheEnvToCompose). Table tests below cover the map-env and
// list-env compose shapes, spec-pinned HF_ENDPOINT preservation, an empty
// choice (set nothing), and malformed YAML.

type composeInjectTestDoc struct {
	Services map[string]struct {
		Environment interface{} `yaml:"environment"`
	} `yaml:"services"`
}

// envAsMap normalizes a service's `environment` (map or "K=V" list form) into
// a plain map[string]string so assertions don't care which shape the compose
// used.
func envAsMap(t *testing.T, raw interface{}) map[string]string {
	t.Helper()
	out := map[string]string{}
	switch v := raw.(type) {
	case nil:
	case map[string]interface{}:
		for k, val := range v {
			out[k] = fmt.Sprintf("%v", val)
		}
	case []interface{}:
		for _, e := range v {
			s, ok := e.(string)
			if !ok {
				continue
			}
			if k, val, ok := cutEnvKV(s); ok {
				out[k] = val
			}
		}
	default:
		t.Fatalf("unexpected environment shape %T", raw)
	}
	return out
}

func parseComposeForTest(t *testing.T, y string) composeInjectTestDoc {
	t.Helper()
	var doc composeInjectTestDoc
	if err := yaml.Unmarshal([]byte(y), &doc); err != nil {
		t.Fatalf("parse compose: %v", err)
	}
	return doc
}

func TestInjectHFEndpointCompose_MapEnv(t *testing.T) {
	compose := "services:\n  app:\n    image: foo\n    environment:\n      FOO: bar\n"

	out, err := injectHFEndpointCompose(compose, "https://cache.example.com", nil, "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	env := envAsMap(t, parseComposeForTest(t, out).Services["app"].Environment)
	if env["HF_ENDPOINT"] != "https://cache.example.com" {
		t.Errorf("HF_ENDPOINT = %q, want cache endpoint", env["HF_ENDPOINT"])
	}
	if env["HF_HUB_DISABLE_XET"] != "1" {
		t.Errorf("HF_HUB_DISABLE_XET = %q, want 1", env["HF_HUB_DISABLE_XET"])
	}
	if env["FOO"] != "bar" {
		t.Errorf("existing env var FOO dropped: %q", env["FOO"])
	}
}

func TestInjectHFEndpointCompose_ListEnv(t *testing.T) {
	compose := "services:\n  app:\n    image: foo\n    environment:\n      - FOO=bar\n"

	out, err := injectHFEndpointCompose(compose, "https://cache.example.com", nil, "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	env := envAsMap(t, parseComposeForTest(t, out).Services["app"].Environment)
	if env["HF_ENDPOINT"] != "https://cache.example.com" {
		t.Errorf("HF_ENDPOINT = %q, want cache endpoint", env["HF_ENDPOINT"])
	}
	if env["HF_HUB_DISABLE_XET"] != "1" {
		t.Errorf("HF_HUB_DISABLE_XET = %q, want 1", env["HF_HUB_DISABLE_XET"])
	}
	if env["FOO"] != "bar" {
		t.Errorf("existing env var FOO dropped: %q", env["FOO"])
	}
}

func TestInjectHFEndpointCompose_SpecPinnedHFEndpointPreserved(t *testing.T) {
	compose := "services:\n  app:\n    image: foo\n    environment:\n      HF_ENDPOINT: https://operator-pinned.example.com\n"

	out, err := injectHFEndpointCompose(compose, "https://cache.example.com", nil, "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	env := envAsMap(t, parseComposeForTest(t, out).Services["app"].Environment)
	if env["HF_ENDPOINT"] != "https://operator-pinned.example.com" {
		t.Errorf("operator-pinned HF_ENDPOINT overridden: got %q", env["HF_ENDPOINT"])
	}
	// The spec pinning HF_ENDPOINT does not exempt it from XET being disabled.
	if env["HF_HUB_DISABLE_XET"] != "1" {
		t.Errorf("HF_HUB_DISABLE_XET = %q, want 1 even with a pinned HF_ENDPOINT", env["HF_HUB_DISABLE_XET"])
	}
}

func TestInjectHFEndpointCompose_EmptyChoiceSetsNoEndpoint(t *testing.T) {
	compose := "services:\n  app:\n    image: foo\n    environment:\n      FOO: bar\n"

	out, err := injectHFEndpointCompose(compose, "", nil, "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	env := envAsMap(t, parseComposeForTest(t, out).Services["app"].Environment)
	if v, ok := env["HF_ENDPOINT"]; ok {
		t.Errorf("HF_ENDPOINT should not be set for an empty choice, got %q", v)
	}
	// Local cache / upstream-direct still benefit from XET being disabled.
	if env["HF_HUB_DISABLE_XET"] != "1" {
		t.Errorf("HF_HUB_DISABLE_XET = %q, want 1 even with an empty choice", env["HF_HUB_DISABLE_XET"])
	}
}

// TestInjectHFEndpointCompose_MalformedYAMLUnchanged covers the parse-failure
// path: injectHFEndpointCompose returns the ORIGINAL compose untouched (never
// a half-mutated or empty string) alongside its parse error. This is exactly
// what stackrunner.go's deploy path relies on: on error it logs and keeps
// `spec` as it was, so a malformed compose (which would fail `compose up`
// regardless) is never blamed on this injection step, and the deploy is never
// failed BY the injection itself.
func TestInjectHFEndpointCompose_MalformedYAMLUnchanged(t *testing.T) {
	compose := "services:\n  app: [this is not\n valid yaml"

	out, err := injectHFEndpointCompose(compose, "https://cache.example.com", nil, "")
	if err == nil {
		t.Fatalf("expected a parse error for malformed yaml")
	}
	if out != compose {
		t.Errorf("expected compose returned unchanged on parse error, got:\n%s", out)
	}
}

// TestInjectHFEndpointCompose_TokenSwap covers the GF.C6.5 tenant-token swap:
// when a service's effective HF_ENDPOINT is a known MOD model-cache endpoint,
// its HF_TOKEN is replaced with the API-minted tenant token (the cache
// verifies it and fetches the tenant's real token itself); any service whose
// effective endpoint is NOT a MOD cache keeps its raw HF_TOKEN untouched.
func TestInjectHFEndpointCompose_TokenSwap(t *testing.T) {
	const modCache = "https://models.example.com"
	compose := `
services:
  fetcher:
    environment:
      HF_TOKEN: raw-tenant-token
  pinned-mod:
    environment:
      HF_ENDPOINT: https://models.example.com
      HF_TOKEN: raw-tenant-token
  pinned-foreign:
    environment:
      HF_ENDPOINT: https://my-own-cache.example.org
      HF_TOKEN: raw-tenant-token
`
	out, err := injectHFEndpointCompose(compose, modCache, []string{modCache}, "mc-token-123")
	if err != nil {
		t.Fatalf("injectHFEndpointCompose: %v", err)
	}
	var doc composeInjectTestDoc
	if err := yaml.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatalf("unmarshal out: %v", err)
	}
	got := doc.Services["fetcher"].Environment
	if m := envAsMap(t, got); m["HF_TOKEN"] != "mc-token-123" {
		t.Errorf("fetcher HF_TOKEN = %q, want the minted token (chosen endpoint is a MOD cache)", m["HF_TOKEN"])
	}
	if m := envAsMap(t, doc.Services["pinned-mod"].Environment); m["HF_TOKEN"] != "mc-token-123" {
		t.Errorf("pinned-mod HF_TOKEN = %q, want the minted token (pinned endpoint is a MOD cache)", m["HF_TOKEN"])
	}
	if m := envAsMap(t, doc.Services["pinned-foreign"].Environment); m["HF_TOKEN"] != "raw-tenant-token" {
		t.Errorf("pinned-foreign HF_TOKEN = %q, want the raw token untouched (operator's own cache)", m["HF_TOKEN"])
	}
	// A spec pointing at upstream direct ("" choice, no pins) keeps the raw
	// token so it passes through to huggingface.co as-is.
	upstreamOut, err := injectHFEndpointCompose(compose, "", []string{modCache}, "mc-token-123")
	if err != nil {
		t.Fatalf("injectHFEndpointCompose (upstream): %v", err)
	}
	var upDoc composeInjectTestDoc
	if err := yaml.Unmarshal([]byte(upstreamOut), &upDoc); err != nil {
		t.Fatalf("unmarshal out: %v", err)
	}
	if m := envAsMap(t, upDoc.Services["fetcher"].Environment); m["HF_TOKEN"] != "raw-tenant-token" {
		t.Errorf("upstream fetcher HF_TOKEN = %q, want raw token untouched", m["HF_TOKEN"])
	}
	// No minted token (API didn't issue one) never modifies HF_TOKEN.
	noTok, err := injectHFEndpointCompose(compose, modCache, []string{modCache}, "")
	if err != nil {
		t.Fatalf("injectHFEndpointCompose (no token): %v", err)
	}
	var noDoc composeInjectTestDoc
	if err := yaml.Unmarshal([]byte(noTok), &noDoc); err != nil {
		t.Fatalf("unmarshal out: %v", err)
	}
	if m := envAsMap(t, noDoc.Services["fetcher"].Environment); m["HF_TOKEN"] != "raw-tenant-token" {
		t.Errorf("no-token fetcher HF_TOKEN = %q, want raw token untouched", m["HF_TOKEN"])
	}
}

// TestComposeDeclaresHFToken covers the gated-download marker for the
// cross-site guard: any service declaring an HF_TOKEN (in environment, map or
// list form, or as an x-mod.secrets ref) makes the spec gated.
func TestComposeDeclaresHFToken(t *testing.T) {
	envMap := "services:\n  f:\n    environment:\n      HF_TOKEN: raw\n"
	envList := "services:\n  f:\n    environment:\n      - HF_TOKEN=raw\n"
	secretRef := "services:\n  f:\n    x-mod:\n      secrets:\n        HF_TOKEN: 'vault:tenants/1/HF_TOKEN'\n"
	none := "services:\n  f:\n    environment:\n      HF_REPO: org/model\n"
	if !composeDeclaresHFToken(envMap) {
		t.Error("env-map HF_TOKEN: want gated=true")
	}
	if !composeDeclaresHFToken(envList) {
		t.Error("env-list HF_TOKEN: want gated=true")
	}
	if !composeDeclaresHFToken(secretRef) {
		t.Error("x-mod.secrets HF_TOKEN ref: want gated=true")
	}
	if composeDeclaresHFToken(none) {
		t.Error("no HF_TOKEN: want gated=false")
	}
	if composeDeclaresHFToken("not: [valid yaml") {
		t.Error("unparseable spec: want gated=false")
	}
}
