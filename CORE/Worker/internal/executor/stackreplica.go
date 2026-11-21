package executor

// GA.ASW.10.6 — streaming zero-downtime DB migration for a persistent stack:
// the worker-side executor for the five ops services/stack_streaming_migration.py
// builds specs for (db_seed_replica, db_replica_status, db_replica_promote,
// db_source_stop, db_source_start). Spec is built server-side, exactly like
// stacklogical.go's logical_backup/logical_restore — this file only executes
// it, engine-agnostically: it runs whatever argv (StackCommand) the API's
// per-engine builder produced, and never branches on StackEngine itself
// (informational/log-only, same contract as stacklogical.go).
//
// Every op picks one of two execution shapes via StackMode (transport.go):
//
//   - "ephemeral_client" (db_seed_replica, db_replica_status) — a `docker run`
//     client container attached to StackNetwork, same shape as
//     stacklogical.go's dump/restore. db_seed_replica additionally bind-mounts
//     StackVolume (the TARGET stack's own data volume) at
//     filepath.Dir(StackPath) so `pg_basebackup -D <path>` seeds the volume
//     the target's postgres container will actually start on, not a throwaway
//     temp dir — db_replica_status carries no StackVolume and needs none, it
//     only runs a read-only status query.
//
//   - "exec_in_service" (db_replica_promote, db_source_stop, db_source_start)
//     — `docker compose exec` StackCommand INSIDE the already-running
//     StackService container of StackProject, the same primitive
//     runStackOp's "mesh_reup" case already uses for a service-local
//     operation. No client image, no network attach, no password: these
//     commands (`pg_ctl promote|stop|start`) talk to the local postmaster on
//     its own data directory, not over SQL auth.
//
// Secrets, same contract as stacklogical.go: StackCommand never carries a
// password. For "ephemeral_client" ops, ContainerEnv's password value rides
// the docker CLI process's own environment (cmd.Env) via the bare `-e NAME`
// argv form (logicalContainerEnvFlagsAndValues, reused as-is) — never a
// formatted `-e NAME=value`. "exec_in_service" ops carry no password at all
// (the API's builders set password_env=None for them), so no ContainerEnv
// handling applies there.
//
// db_replica_status's whole purpose is reporting lag back to the API, so its
// stdout — not just a summary line — is what runReplicaOp returns; runStackOp
// puts it straight into Result.LogTail (tail()-capped, same as every other
// stack op) for the API to parse (pg_is_in_recovery()/receive-LSN/replay-LSN,
// `-t -A` single-line psql output, comfortably under the 4KB cap).
import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// The two StackMode values this file understands; anything else is rejected
// by runReplicaOp rather than silently falling through to one shape or the
// other.
const (
	replicaModeEphemeralClient = "ephemeral_client"
	replicaModeExecInService   = "exec_in_service"
)

// runReplicaOp dispatches one of the five streaming-migration ops to its
// execution shape by p.StackMode, and returns the invoked command's stdout
// (trimmed) for the caller to fold into Result.LogTail.
func (e *Executor) runReplicaOp(ctx context.Context, cb Callbacks, p transport.TaskPayload) (string, error) {
	switch p.StackMode {
	case replicaModeEphemeralClient:
		return e.replicaEphemeral(ctx, p)
	case replicaModeExecInService:
		return e.replicaExecInService(ctx, cb, p)
	default:
		return "", fmt.Errorf("stack op %q: unknown stack_mode %q (want %q or %q)",
			p.StackOp, p.StackMode, replicaModeEphemeralClient, replicaModeExecInService)
	}
}

// replicaEphemeral runs StackImage as an ephemeral `docker run --rm` client
// on StackNetwork with StackCommand, mirroring logicalBackup's container-run
// mechanics (stacklogical.go) but returning stdout instead of uploading a
// dump file to MinIO — there is no dump artifact here, only a command's
// output (db_replica_status) or a side effect on a mounted volume
// (db_seed_replica).
func (e *Executor) replicaEphemeral(ctx context.Context, p transport.TaskPayload) (string, error) {
	if p.StackImage == "" || p.StackNetwork == "" || len(p.StackCommand) == 0 {
		return "", fmt.Errorf("%s missing stack_image/stack_network/stack_command", p.StackOp)
	}

	args := []string{"run", "--rm", "--network", p.StackNetwork}
	// db_seed_replica only: mount the TARGET stack's own data volume at the
	// path the command writes into, so the seed lands in real, persistent
	// storage instead of the ephemeral container's own filesystem.
	// db_replica_status sets neither StackVolume nor StackPath — this block
	// is a no-op for it.
	if p.StackVolume != "" && p.StackPath != "" {
		args = append(args, "-v", p.StackVolume+":"+filepath.Dir(p.StackPath))
	}
	envFlags, envValues := logicalContainerEnvFlagsAndValues(p.StackEngine, p.ContainerEnv)
	args = append(args, envFlags...)
	args = append(args, p.StackImage)
	args = append(args, p.StackCommand...)

	var stdout, stderr bytes.Buffer
	cmd := execCommandContext(ctx, "docker", args...)
	// os.Environ() returns a fresh copy each call, so appending is safe.
	cmd.Env = append(os.Environ(), envValues...)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("%s (%s) failed: %v: %s", p.StackOp, p.StackEngine, err, stderr.String())
	}
	return strings.TrimSpace(stdout.String()), nil
}

// replicaExecInService execs StackCommand inside the already-running
// StackService container of StackProject via `docker compose exec -T`, the
// same primitive the "mesh_reup" op uses (stackrunner.go) for a service-local
// operation with no ephemeral client and no network attach.
func (e *Executor) replicaExecInService(ctx context.Context, cb Callbacks, p transport.TaskPayload) (string, error) {
	project := strings.TrimSpace(p.StackProject)
	if project == "" {
		return "", fmt.Errorf("%s missing stack_project", p.StackOp)
	}
	if strings.TrimSpace(p.StackService) == "" {
		return "", fmt.Errorf("%s missing stack_service", p.StackOp)
	}
	if len(p.StackCommand) == 0 {
		return "", fmt.Errorf("%s missing stack_command", p.StackOp)
	}

	args := append([]string{"exec", "-T", p.StackService}, p.StackCommand...)
	out, err := e.composeCmd(ctx, cb, project, "", nil, args...)
	if err != nil {
		return "", fmt.Errorf("%s failed: %w\n%s", p.StackOp, err, out)
	}
	return strings.TrimSpace(out), nil
}
