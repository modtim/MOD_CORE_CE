package executor

// 30.6.1 c1-J3b — pure unit tests for the read-only per-table row-count
// helpers in stacklogical.go. No docker daemon: these assert on the ARGV the
// helpers build and on the parsing/capping rules, which is the whole security
// surface (table names come from a dump the TENANT uploaded). Names MUST start
// with uppercase `Test` (c1-J3a once shipped lowercase names, which `go test`
// silently ran 0 of).
import (
	"fmt"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// T1 — clean output parses, and a 0-row table is a REAL count, never dropped.
func TestT1ParseCounts(t *testing.T) {
	counts, err := parseTableCounts("a 12\n\nb 0\n")
	if err != nil {
		t.Fatalf("parseTableCounts rejected clean output: %s", err)
	}
	if len(counts) != 2 || counts["a"] != 12 || counts["b"] != 0 {
		t.Fatalf("lost a table or a zero count: %v", counts)
	}
}

// T2 — a table name carrying a single quote is REJECTED, no argv returned.
func TestT2Quoting(t *testing.T) {
	env := map[string]string{"PLATFORM_TASK": "1", "PGPASSWORD": "hunter2"}
	argv, err := tableCountArgv("postgres", env, []string{"we'ird"})
	if err == nil {
		t.Fatalf("tableCountArgv accepted an unsafe name: %q", argv)
	}
	if len(argv) != 0 {
		t.Fatalf("rejected name still returned argv: %q", argv)
	}
}

// T3 — 201 tables: the cap keeps 200 and says so (never an error — the restore
// itself succeeded).
func TestT3TableCap(t *testing.T) {
	tables := []string{}
	for i := 0; i < 201; i++ {
		tables = append(tables, fmt.Sprintf("t%d", i))
	}
	kept, truncated := verifyTableCap(tables)
	if len(kept) != maxVerifyTables || !truncated {
		t.Fatalf("table cap wrong: kept=%d truncated=%t", len(kept), truncated)
	}
	env := map[string]string{"PLATFORM_TASK": "1"}
	argv, err := tableCountArgv("postgres", env, kept)
	if err != nil {
		t.Fatalf("over-cap tables must not error: %s", err)
	}
	if len(argv) != 3+maxVerifyTables {
		t.Fatalf("argv carried every name, not the capped 200: %d", len(argv))
	}
}

// T4 — a count above maxVerifyRows is recorded AS maxVerifyRows with the cap
// flag set (honest: never a silent lie about what landed).
func TestT4RowCapOverflow(t *testing.T) {
	counts, err := parseTableCounts("big 10000001")
	if err != nil {
		t.Fatalf("parseTableCounts rejected a legal big count: %s", err)
	}
	capped, cappedFlag := capVerifyRows(counts)
	if !cappedFlag || capped["big"] != maxVerifyRows {
		t.Fatalf("row cap not applied: %v capped=%t", capped, cappedFlag)
	}
}

// T5 — a non-integer count is an error, never a 0.
func TestT5MalformedLine(t *testing.T) {
	_, err := parseTableCounts("x notanint")
	if err == nil {
		t.Fatal("parseTableCounts accepted a non-integer count")
	}
}

// T6 — the same table named twice is a lie about what landed: error.
func TestT6DuplicateTableName(t *testing.T) {
	_, err := parseTableCounts("dup 3\ndup 4")
	if err == nil {
		t.Fatal("parseTableCounts accepted a duplicate table name")
	}
}

// T7 — the password VALUE appears in NO argv element (the file header's
// invariant: the secret rides the CLI's own environment, never argv).
func TestT7NoSecretInArgv(t *testing.T) {
	secret := "s3cr3t-p@ss"
	env := map[string]string{"PLATFORM_TASK": "1", "PGPASSWORD": secret}
	argv, err := tableCountArgv("postgres", env, []string{"alpha", "beta_2", "gamma-x"})
	if err != nil {
		t.Fatalf("safe names rejected: %s", err)
	}
	for _, a := range argv {
		if strings.Contains(a, secret) {
			t.Fatalf("password value leaked into argv element %q", a)
		}
	}
	// The FULL docker-run argv the count execs use carries the bare `-e NAME`
	// form only — the value rides the CLI's own environment instead.
	p := transport.TaskPayload{
		StackEngine:  "postgres",
		StackImage:   "docker.io/shell/postgres:16",
		StackNetwork: "modstack-7_db",
		StackCommand: []string{"pg_restore", "--clean", "-h", "db", "-p", "5432",
			"-U", "postgres", "-d", "moddb", "/backup/dump.dump"},
	}
	runArgv := verifyRunArgv(p, env, argv)
	for _, a := range runArgv {
		if strings.Contains(a, secret) {
			t.Fatalf("password value leaked into run argv element %q", a)
		}
	}
	if !containsArg(runArgv, "-e") || !containsArg(runArgv, "PGPASSWORD") {
		t.Fatalf("count run argv lost the env channel: %q", runArgv)
	}
}

// T8 — backtick, `;`, `$(...)`, a NUL, whitespace and a newline are ALL
// rejected before any exec runs.
func TestT8DollarBacktickRejected(t *testing.T) {
	env := map[string]string{"PLATFORM_TASK": "1"}
	hostile := []string{"a`b", "a;b", "a$(id)", "a\x00b", "a\nb",
		"a'; DROP TABLE x; --", "a|b", "a&b", "a(b)", "a>b", "a\"b", "a.b\tc"}
	rejected := 0
	for _, name := range hostile {
		argv, err := tableCountArgv("postgres", env, []string{name})
		if err != nil && len(argv) == 0 {
			rejected++
			continue
		}
		t.Fatalf("hostile table name %q was not rejected: %q", name, argv)
	}
	if rejected != len(hostile) {
		t.Fatalf("rejected %d/%d hostile names", rejected, len(hostile))
	}
}

// T9 — empty stdout is an empty map, not an error (a restore that landed no
// user tables is legitimately "nothing to count").
func TestT9EmptyOutput(t *testing.T) {
	counts, err := parseTableCounts("")
	if err != nil {
		t.Fatalf("empty output errored: %s", err)
	}
	if len(counts) != 0 {
		t.Fatalf("empty output produced counts: %v", counts)
	}
}
