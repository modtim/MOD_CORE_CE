package executor

// k8s-reduction-engine W1: task -> TaskSpec translation for the K8s dispatch
// path (cmd/worker executeTask's k8s branch).
//
// This MIRRORS what the Docker path derives from a claimed task + its
// /credentials grant (Executor.Run -> resolveProcessingImage ->
// runContainerWithCreds env building + SelectNetworkMode + resource caps). It
// deliberately does not refactor executor.go: the docker path stays the
// source of truth and is untouched; keep the two in step when either changes.
//
// Intentionally NOT mirrored (host-specific to a Docker worker, meaningless in
// a pod): the /dev/dxg WSL GPU-lib env, raw --device mounts for AMD/Intel/NPU,
// the docker network name itself (K8s egress is a NetworkPolicy selected by
// TaskSpec.Egress), and the download/upload S3 staging sidecars.

import (
	"errors"
	"io"
	"log/slog"
	"os"
	"strconv"
	"strings"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// Optional per-task GPU overrides carried in container_env. These are the
// carrier for T6b batch 2 (fit-gate -> MIG-profile/time-slice selection):
// the API can set them and the K8s path honours them; the Docker path ignores
// them (it always attaches all GPUs when gpu_required).
const (
	EnvGPUCount      = "MOD_GPU_COUNT"
	EnvGPUMIGProfile = "MOD_GPU_MIG_PROFILE"
)

// Sentinel network names used only to classify SelectNetworkMode's verdict
// into an EgressPolicy — never handed to Docker or K8s.
const (
	tsNetMain   = "\x00main"
	tsNetUser   = "\x00user"
	tsNetEgress = "\x00egress"
)

// platformVaultEnvKeys are forwarded from the worker's own env into
// PLATFORM_TASK containers only (same list + trust gate as
// runContainerWithCreds).
var platformVaultEnvKeys = []string{
	"VAULT_ADDR",
	"VAULT_APPROLE_ROLE_ID",
	"VAULT_APPROLE_SECRET_ID",
	"VAULT_DB_ROLE",
	"POSTGRES_HOST",
	"POSTGRES_PORT",
	"POSTGRES_DB",
}

// TaskSpecOptions carries the worker-side facts the Docker Executor holds as
// fields (API URL override, hardware tags for gpu_variants selection).
type TaskSpecOptions struct {
	// APIURL overrides container_env.API_URL (the worker, not the server, is
	// authoritative — same rule as the docker path). Empty = drop API_URL.
	APIURL string
	// HardwareTags drives gpu_variants image selection (priority-ordered).
	HardwareTags []string
	// Log receives image-selection info lines; nil = discard.
	Log *slog.Logger
}

// K8sTaskSpec is TaskSpecFromTask using this Executor's own API URL and
// hardware tags, so the K8s path resolves the same image the Docker path would.
func (e *Executor) K8sTaskSpec(task *transport.Task, creds *transport.Credentials) (*TaskSpec, error) {
	return TaskSpecFromTask(task, creds, TaskSpecOptions{APIURL: e.apiURL, HardwareTags: e.hardwareTags, Log: e.log})
}

// TaskSpecFromTask translates a claimed task + its credential bundle into a
// backend-neutral TaskSpec. MOD_TASK_ID is always set (K8sBackend derives the
// Job name/labels from it).
func TaskSpecFromTask(task *transport.Task, creds *transport.Credentials, opts TaskSpecOptions) (*TaskSpec, error) {
	if task == nil {
		return nil, errors.New("TaskSpecFromTask: nil task")
	}
	if creds == nil {
		creds = &transport.Credentials{}
	}
	payload := task.Payload
	log := opts.Log
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}

	// Image: same resolution as Executor.Run (default image, gpu_variants by
	// hardware tag, digest pin). The harbor registry is only needed to build
	// the default image name when the payload doesn't carry one.
	harborReg, regErr := harborRegistry(creds.Harbor)
	if payload.ProcessingImage == "" && regErr != nil {
		return nil, regErr
	}
	resolver := &Executor{hardwareTags: opts.HardwareTags, log: log}
	image := resolver.resolveProcessingImage(payload, harborReg)

	// Env: container_env minus API_URL, then the worker's API_URL.
	env := make(map[string]string, len(payload.ContainerEnv)+12)
	for k, v := range payload.ContainerEnv {
		if k == "API_URL" {
			continue
		}
		env[k] = v
	}
	for _, kv := range defaultIODirEnv(env) {
		if i := strings.IndexByte(kv, '='); i > 0 {
			env[kv[:i]] = kv[i+1:]
		}
	}
	if opts.APIURL != "" {
		env["API_URL"] = opts.APIURL
	}
	// W9: the claimed task's tenant is authoritative for the pod's
	// mod.tech/tenant-id label (K8sBackend reads it back from Env).
	if task.TenantID > 0 {
		env[EnvTenantID] = strconv.Itoa(task.TenantID)
	}

	// Per-task storage creds (always passed on the docker path).
	m := creds.MinIO
	if m.Endpoint != "" || m.AccessKey != "" || m.SecretKey != "" {
		endpoint := strings.TrimPrefix(strings.TrimPrefix(m.Endpoint, "http://"), "https://")
		env["MINIO_ENDPOINT"] = endpoint
		env["MINIO_ACCESS_KEY"] = m.AccessKey
		env["MINIO_SECRET_KEY"] = m.SecretKey
		if m.SessionToken != "" {
			env["MINIO_SESSION_TOKEN"] = m.SessionToken
		}
	}

	// PLATFORM_TASK only: Harbor push creds + Vault/Postgres bootstrap.
	if _, isPlatform := payload.ContainerEnv["PLATFORM_TASK"]; isPlatform {
		// GSEC FX.16 R-023 — build-DAG stages get their registry
		// creds from claim-time secret_refs; the durable tenant
		// robot from the /credentials grant must never reach them.
		if !IsBuildDAGStage(payload.ContainerEnv) {
			h := creds.Harbor
			if h.Username != "" {
				env["HARBOR_USERNAME"] = h.Username
				env["HARBOR_PASSWORD"] = h.Password
				if h.Registry != "" {
					env["HARBOR_REGISTRY"] = h.Registry
				}
			}
		}
		for _, key := range platformVaultEnvKeys {
			if v := os.Getenv(key); v != "" {
				if _, alreadySet := payload.ContainerEnv[key]; !alreadySet {
					env[key] = v
				}
			}
		}
	}

	// Egress: reuse the docker policy verdict. Only the isolated user
	// network is default-deny; every other verdict (platform, stack posture,
	// API callback, explicit egress) grants outbound.
	//
	// GB.EGR.1: when the egress-network verdict came specifically from the
	// AllowedEgress branch (not NEEDS_INTERNET, which wants the open egress
	// network with no declared allowlist), narrow it to EgressAllowlist and
	// carry the declared hosts, so K8sBackend can render a per-task Cilium
	// FQDN policy instead of opening 0.0.0.0/0.
	netSel := SelectNetworkMode(payload, tsNetMain, tsNetUser, tsNetEgress)
	egress := EgressUserEgress
	var egressHosts []string
	switch {
	case netSel.DockerNetwork == tsNetUser:
		egress = EgressDefaultDeny
	case netSel.DockerNetwork == tsNetEgress && len(payload.AllowedEgress) > 0:
		if _, needsInternet := payload.ContainerEnv["NEEDS_INTERNET"]; !needsInternet {
			egress = EgressAllowlist
			egressHosts = append([]string(nil), payload.AllowedEgress...)
		}
	}
	for _, kv := range netSel.EnvAdditions {
		if i := strings.IndexByte(kv, '='); i > 0 {
			env[kv[:i]] = kv[i+1:]
		}
	}

	// GPU: docker attaches GPUs when gpu_required or a TARGET_KIND is set.
	var gpu GPURequest
	if payload.GPURequired || strings.TrimSpace(payload.ContainerEnv["TARGET_KIND"]) != "" {
		gpu.Count = 1
	}
	if v := strings.TrimSpace(payload.ContainerEnv[EnvGPUCount]); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			gpu.Count = n
		}
	}
	if p := strings.TrimSpace(payload.ContainerEnv[EnvGPUMIGProfile]); p != "" {
		gpu.MIGProfile = p
		if gpu.Count == 0 {
			gpu.Count = 1
		}
	}

	spec := &TaskSpec{
		Image:       image,
		Env:         env,
		Cmd:         append([]string(nil), payload.Cmd...),
		GPU:         gpu,
		Egress:      egress,
		EgressHosts: egressHosts,
		Resources: ResourceLimits{
			CPUCores:  payload.CPULimit,
			MemoryMB:  int64(payload.MemoryLimitMB),
			ShmSizeMB: payload.ShmSizeMB,
		},
		// GP.17f: carry the per-task Harbor cred sets so the wslc backend can
		// authenticate the run's implicit pull (docker/k8s ignore this field).
		RegistryAuth: TaskRegistryAuth{
			Platform: creds.Harbor,
			Tenant:   creds.TenantHarbor,
		},
		// 14.13: the API-computed pod-spec GPU fragment (nil when absent —
		// older API / docker / non-GPU; K8sBackend then uses its own
		// whole-GPU + W21 placement).
		K8sGPU: payload.K8sGPU,
	}
	return WithTaskID(spec, strconv.Itoa(task.ID)), nil
}
