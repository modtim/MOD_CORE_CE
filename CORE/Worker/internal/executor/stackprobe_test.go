package executor

// 30.6.1 c1-J4 (GX-c1 restore guard) — the probe_empty op's helpers. No docker
// daemon here: every child process goes through the execCommandContext seam
// (stackrunner.go:783), swapped by withFakeExec, so the tests assert on the EXACT
// argv the op built (read-only mount, no `sh -c`) and on how output was counted.
import (
	"context"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// P1 — top-level entries are counted, read-only, no shell.
func TestProbeCountsTopLevelEntries(t *testing.T) {
	var calls []string
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		_ = name
		calls = append(calls, strings.Join(arg, " "))
		joined := strings.Join(arg, " ")
		switch {
		case strings.Contains(joined, "volume inspect"):
			return exec.CommandContext(ctx, "sh", "-c", `printf '/data\n'`)
		case strings.Contains(joined, "ls -1A /data"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'a\nb\nlost+found\nc\n'`)
		default:
			return exec.CommandContext(ctx, "sh", "-c", `printf ''`)
		}
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "probe_empty",
		StackProject: "modstack7g0",
		ProbeVolumes: []string{"modstack7g0_data"},
	}}
	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("probe_empty failed: %v", err)
	}
	if !strings.Contains(res.LogTail, `"modstack7g0_data":3`) {
		t.Fatalf("expected 3 entries, got log tail %q", res.LogTail)
	}
	joined := strings.Join(calls, " | ")
	if !strings.Contains(joined, "modstack7g0_data:/data:ro") {
		t.Fatalf("volume mount was not read-only: %s", joined)
	}
	if strings.Contains(joined, "sh -c") {
		t.Fatalf("a probe exec went through a shell: %s", joined)
	}
}

// P2 — lost+found never makes a volume look non-empty.
func TestProbeExcludesLostFound(t *testing.T) {
	if n := countTopLevelEntries("lost+found\n"); n != 0 {
		t.Fatalf("lost+found alone must count 0, got %d", n)
	}
	if n := countTopLevelEntries("lost+found\nnotes\n"); n != 1 {
		t.Fatalf("lost+found plus one entry must count 1, got %d", n)
	}
}

// P3 — a volume that does not exist yet counts 0, never an error, and is never
// mounted (docker run -v would auto-create it).
func TestProbeMissingVolumeCountsZero(t *testing.T) {
	var calls []string
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		_ = name
		calls = append(calls, strings.Join(arg, " "))
		if strings.Contains(strings.Join(arg, " "), "volume inspect") {
			return exec.CommandContext(ctx, "sh", "-c",
				`printf 'docker: Error: no such volume: modstack7g0_data does not exist.' >&2; exit 1`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `printf ''`)
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "probe_empty",
		StackProject: "modstack7g0",
		ProbeVolumes: []string{"modstack7g0_data"},
	}}
	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("a missing volume must count 0, not fail: %v", err)
	}
	if !strings.Contains(res.LogTail, `"modstack7g0_data":0`) {
		t.Fatalf("missing volume must count 0, got log tail %q", res.LogTail)
	}
	for _, c := range calls {
		if strings.Contains(c, "run --rm") {
			t.Fatalf("a missing volume was mounted, which docker would create: %s", c)
		}
	}
}

// P4 — psql's "-tAc" answer "5\n" parses to 5.
func TestProbePsqlAnswerParsesFive(t *testing.T) {
	n, err := parseTableCount("5\n")
	if err != nil {
		t.Fatalf("5 must parse: %v", err)
	}
	if n != 5 {
		t.Fatalf("5 must parse to 5, got %d", n)
	}
}

// P5 — garbage psql output is an error, never a 0.
func TestProbeGarbagePsqlOutputFails(t *testing.T) {
	for _, bad := range []string{"", "psql: connection refused", "5 tables", "n/a", "-1"} {
		if n, err := parseTableCount(bad); err == nil {
			t.Fatalf("garbage %q must be an error, not %d", bad, n)
		}
	}
}

// P6 - a volume name from ANOTHER stack fails the task, with zero exec calls.
func TestProbeForeignVolumePrefixFails(t *testing.T) {
	var calls []string
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		_ = name
		calls = append(calls, strings.Join(arg, " "))
		return exec.CommandContext(ctx, "sh", "-c", `printf ''`)
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "probe_empty",
		StackProject: "modstack7g0",
		ProbeVolumes: []string{"modstack99g0_data"},
	}}
	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatalf("a foreign stack's volume must fail the probe")
	}
	if !strings.Contains(err.Error(), "invalid probe volume") {
		t.Fatalf("want the name-rejection reason, got %q", err.Error())
	}
	if len(calls) != 0 {
		t.Fatalf("a rejected name reached %d exec calls: %v", len(calls), calls)
	}
}

// P7 - a name carrying shell/CLI junk (`;` or `/`) fails the task, zero execs.
func TestProbeUnsafeVolumeNameFails(t *testing.T) {
	for _, bad := range []string{"modstack7g0_data; rm -rf /", "modstack7g0_data/other"} {
		var calls []string
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			_ = name
			calls = append(calls, strings.Join(arg, " "))
			return exec.CommandContext(ctx, "sh", "-c", `printf ''`)
		})
		defer restore()

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp:      "probe_empty",
			StackProject: "modstack7g0",
			ProbeVolumes: []string{bad},
		}}
		_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
		if err == nil {
			t.Fatalf("unsafe name %q must fail the probe", bad)
		}
		if !strings.Contains(err.Error(), "invalid probe volume") {
			t.Fatalf("want the name-rejection reason for %q, got %q", bad, err.Error())
		}
		if len(calls) != 0 {
			t.Fatalf("unsafe name %q reached %d exec calls: %v", bad, len(calls), calls)
		}
	}
}
