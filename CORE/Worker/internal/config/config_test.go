package config

import (
	"bytes"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// --- k8s-reduction-engine backend selection (design.md "Config / tiering") ---

func TestBackendDefaultsToDocker(t *testing.T) {
	cfg := defaults()
	if cfg.ExecutorBackend != BackendDocker {
		t.Fatalf("default ExecutorBackend = %q, want %q", cfg.ExecutorBackend, BackendDocker)
	}
	if cfg.StackBackend != BackendDocker {
		t.Fatalf("default StackBackend = %q, want %q", cfg.StackBackend, BackendDocker)
	}
	if cfg.IsK8sExecutorBackend() {
		t.Fatal("IsK8sExecutorBackend() should be false by default")
	}
	if cfg.IsK8sStackBackend() {
		t.Fatal("IsK8sStackBackend() should be false by default")
	}
}

func TestBackendEnvOverride(t *testing.T) {
	t.Setenv("MOD_EXECUTOR_BACKEND", "k8s")
	t.Setenv("MOD_STACK_BACKEND", "k8s")
	cfg := defaults()
	loadEnv(cfg)

	if !cfg.IsK8sExecutorBackend() {
		t.Fatal("MOD_EXECUTOR_BACKEND=k8s should select the K8s executor backend")
	}
	if !cfg.IsK8sStackBackend() {
		t.Fatal("MOD_STACK_BACKEND=k8s should select the K8s stack backend")
	}
}

func TestBackendEnvCaseInsensitiveAndTrimmed(t *testing.T) {
	t.Setenv("MOD_EXECUTOR_BACKEND", "  K8S  ")
	cfg := defaults()
	loadEnv(cfg)

	if !cfg.IsK8sExecutorBackend() {
		t.Fatal("MOD_EXECUTOR_BACKEND should be case-insensitive and tolerate whitespace")
	}
}

func TestBackendUnknownValueFailsSafeToDocker(t *testing.T) {
	t.Setenv("MOD_EXECUTOR_BACKEND", "openshift")
	cfg := defaults()
	loadEnv(cfg)

	if cfg.IsK8sExecutorBackend() {
		t.Fatal("an unrecognized backend value must fail safe to docker, not silently enable k8s")
	}
	if cfg.ExecutorBackend != BackendDocker {
		t.Fatalf("ExecutorBackend = %q, want %q", cfg.ExecutorBackend, BackendDocker)
	}
}

func TestKubeconfigPathFromEnv(t *testing.T) {
	t.Setenv("KUBECONFIG", "/tmp/example-kubeconfig")
	cfg := defaults()
	loadEnv(cfg)

	if cfg.KubeconfigPath != "/tmp/example-kubeconfig" {
		t.Fatalf("KubeconfigPath = %q, want /tmp/example-kubeconfig", cfg.KubeconfigPath)
	}
}

// --- R-161 follow-up: token server bind env override ---

func TestTokenServerBindDefaultsToEmptyAuto(t *testing.T) {
	cfg := defaults()
	loadEnv(cfg)
	if cfg.TokenServerBind != "" {
		t.Fatalf("TokenServerBind default = %q, want empty (auto)", cfg.TokenServerBind)
	}
}

func TestTokenServerBindEnvOverride(t *testing.T) {
	t.Setenv("MOD_TOKEN_SERVER_BIND", "::")
	cfg := defaults()
	loadEnv(cfg)
	if cfg.TokenServerBind != "::" {
		t.Fatalf("TokenServerBind = %q, want :: from MOD_TOKEN_SERVER_BIND", cfg.TokenServerBind)
	}
}

func TestTokenServerBindEmptyEnvKeepsYAMLValue(t *testing.T) {
	t.Setenv("MOD_TOKEN_SERVER_BIND", "")
	cfg := defaults()
	cfg.TokenServerBind = "127.0.0.1" // e.g. set via YAML
	loadEnv(cfg)
	if cfg.TokenServerBind != "127.0.0.1" {
		t.Fatalf("empty MOD_TOKEN_SERVER_BIND must not clobber the YAML value, got %q", cfg.TokenServerBind)
	}
}

// --- P9B-5R-a: S3_* is the new reader name; MINIO_* stays as fallback ---

func TestMinioEndpointPrefersS3Name(t *testing.T) {
	t.Setenv("S3_INTERNAL_URL", "http://s3-new:7070")
	t.Setenv("MINIO_INTERNAL_URL", "http://old:9000")
	cfg := defaults()
	loadEnv(cfg)

	if cfg.MinIOEndpoint != "http://s3-new:7070" {
		t.Fatalf("MinIOEndpoint = %q; want the S3_INTERNAL_URL value", cfg.MinIOEndpoint)
	}
}

func TestMinioEndpointFallsBackToOldName(t *testing.T) {
	t.Setenv("MINIO_INTERNAL_URL", "http://old:9000")
	os.Unsetenv("S3_INTERNAL_URL")
	cfg := defaults()
	loadEnv(cfg)

	if cfg.MinIOEndpoint != "http://old:9000" {
		t.Fatalf("MinIOEndpoint = %q; want the MINIO_INTERNAL_URL fallback", cfg.MinIOEndpoint)
	}
}

func TestMinioEndpointUnsetStaysEmpty(t *testing.T) {
	os.Unsetenv("S3_INTERNAL_URL")
	os.Unsetenv("MINIO_INTERNAL_URL")
	cfg := defaults()
	loadEnv(cfg)

	if cfg.MinIOEndpoint != "" {
		t.Fatalf("MinIOEndpoint = %q; want empty (no default in this file)", cfg.MinIOEndpoint)
	}
}

// --- embedded worker identity: the manager-node worker is ALWAYS mod-worker-embedded ---

func TestEmbeddedModeForcesCanonicalWorkerName(t *testing.T) {
	for _, envName := range []string{"mod-worker-ce", "mod-worker", "anything-else", ""} {
		t.Run("env="+envName, func(t *testing.T) {
			t.Setenv("WORKER_MODE", "embedded")
			t.Setenv("WORKER_NAME", envName)
			cfg := defaults()
			loadEnv(cfg)
			applyEmbeddedIdentity(cfg)
			if cfg.WorkerName != EmbeddedWorkerName {
				t.Fatalf("embedded WorkerName = %q, want %q", cfg.WorkerName, EmbeddedWorkerName)
			}
		})
	}
	if EmbeddedWorkerName != "mod-worker-embedded" {
		t.Fatalf("EmbeddedWorkerName = %q; platform scripts key off the exact literal", EmbeddedWorkerName)
	}
}

func TestEmbeddedModeIsCaseInsensitive(t *testing.T) {
	t.Setenv("WORKER_MODE", "Embedded")
	t.Setenv("WORKER_NAME", "mod-worker-ce")
	cfg := defaults()
	loadEnv(cfg)
	applyEmbeddedIdentity(cfg)
	if cfg.WorkerName != EmbeddedWorkerName {
		t.Fatalf("WorkerName = %q, want %q", cfg.WorkerName, EmbeddedWorkerName)
	}
}

func TestRemoteModeKeepsOperatorChosenName(t *testing.T) {
	t.Setenv("WORKER_MODE", "remote")
	t.Setenv("WORKER_NAME", "gpu-box-7")
	cfg := defaults()
	loadEnv(cfg)
	applyEmbeddedIdentity(cfg)
	if cfg.WorkerName != "gpu-box-7" {
		t.Fatalf("remote WorkerName = %q, want gpu-box-7", cfg.WorkerName)
	}
}

func TestDefaultModeIsRemoteSoNameIsNotForced(t *testing.T) {
	t.Setenv("WORKER_MODE", "")
	t.Setenv("WORKER_NAME", "custom")
	cfg := defaults()
	loadEnv(cfg)
	applyEmbeddedIdentity(cfg)
	if cfg.WorkerName != "custom" {
		t.Fatalf("default-mode WorkerName = %q, want custom", cfg.WorkerName)
	}
}

// --- S-go-worker-auth LOW findings #3 and #4 ---

func captureSlog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(old) })
	return &buf
}

func TestLoadYAMLWarnsOnWorldReadableConfig(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("permission bits are not meaningful on windows")
	}
	buf := captureSlog(t)
	dir := t.TempDir()
	p := filepath.Join(dir, "worker.yaml")
	if err := os.WriteFile(p, []byte("bootstrap_token: SECRETVALUE-xyz\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(p, 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := defaults()
	if err := loadYAML(p, cfg); err != nil {
		t.Fatal(err)
	}
	out := buf.String()
	if !strings.Contains(out, "accessible to group/other") || !strings.Contains(out, p) {
		t.Fatalf("expected loose-mode warning naming the path, got %q", out)
	}
	if strings.Contains(out, "SECRETVALUE") {
		t.Fatalf("warning must never log file content: %q", out)
	}
}

func TestLoadYAMLNoWarningOnOwnerOnlyConfig(t *testing.T) {
	buf := captureSlog(t)
	p := filepath.Join(t.TempDir(), "worker.yaml")
	if err := os.WriteFile(p, []byte("worker_name: x\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := loadYAML(p, defaults()); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(buf.String(), "accessible to group/other") {
		t.Fatalf("0600 file must not warn: %q", buf.String())
	}
}

func TestLoadEnvWarnsOnInvalidNumericEnv(t *testing.T) {
	buf := captureSlog(t)
	t.Setenv("CONTAINER_TIMEOUT", "30")
	t.Setenv("MAX_CONCURRENT_JOBS", "many")
	cfg := defaults()
	want := cfg.ContainerTimeout
	loadEnv(cfg)
	out := buf.String()
	if !strings.Contains(out, "CONTAINER_TIMEOUT") || !strings.Contains(out, "MAX_CONCURRENT_JOBS") {
		t.Fatalf("expected one warning per bad var, got %q", out)
	}
	if cfg.ContainerTimeout != want || cfg.MaxConcurrentJobs != 0 {
		t.Fatalf("bad env must keep previous values, got %v / %d", cfg.ContainerTimeout, cfg.MaxConcurrentJobs)
	}
}

func TestLoadEnvValidNumericEnvNoWarning(t *testing.T) {
	buf := captureSlog(t)
	t.Setenv("CONTAINER_TIMEOUT", "45m")
	t.Setenv("MAX_CONCURRENT_JOBS", "3")
	cfg := defaults()
	loadEnv(cfg)
	if buf.Len() != 0 || cfg.MaxConcurrentJobs != 3 || cfg.ContainerTimeout.Minutes() != 45 {
		t.Fatalf("valid env: log=%q jobs=%d timeout=%v", buf.String(), cfg.MaxConcurrentJobs, cfg.ContainerTimeout)
	}
}

func TestValidateRejectsNegativeMaxConcurrentJobs(t *testing.T) {
	cfg := defaults()
	cfg.WorkerName = "w"
	cfg.MaxConcurrentJobs = -1
	if err := validate(cfg); err == nil || !strings.Contains(err.Error(), "max_concurrent_jobs") {
		t.Fatalf("negative max_concurrent_jobs must be rejected, got %v", err)
	}
}
