package executor

// sz-build-queue.1 — the `build` task_type routing endpoint.
//
// Executor.Run dispatches here whenever task.Payload.TaskType == "build"
// (see executor.go). This is deliberately a STUB: the sandboxed BuildKit
// runner (sz-build-queue.4), the scoped Harbor push-token flow
// (sz-build-queue.5) and the audit events (sz-build-queue.6) all land in
// later jobs. Until then a build task must fail LOUDLY and immediately
// rather than silently falling through to the generic container-processing
// path (which would blow up confusingly deep inside image-pull, since a
// build task carries no ProcessingImage).

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// buildHandlerNotImplementedMsg is the exact failure string reported back to
// the API via /workers/tasks/{id}/failed — later jobs may grep logs/tests
// for this literal, so keep it stable.
const buildHandlerNotImplementedMsg = "build handler not implemented"

// handleBuildTask always returns a non-nil error: the caller (Executor.Run,
// and above it cmd/worker/main.go's task loop) treats any error return as a
// failed task and reports it via reporter.Failed — never a silent success,
// never a silent drop.
func (e *Executor) handleBuildTask(ctx context.Context, task *transport.Task, creds *transport.Credentials, started time.Time, cb Callbacks) (*Result, error) {
	taskID := 0
	recipeHint := ""
	if task != nil {
		taskID = task.ID
		recipeHint = task.Payload.NodeKind
	}

	logger := e.log
	if logger == nil {
		logger = slog.Default()
	}
	logger.Error(buildHandlerNotImplementedMsg,
		"task_id", taskID,
		"task_type", "build",
		"recipe_hint", recipeHint,
	)

	return nil, fmt.Errorf("%s (task_id=%d)", buildHandlerNotImplementedMsg, taskID)
}
