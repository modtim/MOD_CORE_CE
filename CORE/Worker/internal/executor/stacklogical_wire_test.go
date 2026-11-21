package executor

// 30.6.1 c1-J3d — the WIRE half of the logical-restore verify counts: logicalVerifyCounts
// is invoked ONLY after a clean logicalRestore, its counts land on Result.VerifyTables /
// VerifyTruncated (and from there on transport.TaskResult in cmd/worker/main.go), and its
// bookkeeping NEVER fails a restore that already succeeded.
//
// W1 counts on a clean restore; W2 count-batch failure never fails the restore;
// W3 >200 tables truncates; W4 counts never run on a FAILED restore; W5 empty
// count output leaves the map nil so the wire key is omitted.

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// verifyPayload is a postgres logical_restore payload shaped like the ones in
// stacklogical_test.go — the psql connection args live in StackCommand.
func verifyPayload() transport.TaskPayload {
	return transport.TaskPayload{
		StackOp:      "logical_restore",
		StackEngine:  "postgres",
		StackImage:   "postgres:16",
		StackNetwork: "modstack9g0_default",
		StackService: "db",
		StackCommand: []string{"pg_restore", "--clean", "--if-exists",
			"-h", "db", "-p", "5432", "-U", "postgres", "-d", "postgres", "/backup/dump.dump"},
		StackPath:      "/backup/dump.dump",
		SnapshotBucket: "stack-snapshots",
		SnapshotKey:    "modstack9g0/w/verify.dump",
		ContainerEnv:   map[string]string{"PLATFORM_TASK": "1"},
	}
}

// tableListBody emits n well-formed table names for the list exec.
func tableListBody(n int) string {
	var b strings.Builder
	for i := 0; i < n; i++ {
		b.WriteString("t" + fmt.Sprint(i) + "\\n")
	}
	return b.String()
}

// driveRestore runs the logical_restore op against the package's seams: fakeS3 for
// the dump download, execCommandContext for every docker exec. It answers the two
// count execs (the table list and the count batch) with canned bodies/exits and the
// restore itself with restoreExit, and records every argv it saw. Bodies carry
// literal \n pairs so the canned sh printf emits the lines.
func driveRestore(t *testing.T, listBody, countBody string, countExit, restoreExit int) (*Result, error, [][]string) {
	f, _ := withFakeS3Env(t, "stack-snapshots")
	f.objs["modstack9g0/w/verify.dump"] = []byte("-- dump --")
	seen := [][]string{}
	orig := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		seen = append(seen, append([]string{name}, arg...))
		// The restore exec is the ONLY one that is not a count exec.
		if !strings.Contains(full, "psql -t -A") {
			return exec.CommandContext(ctx, "sh", "-c", "exit "+fmt.Sprint(restoreExit))
		}
		// The count batch (verifyCountsScript) is the one that SELECTs COUNT(1).
		if strings.Contains(full, "SELECT COUNT(1)") {
			return exec.CommandContext(ctx, "sh", "-c", "printf '"+countBody+"' | cat; exit "+fmt.Sprint(countExit))
		}
		return exec.CommandContext(ctx, "sh", "-c", "printf '"+listBody+"'")
	}
	defer func() { execCommandContext = orig }()

	e := &Executor{}
	task := &transport.Task{Payload: verifyPayload()}
	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	return res, err, seen
}

// countBodyOf emits "<table> <rows> \n" lines for the first n tables of tableListBody(n).
// countBodyOf emits "<table> <rows> \n" lines for the first n tables of tableListBody(n).
func countBodyOf(n int) string {
	var b strings.Builder
	for i := 0; i < n; i++ {
		b.WriteString("t" + fmt.Sprint(i) + " " + fmt.Sprint(i) + "\\n")
	}
	return b.String()
}

// W1 — clean restore + well-formed counts: the Result carries them.
func TestWLogicalRestoreCarriesVerifyCounts(t *testing.T) {
	res, err, _ := driveRestore(t, tableListBody(2), countBodyOf(2), 0, 0)
	if err != nil {
		t.Fatalf("restore failed: %v", err)
	}
	if res.VerifyTables == nil {
		t.Fatal("expected VerifyTables on the Result after a clean restore with counts")
	}
	if len(res.VerifyTables) != 2 || res.VerifyTables["t0"] != 0 || res.VerifyTables["t1"] != 1 {
		t.Fatalf("counts wrong: %v", res.VerifyTables)
	}
	if res.VerifyTruncated {
		t.Fatal("2 tables must not be flagged truncated")
	}
}

// W2 — the count batch fails (non-zero exit): the restore STILL succeeds and the
// wire body carries no counts at all (nil map → key omitted).
func TestWCountBatchFailureNeverFailsRestore(t *testing.T) {
	res, err, _ := driveRestore(t, tableListBody(2), countBodyOf(2), 7, 0)
	if err != nil {
		t.Fatalf("count-batch failure leaked into the restore result: %v", err)
	}
	if res.VerifyTables != nil {
		t.Fatalf("failed count batch must yield no counts, got: %v", res.VerifyTables)
	}
	if res.VerifyTruncated {
		t.Fatal("truncated flag must be false when no counts landed")
	}
}

// W3 — more tables than maxVerifyTables: truncated flag set, at most the cap kept.
func TestWTruncateOverCap(t *testing.T) {
	res, err, _ := driveRestore(t, tableListBody(201), countBodyOf(200), 0, 0)
	if err != nil {
		t.Fatalf("restore failed: %v", err)
	}
	if res.VerifyTables == nil || len(res.VerifyTables) != 200 {
		t.Fatalf("expected exactly the cap (200) of counts, got %v", res.VerifyTables)
	}
	if !res.VerifyTruncated {
		t.Fatal("201 tables must set VerifyTruncated")
	}
}

// W4 — the restore itself fails: the op errors and NO count exec ever runs, so a
// half-applied DB is never reported as verified.
func TestWNoCountsOnFailedRestore(t *testing.T) {
	res, err, seen := driveRestore(t, tableListBody(2), countBodyOf(2), 0, 9)
	if err == nil {
		t.Fatal("failed restore must still surface as an error")
	}
	if res != nil {
		t.Fatal("failed restore must not return a Result")
	}
	for _, argv := range seen {
		if strings.Contains(strings.Join(argv, " "), "psql -t -A") {
			t.Fatalf("count exec ran on a failed restore: %v", argv)
		}
	}
}

// W5 — the list exec returns no tables (TestT9-shaped empty DB restore): the map
// stays nil, NOT {}, so the completion body is byte-identical for old consumers.
func TestWEmptyCountsLeaveWireKeyOmitted(t *testing.T) {
	res, err, _ := driveRestore(t, "", "", 0, 0)
	if err != nil {
		t.Fatalf("restore failed: %v", err)
	}
	if res.VerifyTables != nil {
		t.Fatalf("empty count output must leave the map nil (key omitted), got %v", res.VerifyTables)
	}
}
