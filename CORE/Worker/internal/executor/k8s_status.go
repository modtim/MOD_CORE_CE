package executor

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"time"
)

// NodeRun status values the worker reports to the API. They correspond to
// the worker transport endpoints (/workers/tasks/{id}/started|complete|failed)
// and the API's lowercase TaskStatus values.
const (
	NodeRunRunning   = "running"
	NodeRunCompleted = "completed"
	NodeRunFailed    = "failed"
)

// Failure reason classes surfaced from K8s Job/Pod state.
const (
	ReasonOOMKilled        = "OOMKilled"
	ReasonImagePull        = "ImagePullBackOff"
	ReasonUnschedulable    = "Unschedulable"
	ReasonDeadlineExceeded = "DeadlineExceeded"
	ReasonJobNotFound      = "JobNotFound"
	ReasonContainerExit    = "ContainerExited"
	ReasonBackoffLimit     = "BackoffLimitExceeded"
	ReasonEvicted          = "Evicted"
	ReasonConfigError      = "CreateContainerConfigError"
	ReasonStageInFailed    = "StageInFailed"  // W3: S3 input download (stage-in initContainer)
	ReasonStageOutFailed   = "StageOutFailed" // W3: S3 output upload / manifest (stage-out)
)

// NodeRunUpdate is a TaskStatus translated into the shape the worker's
// reporter/transport expects (ReportStarted / ReportComplete /
// ReportFailed(errMsg, shouldRetry, logTail, oomKilled, errorDetail)).
type NodeRunUpdate struct {
	Status       string // NodeRunRunning | NodeRunCompleted | NodeRunFailed
	ExitCode     int
	Reason       string // one of the Reason* classes when failed ("" if unknown)
	ErrorMessage string // human message incl. reason + exit code (failed only)
	ErrorDetail  string // raw backend error text
	OOMKilled    bool
	ShouldRetry  bool // transient infra failure (lost job, unschedulable, evicted)
}

// classifyReason maps backend error text to a reason class.
func classifyReason(errText string) string {
	s := strings.ToLower(errText)
	switch {
	// Stage markers first: a stage container's own OOM/exit is a staging failure.
	case strings.Contains(s, "stageinfailed"):
		return ReasonStageInFailed
	case strings.Contains(s, "stageoutfailed"):
		return ReasonStageOutFailed
	case strings.Contains(s, "oomkilled"):
		return ReasonOOMKilled
	case strings.Contains(s, "imagepullbackoff"), strings.Contains(s, "errimagepull"),
		strings.Contains(s, "invalidimagename"):
		return ReasonImagePull
	case strings.Contains(s, "unschedulable"):
		return ReasonUnschedulable
	case strings.Contains(s, "deadlineexceeded"):
		return ReasonDeadlineExceeded
	case strings.Contains(s, "job not found"):
		return ReasonJobNotFound
	case strings.Contains(s, "evicted"):
		return ReasonEvicted
	case strings.Contains(s, "createcontainerconfigerror"), strings.Contains(s, "createcontainererror"):
		return ReasonConfigError
	case strings.Contains(s, "backofflimitexceeded"):
		return ReasonBackoffLimit
	case strings.Contains(s, "container exited"):
		return ReasonContainerExit
	}
	return ""
}

// MapTaskStatus translates a Backend TaskStatus (e.g. from K8sBackend.Poll)
// into the NodeRun status the worker reports. Pure; nil ⇒ running.
func MapTaskStatus(st *TaskStatus) NodeRunUpdate {
	if st == nil {
		return NodeRunUpdate{Status: NodeRunRunning}
	}
	switch st.Phase {
	case TaskSucceeded:
		return NodeRunUpdate{Status: NodeRunCompleted, ExitCode: st.ExitCode}
	case TaskFailed:
		reason := classifyReason(st.Err)
		u := NodeRunUpdate{
			Status:      NodeRunFailed,
			ExitCode:    st.ExitCode,
			Reason:      reason,
			ErrorDetail: st.Err,
			OOMKilled:   reason == ReasonOOMKilled || (st.ExitCode == 137 && reason == ""),
		}
		switch reason {
		case ReasonJobNotFound, ReasonUnschedulable, ReasonEvicted,
			ReasonStageInFailed, ReasonStageOutFailed: // S3 blips; Docker retries these too
			u.ShouldRetry = true
		}
		u.ErrorMessage = failureMessage(reason, st.ExitCode, st.Err)
		return u
	default:
		return NodeRunUpdate{Status: NodeRunRunning}
	}
}

func failureMessage(reason string, exitCode int, detail string) string {
	var b strings.Builder
	b.WriteString("k8s task failed")
	if reason != "" {
		b.WriteString(" [" + reason + "]")
	}
	if exitCode >= 0 {
		b.WriteString(" (exit code ")
		b.WriteString(strconv.Itoa(exitCode))
		b.WriteString(")")
	}
	if detail != "" {
		b.WriteString(": ")
		b.WriteString(detail)
	}
	return b.String()
}

// WithTaskID returns spec with MOD_TASK_ID set in Env (the key K8sBackend
// derives the Job name/labels from). The Env map is copied so a caller's
// shared map is never mutated; a nil spec yields a new spec.
func WithTaskID(spec *TaskSpec, id string) *TaskSpec {
	out := &TaskSpec{}
	if spec != nil {
		cp := *spec
		out = &cp
	}
	env := make(map[string]string, len(out.Env)+1)
	for k, v := range out.Env {
		env[k] = v
	}
	env["MOD_TASK_ID"] = id
	out.Env = env
	return out
}

// PollUntilDone polls b for h every interval, calling onUpdate only when the
// mapped NodeRun status (status + reason + exit code) changes, and returns the
// terminal update (completed/failed). Transient Poll errors are retried until
// ctx ends; on ctx cancellation it returns ctx.Err() with the last update.
func PollUntilDone(ctx context.Context, b Backend, h *Handle, interval time.Duration, onUpdate func(NodeRunUpdate)) (NodeRunUpdate, error) {
	if b == nil || h == nil {
		return NodeRunUpdate{}, errors.New("PollUntilDone: nil backend or handle")
	}
	if interval <= 0 {
		interval = 2 * time.Second
	}
	var last NodeRunUpdate
	first := true
	for {
		st, err := b.Poll(ctx, h)
		if err == nil {
			u := MapTaskStatus(st)
			if first || u.Status != last.Status || u.Reason != last.Reason || u.ExitCode != last.ExitCode {
				first = false
				last = u
				if onUpdate != nil {
					onUpdate(u)
				}
			}
			if u.Status != NodeRunRunning {
				return u, nil
			}
		}
		t := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			t.Stop()
			return last, ctx.Err()
		case <-t.C:
		}
	}
}
