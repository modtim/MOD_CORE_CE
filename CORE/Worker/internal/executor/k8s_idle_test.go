package executor

import (
	"context"
	"errors"
	"io"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) Now() time.Time      { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *fakeClock) Add(d time.Duration) { c.mu.Lock(); c.t = c.t.Add(d); c.mu.Unlock() }

func TestIdleWatchdogNotStartedBeforeRunning(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1000, 0)}
	var running atomic.Bool
	w := &IdleWatchdog{Window: time.Minute, Now: clk.Now,
		Started: func(context.Context) bool { return running.Load() }}
	ctx := context.Background()
	// Stage-in / image pull: hours pass, never idle.
	for i := 0; i < 5; i++ {
		clk.Add(time.Hour)
		if w.check(ctx, clk.Now()) {
			t.Fatal("idle fired before the task container was Running")
		}
	}
	running.Store(true)
	if w.check(ctx, clk.Now()) { // starts the clock now
		t.Fatal("fired on the start tick")
	}
	clk.Add(59 * time.Second)
	if w.check(ctx, clk.Now()) {
		t.Fatal("fired within window")
	}
	clk.Add(2 * time.Second)
	if !w.check(ctx, clk.Now()) {
		t.Fatal("did not fire after window")
	}
}

func TestIdleWatchdogActivityResets(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1000, 0)}
	w := &IdleWatchdog{Window: time.Minute, Now: clk.Now}
	ctx := context.Background()
	clk.Add(time.Hour)
	if w.check(ctx, clk.Now()) {
		t.Fatal("fired with no Started probe and no activity")
	}
	w.Touch() // first log line starts the clock
	for i := 0; i < 10; i++ {
		clk.Add(50 * time.Second)
		if w.check(ctx, clk.Now()) {
			t.Fatalf("fired despite activity (iter %d)", i)
		}
		w.Touch()
	}
	clk.Add(61 * time.Second)
	if !w.check(ctx, clk.Now()) {
		t.Fatal("did not fire after activity stopped")
	}
}

func TestIdleWatchdogDisabledWhenZero(t *testing.T) {
	w := &IdleWatchdog{Window: 0, Started: func(context.Context) bool { return true }}
	w.Touch()
	if w.check(context.Background(), time.Now().Add(1000*time.Hour)) {
		t.Fatal("disabled watchdog fired")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := w.Run(ctx); err != nil {
		t.Fatalf("disabled Run returned %v", err)
	}
}

// idleFakeBackend is Running forever until cancelled.
type idleFakeBackend struct {
	cancelled atomic.Int32
	done      atomic.Bool
}

func (f *idleFakeBackend) Submit(context.Context, *TaskSpec) (*Handle, error) {
	return &Handle{Backend: "k8s", ID: "ns/j"}, nil
}
func (f *idleFakeBackend) Poll(context.Context, *Handle) (*TaskStatus, error) {
	if f.done.Load() {
		return &TaskStatus{Phase: TaskSucceeded}, nil
	}
	return &TaskStatus{Phase: TaskRunning}, nil
}
func (f *idleFakeBackend) Logs(context.Context, *Handle) (io.ReadCloser, error) {
	return io.NopCloser(strings.NewReader("")), nil
}
func (f *idleFakeBackend) Cancel(context.Context, *Handle) error {
	f.cancelled.Add(1)
	return nil
}

func TestPollUntilDoneWithIdleFiresCancelsAndReportsDockerMessage(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1000, 0)}
	b := &idleFakeBackend{}
	w := &IdleWatchdog{Window: 30 * time.Minute, Now: clk.Now, Tick: time.Millisecond,
		Started: func(context.Context) bool { return true }}
	// Advance the fake clock past the window shortly after start.
	go func() {
		time.Sleep(5 * time.Millisecond)
		clk.Add(31 * time.Minute)
	}()
	_, err := PollUntilDoneWithIdle(context.Background(), b, &Handle{ID: "ns/j"}, time.Millisecond, w, time.Second, nil)
	var ik *IdleKillError
	if !errors.As(err, &ik) {
		t.Fatalf("want IdleKillError, got %v", err)
	}
	if b.cancelled.Load() != 1 {
		t.Fatalf("Cancel called %d times, want 1", b.cancelled.Load())
	}
	want := "run container: container killed: no progress (no logs, CPU/GPU, or output growth) for 30m0s"
	if err.Error() != want {
		t.Fatalf("message = %q, want %q", err.Error(), want)
	}
}

func TestPollUntilDoneWithIdleCompletesWithoutCancel(t *testing.T) {
	b := &idleFakeBackend{}
	b.done.Store(true)
	w := &IdleWatchdog{Window: time.Minute, Tick: time.Millisecond}
	u, err := PollUntilDoneWithIdle(context.Background(), b, &Handle{ID: "ns/j"}, time.Millisecond, w, time.Second, nil)
	if err != nil || u.Status != NodeRunCompleted {
		t.Fatalf("u=%+v err=%v", u, err)
	}
	if b.cancelled.Load() != 0 {
		t.Fatal("Cancel called on a completed task")
	}
}

func TestLogStreamOnActivityPerLine(t *testing.T) {
	var n atomic.Int32
	s := &LogStreamer{
		Open: func(context.Context, *time.Time) (io.ReadCloser, error) {
			return io.NopCloser(strings.NewReader(
				"2026-01-01T00:00:00.000000001Z hello\n2026-01-01T00:00:00.000000002Z\n2026-01-01T00:00:00.000000003Z world\n")), nil
		},
		Done:       func(context.Context) bool { return true },
		OnBatch:    func([]string) {},
		OnActivity: func() { n.Add(1) },
		Timestamps: true,
	}
	s.Run(context.Background())
	if n.Load() != 3 {
		t.Fatalf("activity count = %d, want 3 (incl. empty line)", n.Load())
	}
}

func TestK8sTaskLivenessMirrorsDocker(t *testing.T) {
	t.Setenv("MOD_INACTIVITY_LIVENESS", "")
	t.Setenv("MOD_CONTAINER_ABSOLUTE_MAX_S", "")
	if l := K8sTaskLiveness(transport.TaskPayload{}); l.IdleWindow != 30*time.Minute || l.HardCap != 0 {
		t.Fatalf("default: %+v", l)
	}
	if l := K8sTaskLiveness(transport.TaskPayload{ContainerTimeout: 90}); l.IdleWindow != 90*time.Second {
		t.Fatalf("timeout: %+v", l)
	}
	t.Setenv("MOD_CONTAINER_ABSOLUTE_MAX_S", "7200")
	if l := K8sTaskLiveness(transport.TaskPayload{}); l.HardCap != 2*time.Hour {
		t.Fatalf("abs cap: %+v", l)
	}
	if l := K8sTaskLiveness(transport.TaskPayload{Purpose: "wizard_test_run", SandboxTimeoutS: 60}); l.IdleWindow != 0 || l.HardCap != time.Minute {
		t.Fatalf("sandbox: %+v", l)
	}
	t.Setenv("MOD_INACTIVITY_LIVENESS", "0")
	if l := K8sTaskLiveness(transport.TaskPayload{ContainerTimeout: 120}); l.IdleWindow != 0 || l.HardCap != 2*time.Minute {
		t.Fatalf("legacy: %+v", l)
	}
}
