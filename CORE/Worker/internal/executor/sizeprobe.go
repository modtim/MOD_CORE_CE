package executor

// sz-tcp.16b — size-probe hook (sz-tiered-credit-pricing §12).
//
// Runs the size-probe sidecar (CORE/DockerContainers/size-probe, sz-tcp.16a)
// over a task's downloaded inputs BEFORE the node container starts, and caches
// its JSON in Redis under `size_probe:<sha256 of the sorted input digests>` with
// a 24h TTL so the same inputs never pay the probe twice. The probe is a PRICING
// aid (the API's cost_estimator falls back to the medium tier), so EVERY failure
// path — no cache wiring, pull failure, create/start/wait failure, non-zero exit,
// unparseable stdout, cache error — logs and returns nil. A task must never fail
// because its probe failed.
//
// probe.py always exits 0 (a bad file gets {"path","bytes","family","error"}
// instead of a size), so a non-zero exit here means the CONTAINER failed, not
// the input.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"

	ocispec "github.com/opencontainers/image-spec/specs-go/v1"

	"github.com/redis/go-redis/v9"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/auth"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const sizeProbeKeyPrefix = "size_probe:"
const sizeProbeCacheTTL = 24 * time.Hour
const sizeProbeIdleWindow = 45 * time.Second
const sizeProbeImageSuffix = "/modtim/size-probe:latest"

// --- SIZE-PROBE-CONTAINER-LOCKDOWN (posture) --------------------------------
// The probe reads UNTRUSTED tenant input files, so it carries the same
// container-level lockdown posture the platform applies to node containers
// (node_lockdown.go / executor.go #85 / egress_proxy.go / stacksecretbind.go):
// no network, all capabilities dropped, no privilege escalation, read-only
// rootfs. The probe needs NO network: it exits 0 with byte-identical JSON
// under `--network none` (proven on gpu5 in SIZE-PROBE-IMAGE-CLOSE). The
// dockerNetwork parameter stays in the signature (the async wrapper and the
// call site pass it) but is deliberately NOT applied any more.
//
// The Resources limits are TECHNICAL SAFETY limits, NOT product caps (owner
// rule: no quantity caps): the probe stats a handful of small files, so
// anything approaching 512 MiB / 256 processes / 2 cores means a runaway.
// They are generous by design and never bind a legitimate probe.
const sizeProbeMemoryBytes = int64(512 * 1024 * 1024) // 512 MiB
const sizeProbeNanoCPUs = int64(2 * 1000 * 1000 * 1000) // 2 cores
const sizeProbePidsLimit = int64(256)
// ReadonlyRootfs + a small writable /tmp: probe.py itself never writes, but
// ffmpeg/ffprobe (video/audio families) may scratch to /tmp; the tmpfs keeps
// those families working without making the image layers writable.
const sizeProbeTmpfsOpts = "rw,size=64m,mode=1777"

// sizeProbeCacheKey is the cache key for a set of input digests: sha256 over the
// sorted, de-duplicated digests joined by ','. Sorted so the same input set
// arriving in any order hits one key. Empty digest set -> "" (nothing to probe).
func sizeProbeCacheKey(hashes []string) string {
	seen := make(map[string]bool, len(hashes))
	var sorted []string
	for _, h := range hashes {
		h = strings.TrimSpace(h)
		if h == "" || seen[h] {
			continue
		}
		seen[h] = true
		sorted = append(sorted, h)
	}
	if len(sorted) == 0 {
		return ""
	}
	sort.Strings(sorted)
	return sizeProbeKeyPrefix + hashForProbeKey(sorted)
}

func hashForProbeKey(sorted []string) string {
	joined := strings.Join(sorted, ",")
	sum := sha256.Sum256([]byte(joined))
	return hex.EncodeToString(sum[:])
}

// dockerProbeAPI is the narrow slice of the docker client the probe needs, so
// tests stub it — same pattern as egress_proxy.go's dockerAPI.
type dockerProbeAPI interface {
	ContainerCreate(ctx context.Context, config *container.Config, hostConfig *container.HostConfig, networkingConfig *network.NetworkingConfig, platform *ocispec.Platform, containerName string) (container.CreateResponse, error)
	ContainerStart(ctx context.Context, containerID string, options container.StartOptions) error
	ContainerRemove(ctx context.Context, containerID string, options container.RemoveOptions) error
	ContainerKill(ctx context.Context, containerID, signal string) error
	ContainerWait(ctx context.Context, containerID string, condition container.WaitCondition) (<-chan container.WaitResponse, <-chan error)
	ContainerLogs(ctx context.Context, container string, options container.LogsOptions) (io.ReadCloser, error)
	ImagePull(ctx context.Context, ref string, options image.PullOptions) (io.ReadCloser, error)
}

var _ dockerProbeAPI = (*client.Client)(nil)

// sizeProbeStdout is the probe container's stdout, demuxed from docker's
// multiplexed log stream. probe.py writes the JSON doc to stdout only, so
// stderr is deliberately dropped (a warning on stderr must not corrupt the
// cached doc).
func sizeProbeStdout(ctx context.Context, d dockerProbeAPI, containerID string) string {
	reader, err := d.ContainerLogs(ctx, containerID, container.LogsOptions{
		ShowStdout: true,
		ShowStderr: false,
		Tail:       "all",
	})
	if err != nil {
		return ""
	}
	defer reader.Close()

	var raw bytes.Buffer
	_, _ = io.Copy(&raw, reader)
	var stdout, stderr bytes.Buffer
	stdcopy.StdCopy(&stdout, &stderr, bytes.NewBuffer(raw.Bytes()))
	if out := stdout.String(); out != "" {
		return out
	}
	return raw.String()
}

// sizeProbeCache is the installed cache hook (nil = no cache: the probe still
// runs, results just are not stored). Interface-of-methods so tests install a
// fake without a live Redis — the same mocking pattern the executor already
// uses for docker (egress_proxy.go's dockerAPI) and for the token server's
// RunTokenMinter.
type sizeCacheOps interface {
	Lookup(ctx context.Context, key string) (val string, err error)
	Store(ctx context.Context, key, val string) error
}

var sizeProbeCache sizeCacheOps

func SetSizeProbeCache(c sizeCacheOps) { sizeProbeCache = c }

// redisSizeCache adapts the worker's own go-redis client (the same `rdb`
// main.go builds from cfg.RedisURL / REDIS_URL — no new env var) to sizeCacheOps.
// redis.Nil (key absent) is a miss, not an error.
type redisSizeCache struct {
	rdb *redis.Client
}

func (c *redisSizeCache) Lookup(ctx context.Context, key string) (string, error) {
	if c.rdb == nil {
		return "", fmt.Errorf("size-probe cache: no redis client")
	}
	cmd := c.rdb.Get(ctx, key)
	if err := cmd.Err(); err != nil && err != redis.Nil {
		return "", err
	}
	return cmd.Val(), nil
}

func (c *redisSizeCache) Store(ctx context.Context, key, val string) error {
	if c.rdb == nil {
		return fmt.Errorf("size-probe cache: no redis client")
	}
	return c.rdb.Set(ctx, key, val, sizeProbeCacheTTL).Err()
}

func NewRedisSizeCache(rdb *redis.Client) *redisSizeCache {
	return &redisSizeCache{rdb: rdb}
}

// sizeProbeLogger is the executor's slog logger, injected next to where the
// executor is built (main.go: SetSizeProbeLogger(exec.Log)). Package-local so
// tests can call SizeProbe without standing up a full Executor.
var sizeProbeLogger *slog.Logger

func SetSizeProbeLogger(l *slog.Logger) { sizeProbeLogger = l }

func probeLogWarn(msg string, kv ...interface{}) {
	l := sizeProbeLogger
	if l == nil {
		l = slog.Default()
	}
	l.Warn(msg, kv...)
}

func probeLogInfo(msg string, kv ...interface{}) {
	l := sizeProbeLogger
	if l == nil {
		l = slog.Default()
	}
	l.Info(msg, kv...)
}

// SizeProbeDoc is one parsed probe document: Raw is the probe's JSON verbatim
// (what gets cached and what the API's cost estimator consumes), Probes is the
// decoded view for the worker's own use.
type SizeProbeDoc struct {
	Raw    string
	Probes []ProbeEntry
}

type ProbeEntry struct {
	Path   string
	Bytes  int64
	Family string
	Format string
	Unit   float64
	Note   string
	Err    string
}

// parseSizeProbeDoc validates that raw stdout carries the
// {"probes":[{path,bytes,family,format,size_unit_value,...},...]} document probe.py
// emits, tolerating leading/trailing noise. Returns the doc verbatim + decoded,
// or nil when it is not a probe document.
func parseSizeProbeDoc(raw string) *SizeProbeDoc {
	trimmed := strings.TrimSpace(raw)
	start := strings.IndexByte(trimmed, '{')
	end := strings.LastIndexByte(trimmed, '}')
	if start < 0 || end <= start {
		return nil
	}
	doc := trimmed[start : end+1]
	var parsed struct {
		Probes []struct {
			Path          string  `json:"path"`
			Bytes         int64   `json:"bytes"`
			Family        string  `json:"family"`
			Format        string  `json:"format"`
			SizeUnitValue float64 `json:"size_unit_value"`
			Note          string  `json:"note"`
			Error         string  `json:"error"`
		} `json:"probes"`
	}
	if err := json.Unmarshal([]byte(doc), &parsed); err != nil {
		return nil
	}
	out := &SizeProbeDoc{Raw: doc}
	for _, p := range parsed.Probes {
		out.Probes = append(out.Probes, ProbeEntry{
			Path: p.Path, Bytes: p.Bytes,
			Family: p.Family, Format: p.Format,
			Unit: p.SizeUnitValue, Note: p.Note, Err: p.Error,
		})
	}
	return out
}

// sizeProbeImage is the probe sidecar's Harbor ref — the same modtim/<name>
// pattern the worker already uses for its other sidecars (minio-client, the
// egress proxy), resolved from the per-task Vault-sourced Harbor host. No new
// env var: the host is creds.Harbor.Registry, already delivered with every task
// claim.
func sizeProbeImage(harborReg string) string {
	if harborReg == "" {
		return ""
	}
	return strings.TrimRight(harborReg, "/") + sizeProbeImageSuffix
}

// sizeProbeInputHashes is the task's input identity: the content digests the
// API attached to the task (transport.InputFiles[].ContentHash), plus the
// single SourceFileLocation when the server still sets one. Order-independent —
// the key sorts them.
func sizeProbeInputHashes(payload transport.TaskPayload) []string {
	var hashes []string
	if src := strings.TrimSpace(payload.SourceFileLocation); src != "" {
		hashes = append(hashes, src)
	}
	for _, f := range payload.InputFiles {
		if h := strings.TrimSpace(f.ContentHash); h != "" {
			hashes = append(hashes, h)
		}
	}
	return hashes
}

// SizeProbe is the hook: run the size-probe container for a task's inputs
// (unless the Redis cache already has the answer) and return the parsed
// document, or nil on ANY failure. Never fails a task, never panics.
//
// Wiring (executor.go Run, after the download step, before the processing
// container) — one call site, kept tiny:
//
//	if doc := SizeProbe(pipelineCtx, task, volName, creds); doc != nil {
//	    e.log.Info("size-probe ok", "probes", len(doc.Probes), "raw", doc.Raw)
//	}
func SizeProbe(ctx context.Context, task *transport.Task, creds *transport.Credentials, dockerNetwork, localRegistry, volName string, d dockerProbeAPI) *SizeProbeDoc {
	p := task.Payload

	// Nothing downloaded -> nothing to probe. Same gate the download step uses.
	if p.SkipDownload || (p.SourceFileLocation == "" && len(p.InputFiles) == 0) {
		return nil
	}

	key := sizeProbeCacheKey(sizeProbeInputHashes(p))
	if key == "" {
		return nil
	}

	if sizeProbeCache != nil {
		cached, cacheErr := sizeProbeCache.Lookup(ctx, key)
		if cacheErr != nil {
			probeLogWarn("size-probe cache lookup failed", "key", key, "error", cacheErr)
		} else if cached != "" {
			if doc := parseSizeProbeDoc(cached); doc != nil {
				probeLogInfo("size-probe cache hit", "key", key, "probes", len(doc.Probes))
				return doc
			}
			probeLogWarn("size-probe cache entry unparseable, re-probing", "key", key)
		}
	}

	probeImage := sizeProbeImage(harborRegistryOrEmpty(creds.Harbor))
	if probeImage == "" {
		probeLogWarn("size-probe skipped: no harbor registry for the probe image")
		return nil
	}

	doc := runSizeProbe(ctx, probeImage, dockerNetwork, localRegistry, volName, creds, d)
	if doc == nil {
		return nil
	}
	if sizeProbeCache != nil {
		if err := sizeProbeCache.Store(ctx, key, doc.Raw); err != nil {
			probeLogWarn("size-probe cache store failed", "key", key, "error", err)
		} else {
			probeLogInfo("size-probe cached", "key", key, "probes", len(doc.Probes))
		}
	}
	return doc
}

func harborRegistryOrEmpty(harbor transport.HarborCredentials) string {
	reg, err := harborRegistry(harbor)
	if err != nil {
		return ""
	}
	return reg
}

// sizeProbeHostConfig builds the probe helper's HostConfig, split out so tests
// can assert the lockdown (network none, CapDrop ALL, no-new-privileges,
// read-only rootfs + /tmp tmpfs, safety limits, ro /data bind) without a docker
// daemon — the same split stacksecretbind.go's probeHostConfig uses so posture
// stays uniform across the platform's short-lived helpers.
func sizeProbeHostConfig(volName string) *container.HostConfig {
	pids := sizeProbePidsLimit
	return &container.HostConfig{
		// Default-deny egress: the probe only stats local files, so it gets
		// no network at all (stricter than the isolated-network posture the
		// node containers get; matches build_executor.go and stacksecretbind.go).
		NetworkMode:    container.NetworkMode("none"),
		// Tenant inputs stay read-only: the probe can never mutate what the
		// node is about to read.
		Binds:          []string{volName + ":/data:ro"},
		ReadonlyRootfs: true,
		CapDrop:        []string{"ALL"},
		SecurityOpt:    []string{"no-new-privileges:true"},
		// Writable scratch for ffmpeg/ffprobe under a read-only rootfs (the
		// node lockdown's scratch set, sized down: this helper never writes
		// multi-megabyte state).
		Tmpfs: map[string]string{"/tmp": sizeProbeTmpfsOpts},
		// Safety limits (see the constants): generous, never a product cap.
		Resources: container.Resources{
			Memory:    sizeProbeMemoryBytes,
			NanoCPUs:  sizeProbeNanoCPUs,
			PidsLimit: &pids,
		},
	}
}

func runSizeProbe(ctx context.Context, imageRef, dockerNetwork, localRegistry, volName string, creds *transport.Credentials, d dockerProbeAPI) *SizeProbeDoc {
	if volName == "" || d == nil {
		probeLogWarn("size-probe skipped: no data volume / docker client")
		return nil
	}

	// Pull the probe image (small; cached on the worker after the first task).
	// Failure here is a nil return, never a task failure.
	opts := image.PullOptions{}
	if !(localRegistry != "" && strings.HasPrefix(imageRef, localRegistry+"/")) {
		encoded, encErr := auth.EncodeRegistryAuth(creds.Harbor.Username, creds.Harbor.Password)
		if encErr != nil {
			probeLogWarn("size-probe registry auth encode failed", "image", imageRef, "error", encErr)
			return nil
		}
		opts.RegistryAuth = encoded
	}
	pullReader, pullErr := d.ImagePull(ctx, imageRef, opts)
	if pullErr != nil {
		probeLogWarn("size-probe pull failed", "image", imageRef, "error", pullErr)
		// OPT2 (lane gpu2-2): a pull that fails at the RPC level means the
		// daemon has no such image locally either — the ContainerCreate that
		// follows dies "No such image" ~0.3 s later (measured on the xeon
		// embedded worker: every task paid that dead create for
		// ghcr.io/modtim/size-probe:latest, which Harbor
		// does not ship). Skip the create; the probe stays nil (never a
		// task failure), same as today's end state, minus the dead leg.
		return nil
	}
	// OPT2: the docker pull RPC returning does NOT mean the pull worked —
	// a missing/404 image reports in the JSON body. executor.go's pullImage
	// reads that stream (pullStreamError); the probe never did, so a missing
	// probe image cost a full create/start round trip per task. Read the
	// stream now (same package, same helper) and bail before the create.
	if perr := pullStreamError(imageRef, pullReader); perr != nil {
		probeLogWarn("size-probe pull failed", "image", imageRef, "error", perr)
		return nil
	}
	pullReader.Close()

	// probe.py's ENTRYPOINT is ["python3","/app/probe.py"]; with no args it scans
	// /data/input. Inputs are mounted READ-ONLY so the probe can never mutate
	// what the node is about to read.
	created, createErr := d.ContainerCreate(ctx,
		&container.Config{
			Image:  imageRef,
			Labels: map[string]string{"mod.purpose": "size-probe"},
		},
		sizeProbeHostConfig(volName),
		&network.NetworkingConfig{},
		nil,
		"",
	)
	if createErr != nil {
		probeLogWarn("size-probe create failed", "image", imageRef, "error", createErr)
		return nil
	}
	probeID := created.ID

	defer func() {
		rmCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		d.ContainerRemove(rmCtx, probeID, container.RemoveOptions{Force: true})
	}()

	if startErr := d.ContainerStart(ctx, probeID, container.StartOptions{}); startErr != nil {
		probeLogWarn("size-probe start failed", "container", probeID, "error", startErr)
		return nil
	}

	// Bounded wait: the probe stats a handful of files, so an idle window is a
	// hard cap here (unlike the minio sidecars, which stream multi-hour
	// transfers). A timeout SIGKILLs the probe and still returns nil.
	waitCtx, cancelWait := context.WithTimeout(ctx, sizeProbeIdleWindow)
	defer cancelWait()
	statusCh, errCh := d.ContainerWait(waitCtx, probeID, container.WaitConditionNotRunning)
	var exitStatus container.WaitResponse
	var exitOK bool
	var waitErr error
	for !exitOK && waitErr == nil {
		select {
		case err := <-errCh:
			waitErr = err
		case st := <-statusCh:
			exitStatus = st
			exitOK = true
		}
	}

	if waitErr != nil {
		probeLogWarn("size-probe wait failed", "container", probeID, "error", waitErr)
		return nil
	}
	if !exitOK || exitStatus.StatusCode != 0 {
		code := int64(-1)
		if exitOK {
			code = exitStatus.StatusCode
		}
		probeLogWarn("size-probe exited non-zero", "container", probeID, "exit", code)
		return nil
	}

	return parseSizeProbeDoc(sizeProbeStdout(ctx, d, probeID))
}
