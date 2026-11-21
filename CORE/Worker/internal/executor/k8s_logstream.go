package executor

// k8s-reduction-engine W11: live log streaming for K8s task pods.
//
// Parity with the docker path (executor.go's ContainerLogs Follow loop): the
// `task` container's stdout/stderr is followed while the pod runs, batched
// (32 lines / 500ms) and handed to an OnLogBatch-shaped callback — the caller
// wires that to transport.SendLog + transport.ExtractCheckpoints exactly like
// the docker node path does. Streaming is strictly best-effort: errors are
// logged and retried, never surfaced as a task failure, and the final
// status/outputs path (PollUntilDone + Logs tail) is untouched.
//
// The streamer is built around an injectable opener so it is testable
// without a cluster (the fake clientset's GetLogs always returns "fake logs").

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	logStreamBatchLines = 32
	logStreamFlushEvery = 500 * time.Millisecond
	logStreamRetryDelay = time.Second
	logStreamMaxLine    = 1024 * 1024
)

// ErrLogStreamNotReady means the task container has not started yet (no pod,
// or the container is still Waiting); the streamer retries quietly.
var ErrLogStreamNotReady = errors.New("k8s log stream: task container not started yet")

// LogOpener opens a follow stream of the task container's logs. since is nil
// on the first open and the last-seen line time on a reconnect.
type LogOpener func(ctx context.Context, since *time.Time) (io.ReadCloser, error)

// LogStreamer follows a container's logs, reconnecting until Done reports
// the container terminated or ctx is cancelled.
type LogStreamer struct {
	Open LogOpener
	// Done reports whether the followed container has terminated (or is
	// gone). Consulted only after a stream ends, to decide reconnect vs stop.
	Done func(ctx context.Context) bool
	// OnBatch receives batches of non-empty lines (the docker path's
	// Callbacks.OnLogBatch shape). Required.
	OnBatch func(lines []string)
	// Timestamps: lines are prefixed with an RFC3339Nano timestamp (K8s
	// PodLogOptions.Timestamps). They are stripped and used to de-duplicate
	// the overlap a SinceTime reconnect replays.
	Timestamps bool
	Log        *slog.Logger
	// OnActivity (optional, W16) is called for every new (non-replayed) log
	// line, including empty ones — the idle watchdog's activity signal, like
	// the docker path bumping lastActivity per container log line.
	OnActivity func()

	BatchLines int
	FlushEvery time.Duration
	RetryDelay time.Duration

	batch   []string
	lastTS  time.Time
	dedupTS time.Time // after a reconnect, drop lines at/before this time
}

func (s *LogStreamer) logger() *slog.Logger {
	if s.Log != nil {
		return s.Log
	}
	return slog.Default()
}

// Run follows the logs until the container terminates or ctx is cancelled.
// It always flushes whatever it buffered before returning.
func (s *LogStreamer) Run(ctx context.Context) {
	if s.Open == nil || s.OnBatch == nil {
		return
	}
	if s.BatchLines <= 0 {
		s.BatchLines = logStreamBatchLines
	}
	if s.FlushEvery <= 0 {
		s.FlushEvery = logStreamFlushEvery
	}
	if s.RetryDelay <= 0 {
		s.RetryDelay = logStreamRetryDelay
	}
	defer s.flush()

	opened := false
	for ctx.Err() == nil {
		var since *time.Time
		if !s.lastTS.IsZero() {
			t := s.lastTS
			since = &t
		}
		rc, err := s.Open(ctx, since)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if opened && s.done(ctx) {
				return
			}
			if !errors.Is(err, ErrLogStreamNotReady) {
				s.logger().Debug("k8s log stream open failed; retrying", "error", err)
				// Container may have finished before we ever attached.
				if !opened && s.done(ctx) {
					return
				}
			}
			if !sleepCtx(ctx, s.RetryDelay) {
				return
			}
			continue
		}
		if opened {
			s.dedupTS = s.lastTS // reconnect: skip the SinceTime overlap
		}
		opened = true
		perr := s.pump(ctx, rc)
		if ctx.Err() != nil {
			return
		}
		if s.done(ctx) {
			return
		}
		s.logger().Debug("k8s log stream dropped before termination; reconnecting", "error", perr, "since", s.lastTS)
		if !sleepCtx(ctx, s.RetryDelay) {
			return
		}
	}
}

func (s *LogStreamer) done(ctx context.Context) bool {
	if s.Done == nil {
		return true
	}
	return s.Done(ctx)
}

// pump reads one stream to EOF (or ctx cancel), batching lines.
func (s *LogStreamer) pump(ctx context.Context, rc io.ReadCloser) error {
	lineCh := make(chan string, 256)
	errCh := make(chan error, 1)
	go func() {
		defer close(lineCh)
		sc := bufio.NewScanner(rc)
		sc.Buffer(make([]byte, 64*1024), logStreamMaxLine)
		for sc.Scan() {
			select {
			case lineCh <- sc.Text():
			case <-ctx.Done():
				errCh <- ctx.Err()
				return
			}
		}
		errCh <- sc.Err()
	}()

	ticker := time.NewTicker(s.FlushEvery)
	defer ticker.Stop()
	ctxDone := ctx.Done()
	for {
		select {
		case l, ok := <-lineCh:
			if !ok {
				_ = rc.Close()
				s.flush()
				return <-errCh
			}
			s.add(l)
		case <-ticker.C:
			s.flush()
		case <-ctxDone:
			_ = rc.Close() // unblock the scanner; keep draining until it exits
			ctxDone = nil
		}
	}
}

func (s *LogStreamer) add(raw string) {
	line := raw
	if s.Timestamps {
		if i := strings.IndexByte(raw, ' '); i > 0 {
			if ts, err := time.Parse(time.RFC3339Nano, raw[:i]); err == nil {
				if !s.dedupTS.IsZero() && !ts.After(s.dedupTS) {
					return // already shipped before the reconnect
				}
				s.dedupTS = time.Time{}
				s.lastTS = ts
				line = raw[i+1:]
			}
		} else if ts, err := time.Parse(time.RFC3339Nano, raw); err == nil {
			// timestamp-only line = an empty log line
			if s.dedupTS.IsZero() || ts.After(s.dedupTS) {
				s.dedupTS = time.Time{}
				s.lastTS = ts
				s.activity()
			}
			return
		}
	}
	s.activity()
	if line == "" {
		return
	}
	s.batch = append(s.batch, line)
	if len(s.batch) >= s.BatchLines {
		s.flush()
	}
}

func (s *LogStreamer) activity() {
	if s.OnActivity != nil {
		s.OnActivity()
	}
}

func (s *LogStreamer) flush() {
	if len(s.batch) == 0 {
		return
	}
	cp := make([]string, len(s.batch))
	copy(cp, s.batch)
	s.batch = s.batch[:0]
	s.OnBatch(cp)
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// --- K8sBackend wiring ------------------------------------------------------

// taskPod returns the Job's newest pod (backoffLimit=0 ⇒ normally one).
// Uses pods list only (already granted in the task ns).
func (b *K8sBackend) taskPod(ctx context.Context, h *Handle) (ns string, pod *corev1.Pod, err error) {
	ns, name, err := parseHandle(h)
	if err != nil {
		return "", nil, err
	}
	pods, err := b.jobPods(ctx, ns, name)
	if err != nil {
		return ns, nil, fmt.Errorf("k8s backend: list pods: %w", err)
	}
	if len(pods) == 0 {
		return ns, nil, nil
	}
	p := pods[0]
	for _, q := range pods[1:] {
		if q.CreationTimestamp.After(p.CreationTimestamp.Time) {
			p = q
		}
	}
	return ns, &p, nil
}

func taskContainerStatus(p *corev1.Pod) *corev1.ContainerStatus {
	if p == nil {
		return nil
	}
	for i := range p.Status.ContainerStatuses {
		if p.Status.ContainerStatuses[i].Name == k8sTaskContainerName {
			return &p.Status.ContainerStatuses[i]
		}
	}
	return nil
}

// openTaskLogs opens a Follow stream of the `task` container once it has
// started (Running or already Terminated); ErrLogStreamNotReady before that.
func (b *K8sBackend) openTaskLogs(ctx context.Context, h *Handle, since *time.Time) (io.ReadCloser, error) {
	ns, pod, err := b.taskPod(ctx, h)
	if err != nil {
		return nil, err
	}
	cs := taskContainerStatus(pod)
	if cs == nil || (cs.State.Running == nil && cs.State.Terminated == nil) {
		if pod != nil && (pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed) {
			return nil, fmt.Errorf("k8s log stream: pod %s finished without a task container status", pod.Name)
		}
		return nil, ErrLogStreamNotReady
	}
	opts := &corev1.PodLogOptions{Container: k8sTaskContainerName, Follow: true, Timestamps: true}
	if since != nil {
		st := metav1.NewTime(*since)
		opts.SinceTime = &st
	}
	return b.Client.CoreV1().Pods(ns).GetLogs(pod.Name, opts).Stream(ctx)
}

// taskContainerDone reports whether the `task` container has terminated, or
// the pod/job is gone (cancelled / TTL-reaped). A list error is "not done"
// (the caller's ctx bounds the retry loop).
func (b *K8sBackend) taskContainerDone(ctx context.Context, h *Handle) bool {
	_, pod, err := b.taskPod(ctx, h)
	if err != nil {
		return false
	}
	if pod == nil {
		return true
	}
	if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
		return true
	}
	cs := taskContainerStatus(pod)
	return cs != nil && cs.State.Terminated != nil
}

// NewTaskLogStreamer returns a LogStreamer that follows the `task`
// container of the Job behind h, delivering batches to onBatch.
func (b *K8sBackend) NewTaskLogStreamer(h *Handle, onBatch func([]string), log *slog.Logger) *LogStreamer {
	return &LogStreamer{
		Open: func(ctx context.Context, since *time.Time) (io.ReadCloser, error) {
			return b.openTaskLogs(ctx, h, since)
		},
		Done:       func(ctx context.Context) bool { return b.taskContainerDone(ctx, h) },
		OnBatch:    onBatch,
		Timestamps: true,
		Log:        log,
	}
}
