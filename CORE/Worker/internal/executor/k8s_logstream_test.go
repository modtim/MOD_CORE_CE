package executor

import (
	"context"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type batchSink struct {
	mu    sync.Mutex
	lines []string
	n     int
}

func (s *batchSink) on(lines []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.n++
	s.lines = append(s.lines, lines...)
}

func (s *batchSink) got() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.lines...)
}

func runStreamer(t *testing.T, s *LogStreamer, timeout time.Duration) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()
	select {
	case <-done:
	case <-time.After(timeout + 2*time.Second):
		t.Fatal("LogStreamer.Run did not return")
	}
}

// Reconnect after a mid-run drop: SinceTime is the last-seen timestamp and
// the replayed overlap is de-duplicated; stops once Done reports terminated.
func TestLogStreamReconnectsWithSinceTimeAndDedups(t *testing.T) {
	t0 := time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC)
	ts := func(s int) string { return t0.Add(time.Duration(s) * time.Second).Format(time.RFC3339Nano) }
	streams := []string{
		ts(1) + " one\n" + ts(2) + " MOD_CHECKPOINT {\"step\":1}\n",
		// server replays from SinceTime (second granularity) → overlap
		ts(2) + " MOD_CHECKPOINT {\"step\":1}\n" + ts(3) + " three\n" + ts(4) + " \n",
	}
	var sinces []*time.Time
	opens := 0
	sink := &batchSink{}
	s := &LogStreamer{
		Open: func(_ context.Context, since *time.Time) (io.ReadCloser, error) {
			sinces = append(sinces, since)
			if opens >= len(streams) {
				return nil, errors.New("gone")
			}
			opens++
			return io.NopCloser(strings.NewReader(streams[opens-1])), nil
		},
		Done:       func(context.Context) bool { return opens >= len(streams) },
		OnBatch:    sink.on,
		Timestamps: true,
		RetryDelay: time.Millisecond,
	}
	runStreamer(t, s, 3*time.Second)

	want := []string{"one", `MOD_CHECKPOINT {"step":1}`, "three"}
	if got := sink.got(); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("lines=%q want %q", got, want)
	}
	if len(sinces) != 2 || sinces[0] != nil || sinces[1] == nil || !sinces[1].Equal(t0.Add(2*time.Second)) {
		t.Fatalf("since args=%v", sinces)
	}
}

// Not-ready opens are retried quietly until the container starts.
func TestLogStreamWaitsForContainerStart(t *testing.T) {
	calls := 0
	sink := &batchSink{}
	s := &LogStreamer{
		Open: func(context.Context, *time.Time) (io.ReadCloser, error) {
			calls++
			if calls < 3 {
				return nil, ErrLogStreamNotReady
			}
			return io.NopCloser(strings.NewReader("a\nb\n")), nil
		},
		Done:       func(context.Context) bool { return calls >= 3 },
		OnBatch:    sink.on,
		RetryDelay: time.Millisecond,
	}
	runStreamer(t, s, 3*time.Second)
	if got := sink.got(); strings.Join(got, ",") != "a,b" || calls != 3 {
		t.Fatalf("lines=%q calls=%d", got, calls)
	}
}

// ctx cancel stops a blocked follow promptly and flushes buffered lines.
func TestLogStreamCtxCancelStopsFollow(t *testing.T) {
	pr, pw := io.Pipe()
	sink := &batchSink{}
	s := &LogStreamer{
		Open: func(context.Context, *time.Time) (io.ReadCloser, error) {
			return pr, nil
		},
		Done:       func(context.Context) bool { return false },
		OnBatch:    sink.on,
		FlushEvery: time.Hour, // only the final flush can deliver
		RetryDelay: time.Millisecond,
	}
	go func() { _, _ = pw.Write([]byte("live line\n")) }()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()
	time.Sleep(100 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not stop on ctx cancel")
	}
	if got := sink.got(); len(got) != 1 || got[0] != "live line" {
		t.Fatalf("lines=%q", got)
	}
}

// Size-triggered batching (docker path: 32 lines).
func TestLogStreamBatchesBySize(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 70; i++ {
		b.WriteString("x\n")
	}
	sink := &batchSink{}
	s := &LogStreamer{
		Open: func(context.Context, *time.Time) (io.ReadCloser, error) {
			return io.NopCloser(strings.NewReader(b.String())), nil
		},
		Done:    func(context.Context) bool { return true },
		OnBatch: sink.on,
	}
	runStreamer(t, s, 3*time.Second)
	if len(sink.got()) != 70 || sink.n != 3 {
		t.Fatalf("lines=%d batches=%d", len(sink.got()), sink.n)
	}
}

// K8sBackend wiring against the fake clientset (GetLogs → "fake logs").
func TestK8sTaskLogStreamerFakeClient(t *testing.T) {
	b, cs := newTestK8s(t)
	h, job := submitK8s(t, b, &TaskSpec{Image: "x"})
	ctx := context.Background()

	// No pod yet: the open path reports NotReady (quiet retry).
	if _, err := b.openTaskLogs(ctx, h, nil); !errors.Is(err, ErrLogStreamNotReady) {
		t.Fatalf("no pod: err=%v want NotReady", err)
	}

	addPod(t, cs, job.Namespace, job.Name, time.Now(), corev1.PodStatus{
		Phase: corev1.PodPending,
		ContainerStatuses: []corev1.ContainerStatus{{Name: k8sTaskContainerName,
			State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ContainerCreating"}}}},
	})
	if _, err := b.openTaskLogs(ctx, h, nil); !errors.Is(err, ErrLogStreamNotReady) {
		t.Fatalf("waiting: err=%v want NotReady", err)
	}
	if b.taskContainerDone(ctx, h) {
		t.Fatal("waiting container reported done")
	}

	pod, _ := cs.CoreV1().Pods(job.Namespace).Get(ctx, job.Name+"-pod", metav1.GetOptions{})
	pod.Status.Phase = corev1.PodSucceeded
	pod.Status.ContainerStatuses[0].State = corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: 0}}
	if _, err := cs.CoreV1().Pods(job.Namespace).UpdateStatus(ctx, pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if !b.taskContainerDone(ctx, h) {
		t.Fatal("terminated container not done")
	}

	sink := &batchSink{}
	s := b.NewTaskLogStreamer(h, sink.on, nil)
	s.RetryDelay = time.Millisecond
	runStreamer(t, s, 3*time.Second)
	if got := sink.got(); len(got) != 1 || got[0] != "fake logs" {
		t.Fatalf("lines=%q want [fake logs]", got)
	}
}
