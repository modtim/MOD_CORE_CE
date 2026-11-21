package executor

// add-persistent-stacks: the compose-runner. A PLATFORM_TASK whose payload sets
// StackOp != "" deploys or tears down a long-lived docker-compose project on this
// worker's host (via the mounted docker socket), instead of running the normal
// single-container processing pipeline.
//
// The runner shells out to `docker compose`: the CLI reads the compose file
// locally (inside the worker container) and drives the host daemon through the
// mounted /var/run/docker.sock, so the stack's containers come up as siblings on
// the host. The image carries the compose plugin (see Dockerfile).

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"gopkg.in/yaml.v3"
)

// runStackOp handles a persistent-stack deploy/teardown task.
func (e *Executor) runStackOp(ctx context.Context, task *transport.Task, creds *transport.Credentials, started time.Time, cb Callbacks) (*Result, error) {
	p := task.Payload

	// snapshot/restore operate on a docker volume, not a compose project.
	switch p.StackOp {
	case "snapshot":
		// GX.S9 — tarring a LIVE, writing data dir (e.g. a postgres data
		// volume) without a write-freeze can produce a tar that fails to
		// restore cleanly (inconsistent files / in-flight WAL). In-place
		// snapshots of a deployed stack arrive with the project name: PAUSE
		// it first (freezes the containers in place — memory + network +
		// open fds kept, so the freeze is short and recovery is just
		// unpause), fall back to stop/start when the daemon refuses pause
		// (old compose or unsupported runtimes), and ALWAYS thaw afterwards,
		// even when the snapshot itself fails — a half-finished backup must
		// not leave the customer's stack frozen. Migration/backfill
		// snapshots into a fresh target send no project and keep today's
		// bare behaviour (no containers to freeze). Mirrors restore's
		// stop/start write-freeze.
		sproj := strings.TrimSpace(p.StackProject)
		var freezeMethod string // "pause" or "stop" (whichever thaw applies)
		unfreeze := func() {
			if freezeMethod == "" {
				return
			}
			// Thaw with the counterpart of the freeze actually in effect:
			// an unpause on a stopped container is a NO-OP (it resumes
			// processes, it never starts them), so the method must match.
			thaw := "unpause"
			if freezeMethod == "stop" {
				thaw = "start"
			}
			_, err := e.composeCmd(ctx, cb, sproj, "", nil, thaw)
			if err != nil {
				// Thaw failed — retry once with start (works for both a
				// paused and a stopped container). Surface the failure so
				// a half-finished backup doesn't leave the stack frozen
				// silently.
				if sout, serr := e.composeCmd(ctx, cb, sproj, "", nil, "start"); serr != nil {
					cb.OnLogBatch([]string{fmt.Sprintf(
						"snapshot unfreeze: %s FAILED (%v) and fallback start failed (%v) — stack %s left frozen, manual recovery required",
						thaw, err, serr, sproj)})
				} else {
					cb.OnLogBatch([]string{"snapshot unfreeze: " + strings.TrimSpace(sout)})
				}
			}
		}
		defer unfreeze()
		if sproj != "" {
			pout, perr := e.composeCmd(ctx, cb, sproj, "", nil, "pause")
			if perr != nil {
				// Fall back to stop/start when pause is unavailable (old
				// compose plugin or a runtime that refuses pause).
				sout, s2 := e.composeCmd(ctx, cb, sproj, "", nil, "stop")
				if s2 != nil {
					return nil, fmt.Errorf("snapshot: freeze %s failed (pause: %v; %s; stop: %v; %s)",
						sproj, perr, strings.TrimSpace(pout), s2, strings.TrimSpace(sout))
				}
				freezeMethod = "stop"
				if o := strings.TrimSpace(pout + "\n" + strings.TrimSpace(sout)); o != "" {
					cb.OnLogBatch([]string{"snapshot freeze: pause unavailable (" + perr.Error() + "), stopped " + sproj + " instead:\n" + o})
				}
			} else {
				freezeMethod = "pause"
			}
		}
		size, err := e.snapshotVolume(ctx, p.StackVolume, p.SnapshotBucket, p.SnapshotKey)
		if err != nil {
			return nil, fmt.Errorf("snapshot failed: %w", err)
		}
		frozenNote := ""
		if freezeMethod != "" {
			frozenNote = fmt.Sprintf(", frozen via %s", freezeMethod)
		}
		return &Result{
			LogTail:  fmt.Sprintf("snapshot %s -> %s/%s (%d bytes%s)", p.StackVolume, p.SnapshotBucket, p.SnapshotKey, size, frozenNote),
			Duration: time.Since(started),
		}, nil
	case "restore":
		// In-place restore over a LIVE stateful service is a silent data-loss
		// no-op: the container holds the data-dir files open, so untarring the
		// snapshot underneath it has no effect. When the API sends the project
		// (in-place restore of a deployed stack), STOP it, restore, then START.
		// Migration/backfill restore into a fresh target send no project — those
		// keep the bare restore (no container to stop). Mirrors the migrate
		// path's write-freeze.
		rproj := strings.TrimSpace(p.StackProject)
		if rproj != "" {
			if out, err := e.composeCmd(ctx, cb, rproj, "", nil, "stop"); err != nil {
				return nil, fmt.Errorf("restore: stop %s failed: %w (%s)", rproj, err, out)
			}
		}
		if err := e.restoreVolume(ctx, p.StackVolume, p.SnapshotBucket, p.SnapshotKey, p.MaxUncompressedBytes); err != nil {
			return nil, fmt.Errorf("restore failed: %w", err)
		}
		if rproj != "" {
			if out, err := e.composeCmd(ctx, cb, rproj, "", nil, "start"); err != nil {
				return nil, fmt.Errorf("restore: start %s failed: %w (%s)", rproj, err, out)
			}
		}
		return &Result{
			LogTail:  fmt.Sprintf("restored %s from %s/%s (stop/start=%t)", p.StackVolume, p.SnapshotBucket, p.SnapshotKey, rproj != ""),
			Duration: time.Since(started),
		}, nil
	case "rehearse":
		// §9.4 — prove the snapshot restores, without going near the live stack.
		// StackProject is deliberately ignored even if the server sends one: the
		// whole guarantee of a rehearsal is that it cannot stop or overwrite the
		// running service, and that must not depend on the caller getting the
		// payload right.
		files, size, err := e.rehearseSnapshot(ctx, p.StackVolume, p.SnapshotBucket, p.SnapshotKey, p.MaxUncompressedBytes)
		if err != nil {
			return nil, fmt.Errorf("rehearsal failed: %w", err)
		}
		return &Result{
			LogTail: fmt.Sprintf("rehearsal ok: %s restores %d file(s), %d byte(s) from %s/%s",
				p.StackVolume, files, size, p.SnapshotBucket, p.SnapshotKey),
			Duration: time.Since(started),
		}, nil
	case "logical_backup":
		// GA.ASW.5c — the pg_dump/mysqldump/mongodump counterpart of "snapshot":
		// runs the API-built client container against the stack's own DB
		// service instead of tarring a volume. See stacklogical.go.
		size, err := e.logicalBackup(ctx, p)
		if err != nil {
			return nil, fmt.Errorf("logical backup failed: %w", err)
		}
		return &Result{
			LogTail: fmt.Sprintf("logical backup (%s) %s -> %s/%s (%d bytes)",
				p.StackEngine, p.StackService, p.SnapshotBucket, p.SnapshotKey, size),
			Duration: time.Since(started),
		}, nil
	case "logical_restore":
		if err := e.logicalRestore(ctx, p); err != nil {
			return nil, fmt.Errorf("logical restore failed: %w", err)
		}
		// 30.6.1 c1-J3d — the counts run ONLY here, after a clean restore (the
		// call-site contract in stacklogical.go:371-376). Bookkeeping: any error
		// from the count batch is swallowed into "no counts" — the restore already
		// succeeded and must not be failed by its own verification. Nothing about
		// the counts (names, values, argv) is ever logged.
		verified, vtrunc, verr := e.logicalVerifyCounts(ctx, p)
		if verr != nil && cb.OnLogBatch != nil {
			// Fixed, secret-free line: the batch failed (bad argv / bad connection
			// args), which is "no counts", never a failed restore.
			cb.OnLogBatch([]string{"logical restore verified: counts unavailable"})
		}
		restored := &Result{
			LogTail: fmt.Sprintf("logical restore (%s) %s <- %s/%s",
				p.StackEngine, p.StackService, p.SnapshotBucket, p.SnapshotKey),
			Duration: time.Since(started),
		}
		if len(verified) > 0 {
			restored.VerifyTables = verified
			restored.VerifyTruncated = vtrunc
		}
		return restored, nil
	case "probe_empty":
		// 30.6.1 c1-J4 (GX-c1 restore guard) — the read-only emptiness probe the
		// API runs BEFORE it lets an EXTERNAL (seed-committed) restore dispatch
		// anything (routers/persistent_stacks/persistent_stacks.py:_probe_stack_empty).
		// It lives in the FIRST switch on purpose: every case there returns
		// (this one included), so the op never falls through to the project/
		// compose switch below, which is the half that deploys and tears down.
		// See stackprobe.go for the mechanics.
		probeJSON, perr := e.probeStackEmpty(ctx, cb, p)
		if perr != nil {
			// Never a partial/empty answer: the API turns a FAILED probe into
			// 503 stack_probe_unavailable and refuses the restore.
			return nil, fmt.Errorf("probe_empty failed: %w", perr)
		}
		return &Result{
			// The probe payload is the contract's result_payload body
			// ({"volumes":{...},"user_tables":N|null}); it is also the LogTail so
			// the operator can read what the stack actually held.
			LogTail:  probeJSON,
			Duration: time.Since(started),
		}, nil
	case "db_seed_replica", "db_replica_status", "db_replica_promote", "db_source_stop", "db_source_start":
		// GA.ASW.10.6 — streaming zero-downtime DB migration
		// (services/stack_streaming_migration.py). Engine-agnostic: the worker
		// runs whatever argv the API's per-engine spec builder produced;
		// p.StackMode picks which of the two execution shapes the op uses —
		// see stackreplica.go. db_replica_status's stdout carries the lag
		// signal (pg_is_in_recovery()/receive-replay LSNs) the API polls for,
		// so it goes straight into LogTail like every other stack op's output.
		out, err := e.runReplicaOp(ctx, cb, p)
		if err != nil {
			return nil, err
		}
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil
	}

	project := strings.TrimSpace(p.StackProject)
	if project == "" {
		return nil, fmt.Errorf("stack op missing stack_project")
	}

	switch p.StackOp {
	case "deploy":
		if strings.TrimSpace(p.ComposeSpec) == "" {
			return nil, fmt.Errorf("stack deploy missing compose_spec")
		}
		dir, err := os.MkdirTemp("", "modstack-"+sanitizeProject(project)+"-")
		if err != nil {
			return nil, fmt.Errorf("stack workspace: %w", err)
		}
		defer os.RemoveAll(dir)
		composePath := filepath.Join(dir, "docker-compose.yml")
		spec := p.ComposeSpec
		if _, err := os.Stat("/dev/dxg"); err == nil {
			// WSL2 has no /dev/kfd or /dev/dri: rewrite a native-ROCm compose's
			// device passthrough to /dev/dxg before compose up, or the daemon
			// fails on the missing node and the stack never starts.
			var remapped []string
			spec, remapped = wslRemapRocmDevices(spec)
			if len(remapped) > 0 && cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{fmt.Sprintf(
					"WSL2 GPU: remapped ROCm devices /dev/kfd,/dev/dri → /dev/dxg for %s",
					strings.Join(remapped, ", "))})
			}
			lib := findRocdxgLib()
			var bound []string
			spec, bound = wslStackGPUBinds(ctx, spec, lib, e.imageLdLibraryPathFromDocker)
			if len(bound) > 0 && cb.OnLogBatch != nil {
				msg := fmt.Sprintf("WSL2 GPU: mounted host GPU libraries into %s", strings.Join(bound, ", "))
				if lib == "" {
					msg += " (no librocdxg on this host — ROCm will not see the GPU; set MOD_ROCDXG_LIB)"
				} else {
					msg += " (librocdxg: " + lib + ")"
				}
				cb.OnLogBatch([]string{msg})
			}
		}
		// megaplan §14.7 — pin GPU services to the cards the API assigned this
		// deploy task (see stackgpupin.go), same as a node task's GPUAssignment.
		// No-op (spec unchanged) when the task carries no assignment.
		var gpuPinned, vllmPinned, llamaCppSkipped []string
		spec, gpuPinned = pinStackGPUs(spec, p.GPUAssignment, driPathsFromSysfs)
		spec, vllmPinned, llamaCppSkipped = pinVLLMMemUtil(spec, p.GPUAssignment)
		if len(gpuPinned) > 0 && cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf(
				"GPU pin: mode=%s cards=%d -> service(s) %s",
				p.GPUAssignment.Mode, len(p.GPUAssignment.Cards), strings.Join(gpuPinned, ", "))})
		}
		if len(vllmPinned) > 0 && cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf(
				"vLLM: capped --gpu-memory-utilization on service(s) %s", strings.Join(vllmPinned, ", "))})
		}
		if len(llamaCppSkipped) > 0 && cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf(
				"vLLM: llama.cpp service(s) %s have no memory-utilization equivalent — left as-is",
				strings.Join(llamaCppSkipped, ", "))})
		}
		// Give every new compose-managed network a subnet from MOD's pool so an
		// exhausted default pool can never hand a stack a LAN-shadowing
		// 192.168.x block (see stacksubnet.go).
		spec = e.pinStackSubnets(ctx, cb, project, spec)
		// gw-stack-networking GW.2 — host ports are MOD-owned. An
		// `x-mod.host_ports` entry of `any` gets a concrete port from the
		// per-worker range (skipping daemon-bound and stack-reserved ports),
		// and the choice is reported back in the result tail so the API can
		// persist it (see stackhostports.go). A spec without host_ports is
		// left byte-for-byte alone.
		var hostPortAllocs []hostPortMapping
		allocated, hostPortAllocs, allocErr := e.applyStackHostPorts(ctx, project, spec)
		if allocErr != nil {
			return nil, allocErr
		}
		spec = allocated
		e.recordStackHostPorts(project, hostPortAllocs)
		// Offline HF cache priming: materialize the task's weight refs into
		// the local cache and flip the spec to HF_HUB_OFFLINE=1 so the model
		// containers never reach the network for weights. No-op when the
		// task carries no weights. Must happen before the compose file is
		// written (and thus before prePullStackImages / compose up).
		if len(p.Weights) > 0 {
			prepared, err := prepareStackWeights(ctx, &p, spec, creds, func(msgs ...string) {
				if cb.OnLogBatch != nil {
					cb.OnLogBatch(msgs)
				}
			})
			if err != nil {
				return nil, err
			}
			spec = prepared
		}
		// GF.C2 — point every stack service's HF client at the best measured
		// model cache, same rule as the node-container path (applyModelCacheEnv):
		// a service that already pins its own HF_ENDPOINT is left alone, and a
		// resolved endpoint of "" (local cache or upstream direct) still adds
		// HF_HUB_DISABLE_XET=1 and no HF_ENDPOINT. Never fails the deploy for
		// an injection error — it logs and the compose ships as it was — EXCEPT
		// the sealed refusal (GY.C8): shipping that compose would let the
		// stack's HF client reach the public upstream, so the deploy fails
		// with the offending egress settings named.
		// GF.C6.5 — any service declaring an HF_TOKEN marks this deploy as
		// token-gated (never routed through another site's cache), and the
		// tenant-token swap applies per service (see
		// applyModelCacheEnvToCompose / composeDeclaresHFToken). creds is
		// nil in test/legacy paths — that means "no minted token", which
		// leaves every HF_TOKEN untouched.
		_stkMct := ""
		if creds != nil {
			_stkMct = creds.ModelCacheToken
		}
		_stkGated := composeDeclaresHFToken(spec)
		if withCache, err := e.applyModelCacheEnvToCompose(ctx, spec, taskModelRepoID(p), _stkMct, _stkGated); err != nil {
			if errors.Is(err, ErrModelCacheSealed) {
				return nil, err
			}
			if cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{fmt.Sprintf("model-cache env injection skipped: %v", err)})
			}
		} else {
			spec = withCache
		}
		// B86i — a co-located MOD API (the host's /etc/hosts maps the API
		// hostname to 127.0.0.1, and that entry leaks through docker DNS into
		// every stack container) made the app dial ITSELF: every files/secrets
		// call was ECONNREFUSED (stack 114, modstack114g0-api-1, ExtraHosts
		// []). Inject <api-host>:host-gateway into every service so docker
		// re-maps it to the host's routable gateway IP. No-op when the API host
		// is routable, when the author already pinned that hostname, or when
		// the spec carries no services.
		spec = e.injectStackAPIHost(ctx, cb, e.apiURL, spec)
		if err := os.WriteFile(composePath, []byte(spec), 0o600); err != nil {
			return nil, fmt.Errorf("write compose file: %w", err)
		}
		// 13.5b — fail closed, BEFORE materializing any secret bytes, when this
		// worker cannot be VERIFIED to hand its secrets to the daemon. A
		// containerized worker's default /dev/shm is private to the container,
		// so the daemon would silently serve EMPTY secret files (probe-verified
		// on WSL2): a silent empty secret is worse than a loud 500 — the app
		// "works" with a blank credential and every auth call 401s with no hint
		// in any log. The guard permits a containerized worker only when its
		// secrets root is a tmpfs same-path host bind (fileSecretsRootStatus);
		// a host-process worker always passes. Running this check first also
		// guarantees secret bytes are never written to a non-tmpfs mount on a
		// worker whose bind we could not verify.
		if len(p.SecretFiles) > 0 && !fileSecretsRootStatus().Allowed {
			cleanupStackFiles(project)
			return nil, fileSecretsDeployError()
		}
		// GX.S10b — materialize the payload's file secrets and storage files
		// BEFORE compose up, and export MOD_SECRETS_DIR / MOD_FILES_DIR to the
		// compose process so ${...} interpolates. secretValues feed the redaction
		// list below so a secret value can never reach a log line.
		var deployExtraEnv, deploySecrets []string
		if secretsDir, filesDir, sv, merr := materializeStackFiles(ctx, project, p); merr != nil {
			return nil, merr
		} else {
			deployExtraEnv = stackEnvForCompose(secretsDir, filesDir)
			deploySecrets = sv
		}
		// delegated-stack-authoring §5.4 — refuse a spec that smuggles a service
		// onto the reserved platform network, defeating the API's egress rewrite.
		// Only active when the server marks the task enforced (ships dark). Runs
		// before ensureExternalNetworks / compose up so a bypass never starts.
		if p.EgressEnforced {
			if err := refuseEgressBypass(p.ComposeSpec, platformNetworkName()); err != nil {
				return nil, err
			}
		}
		// A stack's compose declares the platform network as `external: true`,
		// which is correct ON THE MANAGER — nginx and the API live there and
		// reach the stack by alias. A REMOTE worker has no such network, and
		// compose refuses to start rather than creating one:
		//
		//     network mod_modtex-network declared as external, but could not be found
		//
		// The image pulls, the model never runs. That made every stack
		// undeployable off-manager, and stayed invisible for as long as every
		// stack happened to land on the manager.
		//
		// Create what is missing instead of rewriting the spec: on the manager
		// this is a no-op because the network already exists, and on a remote
		// host the stack gets a local network of the same name, so aliases keep
		// working for anything sharing that host. Reaching ACROSS hosts is a
		// different problem and belongs to the service-mesh work — this only
		// ensures a stack can start at all.
		if created := e.ensureExternalNetworks(ctx, p.ComposeSpec); len(created) > 0 && cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf(
				"created missing external network(s): %s", strings.Join(created, ", "))})
		}
		// Clear anything an interrupted download left in the shared model cache.
		// Without this a killed deploy poisons that model permanently: the next
		// container starts, publishes its port, and blocks on the stale lock for
		// ever with nothing in its log. See sweepStaleDownloads.
		if swept := e.sweepStaleDownloads(p.ComposeSpec); len(swept) > 0 && cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf(
				"cleared %d stale download artefact(s) from the model cache", len(swept))})
		}
		// Mark every cached model as just-used if this stack shares the model
		// cache. Feeds evictModelCache.go's LRU ordering (megaplan 25.5/Q.3):
		// without this, a stack that has been running for days but never
		// redeployed looks idle to the GC and its weights get evicted out
		// from under it.
		e.touchModelCacheForStack(p.ComposeSpec)
		// A container that carries NO com.docker.compose.project label is not
		// compose's — compose will never adopt or replace it, so `up` dies with
		// a bare "Conflict. The container name ... is already in use" and every
		// later revision fails identically forever (stack 140). Name it, refuse,
		// and stop BEFORE burning image pulls. Read-only: MOD never removes it.
		if err := e.preflightOrphanNames(ctx, cb, project, p.ComposeSpec); err != nil {
			return nil, err
		}
		if err := e.prePullStackImages(ctx, p.ComposeSpec, creds, cb); err != nil {
			return nil, err
		}
		out, err := e.composeUpWithRetry(ctx, cb, project, composePath, deploySecrets, deployExtraEnv, false)
		if err != nil && isNvidiaPersistencedSocketErr(out, err) {
			// 2026-10-12 - GPU2 stack 111 and HP both failed compose-up with
			// "open /run/nvidia-persistenced/socket: no such file". Two causes
			// look identical from compose's side but need very different
			// handling: unattended-upgrades can bump the nvidia userspace
			// package while the old kernel module is still loaded, which
			// nvidia-smi reports as an NVML "Driver/library version mismatch" -
			// that only clears on reboot, so retrying persistenced is useless
			// and just burns time. A merely-stopped persistenced daemon, by
			// contrast, is a one-line restart. Ask nvidia-smi which case this
			// is before doing anything else. These NVML/systemctl probes get
			// their own short timeout independent of ctx: a driver/library
			// mismatch is exactly the condition under which nvidia-smi is
			// known to hang rather than error out, and ctx itself may carry
			// no deadline (or a very long one meant for compose up).
			smiCtx, smiCancel := context.WithTimeout(ctx, nvmlProbeTimeout)
			smiOut, _ := execCommandContext(smiCtx, "nvidia-smi").CombinedOutput()
			smiCancel()
			if isNVMLDriverMismatch(string(smiOut)) {
				if cb.OnLogBatch != nil {
					cb.OnLogBatch([]string{fmt.Sprintf(
						"nvidia-persistenced socket missing for %s and nvidia-smi reports an NVML driver/library version mismatch - reboot required, not retrying", project)})
				}
				setGPUDriverMismatchWarning("gpu_driver_mismatch: reboot required")
				return nil, fmt.Errorf("compose up failed: %w\n%s", err, out)
			}
			clearGPUDriverMismatchWarning()
			if cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{fmt.Sprintf(
					"nvidia-persistenced socket missing for %s and NVML looks healthy - attempting to start persistenced", project)})
			}
			restartCtx, restartCancel := context.WithTimeout(ctx, nvmlProbeTimeout)
			restartOut, restartErr := execCommandContext(restartCtx, "systemctl", "start", "nvidia-persistenced").CombinedOutput()
			restartCancel()
			if restartErr != nil {
				if cb.OnLogBatch != nil {
					cb.OnLogBatch([]string{fmt.Sprintf(
						"systemctl start nvidia-persistenced failed (%v): %s - falling back to nvidia-smi -pm 1",
						restartErr, strings.TrimSpace(string(restartOut)))})
				}
				pmCtx, pmCancel := context.WithTimeout(ctx, nvmlProbeTimeout)
				pmOut, pmErr := execCommandContext(pmCtx, "nvidia-smi", "-pm", "1").CombinedOutput()
				pmCancel()
				if cb.OnLogBatch != nil {
					if pmErr != nil {
						cb.OnLogBatch([]string{fmt.Sprintf(
							"nvidia-smi -pm 1 failed (%v): %s", pmErr, strings.TrimSpace(string(pmOut)))})
					} else {
						cb.OnLogBatch([]string{"nvidia-smi -pm 1 succeeded"})
					}
				}
			} else if cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{"systemctl start nvidia-persistenced succeeded"})
			}
			out, err = e.composeUpWithRetry(ctx, cb, project, composePath, deploySecrets, deployExtraEnv, false)
			if err != nil {
				return nil, fmt.Errorf("compose up failed: %w\n%s", err, out)
			}
		} else if err != nil && isActiveEndpointsNetworkErr(out, err) {
			// 2026-10-02 — stack 29 redeploy failed because a compose service the
			// new spec dropped left its mesh sidecar attached to the project
			// network; the daemon refuses to remove that network ("has active
			// endpoints"), so `up` cannot recreate it. Tear the project down with
			// orphans removed — volumes preserved, no -v — then retry once.
			if cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{fmt.Sprintf(
					"orphaned endpoint holding %s's network — tearing down before retry", project)})
			}
			e.composeCmd(ctx, cb, project, "", nil, "down", "--remove-orphans")
			out, err = e.composeUpWithRetry(ctx, cb, project, composePath, deploySecrets, deployExtraEnv, false)
			if err != nil {
				return nil, fmt.Errorf("compose up failed: %w\n%s", err, out)
			}
		} else if err != nil {
			return nil, fmt.Errorf("compose up failed: %w\n%s", err, out)
		}
		// Compose can report success with a container missing one of its
		// declared networks (see stacknetheal.go) — verify and repair.
		if healed := e.healStackNetworks(ctx, cb, project, spec); len(healed) > 0 {
			out += "\n" + strings.Join(healed, "\n")
		}
		// gw-stack-networking GW.2 — report the host-port choices to the
		// manager (see stackhostports.go); empty when nothing was allocated.
		out += formatStackHostPortSentinel(project, hostPortAllocs)
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil

	case "refresh_secret_files":
		// 30.6.8 c8-W1 — rotate a stack's secret files IN PLACE, with no compose
		// call and no container restart. This exists because the identity token
		// is delivered through a DIRECTORY bind (`${MOD_SECRETS_DIR}/identity
		// :/run/mod-identity:ro`): through a dir bind each open re-resolves the
		// name, so a tmp+rename is seen by the RUNNING container at once — no
		// redeploy, no downtime (BUG-1027h: a FILE bind pinned the container to
		// the old inode forever).
		//
		// Refresh only REPLACES content: every target must already exist as a
		// regular file, and every file is validated BEFORE anything is written,
		// so one bad entry means nothing at all changes. The dir inodes are
		// never touched (no clear, no RemoveAll) — that is what keeps the bind
		// live.
		if len(p.SecretFiles) == 0 {
			return nil, fmt.Errorf("refresh_secret_files missing secret_files")
		}
		secretsDir := stackSecretsDirFor(project, stackRootDir(project))
		if fi, err := os.Stat(secretsDir); err != nil || !fi.IsDir() {
			return nil, fmt.Errorf("stack not deployed")
		}
		refreshed, refreshSecrets, rerr := refreshStackSecretFiles(secretsDir, p.SecretFiles)
		if rerr != nil {
			// Validate-first: refreshStackSecretFiles writes nothing until every
			// entry checks out, so a failure here left the stack untouched.
			return nil, rerr
		}
		// The values join the same redaction list deploy uses — a refreshed
		// secret must never reach a log line or the Result tail.
		out := fmt.Sprintf("refresh_secret_files ok: refreshed %d", refreshed)
		for _, s := range refreshSecrets {
			if s != "" {
				out = strings.ReplaceAll(out, s, "***REDACTED***")
			}
		}
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil

	case "logs":
		// READ-ONLY, and the reason it exists: a stack that dies on a remote
		// worker is otherwise undiagnosable from the manager. On 2026-08-13 a
		// model server was stopped repeatedly and the deploy task reported only
		// compose's own output — "Starting / Started" — because that is all the
		// deploy op returns. The container's reason for dying lived on a host
		// nobody could reach without SSH, and that is how it was eventually
		// found: by hand, over SSH, at the end of a long night.
		//
		// `--no-color` because the manager stores this as text and ANSI escapes
		// make it unreadable in psql and in the UI alike. `--timestamps` so a
		// crash can be lined up against the task and heartbeat records that
		// bracket it, which is most of what makes a log useful after the fact.
		//
		// On 2026-09-28 that combined `logs` call was found to be undiagnosable
		// in a DIFFERENT way: a chatty container (e.g. an LLM server) filled the
		// whole 4KB tail, silently hiding every other service's output —
		// including the one that actually mattered (nginx logging DNS failures
		// for the other two). `ps -a --services` needs no compose file (it
		// resolves by the project label, same as `logs` always has), so this
		// gets a status table plus a fair, capped-per-service breakdown instead
		// of one combined stream.
		services, svcErr := e.composeCmd(ctx, cb, project, "", nil, "ps", "-a", "--services")
		if svcErr != nil || strings.TrimSpace(services) == "" {
			out, err := e.composeCmd(ctx, cb, project, "", nil, "logs", "--tail", "200",
				"--no-color", "--timestamps")
			if err != nil || strings.TrimSpace(out) == "" {
				// A project that does not exist here is a legitimate answer to
				// "what happened on this host", not a failure to report — and it
				// must be SAID: the old empty-string result read as "stack logs
				// don't work" to the operator reading the UI.
				return &Result{
					LogTail: tail(fmt.Sprintf(
						"no containers for project %s on this host (never deployed here, or removed)",
						project)),
					Duration: time.Since(started),
				}, nil
			}
			return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil
		}
		// 2026-09-29 — crash-looping mesh sidecars on the Dell/R9000 hosts
		// filled the whole result with tailscale x509 spam and pushed the real
		// app services (lb, vllm-0) out of it entirely. So the sidecar
		// sections go FIRST and the app services LAST: tailN keeps the TAIL,
		// so what the operator actually wants to read is what survives
		// truncation.
		var infra, apps []string
		for _, svc := range strings.Fields(services) {
			if isMeshSidecar(svc) {
				infra = append(infra, svc)
			} else {
				apps = append(apps, svc)
			}
		}
		sort.Strings(infra)
		sort.Strings(apps)
		svcList := append(infra, apps...)

		var b strings.Builder
		if statusOut, err := e.composeCmd(ctx, cb, project, "", nil, "ps", "-a"); err != nil {
			fmt.Fprintf(&b, "(compose ps -a failed: %v)\n%s\n", err, statusOut)
		} else {
			b.WriteString(statusOut)
			if !strings.HasSuffix(statusOut, "\n") {
				b.WriteByte('\n')
			}
		}
		for _, svc := range svcList {
			svcOut, err := e.composeCmd(ctx, cb, project, "", nil, "logs", "--tail", "200",
				"--no-color", "--timestamps", svc)
			fmt.Fprintf(&b, "\n=== %s ===\n", svc)
			if err != nil {
				fmt.Fprintf(&b, "(logs error: %v)\n", err)
			}
			svcCap := perServiceLogsCap
			if isMeshSidecar(svc) {
				svcCap = perSidecarLogsCap // sidecars log to be debugged, not read
			}
			b.WriteString(tailN(svcOut, svcCap))
			if !strings.HasSuffix(svcOut, "\n") {
				b.WriteByte('\n')
			}
		}
		return &Result{LogTail: tailN(b.String(), stackLogsOverallCap), Duration: time.Since(started)}, nil

	case "teardown":
		// `down` works by project name from the daemon labels; no file needed.
		// GA.ASW.8 — data policy. "retain" must NOT destroy the stack's data, so
		// the volumes are left in place: `down` WITHOUT `-v` tears the containers
		// off but keeps the compose project's named volumes (and their contents)
		// on disk. Absent / "erase" keeps the pre-existing `down -v` reclaim.
		// ISO 27001: retain is the no-hard-delete path.
		teardownArgs := []string{"down", "--remove-orphans"}
		if strings.TrimSpace(p.StackDataPolicy) != "retain" {
			teardownArgs = append(teardownArgs, "-v")
		}
		out, err := e.composeCmd(ctx, cb, project, "", nil, teardownArgs...)
		if err != nil {
			return nil, fmt.Errorf("compose down failed: %w\n%s", err, out)
		}
		// GX.S10b — drop the project's materialized secret and file dirs so no
		// secret file outlives the stack. Best-effort; a missing dir is fine.
		cleanupStackFiles(project)
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil

	case "stop":
		// stack-ha-reconciliation §2.3 — a DELIBERATE stop, distinct from
		// teardown. `stop` leaves the containers, volumes and compose project
		// intact so `start` can resume them; `down -v` destroys the data.
		// Collapsing the two would make "pause this for now" silently
		// destructive.
		out, err := e.composeCmd(ctx, cb, project, "", nil, "stop")
		if err != nil {
			return nil, fmt.Errorf("compose stop failed: %w\n%s", err, out)
		}
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil

	case "start":
		// Resume a previously stopped project. Fails if the containers are
		// gone (a teardown happened, or the host was rebuilt) — that is the
		// correct outcome, because the reconciler's deploy path is what
		// recreates a stack, and silently redeploying here would bypass the
		// accelerator fit check that deploy is subject to.
		out, err := e.composeCmd(ctx, cb, project, "", nil, "start")
		if err != nil {
			return nil, fmt.Errorf("compose start failed: %w\n%s", err, out)
		}
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil

	case "boot_restore":
		// Worker-controlled start (stackloop.go): bring a desired-running stack
		// back after a reboot or crash WITHOUT recreating anything. Order:
		// secret/variable files first (tmpfs is empty after a reboot), then
		// `compose start <down services>` on the existing containers (start
		// honours depends_on). Never down, -v or --force-recreate: volumes and
		// each container's writable layer are kept. If the containers are gone
		// the task fails with "no containers" and the API falls back to the
		// full redeploy.
		psOut, psErr := e.composeCmd(ctx, Callbacks{}, project, "", nil, "ps", "-a", "-q")
		if psErr != nil || strings.TrimSpace(psOut) == "" {
			return nil, fmt.Errorf("boot_restore: no containers for %s; full redeploy needed", project)
		}
		// Review2 L4: a stack the operator stopped meanwhile gets no secret
		// files written either (fail closed, same check as before the start).
		if ok, why := stackStartAllowed(ctx, task.ID); !ok {
			return &Result{LogTail: "boot_restore: not started for " + project + ": " + why, Duration: time.Since(started)}, nil
		}
		var restoreSecrets []string
		if len(p.SecretFiles) > 0 || len(p.StorageFiles) > 0 {
			if len(p.SecretFiles) > 0 && !fileSecretsRootStatus().Allowed {
				return nil, fileSecretsDeployError()
			}
			_, _, sv, merr := materializeStackFiles(ctx, project, p)
			if merr != nil {
				return nil, fmt.Errorf("boot_restore: write stack files: %w", merr)
			}
			restoreSecrets = sv
		}
		// owner 11-20: per-container Docker semantics, live desired-state
		// check before any start (stackloop.go bootRestoreStart).
		return e.bootRestoreStart(ctx, cb, task.ID, project, p.RestoreServices, restoreSecrets, len(p.SecretFiles) > 0, started)

	case "mesh_reup":
		// live-mesh-reenroll — re-up the stack's tailscale sidecar against a new
		// login server (tier switch / control-plane migration) without touching
		// the stack's own containers. `--force-reauth` makes this idempotent:
		// the sidecar drops its old node identity and re-registers, rather than
		// silently keeping the old server if already "up".
		//
		// MeshAuthkey is a one-shot credential. It must NEVER reach LogTail, a
		// returned error, or any other log line: not as a literal argv value,
		// and not via the invoked command's own output, because `tailscale`
		// echoes its flags back in some usage/error paths. composeCmdRedacted
		// scrubs it out of everything captured from the child process before
		// any of that output is used.
		if strings.TrimSpace(p.MeshService) == "" {
			return nil, fmt.Errorf("mesh_reup missing mesh_service")
		}
		if strings.TrimSpace(p.MeshLoginServer) == "" {
			return nil, fmt.Errorf("mesh_reup missing mesh_login_server")
		}
		if strings.TrimSpace(p.MeshAuthkey) == "" {
			return nil, fmt.Errorf("mesh_reup missing mesh_authkey")
		}
		if strings.TrimSpace(p.MeshHostname) == "" {
			return nil, fmt.Errorf("mesh_reup missing mesh_hostname")
		}
		out, err := e.composeCmdRedacted(ctx, cb, project, "", []string{p.MeshAuthkey}, nil,
			"exec", "-T", "ts-"+p.MeshService, "tailscale", "up",
			"--login-server="+p.MeshLoginServer,
			"--authkey="+p.MeshAuthkey,
			"--hostname="+p.MeshHostname,
			"--force-reauth")
		if err != nil {
			// out is already scrubbed by composeCmdRedacted — safe to embed.
			return nil, fmt.Errorf("mesh reup failed for %s: %w\n%s", p.MeshService, err, out)
		}
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil

	case "migrate":
		if strings.TrimSpace(p.ComposeSpec) == "" {
			return nil, fmt.Errorf("migrate missing compose_spec")
		}
		if strings.TrimSpace(p.MigrateFromProject) == "" {
			return nil, fmt.Errorf("migrate missing migrate_from_project")
		}
		dir, err := os.MkdirTemp("", "modstack-"+sanitizeProject(project)+"-")
		if err != nil {
			return nil, fmt.Errorf("stack workspace: %w", err)
		}
		defer os.RemoveAll(dir)
		composePath := filepath.Join(dir, "docker-compose.yml")
		migrateSpec := e.pinStackSubnets(ctx, cb, project, p.ComposeSpec)
		// gw-stack-networking GW.2 — the migrate target is a DIFFERENT host,
		// so its daemon has none of this project's bindings: `any` entries
		// allocate fresh ports HERE (and the record the API persisted makes
		// the fixed entries deterministic). Same reporting as deploy.
		var migrateHostAllocs []hostPortMapping
		allocatedMigrate, migrateHostAllocs, herr := e.applyStackHostPorts(ctx, project, migrateSpec)
		if herr != nil {
			return nil, herr
		}
		migrateSpec = allocatedMigrate
		e.recordStackHostPorts(project, migrateHostAllocs)
		// Offline HF cache priming, same as deploy: the migrate target is a
		// DIFFERENT host — precisely the cold one without a local HF cache — so
		// materialize the task's weight refs and flip the spec to
		// HF_HUB_OFFLINE=1 (on a complete fetch) before the compose file is
		// written and the new generation is brought up. No-op when the task
		// carries no weights; a partial fetch stays online and proceeds.
		if len(p.Weights) > 0 {
			prepared, werr := prepareStackWeights(ctx, &p, migrateSpec, creds, func(msgs ...string) {
				if cb.OnLogBatch != nil {
					cb.OnLogBatch(msgs)
				}
			})
			if werr != nil {
				return nil, werr
			}
			migrateSpec = prepared
		}
		// B86i — same co-located-API fix as deploy: the migrate target is a
		// DIFFERENT host, and precisely that host's own /etc/hosts is what the
		// new generation's containers inherit, so re-resolve HERE.
		migrateSpec = e.injectStackAPIHost(ctx, cb, e.apiURL, migrateSpec)
		if err := os.WriteFile(composePath, []byte(migrateSpec), 0o600); err != nil {
			return nil, fmt.Errorf("write compose file: %w", err)
		}
		// 13.5b — same verified fail-closed guard as deploy (before writing any
		// secret bytes): a migrated stack with empty secrets would look healthy
		// and then fail every authenticated call on the NEW host.
		if len(p.SecretFiles) > 0 && !fileSecretsRootStatus().Allowed {
			cleanupStackFiles(project)
			return nil, fileSecretsDeployError()
		}
		// GX.S10b — the migrate target is a DIFFERENT (cold) host, so it needs its
		// own copy of the file secrets and storage files, exactly like deploy.
		var migrateExtraEnv, migrateSecrets []string
		if secretsDir, filesDir, sv, merr := materializeStackFiles(ctx, project, p); merr != nil {
			return nil, merr
		} else {
			migrateExtraEnv = stackEnvForCompose(secretsDir, filesDir)
			migrateSecrets = sv
		}
		// Same reason as deploy: a migration's whole point is landing a stack on
		// a DIFFERENT host, which is exactly where the external platform network
		// will not exist.
		if created := e.ensureExternalNetworks(ctx, p.ComposeSpec); len(created) > 0 && cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf(
				"created missing external network(s): %s", strings.Join(created, ", "))})
		}
		out, err := e.migrateStack(ctx, p.MigrateFromProject, project, p.MigrateVolumes, p.SnapshotBucket, composePath, p.MigrateFreeze, migrateExtraEnv, migrateSecrets)
		if err != nil {
			return nil, fmt.Errorf("migrate failed: %w\n%s", err, out)
		}
		return &Result{LogTail: tail(out), Duration: time.Since(started)}, nil

	case "bench":
		// 27.6.s1 — measure what the stack SERVES (see stack_bench.go). Read-only
		// against the gateway's own port: it never touches the stack's containers,
		// so it is safe on a live stack mid-run, which is the point — an operator
		// sizing a node against a stack does not stop the stack first.
		//
		// It returns a report, not compose output: the JSON rides the log tail
		// inside the bench sentinel, the same channel stackhostports.go uses for
		// the host-port allocation block, because a stack-op Result has no
		// structured field of its own.
		return e.benchStackOp(ctx, p, started)

	default:
		return nil, fmt.Errorf("unknown stack_op %q", p.StackOp)
	}
}

// execCommandContext is a seam over exec.CommandContext so tests can
// substitute a fake child process (no real docker/tailscale needed on the
// test host) and still assert on the exact argv composeCmd built.
var execCommandContext = exec.CommandContext

// composeCmd runs `docker compose -p <project> [-f file] <args...>` and returns
// combined output. Streams output lines through cb.OnLogBatch when wired.
// extraEnv appends process env vars (e.g. MOD_SECRETS_DIR / MOD_FILES_DIR for
// GX.S10b) on top of the inherited environment; empty means unchanged.
func (e *Executor) composeCmd(ctx context.Context, cb Callbacks, project, composePath string, extraEnv []string, args ...string) (string, error) {
	return e.composeCmdRedacted(ctx, cb, project, composePath, nil, extraEnv, args...)
}

// composeCmdRedacted is composeCmd plus scrubbing: every string in `secrets`
// is replaced with a placeholder in the captured output BEFORE that output
// reaches cb.OnLogBatch or is handed back to the caller. Use this instead of
// composeCmd whenever an argv value is a credential, since the invoked
// command may echo its own arguments back on stdout/stderr (e.g. a usage or
// error message) — redacting only the argv is not enough.
func (e *Executor) composeCmdRedacted(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
	// Test hook: when set, unit tests intercept all docker compose invocations.
	if e.composeCmdRedactedHook != nil {
		return e.composeCmdRedactedHook(ctx, cb, project, composePath, secrets, extraEnv, args...)
	}
	full := []string{"compose", "-p", project}
	if composePath != "" {
		full = append(full, "-f", composePath)
	}
	full = append(full, args...)

	cmd := execCommandContext(ctx, "docker", full...)
	if len(extraEnv) > 0 {
		// Keep the inherited environment; append so MOD_SECRETS_DIR /
		// MOD_FILES_DIR (GX.S10b) are visible to `docker compose` for
		// ${...} interpolation without disturbing anything it already had.
		cmd.Env = append(os.Environ(), extraEnv...)
	}
	rawOut, err := cmd.CombinedOutput()
	out := string(rawOut)
	for _, s := range secrets {
		if s != "" {
			out = strings.ReplaceAll(out, s, "***REDACTED***")
		}
	}
	if cb.OnLogBatch != nil && len(out) > 0 {
		lines := []string{}
		for _, ln := range strings.Split(strings.TrimRight(out, "\n"), "\n") {
			if strings.TrimSpace(ln) != "" {
				lines = append(lines, ln)
			}
		}
		if len(lines) > 0 {
			cb.OnLogBatch(lines)
		}
	}
	return out, err
}

// composeUpWithRetry attempts a compose up, retrying on transient post-boot
// errors with exponential backoff.  The specific "stale network" case is
// handled first with an immediate --force-recreate (matching the existing
// inline retry below).  Mount errors and other transient errors get
// exponential-backoff retries via --force-recreate.  Non-transient errors
// fail immediately without retry.
//
// This is a wrapper around composeCmdRedacted so the deploy path can be
// changed in one place when the error-detection or retry strategy evolves.
func (e *Executor) composeUpWithRetry(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, forceRecreate bool, args ...string) (string, error) {
	return composeUpWithRetryImpl(ctx, cb, e, project, composePath, secrets, extraEnv, forceRecreate, args...)
}

// composeUpWithRetryImpl is the retry logic extracted from composeUpWithRetry
// for testability. Tests can call this directly with a fake runner.
func composeUpWithRetryImpl(ctx context.Context, cb Callbacks, e *Executor, project, composePath string, secrets, extraEnv []string, forceRecreate bool, args ...string) (string, error) {
	maxRetries := maxComposeUpRetries
	for attempt := 0; attempt <= maxRetries; attempt++ {
		var composeArgs []string
		// Initial call uses the caller's forceRecreate preference; retries
		// always use --force-recreate because the stale-network / mount
		// scenarios that trigger retries require recreating containers
		// against newly available resources.
		retry := forceRecreate || attempt > 0
		if retry {
			composeArgs = append([]string{"up", "-d", "--remove-orphans", "--force-recreate"}, args...)
		} else {
			composeArgs = append([]string{"up", "-d", "--remove-orphans"}, args...)
		}
		out, err := e.composeCmdRedacted(ctx, cb, project, composePath, secrets, extraEnv, composeArgs...)
		if err == nil {
			return out, nil
		}
		if attempt >= maxRetries {
			return "", fmt.Errorf("compose up failed: %w\n%s", err, out)
		}
		// Immediate retry for stale-network (the existing inline path).
		if isStaleNetworkErr(out, err) {
			if cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{fmt.Sprintf(
					"stale network reference detected for %s — force-recreating containers (attempt %d)", project, attempt+1)})
			}
			continue // loop: retries already include --force-recreate
		}
		// Immediate retry for nvidia-persistenced (the existing inline path).
		if isNvidiaPersistencedSocketErr(out, err) {
			// Let the caller handle nvidia-persistenced specifically.
			return "", fmt.Errorf("compose up failed: %w\n%s", err, out)
		}
		// Immediate retry for active-endpoints network (the existing inline path).
		if isActiveEndpointsNetworkErr(out, err) {
			// Let the caller handle active-endpoints specifically.
			return "", fmt.Errorf("compose up failed: %w\n%s", err, out)
		}
		// Mount errors and other transient failures: backoff and retry.
		if isMountErr(out, err) {
			waitSeconds := 1 << attempt
			if waitSeconds > maxComposeUpRetrySeconds {
				waitSeconds = maxComposeUpRetrySeconds
			}
			if cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{fmt.Sprintf(
					"mount error on %s — retrying in %ds (attempt %d/%d)", project, waitSeconds, attempt+1, maxRetries)})
			}
			time.Sleep(time.Duration(waitSeconds) * time.Second)
			continue
		}
		// Non-transient: give up immediately.
		return "", fmt.Errorf("compose up failed: %w\n%s", err, out)
	}
	// Should not reach here, but just in case.
	return "", fmt.Errorf("compose up failed: too many retries")
}

// preflightOrphanNames refuses a deploy that an UNLABELLED container will block.
//
// `docker compose up` never adopts a container that carries no
// com.docker.compose.project label: a hand-started container squatting on a
// compose-chosen name makes compose die with a bare "Conflict. The container
// name ... is already in use", and because the reconciler re-deploys on every
// revision, that riddle repeats forever (stack 140 on worker-mod-demo-machine).
// Detect it BEFORE the image pulls, name the container, and fail with the
// operator's fix so the log stops being a riddle.
//
// REFUSE, never auto-remove: image-match is not ownership — the incident
// container matched the service image and WAS the operator's tuning state
// (env, mounts, overrides). This pre-flight is strictly read-only (ps,
// inspect); the removal command is printed for the operator, never executed.
//
// A docker LIST error proceeds: we could not prove a collision, and blocking
// on a daemon hiccup would turn a healthy deploy into a failed one. Same for a
// failed inspect — no evidence, no refusal.
func (e *Executor) preflightOrphanNames(ctx context.Context, cb Callbacks, project, composeYAML string) error {
	var doc struct {
		Services map[string]struct {
			Image         string `yaml:"image"`
			ContainerName string `yaml:"container_name"`
		} `yaml:"services"`
	}
	if err := yaml.Unmarshal([]byte(composeYAML), &doc); err != nil {
		// compose's own parse gate already reported this; nothing to pre-flight.
		return nil
	}
	proj := composeProjectName(project)
	// The project prefix gate for the removal HINT (never for an action): only a
	// MOD stack project, named modstack<id>g<gen>, is ours to talk about.
	ownProj := regexp.MustCompile(`^modstack[0-9]+g[0-9]+$`)
	// An explicit container_name is only hintable when it too sits under that
	// prefix — anything else is the operator's own namespace.
	ownName := regexp.MustCompile(`^modstack[0-9]+g[0-9]+-`)

	svcs := make([]string, 0, len(doc.Services))
	for name := range doc.Services {
		svcs = append(svcs, name)
	}
	sort.Strings(svcs)

	for _, svc := range svcs {
		declared := strings.TrimSpace(doc.Services[svc].Image)
		// compose's own container name for this service, plus any name the
		// author pinned.
		type nameEntry struct {
			Name     string
			Explicit bool
		}
		var names []nameEntry
		seen := map[string]bool{}
		entries := []nameEntry{
			{Name: proj + "-" + svc + "-1", Explicit: false},
		}
		if pinned := strings.TrimSpace(doc.Services[svc].ContainerName); pinned != "" {
			entries = append(entries, nameEntry{Name: pinned, Explicit: true})
		}
		for _, en := range entries {
			if en.Name == "" || seen[en.Name] {
				continue
			}
			seen[en.Name] = true
			names = append(names, en)
		}

		for _, en := range names {
			n := en.Name
			// The docker name filter is a regex: anchor it, or every container
			// merely CONTAINING this name would be flagged and healthy deploys
			// would refuse.
			out, err := dockerCLI(ctx, "ps", "-a", "--no-trunc", "--format",
				"{{.Name}}|{{.ImageName}}|{{.Id}}", "--filter", "name=^"+n+"$")
			if err != nil {
				if cb.OnLogBatch != nil {
					cb.OnLogBatch([]string{fmt.Sprintf(
						"deploy pre-flight: cannot list containers for %s: %v", svc, err)})
				}
				// Best-effort: proceed, compose will report anything real.
				continue
			}
			for _, line := range strings.Split(out, "\n") {
				line = strings.TrimSpace(line)
				if line == "" {
					continue
				}
				f := strings.Split(line, "|")
				if len(f) < 3 {
					continue
				}
				cname := strings.TrimSpace(strings.TrimPrefix(f[0], "/"))
				imageName := strings.TrimSpace(f[1])
				id := strings.TrimSpace(f[2])
				if cname != n || id == "" {
					continue
				}
				// Only the label we need is read: never the whole label map,
				// never env values, so no secret can ride into a log line.
				labels, lerr := dockerCLI(ctx, "inspect", "-f",
					"{{range .Config.Labels}}{{.key}}={{.value}};{{end}}", id)
				if lerr != nil {
					if cb.OnLogBatch != nil {
						cb.OnLogBatch([]string{fmt.Sprintf(
							"deploy pre-flight: cannot inspect container %s: %v", n, lerr)})
					}
					continue // no evidence of ownership either way: proceed
				}
				if strings.Contains(labels, composeProjectLabel+"="+proj+";") {
					continue // compose owns it; --force-recreate handles redeploys
				}
				ours := ownProj.MatchString(proj) && declared != "" && imageName == declared &&
					(!en.Explicit || ownName.MatchString(n))
				if cb.OnLogBatch != nil {
					hint := ""
					if ours {
						// Only a MOD stack project (modstack<id>g<gen>) whose image
						// matches gets the operator hint - and it is printed, never run.
						hint = " Fix on that host: docker stop " + n + " && docker rm " + n +
							" (or rename it), then redeploy."
					}
					cb.OnLogBatch([]string{"deploy pre-flight: container " + n + " exists WITHOUT " +
						composeProjectLabel + "=" + proj + " - compose will not adopt or replace it, so the" +
						" deploy cannot succeed. MOD did not touch it." + hint})
				}
				return errors.New("stack deploy refused: unlabelled container " + n + " holds the compose" +
					" name for service " + svc + " (image " + imageName + "). Not compose-managed, so compose up" +
					" fails with a name Conflict. Remove or rename it on the host; MOD never removes" +
					" containers outside modstack<id>g<gen>-")
			}
		}
	}
	return nil
}

// isStaleNetworkErr reports whether a compose up failure is the stale-network
// case: the daemon says a network by ID no longer exists. `--force-recreate`
// is the only compose-level cure, because the stopped containers' config (and
// thus the up-diff) looks unchanged.
func isStaleNetworkErr(out string, err error) bool {
	if err == nil {
		return false
	}
	low := strings.ToLower(out + "\n" + err.Error())
	return strings.Contains(low, "network") && strings.Contains(low, "not found")
}

// isActiveEndpointsNetworkErr reports the active-endpoints case: the daemon
// refuses to remove a project network because an orphaned container (usually a
// mesh sidecar from a compose service the new spec dropped) is still attached
// to it, so `compose up` fails while trying to recreate that network. The cure
// is `down --remove-orphans` (volumes preserved) followed by one `up` retry.
func isActiveEndpointsNetworkErr(out string, err error) bool {
	if err == nil {
		return false
	}
	return strings.Contains(strings.ToLower(out+"\n"+err.Error()), "has active endpoints")
}

// nvmlProbeTimeout bounds every nvidia-smi/systemctl call in the
// persistenced self-heal path (1131). A driver/library version mismatch is
// exactly the condition under which nvidia-smi is known to hang instead of
// erroring, so these probes must not inherit ctx's (possibly absent or very
// long) deadline.
const nvmlProbeTimeout = 10 * time.Second

// isNvidiaPersistencedSocketErr reports whether a compose up failure is the
// nvidia-persistenced-not-running case: a container tried to mount or dial
// /run/nvidia-persistenced/socket and found nothing there. By itself this
// says nothing about *why* persistenced isn't up — see isNVMLDriverMismatch
// for the reboot-required case that this must not retry.
func isNvidiaPersistencedSocketErr(out string, err error) bool {
	if err == nil {
		return false
	}
	low := strings.ToLower(out + "\n" + err.Error())
	return strings.Contains(low, "nvidia-persistenced/socket")
}

// isNVMLDriverMismatch reports whether nvidia-smi output shows the NVML
// "Driver/library version mismatch" condition: unattended-upgrades bumped
// the nvidia userspace package while the old kernel module is still loaded.
// Only a reboot clears this — nvidia-persistenced cannot be restarted into a
// healthy state, so callers must not retry when this is true.
func isNVMLDriverMismatch(out string) bool {
	return strings.Contains(strings.ToLower(out), "driver/library version mismatch")
}

// --- Retry configuration (1360: post-boot transient errors) ---------------

// maxComposeUpRetries is the number of additional retries after the first
// failed attempt. A stack that fails once due to a post-boot transient error
// (stale bind-mount, recycled network ID) has at most this many more tries.
//
// Tests may override: e.maxRetries = n  or  maxComposeUpRetries = n
var maxComposeUpRetries = 3

// retryBackoffSeconds defines the exponential backoff window: attempt 0
// waits 2^0 = 1s, attempt 1 waits 2^1 = 2s, attempt 2 waits 2^2 = 4s,
// attempt 3 waits 2^3 = 8s (capped at 10s).
//
// Tests may override: maxComposeUpRetrySeconds = n
var maxComposeUpRetrySeconds = 10

// isMountErr reports whether a compose up failure is a bind-mount problem
// likely caused by a stale Docker-Desktop WSL2 bind-mount proxy path after
// a host reboot (e.g. the source file path doesn't exist because the proxy
// hasn't synced yet).  We retry with --force-recreate and backoff because
// the mount should heal itself within a few seconds.
func isMountErr(out string, err error) bool {
	if err == nil {
		return false
	}
	low := strings.ToLower(out + "\n" + err.Error())
	// "bind source path <some path> does not exist"
	// The path is quoted/inserted between the two parts, so check them
	// as separate signals rather than one long substring.
	if strings.Contains(low, "bind source path") && strings.Contains(low, "does not exist") {
		return true
	}
	// "error mounting ... no such file or directory"
	if strings.Contains(low, "error mounting") && strings.Contains(low, "no such file or directory") {
		return true
	}
	// "OCI runtime create failed" — only when the root cause is a mount.
	// Docker includes the failing container name and "no such file or
	// directory" in the error string; we check for the mount-related
	// signal rather than retrying every OCI failure (which could mask
	// real container image / runtime issues).
	if strings.Contains(low, "oci runtime create failed") {
		if strings.Contains(low, "no such file or directory") {
			return true
		}
	}
	return false
}

// gpuMismatchMu/gpuMismatchWarning hold the current NVML driver/library
// mismatch warning, if any, surfaced through the heartbeat's `warnings`
// field via GPUWarnings (see heartbeat.OnGPUWarnings). Mirrors
// credsrefresher's package-level Warnings() registry. Empty →
// contributes nothing: compose succeeded or nvidia-smi reports healthy.
var (
	gpuMismatchMu      sync.Mutex
	gpuMismatchWarning string
)

// setGPUDriverMismatchWarning records the current NVML mismatch warning.
func setGPUDriverMismatchWarning(w string) {
	gpuMismatchMu.Lock()
	gpuMismatchWarning = w
	gpuMismatchMu.Unlock()
}

// clearGPUDriverMismatchWarning clears any previously recorded NVML mismatch
// warning, e.g. once nvidia-smi reports healthy again.
func clearGPUDriverMismatchWarning() {
	setGPUDriverMismatchWarning("")
}

// GPUWarnings returns the current NVML driver/library mismatch warning (if
// any) as a single-element slice, or nil when healthy. Wired to the
// heartbeat via hb.OnGPUWarnings(executor.GPUWarnings) in main.
func GPUWarnings() []string {
	gpuMismatchMu.Lock()
	defer gpuMismatchMu.Unlock()
	if gpuMismatchWarning == "" {
		return nil
	}
	return []string{gpuMismatchWarning}
}

// sanitizeProject keeps temp-dir names tame.
func sanitizeProject(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteRune('-')
		}
	}
	return b.String()
}

// perServiceLogsCap, perSidecarLogsCap and stackLogsOverallCap bound the
// "logs" op's per-service breakdown: each service keeps its own budget (so
// one chatty container, e.g. an LLM server, cannot crowd every other service
// out of the tail), and the assembled result has its own, larger, dedicated
// cap — deliberately separate from tail()'s 4KB, which every other stack op
// still uses unchanged. Sidecar services get a much smaller budget: their
// output (tailscale auth chatter and friends) is there to be grepped for when
// the mesh itself is the suspect, never read top-to-bottom.
const (
	perServiceLogsCap   = 6 * 1024
	perSidecarLogsCap   = 1536
	stackLogsOverallCap = 48 * 1024
)

// isMeshSidecar reports whether a compose service name belongs to the mesh
// sidecars the platform injects — the `ts-<svc>`, `ts-<svc>-<anchor>` and
// `ts-<svc>-net` tailscale carriers, plus the `tsegress-*` and `ingress-*`
// egress helpers. The "logs" op keeps their sections (and their per-service
// cap) separate from the stack's own app services, for the reason given at
// the call site.
func isMeshSidecar(svc string) bool {
	return strings.HasPrefix(svc, "ts-") ||
		strings.HasPrefix(svc, "tsegress-") ||
		strings.HasPrefix(svc, "ingress-")
}

// tail returns the last ~4KB of compose output for the result log.
func tail(s string) string { return tailN(s, 4096) }

// tailN returns the last max bytes of s, prefixed with a truncation marker
// when it had to cut anything.
func tailN(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return "...(truncated)...\n" + s[len(s)-max:]
}

// platformNetworkName is the reserved network no delegated stack may attach to —
// reaching the manager's platform services is the API's job (via the sanctioned
// egress rewrite) and the mesh's, never a direct bridge attachment. Mirrors the
// worker config default (config.go: DOCKER_NETWORK).
func platformNetworkName() string {
	if v := strings.TrimSpace(os.Getenv("DOCKER_NETWORK")); v != "" {
		return v
	}
	return "mod_modtex-network"
}

// refuseEgressBypass returns an error if any service in the compose attaches to
// the reserved platform network, whether by its own key or via a top-level
// network whose `name:` resolves to it. delegated-stack-authoring §5.4: the API
// rewrite is authoritative; this stops a hand-authored `networks:` from putting a
// service back on the platform bridge and defeating the declared egress posture.
func refuseEgressBypass(composeYAML, platformNet string) error {
	var doc struct {
		Services map[string]struct {
			Networks interface{} `yaml:"networks"`
		} `yaml:"services"`
		Networks map[string]struct {
			Name string `yaml:"name"`
		} `yaml:"networks"`
	}
	if err := yaml.Unmarshal([]byte(composeYAML), &doc); err != nil {
		// A spec we cannot parse is one we cannot vouch for. Refuse rather than
		// wave it through — compose would reject it downstream anyway.
		return fmt.Errorf("egress enforcement: cannot parse compose to verify network posture: %w", err)
	}
	// The set of top-level network KEYS that resolve to the reserved platform net
	// (a key maps to the platform net either by matching its name directly, or via
	// an explicit `name:` field).
	reserved := map[string]bool{}
	for key, n := range doc.Networks {
		name := strings.TrimSpace(n.Name)
		if name == platformNet || (name == "" && key == platformNet) {
			reserved[key] = true
		}
	}
	// Even with no top-level declaration, a bare service `networks: [<platformNet>]`
	// referencing the platform name directly is a bypass attempt.
	reserved[platformNet] = true
	for svcName, svc := range doc.Services {
		for _, attached := range serviceNetworkKeys(svc.Networks) {
			if reserved[attached] {
				return fmt.Errorf(
					"egress enforcement: service %q attaches to reserved platform network %q; declare x-mod.egress and let the platform place it",
					svcName, platformNet)
			}
		}
	}
	return nil
}

// serviceNetworkKeys normalises a compose service `networks:` value — which is
// either a list of keys or a map keyed by network name — to a slice of keys.
func serviceNetworkKeys(v interface{}) []string {
	switch nets := v.(type) {
	case []interface{}:
		out := make([]string, 0, len(nets))
		for _, n := range nets {
			if s, ok := n.(string); ok {
				out = append(out, s)
			}
		}
		return out
	case map[string]interface{}:
		out := make([]string, 0, len(nets))
		for k := range nets {
			out = append(out, k)
		}
		return out
	}
	return nil
}

// ensureExternalNetworks creates any network the compose spec declares as
// `external: true` that does not already exist on this host, returning the
// names it created.
//
// Compose treats "external" as a promise the operator has already made the
// network, and hard-fails when that promise is false. That is right on the
// manager and wrong everywhere else: a remote worker never had the platform
// network, so every stack died before its first container started.
//
// Deliberately best-effort. A creation failure is NOT returned as an error —
// `compose up` runs regardless and produces the real, specific message. This
// helper exists to remove a foot-gun, not to become a second place a deploy
// can fail from.
func (e *Executor) ensureExternalNetworks(ctx context.Context, composeYAML string) []string {
	if strings.TrimSpace(composeYAML) == "" {
		return nil
	}
	var doc struct {
		Networks map[string]struct {
			Name     string      `yaml:"name"`
			External interface{} `yaml:"external"`
		} `yaml:"networks"`
	}
	if err := yaml.Unmarshal([]byte(composeYAML), &doc); err != nil {
		return nil
	}

	var created []string
	for key, n := range doc.Networks {
		// `external` is a bool in modern compose and was a mapping in older
		// schemas ({external: {name: x}}). Anything present and not literally
		// false is treated as external, because guessing wrong here only costs
		// an unnecessary network on a host that already had one.
		switch v := n.External.(type) {
		case nil:
			continue
		case bool:
			if !v {
				continue
			}
		}
		name := strings.TrimSpace(n.Name)
		if name == "" {
			name = key
		}
		if err := exec.CommandContext(ctx, "docker", "network", "inspect", name).Run(); err == nil {
			continue // already present — the manager's normal case
		}
		if err := exec.CommandContext(ctx, "docker", "network", "create", name).Run(); err == nil {
			created = append(created, name)
		}
	}
	return created
}

// staleDownloadAge is how long a partial download must sit untouched before it
// is treated as abandoned. An in-flight fetch rewrites its .incomplete file
// continuously, so anything older than this had its process killed.
const staleDownloadAge = 15 * time.Minute

// sweepStaleDownloads clears lock and partial files an interrupted model
// download left behind in a SHARED cache, and returns what it removed.
//
// Why this has to be automatic. The model cache is a BIND mount, so
// `compose down -v` never touches it — `-v` removes volumes. Kill a deploy
// mid-download (teardown, worker restart, host reboot, OOM) and the partial
// blob plus its lock outlive every teardown AND every redeploy. The next
// deploy of that model then hangs FOREVER on the lock: the container starts,
// publishes its port, logs nothing past its startup banner, and never loads.
// Reproduced on worker-example-host, where two teardowns during a download left a
// stack that could not be deployed again at all.
//
// That failure is silent, permanent and invisible to the platform — the
// container is "Up", so nothing reports a fault — and the only fix was a human
// with a shell deleting files. A self-healing system cannot have a state that
// only an operator with SSH can clear.
//
// Conservative by construction: if ANY partial in the cache has been touched
// within staleDownloadAge, a download is genuinely in flight (possibly for a
// different stack sharing this host) and the sweep is skipped entirely. Only a
// quiescent cache is cleaned, so this can never race a live fetch.
func (e *Executor) sweepStaleDownloads(composeYAML string) []string {
	roots := modelCacheRoots(composeYAML)
	var removed []string
	for _, root := range roots {
		if fi, err := os.Stat(root); err != nil || !fi.IsDir() {
			continue // not mounted into this worker — nothing we can do
		}
		var candidates []string
		active := false
		_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil //nolint:nilerr // an unreadable subtree must not abort the sweep
			}
			name := d.Name()
			if !strings.HasSuffix(name, ".lock") && !strings.Contains(name, ".incomplete") {
				return nil
			}
			info, err := d.Info()
			if err != nil {
				return nil
			}
			if time.Since(info.ModTime()) < staleDownloadAge {
				active = true
				return filepath.SkipAll
			}
			candidates = append(candidates, path)
			return nil
		})
		if active {
			continue
		}
		for _, p := range candidates {
			if os.Remove(p) == nil {
				removed = append(removed, p)
			}
		}
	}
	return removed
}

// modelCacheRoots returns host paths the compose spec bind-mounts as a model
// cache. Derived from the spec rather than hardcoded so a relocated cache is
// still swept.
func modelCacheRoots(composeYAML string) []string {
	var doc struct {
		Services map[string]struct {
			Volumes []string `yaml:"volumes"`
		} `yaml:"services"`
	}
	if err := yaml.Unmarshal([]byte(composeYAML), &doc); err != nil {
		return nil
	}
	seen := map[string]bool{}
	var roots []string
	for _, svc := range doc.Services {
		for _, v := range svc.Volumes {
			parts := strings.Split(v, ":")
			if len(parts) < 2 || !strings.HasPrefix(parts[0], "/") {
				continue // named volume, or not a bind
			}
			target := parts[1]
			if !strings.Contains(target, "huggingface") && !strings.Contains(target, ".cache") {
				continue
			}
			if !seen[parts[0]] {
				seen[parts[0]] = true
				roots = append(roots, parts[0])
			}
		}
	}
	return roots
}

// stackImages lists the distinct `image:` references in a compose spec.
func stackImages(composeYAML string) []string {
	var doc struct {
		Services map[string]struct {
			Image string `yaml:"image"`
		} `yaml:"services"`
	}
	if err := yaml.Unmarshal([]byte(composeYAML), &doc); err != nil {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, svc := range doc.Services {
		img := strings.TrimSpace(svc.Image)
		if img != "" && !seen[img] {
			seen[img] = true
			out = append(out, img)
		}
	}
	sort.Strings(out)
	return out
}

// prePullStackImages pulls every image the stack uses from a registry we hold
// per-task credentials for, BEFORE `compose up`. Compose's default pull policy
// ("missing") then finds them locally, so a host that was never `docker login`ed
// to Harbor deploys exactly like one that was.
//
// Only images on a registry the grant covers are pulled here; anything else
// (Docker Hub, ghcr, a local registry) is left to compose and the host's own
// auth, as before. A pull failure IS returned: compose would hit the same wall
// with a vaguer message, and the pull error names the image and registry.
func (e *Executor) prePullStackImages(ctx context.Context, composeYAML string, creds *transport.Credentials, cb Callbacks) error {
	if creds == nil {
		return nil
	}
	if reg, err := harborRegistry(creds.Harbor); err == nil {
		e.noteHarborHost(reg)
	}
	hosts := map[string]bool{}
	for _, h := range []transport.HarborCredentials{creds.Harbor, creds.TenantHarbor} {
		if h.Username != "" && h.Registry != "" {
			hosts[normalizeHost(h.Registry)] = true
		}
	}
	if len(hosts) == 0 {
		return nil
	}
	for _, img := range stackImages(composeYAML) {
		if !hosts[imageHost(img)] {
			// Not a registry we hold Harbor creds for — a public image
			// (Docker Hub, ghcr, nvcr). GF.C3b: opportunistically pre-pull
			// it through Harbor's proxy-cache when enabled; best-effort,
			// never fails the deploy — compose's own "missing" pull
			// policy is still the fallback of last resort, same as
			// before this landed.
			if err := e.maybePullPublicStackImage(ctx, img, cb); err != nil && cb.OnLogBatch != nil {
				cb.OnLogBatch([]string{fmt.Sprintf(
					"harbor-proxy-cache: pre-pull %s failed, leaving to compose: %v", img, err)})
			}
			continue
		}
		pullCreds := PickRegistryAuth(img, creds.Harbor, creds.TenantHarbor)
		if cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf("pulling %s (%s registry credentials)",
				img, registryKindFor(img, creds.Harbor, creds.TenantHarbor))})
		}
		if err := e.pullImage(ctx, img, pullCreds, ""); err != nil {
			return fmt.Errorf("pull %s failed: %w", img, err)
		}
	}
	return nil
}

// maybePullPublicStackImage opportunistically pre-pulls a public (non-
// Harbor-credentialed) stack image through Harbor's proxy-cache (GF.C3b),
// when worker.harbor_proxy_cache is enabled and a Harbor host is known. A
// no-op when the setting is off or no host is known yet — compose then
// handles the pull exactly as before this landed.
func (e *Executor) maybePullPublicStackImage(ctx context.Context, imageRef string, cb Callbacks) error {
	if !e.harborProxyCacheEnabled() || e.knownHarborHost() == "" {
		return nil
	}
	if cb.OnLogBatch != nil {
		cb.OnLogBatch([]string{fmt.Sprintf("pulling %s (harbor-proxy-cache)", imageRef)})
	}
	return e.pullPublicImage(ctx, imageRef)
}
