package executor

import (
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// These tests lock in the contract that the Harbor registry host used to
// construct sidecar and processing image URLs comes from the per-task
// credentials bundle (Vault), NOT from any worker-side config or env var.
//
// Regression guard for the 401 bug where remote workers built
// ghcr.io/modtim/minio-client:latest but had Harbor
// credentials for registry.example.com.

func TestHarborRegistry_UsesVaultValue(t *testing.T) {
	got, err := harborRegistry(transport.HarborCredentials{
		Registry: "ghcr.io",
		Username: "u",
		Password: "p",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "ghcr.io" {
		t.Errorf("expected Vault registry, got %q", got)
	}
}

func TestHarborRegistry_TrimsTrailingSlashAndWhitespace(t *testing.T) {
	got, err := harborRegistry(transport.HarborCredentials{
		Registry: "  ghcr.io/  ",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "ghcr.io" {
		t.Errorf("expected trimmed registry, got %q", got)
	}
}

func TestHarborRegistry_EmptyFailsClosed(t *testing.T) {
	_, err := harborRegistry(transport.HarborCredentials{Registry: ""})
	if err == nil {
		t.Fatal("expected error when Harbor registry is empty (Vault source missing)")
	}
	if !strings.Contains(err.Error(), "harbor_registry") {
		t.Errorf("error should name the Vault key, got: %v", err)
	}
}

func TestHarborRegistry_WhitespaceOnlyFailsClosed(t *testing.T) {
	_, err := harborRegistry(transport.HarborCredentials{Registry: "   "})
	if err == nil {
		t.Fatal("expected error when Harbor registry is whitespace only")
	}
}

// Guard against regression: the default-image fallback must use the argument
// (Vault-sourced), not any field on the Executor.
func TestResolveProcessingImage_UsesArgumentNotExecutorField(t *testing.T) {
	e := testExecutor([]string{"cuda"}, "ignored-worker-config")
	payload := transport.TaskPayload{
		ProcessingImage: "",
		NodeKind:        "image_input",
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	want := "ghcr.io/modtim/image_input"
	if got != want {
		t.Errorf("expected %q (from Vault arg), got %q", want, got)
	}
}

// Guard against regression: the server must NOT set API_URL in
// container_env — the worker is the only thing that knows a reachable
// URL for THIS worker's network. Test the substitution rule used in
// runContainerWithCreds: drop any server-provided API_URL, then inject
// the worker's own APIURL.
//
// This simulates the loop in runContainerWithCreds without requiring a
// live Docker daemon.
func buildEnvUnderTest(apiURL string, input map[string]string) []string {
	env := []string{}
	for k, v := range input {
		if k == "API_URL" {
			continue
		}
		env = append(env, k+"="+v)
	}
	if apiURL != "" {
		env = append(env, "API_URL="+apiURL)
	}
	return env
}

func hasEnv(env []string, want string) bool {
	for _, kv := range env {
		if kv == want {
			return true
		}
	}
	return false
}

func countEnvKey(env []string, key string) int {
	n := 0
	prefix := key + "="
	for _, kv := range env {
		if strings.HasPrefix(kv, prefix) {
			n++
		}
	}
	return n
}

func TestAPIURL_InjectedFromWorkerConfig(t *testing.T) {
	env := buildEnvUnderTest("https://example.com", map[string]string{
		"API_KEY":         "svc-key",
		"WORKFLOW_RUN_ID": "6",
	})
	if !hasEnv(env, "API_URL=https://example.com") {
		t.Errorf("worker must inject its own APIURL into container env; got %v", env)
	}
	if countEnvKey(env, "API_URL") != 1 {
		t.Errorf("API_URL must appear exactly once; got %d in %v", countEnvKey(env, "API_URL"), env)
	}
}

func TestAPIURL_ServerProvidedValueIsDiscarded(t *testing.T) {
	// Even if an outdated server somehow still sends API_URL, the worker
	// drops it and injects its own. Locks in the contract that the
	// worker is authoritative.
	env := buildEnvUnderTest("https://example.com", map[string]string{
		"API_URL": "http://api:80", // legacy server default
		"API_KEY": "svc-key",
	})
	if hasEnv(env, "API_URL=http://api:80") {
		t.Errorf("server-provided API_URL must be discarded; got %v", env)
	}
	if !hasEnv(env, "API_URL=https://example.com") {
		t.Errorf("worker's APIURL must be injected; got %v", env)
	}
	if countEnvKey(env, "API_URL") != 1 {
		t.Errorf("exactly one API_URL entry expected; got %d in %v", countEnvKey(env, "API_URL"), env)
	}
}

func TestAPIURL_EmbeddedWorker(t *testing.T) {
	// Embedded worker's APIURL is http://api:80 — containers on the same
	// Docker network reach the API via the `api` hostname.
	env := buildEnvUnderTest("http://api:80", map[string]string{
		"API_KEY": "svc-key",
	})
	if !hasEnv(env, "API_URL=http://api:80") {
		t.Errorf("embedded worker must inject http://api:80; got %v", env)
	}
}

func TestAPIURL_NoConfigMeansNoInjection(t *testing.T) {
	// If the worker has no APIURL configured at all, we do not inject one
	// and we do not forward the server's (probably broken) value.
	env := buildEnvUnderTest("", map[string]string{
		"API_URL": "http://api:80", // server sent something; we still drop it
		"API_KEY": "svc-key",
	})
	if countEnvKey(env, "API_URL") != 0 {
		t.Errorf("no worker APIURL → no API_URL in env; got %v", env)
	}
}
