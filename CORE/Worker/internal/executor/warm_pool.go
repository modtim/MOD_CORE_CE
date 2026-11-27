package executor

// KEEP-WARM-SLICE-1 (owner Tim 11-25: do NOT park keep-warm; offer it as a known,
// opt-in capability). Slice 1 per results-1110/KEEP-WARM-DESIGN-REVIEW-AND-SLICE1-PLAN.md:
// embedded worker only, ONE opted-in node, EXEC mode only, no scheduler change, no
// migration, default-deny egress only.
//
// Why a pool volume: Docker fixes Binds/network/devices/memory/user/caps/shm at
// ContainerCreate, so a running container can NEVER gain a fresh per-call volume.
// The warm container owns ONE pool volume WIPED and VERIFIED empty (find -mindepth
// 1: dotfiles, symlinks, special files included) before AND after every call; any
// non-empty result or wipe failure RETIRES the container, never retries the wipe.
//
// Exec mode: every call is a NEW process (docker exec), so the node's own memory
// cannot carry over. Daemon mode (memory carry-over the platform cannot clear) is
// NOT implemented here; the node flag help text says so.
//
// Dual opt-in (both required, default OFF): the node-definition flag warm_policy
// (API stamps it onto the payload ONLY when the tenant-scope platform_settings
// row worker.warm_pool_enabled is true) AND the platform kill switch (worker
// tunable worker.warm_pool_enabled, default false).
// worker.warm_pool_max_containers is a TECHNICAL SAFETY LIMIT (not a CE quantity
// cap): it bounds one worker's own footprint; flagged for the owner.

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"os"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"sort"
	"strings"
	"sync"
	"time"

	goruntime "runtime"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/volume"
	"github.com/docker/docker/pkg/stdcopy"
)

const warmPoolLabel = "com.example.mod-core.warm-pool"
const warmPoolEnabledTunableKey = "worker.warm_pool_enabled"
const warmPoolMaxContainersTunableKey = "worker.warm_pool_max_containers"

// WarmPool is the per-worker registry: one container per
// (tenant, resolved digest, node kind, create-time config fingerprint).
type WarmPool struct {
	mu      sync.Mutex
	entries map[string]*warmEntry
	// coldOnlyUntil: after a warm INFRA failure the same key runs COLD for a
	// cooldown window, so the retry under max_attempts 3 is deterministically
	// cold rather than racing a fresh warm container.
	coldOnlyUntil map[string]int64
	instanceID    string
}

type warmKeyParts struct {
	tenantID int
	digest   string
	nodeKind string
	configFP string
}

// warmEntry is one live pool container. envHash is the env the entry was last
// served with; a different hash retires it (env change = cold restart).
// busy (L-2): true while a call holds this container; a busy entry is NEVER
// handed to a second concurrent call for the same key (that call runs cold).
// pidBaseline (L-7): the process count the keep-alive init produces, captured
// at acquire with the SAME ps pipeline the check uses, so the check compares
// like-for-like (busybox `ps -e -o pid= | wc -l` itself spawns sh+ps+wc).
type warmEntry struct {
	containerID, volumeName, key, envHash string
	calls, idleSeconds, maxCalls, pidBaseline int
	busy     bool
	lastUse  time.Time
}

// warmCooldownKey is the (tenant, digest) cooldown key: after a warm INFRA
// failure the same node+digest runs cold for the window, regardless of which
// config fingerprint variant failed (L-5: the mark and the check must agree).
func warmCooldownKey(tenantID int, digest string) string {
	return strconv.Itoa(tenantID) + "|" + digest
}
func (k warmKeyParts) str() string {
	return strconv.Itoa(k.tenantID) + "|" + k.digest + "|" + k.nodeKind + "|" + k.configFP
}



// warmEligibility is the fail-closed gate: "" = warm allowed, else the reason
// (the task then runs on the untouched cold path).
func warmEligibility(p transport.TaskPayload, policy *transport.WarmPolicy) string {
	if policy == nil {
		return "no warm_policy"
	}
	if policy.Protocol != "" && policy.Protocol != "exec" {
		return "protocol " + policy.Protocol + " not eligible in slice 1 (exec only)"
	}
	if policy.IdleSeconds <= 0 || policy.MaxMemoryMB <= 0 || policy.MaxCalls < 1 {
		return "warm_policy values invalid"
	}
	if p.GPURequired || p.GPUAssignment != nil || p.K8sGPU != nil {
		return "gpu nodes are not eligible in slice 1"
	}
	if len(p.AllowedEgress) > 0 || p.TailnetEgress != nil ||
		p.EgressMode != "" || p.EgressUpstream != "" {
		return "egress allowlist/mediated egress can never be warm (per-task proxy holder is torn down)"
	}
	if p.NeedsDockerSocket || p.CheckpointVolume != "" ||
		p.CheckpointExportURL != "" || p.CheckpointHydrateURL != "" {
		return "docker-socket/checkpoint posture not eligible"
	}
	if _, isPlatform := p.ContainerEnv["PLATFORM_TASK"]; isPlatform {
		return "platform tasks get host env (vault/postgres/harbor) not reproduced warm"
	}
	if strings.TrimSpace(p.ContainerEnv["TARGET_KIND"]) != "" {
		return "accelerator target kind not eligible"
	}
	if p.UseSandboxRuntime {
		return "sandbox runtime posture not verified warm"
	}
	// L-4 (fail-closed): `docker exec` does NOT honour the image's own
	// ENTRYPOINT/CMD, so a payload with no explicit Cmd cannot be exec'd warm.
	// (The old comment claimed exec honours the entrypoint: it does not.)
	if len(p.Cmd) == 0 {
		return "empty payload Cmd: docker exec cannot run the image entrypoint"
	}
	// The key is built from the RESOLVED digest of the image that actually
	// ran (Run's noteExecutedImageDigest inspect), NEVER from a mutable tag:
	// a moved tag retires the entry instead of reusing it. The caller passes
	// that digest and refuses warm when it is empty (inspect failed).
	return ""
}

// warmConfigFP fingerprints the FULL create-time config (network, binds, cap-drop,
// lockdown opts, pids, memory, cpu, shm, tmpfs keys, group-add, extra hosts,
// devices, readonly rootfs, lockdown user, env KEY set). Values never enter it.
func warmConfigFP(p transport.TaskPayload, hc *container.HostConfig, cfgUser string) string {
	strs := func(v []string) []string { if v == nil { return []string{} }; return v }
	tmpfsK := func(t map[string]string) []string {
		var k []string
		for key, opt := range t { k = append(k, key+"="+opt) }
		// Sorted AND WITH the mount options: Go randomises map iteration order
		// (unsorted made two identical payloads produce two containers), and
		// the tmpfs SIZE is create-time config a warm container can never
		// change (applyEphemeralResources rewrites these options), so a
		// different TmpfsMB must produce a different key, never a reuse.
		sort.Strings(k)
		return k
	}
	i64 := func(v *int64) string { return strconv.FormatInt(derefOr0(v), 10) }
	m := map[string]any{
		"net":         hc.NetworkMode,
		"binds":       strs(hc.Binds),
		"capdrop":     strs(hc.CapDrop),
		"secopt":      strs(hc.SecurityOpt),
		"groupadd":    strs(hc.GroupAdd),
		"extra_hosts": strs(hc.ExtraHosts),
		"tmpfs":       strs(tmpfsK(hc.Tmpfs)),
		"pids":        i64(hc.Resources.PidsLimit),
		"memory":      strconv.FormatInt(hc.Resources.Memory, 10),
		"nanocpus":    strconv.FormatInt(hc.Resources.NanoCPUs, 10),
		"shm":         strconv.FormatInt(hc.ShmSize, 10),
		"readonly":    strconv.FormatBool(hc.ReadonlyRootfs),
		"devices":     len(hc.Devices),
		"user":        cfgUser,
		"envkeys":     envKeySet(p.ContainerEnv),
	}
	var b bytes.Buffer
	if err := json.NewEncoder(&b).Encode(m); err != nil {
		return "fp-err"
	}
	var dg [32]byte = sha256.Sum256(b.Bytes())
	return hex.EncodeToString(dg[:])
}

func derefOr0(v *int64) int64 { if v == nil { return 0 }; return *v }

func envKeySet(env map[string]string) []string {
	var k []string
	for key := range env { k = append(k, key) }
	sort.Strings(k)
	return k
}

// warmEnvHash hashes the per-call env VALUES; a different hash retires the entry
// (env change = cold restart, per design). The list is SORTED before hashing:
// warmExecEnv builds it from a Go map (random iteration order), so an
// order-sensitive hash would differ between two IDENTICAL calls and retire
// the entry at random (measured: the same payload produced a new container on
// most re-acquires).
func warmEnvHash(env []string) string {
	var sorted = env
	sort.Strings(sorted)
	var b bytes.Buffer
	for _, kv := range sorted { b.WriteString(kv); b.WriteByte(0) }
	var dg [32]byte = sha256.Sum256(b.Bytes())
	return hex.EncodeToString(dg[:])
}

// warmNetworkFor resolves the network the SAME way the cold path does
// (SelectNetworkMode + ApplyEgressProxyMode + the fail-closed guard) and
// returns "" unless the verdict is the default-deny isolated posture — a
// allowlist/proxied verdict can never be warm (design R2).
func (e *Executor) warmNetworkFor(p transport.TaskPayload) string {
	netSel := SelectNetworkMode(p, e.network, e.userNetwork, e.egressNetwork)
	proxyMode := EffectiveEgressProxyMode(e.egressProxyModeForTask(), p)
	netSel = ApplyEgressProxyMode(netSel, proxyMode)
	if guard := _egressGuard(netSel, proxyMode); guard != nil { return "" }
	if netSel.FromAllowlist || netSel.Proxied { return "" }
	return netSel.DockerNetwork
}

// warmExecEnv assembles the per-call exec env: the SAME entries a cold create
// would get (payload env minus API_URL, the IO dir defaults, the worker-owned
// API_URL, the model cache choice, the per-task MinIO creds, the network
// verdict's env additions), with NO create-time env and NO Harbor/Vault/host
// env (platform tasks are ineligible). Creds and the model cache token live
// ONLY here — never in the container's create-time env, labels or Cmd.
func (e *Executor) warmExecEnv(p transport.TaskPayload, creds *transport.Credentials, digestImage string) []string {
	env := make([]string, 0, len(p.ContainerEnv)+5)
	for k, v := range p.ContainerEnv {
		if k == "API_URL" { continue }
		env = append(env, k+"="+v)
	}
	env = append(env, defaultIODirEnv(p.ContainerEnv)...)
	if e.apiURL != "" {
		mode := ""
		if e.tunables != nil {
			mode = e.tunables.String(internalTLSModeTunableKey, "", "", "off", "report", "require")
		}
		nodeAPIURL, err := decideNodeAPIURL(e.apiURL, mode)
		if err != nil { return env }
		reportPlainAPIURL(e.log, mode, nodeAPIURL)
		env = append(env, "API_URL="+nodeAPIURL)
		if caPath, _bind := internalCABundleMount(); caPath != "" {
			_ = _bind
			env = append(env, "MOD_INTERNAL_CA_BUNDLE="+caPath)
		}
	}
	var mErr error
	env, mErr = e.applyModelCacheEnv(context.Background(), env, taskModelRepoID(p), creds.ModelCacheToken, false)
	if mErr != nil { return env }
	if creds.MinIO.Endpoint != "" {
		endpoint := creds.MinIO.Endpoint
		if strings.HasPrefix(endpoint, "http://") { endpoint = endpoint[7:] } else if strings.HasPrefix(endpoint, "https://") { endpoint = endpoint[8:] }
		env = append(env,
			"MINIO_ENDPOINT="+endpoint,
			"MINIO_ACCESS_KEY="+creds.MinIO.AccessKey,
			"MINIO_SECRET_KEY="+creds.MinIO.SecretKey,
		)
		if creds.MinIO.SessionToken != "" {
			env = append(env, "MINIO_SESSION_TOKEN="+creds.MinIO.SessionToken)
		}
	}
	return env
}

// warmPool returns this worker's pool (created on first use).
func (e *Executor) warmPool() *WarmPool {
	if e.warmPoolRef == nil {
		e.warmPoolRef = &WarmPool{entries: map[string]*warmEntry{}, coldOnlyUntil: map[string]int64{}, instanceID: e.instanceID}
	}
	return e.warmPoolRef
}

// warmEnabled reads the platform kill switch (default OFF). The tenant-admin
// switch is enforced API-side (the payload only carries warm_policy when the
// tenant-scope row is true), so BOTH are required.
func (e *Executor) warmEnabled() bool {
	if e.tunables == nil {
		return false
	}
	return e.tunables.Bool(warmPoolEnabledTunableKey, "", false)
}

// warmMaxContainers is the per-worker SAFETY LIMIT (a technical limit, not a
// CE quantity cap). 0/negative falls back to the default 4.
func (e *Executor) warmMaxContainers() int {
	if e.tunables == nil {
		return 4
	}
	v := e.tunables.Int(warmPoolMaxContainersTunableKey, 4)
	if v < 1 { return 1 }
	return v
}

// warmAcquire finds or creates the pool container for this key. Creation bakes
// the FULL create-time posture (isolated network, cap-drop ALL, no-new-privileges,
// pids/memory, read-only rootfs + wiped tmpfs set, pool volume) and NO per-task
// env at all. The keep-alive init prints nothing (T9: the container log stays
// empty; call output only ever comes from the exec stream).
// warmHostConfig builds the warm container's HostConfig from the payload with
// the SAME helpers the cold path uses (nodeShmSizeBytes, applyNodeLockdown,
// applyHardenedSandbox, applyEphemeralResources), so the posture is identical
// to a cold create of the same payload. Eligibility already excluded GPU /
// devices / extra binds / proxied egress, so the isolated network + the pool
// volume bind are the only network/bind inputs.
// warmPosture returns the (Config, HostConfig, user) a cold create of THIS
// payload would get, with the per-task volume bind left out (the pool swaps
// its own in). The SAME lockdown layer runs here, so cfg.User is resolved and
// the fingerprint covers it.
func (e *Executor) warmPosture(p transport.TaskPayload, networkName string) (*container.Config, *container.HostConfig, string) {
	cfg := &container.Config{
		// Keep-alive init prints nothing; NO per-task env at creation (R3).
		Cmd: []string{"sh", "-c", "sleep 100000000"},
		Env: []string{},
	}
	hc := e.warmHostConfig(p, "pool-pending", networkName, cfg)
	applyNodeLockdown(cfg, hc, e.tunables, p)
	return cfg, hc, cfg.User
}

func (e *Executor) warmHostConfig(p transport.TaskPayload, poolVolume, networkName string,
	cfg *container.Config) *container.HostConfig {
	memoryLimit := defaultMemoryLimitBytes(e.log)
	if p.MemoryLimitMB != 0 { memoryLimit = int64(p.MemoryLimitMB) * 1024 * 1024 }
	if memoryLimit < 0 { memoryLimit = 0 }
	cpuLimit := float64(goruntime.NumCPU())
	if env := os.Getenv("MOD_WORKER_DEFAULT_CPU_LIMIT"); env != "" {
		if v, err := strconv.ParseFloat(env, 64); err == nil && v != 0 { cpuLimit = v }
	}
	if p.CPULimit != 0 { cpuLimit = p.CPULimit }
	pidsLimit := int64(4096)
	if p.PidsLimit > 0 { pidsLimit = p.PidsLimit }
	hc := &container.HostConfig{
		NetworkMode: container.NetworkMode(networkName),
		Binds:       []string{poolVolume + ":/data:rw"},
		ShmSize:     nodeShmSizeBytes(),
		Resources:   container.Resources{Memory: memoryLimit, NanoCPUs: int64(cpuLimit*1e9), PidsLimit: &pidsLimit},
		CapDrop:     []string{"ALL"},
		Privileged:  false,
		SecurityOpt: []string{"no-new-privileges"},
	}
	// The token-URL host mapping the cold path adds whenever the task has one.
	if _, ok := p.ContainerEnv["MOD_TASK_TOKEN_URL"]; ok {
		hc.ExtraHosts = append(hc.ExtraHosts, "host.docker.internal:host-gateway")
	}
	applyHardenedSandbox(hc, hardenedSandboxEnabled())
	// T3: the writable layer must not carry files between calls. The tmpfs set
	// is mounted FIRST so applyEphemeralResources (which rewrites the /tmp
	// tmpfs OPTIONS with the payload's TmpfsMB) sees it: mounted AFTER, a
	// warm create silently dropped the tmpfs size a cold create would have
	// set (and the config fingerprint could not tell the payloads apart).
	hc.ReadonlyRootfs = true
	if hc.Tmpfs == nil { hc.Tmpfs = map[string]string{} }
	for _, d := range []string{"/tmp", "/var/tmp", "/dev/shm", "/home"} {
		hc.Tmpfs[d] = ""
	}
	if p.ShmSizeMB > 0 || p.TmpfsMB > 0 {
		applyEphemeralResources(hc, p.ShmSizeMB, p.TmpfsMB)
	}
	return hc
}

func (e *Executor) warmAcquire(ctx context.Context, taskID int, tenantID int,
	digestImage string, p transport.TaskPayload, policy *transport.WarmPolicy,
	networkName string, env []string) (*warmEntry, error) {

	pool := e.warmPool()
	// The fingerprint is computed off the SAME HostConfig a cold create of
	// this payload would get (minus the per-task volume bind, which the pool
	// replaces with its own), so any change to egress/network/limits/shm/tmpfs/
	// user produces a different key and a NEW container, never a reuse.
	cfg, hc, cfgUser := e.warmPosture(p, networkName)
	key := warmKeyParts{tenantID: tenantID, digest: digestImage,
		nodeKind: strings.TrimSpace(p.NodeKind),
		configFP: warmConfigFP(p, hc, cfgUser)}.str()
	hash := warmEnvHash(env)

	pool.mu.Lock()
	if until := pool.coldOnlyUntil[warmCooldownKey(tenantID, digestImage)]; until > 0 && time.Now().Unix() < until {
		pool.mu.Unlock()
		return nil, fmt.Errorf("warm pool cooling down for this node")
	}
	if ent := pool.entries[key]; ent != nil {
		if ent.envHash != hash {
			// env change since the entry was created: retire, run cold.
			pool.entries[key] = nil
			pool.mu.Unlock()
			e.warmRetire(ent, "env hash changed")
			return nil, fmt.Errorf("warm entry retired: env changed")
		}
		if ent.busy {
			// L-2 CONCURRENCY: this container is mid-call for another task.
			// One container, one call at a time: the second caller runs COLD
			// rather than sharing the container (no cross-call interleaving of
			// files, env or processes).
			pool.mu.Unlock()
			return nil, fmt.Errorf("warm entry busy; running cold")
		}
		if ent.calls >= ent.maxCalls {
			pool.entries[key] = nil
			pool.mu.Unlock()
			e.warmRetire(ent, "max_calls reached")
			return nil, fmt.Errorf("warm entry retired at max_calls")
		}
		if time.Now().Sub(ent.lastUse).Seconds() > float64(ent.idleSeconds) {
			pool.entries[key] = nil
			pool.mu.Unlock()
			e.warmRetire(ent, "idle timeout")
			return nil, fmt.Errorf("warm entry retired: idle")
		}
		ent.busy = true
		pool.mu.Unlock()
		return ent, nil
	}
	if activeEntries(pool.entries) >= e.warmMaxContainers() {
		// SAFETY LIMIT: evict the least-recently-used entry. L-2: a BUSY entry
		// (mid-call for another task) is never evicted — evicting it would yank
		// the container out from under a live call. If every entry is busy the
		// caller simply runs cold.
		var victim *warmEntry
		for _, ent := range pool.entries {
			if ent == nil || ent.busy { continue }
			if victim == nil || ent.lastUse.Before(victim.lastUse) { victim = ent }
		}
		if victim != nil {
			pool.entries[victim.key] = nil
			pool.mu.Unlock()
			e.warmRetire(victim, "warm pool safety limit")
			return nil, fmt.Errorf("warm pool at safety limit; evicted LRU, retry cold")
		}
	}
	pool.mu.Unlock()

	// The pool volume, labelled so the sweep finds it.
	volName := warmVolumeName(p.NodeKind)
	if _, err := e.docker.VolumeCreate(ctx, volume.CreateOptions{
		Name: volName,
		Labels: map[string]string{
			warmPoolLabel:      "1",
			sidecarWorkerLabel: e.workerName,
			sidecarInstanceLabel: e.instanceID,
		},
	}); err != nil {
		return nil, fmt.Errorf("warm pool volume: %w", err)
	}

	// The pool volume replaces the per-task bind; the fingerprint never includes
	// either bind, so swapping it cannot change the key.
	hc.Binds = []string{volName + ":/data:rw"}
	cfg.Image = digestImage
	cfg.Labels = e.warmContainerLabels(taskID)

	resp, err := e.docker.ContainerCreate(ctx, cfg, hc, &network.NetworkingConfig{}, nil, "")
	if err != nil {
		e.docker.VolumeRemove(context.Background(), volName, true)
		return nil, fmt.Errorf("warm container create: %w", err)
	}
	id := resp.ID
	if err := e.docker.ContainerStart(ctx, id, container.StartOptions{}); err != nil {
		e.docker.ContainerRemove(context.Background(), id, container.RemoveOptions{Force: true})
		e.docker.VolumeRemove(context.Background(), volName, true)
		return nil, fmt.Errorf("warm container start: %w", err)
	}

	// L-7: capture the keep-alive init's own process count with the SAME ps
	// pipeline the check uses, so the check compares like-for-like.
	pidBase, perr := e.warmPidCheck(ctx, id)
	if perr != nil {
		e.warmRetire(&warmEntry{containerID: id, volumeName: volName, key: key,
			envHash: hash, calls: 0, idleSeconds: policy.IdleSeconds, maxCalls: policy.MaxCalls,
			lastUse: time.Now()}, "pid baseline probe failed")
		return nil, fmt.Errorf("warm pid baseline probe: %w", perr)
	}

	pool.mu.Lock()
	if activeEntries(pool.entries) >= e.warmMaxContainers() {
		pool.mu.Unlock()
		e.warmRetire(&warmEntry{containerID: id, volumeName: volName, key: key,
			envHash: hash, calls: 0, idleSeconds: policy.IdleSeconds, maxCalls: policy.MaxCalls,
			pidBaseline: pidBase, busy: true, lastUse: time.Now()}, "warm pool safety limit")
		return nil, fmt.Errorf("warm pool at safety limit; retry cold")
	}
	ent := &warmEntry{containerID: id, volumeName: volName, key: key,
		envHash: hash, calls: 0, idleSeconds: policy.IdleSeconds, maxCalls: policy.MaxCalls,
		pidBaseline: pidBase, busy: true, lastUse: time.Now()}
	pool.entries[key] = ent
	pool.mu.Unlock()
	e.log.Info("warm container started", "container", id, "volume", volName,
		"node_kind", p.NodeKind, "digest", digestImage)
	return ent, nil
}

func activeEntries(m map[string]*warmEntry) int {
	var n int
	for _, ent := range m { if ent != nil { n++ } }
	return n
}

// warmVolumeName is the pool volume's name: task-volume-prefixed so the generic
// volume sweeper can age it out if the worker dies before the label sweep runs.
func warmVolumeName(nodeKind string) string {
	return taskVolumePrefix + "warm-" + sanitizeProject(nodeKind) + "-" + randHex8()
}

// randHex8 is a fresh 8-byte hex suffix (crypto/rand, same source as the task
// volume names).
func randHex8() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil { return "0" }
	return hex.EncodeToString(b[:])
}

// warmContainerLabels: worker identity + the warm-pool label, so a cleanup pass
// finds orphans by label plus tenant and digest (design 4).
func (e *Executor) warmContainerLabels(taskID int) map[string]string {
	l := e.sidecarLabels()
	l[warmPoolLabel] = "1"
	l["mod.warm.task"] = strconv.Itoa(taskID)
	return l
}

// warmRetire stops+removes the container AND removes the pool volume. Fail-closed
// everywhere: a retire failure WARNs (the label sweep is the backstop).
func (e *Executor) warmRetire(ent *warmEntry, reason string) {
	e.log.Warn("warm container retired", "reason", reason,
		"container", ent.containerID, "volume", ent.volumeName, "calls", ent.calls)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	e.docker.ContainerStop(ctx, ent.containerID, container.StopOptions{})
	e.docker.ContainerRemove(ctx, ent.containerID, container.RemoveOptions{Force: true})
	e.docker.VolumeRemove(ctx, ent.volumeName, true)
}

// warmRetireEntry retires the container AND drops it from the pool map, so a
// dead entry can NEVER be handed to a later call (fail-closed: warmRetire alone
// left the entry in the map, and the next acquire for the key would have been
// served a removed container).
func (e *Executor) warmRetireEntry(ent *warmEntry, reason string) {
	pool := e.warmPool()
	pool.mu.Lock()
	if cur := pool.entries[ent.key]; cur == ent {
		pool.entries[ent.key] = nil
	}
	pool.mu.Unlock()
	e.warmRetire(ent, reason)
}

// warmMarkColdOnly records a warm INFRA failure so the SAME tenant+digest runs
// cold for the cooldown window (the retry under max_attempts 3 is cold).
// L-5: keyed by warmCooldownKey — the SAME key warmAcquire checks (the old
// call site passed the bare digest while the check looked up the composite
// key, so the cooldown never actually fired).
func (e *Executor) warmMarkColdOnly(tenantID int, digest string) {
	pool := e.warmPool()
	pool.mu.Lock()
	pool.coldOnlyUntil[warmCooldownKey(tenantID, digest)] = time.Now().Unix() + 120
	pool.mu.Unlock()
}

// warmSweepOrphans removes warm containers/volumes left by a previous worker
// instance (T13). Guarded by identity exactly like the sidecar sweep: only
// resources carrying THIS worker's name but a DIFFERENT instance id are removed.
func (e *Executor) warmSweepOrphans(ctx context.Context) int {
	var removed int
	var list container.ListOptions
	list.All = true
	list.Filters = filters.NewArgs(filters.Arg("label", warmPoolLabel))
	cs, err := e.docker.ContainerList(ctx, list)
	if err != nil {
		e.log.Warn("warm orphan sweep: container list failed", "error", err)
		return 0
	}
	for _, c := range cs {
		if c.Labels[warmPoolLabel] != "1" { continue }
		if c.Labels[sidecarInstanceLabel] == e.instanceID { continue }
		if c.Labels[sidecarWorkerLabel] != e.workerName { continue }
		e.docker.ContainerRemove(ctx, c.ID, container.RemoveOptions{Force: true})
		removed++
	}
	vs, verr := e.docker.VolumeList(ctx, volume.ListOptions{Filters: filters.NewArgs(filters.Arg("label", warmPoolLabel))})
	if verr == nil {
		for _, v := range vs.Volumes {
			if v.Labels[warmPoolLabel] != "1" { continue }
			if v.Labels[sidecarInstanceLabel] == e.instanceID { continue }
			if v.Labels[sidecarWorkerLabel] != e.workerName { continue }
			e.docker.VolumeRemove(ctx, v.Name, true)
			removed++
		}
	}
	if removed > 0 {
		e.log.Info("warm orphan sweep removed containers/volumes", "removed", removed)
	}
	return removed
}

// warmWipeVerify wipes /data/input + /data/output AND the tmpfs set the warm
// create mounted (L-1: /tmp, /var/tmp, /dev/shm, /home — the writable layer is
// read-only so those mounts are the ONLY places a call can leave a file;
// wiping their CONTENTS, never the mount points, closes the cross-call leak
// inside one tenant), re-seeds the IO dirs at 0777 uid 0, VERIFIES empty with
// find (dotfiles, symlinks, special files, hardlink targets included) and
// renders the SAME `ls -ld` text the legacy prep helper printed, so the
// "data volume prepared" line stays byte-identical.
// A non-empty verify result is an ERROR (the caller retires, never retries).
func (e *Executor) warmWipeVerify(ctx context.Context, containerID string) (string, error) {
	script := `rm -rf /data/input /data/output /tmp/* /tmp/.[!.]* /tmp/..?* ` +
		`/var/tmp/* /var/tmp/.[!.]* /var/tmp/..?* /dev/shm/* /dev/shm/.[!.]* /dev/shm/..?* ` +
		`/home/* /home/.[!.]* /home/..?* 2>/dev/null; ` +
		`mkdir -p /data/input /data/output; chmod 0777 /data/input /data/output; ` +
		`n=$(find /data/input /data/output /tmp /var/tmp /dev/shm /home -mindepth 1 2>/dev/null | wc -l); ` +
		`if [ "$n" != 0 ]; then echo "WARMWIPEFAIL $n"; else echo "prep OK: $(ls -ld /data /data/input /data/output | tr '\n' ' | ')"; fi`
	out, err := e.warmExecSh(ctx, containerID, script)
	if err != nil { return "", err }
	if strings.HasPrefix(out, "WARMWIPEFAIL") {
		return "", fmt.Errorf("warm pool volume not empty after wipe: %s", out)
	}
	if !strings.HasPrefix(out, "prep OK: ") {
		return "", fmt.Errorf("warm wipe verify unexpected output: %s", out)
	}
	return strings.TrimSpace(strings.TrimPrefix(out, "prep OK: ")), nil
}

// warmExecSh runs one shell one-liner in the warm container. L-3: User "0" is
// set EXPLICITLY — the container's default user is the NON-ROOT node uid that
// applyNodeLockdown resolved at create, so without it the node could leave
// files the wipe could not delete (and the wipe would fail-closed retire on
// every call). The housekeeping execs are MOD's own, run as root.
func (e *Executor) warmExecSh(ctx context.Context, containerID, script string) (string, error) {
	resp, err := e.docker.ContainerExecCreate(ctx, containerID, container.ExecOptions{
		Cmd: []string{"sh", "-c", script}, User: "0", AttachStdout: true, AttachStderr: true})
	if err != nil { return "", err }
	r, aerr := e.docker.ContainerExecAttach(ctx, resp.ID, container.ExecAttachOptions{})
	if aerr != nil { return "", aerr }
	var so, se bytes.Buffer
	stdcopy.StdCopy(&so, &se, r.Reader)
	r.Close()
	ins, ierr := e.docker.ContainerExecInspect(ctx, resp.ID)
	if ierr != nil { return "", ierr }
	if ins.ExitCode != 0 {
		return "", fmt.Errorf("warm exec exit %d: %s", ins.ExitCode, so.String()+se.String())
	}
	return strings.TrimSpace(so.String()), nil
}

// warmPidCheck counts processes in the container; anything besides the keep-alive
// init (and the ps itself) means a detached process survived the call (T5) ->
// the caller retires.
func (e *Executor) warmPidCheck(ctx context.Context, containerID string) (int, error) {
	out, err := e.warmExecSh(ctx, containerID, `ps -e -o pid= | wc -l`)
	if err != nil { return -1, err }
	n := 0
	if v, perr := strconv.Atoi(strings.TrimSpace(out)); perr == nil { n = v }
	return n, nil
}

// warmRunExec runs ONE call: pre-wipe, exec the node's Cmd with the per-call
// env, stream its own stdout/stderr (live batches + final tail, same lines as
// cold), read the exit code, post-wipe, pid check. Any failure retires.
// Returns (logTail, exitCode, oomKilled). An OOM kill takes the whole warm
// container (T7): the caller retires and the retry runs cold.
func (e *Executor) warmRunExec(ctx context.Context, ent *warmEntry, p transport.TaskPayload,
	env []string, timeout time.Duration, cb Callbacks) (string, int, bool, error) {

	containerID := ent.containerID
	if _, err := e.warmWipeVerify(ctx, containerID); err != nil {
		// A failed pre-call wipe means isolation cannot be guaranteed for this
		// container (or the container is dead): retire it, never hand it to
		// another call.
		e.warmRetireEntry(ent, "pre-call wipe failed")
		return "", -1, false, err
	}

	resp, err := e.docker.ContainerExecCreate(ctx, containerID, container.ExecOptions{
		Cmd: warmCmdFor(p), Env: env, AttachStdout: true, AttachStderr: true})
	if err != nil {
		// The container is not in a state that can serve a call (died between
		// acquire and exec, or the daemon refused): retire it, never hand it
		// to another call.
		e.warmRetireEntry(ent, "exec create failed")
		return "", -1, false, err
	}
	exID := resp.ID

	r, aerr := e.docker.ContainerExecAttach(ctx, exID, container.ExecAttachOptions{})
	if aerr != nil {
		e.warmRetireEntry(ent, "exec attach failed")
		return "", -1, false, aerr
	}

	// Live tail: identical shape to the cold stream (same centralLogLine rule,
	// same OnLogBatch batching), fed from the EXEC stream.
	var linesMu sync.Mutex
	var allLines []string
	lineCh := make(chan string, 256)
	var wg sync.WaitGroup
	// streamDone fires when the log-scanning goroutine has consumed the whole
	// exec stream. warmRunExec WAITS for it before returning: the worker's
	// per-line log writes must not land after the call returns (they would
	// interleave with the caller's own next log line, and a reader of the
	// log buffer would race the writer).
	var streamDone chan bool = make(chan bool)
	go func() {
		defer r.Close()
		stdoutR, stdoutW := io.Pipe()
		stderrR, stderrW := io.Pipe()
		go func() {
			defer stdoutW.Close()
			defer stderrW.Close()
			stdcopy.StdCopy(stdoutW, stderrW, r.Reader)
		}()
		scanOne := func(rd io.Reader, stream string) {
			defer wg.Done()
			sc := bufio.NewScanner(rd)
			sc.Buffer(make([]byte, 64*1024), 1024*1024)
			for sc.Scan() {
				line := sc.Text()
				if line == "" { continue }
				e.log.Info("container", "stream", stream, "log", centralLogLine(line))
				lineCh <- line
			}
		}
		wg.Add(2)
		go scanOne(stdoutR, "stdout")
		go scanOne(stderrR, "stderr")
		go func() { wg.Wait(); close(lineCh) }()
		batch := make([]string, 0, 32)
		flush := func() {
			if len(batch) == 0 || cb.OnLogBatch == nil { batch = batch[:0]; return }
			cp := make([]string, len(batch))
			copy(cp, batch)
			cb.OnLogBatch(cp)
			batch = batch[:0]
		}
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case l, ok := <-lineCh:
				if !ok { flush(); r.Close(); streamDone <- true; return }
				linesMu.Lock(); allLines = append(allLines, l); linesMu.Unlock()
				batch = append(batch, l)
				if len(batch) >= 32 { flush() }
			case <-ticker.C:
				flush()
			}
		}
	}()

	// docker exec has NO kill API (T6): a timed-out call cannot stop the exec'd
	// process, so the WHOLE container is stopped+removed (retire) and the retry
	// runs cold.
	deadline := time.Now().Add(timeout)
	var ins container.ExecInspect
	for {
		insTry, ierr := e.docker.ContainerExecInspect(ctx, exID)
		if ierr != nil {
			if time.Now().After(deadline) {
				e.warmRetireEntry(ent, "warm exec wait error")
				return "", -1, false, fmt.Errorf("warm exec wait error: %w", ierr)
			}
			time.Sleep(200 * time.Millisecond)
			continue
		}
		if insTry.Running {
			if time.Now().After(deadline) {
				// docker exec has NO kill API (T6): a timed-out call cannot stop
				// the exec'd process, so the WHOLE container is stopped+removed
				// HERE (retire) and the retry runs cold.
				e.warmRetireEntry(ent, "warm exec timeout")
				return "", -1, false, fmt.Errorf("warm exec timeout: container retired")
			}
			time.Sleep(200 * time.Millisecond)
			continue
		}
		ins = insTry
		break
	}

	// Drain the log stream before returning (bounded): the exec has settled,
	// so the attach stream ends with it; the worker's per-line log writes
	// must all land BEFORE this call returns, never interleaved with the
	// caller's next line. On the timeout paths the stream may stay open (the
	// process is still running) — those paths retire the container instead.
	select {
	case <-streamDone:
	case <-time.After(30 * time.Second):
	}

	// OOM verdict: the CONTAINER (not the exec) carries it.
	oom := false
	if cj, cerr := e.docker.ContainerInspect(ctx, containerID); cerr == nil {
		if st := cj.State; st != nil { oom = st.OOMKilled }
	}
	if oom {
		// An OOM kill takes the whole warm container (T7): retire it here so
		// the entry can never be handed to another call; the retry runs cold.
		e.warmRetireEntry(ent, "warm container OOM-killed")
		return "", ins.ExitCode, true, fmt.Errorf("warm container OOM-killed")
	}

	linesMu.Lock()
	tail := tailLines(allLines, 5000)
	linesMu.Unlock()
	return tail, ins.ExitCode, false, nil
}

// warmFinishCall is the post-call half of a warm call: wipe + verify the pool
// volume empty and check for surviving processes. Run() calls it on EVERY return
// path (success, scan refusal, upload failure) via a defer, so a call never
// returns its outputs to the next call's container. Any failure retires.
func (e *Executor) warmFinishCall(ent *warmEntry) error {
	// If the entry was already retired (timeout/OOM/attach paths dropped it),
	// there is nothing to finish: never wipe or retire a dead container twice.
	{
		pool := e.warmPool()
		pool.mu.Lock()
		var live = pool.entries[ent.key] == ent
		pool.mu.Unlock()
		if !live { return nil }
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if _, err := e.warmWipeVerify(ctx, ent.containerID); err != nil {
		e.warmRetireEntry(ent, "post-call wipe failed")
		return err
	}
	// L-7: compare against the baseline captured at acquire (the keep-alive
	// init's own sh+sleep, plus the sh+ps+wc this probe itself spawns).
	// Anything ABOVE that count is a process call 1 detached (T5) -> retire.
	if n, perr := e.warmPidCheck(ctx, ent.containerID); perr != nil || n > ent.pidBaseline {
		e.warmRetireEntry(ent, "processes survived the call")
		return fmt.Errorf("warm container has %d processes after the call (baseline %d)",
			n, ent.pidBaseline)
	}
	pool := e.warmPool()
	pool.mu.Lock()
	if cur := pool.entries[ent.key]; cur == ent {
		ent.calls++
		ent.lastUse = time.Now()
	}
	ent.busy = false
	pool.mu.Unlock()
	return nil
}

// warmCmdFor is the node's command exactly as a cold create would run it
// (payload.Cmd; empty = the image's own entrypoint, which exec honours too).
func warmCmdFor(p transport.TaskPayload) []string {
	if len(p.Cmd) > 0 { return p.Cmd }
	return []string{}
}

func tailLines(lines []string, n int) string {
	if len(lines) > n { lines = lines[len(lines)-n:] }
	return strings.Join(lines, "\n")
}

