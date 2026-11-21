package main

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	osExec "os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/capabilities"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/credsrefresher"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/heartbeat"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/mining"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/modelcache"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/monitor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/outbox"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/releases"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/selfupdate"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tlsconf"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/vault"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/wake"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/watcher"
)

// execCommand wraps exec.CommandContext for testability.
var execCommand = osExec.CommandContext

// evalSymlinks wraps filepath.EvalSymlinks for testability.
var evalSymlinks = filepath.EvalSymlinks

var Version = "dev"

// GSEC Q21.8 — graceful restart request for degraded-boot Vault recovery.
//
// The transports (task claims, run-token mint, self-update) captured
// cfg.BootstrapToken by value at boot, so a token obtained after recovery can
// never be applied to them, and writing it into cfg would race with the
// heartbeat's per-beat read of h.cfg.BootstrapToken (heartbeat holds the same
// *config.Config). The recovery goroutine therefore does not mutate cfg: it
// records the reason and triggers the existing graceful-shutdown path (the
// same stop func SIGTERM/SIGINT use), so the job loop drains in-flight tasks
// the normal way before exiting. sync.Once keeps a second recovery event from
// re-triggering a shutdown that is already draining.
var (
	restartOnce   sync.Once
	restartReason atomic.Value
)

// exitCodeRestartForRecovery is non-zero so every supervisor in this repo
// restarts the worker: the systemd units written by scripts/install.sh and
// setup-worker.sh use Restart=always (any exit), and the Windows SCM restart
// actions only fire on a non-graceful stop — an os.Exit from a running service
// counts as one (see startFetchLoopWatchdog's comment). The self-update path's
// exit(0) convention only works on systemd, so the recovery path uses the
// watchdog's non-zero convention instead.
const exitCodeRestartForRecovery = 75

func requestGracefulRestart(stop func(), log *slog.Logger, reason string) {
	restartOnce.Do(func() {
		restartReason.Store(reason)
		log.Info("vault recovered; bootstrap token now available — restarting worker cleanly to apply it")
		stop()
	})
}

// restartExitCode is the job loop's exit decision after draining: the restart
// code when a graceful restart was requested, 0 for an ordinary shutdown.
// Split out from exitAfterDrain so it is unit-testable without os.Exit.
func restartExitCode() int {
	if r, ok := restartReason.Load().(string); ok && r != "" {
		return exitCodeRestartForRecovery
	}
	return 0
}

// exitAfterDrain ends the job loop after wg.Wait(): exits with the restart code
// so the supervisor brings the worker back with the recovered token applied,
// otherwise returns normally (defers still close transport/executor).
func exitAfterDrain(log *slog.Logger) {
	if code := restartExitCode(); code != 0 {
		log.Info("exiting so the supervisor restarts the worker and applies the recovered bootstrap token",
			"exit_code", code, "reason", restartReason.Load())
		os.Exit(code)
	}
	log.Info("all jobs complete, exiting")
}

// loadBootstrapTokenFromVault fetches the embedded worker's bootstrap token
// from Vault (secrets live in Vault, never in env) and stores it on cfg —
// only when no token was already supplied by env/flag, so an explicitly
// configured token always wins. The API seeds the same value at startup and
// hashes it onto a WorkerEnrollment row; the worker sends it as
// X-Bootstrap-Token so its heartbeats authenticate like any other enrolled
// worker.
//
// GSEC Q21.8 — this is the ONE place the bootstrap token is read from Vault
// and stored, and it runs in main() before any goroutine reads cfg.BootstrapToken
// (transports and heartbeat), so the assignment happens-before every read and
// the value is stable for the lifetime of the process. On a sealed-Vault boot
// the read fails and the token stays empty (degraded); the post-recovery boot
// (see requestGracefulRestart) re-runs this and picks the token up. Returns the
// token that was loaded ("" when none).
func loadBootstrapTokenFromVault(vc *vault.Client, cfg *config.Config, log *slog.Logger) string {
	if cfg.BootstrapToken != "" {
		return cfg.BootstrapToken
	}
	secrets, err := vc.GetInfraSecret("embedded-worker")
	if err != nil {
		log.Warn("could not read embedded-worker secret from vault", "error", err)
		return ""
	}
	if tok, ok := secrets["bootstrap_token"]; ok && tok != "" {
		cfg.BootstrapToken = tok
		log.Info("loaded embedded worker bootstrap token from Vault")
		return tok
	}
	return ""
}

// readBootstrapTokenFromVault is the read-only half of loadBootstrapTokenFromVault:
// it fetches the embedded-worker bootstrap token from Vault and returns it
// WITHOUT writing it anywhere. The post-recovery path uses it to CONFIRM the
// token is now available before requesting the clean restart — it must never
// store it into cfg, because the heartbeat reads cfg.BootstrapToken on the
// shared *config.Config from its own goroutine (a write there would be a data
// race), and the transports captured the token by value at boot, so an in-place
// write could never reach them anyway. The clean restart (fresh process) is
// what applies the token to every transport.
func readBootstrapTokenFromVault(vc *vault.Client) string {
	secrets, err := vc.GetInfraSecret("embedded-worker")
	if err != nil {
		return ""
	}
	return secrets["bootstrap_token"]
}

func main() {
	configPath := flag.String("config", "", "path to worker.yaml config file")
	mode := flag.String("mode", "", "override mode: embedded, datacenter, remote")
	apiURL := flag.String("api-url", "", "override API URL")
	token := flag.String("bootstrap-token", "", "override bootstrap token")
	once := flag.Bool("once", false, "run one task then exit (for testing)")
	// NEWER-ONLY override (owner decision 11-11): release publishing is newer-only
	// by default; an operator who really wants to roll every remote worker back
	// passes this explicitly. Never an env var.
	allowDowngrade := flag.Bool("allow-downgrade", false, "publish even when the payload version is OLDER than the manifest (deliberate rollback)")
	showVersion := flag.Bool("version", false, "print version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Printf("mod-worker %s\n", Version)
		os.Exit(0)
	}

	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	// Load config
	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Error("config", "error", err)
		os.Exit(1)
	}

	// CLI overrides
	if *mode != "" {
		cfg.Mode = config.Mode(*mode)
	}
	if *apiURL != "" {
		cfg.APIURL = *apiURL
	}
	if *token != "" {
		cfg.BootstrapToken = *token
	}

	// Detect capabilities
	profile := capabilities.Detect()
	// Auto-detect queues from hardware if not configured
	if len(cfg.Queues) == 0 {
		cfg.Queues = profile.SupportedQueues()
		log.Info("auto-detected queues from hardware", "queues", cfg.Queues)
	}
	if len(cfg.Queues) == 0 {
		log.Error("no queues configured and auto-detection returned none")
		os.Exit(1)
	}

	log.Info("worker starting",
		"name", cfg.WorkerName,
		"mode", cfg.Mode,
		"version", Version,
		"os", profile.OS,
		"arch", profile.Arch,
		"gpu", profile.GPUType,
		"hardware_tags", profile.HardwareTags,
		"ram_gb", fmt.Sprintf("%.1f", profile.RAMGB),
		"queues", cfg.Queues,
	)

	// fortress 2.3: confirm the per-run proof-signing capability works on
	// this build before accepting any task. A throwaway ephemeral key is
	// generated, used to sign+verify a sentinel payload, then zeroed — no
	// state from this self-test outlives this block.
	if selfTestSigner, err := executor.NewRunProofSigner("startup-selftest"); err != nil {
		log.Error("worker proof signer self-test failed", "error", err)
	} else {
		sig, signErr := selfTestSigner.SignContribution(map[string]interface{}{"selftest": true})
		if signErr != nil || !executor.Verify(sig, map[string]interface{}{"selftest": true}) {
			log.Error("worker proof signer self-test failed", "error", signErr)
		} else {
			log.Info("worker proof signer ready", "algorithm", executor.WorkerSignatureAlgorithm)
		}
		selfTestSigner.Zero()
	}

	// Automatic release publication (embedded mode only).
	//
	// The embedded worker always runs on the control server, has admin
	// MinIO creds, and its Docker image carries linux/amd64, linux/arm64,
	// windows/amd64, darwin/amd64 and darwin/arm64 binaries under
	// /releases/ (see Dockerfile). On every startup, check whether the
	// version baked into this container is already advertised in
	// worker-releases/latest.json — if not, upload all binaries + a
	// fresh manifest. This replaces the old manual release.sh workflow:
	// every rebuild of the mod-worker image that boots here is an
	// automatic release. Linux workers pick up the new binary via the
	// normal self-update flow on their next heartbeat; windows/darwin
	// installs pull their (non-self-updating) binary once via the
	// /workers/agent/download token flow.
	//
	// Errors are logged but do NOT block worker startup. A failed publish
	// is retried on the next container restart.
	if cfg.Mode == config.ModeEmbedded {
		binaries := []releases.ReleaseBinary{
			{OS: "linux", Arch: "amd64", Path: "/releases/mod-worker-linux-amd64"},
			{OS: "linux", Arch: "arm64", Path: "/releases/mod-worker-linux-arm64"},
			{OS: "windows", Arch: "amd64", Path: "/releases/mod-worker-windows-amd64.exe"},
			{OS: "darwin", Arch: "amd64", Path: "/releases/mod-worker-darwin-amd64"},
			{OS: "darwin", Arch: "arm64", Path: "/releases/mod-worker-darwin-arm64"},
		}
		// The version of the PAYLOAD, not of this process.
		//
		// `Version` is baked into the running binary — and after a self-update
		// the running binary is newer than /releases/, which still holds what
		// the IMAGE shipped. Publishing the running version against the baked
		// binaries advertises one version and ships another, so a remote worker
		// installs it, reports the older number, sees the newer one advertised,
		// and updates again. For ever, re-downloading every heartbeat cycle.
		// Observed 2026-08-14: worker-example-host went v1.0.42 -> v1.0.39 and sat
		// with update_requested stuck true.
		//
		// Unreadable means DO NOT PUBLISH. A release nobody cut beats one that
		// cannot be satisfied — a manifest's only job is that its version and
		// its bytes agree.
		if payloadVersion, verErr := releases.PayloadVersion("/releases/VERSION"); verErr != nil {
			log.Warn("skipping release publish: cannot read the payload's version",
				"error", verErr)
		} else {
			pubCtx, pubCancel := context.WithTimeout(context.Background(), 60*time.Second)
			// NEWER-ONLY: PublishIfNewer refuses an OLDER version unless the
			// operator passed --allow-downgrade (default off).
			if v, published, err := releases.PublishIfNewer(pubCtx, binaries, payloadVersion, log,
				releases.WithAllowDowngrade(*allowDowngrade)); err != nil {
				log.Warn("release publish failed (non-fatal; will retry on next restart)", "error", err)
			} else if published {
				log.Info("release published", "version", v)
			} else {
				log.Info("release manifest already current", "version", v)
			}
			pubCancel()
		}
	}

	// Resolve secrets from Vault (direct modes only).
	// GSEC Q21.8 — per-worker env creds (VAULT_APPROLE_ROLE_ID/SECRET_ID, the
	// names the install script writes per worker) win over the shared platform
	// role, which stays as the fallback. A login failure no longer exits: the
	// worker boots degraded — API-mediated creds only — and the retry loop
	// (backoff 10s→5m cap, started once the signal ctx exists below) recovers
	// the login in the background.
	var vaultClient *vault.Client
	if cfg.IsDirect() {
		vaultClient = vault.NewWithEnv(cfg.VaultAddr, cfg.VaultRoleID, cfg.VaultSecretID)
		if err := vaultClient.Login(); err != nil {
			log.Warn("vault login failed at boot — running degraded on API-mediated creds", "error", err)
		} else {
			log.Info("authenticated with Vault")
		}

		// Fetch Redis password if URL doesn't already contain one
		if !containsPassword(cfg.RedisURL) {
			secrets, err := vaultClient.GetInfraSecret("redis")
			if err != nil {
				log.Warn("could not read redis secret from vault", "error", err)
			} else if pw, ok := secrets["password"]; ok {
				cfg.RedisURL = injectRedisPassword(cfg.RedisURL, pw)
			}
		}

		// Fetch the embedded worker's bootstrap token from Vault (secrets live in
		// Vault, never in env). The API seeds the same value at startup and hashes
		// it onto a WorkerEnrollment row; the worker sends it as X-Bootstrap-Token
		// so its heartbeats authenticate like any other enrolled worker. On a
		// sealed-Vault boot this fails (token stays empty, degraded); the
		// post-recovery boot re-runs it and picks the token up.
		loadBootstrapTokenFromVault(vaultClient, cfg, log)
	}

	// Set up transport
	var tp transport.Transport
	var rdb *redis.Client

	switch {
	case cfg.IsDirect():
		// Embedded: direct Redis + Keycloak + Vault access
		opts, err := transport.ParseRedisURL(cfg.RedisURL)
		if err != nil {
			log.Error("parse redis URL", "error", err) // fixed text, never the URL
			os.Exit(1)
		}
		rdb = redis.NewClient(opts)
		if rdb != nil {
			executor.SetSizeProbeCache(executor.NewRedisSizeCache(rdb))
			executor.SetSizeProbeLogger(log)
		}

		dt, err := transport.NewDirectTransport(cfg.RedisURL, cfg.WorkerName, cfg.Queues, cfg.APIURL, cfg.BootstrapToken, log)
		if err != nil {
			log.Error("direct transport", "error", err)
			os.Exit(1)
		}
		tp = dt

	default:
		// Remote (and deprecated datacenter): API-only
		tp = transport.NewAPITransport(cfg.APIURL, cfg.WorkerName, cfg.BootstrapToken, cfg.Queues, log)
	}
	defer tp.Close()

	// Set up executor.
	// Harbor registry is intentionally NOT passed in — it is sourced per-task
	// from creds.Harbor.Registry (Vault, via the API) so the registry host
	// always matches the Harbor credentials shipped with the task.
	// APIURL is passed so the executor can rewrite container_env.API_URL to a
	// host the container can actually reach (remote workers can't resolve
	// Docker-internal `api`).
	// The egress proxy image tracks the worker release unless pinned explicitly.
	if os.Getenv("MOD_EGRESS_PROXY_IMAGE") == "" && cfg.LocalRegistry != "" {
		os.Setenv("MOD_EGRESS_PROXY_IMAGE", strings.TrimSuffix(cfg.LocalRegistry, "/")+"/mod-egress-proxy:"+Version)
	}
	exec, err := executor.New(cfg.LocalRegistry, cfg.DockerNetwork, cfg.APIURL, profile.HardwareTags, log)
	if err != nil {
		log.Error("executor", "error", err)
		os.Exit(1)
	}
	defer exec.Close()

	// Worker-controlled stack start (stackloop.go): Docker must not restart
	// user stacks at boot; the worker + API restore them in order instead.
	if executor.StackWorkerStartEnabled() {
		pctx, pcancel := context.WithTimeout(context.Background(), 60*time.Second)
		n, perr := exec.DisableStackAutostart(pctx)
		pcancel()
		if perr != nil {
			log.Warn("stack autostart: restart-policy update failed", "error", perr)
		} else {
			log.Info("stack autostart: restart policy set to no", "containers", n)
		}
	}

	// GS.PSR.7 — worker tunables delivered from the API's platform_settings
	// registry instead of a per-box env recreate. tun is the single
	// process-wide snapshot; wired to the executor now (readers consult it
	// lazily, so it's fine that no heartbeat has landed yet) and to the
	// heartbeat sender below (hb.OnTunables), which replaces the snapshot
	// wholesale on every response.
	tun := tunables.New()
	exec.SetTunables(tun)

	// fortress 3.5 — the output-scan consult must carry the same worker
	// identity headers as every other worker→API call (transport.API transport
	// setHeaders). Read from cfg at call time so Vault-recovery token rotation
	// (GSEC Q21.8) is always honoured.
	executor.SetOutputScanAuth(func(req *http.Request) {
		req.Header.Set("X-Worker-Name", cfg.WorkerName)
		req.Header.Set("X-Bootstrap-Token", cfg.BootstrapToken)
	})
	// 943 (Fortress 3.5 native path): wire the native backend's pre-store
	// consult onto the SAME posture decision the docker path's Run() uses —
	// exec.NativeScanConsult is a thin wrapper around the unexported
	// scanOutputBeforeStore, so there is exactly one implementation of the
	// off/shadow/enforce decision, never a second Go copy for native tasks.
	executor.SetNativeScanConsult(exec.NativeScanConsult)

	initK8sBackends(cfg, exec, log) // k8s-reduction-engine W1: no-op unless MOD_EXECUTOR_BACKEND/MOD_STACK_BACKEND=k8s
	initNativeBackend(tp, log, tun) // launch-completion-megaplan GP.16: execution: native tasks
	// GP.17e: wslc NOAH session lifecycle. Inert unless this host probed
	// wslc available (GP.17c) — with no capability wslcSession stays nil and
	// every wslc hook is a nil-safe no-op, so non-wslc workers are unchanged.
	if profile.Wslc != nil && profile.Wslc.Available {
		wslcScratch := os.TempDir()
		if err := os.MkdirAll("/var/lib/mod-worker", 0o755); err == nil {
			wslcScratch = "/var/lib/mod-worker"
		}
		initWslcSession(true, wslcScratch, log)
	}
	setK8sLogSink(tp) // k8s-reduction-engine W11: live task logs → API

	// worker-mediated-node-credentials (Group 5): stand up the worker's local
	// token endpoint. It vends short-lived run_task tokens to node containers
	// that present a valid per-task nonce, minting them from the API.
	// Best-effort: a bind failure is logged inside Start() and nodes fall back
	// to the transitional API_KEY.
	//
	// The minter is pinned to cfg.APIURL (the internal manager address) via a
	// DEDICATED transport that is never wired to OnManagerRotate. Run-token
	// minting is a LOCAL credential-vending operation for containers this worker
	// launched — it must not follow manager failover onto a peer/external
	// manager URL (e.g. https://example.com). That URL is the same
	// API's public face; inside the worker container it resolves to 127.0.0.1
	// (dead) and offline it is unreachable entirely. Following failover there
	// broke every node's token mint, failing inputs → retry storm → API load →
	// heartbeat timeouts → more failover: a self-reinforcing cascade. Pinning
	// keeps minting on the always-reachable internal URL regardless of failover.
	tokenMinter := transport.NewAPITransport(cfg.APIURL, cfg.WorkerName, cfg.BootstrapToken, cfg.Queues, log)
	defer tokenMinter.Close()
	// R-161: narrow per-backend bind (auto) + per-source-IP rate limiting.
	// The docker gateways give auto mode the bridge IPs node containers
	// actually reach the endpoint on; embedded workers additionally bind
	// their own veth-internal IP (MOD_TASK_TOKEN_HOST = container name).
	// k8s workers' task Pods are remote (in-cluster Service), so their
	// narrow set is impossible — set token_server_bind: "::" in worker
	// config for those (override is logged by Start).
	nodeTokens = executor.NewTokenServer(tokenMinter, log, cfg.TokenServerBind, os.Getenv("MOD_TASK_TOKEN_PORT"), cfg.DockerNetwork)
	if exec != nil {
		nodeTokens.SetDockerGateways(executor.NewDockerGateways(exec.DockerClient()))
		// NODE-RETRY: the executor tells the token server the instant a task
		// creates/observes a posture network it is about to attach to
		// (mod_stack-api-net is created on demand), so the endpoint rebinds that
		// net's gateway IP immediately instead of waiting for the next 5s probe
		// round — without this the first node on that net cannot reach
		// MOD_TASK_TOKEN_URL and exits 1 while its retry succeeds (CE run 19).
		exec.SetPostureNetNotify(nodeTokens.NoteNetwork)
	}
	nodeTokens.SetEmbedded(cfg.Mode == config.ModeEmbedded)
	nodeTokens.Start()
	defer nodeTokens.Close()

	// Signal handling for graceful shutdown
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	// GSEC Q21.8 — degraded-boot recovery. When the boot login failed, retry
	// the Vault login in the background with backoff; on recovery, reload the
	// embedded-worker bootstrap token so heartbeats authenticate like any other
	// enrolled worker (the heartbeat reads cfg.BootstrapToken per beat).
	if vaultClient != nil && vaultClient.Degraded() {
		go func() {
			if err := vaultClient.RetryLogin(ctx); err != nil {
				return
			}
			log.Info("vault login recovered")
			// Do NOT write cfg.BootstrapToken here: the heartbeat reads
			// h.cfg.BootstrapToken on the same *config.Config every beat (data
			// race), and the transports captured the token by value at boot, so
			// a late token could never reach task claims or run-token minting
			// anyway. Confirm the token is now available (read-only), then
			// request a clean restart — the supervisor brings the worker back
			// and the boot path applies the token to every transport. If the
			// token was already present (env/flag), recovery just clears the
			// degraded warning: no restart.
			if cfg.BootstrapToken == "" && readBootstrapTokenFromVault(vaultClient) != "" {
				requestGracefulRestart(stop, log, "bootstrap token recovered from Vault after degraded boot")
			}
		}()
	}

	// Windows-service hook (worker_install.ps1 registers this binary as the
	// "MODWorker" SCM service). runDone is closed once the job loop below
	// has actually returned so the SCM handler can report Stopped instead
	// of guessing; on every other OS, and on an interactive Windows console,
	// this is a no-op and the worker behaves exactly as before. See
	// service_windows.go / service_other.go.
	runDone := make(chan struct{})
	defer close(runDone)
	runMaybeAsService(ctx, stop, runDone)

	// Reclaim any leaked workflow containers from previous sessions and
	// keep up with leaks during long-running sessions. Runs an initial
	// pass now plus every 30 minutes; only touches stopped
	// `ghcr.io/modtim/*` containers — see
	// executor.PruneStaleContainers.
	exec.StartBackgroundJobs(ctx)

	// add-workflow-triggers §6.2 — on-prem filesystem watch trigger source.
	// No-op unless WATCH_PATHS is configured.
	watcher.Start(ctx)

	// Start heartbeat (pass real version so server can detect updates)
	hb := heartbeat.New(cfg, profile, Version, rdb, log)

	// Wire server coordination signals from heartbeat → transport.
	// Interface-based (not concrete-type-based) so embedded + datacenter
	// workers — which use DirectTransport — also receive drain /
	// update-available / server-updating flags. Previously this was a
	// `tp.(*APITransport)` check that silently dropped the signal for
	// DirectTransport, leaving admin-triggered updates in a stuck loop.
	if sr, ok := tp.(transport.ServerStateReceiver); ok {
		hb.OnServerState(sr.SetServerState)
	}

	// BUG.B83 — hand the fetch-loop liveness stamp to the transport so it can
	// refresh it on every completed poll round-trip. The job loop's own
	// MarkFetchLoopIteration calls below cannot cover an idle EMBEDDED worker:
	// DirectTransport.WaitForTask owns the whole idle wait (pub/sub + its own
	// ticker + the API long-poll) and only returns when it has work or a
	// signal, so main()'s loop never spins while the worker polls normally.
	// The watchdog then measured ~11 minutes of "no fetch" on a worker the
	// access log shows polling every ~30s, force-exited it, and orphaned
	// whatever task the API had just handed out. Interface-based like the
	// wiring above so DirectTransport (embedded + datacenter) gets it too.
	// The dedicated run-token minter transport is deliberately NOT wired.
	if fm, ok := tp.(transport.FetchLoopMarkerSetter); ok {
		hb.OnFetchLoopMarker(fm.SetFetchLoopMarker)
	}

	// GS.PSR.7 — apply server-pushed tunables from every heartbeat response.
	hb.OnTunables(tun.Apply)

	// GSEC Q21.7 — report per-task storage-creds refresh failures on the
	// heartbeat warnings field.
	hb.OnCredsWarnings(credsrefresher.Warnings)

	// 1131 — report NVML driver/library version mismatch (reboot required) on
	// the same warnings field.
	hb.OnGPUWarnings(executor.GPUWarnings)

	// GSEC Q21.8 — degraded-boot Vault warning on the same warnings field, and
	// the optional heartbeat `vault` object wired to the two-secret rotation
	// window: a delivered secret_id is validated by login BEFORE the old one is
	// discarded, so a failed delivery leaves the working secret in place.
	if vaultClient != nil {
		hb.OnVaultWarnings(vaultClient.Warning)
		hb.OnVaultDelivery(func(d *heartbeat.VaultDelivery) {
			if d == nil || d.SecretID == "" {
				return
			}
			if vaultClient.ApplySecretID(d.SecretID) {
				log.Info("vault secret_id rotated via heartbeat delivery (two-secret window confirmed)")
			}
		})
	}

	// Megaplan 25.5 — worker self-cleanup: report the latest pass (reclaim
	// gated by the worker.cleanup_reclaim tunable) on every beat, and hand
	// the self-cleanup bridge the live active-task set so it never touches
	// anything this worker is executing.
	hb.OnCleanupSummary(exec.CleanupSummary)
	exec.SetActiveTaskIDsProvider(hb.ActiveTaskIDs)

	// FX.80.p2 — report this worker's recent cross-tenant VRAM scrubs so the
	// manager can see which cards it scrubbed between which tenants (the
	// hosted-cloud isolation record). Omitted until the first scrub, so an
	// on-prem fleet with the gate off sends nothing new.
	hb.OnGpuScrubs(exec.ScrubSummary)

	// FX.91 p5 — report the posture this worker actually observed on the
	// internal-only posture network a gov-undeclared stamped node is parked on.
	// Omitted until the first stamped task lands, so an untouched box sends
	// nothing new. The point is the egress_gap bool: a posture net that exists
	// without Internal:true still has egress and the worker will not recreate it
	// (live containers are attached), so the manager must see it per box.
	hb.OnNetpolStatus(exec.NetpolStatus)

	// 13.5b — report the HONEST file-secrets capability: true for a
	// host-process worker, and for a containerized worker only when its
	// secrets root is a verified tmpfs same-path host bind the docker daemon
	// can see. The API's placement gate trusts this, so a container missing
	// the bind reports false and file-secret stacks are no longer placed on it
	// (they then fail loudly rather than deploy with empty secrets).
	hb.OnFileSecrets(executor.FileSecretsRootAllowed)

	// FX.135 p4a — mining-signature telemetry counter.  Fed from the task
	// loop's monitor samples so the heartbeat reports pre-computed GPU util
	// fraud signals (feeding wired in a follow-up hunk).
	miningC := mining.New(mining.DefaultConfig())
	mining.SetDefault(miningC)
	hb.OnMiningTelemetry(func() *mining.Counter { return miningC })

	// NOAH opportunistic-harvest gate. Withholds claims while a human is using
	// this machine and reports availability up the heartbeat. Ships off; the
	// server enables it live via heartbeat settings.
	noah := newIdleGate(cfg, profile.GPUType, hb, log, exec)
	hb.OnNoahSettings(noah.applySettings)
	// 24.5.s2 — the server-side lend/pause pin lands on the SAME LocalOverride
	// the machine's own tray switch writes (localui POST /noah/mode), so the
	// Dashboard control and the tray are one switch, not two verdicts.
	hb.OnNoahMode(noah.applyServerMode)
	// 24.5.s3 — drain + hand back on user return. The grace window the
	// preempt controller gives in-flight harvest tasks is read LIVE through
	// noah.PauseGraceS, which consults the worker.noah_handback_grace_s
	// platform setting on the shared tunables Store (same GS.PSR.7 delivery
	// as worker.cleanup_reclaim), so the API can retune it without a restart.
	// The gate reads the controller's drain report back (handback) and
	// publishes it up the heartbeat as `noah_handback`: {at, tasks_requeued}.
	// Explicit "pause" (the s2 pin) takes the SAME path: pinnedMode()==pause
	// makes shouldClaim() false immediately AND the next poll publishes
	// unavailable, which fires onYield -> preempt.yieldAll here.
	// graceFn is a closure, not a method value, so a live tunable push takes
	// effect on the NEXT yield rather than being frozen at startup.
	preempt := newPreemptController(exec, func() time.Duration { return noah.PauseGraceS() }, log)
	noah.wire(tun, preempt.handbackReport, func() {
		preempt.yieldAll()
		if wslcSession != nil { // GP.17e: hard-evict wslc tasks + stop session
			wslcSession.OnYield("NOAH: human returned")
		}
	}, preempt.resumeAll)
	go wslcSessionTick(ctx) // no-op unless wslcSession is non-nil

	// GP.8/D-P4: local management web UI (loopback-only, token-gated). Its
	// pause button sets localPause, which the job loop below checks (see
	// "Local pause gate") — separate from NOAH's automatic yield/resume, this
	// is only ever operator-triggered and never auto-clears. Best-effort:
	// startLocalUI logs and returns on any setup failure rather than
	// blocking worker startup (MOD_WORKER_LOCAL_UI_PORT=0 disables it
	// outright).
	localPause := &pauseGate{}
	// MAC.1.s13b (design 4.6): pre-claim free-disk gate state + the
	// worker's scratch/working-set root (same preference order initNative-
	// Backend/initWslcSession and the self-cleanup pass use).
	diskGate := &diskFloorGate{}
	diskRoot := diskWorkRoot()
	hb.OnDiskFloor(func() map[string]interface{} {
		return diskGate.Report(diskRoot)
	})
	workerProv := &workerProvider{
		cfg:     cfg,
		version: Version,
		profile: profile,
		hb:      hb,
		noah:    noah,
		pause:   localPause,
	}
	uiSrv := startLocalUI(ctx, workerProv, log)

	// GP.9/D-P5: native desktop tray (windows/darwin, interactive runs only —
	// no-op on linux, in containers, and under the Windows SCM). Shares the
	// worker's pause gate and local-UI server; "Quit" cancels the same root
	// context as SIGTERM/SIGINT. Best-effort: never blocks or crashes startup.
	startTray(ctx, stop, workerProv, uiSrv, log)

	// 14.6b-2: per-card GPU VRAM budget enforcement. One sampler polls
	// nvidia-smi/rocm-smi every 5s, but only while a GPU task is tracked
	// (vramEnforcer.hasTasks) — a worker with no GPU tasks never shells out.
	// enforceCardBudget evicts the shared task most over its reservation
	// once a card's total usage crosses cardTotal-slack (MOD_WORKER_VRAM_SLACK_MB,
	// default 512). The same sampler's Peak(containerID) fills
	// TaskResult.PeakVRAMMB at task completion (see executeTask).
	vramSampler := executor.NewVRAMSampler()
	vramEnf := newVramEnforcer(vramSampler, preempt, vramSlackMB())
	go vramEnf.run(ctx, 5*time.Second)

	// Hard-cancel: when the server reports (via heartbeat) that one of our
	// tasks was cancelled/failed/expired, kill its container immediately so a
	// user Cancel actually terminates the in-flight run and frees the slot.
	hb.OnCancelTasks(func(taskIDs []int) {
		preempt.evictTasks(taskIDs, "hard-cancel: task cancelled server-side")
	})

	// Wire observed stacks into the heartbeat (stack-ha-reconciliation group 1).
	// Sampled from the Docker daemon on each beat, NOT from any record of what
	// this worker was told to deploy — a stack that deployed successfully and
	// then died must read as absent, which is exactly the case a
	// dispatch-outcome record gets wrong.
	//
	// A failure to sample reports nothing rather than an empty list: "I could
	// not look" and "I looked and there is nothing here" are opposite claims,
	// and treating the first as the second would tell the manager every stack
	// on this host had vanished.
	hb.SetStackLoop(executor.StackWorkerStartEnabled())
	hb.OnHostedStacks(func() []map[string]interface{} {
		obs, err := exec.ObserveStacks(ctx)
		if err != nil {
			log.Warn("stack observation failed; reporting no observation", "error", err)
			return nil
		}
		out := make([]map[string]interface{}, 0, len(obs))
		for _, o := range obs {
			entry := map[string]interface{}{
				"project":  o.Project,
				"running":  o.Running,
				"total":    o.Total,
				"services": o.Services,
			}
			// Only send a measured per-stack VRAM figure. Zero means
			// "unmeasured" (no GPU / could not attribute), and the manager
			// must record NULL, not 0 — omit it so an absent key reads as
			// "no measurement" on the API side.
			if o.VramGB > 0 {
				entry["vram_gb"] = o.VramGB
			}
			// 1900Y1q: tmpfs secrets lost on host/WSL restart -> ask the manager
			// to re-dispatch the deploy (its payload carries fresh secrets).
			// Worker-controlled start: containers that should run but do not.
			// The API answers with one boot_restore at a time (stackloop.go).
			if o.Down >= 0 {
				entry["down"] = o.Down
				entry["down_services"] = o.DownServices
			}
			if executor.SecretRedeployWanted(o.Running, o.Total, executor.StackSecretsMissing(o.Project)) {
				entry["secrets_missing"] = true
			}
			// LLM-gateway scrape (group R 27.5c): raw /workers + /metrics the
			// manager parses server-side. Absent when the project has no gateway
			// or the scrape timed out — same "omit rather than fake" rule as VRAM.
			if o.Gateway != nil {
				entry["gateway"] = o.Gateway
			}
			out = append(out, entry)
		}
		return out
	})

	// Wire in-flight weight downloads. A managed-AI stack on a rung nobody has
	// served here downloads gigabytes before it binds a port, silently, and
	// without this the only available signal is "up but not answering" — which
	// looks identical to hung. Reported every beat so the figure moves.
	// gw-stack-networking GW.2 — the per-protocol set of HOST ports bound on
	// this worker (docker port bindings of every running container) rides on
	// every beat so the manager's placement (GW.3) can treat a requested fixed
	// host port as a resource. Sampled from the daemon with a SHORT timeout:
	// the heartbeat must not stall on a wedged daemon. As with OnHostedStacks,
	// a failure returns nil — "could not look" is reported as absent, never as
	// "nothing bound" (which would tell the manager every port is free).
	hb.OnHostBoundPorts(func() map[string][]int {
		hbCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		return exec.HostBoundPorts(hbCtx)
	})
	// Per-project attribution of the same bindings: lets the manager exclude
	// a redeploying stack's OWN running containers (compose stops them first)
	// from its fixed-port conflict check instead of blocking on itself.
	hb.OnHostBoundPortsByProject(func() map[string]map[string][]int {
		hbCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		return exec.HostBoundPortsByProject(hbCtx)
	})

	hb.OnModelDownloads(func() []map[string]interface{} {
		dl := executor.ScanModelDownloads()
		if dl == nil {
			return nil
		}
		out := make([]map[string]interface{}, 0, len(dl))
		for _, d := range dl {
			out = append(out, map[string]interface{}{
				"repo":  d.Repo,
				"bytes": d.Bytes,
			})
		}
		return out
	})

	hb.OnCachedModels(func() []string {
		return executor.ScanCachedModels()
	})

	// Wire the model-cache manager's latest scores (GF.C2) into the
	// heartbeat, so the manager sees which candidate cache this worker last
	// measured as best without waiting on a download to be in flight.
	hb.OnModelCacheScores(func() []modelcache.Score {
		return exec.ModelCacheScores()
	})

	// Wire self-fencing (stack-ha-reconciliation group 3). Fires when this
	// worker has not reached its manager within the lease window. Nothing
	// instructs it — a partitioned worker cannot BE instructed, which is
	// exactly why it must decide for itself.
	hb.OnFenceStacks(func() ([]string, error) {
		return exec.FenceStacks(ctx)
	})

	// BUG-1028h J3 — the retry-aware fence. A FAILED fence is retried (section
	// 3.7), and the plain fencer above would re-enumerate the whole host set
	// and re-issue `compose stop` for the projects this worker ALREADY stopped
	// earlier in the same episode. fence.go hands this variant that set and
	// prefers it whenever it is wired, so the retry acts only on what is still
	// running. The executor's own enumeration is running-containers-only
	// (§3.8a), so a stopped project never re-appears here anyway — the filter
	// below is belt-and-braces: whatever the executor reports as stopped this
	// attempt, anything already in the episode's stopped set is not reported
	// again, so `fencedStacks` stays exactly this episode's stopped set.
	hb.OnFenceStacksScoped(func(skip []string) ([]string, error) {
		seen := map[string]bool{}
		for _, s := range skip {
			seen[s] = true
		}
		stopped, err := exec.FenceStacks(ctx)
		if stopped == nil {
			return stopped, err
		}
		out := make([]string, 0, len(stopped))
		for _, p := range stopped {
			if !seen[p] {
				out = append(out, p)
			}
		}
		return out, err
	})

	// 2400BUG-1028h J1 — resume fenced stacks IN PLACE on confirmed recontact.
	// The bug: a site network loss fences every stack and nothing comes back
	// — the worker stops its own stacks at lease expiry, the manager's grace
	// expires, the reconciler re-deploys what it thinks belongs here, and the
	// host's own copy stays stopped forever behind a full teardown/redeploy.
	//
	// This callback is NOT free-running: fence.go only ever hands it the
	// projects the manager named in `stack_resume_projects` on the beat that
	// restored contact, intersected with what this worker actually stopped
	// this episode. Placement is the manager's call, never this
	// worker's, so an absent/empty list means no callback at all.
	//
	// The restart itself goes through the SAME compose path the manager uses
	// for a deliberate resume — the executor's `start` op
	// (stackrunner.go `case "start":`), which is `docker compose -p <project>
	// start`: containers, volumes and the compose project survive. It is
	// deliberately NOT `up` (would recreate) and NOT `down`/`teardown`
	// (would destroy the data a partition must not touch).
	hb.OnResumeStacks(func(projects []string) ([]string, error) {
		var started []string
		var failures []string
		for _, p := range projects {
			// A synthetic stack-op task: no task row exists for this restart and
			// none is reported — the manager already decided placement
			// when it named this project, and a redeploy task would re-run the
			// accelerator fit check for a stack that never left this host.
			task := &transport.Task{Payload: transport.TaskPayload{
				StackOp: "start", StackProject: p,
			}}
			if _, err := exec.Run(ctx, task, nil, executor.Callbacks{}); err != nil {
				failures = append(failures, p+": "+err.Error())
				continue
			}
			started = append(started, p)
		}
		if len(failures) > 0 {
			return started, fmt.Errorf("resume incomplete: %s", strings.Join(failures, "; "))
		}
		return started, nil
	})

	// Wire checkpoint reclamation (Gate 3). The worker holds these volumes and
	// can remove them; only the manager knows whether a run still wants one, so
	// it asks. Only the API transport can — an embedded worker shares the
	// manager's database and has no such call — and leaving it unwired means
	// the sweep does nothing, which is the safe default.
	if rt, ok := tp.(interface {
		ReapableCheckpoints(context.Context, []string) ([]string, error)
	}); ok {
		exec.OnReapCheckpoints(rt.ReapableCheckpoints)
	}
	// Worker-controlled stack start: live desired-state check before a
	// boot_restore starts anything (fail closed when not wired).
	if rt, ok := tp.(interface {
		StackState(context.Context, int) (bool, string, error)
	}); ok {
		exec.OnStackState(rt.StackState)
	}

	// Wire manager failover: when the heartbeat rotates to a surviving manager
	// (current one unreachable), repoint the transport too so task-claim/report
	// follow. Both API + Direct transports implement ManagerFailover.
	if mf, ok := tp.(transport.ManagerFailover); ok {
		hb.OnManagerRotate(mf.SetBaseURL)
	}

			// Pre-pull images the server says we'll need. Fires during profiling
	// (new worker join) and whenever pending tasks have uncached images.
	// Pulls run in a background goroutine so they don't block the heartbeat.
	var prepullMu sync.Mutex
	prepulled := make(map[string]bool)
	hb.OnPrepullReady(func(images []string) {
		go func() {
			prepullMu.Lock()
			defer prepullMu.Unlock()
			for _, img := range images {
				if prepulled[img] {
					continue
				}
				log.Info("pre-pulling image", "image", img)
				if err := exec.PullImagePublic(ctx, img); err != nil {
					log.Warn("pre-pull failed (will retry next heartbeat)", "image", img, "error", err)
				} else {
					prepulled[img] = true
					log.Info("pre-pull complete", "image", img)
				}
			}
		}()
	})

	// Undelivered-report queue. A task that finishes while the manager is
	// unreachable would otherwise have its result logged and dropped, and the
	// manager would requeue the whole job — re-running work that had actually
	// completed. Outputs are already in MinIO by this point (uploadSidecar runs
	// before any report), so what is queued here is a few hundred bytes of
	// metadata, not the artifact.
	//
	// MOD_OUTBOX_DIR should point at a persistent mount for results to survive
	// a worker restart as well as a manager one; without that the queue is
	// still correct, just process-lifetime, which already covers the common
	// case of a worker that stays up while the manager restarts.
	outboxDir := os.Getenv("MOD_OUTBOX_DIR")
	if outboxDir == "" {
		outboxDir = "/var/lib/mod-worker/outbox"
	}
	store, err := outbox.New(outboxDir)
	if err != nil {
		// Fall back to a temp dir rather than refusing to start: an
		// in-process queue is still far better than dropping results.
		log.Warn("outbox: cannot use configured directory, falling back to temp",
			"dir", outboxDir, "error", err)
		store, err = outbox.New(filepath.Join(os.TempDir(), "mod-worker-outbox"))
		if err != nil {
			log.Error("outbox: no usable directory", "error", err)
			os.Exit(1)
		}
	}
	reporter = outbox.NewReporter(store, tp, log, 30*time.Second)
	if n := store.Len(); n > 0 {
		log.Warn("outbox: results held from a previous session; will deliver",
			"count", n, "dir", store.Dir())
	}
	go reporter.Run(ctx)

	go hb.Run(ctx)

	// hx.18 — the host-exec seams cannot be wired at boot: this worker learns its
	// numeric worker_id only from the API's first answered heartbeat
	// (heartbeat.WorkerID, item 126b), and wireHostExec fails closed on id == 0.
	// This spawns a goroutine that waits for that first id-carrying beat and then
	// wires exactly once (runner + auth headers + the sandboxed-mode wrapper).
	startHostExecWiring(ctx, cfg, log)

	go noah.run(ctx)

	// Green-but-silent watchdog: heartbeats are sent by a separate goroutine
	// above, so a worker whose job loop below has deadlocked or wedged still
	// looks "online" to the manager forever. This force-exits the process if
	// the job loop stops touching hb.MarkFetchLoopIteration for too long, so
	// the service supervisor (systemd Restart=always / Windows SCM restart
	// actions — see startFetchLoopWatchdog's doc comment) brings it back.
	go startFetchLoopWatchdog(ctx, hb, log)

	// Start disk usage collection loop (runs in background, updates heartbeat snapshot)
	dockerClient := exec.DockerClient()
	go monitor.StartDiskUsageLoop(ctx, dockerClient, 10*time.Minute)

	// Job loop — workers accept all assigned tasks (scheduler controls capacity)
	var activeJobs atomic.Int64
	var wg sync.WaitGroup

	// FX.165.p4 — direct wake (API side: CORE/API/services/worker_direct_wake.py).
	// The API POSTs http://<advertise_address>:<port>/wake (empty body, ~300ms)
	// right after it commits a task ASSIGNED to this worker, so the worker runs
	// its claim pass immediately instead of waiting for the Valkey
	// task:available:<queue> publish or for its long-poll backstop to expire.
	//
	// Default OFF: internal/wake binds nothing until the platform-settings
	// registry delivers worker.direct_wake_enabled on a heartbeat response
	// (the `tun` snapshot wired to hb.OnTunables below), and the port comes
	// from worker.direct_wake_port (API default 8711). The listener binds ONLY
	// this worker's advertise addresses (the ones the heartbeat reports as
	// advertise_address / advertise_address_site / advertise_address_cloud) —
	// never 0.0.0.0 / :: — and refuses a public source IP with 403.
	//
	// Structural assertion: only a transport with the worker's claim-now path
	// (APITransport.TryClaimNow) gets the wake endpoint. An embedded DirectTransport
	// is woken by the Valkey publish on the same host and stays unwired.
	var wl *wake.Listener
	if cl, ok := tp.(wake.WakeClaimer); ok {
		wl = wake.New(log, tun, cfg.APIURL, cfg.SiteAnchorHost, cfg.CloudAnchorHost,
			cl, func() bool {
				maxConc := effectiveConcurrency(hb.MaxConcurrentTasks(), int64(cfg.MaxConcurrentJobs))
				return maxConc > 0 && activeJobs.Load() >= maxConc
			})
		wl.Start(ctx)
	}

	// Poll-error backoff: on a transient poll failure (nginx 502 while the API
	// restarts/deploys, a dropped connection, a decode hiccup) retry with a
	// fast, capped exponential backoff instead of a flat 5s penalty — so the
	// worker resumes claiming within ~pollErrBase of the API coming back, not
	// 5s per attempt. Reset to base after any successful poll.
	pollErrBase := durationFromEnvMs("MOD_WORKER_POLL_ERR_BACKOFF_MS", 250)
	pollErrMax := durationFromEnvMs("MOD_WORKER_POLL_ERR_BACKOFF_MAX_MS", 5000)
	pollErrBackoff := pollErrBase

	log.Info("ready, waiting for tasks")

	for {
		select {
		case <-ctx.Done():
			log.Info("shutdown signal received, draining")
			hb.SetStatus(heartbeat.StatusDraining)
			wg.Wait()
			exitAfterDrain(log)
			return

		default:
			// Fetch-loop-alive beacon (green-but-silent watchdog). Marked at
			// the top of every pass, and again inside each inner gate loop
			// below, so a worker that is legitimately at its concurrency cap
			// or NOAH-withheld keeps touching this and is never confused with
			// one whose job loop has actually deadlocked.
			hb.MarkFetchLoopIteration()

			// Check if server told us to drain (mid-update) and we have no active jobs.
			// Interface assertion so both APITransport (remote) and
			// DirectTransport (embedded + datacenter) reach this code path.
			if su, ok := tp.(transport.SelfUpdateChecker); ok {
				if su.Draining() && activeJobs.Load() == 0 {
					// Drained — check if we should self-update
					if avail, ver := su.UpdateAvailable(); avail && !su.ServerUpdating() {
						// FX.62 p3a1 — the cycle's outcome must reach the manager,
						// because a rolling campaign (services/worker_update_rolling
						// .py, FX.62 p3a2) sequences on exactly two heartbeat signals:
						// `agent_version` reaching the target (the worker upgraded) and
						// hardware_specs['last_update_error'] (it failed). Without the
						// second, a worker whose download/verify/swap failed is
						// indistinguishable from one still in flight, and the campaign
						// sits in `requested` until the health timeout holds it with
						// "no heartbeat reporting vX" — nobody can act on that.
						//
						// The reason is the ACTUAL ceremony failure (HTTP, SHA-256
						// mismatch, signature refusal, failed --version exec, swap
						// EACCES), which is also what the operator needs to see.
						log.Info("update available, self-updating", "current", Version, "new", ver)
						if err := selfUpdate(cfg.APIURL, cfg.BootstrapToken, profile.OS, profile.Arch, log); err != nil {
							// Reported, never fatal: the worker keeps serving on its
							// current binary. The manager holds the rollout and clears
							// this worker's update_requested, so the drain loop stops.
							hb.ReportUpdateError(err.Error())
							log.Error("self-update failed", "error", err)
						} else {
							// The clear is explicit rather than "no key": a completed
							// cycle must replace the manager's stale reading of a
							// previous failure. It is a belt-and-braces beat here —
							// this process exits immediately and the NEW binary starts
							// with nothing to say, which the router also clears on the
							// version change — but a cycle that completes without an
							// exec (a future variant, or a refused-but-not-fatal path)
							// must not leave the old error riding every beat.
							hb.ClearUpdateError()
							log.Info("self-update complete, exiting for restart")
							os.Exit(0) // systemd will restart with new binary
						}
					}
				}
			}

			// Concurrency gate, from two independent limits.
			//
			// Server-pushed: the Heartbeat goroutine pulls
			// `max_concurrent_tasks` from each heartbeat response and stores
			// it atomically; we read it here on every loop iteration so a
			// platform-admin slider change takes effect within one heartbeat
			// tick. 0 = unlimited (matches the API default on
			// `worker_licenses.max_concurrent_tasks`).
			//
			// Local: MAX_CONCURRENT_JOBS / max_concurrent_jobs. This was
			// parsed by the config package and then read by nothing at all,
			// so an operator who set it got no cap and no warning — the
			// docker-compose default of 1 in particular described a limit
			// that did not exist (a live worker was measured running 3 tasks
			// at once under it). It is a machine-local ceiling: the host
			// owner's say over their own box, independent of what the control
			// plane asks for.
			//
			// The stricter of the two wins, and 0 on either side means "no
			// opinion" rather than "zero tasks".
			for {
				hb.MarkFetchLoopIteration()
				maxConc := effectiveConcurrency(hb.MaxConcurrentTasks(),
					int64(cfg.MaxConcurrentJobs))
				if maxConc <= 0 || activeJobs.Load() < maxConc {
					break
				}
				select {
				case <-ctx.Done():
					wg.Wait()
					exitAfterDrain(log)
					return
				case <-time.After(500 * time.Millisecond):
				}
			}

			// Local pause gate (GP.8/D-P4): an operator paused this worker
			// from the local web UI. Unlike the NOAH gate just below, this
			// never diverts to WaitForStackOp — it's a deliberate "stop
			// taking new work" switch a human threw, not an automatic
			// availability reaction, so it blocks everything ordinary claims
			// would reach, full stop, until the same human resumes it. Still
			// marks every spin so the fetch-loop watchdog (below) never
			// mistakes an intentionally-paused worker for a wedged one.
			for localPause.Paused() {
				hb.MarkFetchLoopIteration()
				select {
				case <-ctx.Done():
					wg.Wait()
					exitAfterDrain(log)
					return
				case <-time.After(500 * time.Millisecond):
				}
			}

			// NOAH availability gate: withhold claims while a human is using
			// this machine. A no-op unless NOAH is enabled for this worker.
			//
			// While gated, still poll (WaitForStackOp) for this worker's own
			// persistent-stack-* ops tasks (deploy/stop/logs/migrate). Those
			// are pinned to this host by the scheduler and, per §1 above, are
			// often the ONLY way to relieve the very load NOAH is reacting to
			// (2026-09-29: worker-AlphaBoxEVO-X2 / stack 32 self-deadlock — the
			// worker could never claim the redeploy task for the stack whose
			// own load was blocking every claim, including that one). The API
			// enforces the node_kind restriction server-side (stack_ops_only),
			// so this can never leak an ordinary claim through the gate.
			var gatedTask *transport.Task
			for !noah.shouldClaim() {
				hb.MarkFetchLoopIteration()
				if t, serr := tp.WaitForStackOp(ctx); serr == nil && t != nil {
					gatedTask = t
					break
				}
				select {
				case <-ctx.Done():
					wg.Wait()
					exitAfterDrain(log)
					return
				case <-time.After(500 * time.Millisecond):
				}
			}

			// MAC.1.s13b (design 4.6) free-disk gate: if the scratch/working-
			// set volume is below the platform floor, ordinary claims are
			// withheld — starting a job certain to hit ENOSPACE just burns a
			// claim and a lease. Like the NOAH gate above, this still polls
			// WaitForStackOp: a persistent-stack stop/deploy (or the self-
			// cleanup GC it can trigger) is often the ONLY way to relieve the
			// very disk pressure this gate is reacting to, so leaking those
			// through is the difference between a worker that frees up and
			// one that sits full forever. The API restricts that poll
			// server-side (stack_ops_only), so no ordinary claim leaks here.
			for diskGate.BelowFloor(log, diskRoot, tun) {
				hb.MarkFetchLoopIteration()
				if t, serr := tp.WaitForStackOp(ctx); serr == nil && t != nil {
					gatedTask = t
					break
				}
				select {
				case <-ctx.Done():
					wg.Wait()
					exitAfterDrain(log)
					return
				case <-time.After(5 * time.Second):
				}
			}

			// FX.165.p4 — a task won by a wake-triggered claim pass arrives here,
			// on the same path an ordinary WaitForTask return takes.
			if gatedTask == nil && wl != nil {
				gatedTask = wl.NextClaim()
			}
			task := gatedTask
			var err error
			if task == nil {
				task, err = tp.WaitForTask(ctx)
			}
			if err != nil {
				if ctx.Err() != nil {
					wg.Wait()
					exitAfterDrain(log)
					return
				}
				// FX.123 p3b: the API retired this worker (401 + detail "Worker retired").
				// Act only on consecutive signals (executor.retireConsecutivePolls) so a
				// one-off proxy oddity cannot shred a healthy worker, then destroy the
				// persisted credentials and exit: a retired worker must not keep polling
				// on revoked credentials.
				if executor.RetireSignal(err) {
					if executor.NotePollSignal() {
						rep := executor.RetireWorker(*configPath)
						log.Error("WORKER RETIRED by the API - destroyed this worker's credentials and exiting",
							"scratches_destroyed", rep.ScratchesDestroyed,
							"steps", rep.Steps, "failures", rep.Failures)
						os.Exit(executor.RetireExitCode)
					}
				} else {
					executor.ResetPollSignalStreak()
				}
				log.Error("wait for task", "error", err, "retry_in", pollErrBackoff)
				select {
				case <-ctx.Done():
					wg.Wait()
					exitAfterDrain(log)
					return
				case <-time.After(pollErrBackoff):
				}
				pollErrBackoff *= 2
				if pollErrBackoff > pollErrMax {
					pollErrBackoff = pollErrMax
				}
				continue
			}
			// Successful poll (task or empty queue) — clear the error backoff
			// and mark the fetch loop as having completed a full pass.
			pollErrBackoff = pollErrBase
			hb.MarkFetchLoopIteration()

			if task == nil {
				continue
			}

			wg.Add(1)
			activeJobs.Add(1)
			hb.SetActiveJobs(activeJobs.Load())
			// Claim renewal keys on this. Recorded before the goroutine starts
			// so there is no window where the task is claimed, not yet
			// reported, and therefore reapable.
			hb.AddActiveTask(task.ID)
			if activeJobs.Load() > 0 {
				hb.SetStatus(heartbeat.StatusBusy)
			}

			go func(t *transport.Task) {
				defer wg.Done()
				defer func() {
					n := activeJobs.Add(-1)
					hb.SetActiveJobs(n)
					hb.RemoveActiveTask(t.ID)
					hb.RemoveTaskSnapshot(t.ID)
					if n == 0 {
						hb.SetStatus(heartbeat.StatusIdle)
					}
				}()

				executeTask(ctx, log, cfg, tp, exec, t, hb, preempt, vramEnf, profile.GPUType, profile.HardwareTags, tun)
			}(task)

			if *once {
				wg.Wait()
				log.Info("--once flag set, exiting after single task")
				return
			}
		}
	}
}

// reporter delivers task results, queueing anything the manager cannot be told
// about right now and retrying until it lands. Package-level because both task
// paths report and threading it through two signatures buys nothing — it is
// assigned once in main() before any task can run.
//
// Reports go through this instead of calling tp.Report* directly. A direct call
// that failed used to be logged and dropped, so a task that finished during a
// manager outage was silently redone from scratch on requeue.
var reporter *outbox.Reporter

// nodeTokens is the worker's local run_task-token endpoint
// (worker-mediated-node-credentials, Group 5). Package-level for the same
// reason as reporter: it is a single per-process service, assigned once in
// main() before any task runs, and both task paths consult it to issue/revoke
// per-task nonces. nil only if the worker chose not to stand it up.
var nodeTokens *executor.TokenServer

// executeStackTask handles a persistent-stack deploy/teardown task: report
// started → run the compose-runner → report complete/failed. It skips the normal
// phase-validation / image-pull / download / upload pipeline. (add-persistent-stacks)
func executeStackTask(ctx context.Context, taskLog *slog.Logger, tp transport.Transport,
	exec *executor.Executor, task *transport.Task) {

	taskLog.Info("stack op", "stack_op", task.Payload.StackOp, "project", task.Payload.StackProject)

	if err := tp.ReportStarted(ctx, task.ID, 0); err != nil {
		// A 409 from /started is terminal: the API already finalised the task
		// (cancelled/expired/failed, or the compliance gate refused it), so
		// starting the container would run work the manager said no to — and
		// reporting failed/complete afterwards would overwrite its verdict.
		// Log the refusal once, with only the API's detail string.
		if transport.IsConflict(err) {
			taskLog.Warn("start refused — task not run", "task", task.ID, "status", 409,
				"detail", transport.ConflictDetail(err))
			return
		}
		taskLog.Warn("report started", "error", err)
	}

	cb := executor.Callbacks{
		OnLogBatch: func(lines []string) {
			_ = tp.SendLog(ctx, task.ID, lines)
			// Gate 3 — pull progress out here, where EVERY node's output is
			// visible regardless of how this worker talks home. Parsing the
			// log stream on the manager instead recorded nothing for embedded
			// workers, whose lines go straight to Redis and never reach the
			// API. The lines stay in the log too: a node's stdout is its own,
			// and filtering it would be a surprise.
			if cps := transport.ExtractCheckpoints(lines); len(cps) > 0 {
				_ = tp.SendCheckpoints(ctx, task.ID, cps)
			}
		},
	}
	// Stack deploys pull images through `docker compose`, which only knows the
	// host's own `docker login`. A worker that was never logged into Harbor
	// then fails every private image ("no basic auth credentials") while node
	// tasks on the same box pull fine with the per-task grant. Fetch that same
	// grant here so the stack runner can pre-pull with it. Non-fatal: a host
	// with a stored login, or a spec of public images, still deploys without it.
	var creds *transport.Credentials
	if task.Payload.StackOp == "deploy" {
		c, cerr := tp.GetCredentials(ctx, task.ID)
		if cerr != nil {
			taskLog.Warn("stack deploy: no per-task registry credentials; relying on host docker login", "error", cerr)
		} else {
			creds = c
		}
	}
	result, err := exec.Run(ctx, task, creds, cb)
	if err != nil {
		taskLog.Error("stack op failed", "error", err)
		reporter.Failed(ctx, task.ID, err.Error(), false, "", false, "")
		return
	}

	reporter.Complete(ctx, task.ID, transport.TaskResult{
		Outputs: []transport.OutputFile{},
		LogTail: result.LogTail,
		// 30.6.1 c1-J3d — nil/empty on every op but logical_restore, and transport.
		// TaskResult carries both with omitempty, so the wire body stays byte-identical.
		VerifyTables:    result.VerifyTables,
		VerifyTruncated: result.VerifyTruncated,
	})
	taskLog.Info("stack op complete", "project", task.Payload.StackProject)
}

// executeStorageMigrationTask runs a WS4.Q17.16 BYO-S3 migrate-on-activate
// copy job: pure Go S3-to-S3 streaming copy+verify, no container at all.
//
// Credentials for BOTH buckets come from ONE GetCredentials call — MinIO is
// the destination (the connection being activated), MinIOSource is the OLD
// backend. Progress is reported to the API as it goes (throttled to avoid
// flooding on a manifest with thousands of small objects); the final tally
// is always reported synchronously right before the terminal
// reporter.Complete/Failed call, so the API's completion hook — which
// sanity-checks migrated_object_count against len(payload["objects"]) — sees
// the authoritative count even if an earlier throttled report was dropped.
func executeStorageMigrationTask(ctx context.Context, taskLog *slog.Logger, tp transport.Transport, task *transport.Task) {
	spec := task.Payload.StorageMigration
	taskLog.Info("storage migration", "connection_id", spec.StorageConnectionID,
		"source_bucket", spec.SourceBucket, "dest_bucket", spec.DestBucket, "objects", len(spec.Objects))

	if err := tp.ReportStarted(ctx, task.ID, 0); err != nil {
		// A 409 from /started is terminal: the API already finalised the task
		// (cancelled/expired/failed, or the compliance gate refused it), so
		// starting the container would run work the manager said no to — and
		// reporting failed/complete afterwards would overwrite its verdict.
		// Log the refusal once, with only the API's detail string.
		if transport.IsConflict(err) {
			taskLog.Warn("start refused — task not run", "task", task.ID, "status", 409,
				"detail", transport.ConflictDetail(err))
			return
		}
		taskLog.Warn("report started", "error", err)
	}

	creds, err := tp.GetCredentials(ctx, task.ID)
	if err != nil {
		taskLog.Error("storage migration: credential fetch failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("credential fetch failed: %s", err), true, "", false, "")
		return
	}

	dst, err := executor.NewMinioObjectStore(creds.MinIO)
	if err != nil {
		taskLog.Error("storage migration: dest client build failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("dest client build failed: %s", err), false, "", false, "")
		return
	}
	src, err := executor.NewMinioObjectStore(creds.MinIOSource)
	if err != nil {
		taskLog.Error("storage migration: source client build failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("source client build failed: %s", err), false, "", false, "")
		return
	}

	var lastReport time.Time
	const reportInterval = 3 * time.Second
	onProgress := func(result executor.StorageMigrationResult) {
		now := time.Now()
		if now.Sub(lastReport) < reportInterval {
			return
		}
		lastReport = now
		if perr := tp.ReportStorageMigrationProgress(ctx, task.ID, result.ObjectsDone, result.BytesDone); perr != nil {
			taskLog.Debug("storage migration: progress report failed", "error", perr)
		}
	}

	// FIX-BYO-MIGRATION-SKIP-MISSING — the activation's opt-in flag decides
	// which runner runs. Default (false) is byte-for-byte the old fail-closed
	// path; only a genuine missing SOURCE key is ever skipped, and it is
	// reported, never hidden.
	runner := executor.RunStorageMigration
	if spec.SkipMissingSource {
		runner = executor.RunStorageMigrationSkipMissing
	}
	result, runErr := runner(ctx, src, dst, spec.SourceBucket, spec.DestBucket, spec.Objects, onProgress)

	// Always send the FINAL tally synchronously (bypassing the throttle
	// above) so the API's completion/failure hook sees the authoritative
	// count even if the object list is short enough that every progress
	// call above got throttled away, or the last throttled call landed
	// slightly stale.
	if perr := tp.ReportStorageMigrationProgress(ctx, task.ID, result.ObjectsDone, result.BytesDone); perr != nil {
		taskLog.Warn("storage migration: final progress report failed", "error", perr)
	}

	if runErr != nil {
		taskLog.Error("storage migration failed", "error", runErr,
			"objects_done", result.ObjectsDone, "objects_total", len(spec.Objects))
		reporter.Failed(ctx, task.ID, runErr.Error(), false, "", false, "")
		return
	}

	// FIX-BYO-MIGRATION-SKIP-MISSING — the skip report rides the completion
	// body as the flat "storage_migration_report" field (same idiom as
	// hf_cache_report / lockdown_report) so the API's completion hook can
	// finish a migration that skipped dangling source keys, and the
	// migration-status response can make the skipped count obvious. Omitted
	// (nil) when nothing was skipped or the flag was off: every other
	// completion body stays byte-for-byte unchanged.
	var migReport *transport.StorageMigrationReport
	if result.MissingCount > 0 {
		migReport = &transport.StorageMigrationReport{
			MissingSourceCount: result.MissingCount,
			MissingSourceKeys:  result.MissingSourceKeys,
		}
	}
	taskLog.Info("storage migration complete", "objects_done", result.ObjectsDone,
		"bytes_done", result.BytesDone, "skipped", result.Skipped,
		"missing_source_count", result.MissingCount)
	reporter.Complete(ctx, task.ID, transport.TaskResult{
		Outputs:              []transport.OutputFile{},
		StorageMigrationReport: migReport,
	})
}

// executeStorageMoveOutChunkTask runs ONE CHUNK of a BYO move-out
// (IMPL-BYO-MOVE-OUT-B-GO-WORKER, design s2/s5). Same pure Go S3-to-S3
// streaming copy+verify as the migrate-on-activate job above, no container
// at all, with the move-out rules: the manifest is a bucket LIST (entries
// can lack size/sha for FOREIGN keys the tenant owns but MOD never knew),
// a missing source key is RECORDED and reported, never fatal, an object
// already verified at the destination is skipped (resumable retry), and the
// source is NEVER deleted or truncated (soft-delete rule: clean-up is the
// operator's decision). The per-object tally rides back through the same
// storage-migration-progress endpoint and the same storage_migration_report
// completion field the API's complete_move_out hook reads.
func executeStorageMoveOutChunkTask(ctx context.Context, taskLog *slog.Logger, tp transport.Transport, task *transport.Task) {
	spec := task.Payload.StorageMigration
	if spec == nil {
		// A move-out chunk without the nested spec has nothing to copy: the
		// API's enqueue always stamps it, so this is a wire bug - fail loudly,
		// never silently succeed (the job row would otherwise read verified).
		taskLog.Error("storage move-out: chunk carries no storage_migration spec",
			"move_out_job_id", task.Payload.MoveOutJobID)
		reporter.Failed(ctx, task.ID, "move-out chunk carries no storage_migration spec", false, "", false, "")
		return
	}
	taskLog.Info("storage move-out chunk", "connection_id", task.Payload.MoveOutConnectionID,
		"move_out_job_id", task.Payload.MoveOutJobID,
		"source_bucket", spec.SourceBucket, "dest_bucket", spec.DestBucket, "objects", len(spec.Objects))

	if err := tp.ReportStarted(ctx, task.ID, 0); err != nil {
		// A 409 from /started is terminal (a cancelled move-out lands here):
		// the API already finalised the task, so running it or reporting a
		// verdict would be ours to make. Log the refusal, return.
		if transport.IsConflict(err) {
			taskLog.Warn("start refused - task not run", "task", task.ID, "status", 409,
				"detail", transport.ConflictDetail(err))
			return
		}
		taskLog.Warn("report started", "error", err)
	}

	creds, err := tp.GetCredentials(ctx, task.ID)
	if err != nil {
		taskLog.Error("storage move-out: credential fetch failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("credential fetch failed: %s", err), true, "", false, "")
		return
	}
	dst, err := executor.NewMinioObjectStore(creds.MinIO)
	if err != nil {
		taskLog.Error("storage move-out: dest client build failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("dest client build failed: %s", err), false, "", false, "")
		return
	}
	src, err := executor.NewMinioObjectStore(creds.MinIOSource)
	if err != nil {
		taskLog.Error("storage move-out: source client build failed", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("source client build failed: %s", err), false, "", false, "")
		return
	}

	var lastReport time.Time
	const reportInterval = 3 * time.Second
	onProgress := func(result executor.StorageMigrationResult) {
		now := time.Now()
		if now.Sub(lastReport) < reportInterval {
			return
		}
		lastReport = now
		if perr := tp.ReportStorageMigrationProgress(ctx, task.ID, result.ObjectsDone, result.BytesDone); perr != nil {
			taskLog.Debug("storage move-out: progress report failed", "error", perr)
		}
	}

	// The move-out runner is fixed: resumable skip, missing source recorded
	// (never abort), sha-less entries verified against the source bytes.
	// Credentials are never logged; only bucket NAMES and counts are.
	result, runErr := executor.RunStorageMoveOutChunk(
		ctx, src, dst, spec.SourceBucket, spec.DestBucket, spec.Objects, onProgress)

	// Always send the FINAL tally synchronously so the API's move-status sees
	// the authoritative count even if every throttled report above was dropped.
	if perr := tp.ReportStorageMigrationProgress(ctx, task.ID, result.ObjectsDone, result.BytesDone); perr != nil {
		taskLog.Warn("storage move-out: final progress report failed", "error", perr)
	}

	if runErr != nil {
		taskLog.Error("storage move-out chunk failed", "error", runErr,
			"objects_done", result.ObjectsDone, "objects_total", len(spec.Objects))
		reporter.Failed(ctx, task.ID, runErr.Error(), false, "", false, "")
		return
	}

	// The skip report (keys the source no longer holds) rides the completion
	// body in the same flat storage_migration_report field complete_move_out
	// reads, so the chunk still accounts and the move-status makes the skips
	// obvious. Omitted when nothing was skipped.
	var moveReport *transport.StorageMigrationReport
	if result.MissingCount > 0 {
		// IMPL-BYO-MOVE-OUT-B-GO-WORKER: the move-out skip report is the
		// SAME wire shape as the activate one (missing_source_count +
		// capped keys) - the API's _skip_report already normalizes it.
		moveReport = &transport.StorageMigrationReport{
			MissingSourceCount: result.MissingCount,
			MissingSourceKeys:  result.MissingSourceKeys,
		}
	}
	taskLog.Info("storage move-out chunk complete", "objects_done", result.ObjectsDone,
		"bytes_done", result.BytesDone, "skipped", result.Skipped,
		"missing_source_count", result.MissingCount)
	reporter.Complete(ctx, task.ID, transport.TaskResult{
		Outputs:              []transport.OutputFile{},
		StorageMigrationReport: moveReport,
	})
}

// executeHFCacheRelayoutTask runs the GSEC R-159 one-time per-tenant HF
// weight-cache re-layout natively on the host: per-repo-dir moves decided
// by the API (rename within the same filesystem, never delete, no
// container). No credentials are needed — this touches only local
// directories. The tally rides back as a flat "hf_cache_report" field on
// the completion (TaskResult.HFCacheReport); the API's completion hook
// (services/hf_cache_relayout.py) records it as the per-worker completion
// record the isolation flip gate reads. Failures report retry=false: the
// task is pinned to this worker and a re-trigger via the admin endpoint is
// the intended recovery path (re-runs are idempotent).
func executeHFCacheRelayoutTask(ctx context.Context, taskLog *slog.Logger, tp transport.Transport, task *transport.Task) {
	spec := task.Payload.HFCacheRelayout
	taskLog.Info("hf cache re-layout", "root", spec.Root, "plans", len(spec.Plans))

	if err := tp.ReportStarted(ctx, task.ID, 0); err != nil {
		// A 409 from /started is terminal: the API already finalised the task
		// (cancelled/expired/failed, or the compliance gate refused it), so
		// starting the container would run work the manager said no to — and
		// reporting failed/complete afterwards would overwrite its verdict.
		// Log the refusal once, with only the API's detail string.
		if transport.IsConflict(err) {
			taskLog.Warn("start refused — task not run", "task", task.ID, "status", 409,
				"detail", transport.ConflictDetail(err))
			return
		}
		taskLog.Warn("report started", "error", err)
	}

	result, runErr := executor.RunHFCacheRelayout(ctx, spec)
	if runErr != nil {
		taskLog.Error("hf cache re-layout failed", "error", runErr,
			"moved", result.Moved, "verified", result.Verified, "quarantined", result.Quarantined)
		reporter.Failed(ctx, task.ID, runErr.Error(), false, "", false, "")
		return
	}

	taskLog.Info("hf cache re-layout complete",
		"moved", result.Moved, "verified", result.Verified, "quarantined", result.Quarantined)
	reporter.Complete(ctx, task.ID, transport.TaskResult{
		Outputs: []transport.OutputFile{},
		HFCacheReport: &transport.HFCacheReport{
			Moved:       result.Moved,
			Verified:    result.Verified,
			Quarantined: result.Quarantined,
		},
	})
}

// BUG P2b — phantom VRAM demand on a non-GPU queue (observed live 2026-10-09:
// build task 50645, node build-cpu, queue CPU, claimed by the HP worker and
// never STARTED for 4+ minutes). The scheduler's phase plan carried
// vram_mb=39542 for both container_start and processing — a poisoned
// resource_patterns[system_build_one_variant/CPU/any].avg_peak_vram_mb, corrupt
// because a worker's TaskResourceSample rows report the BOX's GPU state, not the
// task's, and because _get_pattern()'s queue-wide fallback can hand a GPU node's
// profile to a CPU task. The API's validate-phase rejected the plan and reverted
// the claim to PENDING, so executeTask returned BEFORE ReportStarted with no
// report at all: the claim→validate→revert loop ran ~1.4/s (scheduling_version
// 1172→1446) while the row sat CLAIMED with started_at NULL.
//
// A non-GPU queue has no VRAM to reserve, so a rejection whose only complaints
// are VRAM / GPU power-state is a phantom demand: the worker proceeds instead of
// stalling silently. This mirrors routers/worker_tasks.validate_phase's guard so
// the loop dies even when the API is running a build that predates that fix.
func isGPUQueue(queue string) bool {
	switch strings.ToLower(strings.TrimSpace(queue)) {
	case "cuda", "rocm", "intel", "npu", "accelerator", "gpu":
		return true
	}
	return false
}

// phantomVRAMRejection is true when a phase-validation rejection can only be the
// phantom-VRAM artifact above: a known non-GPU queue, and every blocking reason
// (the API joins them with "; ") a VRAM or GPU-power-state complaint. An unknown
// queue name is NOT treated as non-GPU — never widen the exemption on a guess.
func phantomVRAMRejection(queueName, reason string) bool {
	if queueName == "" || isGPUQueue(queueName) {
		return false
	}
	parts := strings.Split(reason, ";")
	seen := false
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		if !strings.HasPrefix(part, "VRAM:") && !strings.HasPrefix(part, "GPU in low_power") {
			return false
		}
		seen = true
	}
	return seen
}

// fortress-4.5 (S) — the worker's own region + storage claims. The API lifts
// string "region"/"storage" keys out of the signed worker_contribution into
// node_run_attestations.attested_region / attested_storage (services/node_run_
// attestations.py _insert_attestation); the 4.4 compliance-tag enforcement
// reads those columns and treats NULL as "not attested" — never "allowed" —
// so an absent claim must stay absent, never become an empty-string claim.
//
// workerResidencyZone mirrors utils/residency.zone_from_tags EXACTLY (its
// docstring: 'region:us' -> 'us', 'region:eu' -> 'eu', anything else ->
// "unknown"): the worker's residency zone is the opt-in 'region:<zone>'
// hardware tag the operator set via WORKER_HARDWARE_TAGS, which
// capabilities/detect.go merges into profile.HardwareTags and the heartbeat
// stores on WorkerLicense.hardware_specs['hardware_tags'] — the SAME tag list
// the API's 4.2/4.4 region attestations key on. First valid tag wins, in tag
// order, like the Python loop. An unknown zone ('region:apac') or no tag
// returns "" — the claim is OMITTED (the column stays NULL = not attested),
// deliberately NOT "unknown": the 4.5 column is the worker's own region label
// matched against the tenant's allowed-region policy, and inventing a zone
// would be a false claim.
func workerResidencyZone(hardwareTags []string) string {
	for _, t := range hardwareTags {
		lt := strings.ToLower(strings.TrimSpace(t))
		if !strings.HasPrefix(lt, "region:") {
			continue
		}
		zone := strings.TrimSpace(strings.TrimPrefix(lt, "region:"))
		// RESIDENCY_ZONES parity (utils/residency.py: ("us", "eu")).
		if zone == "us" || zone == "eu" {
			return zone
		}
	}
	return ""
}

// storageTargetLabel is the storage target the worker writes this task's
// outputs to: the tenant's MinIO bucket (transport.TaskPayload.MinioBucket —
// "MinIO bucket for this tenant"), which is exactly where executor.go's
// upload phase and native_staging.go's PutObject land the bytes. A bucket
// NAME — never the credentials endpoint (access/secret/session material
// travels with it) and never a URL, so no userinfo can leak into a signed,
// indexed column. Empty bucket (a task that stages no tenant files) => "" =>
// the key is omitted and the attestation column stays NULL = not attested.
func storageTargetLabel(task *transport.Task) string {
	if task == nil {
		return ""
	}
	return strings.TrimSpace(task.Payload.MinioBucket)
}

// executeTask runs a single task end-to-end with phase validation and resource monitoring.
//
// Flow with scheduler (phase plan present):
//  1. Wait until start_after (using wait time for pre-pulling)
//  2. For each phase: validate → execute → report samples
//  3. Monitor runs alongside processing phase, sending batched samples
//
// Flow without scheduler (FIFO fallback):
//
//	Original simple flow: report started → get creds → execute → report result
func executeTask(ctx context.Context, log *slog.Logger, cfg *config.Config, tp transport.Transport,
	exec *executor.Executor, task *transport.Task,
	hb *heartbeat.Heartbeat, preempt *preemptController, vramEnf *vramEnforcer, gpuType string, hardwareTags []string,
	tun *tunables.Store) {
	defer preempt.unregister(task.ID)
	defer vramEnf.unregister(task.ID)

	taskLog := log.With("task_id", task.ID, "node_kind", task.Payload.NodeKind, "workflow", task.WorkflowRunID)
	taskLog.Info("executing task")

	// add-persistent-stacks: stack ops (deploy/teardown a docker-compose project)
	// bypass phase validation, image pull, download, and upload — they run the
	// compose-runner directly.
	if task.Payload.StackOp != "" {
		executeStackTask(ctx, taskLog, tp, exec, task)
		return
	}

	// IMPL-BYO-MOVE-OUT-B-GO-WORKER — a BYO move-out CHUNK carries the same
	// nested storage_migration spec, so it must branch BEFORE the
	// migrate-on-activate branch above: it runs the move-out runner
	// (resumable skip, a missing source key recorded not fatal, a sha-less
	// entry verified against the source bytes) instead of the activate
	// runner. Never flips is_active, never deletes a source object.
	if task.Payload.MoveOut {
		executeStorageMoveOutChunkTask(ctx, taskLog, tp, task)
		return
	}

	// WS4.Q17.16 — BYO-S3 migrate-on-activate. Pure Go S3-to-S3 copy+verify,
	// no container/compose path at all: bypasses phase validation, image
	// pull, download, and upload exactly like the StackOp branch above.
	if task.Payload.StorageMigration != nil {
		executeStorageMigrationTask(ctx, taskLog, tp, task)
		return
	}

	// GSEC R-159 — one-time per-tenant HF weight-cache re-layout. Native
	// host moves (rename), no container/compose path, same bypass shape as
	// the StorageMigration branch above.
	if task.Payload.HFCacheRelayout != nil {
		executeHFCacheRelayoutTask(ctx, taskLog, tp, task)
		return
	}

	// If there's a phase plan, validate the first phase before starting
	if task.PhasePlan != nil {
		sysColl := monitor.NewSystemCollector()
		sys := sysColl.Sample()
		gpu := monitor.SampleGPU(gpuType)

		snap := transport.ResourceSnapshot{
			RAMAvailableMB:     sys.RAMAvailableMB,
			VRAMFreeMB:         gpu.VRAMFreeMB,
			VRAMLargestBlockMB: gpu.VRAMFreeMB * (1 - gpu.FragmentationPct),
			CPUIdlePercent:     100.0 - sys.CPUPercent,
			GPUPowerState:      gpu.PowerState,
			GPUTemperatureC:    gpu.TemperatureC,
			DiskFreeGB:         sys.DiskFreeGB,
			NICUtilizationPct:  (sys.NICRxMbps + sys.NICTxMbps) / maxF64(sys.NICCapacityMbps, 1) * 100,
			ActiveContainers:   0,
			ContainersStarting: 0,
			ThermalThrottled:   sys.ThermalThrotl,
		}

		resp, err := tp.ValidatePhase(ctx, task.ID, "processing", snap)
		if err != nil {
			taskLog.Warn("phase validation failed", "error", err)
			// Continue anyway — validation is advisory
		} else if resp != nil && !resp.Proceed {
			// BUG P2b — the silent stall. A rejection on a non-GPU queue whose
			// only complaints are VRAM / GPU power-state is a phantom demand
			// (poisoned pattern, see the helpers above): the API reverts the
			// claim and we return BEFORE ReportStarted, so the row sits CLAIMED
			// with started_at NULL and the loop just burns scheduling_version.
			// Proceed instead — a CPU queue has no VRAM to reserve — and say so.
			if phantomVRAMRejection(task.QueueName, resp.Reason) {
				taskLog.Warn("phase validation rejected on non-GPU queue "+
					task.QueueName+" for VRAM only — proceeding anyway",
					"reason", resp.Reason)
			} else {
				taskLog.Info("phase validation rejected, task will be rescheduled", "reason", resp.Reason)
				return // Task reverts to PENDING on the API side
			}
		}
	}

	// Report started
	estimatedDuration := 5 * time.Minute
	if task.PhasePlan != nil {
		// Use the scheduler's estimate
		estSec := task.PhasePlan.TotalEstDurationS
		if estSec > 0 {
			estimatedDuration = time.Duration(estSec) * time.Second
		}
	}
	if err := tp.ReportStarted(ctx, task.ID, estimatedDuration); err != nil {
		// A 409 from /started is terminal: the API already finalised the task
		// (cancelled/expired/failed, or the compliance gate refused it), so
		// starting the container would run work the manager said no to — and
		// reporting failed/complete afterwards would overwrite its verdict.
		// Log the refusal once, with only the API's detail string.
		if transport.IsConflict(err) {
			taskLog.Warn("start refused — task not run", "task", task.ID, "status", 409,
				"detail", transport.ConflictDetail(err))
			return
		}
		taskLog.Error("report started", "error", err)
	}

	// Get credentials (fetch early so pre-pull can use Harbor creds).
	//
	// Gate 2/6 (remove-minio-versitygw): the API's /workers/tasks/{id}/credentials
	// endpoint is the SINGLE credential source on ALL paths — remote AND
	// embedded/direct. It returns whatever the active storage backend mints
	// (versitygw tenant-scoped creds, or BYO-S3 creds). The worker consumes the
	// bundle verbatim and never cares which backend produced it, so there is no
	// worker-side STS exchange.
	var creds *transport.Credentials
	var err error

	taskLog.Info("using API per-task credentials (single-source /credentials grant)")
	creds, err = tp.GetCredentials(ctx, task.ID)

	if err != nil {
		taskLog.Error("get credentials", "error", err)
		reporter.Failed(ctx, task.ID, fmt.Sprintf("credential fetch failed: %s", err), true, "", false, "")
		return
	}

	// GSEC Q21.7 — per-task storage-creds file + auto-refresh loop. The file
	// is bind-mounted read-only into the task container at the same path by
	// the executor; MINIO_* env (below) stays the bootstrap value so existing
	// images are unchanged. A refresher that cannot start degrades to
	// "no refresh" — it never fails the task. Only storage-bearing tasks
	// (non-empty access key) get a file + loop.
	if creds.MinIO.AccessKey != "" {
		refresher, rerr := credsrefresher.Start(tp, task.ID, &creds.MinIO, credsrefresher.Options{
			Log: taskLog,
			BaseInterval: func() time.Duration {
				return time.Duration(tun.Int("worker.creds_refresh_interval", 300)) * time.Second
			},
		})
		if rerr != nil {
			taskLog.Warn("creds refresher unavailable; task runs on bootstrap creds only",
				"task_id", task.ID, "error", rerr)
		} else {
			taskLog.Info("creds refresher started", "task_id", task.ID, "path", refresher.Path())
			// Stopped (and the creds file deleted) at every task-lifecycle exit.
			defer refresher.Stop()
		}
	}

	// Inject the per-task scoped build token as API_KEY for build-DAG stages.
	// Done via ContainerEnv (not the container's env list directly) so BOTH
	// the env-assembly in the executor AND the netpol main-network decision
	// (which keys off ContainerEnv["API_KEY"]) observe it. Replaces the
	// long-lived platform master key the build stages used to receive.
	// GSEC FX.16 R-023 — the scoped build token is a live API
	// credential, so it is injected only for the build-DAG stages that
	// call back into the API (executor.IsBuildDAGStage, keyed off the
	// STAGE the API's submit puts in container_env). The wizard-generate
	// node has no STAGE and needs no API access — fail-safe: an unknown
	// or missing STAGE gets no token.
	if creds.BuildToken != "" && executor.IsBuildDAGStage(task.Payload.ContainerEnv) {
		if task.Payload.ContainerEnv == nil {
			task.Payload.ContainerEnv = map[string]string{}
		}
		task.Payload.ContainerEnv["API_KEY"] = creds.BuildToken
		taskLog.Info("build-task scoped token injected as API_KEY", "task_id", task.ID)
	} else if creds.BuildToken != "" {
		taskLog.Info("build-task scoped token withheld (not a build-DAG stage)", "task_id", task.ID)
	}

	// worker-mediated-node-credentials (Group 5.1): for any task that gets
	// API/worker reachability — build-DAG stages, or a node the server marked
	// with API_KEY — issue a per-task nonce and point the container at the
	// worker's local token endpoint (MOD_TASK_TOKEN_URL). The node fetches
	// short-lived run_task tokens from there instead of carrying a static
	// long-lived credential; we deliberately do NOT inject a static API_KEY for
	// general tasks. The build_token→API_KEY injection above is left untouched
	// (transitional). The nonce is revoked when this task finishes.
	//
	// Scoped to tasks with an API-access signal so a default, isolated node
	// stays on the zero-egress network — injecting the token URL unconditionally
	// would promote every task to the main network via netpol. The signals are:
	// a build-DAG token; a legacy API_KEY (vector/maintenance nodes, transitional);
	// or MOD_NEEDS_API_TOKEN, which the API-side workflow orchestrator sets on a
	// GENERAL processing node that must call back (resolve cog, report status).
	if nodeTokens != nil {
		_, wantsAPI := task.Payload.ContainerEnv["API_KEY"]
		_, needsToken := task.Payload.ContainerEnv["MOD_NEEDS_API_TOKEN"]
		// Platform-service nodes (e.g. HarborScanFollowup) carry the STATIC
		// platform-service key on purpose and call endpoints that only accept it
		// (require_service_api_key). MOD_STATIC_API_KEY tells us NOT to swap it for
		// a scoped run_task token — doing so made those endpoints 401. API_KEY is
		// still present, so netpol keeps the container on the main network.
		_, staticKey := task.Payload.ContainerEnv["MOD_STATIC_API_KEY"]
		// GSEC FX.16 R-023 — the build-token signal only grants
		// API-reachability for actual build-DAG stages (see the
		// API_KEY gate above); the generate node must not get a
		// token-endpoint nonce it never uses.
		if (wantsAPI || needsToken || (creds.BuildToken != "" && executor.IsBuildDAGStage(task.Payload.ContainerEnv))) && !staticKey {
			nonce, nerr := nodeTokens.IssueNonce(executor.TaskIdentity{
				TaskID:   task.ID,
				TenantID: task.TenantID,
				RunID:    task.WorkflowRunID,
			})
			if nerr != nil {
				taskLog.Warn("could not issue task nonce; node uses transitional API_KEY",
					"task_id", task.ID, "error", nerr)
			} else {
				if task.Payload.ContainerEnv == nil {
					task.Payload.ContainerEnv = map[string]string{}
				}
				task.Payload.ContainerEnv["MOD_TASK_TOKEN_URL"] = nodeTokens.ContainerURL()
				task.Payload.ContainerEnv["MOD_TASK_NONCE"] = nonce
				defer nodeTokens.RevokeNonce(nonce)
				taskLog.Info("per-task node token nonce issued", "task_id", task.ID)
			}
		}
	}

	// Wait for start_after if specified — use wait time for image pre-pull
	if task.StartAfter != nil && time.Until(*task.StartAfter) > 0 {
		waitDur := time.Until(*task.StartAfter)
		taskLog.Info("waiting for start_after, pre-pulling image", "wait", waitDur.Round(time.Second))

		// Pre-pull processing image in background while waiting
		prePullDone := make(chan struct{})
		go func() {
			defer close(prePullDone)
			if _, pullErr := exec.PrePullImage(ctx, task.Payload, creds.Harbor, creds.TenantHarbor); pullErr != nil {
				taskLog.Warn("image pre-pull failed (will retry during execution)", "error", pullErr)
			}
		}()

		select {
		case <-ctx.Done():
			return
		case <-time.After(waitDur):
		}
		// Wait for pre-pull to finish (may already be done)
		select {
		case <-prePullDone:
		case <-time.After(5 * time.Second):
			taskLog.Debug("pre-pull still running, proceeding to execution")
		}
	}

	// Resolve user secrets (if the task has secret_refs)
	if len(task.Payload.SecretRefs) > 0 {
		taskLog.Info("resolving secrets", "count", len(task.Payload.SecretRefs))
		secrets, err := tp.ResolveSecrets(ctx, task.ID)
		if err != nil {
			taskLog.Error("resolve secrets", "error", err)
			reporter.Failed(ctx, task.ID, fmt.Sprintf("secret resolution failed: %s", err), true, "", false, "")
			return
		}
		// Merge resolved secrets into container_env
		if task.Payload.ContainerEnv == nil {
			task.Payload.ContainerEnv = make(map[string]string)
		}
		for envVar, value := range secrets {
			task.Payload.ContainerEnv[envVar] = value
		}
		taskLog.Info("secrets injected", "env_vars", len(secrets))
	}

	// k8s-reduction-engine W1: MOD_EXECUTOR_BACKEND=k8s runs the task as a K8s
	// Job; the docker path below is untouched when unset/docker.
	if shouldRunOnK8s(k8sTaskMode, task.Payload.ExecutorBackend) { // W7: per-task backend gate
		runK8sTask(ctx, taskLog, exec, task, creds)
		return
	}

	// GP.17e: per-task wslc gate (same pattern as k8s) — the API stamps
	// executor_backend="wslc" only for wslc-capable placements; the
	// wslcSession nil-check keeps non-wslc workers on the docker path.
	if wslcSession != nil && isWslcTask(task.Payload.ExecutorBackend) {
		runWslcTask(ctx, taskLog, task, creds)
		return
	}

	// launch-completion-megaplan GP.16 / D-P1: execution: native runs the task
	// as a host process (no container) via the worker's NativeBackend. Never
	// inferred: the API stamps it only for nodes whose custom_metadata declares
	// it. A native task that cannot run here (no recipe, or the worker has no
	// native backend) fails the task loudly — it never silently falls back to
	// docker.
	if shouldRunNativeTask(task.Payload.Execution) {
		runNativeTask(ctx, taskLog, task, creds)
		return
	}

	// Per-task callbacks for executor hooks. Used to be set on the
	// shared `exec` instance via `exec.OnLogBatch = ...`, which raced
	// across concurrent goroutines on the same worker — Task B's
	// reassignment of the field could route Task A's container log
	// lines into Task B's Redis key. Now passed per-call so each
	// task's closure captures its own task.ID with no shared state.
	// See executor.Callbacks for the full incident notes.
	cb := executor.Callbacks{}

	// Start resource monitor (runs alongside execution)
	var mon *monitor.Monitor
	var gpuContainerID string // 14.6b-2: captured for the VRAM-peak lookup at completion, below
	dockerClient := exec.DockerClient()
	if dockerClient != nil {
		mon = monitor.New(task.ID, "", gpuType, dockerClient, taskLog)

		// Wire executor → monitor: when processing container is created,
		// tell the monitor so it can track CPU/RAM via Docker stats API.
		cb.OnContainerCreated = func(containerID string) {
			taskLog.Info("container created, enabling resource monitoring", "container_id", containerID[:12])
			mon.SetContainerID(containerID)
			gpuContainerID = containerID
			vramFn := func() float64 { return mon.LastTaskSnapshot().VRAMMB }
			// Register with the NOAH preemption controller so a returning human
			// can pause or evict this task. VRAM probe classifies pause-vs-evict.
			// A task the API pinned to specific cards (§14.2) also registers its
			// mode/reservation via registerGPU and its card assignment with the
			// VRAM enforcer (14.6b-2), so the per-card budget tick can evict it
			// if it grows past its reservation.
			if ga := task.Payload.GPUAssignment; ga != nil && len(ga.Cards) > 0 {
				preempt.registerGPU(task.ID, containerID, ga.Mode, ga.VRAMLimitMB, vramFn)
				vramEnf.register(task.ID, containerID, ga.Cards)
			} else {
				preempt.register(task.ID, containerID, vramFn)
			}
		}

		// Wire executor → monitor: resource-activity progress signal for the
		// inactivity-liveness check. A container pegging CPU/GPU counts as
		// "making progress" even when it's emitting no logs, so a long silent
		// compute (e.g. photogrammetry) is never killed for "taking too long".
		cb.ResourceActive = func() bool {
			snap := mon.LastTaskSnapshot()
			return snap.CPUPercent > 1.0 || snap.VRAMMB > 50.0
		}

		// Wire executor → transport: forward batched container stdout/stderr
		// to Redis via SendLog so the API's /container-logs endpoint can
		// serve live output to the Dashboard's Logs panel. Errors are
		// logged but swallowed — a Redis blip shouldn't fail the task.
		cb.OnLogBatch = func(lines []string) {
			if err := tp.SendLog(ctx, task.ID, lines); err != nil {
				taskLog.Warn("container-log SendLog failed", "error", err, "lines", len(lines))
			}
			// Gate 3 — extract progress here too. This is the NODE path; the
			// stack-op callback above is a different one, and wiring only that
			// recorded nothing for the tasks that actually matter. Twice now
			// this feature has been broken by treating one call site as the
			// system.
			if cps := transport.ExtractCheckpoints(lines); len(cps) > 0 {
				if err := tp.SendCheckpoints(ctx, task.ID, cps); err != nil {
					taskLog.Warn("checkpoint report failed", "error", err, "count", len(cps))
				}
			}
		}

		// FX.135 p4a1b — feed the mining counter.  A task that declared no GPU
		// workload still shows GPU utilisation on the card; labelling those
		// samples "idle" is what makes the sustained-high-util mining
		// signature visible to the manager (idle_high_util_samples).
		miningPhase := "task"
		if !task.Payload.GPURequired {
			miningPhase = "idle"
		}

		mon.Start(int(estimatedDuration.Seconds()))

		// Periodically drain samples and send to API + update heartbeat snapshot
		stopSampler := make(chan struct{})
		go func() {
			ticker := time.NewTicker(30 * time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-stopSampler:
					return
				case <-ticker.C:
					samples := mon.DrainSamples()
					for _, s := range samples {
						mining.Feed(mining.Sample{
							Timestamp: s.Timestamp,
							GPUUtil:   s.GPUUtilPct,
							Phase:     miningPhase,
						})
					}
					if len(samples) > 0 {
						apiSamples := make([]transport.ResourceSample, len(samples))
						for i, s := range samples {
							apiSamples[i] = transport.ResourceSample{
								Timestamp:            s.Timestamp,
								Phase:                s.Phase,
								CPUPercent:           s.CPUPercent,
								RAMMB:                s.RAMMB,
								VRAMMB:               s.VRAMMB,
								GPUUtilPct:           s.GPUUtilPct,
								GPUTempC:             s.GPUTempC,
								NetRxMbps:            s.NetRxMbps,
								NetTxMbps:            s.NetTxMbps,
								DiskReadIOPS:         s.DiskReadIOPS,
								DiskWriteIOPS:        s.DiskWriteIOPS,
								SingleThreadDetected: s.SingleThreadDetected,
								ContainerDiskReadMB:  s.ContainerDiskReadMB,
								ContainerDiskWriteMB: s.ContainerDiskWriteMB,
								VRAMAllocRateMBs:     s.VRAMAllocRateMBs,
								ProcessCount:         s.ProcessCount,
								GPUClockMHz:          s.GPUClockMHz,
								SwapUsedMB:           s.SwapUsedMB,
							}
						}
						if err := tp.SendResourceSamples(ctx, task.ID, apiSamples); err != nil {
							taskLog.Debug("send resource samples", "error", err)
						}
					}
					// Update heartbeat task snapshot
					hb.UpdateTaskSnapshot(task.ID, mon.LastTaskSnapshot())
				}
			}
		}()
		defer func() {
			close(stopSampler)
			mon.Stop()
			// Send remaining samples
			remaining := mon.AllSamples()
			for _, s := range remaining {
				mining.Feed(mining.Sample{
					Timestamp: s.Timestamp,
					GPUUtil:   s.GPUUtilPct,
					Phase:     miningPhase,
				})
			}
			if len(remaining) > 0 {
				apiSamples := make([]transport.ResourceSample, len(remaining))
				for i, s := range remaining {
					apiSamples[i] = transport.ResourceSample{
						Timestamp:     s.Timestamp,
						Phase:         s.Phase,
						CPUPercent:    s.CPUPercent,
						RAMMB:         s.RAMMB,
						VRAMMB:        s.VRAMMB,
						GPUUtilPct:    s.GPUUtilPct,
						GPUTempC:      s.GPUTempC,
						NetRxMbps:     s.NetRxMbps,
						NetTxMbps:     s.NetTxMbps,
						DiskReadIOPS:  s.DiskReadIOPS,
						DiskWriteIOPS: s.DiskWriteIOPS,
					}
				}
				tp.SendResourceSamples(ctx, task.ID, apiSamples)
			}
		}()
	}

	// fortress 2.3b: the worker's own start observation, so the completion
	// signature can cover when the worker says the work began (the API has
	// no independent way to learn it).
	workerStartedAt := time.Now()

	// Execute the pipeline
	result, err := exec.Run(ctx, task, creds, cb)
	if err != nil {
		taskLog.Error("execution failed", "error", err)
		// Include container logs in the failure report (if available)
		logTail := ""
		oomKilled := false
		errorDetail := ""
		if result != nil {
			logTail = result.LogTail
			oomKilled = result.OOMKilled
			// calibration-health-fixtures-and-timeouts §3 — a sidecar failure
			// (prepare/download/upload) carries its raw stderr here even though
			// the pipeline produced no other outputs. See executor.sidecarErrorResult.
			errorDetail = result.ErrorDetail
		}
		if oomKilled {
			taskLog.Warn("container was OOM-killed by the kernel — reporting to manager",
				"task_id", task.ID)
		}
		// shouldRetry stays true; the MANAGER decides what an OOM means for
		// this task (requeue elsewhere, fail, or record it as calibration data).
		errMsg := err.Error()
		if preempt.wasPreempted(task.ID) {
			taskLog.Info("task yielded to returning user — requeue without retry charge")
			errMsg = "[noah-preempted] " + errMsg
		}
		var egress *transport.EgressReport
		if result != nil {
			egress = result.Egress
		}
		reporter.Failed(ctx, task.ID, errMsg, true, logTail, oomKilled, errorDetail, egress)
		return
	}

	// Report success (ensure Outputs is never nil — API rejects null)
	outputs := result.Outputs
	if outputs == nil {
		outputs = []transport.OutputFile{}
	}
	taskResult := transport.TaskResult{
		Outputs: outputs,
		LogTail: result.LogTail,
		Egress:  result.Egress,
	}
	// WS1.38c: forward the post-clamp ephemeral values the executor actually
	// applied (zero for tasks that declared none — omitempty drops them).
	taskResult.EphemeralAppliedShmMB = result.EphemeralShmMB
	taskResult.EphemeralAppliedTmpfsMB = result.EphemeralTmpfsMB
	taskResult.EphemeralClamped = result.EphemeralClamped
	// 14.6b-2: additive — only set when the sampler actually observed this
	// container (non-GPU tasks and tasks whose container never lived past
	// one sample interval report 0 here and are correctly omitted).
	if gpuContainerID != "" {
		if peak := vramEnf.peak(gpuContainerID); peak > 0 {
			taskResult.PeakVRAMMB = peak
		}
	}
	// GSEC FX.115 p2 — the per-container lockdown verdict the executor
	// produced (node_lockdown.go planMap, threaded through Result.LockdownReport)
	// rides the completion body so the API can aggregate per (node kind, flag)
	// what WOULD change under enforce. nil (key omitted) for paths that ran no
	// node container.
	if result.LockdownReport != nil {
		taskResult.LockdownReport = result.LockdownReport
	}
	// fortress 2.3b — per-task ephemeral signature on completion. A fresh
	// Ed25519 keypair exists ONLY for this task attempt: NewRunProofSigner
	// keyed by "run:node:attempt" (design §2), signs the worker's own
	// contribution (ids, worker name, the image digest it pinned, the
	// output content-hash manifest, and the start/end times only the
	// worker knows), then Zero() runs immediately so the private key never
	// outlives this block. Signing failure NEVER fails the task — the
	// completion is sent unsigned (both wire keys omitted). Nothing here
	// logs key material: the signature struct carries only the public
	// half, which is what the API verifier needs to check the bytes.
	// The contribution rides the body alongside the signature because
	// verify_worker_signature checks the signature against the EXACT
	// payload bytes it covered — the API must not rebuild them.
	if proofSigner, signerErr := executor.NewRunProofSigner(
		fmt.Sprintf("%d:%d:%d", task.WorkflowRunID, task.NodeRunID, task.AttemptCount)); signerErr != nil {
		taskLog.Warn("proof signer unavailable — completing without worker signature", "error", signerErr)
	} else {
		defer proofSigner.Zero()
		manifestParts := []string{}
		for _, o := range outputs {
			manifestParts = append(manifestParts, o.ContentHash+":"+o.FileName)
		}
		execDigest := task.Payload.ImageDigest
		if result != nil && result.ExecutedImageDigest != "" {
			execDigest = result.ExecutedImageDigest
		}
		contribution := map[string]interface{}{
			"task_id":                 int64(task.ID),
			"workflow_run_id":         int64(task.WorkflowRunID),
			"node_run_id":             int64(task.NodeRunID),
			"attempt_count":           int64(task.AttemptCount),
			"worker_name":             cfg.WorkerName,
			"executed_image_digest":   execDigest,
			"outputs_sha256_manifest": strings.Join(manifestParts, "\n"),
			"started_at_utc":          workerStartedAt.UTC().Format(time.RFC3339Nano),
			"completed_at_utc":        time.Now().UTC().Format(time.RFC3339Nano),
		}
		// fortress-4.5 (S) — region + storage ride INSIDE the signed
		// contribution, added BEFORE SignContribution: canonicalPayload sorts
		// keys over the map at call time, so the Ed25519 signature and
		// payload_sha256 cover the new keys, and the API verifier
		// (run_proof_bundle.py verify_worker_signature) canonicalizes the
		// contribution that travels beside the signature with the same
		// sort_keys/compact rules — byte-identical for plain strings, so
		// verify still passes with the keys present (and, because they are
		// absent when unknown, on the old byte-for-byte contribution too).
		// Omit-when-unknown, never empty-string: node_run_attestations.py
		// _insert_attestation maps a missing key to NULL ("not attested"),
		// while "" would be a real claim the 4.4 policy could match.
		// Values are a zone label and a bucket NAME — no endpoint, no
		// credentials, no URL (the API truncates to 128 chars either way).
		if zone := workerResidencyZone(hardwareTags); zone != "" {
			contribution["region"] = zone
		}
		if bucket := storageTargetLabel(task); bucket != "" {
			contribution["storage"] = bucket
		}
		proofSig, signErr := proofSigner.SignContribution(contribution)
		if signErr != nil {
			taskLog.Warn("worker proof signing failed — completing without worker signature", "error", signErr)
		} else if proofSig == nil {
			taskLog.Warn("worker proof signing returned no signature — completing unsigned")
		} else {
			taskResult.WorkerSignature = &transport.WorkerSignature{
				Algorithm:     proofSig.Algorithm,
				KeyID:         proofSig.KeyID,
				PublicKeyB64:  proofSig.PublicKeyB64,
				PayloadSHA256: proofSig.PayloadSHA256,
				SignatureB64:  proofSig.SignatureB64,
				SignedAt:      proofSig.SignedAt,
			}
			taskResult.WorkerContribution = contribution
		}
	}
	reporter.Complete(ctx, task.ID, taskResult)

	taskLog.Info("task completed", "duration", result.Duration.Round(time.Second), "outputs", len(result.Outputs))
}

// ---------------------------------------------------------------------------
// launch-completion-megaplan GP.16 — native execution dispatch.
//
// An execution:"native" task runs as a host process through the worker's
// NativeBackend (internal/executor/native_backend.go) instead of a container:
// inputs are staged from S3 into the host scratch dir by MinIONativeStager,
// the recipe's command runs natively, and the outputs are uploaded back to
// S3. The result is reported through the same outbox reporter the docker
// branch uses. A native task that cannot run here is FAILED loudly — it never
// silently falls back to docker (D-P1: "It is never inferred, and never a
// fallback for a failed container").
//
// Native capability is not a separate flag: every worker binary carries the
// NativeBackend, so "enabled" == "built" (unlike k8s, which needs a cluster
// client). The claim-side capability advertisement is the heartbeat's
// native_apps list (GP.4) plus the per-task requires_app guard in the API's
// claim SQL.

// nativePollInterval bounds how often runNativeTask polls the native run.
const nativePollInterval = 2 * time.Second

// nativeEncryptedScratchKey is the FX.123.s1b worker setting (platform_settings
// registry, delivered on the heartbeat — no new env var) that turns the native
// staging path onto the executor's encrypted per-task scratch. Default false.
const nativeEncryptedScratchKey = "worker.native_encrypted_scratch"

// nativeRunner is the slice of the native backend the dispatch path uses.
// *executor.NativeBackend satisfies it; tests substitute a fake so the
// branch selection can be exercised without spawning host processes.
type nativeRunner interface {
	executor.Backend
	SubmitStaged(ctx context.Context, spec *executor.TaskSpec, task *transport.Task, creds *transport.Credentials) (*executor.Handle, error)
	CollectOutputs(ctx context.Context, h *executor.Handle, task *transport.Task, creds *transport.Credentials) ([]transport.OutputFile, error)
}

var _ nativeRunner = (*executor.NativeBackend)(nil)

// nativeBackend is the worker's native backend, built once in main(). nil
// only if the scratch root could not be created at startup (runNativeTask
// then fails every native task loudly rather than falling back to docker).
var nativeBackend nativeRunner

// shouldRunNativeTask is the per-task routing decision (GP.16). A task runs
// natively only when the API explicitly marked it execution:"native" — the
// same never-inferred, fail-closed posture as shouldRunOnK8s (a non-native
// value, including empty from an older API, runs on docker).
func shouldRunNativeTask(execution string) bool {
	return strings.EqualFold(strings.TrimSpace(execution), "native")
}

// initNativeBackend builds the NativeBackend once at start. Scratch root
// follows the worker's package-standard dir, falling back to the system temp
// (mirrors sandbox.go's /var/lib/mod-worker preference). The production
// stager re-mints task storage credentials through the API transport (GP.5:
// a native render can legitimately outlive its minted creds).
func initNativeBackend(tp transport.Transport, log *slog.Logger, tun *tunables.Store) {
	var scratch string
	for _, dir := range []string{"/var/lib/mod-worker", os.TempDir()} {
		if err := os.MkdirAll(dir, 0o755); err == nil {
			scratch = dir
			break
		}
	}
	if scratch == "" {
		log.Error("native execution unavailable: could not create a scratch dir; execution:native tasks will be FAILED (no docker fallback)")
		return
	}
	stager := &executor.MinIONativeStager{ReMint: tp.GetCredentials}
	nb := executor.NewNativeBackend(filepath.Join(scratch, "native"))
	nb.SetStager(stager)
	// GSEC FX.17 / R-069: the tenant gate and the Linux uid-drop tunables
	// read from the same process-wide snapshot the executor uses.
	nb.SetTunables(tun)
	// FX.123.s1b (launch-completion-megaplan): encrypted per-task scratch.
	// OFF by default — with the platform setting unset, native staging keeps
	// the plaintext host scratch dirs it has always used, byte for byte. When
	// worker.native_encrypted_scratch is pushed true from the platform_settings
	// registry (heartbeat-delivered, same snapshot as the tenant gate), the
	// task's input bytes are sealed AES-256-GCM in memory and only opened into
	// the 0700 task dir just before exec; the run's teardown destroys the
	// scratch (key zeroed, dir removed) on success, failure, cancel and panic.
	if tun != nil && tun.Bool(nativeEncryptedScratchKey, "", false) {
		executor.SetScratchFactory(executor.ScratchFactoryDefault)
		log.Info("native encrypted scratch enabled", "setting", nativeEncryptedScratchKey)
	}
	nativeBackend = nb
	log.Info("native execution enabled", "scratch", scratch, "os", runtime.GOOS)
}

// runNativeTask runs one execution:"native" task end to end:
// recipe check -> TaskSpec -> SubmitStaged (stages inputs) -> Poll ->
// CollectOutputs (uploads) -> reporter Complete/Failed, the same result path
// the docker branch uses. It uses the worker's nativeBackend; the testable
// core is dispatchNativeTask.
func runNativeTask(ctx context.Context, taskLog *slog.Logger, task *transport.Task, creds *transport.Credentials) {
	dispatchNativeTask(ctx, taskLog, nativeBackend, reporter, task, creds)
}

// dispatchNativeTask is the testable native dispatch core. backend may be
// nil (worker has no native backend) which fails the task loudly. rep
// receives the terminal Complete/Failed — the same outbox reporter the docker
// branch uses, so a native result is delivered (and retried) identically.
func dispatchNativeTask(ctx context.Context, taskLog *slog.Logger, backend nativeRunner,
	rep *outbox.Reporter, task *transport.Task, creds *transport.Credentials) {
	fail := func(msg string, retry bool, logTail string) {
		taskLog.Error("native task failed", "error", msg)
		rep.Failed(ctx, task.ID, msg, retry, logTail, false, "")
	}

	if task.Payload.Native == nil || strings.TrimSpace(task.Payload.Native.Command) == "" {
		fail("execution:native task carries no recipe (native.command empty); refusing to run on docker (never a fallback)", true, "")
		return
	}
	if backend == nil {
		fail("execution:native requested but this worker has no native backend (scratch dir unavailable); refusing to run on docker", true, "")
		return
	}
	recipe := task.Payload.Native
	goos := strings.ToLower(runtime.GOOS)
	if recipe.Platform != "" && !strings.EqualFold(recipe.Platform, goos) {
		fail(fmt.Sprintf("native recipe is for platform %q but this worker is %s; refusing to run on docker (never a fallback)", recipe.Platform, goos), true, "")
		return
	}

	env := make(map[string]string, len(task.Payload.ContainerEnv)+len(recipe.Env)+3)
	for k, v := range task.Payload.ContainerEnv {
		env[k] = v
	}
	for k, v := range recipe.Env {
		env[k] = v
	}
	env["MOD_TASK_ID"] = strconv.Itoa(task.ID)
	if creds != nil {
		env["MINIO_ENDPOINT"] = creds.MinIO.Endpoint
	}

	// GSEC FX.17 / R-069: carry the tenant onto the spec so the native
	// backend's tenant gate (refuse tenant code without sandbox unless the
	// worker tunable says otherwise) sees it. 0 = platform/system task.
	spec := executor.WithTaskID(&executor.TaskSpec{
		TenantID:      task.TenantID,
		Cmd:           []string{recipe.Command},
		Env:           env,
		NativeCommand: recipe.Command,
		NativeArgs:    recipe.ArgsTemplate,
		NativeEnv:     recipe.Env,
	}, strconv.Itoa(task.ID))

	h, err := backend.SubmitStaged(ctx, spec, task, creds)
	if err != nil {
		// A tenant-code refusal is a policy decision, not a transient
		// failure — retrying it just re-refuses the same task.
		retry := !errors.Is(err, executor.ErrNativeTenantRefused)
		fail(fmt.Sprintf("native submit: %s", err), retry, "")
		return
	}
	defer func() {
		_ = backend.Cancel(context.Background(), h) // idempotent once finished
	}()
	taskLog.Info("native task submitted", "command", recipe.Command, "platform", goos, "args", len(recipe.ArgsTemplate))
	started := time.Now()

	u, perr := executor.PollUntilDone(ctx, backend, h, nativePollInterval, func(u executor.NodeRunUpdate) {
		if u.Status != executor.NodeRunRunning {
			taskLog.Info("native task status", "status", u.Status, "reason", u.Reason, "exit_code", u.ExitCode)
		}
	})
	if perr != nil {
		msg := fmt.Sprintf("native task aborted: %s", perr)
		fail(msg, true, nativeLogTail(backend, h))
		return
	}
	if u.Status != executor.NodeRunCompleted {
		fail(u.ErrorMessage, u.ShouldRetry, nativeLogTail(backend, h))
		return
	}

	outputs := []transport.OutputFile{}
	if creds != nil {
		cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		outs, oerr := backend.CollectOutputs(cctx, h, task, creds)
		if oerr != nil {
			cancel()
			fail(fmt.Sprintf("native output collection: %s", oerr), true, nativeLogTail(backend, h))
			return
		}
		outputs = outs
	}
	rep.Complete(ctx, task.ID, transport.TaskResult{Outputs: outputs, LogTail: nativeLogTail(backend, h)})
	taskLog.Info("native task completed", "duration", time.Since(started).Round(time.Second), "outputs", len(outputs))
}

// nativeLogTail returns the last lines of a native run's combined
// stdout/stderr, best-effort on its own bounded context (so it still works
// after a timeout/cancel).
func nativeLogTail(backend nativeRunner, h *executor.Handle) string {
	if backend == nil || h == nil {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	rc, err := backend.Logs(ctx, h)
	if err != nil || rc == nil {
		return ""
	}
	defer rc.Close()
	return tailLines(rc, k8sLogTailLines)
}

// selfUpdate downloads the latest worker binary from the API, verifies it, and replaces the current binary.
// The worker should exit after this so systemd restarts it with the new binary.
func selfUpdate(apiURL, bootstrapToken, goos, arch string, log *slog.Logger) error {
	// Get the path of the currently running binary
	execPath, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve executable path: %w", err)
	}
	// Resolve symlinks to get the real path
	execPath, err = resolveRealPath(execPath)
	if err != nil {
		return fmt.Errorf("resolve real path: %w", err)
	}

	// Map Go runtime arch names to our API convention
	dlArch := arch
	switch arch {
	case "amd64", "x86_64":
		dlArch = "amd64"
	case "arm64", "aarch64":
		dlArch = "arm64"
	}
	dlOS := goos

	// API is single-mounted under /v1 only — see internal/transport/api.go's apiPrefix.
	downloadURL := fmt.Sprintf("%s/v1/workers/download/%s/%s", apiURL, dlOS, dlArch)
	log.Info("downloading new binary", "url", downloadURL)

	// Download to temp file next to the current binary
	tmpPath := execPath + ".update"
	req, err := http.NewRequest("GET", downloadURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-Bootstrap-Token", bootstrapToken)

	// X-Bootstrap-Token is a custom header Go does not strip on a cross-host
	// redirect: refuse such redirects (this client also fetches the .sig).
	client := newSelfUpdateHTTPClient()
	// GSEC R-059: the self-update download hits the API — trust the platform
	// internal CA when MOD_INTERNAL_CA_BUNDLE names a readable bundle
	// (unset ⇒ client untouched, byte-identical behaviour).
	tlsconf.SetClientTLS(client)
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("download: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download: HTTP %d", resp.StatusCode)
	}

	tmpFile, err := os.Create(tmpPath)
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	if _, err := io.Copy(tmpFile, resp.Body); err != nil {
		tmpFile.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write temp file: %w", err)
	}
	tmpFile.Close()

	// GSEC R-035: verify the download against the SHA-256 the API published
	// for this (os, arch) BEFORE touching the running binary. Refuse on
	// mismatch or a missing/empty published hash — an unverified download must
	// never replace the current binary.
	if err := verifyDownloadSHA256(tmpPath, resp.Header.Get("X-Content-SHA256")); err != nil {
		os.Remove(tmpPath)
		log.Error("self-update refused: SHA-256 verification failed", "error", err)
		return err
	}

	// GSEC R-035b / FX.61.p3: the hash only proves the bytes match what the
	// API SAYS they are — a hijacked bucket can publish attacker bytes plus a
	// matching hash. The release pipeline signs every artefact with
	// `cosign sign-blob --key` into a sibling <artifact>.sig, and the worker
	// carries the matching public key compiled in (internal/selfupdate), so
	// the signature is checked against bytes + a key the download channel
	// cannot influence. Both the .sig fetch and the verify happen BEFORE the
	// chmod/--version-exec/swap below, so a refused signature leaves the
	// running binary untouched.
	if err := verifyDownloadSignature(downloadURL, tmpPath, client, bootstrapToken, log); err != nil {
		os.Remove(tmpPath)
		return err
	}

	// Make executable
	if err := os.Chmod(tmpPath, 0755); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("chmod: %w", err)
	}

	// Verify the new binary runs (--version should exit 0)
	verifyCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := execCommand(verifyCtx, tmpPath, "--version")
	if out, err := cmd.CombinedOutput(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("verify new binary: %w (output: %s)", err, string(out))
	}

	// Atomic swap: rename new binary over the old one
	if err := os.Rename(tmpPath, execPath); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("atomic swap: %w", err)
	}

	log.Info("binary replaced", "path", execPath)
	return nil
}

// verifyDownloadSHA256 (GSEC R-035) hashes the downloaded file at path and
// compares it, case-insensitively and in constant time, against the expected
// hex digest published in the release manifest. An empty expected digest is a
// refusal, not a pass: without a published hash there is nothing to verify
// against, so the caller must keep the current binary.
func verifyDownloadSHA256(path, expectedHex string) error {
	expectedHex = strings.ToLower(strings.TrimSpace(expectedHex))
	if expectedHex == "" {
		return fmt.Errorf("no published SHA-256 for this release, refusing unverified download")
	}
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open download for hashing: %w", err)
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return fmt.Errorf("hash download: %w", err)
	}
	got := hex.EncodeToString(h.Sum(nil))
	if subtle.ConstantTimeCompare([]byte(got), []byte(expectedHex)) != 1 {
		return fmt.Errorf("SHA-256 mismatch: got %s want %s", got, expectedHex)
	}
	return nil
}

// verifyDownloadSignature (GSEC R-035b / FX.61.p3) is the worker's thin
// wrapper over internal/selfupdate's VerifyDownloadedArtifact: the fetch of
// the sibling <downloadURL>.sig, the read-back of the bytes that actually
// landed in tmpPath, and the check of one against the other under the public
// key compiled into this binary. The package function is where it lives so
// the desktop/plugin update paths (FX.113.p5c: the MSI, the macOS PKG, the
// tray exe) run the SAME check — a signature rule that only exists inside
// selfUpdate is a rule those clients silently don't have. Fail closed in
// every direction: no key compiled in, no .sig beside the artefact, an
// undecodable signature, or a signature that is not this release key's all
// refuse the update, so the caller keeps the current binary.
func verifyDownloadSignature(downloadURL, tmpPath string, client *http.Client, bootstrapToken string, log *slog.Logger) error {
	if !selfupdate.HasReleaseKey() {
		log.Error("self-update refused: no release key compiled in", "detail", "this build carries the placeholder release_pub.pem; refusing an unverified binary")
		return errors.New("no release key compiled in: self-update disabled")
	}

	// The channel's auth header is forwarded verbatim and never consulted for
	// trust: a signature made by some other key verifies nothing no matter
	// what credentials fetched it.
	headers := map[string]string{"X-Bootstrap-Token": bootstrapToken}
	if err := selfupdate.VerifyDownloadedArtifact(downloadURL, tmpPath, client, headers); err != nil {
		log.Error("self-update refused: release signature verification failed", "url", selfupdate.SignatureURL(downloadURL), "error", err)
		return err
	}
	return nil
}

// resolveRealPath resolves symlinks to get the actual file path.
func resolveRealPath(path string) (string, error) {
	resolved, err := evalSymlinks(path)
	if err != nil {
		return path, err
	}
	return resolved, nil
}

func maxF64(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

// containsPassword checks if a Redis URL already has a password.
func containsPassword(redisURL string) bool {
	return len(redisURL) > 0 && (contains(redisURL, "://:") && !contains(redisURL, "://:@"))
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && searchString(s, substr)
}

func searchString(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}

func injectRedisPassword(redisURL, password string) string {
	// redis://redis:6379/2 → redis://:password@redis:6379/2
	if idx := findString(redisURL, "://"); idx >= 0 {
		return redisURL[:idx+3] + ":" + password + "@" + redisURL[idx+3:]
	}
	return redisURL
}

func findString(s, substr string) int {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return i
		}
	}
	return -1
}

// durationFromEnvMs reads a millisecond duration from env, falling back to
// defaultMs when unset, empty, non-numeric, or <= 0.
func durationFromEnvMs(name string, defaultMs int) time.Duration {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Millisecond
		}
	}
	return time.Duration(defaultMs) * time.Millisecond
}

// intFromEnv reads a positive int from env, falling back to defaultVal when
// unset, empty, non-numeric, or <= 0.
func intFromEnv(name string, defaultVal int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return defaultVal
}

// fetchLoopPollInterval mirrors the long-poll timeout WaitForTask asks the
// API for (internal/transport/api.go's pollForTask(ctx, 30, false)) — the
// fetch loop's natural cadence, and the unit the stall watchdog's threshold
// multiplier is expressed in.
const fetchLoopPollInterval = 30 * time.Second

// startFetchLoopWatchdog detects a "green but silent" worker: heartbeats
// (sent by a separate goroutine, hb.Run) keep flowing while the job loop
// below has deadlocked or wedged on a blocking call, so PENDING tasks this
// worker is eligible for sit forever with nothing surfacing it. It polls
// hb.FetchLoopAge() — updated by hb.MarkFetchLoopIteration() at every pass of
// the job loop and its inner gate loops — and force-exits the process if that
// age exceeds the threshold, loudly, so the failure is visible in logs before
// the process disappears.
//
// Threshold: MOD_WORKER_FETCH_STALL_MULTIPLIER (N, default 20) ×
// fetchLoopPollInterval (30s) = 10 minutes by default. An absolute override,
// MOD_WORKER_FETCH_STALL_TIMEOUT_MS, wins over the multiplier when set.
//
// Exit(1) rather than restarting the loop in-process: the job loop is
// main()'s own top-level for{}, not a goroutine spawned off it, so there is
// nothing to restart without unwinding main() — and if the deadlock is on a
// lock shared with the executor, an in-process retry could just re-acquire
// the same stuck lock. A process exit is what this worker's supervisors are
// already built to catch:
//   - Linux: systemd units written by scripts/install.sh and
//     scripts/setup-worker.sh both set `Restart=always` — any exit, 0 or
//     nonzero, restarts the unit. Confirmed in-repo.
//   - Windows: the service created by scripts/install.ps1 runs
//     `sc.exe failure <name> reset= 86400 actions= restart/10000/restart/30000/restart/60000`
//     — the SCM restarts on any non-graceful stop (which an os.Exit from a
//     running service counts as), first after 10s, then 30s, then 60s,
//     resetting the backoff after 24h of health. Confirmed in-repo.
//   - macOS/darwin: this repo has no install script or launchd plist for the
//     worker (only the cross-compiled binary, from `GOOS=darwin go build`).
//     Exit-based recovery is UNCONFIRMED on darwin — whatever supervises the
//     binary there (if anything) needs its own restart policy, or this
//     watchdog will just take the worker offline for good. Flagged in
//     WS4.Q21's report; not fixed here (no darwin service definition exists
//     in this repo to fix).
func startFetchLoopWatchdog(ctx context.Context, hb *heartbeat.Heartbeat, log *slog.Logger) {
	threshold := fetchLoopStallThreshold()
	checkEvery := fetchLoopWatchdogCheckInterval(threshold)
	log.Info("fetch loop watchdog armed", "stall_threshold", threshold, "check_interval", checkEvery)

	ticker := time.NewTicker(checkEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			age := hb.FetchLoopAge()
			if fetchLoopStalled(age, threshold) {
				log.Error("FETCH LOOP STALLED — worker is heartbeating but not fetching tasks (green-but-silent); exiting so the service supervisor restarts it",
					"fetch_loop_age", age.String(), "stall_threshold", threshold.String())
				os.Exit(1)
			}
		}
	}
}

// fetchLoopStallThreshold computes the watchdog's stall threshold: N ×
// fetchLoopPollInterval (MOD_WORKER_FETCH_STALL_MULTIPLIER, default 20 → 10
// minutes), or the absolute MOD_WORKER_FETCH_STALL_TIMEOUT_MS override when
// set. Pure env parsing — split out from startFetchLoopWatchdog so it (and
// fetchLoopStalled below) are unit-testable without a running goroutine.
func fetchLoopStallThreshold() time.Duration {
	n := intFromEnv("MOD_WORKER_FETCH_STALL_MULTIPLIER", 20)
	threshold := time.Duration(n) * fetchLoopPollInterval
	if abs := durationFromEnvMs("MOD_WORKER_FETCH_STALL_TIMEOUT_MS", 0); abs > 0 {
		threshold = abs
	}
	return threshold
}

// fetchLoopWatchdogCheckInterval is how often the watchdog polls
// hb.FetchLoopAge() — a quarter of the threshold, floored at 10s so a very
// small test/override threshold still gets checked promptly.
func fetchLoopWatchdogCheckInterval(threshold time.Duration) time.Duration {
	checkEvery := threshold / 4
	if checkEvery < 10*time.Second {
		checkEvery = 10 * time.Second
	}
	return checkEvery
}

// fetchLoopStalled is the watchdog's actual trip condition, isolated for
// testing: the fetch loop has gone silent longer than threshold.
func fetchLoopStalled(age, threshold time.Duration) bool {
	return threshold > 0 && age > threshold
}

// effectiveConcurrency combines the server-pushed task cap with the worker's
// own MAX_CONCURRENT_JOBS. Either may be 0, meaning "no opinion"; when both
// express one, the stricter wins, because the control plane must not be able
// to push a host past what its owner allowed, and an operator must not be able
// to exceed what their license grants.
func effectiveConcurrency(server, local int64) int64 {
	switch {
	case server <= 0:
		return local
	case local <= 0:
		return server
	case local < server:
		return local
	default:
		return server
	}
}
