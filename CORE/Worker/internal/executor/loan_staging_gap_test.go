package executor

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// loan_staging_gap_test.go — GC.MWL.12 follow-ups (Gaps A and B).
//
// Gap A: the mc-sidecar / K8s stage-out output-upload path (the shared
// buildUploadScript) must stamp the SAME loan markers the worker's minio-go
// native path stamps, via `mc cp --attr "..."` — only when task.Payload.LoanID
// is set, and never for non-loan tasks.
// Gap B: NativeBackend.Cancel (the kill path) must best-effort invoke the
// PartialCollector helper for a loaned task, and not for a non-loan one, with
// collector errors logged and never propagated.

// ============================================================================
// Gap A — buildUploadScript / LoanOutputAttr
// ============================================================================

// The mc-sidecar (Docker uploadSidecar) and the K8s stage-out container both
// build their upload command from buildUploadScript, so asserting the --attr
// flag lands there pins both paths at once.
func TestUploadScriptLoanTaskStampsAttrFlag(t *testing.T) {
	// Docker sidecar path.
	docker := buildUploadScript(uploadScriptParamsFromTask(&transport.Task{
		Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: "913"},
	}))
	// K8s stage-out path.
	k8s := buildUploadCmd(&transport.Task{
		Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: "913"},
	})
	for name, got := range map[string]string{"docker": docker, "k8s": k8s} {
		if !strings.Contains(got, `--attr "Mod-Loan-Id=913;Mod-Loan-Status=uncollected"`) {
			t.Errorf("%s: loaned task upload script must carry the --attr loan marker, got:\n%s", name, got)
		}
		// The marker must be attached to the actual `mc cp` upload, not
		// anywhere else in the script.
		if !strings.Contains(got, `myminio/tenant-1-files/$hash --attr "Mod-Loan-Id=913;Mod-Loan-Status=uncollected"`) {
			t.Errorf("%s: --attr not appended to the mc cp upload line, got:\n%s", name, got)
		}
	}
}

// A loan id (not a wizard prefix) must still work alongside the wizard
// output prefix — the two are orthogonal.
func TestUploadScriptLoanWithWizardPrefix(t *testing.T) {
	got := buildUploadScript(uploadScriptParamsFromTask(&transport.Task{
		Payload: transport.TaskPayload{
			MinioBucket: "tenant-7-files", Purpose: "wizard_test_run",
			OutputPrefix: "_wizard_test_runs/s1/2/", SandboxMaxOutputMB: 5,
			LoanID: "913",
		},
	}))
	if !strings.Contains(got, `myminio/tenant-7-files/_wizard_test_runs/s1/2/$hash --attr "Mod-Loan-Id=913;Mod-Loan-Status=uncollected"`) {
		t.Fatalf("loan + wizard prefix must both appear; got:\n%s", got)
	}
}

// A UUID loan id must be accepted too (WorkLoan.id is an integer today, but a
// UUID-shaped id is a valid shell-safe token and must stamp, not skip).
func TestUploadScriptLoanUUIDIDStamps(t *testing.T) {
	const uuid = "3f2c1a9e-8b4d-4f01-9c7a-5e6d7f8a9b0c"
	got := buildUploadScript(uploadScriptParamsFromTask(&transport.Task{
		Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: uuid},
	}))
	if !strings.Contains(got, `--attr "Mod-Loan-Id=`+uuid+`;Mod-Loan-Status=uncollected"`) {
		t.Fatalf("UUID loan id must be stamped; got:\n%s", got)
	}
}

// LoanOutputAttr is the pre-rendered, shell-safe --attr argument. The value
// must be exactly the double-quoted, `;`-separated key=value pairs — so it
// can be interpolated verbatim into the generated shell command.
func TestLoanOutputAttrRendersQuotedShellSafeArg(t *testing.T) {
	if got := LoanOutputAttr("913"); got != `"Mod-Loan-Id=913;Mod-Loan-Status=uncollected"` {
		t.Fatalf("LoanOutputAttr(913) = %q, want the double-quoted key=value pair", got)
	}
	if got := LoanOutputAttr(""); got != "" {
		t.Fatalf("LoanOutputAttr(\"\") must be empty (no flag) for non-loan, got %q", got)
	}
}

// shellSafeMetaValue: the real id shapes are allowed; shell metacharacters are
// not (they could break out of the generated command).
func TestShellSafeMetaValue(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"913", true},
		{"3f2c1a9e-8b4d-4f01-9c7a-5e6d7f8a9b0c", true},
		{"abc-123_X.y", true},
		{"has space", false},
		{"semi;colon", false},
		{"quote\"", false},
		{"$VAR", false},
		{"back\\slash", false},
		{"", false},
	}
	for _, c := range cases {
		if got := shellSafeMetaValue(c.in); got != c.want {
			t.Errorf("shellSafeMetaValue(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

// Invalid (not shell-safe) loan ids are skipped with a log line: the script
// must be byte-for-byte identical to the non-loan one.
func TestUploadScriptInvalidLoanIDSkipsStamp(t *testing.T) {
	base := buildUploadCmd(&transport.Task{Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}})
	for _, bad := range []string{"1; rm -rf /", "has space", "quote\"x"} {
		got := buildUploadCmd(&transport.Task{Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: bad}})
		if strings.Contains(got, "--attr") {
			t.Fatalf("invalid loan id %q must NOT be stamped, got:\n%s", bad, got)
		}
		if got != base {
			t.Fatalf("invalid loan id %q: script must equal the non-loan script byte-for-byte", bad)
		}
	}
}

// ============================================================================
// Gap B — NativeBackend.Cancel partial-output recovery
// ============================================================================

// fakePartialStager is a fakeStager (NativeStager) that ALSO implements
// PartialCollector, so NativeBackend.CollectPartialOutputs will invoke it.
// It records every CollectPartialOutputs call and can be made to error, to
// prove the error is swallowed (never propagated through Cancel).
type fakePartialStager struct {
	*fakeStager
	mu            sync.Mutex
	partialN      int
	partialErr    error
	partialLoanID string
	partialTaskID int
}

func (f *fakePartialStager) CollectPartialOutputs(_ context.Context, task *transport.Task, _ *transport.Credentials, _ string, loanID string) ([]transport.OutputFile, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.partialN++
	f.partialLoanID = loanID
	if task != nil {
		f.partialTaskID = task.ID
	}
	if f.partialErr != nil {
		return nil, f.partialErr
	}
	return []transport.OutputFile{{ContentHash: "h", FileName: "f", SizeBytes: 1}}, nil
}

var _ PartialCollector = (*fakePartialStager)(nil)

// submitLoanTask runs a long native sleep as task 42 (staged, so Cancel has
// the task recorded), writes a partial output file, and polls until running.
func submitLoanTask(t *testing.T, scratch string, nb *NativeBackend, task *transport.Task, creds *transport.Credentials) *Handle {
	t.Helper()
	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "sleep 30"},
		Env:           map[string]string{"MOD_TASK_ID": "42"},
	}
	h, err := nb.SubmitStaged(context.Background(), spec, task, creds)
	if err != nil {
		t.Fatalf("SubmitStaged: %v", err)
	}
	outputDir := filepath.Join(scratch, "42", "output")
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outputDir, "partial.png"), []byte("half done"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Ensure the process is up and the run registered before we kill it.
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		st, perr := nb.Poll(context.Background(), h)
		if perr != nil {
			t.Fatalf("Poll: %v", perr)
		}
		if st.Phase != TaskRunning {
			t.Fatalf("task left running too soon: %+v", st)
		}
		break
	}
	return h
}

func TestCancelLoanTaskInvokesPartialCollector(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	fs := &fakePartialStager{fakeStager: &fakeStager{}}
	nb.SetStager(fs)

	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: "913"}}
	creds := &transport.Credentials{}
	h := submitLoanTask(t, scratch, nb, task, creds)

	if err := nb.Cancel(context.Background(), h); err != nil {
		t.Fatalf("Cancel must not error for a loaned kill: %v", err)
	}

	// Wait a moment for the best-effort collection to have run (it is invoked
	// synchronously before Cancel returns, but the Wait goroutine flipping the
	// phase to failed happens around the kill).
	fs.mu.Lock()
	n, loanID, taskID := fs.partialN, fs.partialLoanID, fs.partialTaskID
	fs.mu.Unlock()
	if n == 0 {
		t.Fatal("Cancel of a loaned task must invoke the PartialCollector")
	}
	if loanID != "913" {
		t.Fatalf("partial collector got loan id %q, want 913", loanID)
	}
	if taskID != 42 {
		t.Fatalf("partial collector got task id %d, want 42", taskID)
	}
}

func TestCancelNonLoanTaskDoesNotInvokePartialCollector(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	fs := &fakePartialStager{fakeStager: &fakeStager{}}
	nb.SetStager(fs)

	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}} // no LoanID
	h := submitLoanTask(t, scratch, nb, task, &transport.Credentials{})

	if err := nb.Cancel(context.Background(), h); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	fs.mu.Lock()
	n := fs.partialN
	fs.mu.Unlock()
	if n != 0 {
		t.Fatalf("a non-loan kill must NOT invoke the PartialCollector, but it was called %d times", n)
	}
}

// A plain Submit (no staging → no recorded task/creds) must never invoke the
// partial collector, even if the payload carried a LoanID — Cancel simply has
// nothing loan-related recorded on the run.
func TestCancelPlainSubmitDoesNotInvokePartialCollector(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	fs := &fakePartialStager{fakeStager: &fakeStager{}}
	nb.SetStager(fs)

	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "sleep 30"},
		Env:           map[string]string{"MOD_TASK_ID": "42"},
	}
	h, err := nb.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	// Wait until running (the process has started and the run is registered).
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		st, perr := nb.Poll(context.Background(), h)
		if perr != nil {
			t.Fatalf("Poll: %v", perr)
		}
		if st.Phase == TaskRunning {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err := nb.Cancel(context.Background(), h); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	fs.mu.Lock()
	n := fs.partialN
	fs.mu.Unlock()
	if n != 0 {
		t.Fatalf("a plain-Submit kill must NOT invoke the PartialCollector, called %d times", n)
	}
}

func TestCancelLoanTaskPartialCollectorErrorNotPropagated(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	fs := &fakePartialStager{fakeStager: &fakeStager{}, partialErr: errors.New("s3 boom")}
	nb.SetStager(fs)

	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: "913"}}
	h := submitLoanTask(t, scratch, nb, task, &transport.Credentials{})

	// The collector errors — Cancel must still return nil (best-effort, never
	// fatal) and the kill still happened.
	if err := nb.Cancel(context.Background(), h); err != nil {
		t.Fatalf("a partial-collector error must not propagate through Cancel: %v", err)
	}
	fs.mu.Lock()
	n := fs.partialN
	fs.mu.Unlock()
	if n == 0 {
		t.Fatal("expected the partial collector to have been invoked")
	}
}
