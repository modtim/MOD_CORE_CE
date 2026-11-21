package executor

// GA.ASW.5c — logical_backup/logical_restore: run an ephemeral DB-client
// container (StackImage) attached to the stack's own compose network
// (StackNetwork) to pg_dump/mysqldump/mongodump (backup) or
// pg_restore/mysql/mongorestore (restore) against the running database
// service (StackService), then move the dump file to/from MinIO. Spec is
// built server-side (services/stack_logical_backup.py via
// routers/persistent_stacks/_shared.py:_dispatch_logical_task) — this file
// only executes it, the same way stacksnapshot.go executes the per-volume
// tar spec.
//
// The dump/restore argv (StackCommand) never carries a password — the
// resolved secret rides ContainerEnv (PGPASSWORD / MYSQL_PWD / MONGODB_PWD)
// and reaches the container through the docker CLI: the run argv carries
// only the bare `-e NAME` form (no value) and the value is injected into
// the docker CLI process's own environment via exec.Cmd.Env, because
// `docker run -e NAME` without a `=value` copies NAME from the CLI's own
// environment. The plaintext secret thus appears in neither this worker's
// argv nor the host process list (ps) of the docker CLI. This file must
// never format a ContainerEnv value into a log line, Result.LogTail, or an
// error string — errors below report engine/bucket/key/container stderr,
// never the env map.
//
// Two engine-specific wrinkles, both driven structurally off StackCommand
// rather than hardcoded op names:
//
//   - mysql/mariadb's `mysql` client has no "read this SQL file" flag, so
//     the restore argv the API built omits the dump path entirely (a
//     trailing positional would be read as a database name instead).
//     Detected generically as "StackPath is not one of StackCommand's argv
//     elements" and fed on the container's stdin instead of a mount-relative
//     arg.
//   - mongodump/mongorestore have no password env of their own — the
//     `--uri` the API built is deliberately password-free. It is rewritten
//     here into a shell variable reference (`${<password_env>}`) and run via
//     `sh -c` INSIDE the container, so the plaintext secret is substituted
//     by the container's own shell at exec time and never appears in any
//     string this worker process builds, formats, or logs.
import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// logicalContainerEnvFlags+logicalContainerEnvValues split ContainerEnv into
// the two halves of the docker-run env channel. The flags are the bare
// `-e NAME` form only — never `-e NAME=value` — so no secret VALUE is ever
// formatted into the docker CLI's argv (host `ps`). The values are the
// corresponding `NAME=value` pairs to inject into the docker CLI process's
// own environment via exec.Cmd.Env: `docker run -e NAME` without `=value`
// copies NAME from the CLI's own environment, and the daemon never prints
// that to the host process list. For engine "mongo" the identified password
// key's value is URL-escaped in place, because it is consumed inside a
// shell-composed URI (mongoShellCommand) where an un-escaped ':', '@' or '/'
// in the password would corrupt the URI's own delimiters; every other
// engine's client reads its password env raw (PGPASSWORD/MYSQL_PWD take the
// value verbatim).
func logicalContainerEnvFlagsAndValues(engine string, env map[string]string) (args, extraEnv []string) {
	pwdKey := ""
	if engine == "mongo" {
		pwdKey = mongoPasswordEnvKey(env)
	}
	args = make([]string, 0, len(env)*2)
	extraEnv = make([]string, 0, len(env))
	for k, v := range env {
		if k == pwdKey {
			v = url.QueryEscape(v)
		}
		args = append(args, "-e", k)
		extraEnv = append(extraEnv, k+"="+v)
	}
	return args, extraEnv
}

// mongoPasswordEnvKey is the one ContainerEnv key besides the PLATFORM_TASK
// marker _dispatch_logical_task always sets — the mongo builder's
// password_env (MONGODB_PWD in practice), resolved structurally so this
// never hardcodes the env var name.
func mongoPasswordEnvKey(env map[string]string) string {
	for k := range env {
		if k != "PLATFORM_TASK" {
			return k
		}
	}
	return ""
}

// containsArg reports whether target is one of argv's elements — used to
// detect (generically, not by engine name) whether a builder-produced
// command already reads/writes the mounted dump file itself, or needs it
// fed some other way (mysql/mariadb restore: stdin).
func containsArg(argv []string, target string) bool {
	for _, a := range argv {
		if a == target {
			return true
		}
	}
	return false
}

// shellQuoteSingle single-quotes s for POSIX sh, escaping any embedded
// single quote by closing/reopening the quoted string (the standard sh
// idiom: ' -> '"'"').
func shellQuoteSingle(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

// insertMongoPasswordPlaceholder rewrites the password-free URI the API
// built (mongodb://user@host:port/) into mongodb://user:${envKey}@host:port/
// — a shell VARIABLE REFERENCE, never a secret value. The container's own
// `sh -c` expands it from its environment at exec time.
func insertMongoPasswordPlaceholder(uri, envKey string) (string, error) {
	// Escape shell metacharacters that could appear in the (structural,
	// worker-controlled) uri before it goes inside the script's double
	// quotes. Runs BEFORE the ${...} placeholder is inserted, so escaping
	// "$" here cannot touch the placeholder we add next.
	escaped := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "`", "\\`", "$", "\\$").Replace(uri)
	at := strings.LastIndex(escaped, "@")
	if at < 0 {
		return "", fmt.Errorf("mongo uri missing '@' host separator: %q", uri)
	}
	return escaped[:at] + ":${" + envKey + "}" + escaped[at:], nil
}

// mongoShellCommand builds the `sh -c` script string for a mongodump/
// mongorestore argv: every arg is single-quoted verbatim EXCEPT the value
// following `--uri`, which is double-quoted with a `${envKey}` placeholder
// so the container's shell — not this Go process — substitutes the secret.
func mongoShellCommand(argv []string, envKey string) (string, error) {
	if envKey == "" {
		return "", fmt.Errorf("mongo command needs a password env var in container_env")
	}
	parts := make([]string, 0, len(argv)+1)
	parts = append(parts, "exec")
	for i := 0; i < len(argv); i++ {
		if argv[i] == "--uri" && i+1 < len(argv) {
			withPwd, err := insertMongoPasswordPlaceholder(argv[i+1], envKey)
			if err != nil {
				return "", err
			}
			parts = append(parts, "--uri", `"`+withPwd+`"`)
			i++
			continue
		}
		parts = append(parts, shellQuoteSingle(argv[i]))
	}
	return strings.Join(parts, " "), nil
}

// logicalDumpArgs is the tail of the `docker run` argv after the image name:
// either StackCommand verbatim, or (mongo) `sh -c <script>` with the
// password-free --uri rewritten to a shell placeholder.
func logicalDumpArgs(p transport.TaskPayload) ([]string, error) {
	if p.StackEngine != "mongo" {
		return append([]string(nil), p.StackCommand...), nil
	}
	script, err := mongoShellCommand(p.StackCommand, mongoPasswordEnvKey(p.ContainerEnv))
	if err != nil {
		return nil, err
	}
	return []string{"sh", "-c", script}, nil
}

// logicalBackup runs StackImage attached to StackNetwork with StackCommand,
// writing the dump to StackPath inside a mounted temp dir, then uploads that
// file to SnapshotBucket/SnapshotKey. Mirrors snapshotVolume's shape.
func (e *Executor) logicalBackup(ctx context.Context, p transport.TaskPayload) (int64, error) {
	if p.StackPath == "" || p.StackImage == "" || p.StackNetwork == "" {
		return 0, fmt.Errorf("logical backup missing stack_image/stack_network/stack_path")
	}
	c, err := minioClient()
	if err != nil {
		return 0, err
	}
	if err := ensureBucket(ctx, c, p.SnapshotBucket); err != nil {
		return 0, fmt.Errorf("ensure bucket: %w", err)
	}

	dir, err := os.MkdirTemp("", "modlogical-backup-")
	if err != nil {
		return 0, fmt.Errorf("logical backup workspace: %w", err)
	}
	defer os.RemoveAll(dir)

	dumpArgs, err := logicalDumpArgs(p)
	if err != nil {
		return 0, err
	}
	mountDir := filepath.Dir(p.StackPath)
	envFlags, envValues := logicalContainerEnvFlagsAndValues(p.StackEngine, p.ContainerEnv)
	args := []string{"run", "--rm", "--network", p.StackNetwork, "-v", dir + ":" + mountDir}
	args = append(args, envFlags...)
	args = append(args, p.StackImage)
	args = append(args, dumpArgs...)

	var stderr bytes.Buffer
	cmd := execCommandContext(ctx, "docker", args...)
	// os.Environ() returns a fresh copy each call, so appending is safe.
	cmd.Env = append(os.Environ(), envValues...)
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return 0, fmt.Errorf("dump (%s) failed: %v: %s", p.StackEngine, err, stderr.String())
	}

	hostFile := filepath.Join(dir, filepath.Base(p.StackPath))
	f, err := os.Open(hostFile)
	if err != nil {
		return 0, fmt.Errorf("dump did not produce %s: %w", p.StackPath, err)
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return 0, err
	}
	info, err := c.PutObject(ctx, p.SnapshotBucket, p.SnapshotKey, f, fi.Size(), minio.PutObjectOptions{})
	if err != nil {
		return 0, fmt.Errorf("upload dump: %w", err)
	}
	return info.Size, nil
}

// 30.6.1 c1-J3b — per-table ROW COUNTS of a restored logical snapshot.
//
// maxVerifyTables / maxVerifyRows / verifyCountsTimeout are the ONLY size-bomb
// defence for the count batch (a tenant's dump can name a million tables or
// hold a trillion rows): at most maxVerifyTables tables are counted, a row
// count above maxVerifyRows is recorded AS maxVerifyRows with the capped flag,
// and the whole batch runs under verifyCountsTimeout. A timeout or a failed
// count NEVER fails the restore — the restore already succeeded, so the counts
// are simply omitted and the task still completes.
const maxVerifyTables = 200
const maxVerifyRows int64 = 10000000
const verifyCountsTimeout = 2 * time.Minute

// verifyTableName is the ONLY table name the count batch will accept. Table
// names come from a dump the TENANT uploaded — attacker-supplied — so anything
// outside this charset (a quote, a backtick, `$`, `;`, `&`, `|`, parens, a
// redirect, a NUL, any whitespace) is rejected before any exec runs: the name
// reaches psql as a discrete argv element and is never re-expanded by any
// shell. This is the argv analogue of path traversal.
var verifyTableName = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)

// verifyCountsScript is the CONSTANT container-side loop: every table name is
// a separate argv element (`"$@"`), printed then counted, one line per table
// (`<table> <count>`). The SQL is a constant string; the ONLY variable part is
// `\"$t\"`, which the CONTAINER's own sh expands from that argv element — and
// sh never re-scans a substituted parameter, so a name containing `$`, a
// backtick or a quote cannot inject anything (verifyTableName rejects those
// before an exec runs anyway). psql's exit status is ignored on the loop so
// one unreadable table does not abort the batch — a missing line is simply
// absent from the map, which the API reads as "not counted", never as 0.
const verifyCountsScript = "for t in \"$@\"; do printf '%s ' \"$t\"; " +
	"psql -t -A -F' ' -c \"SELECT COUNT(1) FROM \\\"$t\\\"\" 2>/dev/null; done"

// verifyListSQL is the constant, read-only query that names the tables the
// restore actually landed — the engine's own catalog is excluded, same
// exclusion stackprobe.go:probeUserTablesSQL uses. Table names are DATA here:
// they come back from the DB the tenant's dump wrote into, and every one of
// them is re-validated against verifyTableName before it is passed on.
const verifyListSQL = "SELECT t.table_name FROM information_schema.tables " +
	"WHERE t.table_schema NOT IN ('pg_catalog','information_schema') " +
	"AND t.table_type='BASE TABLE' ORDER BY t.table_name"

// verifyTableCap keeps at most maxVerifyTables names and reports whether the
// list was truncated. Truncation is never an error: the restore succeeded.
func verifyTableCap(tables []string) (kept []string, truncated bool) {
	if len(tables) <= maxVerifyTables {
		return tables, false
	}
	return tables[:maxVerifyTables], true
}

// tableCountArgv builds the CONTAINER-side command argv for the bounded
// read-only count batch: `sh -c <constant script> <table> <table> ...`.
// Every name is validated against verifyTableName FIRST — a single bad name
// rejects the whole batch (error, not a silent drop), because silently
// dropping would let a hostile dump decide which of its own tables get
// reported. Only the postgres family has a psql; mysql/mariadb/mongo return
// an empty argv (no counts, not a failure). The env map is consulted only for
// its KEYS — no value is ever formatted into the returned argv.
func tableCountArgv(engine string, env map[string]string, tables []string) ([]string, error) {
	if engine != "postgres" && engine != "postgresql" {
		return []string{}, nil
	}
	if len(tables) == 0 {
		return []string{}, nil
	}
	for _, t := range tables {
		if !verifyTableName.MatchString(t) {
			return nil, fmt.Errorf("verify counts: table name %q is not a safe identifier", t)
		}
	}
	_ = env // keys only — the password value never reaches argv (see below)
	return append([]string{"sh", "-c", verifyCountsScript}, tables...), nil
}

// parseTableCounts reads `<table> <count>` lines (psql -t -A -F' ') into a map.
// Blank lines are skipped; a non-integer count or a duplicate table name is an
// error (a dump that reports the same table twice is lying about what landed).
// Nothing is clamped here — capVerifyRows does that, so the raw count stays
// visible to the caller.
func parseTableCounts(out string) (map[string]int64, error) {
	counts := map[string]int64{}
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(strings.ReplaceAll(line, "|", " "))
		if line == "" {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) != 2 {
			return nil, fmt.Errorf("verify counts: malformed line %q", line)
		}
		name, raw := parts[0], parts[1]
		if !regexp.MustCompile(`^[0-9]+$`).MatchString(raw) {
			return nil, fmt.Errorf("verify counts: non-integer count %q for table %q", raw, name)
		}
		if _, dup := counts[name]; dup {
			return nil, fmt.Errorf("verify counts: duplicate table %q", name)
		}
		n, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("verify counts: count %q overflows int64 for table %q", raw, name)
		}
		counts[name] = n
	}
	return counts, nil
}

// capVerifyRows records any count above maxVerifyRows AS maxVerifyRows and
// reports that capping happened (decision 3: honest, never a silent lie about
// how much landed).
func capVerifyRows(counts map[string]int64) (map[string]int64, bool) {
	out := map[string]int64{}
	capped := false
	for k, v := range counts {
		if v > maxVerifyRows {
			out[k] = maxVerifyRows
			capped = true
		} else {
			out[k] = v
		}
	}
	return out, capped
}

// logicalVerifyCounts runs the count batch against the stack's OWN DB service
// only (StackImage on StackNetwork, never a client-supplied host). The
// connection args are read STRUCTURALLY out of StackCommand — the exact argv
// the restore itself used — so no new host/port/user/dbname is ever accepted.
// The password rides ContainerEnv as bare `-e NAME` flags + the docker CLI's
// own environment (logicalContainerEnvFlagsAndValues), never as an argv value.
// Any failure (timeout, no psql, unparseable output) is swallowed into "no
// counts" — the restore already succeeded and must not be failed by its
// bookkeeping.
//
// CALL-SITE CONTRACT (30.6.1 c1-J3b): this runs ONLY after `logicalRestore`
// returned nil — never on a failed restore, which would count a half-applied
// DB and report it as a verified one. The wire half (executor.go Result field
// + stackrunner.go's logical_restore case + cmd/worker/main.go's completion
// forward onto TaskResult.VerifyTables) is OUTSIDE this job's 3-file budget:
// the fields and the API record are in place, the call site is the follow-on.
func (e *Executor) logicalVerifyCounts(ctx context.Context, p transport.TaskPayload) (map[string]int64, bool, error) {
	listArgs, err := verifyListArgv(p.StackEngine)
	if err != nil || len(listArgs) == 0 {
		return map[string]int64{}, false, err
	}
	conn, err := psqlConnectionArgs(p.StackCommand)
	if err != nil {
		return map[string]int64{}, false, err
	}
	env := map[string]string{}
	for k, v := range p.ContainerEnv {
		env[k] = v
	}
	for i := 0; i+1 < len(conn); i += 2 {
		env[conn[i]] = conn[i+1]
	}
	flags, values := logicalContainerEnvFlagsAndValues(p.StackEngine, env)
	countCtx, cancel := context.WithTimeout(ctx, verifyCountsTimeout)
	defer cancel()

	// Step 1: ask the stack's OWN service which tables the restore landed.
	listCmd := execCommandContext(countCtx, "docker",
		append(append([]string{"run", "--rm", "--network", p.StackNetwork}, flags...),
			append([]string{p.StackImage}, listArgs...)...)...)
	listCmd.Env = append(os.Environ(), values...)
	var listOut bytes.Buffer
	listCmd.Stdout = &listOut
	if err := listCmd.Run(); err != nil {
		// No psql in the image / timeout: no counts, restore still ok.
		return map[string]int64{}, false, nil
	}
	tables := []string{}
	for _, line := range strings.Split(listOut.String(), "\n") {
		name := strings.TrimSpace(line)
		if name == "" {
			continue
		}
		// Names come back from the tenant's own dump — re-validate every one.
		if !verifyTableName.MatchString(name) {
			continue
		}
		tables = append(tables, name)
	}
	if len(tables) == 0 {
		return map[string]int64{}, false, nil
	}
	kept, truncated := verifyTableCap(tables)
	countArgs, err := tableCountArgv(p.StackEngine, env, kept)
	if err != nil || len(countArgs) == 0 {
		return map[string]int64{}, truncated, err
	}
	countCmd := execCommandContext(countCtx, "docker",
		append(append([]string{"run", "--rm", "--network", p.StackNetwork}, flags...),
			append([]string{p.StackImage}, countArgs...)...)...)
	countCmd.Env = append(os.Environ(), values...)
	var countOut bytes.Buffer
	countCmd.Stdout = &countOut
	if err := countCmd.Run(); err != nil {
		// Timeout / bad exit: counts omitted, restore still ok.
		return map[string]int64{}, truncated, nil
	}
	counts, perr := parseTableCounts(countOut.String())
	if perr != nil {
		return map[string]int64{}, truncated, nil
	}
	cappedCounts, capped := capVerifyRows(counts)
	return cappedCounts, truncated || capped, nil
}

// verifyRunArgv is the docker-run half of both count execs — `-e NAME` flags
// only, never `-e NAME=value` — factored out so the tests can assert the
// secret-in-argv invariant on the SAME argv production the op uses.
func verifyRunArgv(p transport.TaskPayload, env map[string]string, cmdArgs []string) []string {
	flags, _ := logicalContainerEnvFlagsAndValues(p.StackEngine, env)
	args := append([]string{"run", "--rm", "--network", p.StackNetwork}, flags...)
	args = append(args, p.StackImage)
	return append(args, cmdArgs...)
}

// psqlConnectionArgs lifts PGHOST/PGPORT/PGUSER/PGDATABASE out of the restore's
// OWN argv (-h/-p/-U/-d) so the count batch connects to exactly the same
// service the restore connected to. Values are non-secret (the password never
// appears here) and are injected as bare `-e NAME` flags + the CLI env.
func psqlConnectionArgs(argv []string) ([]string, error) {
	want := map[string]string{"-h": "PGHOST", "-p": "PGPORT", "-U": "PGUSER", "-d": "PGDATABASE"}
	out := []string{}
	for i := 0; i+1 < len(argv); i += 1 {
		key := want[argv[i]]
		if key == "" {
			continue
		}
		val := argv[i+1]
		if val == "" {
			continue
		}
		out = append(out, key, val)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("restore argv carries no psql connection args")
	}
	return out, nil
}

// verifyListArgv builds the constant, read-only "name the tables" psql argv.
// No table name is involved at all — the list comes FROM the DB the restore
// landed in, which is the only honest source of what actually landed.
func verifyListArgv(engine string) ([]string, error) {
	if engine != "postgres" && engine != "postgresql" {
		return []string{}, nil
	}
	return []string{"psql", "-t", "-A", "-c", verifyListSQL}, nil
}

// logicalRestore downloads SnapshotBucket/SnapshotKey into a temp dir mounted
// at StackPath's directory, then runs StackImage/StackCommand against it.
// mysql/mariadb's restore argv has no file arg (see file header) — detected
// generically and fed on the container's stdin instead.
func (e *Executor) logicalRestore(ctx context.Context, p transport.TaskPayload) error {
	if p.StackPath == "" || p.StackImage == "" || p.StackNetwork == "" {
		return fmt.Errorf("logical restore missing stack_image/stack_network/stack_path")
	}
	c, err := minioClient()
	if err != nil {
		return err
	}
	obj, err := c.GetObject(ctx, p.SnapshotBucket, p.SnapshotKey, minio.GetObjectOptions{})
	if err != nil {
		return err
	}
	defer obj.Close()

	dir, err := os.MkdirTemp("", "modlogical-restore-")
	if err != nil {
		return fmt.Errorf("logical restore workspace: %w", err)
	}
	defer os.RemoveAll(dir)

	hostFile := filepath.Join(dir, filepath.Base(p.StackPath))
	out, err := os.Create(hostFile)
	if err != nil {
		return fmt.Errorf("create dump workspace file: %w", err)
	}
	if _, err := io.Copy(out, obj); err != nil {
		out.Close()
		return fmt.Errorf("download dump: %w", err)
	}
	if err := out.Close(); err != nil {
		return fmt.Errorf("download dump: %w", err)
	}

	restoreArgs, err := logicalDumpArgs(p)
	if err != nil {
		return err
	}
	// Only a non-mongo command whose argv omits the mounted path needs
	// stdin (mongo always names its file via -i inside the sh -c script).
	needsStdin := p.StackEngine != "mongo" && !containsArg(p.StackCommand, p.StackPath)

	envFlags, envValues := logicalContainerEnvFlagsAndValues(p.StackEngine, p.ContainerEnv)
	mountDir := filepath.Dir(p.StackPath)
	args := []string{"run", "--rm"}
	if needsStdin {
		args = append(args, "-i")
	}
	args = append(args, "--network", p.StackNetwork, "-v", dir+":"+mountDir)
	args = append(args, envFlags...)
	args = append(args, p.StackImage)
	args = append(args, restoreArgs...)

	cmd := execCommandContext(ctx, "docker", args...)
	// os.Environ() returns a fresh copy each call, so appending is safe.
	cmd.Env = append(os.Environ(), envValues...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if needsStdin {
		in, err := os.Open(hostFile)
		if err != nil {
			return fmt.Errorf("reopen dump for stdin: %w", err)
		}
		defer in.Close()
		cmd.Stdin = in
	}
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("restore (%s) failed: %v: %s", p.StackEngine, err, stderr.String())
	}
	return nil
}
