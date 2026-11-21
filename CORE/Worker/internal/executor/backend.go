package executor

import (
	"context"
	"io"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// Backend is the seam between "run this containerized node task" and the
// concrete engine that actually runs it. DockerBackend (this file's sibling,
// docker_backend.go) wraps the CURRENT single-worker Docker pipeline
// (executor.go) behind this interface with zero behavior change. A future
// K8sBackend (k8s-reduction-engine T2, NOT implemented here — see the TODO
// on SelectBackend below) translates a TaskSpec into a K8s Job instead.
//
// LOCKED CONTRACT: this is Seam 1 from
// the internal design notes. Do not change these
// signatures (or TaskSpec/Handle/TaskStatus/GPURequest/EgressPolicy/
// ResourceLimits below) without updating that file first — T2 implements
// directly against this exact shape.
type Backend interface {
	// Submit creates and starts the run described by spec, returning a
	// Handle the caller uses for all subsequent Poll/Logs/Cancel calls.
	// It does not block until the task finishes.
	Submit(ctx context.Context, spec *TaskSpec) (*Handle, error)

	// Poll reports the current state of a previously-submitted task:
	// running, succeeded, or failed (with exit code / error detail).
	Poll(ctx context.Context, h *Handle) (*TaskStatus, error)

	// Logs returns the task's combined stdout/stderr. The caller must
	// Close() the returned reader.
	Logs(ctx context.Context, h *Handle) (io.ReadCloser, error)

	// Cancel best-effort stops the run and releases any backend-side
	// resources tracked for it (idempotent).
	Cancel(ctx context.Context, h *Handle) error
}

// TaskSpec is the backend-agnostic description of one ephemeral node task.
//
// Inputs/outputs are staged via S3 (versitygw) exactly as today — the
// container gets the same cog S3 creds + API_URL env via Env; NO PVC or
// shared volume is part of this contract. (DockerBackend's own use of a
// scratch Docker volume + download/upload sidecar containers to move bytes
// into/out of that S3-backed env is an implementation detail preserved from
// the current pipeline, not something TaskSpec callers need to know about.)
type TaskSpec struct {
	Image string
	Env   map[string]string
	Cmd   []string

	// TenantID is the task's tenant (transport.Task.TenantID); 0 =
	// platform/system task, >0 = tenant code (GSEC FX.17 / R-069).
	// NativeBackend consults it to refuse tenant code that would run
	// unsandboxed on the host; other backends ignore it (docker already
	// containerizes; k8s stamps it as the MOD_TENANT_ID env).
	TenantID int

	GPU       GPURequest     // count + optional MIG profile
	Egress    EgressPolicy   // default-deny | user-egress | allowlist (-> NetworkPolicy on K8s)
	Resources ResourceLimits // cpu/mem/shm

	// EgressHosts is the declared allowlist when Egress == EgressAllowlist
	// (GB.EGR.1), copied from the claimed task's AllowedEgress. Empty for
	// every other Egress value. K8sBackend (GB.EGR.3) turns this into a
	// per-task CiliumNetworkPolicy (GB.EGR.2) when Cilium FQDN enforcement is
	// active; DockerBackend's equivalent is the egress proxy (GB.EGR.D.x).
	EgressHosts []string

	// NativeCommand is the host command to run for NativeBackend.
	// Empty = use Cmd. Ignored by other backends.
	NativeCommand string
	// NativeArgs are additional arguments for NativeBackend.
	// Ignored by other backends.
	NativeArgs []string
	// NativeEnv is extra environment for NativeBackend.
	// Ignored by other backends.
	NativeEnv map[string]string

	// RegistryAuth carries the per-task Harbor credential sets (platform +
	// tenant) from the claimed task's /credentials grant. WslcBackend uses it
	// (GP.17f) to `wslc login` the image's registry before the run's implicit
	// pull, via the same PickRegistryAuth the docker path uses. DockerBackend
	// and K8sBackend ignore it — both already get creds through their own
	// paths. Zero value = no registry auth (public/local image).
	RegistryAuth TaskRegistryAuth

	// K8sGPU (14.13 / D-GPU Layer 2) is the API-computed pod-spec GPU
	// fragment (resources/annotations/labels/nodeSelector/schedulerName),
	// translated by the API's 14.12 k8s_gpu_request against the target
	// cluster's stored GI.2 probe. K8sBackend's BuildJob applies it verbatim
	// (merged over its own resources/nodeSelector; the existing ones are
	// never dropped); nil (older API / docker / non-GPU) keeps today's
	// whole-GPU + W21 placement behaviour unchanged. Other backends ignore
	// it, like GPURequest's MIGProfile.
	K8sGPU *transport.K8sGPURequest
}

// TaskRegistryAuth is the backend-neutral carrier for the per-task Harbor
// credential sets (GP.17f). Both fields may be zero-value when the task's
// credential bundle had no registry creds (public images).
type TaskRegistryAuth struct {
	Platform transport.HarborCredentials
	Tenant   transport.HarborCredentials
}

// GPURequest describes the GPU shape a task needs.
type GPURequest struct {
	// Count is the number of GPUs requested. 0 = no GPU.
	Count int
	// MIGProfile optionally pins a specific MIG partition (e.g. "1g.10gb").
	// Empty = whole-GPU / no MIG. Docker has no MIG concept, so
	// DockerBackend ignores this field; K8sBackend (T2) + the GPU-sharing
	// work (T6) are where it gets consumed.
	MIGProfile string
}

// EgressPolicy is the coarse network posture for a task's container.
type EgressPolicy string

const (
	// EgressDefaultDeny is the zero value: no outbound, no platform-service
	// access. Docker: the isolated user-internal network. K8s: a
	// default-deny NetworkPolicy.
	EgressDefaultDeny EgressPolicy = "default-deny"
	// EgressUserEgress allows outbound internet access but not the internal
	// platform network. Docker: the user-egress network (same mechanism as
	// today's NEEDS_INTERNET marker, see SelectNetworkMode in netpol.go).
	// K8s: an egress-only NetworkPolicy.
	EgressUserEgress EgressPolicy = "user-egress"
	// EgressAllowlist is EgressUserEgress narrowed to a declared set of hosts
	// (TaskSpec.EgressHosts, from the task's AllowedEgress). Docker: still
	// the egress network (advisory today; the egress proxy in GB.EGR.D.x
	// enforces it). K8s: a per-task CiliumNetworkPolicy when the cluster runs
	// Cilium (MOD_K8S_EGRESS_FQDN, GB.EGR.4); without Cilium the task still
	// runs on user-egress, annotated `mod.tech/egress-allowlist: unenforced`
	// rather than fail-closed (GB.EGR §6a).
	EgressAllowlist EgressPolicy = "allowlist"
)

// ResourceLimits caps a task's container resources.
type ResourceLimits struct {
	CPUCores float64 // 0 = backend default
	MemoryMB int64   // 0 = backend default; negative = uncapped (platform-only)
	// ShmSizeMB caps /dev/shm. Accepted by the contract for K8s parity, but
	// NOT YET wired into DockerBackend's container creation — today's Docker
	// path still gets the fixed runtime default (see the
	// node-ephemeral-memory-gap follow-up). 0 = backend default.
	ShmSizeMB int64
}

// TaskPhase is the coarse state Poll reports.
type TaskPhase string

const (
	TaskRunning   TaskPhase = "running"
	TaskSucceeded TaskPhase = "succeeded"
	TaskFailed    TaskPhase = "failed"
)

// TaskStatus is a point-in-time snapshot of a submitted task's execution.
type TaskStatus struct {
	Phase TaskPhase
	// ExitCode is meaningful once Phase != TaskRunning.
	ExitCode int
	// Err carries failure detail when Phase == TaskFailed. Empty otherwise.
	Err string
}

// Handle identifies a submitted task to its owning Backend.
type Handle struct {
	Backend string // "docker" | "k8s"
	ID      string // container id | "namespace/job"
	// c11-J3b: token usage read off the k8s stage-out logs (nil = none).
	UsageTokens *transport.UsageTokens
}
