package config

import (
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Mode determines how the worker connects to infrastructure.
//
//   - embedded:   runs inside docker-compose, uses container hostnames (redis:6379),
//                 direct Redis pub/sub, direct Keycloak STS, Vault for secrets
//   - remote:     any non-embedded worker (LAN or internet), everything through the HTTPS API
//   - datacenter: DEPRECATED alias for remote (kept for backwards compatibility)
type Mode string

const (
	ModeEmbedded   Mode = "embedded"
	ModeDatacenter Mode = "datacenter" // Deprecated: treated as remote
	ModeRemote     Mode = "remote"
)

// ExecutionBackend selects which infrastructure a worker submits ephemeral
// node tasks / persistent stacks to. See the internal design notes
// design.md "Config / tiering". T2 (K8sBackend) / T4 (K8sStackBackend) read
// this via Config.IsK8sExecutorBackend() / Config.IsK8sStackBackend() rather
// than re-parsing the env directly.
type ExecutionBackend string

const (
	BackendDocker ExecutionBackend = "docker"
	BackendK8s    ExecutionBackend = "k8s"
)

// normalizeBackend validates a raw backend string, defaulting to docker for
// empty/unrecognized input (fail-safe: an on-prem box with no K8s config
// should never silently try to submit K8s Jobs).
func normalizeBackend(raw string) ExecutionBackend {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case string(BackendK8s):
		return BackendK8s
	default:
		return BackendDocker
	}
}

// Config holds all worker configuration.
type Config struct {
	// Identity
	WorkerName string `yaml:"worker_name"`
	Mode       Mode   `yaml:"mode"` // embedded, datacenter, remote

	// Queues this worker listens on (e.g., ["CPU", "CUDA"])
	Queues []string `yaml:"queues"`

	// Concurrency
	MaxConcurrentJobs int           `yaml:"max_concurrent_jobs"`
	ContainerTimeout  time.Duration `yaml:"container_timeout"`

	// Direct mode (embedded + datacenter): service addresses
	RedisURL       string `yaml:"redis_url"`        // e.g. redis://:pass@redis:6379/2
	KeycloakURL    string `yaml:"keycloak_url"`      // e.g. http://keycloak:8080
	KeycloakRealm  string `yaml:"keycloak_realm"`    // e.g. modtex
	KeycloakClient string `yaml:"keycloak_client"`   // e.g. platform-service
	MinIOEndpoint  string `yaml:"minio_endpoint"`    // e.g. http://minio:9000
	DockerRegistry string `yaml:"docker_registry"`   // e.g. example.com
	LocalRegistry  string `yaml:"local_registry"`    // e.g. registry:5000 (user containers)
	DockerNetwork  string `yaml:"docker_network"`    // e.g. mod_modtex-network

	// Vault (embedded + datacenter only)
	VaultAddr         string `yaml:"vault_addr"`           // e.g. http://vault:8200
	VaultRoleID       string `yaml:"vault_role_id"`        // AppRole role_id
	VaultSecretID     string `yaml:"vault_secret_id"`      // AppRole secret_id
	VaultSecretsPath  string `yaml:"vault_secrets_path"`   // KV v2 mount path prefix (default: "infrastructure")

	// API mode (remote, also used by all modes for result reporting)
	APIURL         string `yaml:"api_url"`          // e.g. https://api.yourdomain.com
	BootstrapToken string `yaml:"bootstrap_token"`  // enrollment token for remote auth

	// dual-anchor-mesh-federation — the two mesh anchors this dual-homed worker
	// is enrolled to. Used only to derive per-anchor advertise addresses for the
	// heartbeat (site-first / cloud-fallback resolution). Empty ⇒ that anchor is
	// not reported (single-anchor / pre-dual-homing worker).
	SiteAnchorHost  string `yaml:"site_anchor_host"`  // local control-plane / manager site name
	CloudAnchorHost string `yaml:"cloud_anchor_host"` // fixed cloud rendezvous name

	// Heartbeat
	HeartbeatInterval time.Duration `yaml:"heartbeat_interval"`

	// NOAH opportunistic idle-harvest. Off by default; the server may
	// override these live via the heartbeat response.
	NoahEnabled        bool          `yaml:"noah_enabled"`
	NoahSustainedQuiet time.Duration `yaml:"noah_sustained_quiet"` // quiet-for before claimable
	NoahYieldPoll      time.Duration `yaml:"noah_yield_poll"`      // availability poll cadence
	NoahPauseGrace     time.Duration `yaml:"noah_pause_grace"`     // pause-hold before evict
	NoahPluggedInOnly  bool          `yaml:"noah_plugged_in_only"`
	NoahScheduleStart  string        `yaml:"noah_schedule_start"` // "HH:MM" local; empty = always
	NoahScheduleEnd    string        `yaml:"noah_schedule_end"`

	// k8s-reduction-engine — backend selection (design.md "Config / tiering").
	// Read once at worker start; NOT hot-reloaded. docker is the safe default
	// for every existing on-prem/SaaS deployment.
	ExecutorBackend ExecutionBackend `yaml:"executor_backend"` // ephemeral node tasks: docker|k8s
	StackBackend    ExecutionBackend `yaml:"stack_backend"`    // persistent stacks (NIM/VLM/SDXL): docker|k8s

	// KubeconfigPath: explicit kubeconfig file for the k8s client-go loader
	// (internal/k8sclient). Empty ⇒ try in-cluster ServiceAccount config first,
	// falling back to $HOME/.kube/config. Only consulted when ExecutorBackend
	// or StackBackend == k8s.
	KubeconfigPath string `yaml:"kubeconfig_path"`

	// R-161: listen host for the worker's node run-token endpoint
	// (executor.TokenServer). "auto" (default) derives the per-backend narrow
	// bind from the worker's docker networks (see tokenserver.go); any other
	// value — e.g. "::" for k8s workers whose task Pods are remote — is used
	// verbatim. Never 0.0.0.0 in auto mode. Env override: MOD_TOKEN_SERVER_BIND
	// (the k8s chart sets it to "::" — task Pods reach the worker through the
	// in-cluster {release}-worker Service, not loopback or docker gateways).
	TokenServerBind string `yaml:"token_server_bind"`
}

// Load reads configuration from YAML file (if it exists) then overlays environment variables.
// Environment variables always take precedence.
func Load(configPath string) (*Config, error) {
	cfg := defaults()

	// Try YAML file first
	if configPath != "" {
		if err := loadYAML(configPath, cfg); err != nil {
			return nil, fmt.Errorf("loading config file %s: %w", configPath, err)
		}
	}

	// Environment variables override YAML
	loadEnv(cfg)

	applyEmbeddedIdentity(cfg)

	if err := validate(cfg); err != nil {
		return nil, fmt.Errorf("config validation: %w", err)
	}

	return cfg, nil
}

// EmbeddedWorkerName is the fixed identity of the manager-node (embedded)
// worker on EVERY edition (SaaS, dev and CE). Platform scripts and the
// API key off this exact name, so an embedded worker never registers under any
// other name, whatever WORKER_NAME, the YAML file or the container hostname say.
const EmbeddedWorkerName = "mod-worker-embedded"

// applyEmbeddedIdentity pins the worker name in embedded mode. Remote and
// datacenter workers keep the operator-chosen name.
func applyEmbeddedIdentity(cfg *Config) {
	if cfg.Mode == ModeEmbedded {
		cfg.WorkerName = EmbeddedWorkerName
	}
}

func defaults() *Config {
	hostname, _ := os.Hostname()
	return &Config{
		WorkerName:        hostname,
		Mode:              ModeRemote, // safest default
		Queues:            nil, // auto-detect from GPU capabilities
		MaxConcurrentJobs: 0, // 0 = unlimited; see main.go concurrency gate
		ContainerTimeout:  30 * time.Minute,
		HeartbeatInterval: 15 * time.Second,
		NoahEnabled:        false,
		NoahSustainedQuiet: 5 * time.Minute,
		NoahYieldPoll:      1 * time.Second,
		NoahPauseGrace:     2 * time.Minute,
		KeycloakRealm:     "modtex",
		KeycloakClient:    "platform-service",
		DockerRegistry:    "example.com",
		DockerNetwork:     "host",
		VaultSecretsPath:  "infrastructure",
		ExecutorBackend:   BackendDocker,
		StackBackend:      BackendDocker,
	}
}

// warnLooseConfigMode logs (path and octal mode only, never content) when the
// config file, which can hold vault_secret_id and bootstrap_token, is
// readable by group or other. Permission bits are not meaningful on Windows.
func warnLooseConfigMode(path string, mode fs.FileMode) {
	if runtime.GOOS == "windows" {
		return
	}
	if mode.Perm()&0o077 != 0 {
		slog.Warn("config file may hold secrets but is accessible to group/other; restrict it (chmod 600)",
			"path", path, "mode", fmt.Sprintf("%#o", uint32(mode.Perm())))
	}
}

func loadYAML(path string, cfg *Config) error {
	if fi, statErr := os.Stat(path); statErr == nil {
		warnLooseConfigMode(path, fi.Mode())
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil // no config file is fine
		}
		return err
	}
	return yaml.Unmarshal(data, cfg)
}

func loadEnv(cfg *Config) {
	if v := os.Getenv("WORKER_NAME"); v != "" {
		cfg.WorkerName = v
	}
	if v := os.Getenv("WORKER_MODE"); v != "" {
		cfg.Mode = Mode(strings.ToLower(v))
	}
	if v := os.Getenv("WORKER_QUEUES"); v != "" {
		cfg.Queues = strings.Split(v, ",")
		for i := range cfg.Queues {
			cfg.Queues[i] = strings.TrimSpace(cfg.Queues[i])
		}
	}
	if v := os.Getenv("MAX_CONCURRENT_JOBS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.MaxConcurrentJobs = n
		} else {
			slog.Warn("ignoring invalid env value; keeping previous setting", "var", "MAX_CONCURRENT_JOBS")
		}
	}
	if v := os.Getenv("CONTAINER_TIMEOUT"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			cfg.ContainerTimeout = d
		} else {
			slog.Warn("ignoring invalid env value (needs a unit, e.g. 30m); keeping previous setting", "var", "CONTAINER_TIMEOUT")
		}
	}

	// Direct mode services
	if v := os.Getenv("REDIS_URL"); v != "" {
		cfg.RedisURL = v
	}
	if v := os.Getenv("KEYCLOAK_URL"); v != "" {
		cfg.KeycloakURL = v
	}
	if v := os.Getenv("KEYCLOAK_INTERNAL_URL"); v != "" {
		cfg.KeycloakURL = v // alias
	}
	if v := os.Getenv("KEYCLOAK_REALM_NAME"); v != "" {
		cfg.KeycloakRealm = v
	}
	if v := os.Getenv("KEYCLOAK_SERVICE_CLIENT_ID"); v != "" {
		cfg.KeycloakClient = v
	}
	if v := os.Getenv("S3_INTERNAL_URL"); v != "" {
		cfg.MinIOEndpoint = v
	} else if v := os.Getenv("MINIO_INTERNAL_URL"); v != "" {
		cfg.MinIOEndpoint = v
	}
	if v := os.Getenv("DOCKER_REGISTRY"); v != "" {
		cfg.DockerRegistry = v
	}
	if v := os.Getenv("LOCAL_REGISTRY"); v != "" {
		cfg.LocalRegistry = v
	}
	if v := os.Getenv("DOCKER_NETWORK"); v != "" {
		cfg.DockerNetwork = v
	}

	// Vault
	if v := os.Getenv("VAULT_ADDR"); v != "" {
		cfg.VaultAddr = v
	}
	if v := os.Getenv("VAULT_APPROLE_ROLE_ID"); v != "" {
		cfg.VaultRoleID = v
	}
	if v := os.Getenv("VAULT_APPROLE_SECRET_ID"); v != "" {
		cfg.VaultSecretID = v
	}

	// API / remote
	if v := os.Getenv("API_BASE_URL"); v != "" {
		cfg.APIURL = v
	}
	if v := os.Getenv("API_URL"); v != "" {
		cfg.APIURL = v
	}
	if v := os.Getenv("BOOTSTRAP_TOKEN"); v != "" {
		cfg.BootstrapToken = v
	}
	if v := os.Getenv("MOD_SITE_ANCHOR_HOST"); v != "" {
		cfg.SiteAnchorHost = v
	}
	if v := os.Getenv("MOD_CLOUD_ANCHOR_HOST"); v != "" {
		cfg.CloudAnchorHost = v
	}

	// NOAH
	if v := os.Getenv("NOAH_ENABLED"); v != "" {
		cfg.NoahEnabled = parseBool(v)
	}
	if v := os.Getenv("NOAH_SUSTAINED_QUIET"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			cfg.NoahSustainedQuiet = d
		}
	}
	if v := os.Getenv("NOAH_YIELD_POLL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			cfg.NoahYieldPoll = d
		}
	}
	if v := os.Getenv("NOAH_PAUSE_GRACE"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			cfg.NoahPauseGrace = d
		}
	}
	if v := os.Getenv("NOAH_PLUGGED_IN_ONLY"); v != "" {
		cfg.NoahPluggedInOnly = parseBool(v)
	}
	if v := os.Getenv("NOAH_SCHEDULE_START"); v != "" {
		cfg.NoahScheduleStart = v
	}
	if v := os.Getenv("NOAH_SCHEDULE_END"); v != "" {
		cfg.NoahScheduleEnd = v
	}

	// k8s-reduction-engine backend selection
	if v := os.Getenv("MOD_EXECUTOR_BACKEND"); v != "" {
		cfg.ExecutorBackend = normalizeBackend(v)
	}
	if v := os.Getenv("MOD_STACK_BACKEND"); v != "" {
		cfg.StackBackend = normalizeBackend(v)
	}
	if v := os.Getenv("KUBECONFIG"); v != "" {
		cfg.KubeconfigPath = v
	}

	// R-161 follow-up: k8s workers are reached by task Pods through the
	// in-cluster {release}-worker Service (MOD_TASK_TOKEN_HOST), so the token
	// endpoint cannot bind the narrow loopback+gateway set that auto mode
	// derives for docker workers. The Helm chart (CORE/deploy/k8s/mod) sets
	// this to "::" by default; docker/compose workers leave it unset and keep
	// the narrow auto bind.
	if v := os.Getenv("MOD_TOKEN_SERVER_BIND"); v != "" {
		cfg.TokenServerBind = v
	}
}

func parseBool(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

// IsDirect returns true if this worker connects directly to infrastructure
// (embedded or datacenter mode), as opposed to going through the API proxy.
func (c *Config) IsDirect() bool {
	return c.Mode == ModeEmbedded
}

// IsRemote returns true for any non-embedded mode (remote or deprecated datacenter).
func (c *Config) IsRemote() bool {
	return c.Mode == ModeRemote || c.Mode == ModeDatacenter
}

// IsK8sExecutorBackend reports whether ephemeral node tasks should be
// submitted as K8s Jobs (T2's K8sBackend) instead of run via Docker
// (T1's DockerBackend). Backend-selection accessor for design.md
// "Config / tiering" — T2 gates its Submit/Poll/Logs/Cancel wiring on this.
func (c *Config) IsK8sExecutorBackend() bool {
	return c.ExecutorBackend == BackendK8s
}

// IsK8sStackBackend reports whether persistent stacks (NIM/VLM/SDXL/etc.)
// should be deployed as K8s Deployment+Service+PVC (T4's K8sStackBackend)
// instead of docker-compose (today's DockerStackBackend).
func (c *Config) IsK8sStackBackend() bool {
	return c.StackBackend == BackendK8s
}

func validate(cfg *Config) error {
	if cfg.WorkerName == "" {
		return fmt.Errorf("worker_name is required")
	}
	if cfg.MaxConcurrentJobs < 0 {
		return fmt.Errorf("max_concurrent_jobs must be >= 0 (0 = unlimited), got %d", cfg.MaxConcurrentJobs)
	}

	switch cfg.Mode {
	case ModeEmbedded:
		// Embedded mode needs direct service addresses + Vault
		if cfg.VaultAddr == "" {
			return fmt.Errorf("vault_addr required for embedded mode")
		}
		if cfg.VaultRoleID == "" || cfg.VaultSecretID == "" {
			return fmt.Errorf("vault_role_id and vault_secret_id required for embedded mode")
		}
		if cfg.RedisURL == "" {
			return fmt.Errorf("redis_url required for embedded mode")
		}

	case ModeRemote, ModeDatacenter:
		// Remote mode (and deprecated datacenter) only needs API access
		if cfg.APIURL == "" {
			return fmt.Errorf("api_url required for %s mode", cfg.Mode)
		}
		if cfg.BootstrapToken == "" {
			return fmt.Errorf("bootstrap_token required for %s mode", cfg.Mode)
		}

	default:
		return fmt.Errorf("unknown mode %q (must be embedded or remote)", cfg.Mode)
	}

	// Queues may be empty here — main.go auto-detects from hardware before use.

	return nil
}
