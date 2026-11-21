package transport

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
)

// Checkpoint is a node's own statement of progress, extracted from its stdout.
//
// Advisory: it drives a UI, never a decision. A node reporting 0.99 and then
// dying is still retried, because only the task status proves completion.
type Checkpoint struct {
	Progress *float64    `json:"progress,omitempty"`
	Label    string      `json:"label,omitempty"`
	Payload  interface{} `json:"payload,omitempty"`
}

// Task is the instruction set the worker receives. It tells the worker
// WHAT to run — not HOW. The executor interprets these instructions.
type Task struct {
	ID            int    `json:"task_id"`
	WorkflowRunID int    `json:"workflow_run_id"`
	NodeID        string `json:"node_id"`
	NodeRunID     int    `json:"node_run_id"`
	TenantID      int    `json:"tenant_id"`
	QueueName     string `json:"queue_name"`
	AttemptCount  int    `json:"attempt_count"`
	MaxAttempts   int    `json:"max_attempts"`

	// Payload is the full task spec from Traffic Cop
	Payload TaskPayload `json:"payload"`

	// ClaimExpiresAt is when the claim times out if we don't start
	ClaimExpiresAt time.Time `json:"claim_expires_at"`

	// PhasePlan is the scheduler's lifecycle plan with per-phase timing.
	// nil when predictive scheduling is disabled (FIFO fallback).
	PhasePlan *PhasePlan `json:"phase_plan,omitempty"`

	// StartAfter is when the worker should begin the processing phase.
	// The worker can execute earlier phases (image pull, download) before this time.
	StartAfter *time.Time `json:"start_after,omitempty"`
}

// PhasePlan describes the full task lifecycle as planned by the OverlapScheduler.
type PhasePlan struct {
	Phases               []Phase `json:"phases"`
	TotalEstDurationS    float64 `json:"total_est_duration_s"`
	SchedulingConfidence float64 `json:"scheduling_confidence"`
	SchedulingReasoning  string  `json:"scheduling_reasoning,omitempty"`
}

// Phase is a single lifecycle phase in the plan.
type Phase struct {
	Name         string                 `json:"name"`           // image_check, image_pull, data_download, container_start, processing, upload, cleanup
	StartOffsetS float64                `json:"start_offset_s"` // seconds from task claim time
	EstDurationS float64                `json:"est_duration_s"`
	Resources    map[string]interface{} `json:"resources"` // expected resource impact
}

// InputFile is one staged input. Carries its OWN extension: a capture can mix
// formats (DJI writes JPG alongside DNG), and naming a DNG .jpg because the
// first file happened to be a JPEG hands the container a file it cannot decode
// and produces a confident wrong answer about the user's data.
type InputFile struct {
	CogID         int    `json:"cog_id"`
	ContentHash   string `json:"content_hash"`
	FileExtension string `json:"file_extension"`
}

// K8sGPURequest (14.13 / D-GPU Layer 2) is the pod-spec fragment the API's
// 14.12 translator (services/k8s_gpu_request.py k8s_gpu_request) renders
// against the target cluster's stored GI.2 probe (K8sClusterConfig.cluster_caps)
// and ships as task_payload["k8s_gpu"]. The K8s backend's Job builder
// applies it VERBATIM to the pod spec (merging with the resources/labels/
// nodeSelector it already computes, never dropping the existing ones) and
// sets schedulerName when KAI is present; when the block is ABSENT (older
// API) the builder keeps today's whole-GPU / W21 placement unchanged.
//
// Limits values arrive as JSON numbers (whole counts, e.g. "nvidia.com/gpu":
// 1) or strings (KAI gpu-fraction, e.g. "scheduling.k8s.io/gpu": "0.5");
// the executor normalises both through resource.ParseQuantity.
type K8sGPURequest struct {
	Resources struct {
		Limits map[string]interface{} `json:"limits,omitempty"`
	} `json:"resources,omitempty"`
	Annotations   map[string]string `json:"annotations,omitempty"`
	Labels        map[string]string `json:"labels,omitempty"`
	NodeSelector  map[string]string `json:"nodeSelector,omitempty"`
	SchedulerName string            `json:"scheduler_name,omitempty"`
	// Affinity (GI.6) is the API's merge_affinity output: soft
	// nodeAffinity preferred terms (kubernetes.io/hostname In <node>),
	// ranked by the node's measured durations for this node kind. Nil
	// from older APIs — the worker then applies no affinity.
	Affinity *corev1.Affinity `json:"affinity,omitempty"`
}

// TaskPayload contains the instructions for the executor.
// GPUAssignment is the set of cards the API pinned a task to, and the VRAM it
// reserved on them. Mode is "shared" or "exclusive".
// ServiceCards (§14.7b), when present, is the per-compose-service card
// pinning for a persistent-stack deploy: service name -> its own card IDs
// (a subset of Cards). Lets a mixed-kind stack (e.g. a cuda LLM service and
// a rocm STT service in one compose project) get each service pinned to the
// right kind of card, instead of the whole stack sharing one task-wide card
// set. Nil/empty means "use Cards for every GPU service", which is today's
// task-wide behaviour and what an older API still sends.
type GPUAssignment struct {
	Mode         string              `json:"mode"`
	Cards        []AssignedGPU       `json:"cards"`
	VRAMLimitMB  int                 `json:"vram_limit_mb"`
	Source       string              `json:"source"`
	ServiceCards map[string][]string `json:"service_cards,omitempty"`
}

// AssignedGPU is one pinned card. DeviceUUID is preferred (NVIDIA); Index is
// the fallback when the card reported no uuid. PCIBusID (rocm/intel — those
// vendors have no per-card uuid) is the exact /sys/class/drm correlation key
// when the API's inventory scan reported one; §14.3's index→renderD mapping
// prefers it over Index, which only holds if the host's PCI enumeration
// order hasn't changed since the card was profiled.
type AssignedGPU struct {
	CardID     string `json:"card_id"`
	DeviceUUID string `json:"device_uuid"`
	Index      int    `json:"index"`
	Kind       string `json:"kind"`
	CardVRAMMB int    `json:"card_vram_mb"`
	PCIBusID   string `json:"pci_bus_id,omitempty"`
}

// NativeRecipe (launch-completion-megaplan GP.16 / D-P1) is the per-OS recipe
// for an imageless (native) task: the node's custom_metadata declares
// execution: native + native.platforms.{windows,darwin,linux}.{command,
// args_template, env} (validated on the API by schemas.py
// _validate_native_recipe_metadata). The API resolves the recipe against the
// claiming worker's OS and stamps it here. Command is the host executable;
// ArgsTemplate may carry {input_dir}/{output_dir}/{param:NAME} placeholders
// (executor.substituteArgs); Env is merged on top of the task's
// ContainerEnv for the native process.
type NativeRecipe struct {
	Platform     string            `json:"platform,omitempty"`
	Command      string            `json:"command"`
	ArgsTemplate []string          `json:"args_template,omitempty"`
	Env          map[string]string `json:"env,omitempty"`
}

type TaskPayload struct {
	// ExecutorBackend is the API's per-task backend gate (k8s-reduction-engine
	// W7): "k8s" only when the task's tenant holds Cap.K8S_BACKEND. Empty (older
	// API) or anything else means docker.
	ExecutorBackend string `json:"executor_backend,omitempty"`

	// TaskType (sz-build-queue.1) mirrors ApiTaskQueue.task_type for the one
	// value the worker needs to branch on: "build". The /workers/tasks/poll
	// response only ever forwards task_payload verbatim (never the row's own
	// task_type column — see worker_tasks.py TaskPollResponse), so a build
	// task's enqueue path must stamp "task_type": "build" INTO the payload
	// dict itself for this field to decode; every other task_type ("processing",
	// "system", "pre_pull", "variant_smoke", ...) never sets it and this stays
	// empty, taking today's normal container-processing path unchanged.
	TaskType string `json:"task_type,omitempty"`

	// Execution (launch-completion-megaplan GP.16 / D-P1): "native" runs the
	// task as a host process via the worker's NativeBackend — no container, no
	// Docker. Empty (older API) or anything else means docker. Never inferred:
	// the API sets it only when the node's custom_metadata declared
	// execution: native, and a native task that lands where it cannot run fails
	// the task loudly (no silent docker fallback, per D-P1).
	Execution string `json:"execution,omitempty"`

	// Native is the per-OS recipe for an Execution=="native" task (GP.16 /
	// D-P1): the command/args/env the API resolved for THIS worker's platform.
	// Nil on every non-native task.
	Native *NativeRecipe `json:"native,omitempty"`

	// NativePlatform is the worker OS (windows/darwin/linux) the recipe was
	// resolved for — log/unplaceable-message context, mirroring the API's
	// native_platform task-payload field. Empty on non-native tasks.
	NativePlatform string `json:"native_platform,omitempty"`

	// Docker image to run (already resolved, e.g. "registry.mod.com/modtim/albedo:latest")
	ProcessingImage string `json:"processing_image"`

	// Input file location in MinIO (SHA256 hash)
	SourceFileLocation string `json:"source_file_location"`
	FileExtension      string `json:"file_extension"`

	// Every input file, when a task carries more than one cog.
	//
	// SourceFileLocation holds ONE hash and FileExtension ONE extension, both
	// taken from cog_ids[0]. That is correct for the single-file case and
	// silently wrong for the rest: a downstream node always receives one task
	// carrying every cog, so a 38-image capture staged one image and the
	// container reported finding nothing.
	//
	// Populated by the server only when len > 1, so a single-file task takes
	// exactly the path it takes today. Empty here means "use
	// SourceFileLocation", which is also what an older server sends.
	InputFiles []InputFile `json:"input_files,omitempty"`

	// MinIO bucket for this tenant
	MinioBucket string `json:"minio_bucket"`

	// Environment variables to pass into the processing container
	ContainerEnv map[string]string `json:"container_env"`

	// node metadata
	NodeKind      string                 `json:"node_kind"`
	DisplayName   string                 `json:"display_name"`
	OutputSchema  map[string]interface{} `json:"output_schema"`
	CostInCredits float64                `json:"cost_in_credits"`

	// Processing options
	GPURequired      bool `json:"gpu_required"`
	ContainerTimeout int  `json:"container_timeout"` // seconds, 0 = use default

	// GPUAssignment (§14.2) is the API's claim-time choice of physical NVIDIA
	// cards for this task. Nil from older APIs and for non-GPU tasks, in which
	// case the task gets every GPU as before.
	GPUAssignment *GPUAssignment `json:"gpu_assignment,omitempty"`

	// K8sGPU (14.13) is the API-computed pod-spec GPU fragment for k8s
	// backend tasks (see K8sGPURequest). Nil from older APIs / docker /
	// non-GPU tasks — the K8s backend then uses its own placement.
	K8sGPU *K8sGPURequest `json:"k8s_gpu,omitempty"`

	// Resource limits (#84) — enforced at container creation.
	// Zero values = use defaults (2 CPUs, 4GB RAM, 4096 PIDs).
	MemoryLimitMB int     `json:"memory_limit_mb,omitempty"` // Container RAM limit in MB
	CPULimit      float64 `json:"cpu_limit,omitempty"`       // CPU limit (e.g., 2.0 = 2 cores)
	PidsLimit     int64   `json:"pids_limit,omitempty"`      // Max PIDs (fork bomb protection)

	// Node-declared ephemeral memory (metadata ephemeral_resources). 0 = keep
	// the worker default (/dev/shm = MOD_WORKER_NODE_SHM_MB, /tmp = sandbox tmpfs).
	ShmSizeMB int64 `json:"shm_size_mb,omitempty"`
	TmpfsMB   int64 `json:"tmpfs_mb,omitempty"`

	// Context for metadata updates
	UserID int   `json:"user_id"`
	CogIDs []int `json:"cog_ids"`

	// GPU variant image tags — maps backend name to Docker image.
	// Worker selects the image matching its detected GPU type.
	// e.g., {"cuda": "img:latest", "rocm": "img:latest-rocm", "cpu": "img:latest-cpu"}
	GPUVariants map[string]string `json:"gpu_variants,omitempty"`

	// Immutable digest for the default/ProcessingImage build (node-version-pinning).
	// When set, the worker pulls and runs "<repo>@sha256:<digest>" instead of the
	// mutable tag, so a moved :latest can never change what a pinned run executes.
	// Empty = legacy dispatch; the worker pulls by tag exactly as before.
	ImageDigest string `json:"image_digest,omitempty"`

	// Per-backend immutable digests, parallel to GPUVariants and keyed by the same
	// backend name (e.g. "cuda"/"rocm"/"cpu"). After the worker selects a variant
	// it pins that variant to its digest here. A missing entry = pull that variant
	// by tag, so a partially-populated map degrades gracefully.
	GPUVariantDigests map[string]string `json:"gpu_variant_digests,omitempty"`

	// Secret references — resolved at runtime, never stored as plaintext in the payload.
	// Each entry maps a tenant secret name to a container env var.
	SecretRefs []SecretRef `json:"secret_refs,omitempty"`

	// #87: Egress control — list of allowed outbound domains for this container.
	// Empty/nil = no outbound internet access (isolated bridge network).
	// e.g., ["api.openai.com", "*.amazonaws.com"]
	AllowedEgress []string `json:"allowed_egress,omitempty"`

	// EgressMode — "direct" (task's container egresses directly) |
	// "mediated" (traffic is tunneled through the API gateway).
	// Empty = direct (legacy/default).
	EgressMode string `json:"egress_mode,omitempty"`

	// EgressUpstream — mediated: gateway host:port to forward egress through.
	EgressUpstream string `json:"egress_upstream,omitempty"`

	// EgressUpstreamToken — mediated: short-lived gateway auth token.
	// Never log or persist.
	EgressUpstreamToken string `json:"egress_upstream_token,omitempty"`

	// GB.EGR.D.9d: set when AllowedEgress contains a tailnet destination
	// (100.64.0.0/10 or *.example.com — see
	// services/egress_allowlist.classify_host on the API side). Docker
	// Desktop / WSL2 dockerd containers have no route to the host's
	// tailscale0, exactly like the persistent-stack finding that motivated
	// the tsegress sidecar (routers/persistent_stacks/_shared.py). The API
	// mints a single-use, non-reusable auth key per anchor (mirroring
	// _tailnet_egress_authkey) so the worker can bring up the same kind of
	// sidecar for this one task; nil means no tailnet destination was
	// declared and the worker's normal mod-user-egress bridge is used as-is.
	TailnetEgress *TailnetEgress `json:"tailnet_egress,omitempty"`

	// UseSandboxRuntime=true makes the worker dispatch this task under
	// the `runsc` (gVisor) OCI runtime instead of the default `runc`,
	// if gVisor is installed on the host. ~2-3× perf hit; real VM-grade
	// isolation from the host kernel. Opt-in per node, not plan-tied —
	// every tenant can flip it. Silently falls back to runc when
	// gVisor isn't registered on the dockerd host.
	UseSandboxRuntime bool `json:"use_sandbox_runtime,omitempty"`

	// Gate 3 — a durable volume every ATTEMPT of this node run shares, mounted
	// at MOD_CHECKPOINT_DIR. Empty means this node keeps no state across
	// attempts, which is the default and the behaviour every existing node
	// already has.
	//
	// The ordinary /data volume is per-attempt and removed on completion, so a
	// node five hours into six restarts from zero. This one is keyed to the
	// node run, so attempt 2 mounts what attempt 1 left.
	CheckpointVolume string `json:"checkpoint_volume,omitempty"`

	// GC.MWL.C5/C6 — manager-work-loans checkpoint hand-back. Optional: set
	// by the API only when a prior attempt's checkpoint exists in object
	// storage and this attempt is landing on a DIFFERENT host than the one
	// that wrote it (ensureCheckpointVolume's same-host case already finds
	// the volume locally and needs no download). A presigned GET URL the
	// worker downloads and untars into CheckpointVolume before the node
	// container starts — see HydrateCheckpointVolume (checkpoint_transfer.go).
	// Empty means "nothing to hydrate", the default for every task today.
	CheckpointHydrateURL string `json:"checkpoint_hydrate_url,omitempty"`

	// 985c — the mirror of CheckpointHydrateURL for the OTHER direction: a
	// presigned PUT target the API minted for this loaned node run's
	// checkpoint, so the worker can export CheckpointVolume back to object
	// storage after the container completes (see ExportCheckpointVolume,
	// checkpoint_transfer.go). Empty means "nothing to export", the
	// behaviour every task has today.
	CheckpointExportURL string `json:"checkpoint_export_url,omitempty"`

	// 985c — paired with CheckpointExportURL: the manager-work-loans checkpoint
	// export transfer id (the pending direction='export' row
	// request_checkpoint_export created) the worker's post-PUT report
	// (POST /workers/tasks/{id}/checkpoint-export-uploaded, 985f) must name so
	// the API marks that SAME transfer uploaded. Set together with
	// CheckpointExportURL; zero means either nothing to export, or (until
	// 985c lands) the worker still PUTs the export but has no transfer id to
	// report, which it logs and skips rather than guessing one.
	CheckpointExportTransferID int64 `json:"checkpoint_export_transfer_id,omitempty"`

	// GC.MWL.12 — manager-work-loans worker staging. Optional: set by the
	// API only when this task is a loaned run's own node execution running
	// on the BORROWER manager (services/mesh_loans.py's `_dispatch_loan_run`
	// creates the borrower's local WorkflowRun off the owner's
	// execution_conf; the per-node dispatch for that local run is where
	// this would be threaded through, carrying `WorkLoan.id`). When set,
	// the worker's output-upload path stamps every object it writes with
	// the "uncollected" loan marker (see internal/executor/loan_staging.go)
	// instead of leaving it unmarked, so bytes in the borrower's own bucket
	// start life tagged un-collected and only the owner's collect step
	// (services/mesh_loan_transfer.py collect_loan) may present them as a
	// result. Empty means "not a loan" — every task today.
	LoanID string `json:"loan_id,omitempty"`

	// EXPR 9.3 — the reaper's resume stamp. When a node run's task is requeued
	// after a failure, the API stamps where the node left off (workflow_checkpoints
	// on the DB side) into this payload so a restart landing on ANY host — not
	// just one that still has the checkpoint volume — can pick up. The worker
	// surfaces them as MOD_RESUME_CHECKPOINT_* env vars (see resumeCheckpointEnv,
	// executor.go); a node that understands them skips the work it already did.
	// Zero/empty means "no resume point known", the default for every task today.
	ResumeCheckpointID int64 `json:"resume_checkpoint_id,omitempty"`

	// Human-readable tag the checkpoint was written under (e.g. "epoch-7").
	// Advisory only: the node's own reader decides what to do with it.
	ResumeCheckpointLabel string `json:"resume_checkpoint_label,omitempty"`

	// Fraction of the node's own progress the checkpoint represents, 0..1.
	// A pointer so "the node reported 0" is distinguishable from "no progress
	// was recorded" — the same reason Checkpoint.Progress is one.
	ResumeCheckpointProgress *float64 `json:"resume_checkpoint_progress,omitempty"`

	// The checkpoint's own state blob (workflow_checkpoints.payload JSONB, size-
	// capped by the API). Kept raw: the worker never interprets it, it only
	// re-emits it compacted, and re-serialising through interface{} would
	// reorder keys and change what the node sees on each restart.
	ResumeCheckpointPayload json.RawMessage `json:"resume_checkpoint_payload,omitempty"`

	// System tasks (benchmarks, trivy scans) don't use MinIO.
	// When true, skip the download/upload sidecar entirely.
	SkipDownload bool `json:"skip_download,omitempty"`
	SkipUpload   bool `json:"skip_upload,omitempty"`

	// Opt-in host Docker socket passthrough. Set ONLY by the server
	// dispatcher for trusted system tasks (the HardwareBenchmark
	// container, which times `docker pull alpine` and `docker run
	// alpine true` for its container_startup_ms score). Worker
	// honours it by bind-mounting /var/run/docker.sock into the
	// container; general processing tasks have no way to request
	// this because they don't author the task payload.
	NeedsDockerSocket bool `json:"needs_docker_socket,omitempty"`

	// Persistent stack op (add-persistent-stacks). When StackOp is non-empty
	// the worker runs the compose-runner (docker compose) instead of the
	// single-container processing path. Set only by the server dispatcher for
	// trusted PLATFORM_TASK stack operations.
	StackOp      string `json:"stack_op,omitempty"`      // "deploy" | "teardown" | "snapshot" | "restore" | "rehearse"
	StackProject string `json:"stack_project,omitempty"` // docker compose -p <name>
	// RestoreServices: boot_restore starts only these compose services (the
	// containers Docker itself would have restarted). Empty = worker decides.
	RestoreServices []string `json:"restore_services,omitempty"`
	ComposeSpec  string `json:"compose_spec,omitempty"`  // compose YAML (deploy only)

	// GA.ASW.8 — teardown data policy. "retain" must NOT destroy the stack's
	// data: the worker tears the containers down WITHOUT the `-v` flag, so the
	// compose project's named volumes (and their data) survive on disk. Absent
	// / "erase" keeps the pre-existing `down -v` (reclaim volumes).
	StackDataPolicy string `json:"data_policy,omitempty"` // "retain" | "erase"

	// GX.S10b — stack file secrets + storage files. SecretFiles are delivered
	// as per-stack secret files (content inline, base64); StorageFiles are
	// streamed from a short-TTL presigned URL into a per-stack on-disk dir.
	// Both are materialized before `compose up` and the dirs are exported to
	// the compose process as MOD_SECRETS_DIR / MOD_FILES_DIR (the worker copy
	// of the compose references ${MOD_SECRETS_DIR} / ${MOD_FILES_DIR}).
	SecretFiles  []StackSecretFile  `json:"secret_files,omitempty"`
	StorageFiles []StackStorageFile `json:"storage_files,omitempty"`

	// delegated-stack-authoring §5.4 — when true the deploy path refuses a spec
	// that attaches any service to the reserved platform network, so a declared
	// egress posture cannot be smuggled past by hand-authoring `networks:`. Stamped
	// by the server only when per-stack egress enforcement is on (ships dark).
	EgressEnforced bool `json:"egress_enforced,omitempty"`

	// Snapshot/restore (add-persistent-stacks §3) — tar a docker volume to MinIO
	// and back. The backbone for backup AND cross-worker migration.
	StackVolume    string `json:"stack_volume,omitempty"`    // docker volume to snapshot/restore
	SnapshotBucket string `json:"snapshot_bucket,omitempty"` // MinIO bucket
	SnapshotKey    string `json:"snapshot_key,omitempty"`    // MinIO object key

	// 30.6.1 c1-J3a — the worker validates a seeded volume tarball BEFORE extract
	// (two passes + ETag match). This caps the uncompressed bomb budget; 0/absent
	// means the worker's own default of 512 GiB (the API default at
	// CORE/API/services/stack_seed.py:74). The API passing it is c1-J3b.
	MaxUncompressedBytes int64 `json:"max_uncompressed_bytes,omitempty"`

	// GA.ASW.5c — logical_backup/logical_restore (an ephemeral pg_dump/mysqldump/
	// mongodump client container against a running DB service, over the stack's
	// own compose network) built server-side by
	// services/stack_logical_backup.py:_build_spec and dispatched by
	// routers/persistent_stacks/_shared.py:_dispatch_logical_task. StackEngine is
	// informational (log lines only); the worker never branches executor
	// behaviour on it — StackCommand is already the fully-built argv for
	// whichever engine the API detected.
	StackEngine  string   `json:"stack_engine,omitempty"`  // postgres | mysql | mariadb | mongo (log/metrics only)
	StackImage   string   `json:"stack_image,omitempty"`   // client image to run (e.g. postgres:16)
	StackNetwork string   `json:"stack_network,omitempty"` // compose network to attach the ephemeral client to
	StackService string   `json:"stack_service,omitempty"` // DB service hostname on that network
	StackCommand []string `json:"stack_command,omitempty"` // argv (never carries the password)
	StackPath    string   `json:"stack_path,omitempty"`    // dump file path inside the client container

	// GA.ASW.10.6 — streaming zero-downtime DB migration (db_seed_replica,
	// db_replica_status, db_replica_promote, db_source_stop, db_source_start;
	// services/stack_streaming_migration.py builds the spec, stackreplica.go
	// executes it). StackMode picks which of the two execution shapes the op
	// uses — read off the API's own spec dict ("mode"), never inferred from
	// StackOp's name, so a future streaming-migration op can reuse either
	// shape with no worker code change:
	//   "ephemeral_client" — run StackImage on StackNetwork (docker run),
	//                         exactly like logical_backup/logical_restore.
	//                         When StackVolume is also set (db_seed_replica),
	//                         it is bind-mounted at filepath.Dir(StackPath) so
	//                         the seed writes real data into the TARGET
	//                         stack's own data volume.
	//   "exec_in_service"   — `docker compose exec` StackCommand INSIDE the
	//                         already-running StackService container of
	//                         StackProject; no client image, no password.
	// Reuses StackEngine/StackImage/StackNetwork/StackService/StackCommand/
	// StackPath/ContainerEnv/StackVolume from the logical-op fields above.
	StackMode string `json:"stack_mode,omitempty"`

	// 30.6.1 c1-J4 (GX-c1 restore guard) — the probe_empty op's inputs, sent by
	// routers/persistent_stacks/persistent_stacks.py:_probe_stack_empty before an
	// EXTERNAL (seed-committed) restore is allowed to dispatch anything.
	// ProbeVolumes are the stack's docker volume names to count (the API's
	// _stack_volumes shape: "<project>_<name>"); ProbeLogical asks for the
	// stack DB service's user-table count too. Both are read-only inputs — the
	// worker validates every name against the project prefix before any exec.
	ProbeVolumes  []string `json:"probe_volumes,omitempty"`
	ProbeLogical  bool     `json:"probe_logical,omitempty"`

	// Blue/green migrate (add-persistent-stacks §5). The "migrate" op moves a
	// stack to StackProject (new generation): for each base volume it snapshots
	// {MigrateFromProject}_{vol} -> MinIO -> restores into {StackProject}_{vol},
	// brings the new project up (ComposeSpec), then drains the old project.
	MigrateFromProject string   `json:"migrate_from_project,omitempty"`
	MigrateVolumes     []string `json:"migrate_volumes,omitempty"` // base volume names (compose keys)
	// MigrateFreeze (add-persistent-stacks §5.2): stop the old generation BEFORE the
	// final snapshot so no write is missed during the migrate window (a brief
	// write-freeze for write-heavy stacks). Default false = live migrate.
	MigrateFreeze bool `json:"migrate_freeze,omitempty"`

	// Mesh re-up (live-mesh-reenroll). The "mesh_reup" op re-registers the
	// stack's tailscale sidecar (container "ts-"+MeshService) against a new
	// login server, without touching the stack's own containers. MeshAuthkey
	// is a one-shot credential: the server stamps it fresh per task and the
	// worker must never persist or log it (see stackrunner.go composeCmdRedacted).
	MeshService     string `json:"mesh_service,omitempty"`
	MeshLoginServer string `json:"mesh_login_server,omitempty"`
	MeshAuthkey     string `json:"mesh_authkey,omitempty"`
	MeshHostname    string `json:"mesh_hostname,omitempty"`

	// Weights to materialize for this stack's models before the stack comes
	// up (offline HF cache priming). Shape mirrors the API side's payload.
	Weights []WeightRef `json:"weights,omitempty"`

	// Container command override. When non-empty, the executor passes
	// it to Docker as Cmd (ContainerConfig.Cmd), overriding the image's
	// baked-in CMD. Set only by platform-owned system task types
	// (variant_smoke runs /opt/mod_smoke/probe.py inside the variant
	// image; future variant_bench will follow the same pattern). User
	// workflow payloads never set this — they run the image's default
	// entrypoint.
	Cmd []string `json:"cmd,omitempty"`

	// workflow-agentic-execution-loop §wizard-test-run-dispatch —
	// sandbox-mode metadata plumbed from the orchestrator. Purpose
	// 'production' (default / empty) means today's behavior;
	// 'wizard_test_run' means the worker enforces SandboxTimeoutS +
	// SandboxMaxOutputMB and routes output writes via OutputPrefix
	// inside MinioBucket. Future purposes (event_triggered, training_
	// step, autonomous_canary, adversary_dispatch) reuse this field.
	Purpose            string `json:"purpose,omitempty"`
	OutputPrefix       string `json:"output_prefix,omitempty"`
	SandboxTimeoutS    int    `json:"sandbox_timeout_s,omitempty"`
	SandboxMaxOutputMB int    `json:"sandbox_max_output_mb,omitempty"`

	// StorageMigration (WS4.Q17.16, owner decision A.5) is set only for the
	// BYO-S3 migrate-on-activate copy job. When non-nil the worker performs
	// a pure Go S3-to-S3 streaming copy+verify (minio-go) instead of any
	// container/compose path — mirrors the StackOp bypass above. Set only by
	// the server dispatcher (services/storage_migration.py); never present
	// on an ordinary node task.
	StorageMigration *StorageMigrationSpec `json:"storage_migration,omitempty"`

	// MoveOut (IMPL-BYO-MOVE-OUT-B-GO-WORKER, design s2/s5) is set only on a
	// BYO move-out CHUNK task. The chunk carries the SAME nested
	// storage_migration spec (source_bucket = the tenant's own BYO bucket,
	// dest_bucket = the house bucket or another BYO connection's bucket), so
	// the credential mint and the copy+verify walk are reused verbatim, plus
	// the flat markers services/storage_migration.py:enqueue_move_out stamps
	// ("move_out": true, "move_out_job_id", "move_out_connection_id" — read
	// off that enqueue call, not assumed). The worker branches on MoveOut
	// BEFORE the StorageMigration branch: a move-out chunk runs
	// executor.RunStorageMoveOutChunk (resumable skip, a missing source key
	// is RECORDED not fatal, a sha-less entry is verified against the source
	// bytes) instead of the migrate-on-activate runner, and NEVER flips
	// is_active and NEVER deletes a source object. Absent on every other
	// task, so every other payload decode is unchanged.
	//
	// The API's move-status (chunks_done/chunks_total) is accounted API-side
	// from the chunk tasks' own statuses; the worker reports the per-object
	// tally through ReportStorageMigrationProgress exactly as a migrate-on-
	// activate chunk does (the endpoint's predicate is_migration_task
	// accepts a move-out chunk: same node_kind + output_namespace).
	//
	// NOTE: the API's credential mint (_mint_storage_migration_credentials,
	// routers/worker_tasks.py:4941) resolves the SOURCE creds from
	// payload["source_connection_id"], which enqueue_move_out does NOT stamp
	// (it stamps only dest_connection_id). With no source id the mint hands
	// the HOUSE creds, so a move-out whose SOURCE is a BYO connection would
	// read the wrong store. That is an API-side gap (outside this leg's
	// file set) — reported, not papered over here.
	MoveOut             bool  `json:"move_out,omitempty"`
	// MoveOutJobID is the storage_move_jobs row this chunk belongs to; the
	// API's complete_move_out hook accounts the chunk onto it. The worker
	// only logs it (it never writes the job row). 0 when absent.
	MoveOutJobID        int64 `json:"move_out_job_id,omitempty"`
	// MoveOutConnectionID is the BYO connection being moved out of (the
	// SOURCE row). 0 when absent.
	MoveOutConnectionID int   `json:"move_out_connection_id,omitempty"`

	// HFCacheRelayout (GSEC R-159) is set only for the one-time per-tenant
	// HF weight-cache re-layout: when non-nil the worker performs the moves
	// natively on the host (rename within the same filesystem, never
	// delete) instead of any container/compose path — mirrors the
	// StorageMigration bypass above. Set only by the server dispatcher
	// (services/hf_cache_relayout.py); never present on an ordinary node
	// task.
	HFCacheRelayout *HFCacheRelayoutSpec `json:"hf_cache_relayout,omitempty"`

	// HostExec* (hx.7d, opsx worker-host-exec) — set only for
	// task_type "host-exec". The /workers/tasks/poll response's "payload"
	// is task.task_payload FORWARDED VERBATIM (see TaskType comment above),
	// and worker_host_exec_service.py enqueues job_id/argv/cwd/env/run_as/
	// timeout_s/mode at the TOP LEVEL of that dict (no "host_exec" wrapper
	// — confirmed against the enqueue call, not assumed). So these decode
	// as flat TaskPayload fields, not a nested struct. Zero/nil for every
	// container-processing task.
	HostExecJobID    int64             `json:"job_id,omitempty"`
	HostExecArgv     []string          `json:"argv,omitempty"`
	HostExecCwd      string            `json:"cwd,omitempty"`
	HostExecEnv      map[string]string `json:"env,omitempty"`
	HostExecRunAs    string            `json:"run_as,omitempty"`
	HostExecTimeoutS int               `json:"timeout_s,omitempty"`
	HostExecMode     string            `json:"mode,omitempty"`
}

// HFCacheRelayoutSpec is the full instruction set for the R-159 re-layout:
// the flat HF cache root on this host, and per-repo-dir move decisions made
// by the API (the owner of record for who fetched what). Dest values are
// relative to Root: "public", "tenants/<id>", or "quarantine" — each a
// subtree under the mod-models layout (plans key = repo dir name, already
// sanitized org__name). The worker only MOVES directories (rename) and
// reports counts; it never deletes anything (ISO: no hard deletes).
type HFCacheRelayoutSpec struct {
	Root  string                     `json:"root"`
	Plans map[string]HFCachePlanInfo `json:"plans"`
}

// HFCachePlanInfo is one repo dir's move decision. Dir is the on-disk
// directory name under <Root>/mod-models (the sanitized org__name); the
// plan map key is the repo id (org/name) for API-side readability.
type HFCachePlanInfo struct {
	Dir      string `json:"dir"`
	Dest     string `json:"dest"` // "public" | "tenants/<id>" | "quarantine"
	TenantID *int   `json:"tenant_id,omitempty"`
	Reason   string `json:"reason,omitempty"`
}

// StorageMigrationSpec is the full instruction set for one migrate-on-
// activate copy job: which bucket to read from, which bucket (the
// connection being activated) to write to, and the exact object list.
// Credentials for BOTH buckets come from GetCredentials
// (Credentials.MinIO = dest, Credentials.MinIOSource = source) — never
// carried in this payload.
type StorageMigrationSpec struct {
	StorageConnectionID int                      `json:"storage_connection_id"`
	SourceBucket        string                   `json:"source_bucket"`
	DestBucket          string                   `json:"dest_bucket"`
	Objects             []StorageMigrationObject `json:"objects"`
	// FIX-BYO-MIGRATION-SKIP-MISSING — opt-in per activation (PROOF-BOX-J-2
	// F1: dangling content_blobs rows whose source object is gone aborted the
	// whole migration with migrated_object_count 0). DEFAULT FALSE: a missing
	// source key stays fail-closed exactly as before. When true, a source key
	// the source backend no longer holds is skipped, counted and LISTED
	// (capped) in the completion report — never silently dropped.
	SkipMissingSource bool `json:"skip_missing_source,omitempty"`
}

// StorageMigrationObject is one object to copy. Key/SHA256 are identical
// for every MOD tenant object today (content_blobs.id IS the sha256 AND the
// storage key), but are carried separately so a future non-content-
// addressed source can populate them differently.
type StorageMigrationObject struct {
	Key       string `json:"key"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}

// WeightRef describes a set of model weight files to materialize before a
// stack's model service starts (HF offline cache priming). JSON tags match
// the API side's payload shape (internal/weights Ref/File on the worker).
type WeightRef struct {
	ArtifactRef string       `json:"artifact_ref"`
	RepoID      string       `json:"repo_id"`
	Revision    string       `json:"revision"`
	Files       []WeightFile `json:"files"`
}

// WeightFile is one file within a WeightRef.
type WeightFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// StackFileMode is the wire type for a stack file's mode. The API serializes
// it as a JSON NUMBER (python `0o400` marshals to 256); hand-built payloads
// may send the octal string ("0400"). Decoding the number into a plain string
// field made json.Unmarshal error on the WHOLE element and silently drop the
// mode — the field is only meaningful when it survives the wire. Both shapes
// normalize to the same form (octal digits, e.g. "400").
type StackFileMode string

// UnmarshalJSON accepts a JSON number (0o400 -> 256 -> "400"), an octal
// string ("0400" stays "0400"), null/absent ("").
func (m *StackFileMode) UnmarshalJSON(b []byte) error {
	if len(b) == 0 || string(b) == "null" {
		*m = ""
		return nil
	}
	if b[0] == '"' {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		*m = StackFileMode(s)
		return nil
	}
	var n uint32
	if err := json.Unmarshal(b, &n); err != nil {
		return err
	}
	*m = StackFileMode(fmt.Sprintf("%o", n&0o777))
	return nil
}

// StackSecretFile (GX.S10b) is one Vault-backed secret delivered to a stack as
// a file. Content is base64-encoded in the payload; the worker decodes it into
// a per-stack secrets dir (mode 0400) and composes it in at ${MOD_SECRETS_DIR}.
// The content must NEVER be logged.
type StackSecretFile struct {
	Name       string        `json:"name"`           // file name within the secrets dir
	ContentB64 string        `json:"content_b64"`    // base64-encoded secret content
	Mode       StackFileMode `json:"mode,omitempty"` // file mode (e.g. "0400" or 256); default 0400
}

// StackStorageFile (GX.S10b) is a tenant storage file delivered to a stack.
// The worker streams it from a short-TTL presigned URL into a per-stack on-disk
// dir, enforcing Size and verifying SHA256 when given, then composes it in at
// ${MOD_FILES_DIR}. The URL query string carries the presign signature and must
// NEVER be logged.
type StackStorageFile struct {
	Name   string        `json:"name"`             // file name within the files dir
	URL    string        `json:"url"`              // short-TTL presigned GET URL
	Size   int64         `json:"size"`             // expected byte size (abort past this)
	SHA256 string        `json:"sha256,omitempty"` // expected sha256 (hex), verified when given
	Mode   StackFileMode `json:"mode,omitempty"`   // file mode (e.g. "0444" or 284); default 0444
}

// SecretRef maps a tenant secret to a container environment variable.
type SecretRef struct {
	SecretName string `json:"secret_name"` // name in tenant's secret store
	EnvVar     string `json:"env_var"`     // env var name in the container
}

// TailnetAnchorKey is one anchor's single-use enrollment credential for a
// per-task tsegress sidecar (GB.EGR.D.9d), mirroring the {login_server, key}
// pair persistent_stacks/_shared.py._tailnet_egress_anchors mints for stacks.
type TailnetAnchorKey struct {
	LoginServer string `json:"login_server"`
	AuthKey     string `json:"auth_key"`
}

// TailnetEgress carries the per-anchor credentials the API minted for this
// task's declared tailnet destinations. Either field may be nil — an anchor
// with no control client (or a failed mint) is skipped, exactly like the
// stack injector; the worker brings up a sidecar only for the anchors present
// here.
type TailnetEgress struct {
	Site  *TailnetAnchorKey `json:"site,omitempty"`
	Cloud *TailnetAnchorKey `json:"cloud,omitempty"`
}

// Credentials holds temporary credentials for MinIO and Harbor.
// In direct mode: fetched from Keycloak/Vault directly.
// In remote mode: fetched from the API's /tasks/{id}/credentials endpoint.
//
// Two Harbor entries exist because the platform runs two registries:
//   - Harbor:       registry.example.com — platform images (modtim/*)
//   - TenantHarbor: example.com — wizard-built tenant images
//
// pullImage picks between them by inspecting the image hostname; sending
// the wrong creds to either registry produces a 401 even though one set
// is valid for the other registry.
type Credentials struct {
	MinIO MinIOCredentials `json:"minio"`
	// MinIOSource (WS4.Q17.16) is the OLD backend's credentials for a
	// storage_migration task — house tenant-scoped creds or a prior BYO-S3
	// connection's own key, minted the same way MinIO (the dest, i.e. the
	// connection being activated) is. Zero-value (empty AccessKey) on every
	// non-migration task; older APIs simply omit the key.
	MinIOSource   MinIOCredentials         `json:"minio_source"`
	Harbor        HarborCredentials        `json:"harbor"`
	TenantHarbor  HarborCredentials        `json:"tenant_harbor"`
	LocalRegistry LocalRegistryCredentials `json:"local_registry"`
	// ManagerHarbor carries the manager-mediated Harbor credentials minted
	// by the manager on the task's behalf (registry + proxy project +
	// upstream hosts + short-lived token + optional k8s robot account).
	ManagerHarbor *ManagerHarbor `json:"manager_harbor,omitempty"`
	// BuildToken is a per-task, scoped, short-lived API credential for the
	// wizard build-DAG stages (minted by the API's /credentials endpoint —
	// see API utils/build_task_token). It's injected into the build-task
	// container as API_KEY in place of the long-lived platform master key.
	// Empty for non-build tasks.
	BuildToken string `json:"build_token"`
	// ModelCacheToken (GF.C6.5) is the per-task, tenant-scoped mod-model-cache
	// token (API utils/build_task_token `mint_model_cache_token`, typ
	// `mod-model-cache-v1`), minted by the /credentials endpoint when the
	// platform model cache is enabled. The worker swaps the container's
	// HF_TOKEN for it whenever the container is pointed at a MOD model cache
	// (see modelcache_manager.go): the cache verifies the token, resolves
	// the tenant's REAL HF token itself, and stamps the download with tenant
	// attribution — so the tenant's raw HF token never ships to a container.
	// Empty when the API omits it (cache off, or an API predating GF.C6.5);
	// an empty value leaves HF_TOKEN untouched everywhere.
	ModelCacheToken string `json:"model_cache_token,omitempty"`
}

// ManagerHarbor is the manager-mediated Harbor registry credential set.
// The manager mints a short-lived token (and optionally a k8s robot
// account) and hands it to the worker so the task can pull/push images
// through the manager's Harbor proxy.
type ManagerHarbor struct {
	Registry      string         `json:"registry"`
	ProxyProject  string         `json:"proxy_project"`
	UpstreamHosts []string       `json:"upstream_hosts"`
	Token         string         `json:"token"`
	ExpiresAt     time.Time      `json:"expires_at"`
	K8sRobot      *RegistryRobot `json:"k8s_robot,omitempty"`
}

// RegistryRobot is a Harbor robot account credential (used for k8s image pulls).
type RegistryRobot struct {
	Username  string    `json:"username"`
	Password  string    `json:"password"`
	ExpiresAt time.Time `json:"expires_at"`
}

// LocalRegistryCredentials for the local container registry (user containers).
type LocalRegistryCredentials struct {
	Registry string `json:"registry"` // internal: "registry:5000"
	External string `json:"external"` // for remote workers: "registry.example.com"
}

// MinIOCredentials are STS temporary credentials for MinIO access.
type MinIOCredentials struct {
	AccessKey    string    `json:"access_key"`
	SecretKey    string    `json:"secret_key"`
	SessionToken string    `json:"session_token"`
	Endpoint     string    `json:"endpoint"` // mc-compatible host string
	MCHost       string    `json:"mc_host"`  // MC_HOST_myminio value
	ExpiresAt    time.Time `json:"expires_at"`
	// RefreshIntervalS (GSEC Q21.7) is the API's recommended refresh
	// cadence for these creds, in seconds (populated alongside
	// ExpiresAt when a TTL exists). Zero on old APIs: the worker then
	// falls back to its platform-tunable base interval.
	RefreshIntervalS int `json:"refresh_interval_s,omitempty"`
}

// HarborCredentials for Docker registry login.
type HarborCredentials struct {
	Registry string `json:"registry"`
	Username string `json:"username"`
	Password string `json:"password"`
}

// EgressDenied is a single host the worker was blocked from reaching during a task.
type EgressDenied struct {
	Host   string `json:"host"`
	Port   int    `json:"port"`
	Reason string `json:"reason"`
	Count  int    `json:"count"`
}

// EgressReport summarizes the network egress observed while running a task.
type EgressReport struct {
	Mode            string         `json:"mode"`
	AllowedHosts    []string       `json:"allowed_hosts"`
	Denied          []EgressDenied `json:"denied,omitempty"`
	DeniedTruncated bool           `json:"denied_truncated,omitempty"`
}

// TaskResult is what the worker sends back after executing a task.
type TaskResult struct {
	Outputs []OutputFile  `json:"outputs"`
	LogTail string        `json:"log_tail"`
	Egress  *EgressReport `json:"egress,omitempty"`
	// PeakVRAMMB is the highest per-container VRAM usage (MiB) the local
	// VRAMSampler observed while this task's container ran (14.6b-2). Omitted
	// when never sampled (e.g. non-GPU task, or sampler never ticked before
	// completion). NOTE: as of this writing the API's
	// TaskCompleteRequest/report_task_complete handler (worker_tasks.py)
	// does not read or persist this field — it is sent for forward
	// compatibility only until the API is updated to store it.
	PeakVRAMMB int64 `json:"peak_vram_mb,omitempty"`
	// EphemeralAppliedShmMB / EphemeralAppliedTmpfsMB are the POST-CLAMP
	// values the worker actually applied to the processing container's
	// /dev/shm and /tmp tmpfs (node ephemeral-memory overrides, WS1.38c),
	// so the API records what ran rather than what the node requested.
	// EphemeralClamped is true when either requested value exceeded the
	// worker's ephemeral cap and was reduced. All three are omitted for
	// tasks that declared no ephemeral memory (nothing was applied).
	EphemeralAppliedShmMB   int64 `json:"ephemeral_applied_shm_mb,omitempty"`
	EphemeralAppliedTmpfsMB int64 `json:"ephemeral_applied_tmpfs_mb,omitempty"`
	EphemeralClamped        bool  `json:"ephemeral_clamped,omitempty"`
	// HFCacheReport (GSEC R-159) is the per-worker HF cache re-layout report
	// (moved / verified / quarantined counts). Serialized as a flat
	// "hf_cache_report" object in the completion body — NOT as outputs[0]
	// (outputs are file entries; the completion handler records them in the
	// workflow_node_outputs table, which a report dict would corrupt).
	// Absent on every ordinary task. The API's completion hook
	// (services/hf_cache_relayout.py) reads it back from result_payload.
	HFCacheReport *HFCacheReport `json:"hf_cache_report,omitempty"`
	// LockdownReport (GSEC FX.115 p2) is the per-node-container lockdown
	// verdict the executor produced (node_lockdown.go planMap): a flat map of
	// the SAME keys its "lockdown-report" log line carries (node_kind, exempt
	// + reason, and per flag mode/missing/would_apply/applied). It rides the
	// completion body so the API can answer "what WOULD break under enforce"
	// per (node kind, flag) without scraping logs. nil — key omitted — for
	// tasks that never created a node container (native/k8s/stack ops).
	LockdownReport map[string]any `json:"lockdown_report,omitempty"`
	// WorkerSignature (fortress 2.3b) is the worker's own ephemeral-key
	// signature over its contribution to this task's run proof chain —
	// independent of the platform's Ed25519 bundle signature (fortress 2.1/
	// 2.2), so neither key alone can forge the other's claim. It rides the
	// completion body as a flat "worker_signature" object (same idiom as
	// hf_cache_report / lockdown_report), paired with the flat
	// "worker_contribution" object: the API verifier
	// (services/run_proof_bundle.py verify_worker_signature) checks the
	// signature against canonical_worker_payload(contribution), so the
	// payload bytes must travel with the signature, not be rebuilt
	// API-side. Both keys are omitted together when no signature was
	// produced (signing failure, signer unavailable) — the completion body
	// is byte-for-byte unchanged for those tasks.
	WorkerSignature    *WorkerSignature       `json:"worker_signature,omitempty"`
	WorkerContribution map[string]interface{} `json:"worker_contribution,omitempty"`
	// UsageTokens (30.6.11 c11-J3) is the token metering the worker read back
	// out of the processing container's /data/output/result.json (the stack-model-call
	// helper writes {"model":…,"usage":{prompt_tokens,completion_tokens,total_tokens}}).
	// Serialized as a flat "usage_tokens" object in the completion body — same idiom
	// as hf_cache_report / lockdown_report — so the API can copy it onto NodeRun.usage_tokens.
	// nil (key omitted) for every task that wrote no usage: old completion bodies are
	// byte-for-byte unchanged, so old API consumers are unaffected. The worker never
	// sets node_id — the API already knows which NodeRun this task belongs to.
	UsageTokens *UsageTokens `json:"usage_tokens,omitempty"`
	// VerifyTables (30.6.1 c1-J3b) is the per-table ROW COUNT of a restored
	// logical snapshot, counted READ-ONLY against the stack's own DB service
	// after the restore exited 0 (stacklogical.go:logicalVerifyCounts). It rides
	// the completion body as a flat "verify_tables" object — same idiom as
	// hf_cache_report / lockdown_report / usage_tokens — so the API's completion
	// handler (routers/worker_tasks.py _record_rehearsal_result) can store it in
	// storage_refs.snapshots[id].verify.tables. VerifyTruncated is true when the
	// count batch hit its caps (maxVerifyTables tables, or a hostile/oversized
	// table list) so the map is known-incomplete rather than silently short.
	// Absent (nil) on EVERY other task: only logical_restore produces them, so
	// every other completion body is byte-for-byte unchanged.
	VerifyTables   map[string]int64 `json:"verify_tables,omitempty"`
	VerifyTruncated bool            `json:"verify_truncated,omitempty"`
	// StorageMigrationReport (FIX-BYO-MIGRATION-SKIP-MISSING) is the skip
	// report a BYO-S3 copy job produced when the activation opted in
	// (StorageMigrationSpec.SkipMissingSource): the COUNT of source keys the
	// source backend no longer holds (dangling content_blobs rows — PROOF-
	// BOX-J-2 F1) and up to storageMigrationMissingKeyCap of those keys.
	// It rides the completion body as a flat "storage_migration_report"
	// object — same idiom as hf_cache_report / lockdown_report — so the
	// API's completion hook can mark the migration verified-with-skips
	// instead of failing it, and the migration-status response can make the
	// skipped count obvious. Absent (nil, key omitted) for every task that
	// ran no storage migration or ran with the default fail-closed flag:
	// every other completion body is byte-for-byte unchanged.
	StorageMigrationReport *StorageMigrationReport `json:"storage_migration_report,omitempty"`
}

// StorageMigrationReport is the wire twin of the API's skip report.
// MissingSourceKeys is capped worker-side (storageMigrationMissingKeyCap);
// MissingSourceCount is the TRUE total (it keeps counting past the cap).
type StorageMigrationReport struct {
	MissingSourceCount int      `json:"missing_source_count"`
	MissingSourceKeys  []string `json:"missing_source_keys"`
}

// UsageTokens is the wire twin of the API's RunUsage models list:
// {"models":[{"model":str,"prompt_tokens":int,"completion_tokens":int,"total_tokens":int,"images":int}]}.
// Typed (not RawMessage) so the shape is compile-checked against the API schema;
// transport already uses typed structs for every other worker-supplied report
// (HFCacheReport, WorkerSignature) and reserves RawMessage for opaque payloads.
// The per-count fields are omitted when the worker dropped them (negative /
// >1e9 values are dropped per S9), never serialized as 0.
type UsageTokens struct {
	Models []UsageModel `json:"models"`
}

type UsageModel struct {
	// Model is required: a usage entry with a missing / malformed model name is
	// dropped WHOLESALE by the executor (S9), never sent with an empty name.
	// ≤128 chars, [A-Za-z0-9._:/-] only — the same charset the API's S8 model
	// regex enforces, so the worker never sends a value the API would 422.
	Model            string `json:"model"`
	PromptTokens     int64  `json:"prompt_tokens,omitempty"`
	CompletionTokens int64  `json:"completion_tokens,omitempty"`
	TotalTokens      int64  `json:"total_tokens,omitempty"`
	Images           int64  `json:"images,omitempty"`
}

// WorkerSignature mirrors executor.WorkerProofSignature (proof_signer.go)
// with IDENTICAL json tags. transport cannot import executor (executor
// already imports transport — a cycle), so the wire shape is restated here
// and kept in lockstep by the tests; the executor package remains the
// definition of truth for what the signer produces.
type WorkerSignature struct {
	Algorithm     string `json:"alg"`
	KeyID         string `json:"key_id"`
	PublicKeyB64  string `json:"public_key_b64"`
	PayloadSHA256 string `json:"payload_sha256"`
	SignatureB64  string `json:"signature_b64"`
	SignedAt      string `json:"signed_at"`
}

// HFCacheReport is the terminal tally of one R-159 re-layout run: how many
// repo dirs were moved, how many of the moved ones the worker re-verified
// in place, and how many were quarantined (moved, never deleted).
type HFCacheReport struct {
	Moved       int `json:"moved"`
	Verified    int `json:"verified"`
	Quarantined int `json:"quarantined"`
}

// OutputFile describes a single output uploaded to MinIO.
type OutputFile struct {
	ContentHash   string `json:"content_hash"`
	FileName      string `json:"file_name"`
	FileExtension string `json:"file_extension"`
	SizeBytes     int64  `json:"size_bytes"`
	// Fortress 3.5 — staging_key names a TRANSIENT scan-staging blob
	// (`scan-staging/{task_id}/<hash>`) the worker staged so the API's
	// pre-store consult can fetch real bytes for the output scanner
	// (task_output_scan.py _validate_staging_key / _scan_staged_bytes).
	// omitempty keeps every existing wire shape byte-identical: the /complete
	// manifest-parsed OutputFiles never set it.
	StagingKey string `json:"staging_key,omitempty"`
}

// ResourceSnapshot is the worker's actual resource state, sent during phase validation.
type ResourceSnapshot struct {
	RAMAvailableMB     float64 `json:"ram_available_mb"`
	VRAMFreeMB         float64 `json:"vram_free_mb"`
	VRAMLargestBlockMB float64 `json:"vram_largest_block_mb"`
	CPUIdlePercent     float64 `json:"cpu_idle_percent"`
	GPUPowerState      string  `json:"gpu_power_state"`
	GPUTemperatureC    float64 `json:"gpu_temperature_c"`
	DiskFreeGB         float64 `json:"disk_free_gb"`
	NICUtilizationPct  float64 `json:"nic_utilization_pct"`
	ActiveContainers   int     `json:"active_containers"`
	ContainersStarting int     `json:"containers_starting"`
	ThermalThrottled   bool    `json:"thermal_throttled"`
}

// ValidationResponse is the API's answer to a phase validation request.
type ValidationResponse struct {
	Proceed bool   `json:"proceed"`
	Reason  string `json:"reason,omitempty"`
	Action  string `json:"action,omitempty"` // "reschedule", "abort", ""
}

// ResourceSample is a single time-series data point for task telemetry.
type ResourceSample struct {
	Timestamp     time.Time `json:"timestamp"`
	Phase         string    `json:"phase"`
	CPUPercent    float64   `json:"cpu_percent"`
	RAMMB         float64   `json:"ram_mb"`
	VRAMMB        float64   `json:"vram_mb"`
	GPUUtilPct    float64   `json:"gpu_util_pct"`
	GPUTempC      float64   `json:"gpu_temp_c"`
	NetRxMbps     float64   `json:"net_rx_mbps"`
	NetTxMbps     float64   `json:"net_tx_mbps"`
	DiskReadIOPS  float64   `json:"disk_read_iops"`
	DiskWriteIOPS float64   `json:"disk_write_iops"`

	// Extended v2 fields
	SingleThreadDetected *bool    `json:"single_thread_detected,omitempty"`
	ContainerDiskReadMB  *float64 `json:"container_disk_read_mb,omitempty"`
	ContainerDiskWriteMB *float64 `json:"container_disk_write_mb,omitempty"`
	VRAMAllocRateMBs     *float64 `json:"vram_alloc_rate_mb_s,omitempty"`
	ProcessCount         *int     `json:"process_count,omitempty"`
	GPUClockMHz          *float64 `json:"gpu_clock_mhz,omitempty"`
	SwapUsedMB           *float64 `json:"swap_used_mb,omitempty"`
}

// ServerStateReceiver is implemented by transports that can accept
// server-pushed coordination signals (drain, update available, server
// updating) from the heartbeat loop. Previously the worker's main loop
// used a concrete `tp.(*APITransport)` type assertion here, which
// silently excluded DirectTransport (embedded + datacenter workers) —
// the server's "you should drain for an update" signal was received
// but dropped on the floor, so those workers would never self-update
// after an admin clicked Update. Using this interface makes both
// transports participate.
type ServerStateReceiver interface {
	SetServerState(updating, drain, updateAvail bool, latestVersion string)
}

// ManagerFailover is implemented by transports that can be repointed at a
// different manager (multi-region mesh failover). The heartbeat loop calls
// SetBaseURL after it rotates to a surviving manager so task-claim/report
// follow the worker to the new manager.
type ManagerFailover interface {
	SetBaseURL(url string)
}

// SelfUpdateChecker is implemented by transports that expose the
// coordination-state flags the main loop checks before triggering a
// self-update (drain complete? new binary available? is the server
// mid-update so we should defer?). Same rationale as ServerStateReceiver:
// interface-based so DirectTransport can delegate to its embedded
// APITransport and embedded workers get full self-update support.
type SelfUpdateChecker interface {
	Draining() bool
	UpdateAvailable() (bool, string)
	ServerUpdating() bool
}

// FetchLoopMarkerSetter is implemented by transports that can be handed the
// worker's fetch-loop liveness stamp (heartbeat.MarkFetchLoopIteration). BUG.B83:
// the stamp was only ever touched by cmd/worker's job loop, but an embedded
// worker's job loop spends ALL its idle time inside DirectTransport.WaitForTask's
// own pub/sub/ticker loop — the polls that loop makes every ~30s never return
// control to main.go, so the watchdog read a 10-minute-old stamp on a worker
// that was polling normally and force-exited it every ~11 minutes. Registering
// the stamp lets the transport refresh it on every completed poll round-trip,
// which is the honest definition of "the fetch loop is alive". Interface-based
// (not concrete-type-based) for the same reason as ServerStateReceiver: the
// embedded workers that hit this bug are DirectTransports.
type FetchLoopMarkerSetter interface {
	SetFetchLoopMarker(fn func())
}

// Transport is the interface that both Direct (Redis) and API (HTTPS) modes implement.
// The executor doesn't care which transport it's using.
type Transport interface {
	// WaitForTask blocks until a task is available or the context is cancelled.
	// Returns nil task if context is done (shutdown).
	WaitForTask(ctx context.Context) (*Task, error)

	// WaitForStackOp is the NOAH claim-gate bypass (2026-09-29): a short poll
	// scoped server-side to ONLY this worker's persistent-stack-* ops tasks
	// (deploy/stop/logs/migrate — see task_payload.node_kind). The main loop
	// calls this instead of WaitForTask while noah.shouldClaim() is false, so
	// a worker hosting a stack that is itself the source of the "load" NOAH
	// sees (e.g. a model still loading) can still claim and run ops against
	// that stack — including the very redeploy task that would relieve it.
	// Returns (nil, nil) when nothing is available, same as WaitForTask.
	WaitForStackOp(ctx context.Context) (*Task, error)

	// ReportStarted tells the manager this task is now executing.
	ReportStarted(ctx context.Context, taskID int, estimatedDuration time.Duration) error

	// ReportComplete sends results back to the manager.
	ReportComplete(ctx context.Context, taskID int, result TaskResult) error

	// ReportFailed tells the manager the task failed. shouldRetry=true requests requeue.
	//
	// oomKilled reports that the KERNEL killed the container for memory, which
	// the worker learns from Docker's State.OOMKilled. It is passed as its own
	// flag rather than folded into errMsg because the manager acts on it: an
	// OOM is a property of the HOST, so the task may succeed elsewhere, whereas
	// an ordinary crash will fail identically on every worker. The worker only
	// reports — it does not decide what happens next.
	//
	// errorDetail is the failing sidecar's raw, untruncated stderr (empty when
	// the failure isn't sidecar-shaped) — calibration-health-fixtures-and-timeouts
	// §3. Kept separate from errMsg rather than appended so the manager can
	// store it in its own column without re-parsing errMsg.
	ReportFailed(ctx context.Context, taskID int, errMsg string, shouldRetry bool, logTail string, oomKilled bool, errorDetail string) error

	// GetCredentials fetches fresh MinIO STS + Harbor credentials for a task.
	GetCredentials(ctx context.Context, taskID int) (*Credentials, error)

	// MintRunToken obtains a short-lived run_task token for a task from the
	// API's worker-authenticated mint endpoint (POST
	// /workers/tasks/{id}/run-token). Used by the worker's local token
	// endpoint to vend per-task credentials to nodes that present a valid
	// per-task nonce. The worker never signs tokens itself — it relays the
	// API's. Returns the token and its unix-seconds expiry.
	MintRunToken(ctx context.Context, taskID int) (token string, exp int64, err error)

	// SendLog streams log lines for a running task.
	SendLog(ctx context.Context, taskID int, lines []string) error

	// SendCheckpoints reports how far a node has got. Separate from SendLog
	// because logs are SIGNALLING and checkpoints are durable state — a line
	// that only reaches Redis is fine for the first and useless for the
	// second, which is exactly the bug this split fixes.
	SendCheckpoints(ctx context.Context, taskID int, cps []Checkpoint) error

	// ValidatePhase asks the scheduler if actual resources match the phase plan.
	// The worker calls this before each lifecycle phase to verify assumptions.
	ValidatePhase(ctx context.Context, taskID int, phase string, actual ResourceSnapshot) (*ValidationResponse, error)

	// SendResourceSamples sends batched time-series resource data to the API.
	// Called periodically (every 30s) during task execution.
	SendResourceSamples(ctx context.Context, taskID int, samples []ResourceSample) error

	// ResolveSecrets resolves secret references for a task. Returns env var → plaintext value.
	// Remote workers call the API; direct workers can use Vault.
	ResolveSecrets(ctx context.Context, taskID int) (map[string]string, error)

	// ReportStorageMigrationProgress (WS4.Q17.16) persists how far a
	// storage_migration task has got — the API writes objectCount/byteCount
	// straight onto StorageConnection.migrated_object_count/migrated_bytes
	// so the (separate) Dashboard polling UI has live numbers during a
	// long-running migration. Best-effort by design, same reasoning as
	// SendCheckpoints: a dropped progress report costs a stale counter, not
	// a wrong migration outcome.
	ReportStorageMigrationProgress(ctx context.Context, taskID int, objectCount int, byteCount int64) error

	// Close cleans up resources (Redis connections, etc.)
	Close() error
}
