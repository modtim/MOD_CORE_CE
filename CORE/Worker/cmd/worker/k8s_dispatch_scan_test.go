package main

// Fortress 3.5 (968b) wiring tests for runK8sTask: the scan-posture decision
// (UploadCmd through K8sStagingUploadCmd), the scanned collect seam, and the
// retryability split between a scan VERDICT (retryable=false) and any other
// collect error (retryable=true). "Off / unset seam" must stay byte-identical
// to today's direct-to-durable upload.

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/outbox"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

type scanReportRecorder struct {
	completed bool
	failed    bool
	retry     bool
	msg       string
}

func (r *scanReportRecorder) ReportComplete(ctx context.Context, taskID int, result transport.TaskResult) error {
	r.completed = true
	return nil
}

func (r *scanReportRecorder) ReportFailed(ctx context.Context, taskID int, msg string, shouldRetry bool,
	logTail string, oomKilled bool, errorDetail string) error {
	r.failed = true
	r.retry = shouldRetry
	r.msg = msg
	return nil
}

// scanTestExecutor builds the Executor the dispatch path reads. executor.New
// needs a reachable Docker daemon, so the zero value + SetTunables is used: the
// 968b wiring only reads the tunables posture, nothing Docker-backed.
func scanTestExecutor(t *testing.T, mode string) *executor.Executor {
	t.Helper()
	store := tunables.New()
	store.Apply(map[string]interface{}{"fortress.task_output_scan_mode": mode})
	exec := &executor.Executor{}
	exec.SetTunables(store)
	return exec
}

func scanTestTask() *transport.Task {
	return &transport.Task{ID: 9001, TenantID: 7, Payload: transport.TaskPayload{
		NodeKind:        "image_output",
		ProcessingImage: "harbor.example/nodes/blur:1",
		MinioBucket:     "tenant-7-files",
	}}
}

func scanTestCreds() *transport.Credentials {
	return &transport.Credentials{Harbor: transport.HarborCredentials{Registry: "harbor.example"}}
}

// scanFakeBackend is a K8sBackend over a fake clientset whose Poll answers
// "JobComplete" on the first poll, so runK8sTask reaches the collect branch.
// The captured job name lets the test read back the stage-out command.
func scanFakeBackend(t *testing.T) (*executor.K8sBackend, *fake.Clientset, *string) {
	t.Helper()
	t.Setenv("MOD_INACTIVITY_LIVENESS", "0") // hard cap, no watchdog race
	cs := fake.NewSimpleClientset()
	b := executor.NewK8sBackend(cs, "tenant-7")
	captured := ""
	gvr := schema.GroupVersionResource{Group: "batch", Version: "v1", Resource: "jobs"}

	cs.PrependReactor("create", "jobs", func(a k8stesting.Action) (bool, runtime.Object, error) {
		if ca, ok := a.(k8stesting.CreateAction); ok {
			if j, ok := ca.GetObject().(*batchv1.Job); ok {
				captured = j.Name
			}
		}
		return false, nil, nil
	})
	cs.PrependReactor("get", "jobs", func(a k8stesting.Action) (bool, runtime.Object, error) {
		ga, ok := a.(k8stesting.GetAction)
		if !ok {
			return false, nil, nil
		}
		obj, err := cs.Tracker().Get(gvr, ga.GetNamespace(), ga.GetName())
		if err != nil {
			return false, nil, nil
		}
		j, ok := obj.(*batchv1.Job)
		if !ok {
			return false, nil, nil
		}
		j.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}
		return true, j, nil
	})
	return b, cs, &captured
}

// stageOutCommand returns the stage-out container's shell command as the Job
// was actually submitted.
func stageOutCommand(t *testing.T, cs *fake.Clientset, jobName string) string {
	t.Helper()
	gvr := schema.GroupVersionResource{Group: "batch", Version: "v1", Resource: "jobs"}
	obj, err := cs.Tracker().Get(gvr, "tenant-7", jobName)
	if err != nil {
		t.Fatalf("stage-out job not recorded: %v", err)
	}
	job := obj.(*batchv1.Job)
	for _, c := range job.Spec.Template.Spec.Containers {
		if c.Name == "stage-out" {
			return strings.Join(c.Command, " ")
		}
	}
	t.Fatal("no stage-out container on the submitted Job")
	return ""
}

// runScanDispatch drives runK8sTask once with the collect seam returning err.
func runScanDispatch(t *testing.T, exec *executor.Executor, err error) (*scanReportRecorder, string) {
	t.Helper()

	origBackend, origReporter, origCollect := k8sTaskBackend, reporter, k8sCollectStaged
	var origConsult func(ctx context.Context, task *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string)
	b, cs, jobName := scanFakeBackend(t)
	k8sTaskBackend = b
	rec := &scanReportRecorder{}
	reporter = outbox.NewReporter(mustStore(t), rec, slog.New(slog.DiscardHandler), 0)
	k8sCollectStaged = func(ctx context.Context, kb *executor.K8sBackend, h *executor.Handle,
		task *transport.Task, creds *transport.Credentials, e *executor.Executor) ([]transport.OutputFile, error) {
		return nil, err
	}
	defer func() {
		k8sTaskBackend, reporter, k8sCollectStaged = origBackend, origReporter, origCollect
		executor.SetK8sScanConsult(origConsult)
	}()

	runK8sTask(context.Background(), slog.New(slog.DiscardHandler), exec, scanTestTask(), scanTestCreds())
	return rec, stageOutCommand(t, cs, *jobName)
}

func mustStore(t *testing.T) *outbox.Store {
	t.Helper()
	s, err := outbox.New(t.TempDir())
	if err != nil {
		t.Fatalf("outbox store: %v", err)
	}
	return s
}

func TestK8sDispatchScanBlockedIsNotRetryable(t *testing.T) {
	exec := scanTestExecutor(t, "enforce")
	executor.SetK8sScanConsult(func(ctx context.Context, task *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		return true, k8sOutputScanBlockedReason
	})

	rec, cmd := runScanDispatch(t, exec, errors.New("output_scan_blocked: hash mismatch"))
	if !rec.failed || rec.retry {
		t.Fatalf("scan verdict must report retryable=false, got retry=%v msg=%q", rec.retry, rec.msg)
	}
	if !strings.Contains(rec.msg, k8sOutputScanBlockedReason) {
		t.Fatalf("failure message must carry the scan reason, got %q", rec.msg)
	}
	// Active posture: the stage-out upload targets the transient scan prefix.
	if !strings.Contains(cmd, "scan-staging/9001/") {
		t.Fatalf("active posture must upload to the transient prefix, got %q", cmd)
	}
}

func TestK8sDispatchOtherCollectErrorIsRetryable(t *testing.T) {
	exec := scanTestExecutor(t, "enforce")
	executor.SetK8sScanConsult(func(ctx context.Context, task *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		return false, ""
	})

	rec, _ := runScanDispatch(t, exec, errors.New("stage-out manifest unreadable"))
	if !rec.failed || !rec.retry {
		t.Fatalf("non-scan collect error must stay retryable, got retry=%v msg=%q", rec.retry, rec.msg)
	}
}

func TestK8sDispatchScanOffLeavesUploadCmdUntouched(t *testing.T) {
	exec := scanTestExecutor(t, "off")
	executor.SetK8sScanConsult(func(ctx context.Context, task *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		return true, k8sOutputScanBlockedReason
	})

	if executor.K8sScanStagingActive(exec) {
		t.Fatal("off posture must not activate scan staging")
	}
	rec, cmd := runScanDispatch(t, exec, nil)
	if rec.completed && !strings.Contains(cmd, "tenant-7-files") {
		t.Fatalf("off posture must upload direct-to-durable, got %q", cmd)
	}
	if strings.Contains(cmd, "scan-staging/") {
		t.Fatalf("off posture must never use the transient prefix, got %q", cmd)
	}
}
