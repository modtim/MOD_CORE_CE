package executor

// GX.S9 — the volume-tar `snapshot` op must write-freeze a DEPLOYED stack
// before tarring its live data volume and ALWAYS thaw afterwards, even when
// the snapshot itself fails. These tests pin the freeze/unfreeze ORDER (not
// just membership): a thaw that is skipped on failure leaves the customer's
// stack frozen, and a thaw that used the wrong subcommand (unpause on a
// stopped container) would be a silent no-op.
//
// The fake S3 server does not implement minio-go's multipart PUT (the
// snapshot streams with unknown size, so minio-go always multipart-PUTs —
// a pre-existing test-infra gap, why no earlier test covered snapshotVolume
// success), so the MinIO leg is expected to 405 here. The assertions are on
// the compose event ORDER and the log stream, which are unaffected: the
// deferred unfreeze runs on every exit path, success or failure.

import (
	"context"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// withFreezeRecorder fakes all docker shell-outs, recording an event for
// every compose lifecycle subcommand and one "tar" event for the snapshot's
// tar, so the caller can assert on their ORDER. pauseFailing makes the
// compose `pause` call fail (the stop-fallback path). tarFailing makes the
// snapshot tar exit non-zero.
func withFreezeRecorder(events *[]string, pauseFailing, tarFailing bool) func() {
	return withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		if strings.Contains(full, "compose") {
			// compose argv ends with the subcommand.
			if a := arg[len(arg)-1]; a == "pause" || a == "unpause" || a == "stop" || a == "start" {
				*events = append(*events, a)
			}
			if arg[len(arg)-1] == "pause" && pauseFailing {
				return exec.CommandContext(ctx, "sh", "-c",
					`echo "pause not supported by this runtime" >&2; exit 1`)
			}
			return exec.CommandContext(ctx, "sh", "-c", `echo "frozen"`)
		}
		if strings.Contains(full, "tar") {
			*events = append(*events, "tar")
			if tarFailing {
				return exec.CommandContext(ctx, "sh", "-c", `echo "tar boom" >&2; exit 1`)
			}
			return exec.CommandContext(ctx, "sh", "-c", `echo "fake-tar-bytes"`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected argv: `+full+`" >&2; exit 1`)
	})
}

func snapshotTask(project string) *transport.Task {
	return &transport.Task{Payload: transport.TaskPayload{
		StackOp:        "snapshot",
		StackProject:   project,
		StackVolume:    "modstack9g0_pgdata",
		SnapshotBucket: "stack-snapshots",
		SnapshotKey:    "modstack9g0/snap1/pgdata.tar.gz",
	}}
}

func TestRunStackOpSnapshotFreezeOrder(t *testing.T) {
	withFakeS3Env(t, "stack-snapshots")

	t.Run("pause before tar, unpause after — thaw method matches the freeze", func(t *testing.T) {
		var events []string
		var logged []string
		cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}
		restore := withFreezeRecorder(&events, false, false)
		defer restore()

		_, _ = (&Executor{}).runStackOp(context.Background(), snapshotTask("modstack9g0"), nil, time.Now(), cb)

		// Exact ORDER: pause, then the tar, then the thaw — and the thaw is
		// unpause (the counterpart of a successful pause), never stop/start.
		want := []string{"pause", "tar", "unpause"}
		if len(events) != len(want) {
			t.Fatalf("event order = %v, want %v", events, want)
		}
		for i := range want {
			if events[i] != want[i] {
				t.Fatalf("event order = %v, want %v", events, want)
			}
		}
		// The thaw succeeded: no "left frozen, manual recovery" line.
		for _, ln := range logged {
			if strings.Contains(ln, "manual recovery required") {
				t.Fatalf("thaw failure logged: %q", ln)
			}
		}
	})

	t.Run("unpause still runs when the snapshot fails", func(t *testing.T) {
		var events []string
		cb := Callbacks{}
		restore := withFreezeRecorder(&events, false, true)
		defer restore()

		_, err := (&Executor{}).runStackOp(context.Background(), snapshotTask("modstack9g0"), nil, time.Now(), cb)
		if err == nil {
			t.Fatal("expected the snapshot to fail")
		}
		if !strings.Contains(err.Error(), "tar boom") {
			t.Fatalf("expected the simulated tar failure in the error, got: %v", err)
		}
		// A half-finished backup must never leave the stack frozen.
		want := []string{"pause", "tar", "unpause"}
		if len(events) != len(want) {
			t.Fatalf("event order = %v, want %v", events, want)
		}
		for i := range want {
			if events[i] != want[i] {
				t.Fatalf("event order = %v, want %v", events, want)
			}
		}
	})

	t.Run("pause refused falls back to stop and thaws with start", func(t *testing.T) {
		var events []string
		var logged []string
		cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}
		restore := withFreezeRecorder(&events, true, false)
		defer restore()

		_, _ = (&Executor{}).runStackOp(context.Background(), snapshotTask("modstack9g0"), nil, time.Now(), cb)

		// The thaw MUST match the freeze: unpause on a stopped container is
		// a no-op, so the stop-fallback path thaws with start.
		want := []string{"pause", "stop", "tar", "start"}
		if len(events) != len(want) {
			t.Fatalf("event order = %v, want %v", events, want)
		}
		for i := range want {
			if events[i] != want[i] {
				t.Fatalf("event order = %v, want %v", events, want)
			}
		}
		joined := strings.Join(logged, "\n")
		if !strings.Contains(joined, "pause unavailable") {
			t.Fatalf("freeze fallback was not announced in the log: %q", joined)
		}
		if strings.Contains(joined, "manual recovery required") {
			t.Fatalf("thaw failure logged: %q", joined)
		}
	})

	t.Run("no project: bare snapshot, no freeze at all", func(t *testing.T) {
		var events []string
		restore := withFreezeRecorder(&events, false, false)
		defer restore()

		_, _ = (&Executor{}).runStackOp(context.Background(), snapshotTask(""), nil, time.Now(), Callbacks{})

		if len(events) != 1 || events[0] != "tar" {
			t.Fatalf("bare snapshot must only tar (migration/backfill target), got %v", events)
		}
	})
}
