package main

// launch-completion-megaplan GP.16 — branch-selection tests for native
// execution dispatch. These exercise the routing decision (docker vs native
// vs native-unavailable) and the fail-closed contract (a native task never
// silently falls back to docker) without spawning host processes: the backend
// is a fake, and the result is captured through a real outbox.Reporter backed
// by a recording Sender (the same result path the docker branch uses).

import (
	"context"
	"io"
	"log/slog"
	"runtime"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/outbox"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// --- fakes -----------------------------------------------------------------

// fakeRunner is a minimal nativeRunner: it never starts a process. Poll
// reports the phase configured by the test, so dispatchNativeTask runs its
// full poll -> (collect | fail) path deterministically.
type fakeRunner struct {
	phase      executor.TaskPhase
	submitErr  error
	collectErr error
	outputs    []transport.OutputFile
	submitted  bool
}

var _ nativeRunner = (*fakeRunner)(nil)

func (f *fakeRunner) Submit(ctx context.Context, spec *executor.TaskSpec) (*executor.Handle, error) {
	f.submitted = true
	if f.submitErr != nil {
		return nil, f.submitErr
	}
	return &executor.Handle{Backend: "native", ID: "1"}, nil
}
func (f *fakeRunner) SubmitStaged(ctx context.Context, spec *executor.TaskSpec, task *transport.Task, creds *transport.Credentials) (*executor.Handle, error) {
	return f.Submit(ctx, spec)
}
func (f *fakeRunner) Poll(ctx context.Context, h *executor.Handle) (*executor.TaskStatus, error) {
	return &executor.TaskStatus{Phase: f.phase}, nil
}
func (f *fakeRunner) Logs(ctx context.Context, h *executor.Handle) (io.ReadCloser, error) {
	return nil, nil
}
func (f *fakeRunner) Cancel(ctx context.Context, h *executor.Handle) error { return nil }
func (f *fakeRunner) CollectOutputs(ctx context.Context, h *executor.Handle, task *transport.Task, creds *transport.Credentials) ([]transport.OutputFile, error) {
	if f.collectErr != nil {
		return nil, f.collectErr
	}
	return f.outputs, nil
}

// recordingSender implements outbox.Sender and records the terminal report.
type recordingSender struct {
	completed bool
	failed    bool
	failedMsg string
}

func (r *recordingSender) ReportComplete(ctx context.Context, taskID int, result transport.TaskResult) error {
	r.completed = true
	return nil
}
func (r *recordingSender) ReportFailed(ctx context.Context, taskID int, msg string, shouldRetry bool, logTail string, oomKilled bool, errorDetail string) error {
	r.failed = true
	r.failedMsg = msg
	return nil
}

// newTestReporter builds a real outbox.Reporter (the production type the
// docker path reports through) over a throwaway store + the recording Sender.
func newTestReporter(t *testing.T, send outbox.Sender) (*outbox.Reporter, *recordingSender) {
	t.Helper()
	rec := send.(*recordingSender)
	store, err := outbox.New(t.TempDir())
	if err != nil {
		t.Fatalf("outbox store: %v", err)
	}
	rep := outbox.NewReporter(store, send, slog.New(slog.DiscardHandler), 0)
	return rep, rec
}

// nativeTask builds a task with an execution flag and (optionally) a recipe.
func nativeTask(execution string, withRecipe bool) *transport.Task {
	tk := &transport.Task{ID: 7, TenantID: 1}
	tk.Payload.Execution = execution
	if withRecipe {
		tk.Payload.Native = &transport.NativeRecipe{
			Platform: runtime.GOOS,
			Command:  "echo",
		}
	}
	return tk
}

// --- branch selection -------------------------------------------------------

func TestShouldRunNativeTaskSelection(t *testing.T) {
	cases := []struct {
		execution string
		want      bool
	}{
		{"native", true},
		{"Native", true},
		{" NATIVE ", true},
		{"", false},           // older API: docker
		{"docker", false},     // docker
		{"k8s", false},        // k8s gate, not native
		{"container", false},
	}
	for _, c := range cases {
		if got := shouldRunNativeTask(c.execution); got != c.want {
			t.Errorf("shouldRunNativeTask(%q) = %v, want %v", c.execution, got, c.want)
		}
	}
}

// TestNativeDispatchBranchSelection proves the three outcomes: a native task
// with a working backend COMPLETES (runs native, not docker), a native task
// where the backend is unavailable FAILS loudly, and neither path ever
// "completes via docker".
func TestNativeDispatchBranchSelection(t *testing.T) {
	ctx := context.Background()
	logger := slog.New(slog.DiscardHandler)

	// 1) native + working backend -> reported Complete (not docker).
	{
		rep, rec := newTestReporter(t, &recordingSender{})
		runner := &fakeRunner{phase: executor.TaskSucceeded, outputs: []transport.OutputFile{{ContentHash: "abc"}}}
		dispatchNativeTask(ctx, logger, runner, rep, nativeTask("native", true), &transport.Credentials{})
		if !rec.completed || rec.failed {
			t.Errorf("native + backend: completed=%v failed=%v msg=%q; want completed=true, failed=false", rec.completed, rec.failed, rec.failedMsg)
		}
		if !runner.submitted {
			t.Errorf("native + backend: backend.SubmitStaged not called (did it fall back to docker?)")
		}
	}

	// 2) native but NO backend -> reported Failed, never Complete, and no
	//    docker submission (a fakeRunner would have been used if it fell
	//    through to a container path; here the backend is nil outright).
	{
		rep, rec := newTestReporter(t, &recordingSender{})
		dispatchNativeTask(ctx, logger, nil, rep, nativeTask("native", true), &transport.Credentials{})
		if rec.completed || !rec.failed {
			t.Errorf("native, no backend: completed=%v failed=%v; want completed=false, failed=true (loud, no docker fallback)", rec.completed, rec.failed)
		}
		if rec.failedMsg == "" {
			t.Errorf("native, no backend: failure carried no message")
		}
	}

	// 3) native but recipe missing -> Failed, never Complete.
	{
		rep, rec := newTestReporter(t, &recordingSender{})
		dispatchNativeTask(ctx, logger, &fakeRunner{phase: executor.TaskSucceeded}, rep, nativeTask("native", false), &transport.Credentials{})
		if rec.completed || !rec.failed {
			t.Errorf("native, no recipe: completed=%v failed=%v; want failed=true (never a docker fallback)", rec.completed, rec.failed)
		}
	}

	// 4) a non-native task never enters this dispatch at all — the routing
	//    decision is shouldRunNativeTask, already covered above; here we assert
	//    that an empty execution flag is NOT routed native.
	if shouldRunNativeTask(nativeTask("", true).Payload.Execution) {
		t.Errorf("empty execution must not route native")
	}
}
