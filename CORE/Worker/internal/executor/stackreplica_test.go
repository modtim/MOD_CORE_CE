package executor

// GA.ASW.10.6 — db_seed_replica/db_replica_status/db_replica_promote/
// db_source_stop/db_source_start unit tests. Reuses the package's existing
// seams: execCommandContext (withFakeExec, stackrunner_test.go) stands in for
// `docker run`/`docker compose` without a real daemon.

import (
	"context"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// TestDbSeedReplicaMountsTargetVolume proves db_seed_replica (ephemeral
// client on the source network, per build_seed_spec) runs pg_basebackup with
// the TARGET volume bind-mounted at the seed path's directory, and that the
// PGPASSWORD secret rides cmd.Env, never the docker argv.
func TestDbSeedReplicaMountsTargetVolume(t *testing.T) {
	const password = "seed-super-secret"

	var gotArgs []string
	var gotCmd *exec.Cmd
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		gotArgs = append([]string{name}, arg...)
		gotCmd = exec.CommandContext(ctx, "sh", "-c", "printf 'basebackup ok'")
		return gotCmd
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "db_seed_replica",
		StackMode:    "ephemeral_client",
		StackEngine:  "postgres",
		StackImage:   "postgres:16",
		StackNetwork: "modstack1g0_default",
		StackService: "db",
		StackCommand: []string{"pg_basebackup", "-h", "db", "-p", "5432", "-U", "postgres",
			"-D", "/var/lib/postgresql/data", "--wal-method=stream", "-c", "fast", "-P", "-R"},
		StackPath:    "/var/lib/postgresql/data",
		StackVolume:  "modstack2g0_pgdata",
		ContainerEnv: map[string]string{"PLATFORM_TASK": "1", "PGPASSWORD": password},
	}}

	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("db_seed_replica failed: %v", err)
	}

	if got := findFlagValue(gotArgs, "--network"); got != "modstack1g0_default" {
		t.Fatalf("network = %q, want modstack1g0_default (argv: %v)", got, gotArgs)
	}
	if got := findFlagValue(gotArgs, "-v"); got != "modstack2g0_pgdata:/var/lib/postgresql" {
		t.Fatalf("volume mount = %q, want modstack2g0_pgdata:/var/lib/postgresql (argv: %v)", got, gotArgs)
	}
	if !argvContains(gotArgs, "pg_basebackup") {
		t.Fatalf("pg_basebackup missing from docker argv: %v", gotArgs)
	}
	assertEnvViaCLIEnv(t, gotCmd, gotArgs, "PGPASSWORD", password)
	if strings.Contains(res.LogTail, password) {
		t.Fatalf("LogTail leaked the password: %q", res.LogTail)
	}
}

// TestDbReplicaStatusReturnsStdoutInResult proves db_replica_status
// (ephemeral client, per build_replica_status_spec) has no volume mount and
// puts the psql query's raw stdout into Result.LogTail, exactly as the API
// needs it to parse pg_is_in_recovery()/LSN lag.
func TestDbReplicaStatusReturnsStdoutInResult(t *testing.T) {
	const password = "status-super-secret"
	const statusLine = "t|0/3000060|0/3000060"

	var gotArgs []string
	var gotCmd *exec.Cmd
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		gotArgs = append([]string{name}, arg...)
		gotCmd = exec.CommandContext(ctx, "sh", "-c", "printf '"+statusLine+"\\n'")
		return gotCmd
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "db_replica_status",
		StackMode:    "ephemeral_client",
		StackEngine:  "postgres",
		StackImage:   "postgres:16",
		StackNetwork: "modstack2g0_default",
		StackService: "db",
		StackCommand: []string{"psql", "-h", "db", "-p", "5432", "-U", "postgres", "-d", "postgres",
			"-t", "-A", "-c", "SELECT pg_is_in_recovery(), pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn();"},
		ContainerEnv: map[string]string{"PLATFORM_TASK": "1", "PGPASSWORD": password},
	}}

	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("db_replica_status failed: %v", err)
	}
	if got := findFlagValue(gotArgs, "-v"); got != "" {
		t.Fatalf("db_replica_status should carry no volume mount (argv: %v)", gotArgs)
	}
	if res.LogTail != statusLine {
		t.Fatalf("LogTail = %q, want the raw psql stdout %q so the API can parse lag", res.LogTail, statusLine)
	}
	assertEnvViaCLIEnv(t, gotCmd, gotArgs, "PGPASSWORD", password)
}

// TestDbReplicaPromoteExecsInService proves db_replica_promote
// (exec_in_service, per build_promote_spec) runs `docker compose exec` inside
// the target's own service container — no client image, no network attach,
// no password.
func TestDbReplicaPromoteExecsInService(t *testing.T) {
	var gotArgs []string
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		gotArgs = append([]string{name}, arg...)
		return exec.CommandContext(ctx, "sh", "-c", "printf 'server promoted'")
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "db_replica_promote",
		StackMode:    "exec_in_service",
		StackEngine:  "postgres",
		StackProject: "modstack2g0",
		StackService: "db",
		StackCommand: []string{"pg_ctl", "promote", "-D", "/var/lib/postgresql/data"},
	}}

	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("db_replica_promote failed: %v", err)
	}
	wantArgs := []string{"docker", "compose", "-p", "modstack2g0", "exec", "-T", "db",
		"pg_ctl", "promote", "-D", "/var/lib/postgresql/data"}
	if strings.Join(gotArgs, " ") != strings.Join(wantArgs, " ") {
		t.Fatalf("argv mismatch:\n got: %v\nwant: %v", gotArgs, wantArgs)
	}
	if res.LogTail != "server promoted" {
		t.Fatalf("LogTail = %q, want the exec's stdout", res.LogTail)
	}
}

// TestDbSourceStopAndStartExecInService covers the other two exec_in_service
// ops (build_source_stop_spec/build_source_start_spec) — same shape as
// promote, different service (the SOURCE, not the target).
func TestDbSourceStopAndStartExecInService(t *testing.T) {
	cases := []struct {
		op      string
		command []string
	}{
		{"db_source_stop", []string{"pg_ctl", "stop", "-D", "/var/lib/postgresql/data", "-m", "fast"}},
		{"db_source_start", []string{"pg_ctl", "start", "-D", "/var/lib/postgresql/data"}},
	}
	for _, tc := range cases {
		t.Run(tc.op, func(t *testing.T) {
			var gotArgs []string
			restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
				gotArgs = append([]string{name}, arg...)
				return exec.CommandContext(ctx, "true")
			})
			defer restore()

			e := &Executor{}
			task := &transport.Task{Payload: transport.TaskPayload{
				StackOp:      tc.op,
				StackMode:    "exec_in_service",
				StackEngine:  "postgres",
				StackProject: "modstack1g0",
				StackService: "db",
				StackCommand: tc.command,
			}}
			_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
			if err != nil {
				t.Fatalf("%s failed: %v", tc.op, err)
			}
			wantArgs := append([]string{"docker", "compose", "-p", "modstack1g0", "exec", "-T", "db"}, tc.command...)
			if strings.Join(gotArgs, " ") != strings.Join(wantArgs, " ") {
				t.Fatalf("argv mismatch:\n got: %v\nwant: %v", gotArgs, wantArgs)
			}
		})
	}
}

// TestReplicaOpUnknownModeRejected proves an unrecognized stack_mode is
// rejected before any docker/compose command is invoked — future streaming-
// migration ops must opt into one of the two known shapes explicitly, never
// fall through silently.
func TestReplicaOpUnknownModeRejected(t *testing.T) {
	called := false
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		called = true
		return exec.CommandContext(ctx, "true")
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "db_replica_promote",
		StackMode:    "some_future_mode",
		StackProject: "modstack1g0",
		StackService: "db",
		StackCommand: []string{"pg_ctl", "promote"},
	}}
	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatal("expected an error for an unknown stack_mode")
	}
	if !strings.Contains(err.Error(), "unknown stack_mode") {
		t.Fatalf("error %q does not mention the unknown mode", err.Error())
	}
	if called {
		t.Fatal("docker/compose should never be invoked for an unknown stack_mode")
	}
}

// TestReplicaEphemeralSecretNeverInArgv is an explicit, per-op sweep proving
// that for every ephemeral_client op, a secret placed in ContainerEnv never
// appears anywhere in the docker run argv — the ASW.5d "-e NAME, never -e
// NAME=value" contract stacklogical.go established, exercised here for the
// streaming-migration ops specifically.
func TestReplicaEphemeralSecretNeverInArgv(t *testing.T) {
	const secret = "streaming-migration-secret-xyz"
	ops := []struct {
		op      string
		command []string
		path    string
		volume  string
	}{
		{"db_seed_replica", []string{"pg_basebackup", "-h", "db", "-D", "/pgdata", "-R"}, "/pgdata", "modstack_pgdata"},
		{"db_replica_status", []string{"psql", "-h", "db", "-c", "select 1"}, "", ""},
	}
	for _, tc := range ops {
		t.Run(tc.op, func(t *testing.T) {
			var gotArgs []string
			restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
				gotArgs = append([]string{name}, arg...)
				return exec.CommandContext(ctx, "true")
			})
			defer restore()

			e := &Executor{}
			task := &transport.Task{Payload: transport.TaskPayload{
				StackOp:      tc.op,
				StackMode:    "ephemeral_client",
				StackEngine:  "postgres",
				StackImage:   "postgres:16",
				StackNetwork: "modstack_default",
				StackService: "db",
				StackCommand: tc.command,
				StackPath:    tc.path,
				StackVolume:  tc.volume,
				ContainerEnv: map[string]string{"PLATFORM_TASK": "1", "PGPASSWORD": secret},
			}}
			_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
			if err != nil {
				t.Fatalf("%s failed: %v", tc.op, err)
			}
			for _, a := range gotArgs {
				if strings.Contains(a, secret) {
					t.Fatalf("%s: secret leaked into docker argv element %q (argv: %v)", tc.op, a, gotArgs)
				}
			}
		})
	}
}
