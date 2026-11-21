package executor

// GA.ASW.5c — logical_backup/logical_restore unit tests. Reuses the package's
// existing seams: execCommandContext (see withFakeExec in stackrunner_test.go)
// stands in for `docker run` without a real daemon, and fakeS3/fakeS3Creds
// (native_staging_test.go) stand in for MinIO/versitygw over a real HTTP
// server so PutObject/GetObject exercise the real minio-go client.

import (
	"context"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// withFakeS3Env points minioClient() (legacy MINIO_* control-plane path) at a
// local fakeS3 httptest server for the duration of the test.
func withFakeS3Env(t *testing.T, bucket string) (*fakeS3, *httptest.Server) {
	t.Helper()
	f := newFakeS3(bucket)
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	creds := fakeS3Creds(srv)
	t.Setenv("STORAGE_BACKEND", "minio")
	t.Setenv("MINIO_INTERNAL_URL", "http://"+creds.Endpoint)
	t.Setenv("MINIO_ROOT_USER", creds.AccessKey)
	t.Setenv("MINIO_ROOT_PASSWORD", creds.SecretKey)
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "")
	return f, srv
}

// findFlagValue returns the value that follows the given flag in argv.
func findFlagValue(argv []string, flag string) string {
	for i, a := range argv {
		if a == flag && i+1 < len(argv) {
			return argv[i+1]
		}
	}
	return ""
}

// argvContains reports whether any argv element contains needle.
func argvContains(argv []string, needle string) bool {
	for _, a := range argv {
		if strings.Contains(a, needle) {
			return true
		}
	}
	return false
}

// hasBareEnvFlag reports whether argv contains the adjacent pair
// `-e`, k (the bare-name env form with no value).
func hasBareEnvFlag(argv []string, k string) bool {
	for i := 0; i+1 < len(argv); i++ {
		if argv[i] == "-e" && argv[i+1] == k {
			return true
		}
	}
	return false
}

// assertEnvViaCLIEEnv verifies the secret-channel invariants for one
// ContainerEnv key k: the docker argv carries the bare `-e k` form and NO
// argv element holds the secret value (raw or URL-encoded — the URL check
// guards the mongo password which is url.QueryEscape'd before env injection),
// and the value rides in the docker CLI process environment (cmd.Env).
func assertEnvViaCLIEnv(t *testing.T, cmd *exec.Cmd, argv []string, k, secret string) {
	t.Helper()
	if !hasBareEnvFlag(argv, k) {
		t.Fatalf("%s: bare `-e %s` flag missing from docker argv: %v", k, k, argv)
	}
	enc := url.QueryEscape(secret)
	for _, a := range argv {
		if strings.Contains(a, secret) || (enc != secret && strings.Contains(a, enc)) {
			t.Fatalf("%s: secret value (raw or URL-encoded) leaked into docker argv element %q (argv: %v)", k, a, argv)
		}
	}
	val := k + "=" + secret
	if enc != secret {
		val = k + "=" + enc
	}
	for _, e := range cmd.Env {
		if e == val {
			return
		}
	}
	t.Fatalf("%s: value not present in docker CLI env (cmd.Env): %v", k, cmd.Env)
}

func TestLogicalBackupPostgres(t *testing.T) {
	const password = "pg-super-secret"
	f, _ := withFakeS3Env(t, "stack-snapshots")

	var gotArgs []string
	var gotCmd *exec.Cmd
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		gotArgs = append([]string{name}, arg...)
		hostDir := strings.SplitN(findFlagValue(arg, "-v"), ":", 2)[0]
		// Stand in for the real container: write the dump the "client" would
		// have produced into the mounted host dir.
		gotCmd = exec.CommandContext(ctx, "sh", "-c",
			"printf 'pg-dump-bytes' > "+shellQuoteSingle(hostDir+"/dump.dump"))
		return gotCmd
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:        "logical_backup",
		StackEngine:    "postgres",
		StackImage:     "postgres:16",
		StackNetwork:   "modstack1g0_default",
		StackService:   "db",
		StackCommand:   []string{"pg_dump", "-Fc", "-h", "db", "-p", "5432", "-U", "postgres", "-d", "postgres", "-f", "/backup/dump.dump"},
		StackPath:      "/backup/dump.dump",
		SnapshotBucket: "stack-snapshots",
		SnapshotKey:    "modstack1g0/abc123/db.dump",
		ContainerEnv:   map[string]string{"PLATFORM_TASK": "1", "PGPASSWORD": password},
	}}

	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("logical backup failed: %v", err)
	}

	if got := findFlagValue(gotArgs, "--network"); got != "modstack1g0_default" {
		t.Fatalf("network = %q, want modstack1g0_default (argv: %v)", got, gotArgs)
	}
	// PGPASSWORD travels as the bare `-e NAME` flag; the VALUE rides in the
	// docker CLI process env (host ps never sees it).
	assertEnvViaCLIEnv(t, gotCmd, gotArgs, "PGPASSWORD", password)
	if gotArgs[len(gotArgs)-1] != "/backup/dump.dump" {
		t.Fatalf("command tail = %v, want dump path last", gotArgs)
	}
	if !strings.Contains(strings.Join(gotArgs, " "), "postgres:16") {
		t.Fatalf("image not passed: %v", gotArgs)
	}

	// Uploaded to the right bucket/key with the dump bytes.
	f.mu.Lock()
	body, ok := f.objs["modstack1g0/abc123/db.dump"]
	f.mu.Unlock()
	if !ok {
		t.Fatalf("object not uploaded to expected key; have: %v", f.objs)
	}
	if string(body) != "pg-dump-bytes" {
		t.Fatalf("uploaded body = %q", body)
	}

	if strings.Contains(res.LogTail, password) {
		t.Fatalf("LogTail leaked the password: %q", res.LogTail)
	}
}

func TestLogicalBackupMysql(t *testing.T) {
	const password = "mysql-backup-secret"
	f, _ := withFakeS3Env(t, "stack-snapshots")

	var gotArgs []string
	var gotCmd *exec.Cmd
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		gotArgs = append([]string{name}, arg...)
		hostDir := strings.SplitN(findFlagValue(arg, "-v"), ":", 2)[0]
		gotCmd = exec.CommandContext(ctx, "sh", "-c",
			"printf 'mysql-dump-bytes' > "+shellQuoteSingle(hostDir+"/dump.sql"))
		return gotCmd
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "logical_backup",
		StackEngine:  "mysql",
		StackImage:   "mysql:8.4",
		StackNetwork: "modstack5g0_default",
		StackService: "db",
		// mysqldump writes via its own -r <file> (no shell); the file is a
		// normal argv element here.
		StackCommand:   []string{"mysqldump", "--single-transaction", "--all-databases", "-h", "db", "-uroot", "-r", "/backup/dump.sql"},
		StackPath:      "/backup/dump.sql",
		SnapshotBucket: "stack-snapshots",
		SnapshotKey:    "modstack5g0/m1/db.sql",
		ContainerEnv:   map[string]string{"PLATFORM_TASK": "1", "MYSQL_PWD": password},
	}}

	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("logical backup (mysql) failed: %v", err)
	}
	// No secret value anywhere in argv; bare `-e MYSQL_PWD` present; value
	// in the docker CLI process env.
	assertEnvViaCLIEnv(t, gotCmd, gotArgs, "MYSQL_PWD", password)
	if !argvContains(gotArgs, "mysqldump") {
		t.Fatalf("mysqldump missing from docker argv: %v", gotArgs)
	}

	f.mu.Lock()
	body, ok := f.objs["modstack5g0/m1/db.sql"]
	f.mu.Unlock()
	if !ok {
		t.Fatalf("object not uploaded to expected key; have: %v", f.objs)
	}
	if string(body) != "mysql-dump-bytes" {
		t.Fatalf("uploaded body = %q", body)
	}
	if strings.Contains(res.LogTail, password) {
		t.Fatalf("LogTail leaked the password: %q", res.LogTail)
	}
}

func TestLogicalBackupMongoComposesURIFromEnvNotArgv(t *testing.T) {
	// A password with URI-special characters, to prove escaping matters.
	const password = "p@ss/word:99"
	_, _ = withFakeS3Env(t, "stack-snapshots")

	var gotArgs []string
	var gotCmd *exec.Cmd
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		gotArgs = append([]string{name}, arg...)
		hostDir := strings.SplitN(findFlagValue(arg, "-v"), ":", 2)[0]
		gotCmd = exec.CommandContext(ctx, "sh", "-c",
			"printf 'mongo-dump-bytes' > "+shellQuoteSingle(hostDir+"/dump.archive"))
		return gotCmd
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:        "logical_backup",
		StackEngine:    "mongo",
		StackImage:     "mongo:7.0",
		StackNetwork:   "modstack2g0_default",
		StackService:   "db",
		StackCommand:   []string{"mongodump", "--uri", "mongodb://root@db:27017/", "--archive", "-o", "/backup/dump.archive"},
		StackPath:      "/backup/dump.archive",
		SnapshotBucket: "stack-snapshots",
		SnapshotKey:    "modstack2g0/def456/db.archive",
		ContainerEnv:   map[string]string{"PLATFORM_TASK": "1", "MONGODB_PWD": password},
	}}

	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("logical backup (mongo) failed: %v", err)
	}

	full := strings.Join(gotArgs, " ")
	if strings.Contains(full, password) {
		t.Fatalf("raw mongo password leaked into docker argv: %v", gotArgs)
	}
	if !strings.Contains(full, "${MONGODB_PWD}") {
		t.Fatalf("expected a shell ${MONGODB_PWD} placeholder in the composed uri: %v", gotArgs)
	}
	if gotArgs[len(gotArgs)-3] != "sh" || gotArgs[len(gotArgs)-2] != "-c" {
		t.Fatalf("expected the mongo command to run via sh -c: %v", gotArgs)
	}
	// MONGODB_PWD travels as the bare `-e NAME` flag (the sh -c script
	// expands ${MONGODB_PWD} from the container env at exec time); the
	// URL-escaped VALUE rides in the docker CLI process env.
	assertEnvViaCLIEnv(t, gotCmd, gotArgs, "MONGODB_PWD", password)
}

func TestLogicalRestoreMysqlFeedsStdin(t *testing.T) {
	const password = "mysql-secret"
	f, _ := withFakeS3Env(t, "stack-snapshots")
	f.objs["modstack3g0/xyz789/db.sql"] = []byte("-- dump sql --")

	// Outside the mounted dir (which logicalRestore removes before returning)
	// so the captured stdin survives past the call for the assertion below.
	captureFile := t.TempDir() + "/stdin.captured"

	var gotArgs []string
	var sawStdinFlag bool
	var gotCmd *exec.Cmd
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		gotArgs = append([]string{name}, arg...)
		for _, a := range arg {
			if a == "-i" {
				sawStdinFlag = true
			}
		}
		gotCmd = exec.CommandContext(ctx, "sh", "-c", "cat > "+shellQuoteSingle(captureFile))
		return gotCmd
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "logical_restore",
		StackEngine:  "mysql",
		StackImage:   "mysql:8.4",
		StackNetwork: "modstack3g0_default",
		StackService: "db",
		// mysql's restore argv has no trailing file arg (see stacklogical.go).
		StackCommand:   []string{"mysql", "-h", "db", "-uroot"},
		StackPath:      "/backup/dump.sql",
		SnapshotBucket: "stack-snapshots",
		SnapshotKey:    "modstack3g0/xyz789/db.sql",
		ContainerEnv:   map[string]string{"PLATFORM_TASK": "1", "MYSQL_PWD": password},
	}}

	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("logical restore (mysql) failed: %v", err)
	}
	if !sawStdinFlag {
		t.Fatalf("expected docker run -i (stdin attach) for mysql restore: %v", gotArgs)
	}
	// MYSQL_PWD travels as the bare `-e NAME` flag; the VALUE rides in the
	// docker CLI process env (host ps never sees it).
	assertEnvViaCLIEnv(t, gotCmd, gotArgs, "MYSQL_PWD", password)
	if gotArgs[len(gotArgs)-1] != "-uroot" {
		t.Fatalf("mysql restore argv should end without a file arg: %v", gotArgs)
	}
	captured, err := os.ReadFile(captureFile)
	if err != nil {
		t.Fatalf("read captured stdin: %v", err)
	}
	if string(captured) != "-- dump sql --" {
		t.Fatalf("stdin fed to mysql = %q, want the downloaded dump body", captured)
	}
}

func TestLogicalRestoreErrorNeverLeaksPassword(t *testing.T) {
	const password = "should-never-appear"
	f, _ := withFakeS3Env(t, "stack-snapshots")
	f.objs["modstack4g0/k/db.dump"] = []byte("dump")

	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		// Simulate a failing client that even echoes its own env into stderr
		// (the exact failure mode the design has to survive without this Go
		// code itself having formatted the secret anywhere).
		return exec.CommandContext(ctx, "sh", "-c", "echo 'restore failed: connection refused' >&2; exit 3")
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:        "logical_restore",
		StackEngine:    "postgres",
		StackImage:     "postgres:16",
		StackNetwork:   "modstack4g0_default",
		StackService:   "db",
		StackCommand:   []string{"pg_restore", "--clean", "--if-exists", "-h", "db", "-p", "5432", "-U", "postgres", "-d", "postgres", "/backup/dump.dump"},
		StackPath:      "/backup/dump.dump",
		SnapshotBucket: "stack-snapshots",
		SnapshotKey:    "modstack4g0/k/db.dump",
		ContainerEnv:   map[string]string{"PLATFORM_TASK": "1", "PGPASSWORD": password},
	}}

	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatal("expected the simulated restore failure to surface as an error")
	}
	if strings.Contains(err.Error(), password) {
		t.Fatalf("error leaked the password: %v", err)
	}
}
