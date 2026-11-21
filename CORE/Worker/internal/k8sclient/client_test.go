package k8sclient

import (
	"os"
	"path/filepath"
	"testing"
)

// These tests run outside a cluster (no ServiceAccount token mounted), so
// rest.InClusterConfig() always fails here — exercising the kubeconfig
// fallback path deterministically without a live API server.

func TestLoadConfigExplicitPathNotFound(t *testing.T) {
	_, err := LoadConfig("/nonexistent/kubeconfig")
	if err == nil {
		t.Fatal("expected an error for a nonexistent kubeconfig path")
	}
}

func TestLoadConfigNoPathNoEnvNoHomeConfig(t *testing.T) {
	t.Setenv("KUBECONFIG", "")
	t.Setenv("HOME", t.TempDir()) // guaranteed no ~/.kube/config here

	_, err := LoadConfig("")
	if err == nil {
		t.Fatal("expected an error when no in-cluster config, no explicit path, no $KUBECONFIG, and no ~/.kube/config exist")
	}
}

func TestLoadConfigUsesKubeconfigEnv(t *testing.T) {
	dir := t.TempDir()
	kubeconfig := filepath.Join(dir, "config")
	if err := os.WriteFile(kubeconfig, []byte(minimalKubeconfig), 0o600); err != nil {
		t.Fatalf("writing fake kubeconfig: %v", err)
	}
	t.Setenv("KUBECONFIG", kubeconfig)

	cfg, err := LoadConfig("")
	if err != nil {
		t.Fatalf("LoadConfig() error = %v, want nil", err)
	}
	if cfg.Host != "https://example.invalid:6443" {
		t.Fatalf("cfg.Host = %q, want the fake cluster's server URL", cfg.Host)
	}
}

func TestLoadConfigExplicitPathTakesPriorityOverEnv(t *testing.T) {
	dir := t.TempDir()
	kubeconfig := filepath.Join(dir, "config")
	if err := os.WriteFile(kubeconfig, []byte(minimalKubeconfig), 0o600); err != nil {
		t.Fatalf("writing fake kubeconfig: %v", err)
	}
	t.Setenv("KUBECONFIG", "/should/not/be/used")

	cfg, err := LoadConfig(kubeconfig)
	if err != nil {
		t.Fatalf("LoadConfig() error = %v, want nil", err)
	}
	if cfg.Host != "https://example.invalid:6443" {
		t.Fatalf("cfg.Host = %q, want the explicit path's cluster URL", cfg.Host)
	}
}

func TestLoadClientsetBuildsFromKubeconfig(t *testing.T) {
	dir := t.TempDir()
	kubeconfig := filepath.Join(dir, "config")
	if err := os.WriteFile(kubeconfig, []byte(minimalKubeconfig), 0o600); err != nil {
		t.Fatalf("writing fake kubeconfig: %v", err)
	}

	clientset, err := LoadClientset(kubeconfig)
	if err != nil {
		t.Fatalf("LoadClientset() error = %v, want nil", err)
	}
	if clientset == nil {
		t.Fatal("LoadClientset() returned a nil clientset with no error")
	}
	// Local construction only — no cluster call is made, so this must not
	// require a reachable API server.
}

const minimalKubeconfig = `
apiVersion: v1
kind: Config
clusters:
- name: fake
  cluster:
    server: https://example.invalid:6443
    insecure-skip-tls-verify: true
contexts:
- name: fake
  context:
    cluster: fake
    user: fake
current-context: fake
users:
- name: fake
  user:
    token: fake-token
`
