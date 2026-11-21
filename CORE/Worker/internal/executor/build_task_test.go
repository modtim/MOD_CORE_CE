package executor

// sz-build-queue.1 — routing tests: a task_type "build" payload must reach
// handleBuildTask (via Executor.Run) and fail loudly, never silently
// succeed and never fall through to the generic container-processing path.

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestHandleBuildTask_ReturnsNotImplementedError(t *testing.T) {
	e := &Executor{}
	task := &transport.Task{
		ID:      42,
		Payload: transport.TaskPayload{TaskType: "build"},
	}

	res, err := e.handleBuildTask(context.Background(), task, nil, time.Now(), Callbacks{})

	if err == nil {
		t.Fatal("expected a non-nil error from an unimplemented build handler, got nil")
	}
	if res != nil {
		t.Fatalf("expected a nil *Result on failure, got %+v", res)
	}
	if !strings.Contains(err.Error(), buildHandlerNotImplementedMsg) {
		t.Fatalf("expected error to mention %q, got %q", buildHandlerNotImplementedMsg, err.Error())
	}
	if !strings.Contains(err.Error(), "42") {
		t.Fatalf("expected error to mention the task id, got %q", err.Error())
	}
}

func TestBuildTaskRouting_RunDispatchesOnTaskType(t *testing.T) {
	e := &Executor{}
	task := &transport.Task{
		ID:      7,
		Payload: transport.TaskPayload{TaskType: "build"},
	}

	// Run() must route to handleBuildTask BEFORE it touches anything that
	// needs real credentials or a docker daemon (creds/ProcessingImage are
	// both empty here, so any path other than the build stub would fail
	// differently, e.g. on empty harbor credentials, or hang).
	_, err := e.Run(context.Background(), task, &transport.Credentials{}, Callbacks{})

	if err == nil {
		t.Fatal("expected Run() to fail a build-type task via the stub handler")
	}
	if !strings.Contains(err.Error(), buildHandlerNotImplementedMsg) {
		t.Fatalf("expected Run() to route task_type=build to handleBuildTask, got error %q", err.Error())
	}
}

func TestBuildTaskRouting_NonBuildTaskTypeUnaffected(t *testing.T) {
	e := &Executor{}
	task := &transport.Task{
		ID:      8,
		Payload: transport.TaskPayload{TaskType: ""},
	}

	// An ordinary (non-build) task must NOT be routed into the build stub —
	// it should proceed into the normal path and fail for an UNRELATED
	// reason (empty harbor credentials), never with the build message.
	_, err := e.Run(context.Background(), task, &transport.Credentials{}, Callbacks{})

	if err == nil {
		t.Fatal("expected Run() to fail a bare Executor{} task (empty harbor credentials)")
	}
	if strings.Contains(err.Error(), buildHandlerNotImplementedMsg) {
		t.Fatalf("non-build task_type must not route to the build stub, got %q", err.Error())
	}
}
