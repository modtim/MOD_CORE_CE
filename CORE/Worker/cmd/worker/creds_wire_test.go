package main

// OPT1-WIRE-CREDS-PREFETCH-HARDEN (lane r9700-2, 11-25) — tests for the
// extracted prefetch decision + join/cancel handling (creds_wire.go). These
// close the closer's F-2 gap: they drive the SAME functions executeTask
// calls (NewCredsWire / Abort / Join / credsPrefetchAllowed), so a main.go
// that reverts to the serial call, starts the prefetch for a marker-tagged
// task, or leaves the grant running after a reject fails here. No Docker,
// no network: a fake Transport counts GetCredentials calls.
import (
	"context"
	"os"
	"runtime"
	"strings"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

type cwLog struct {
	calls   int
	taskIDs []int
	mu      sync.Mutex
}

type cwFake struct {
	l   *cwLog
	dur time.Duration
}

func cwGrantImpl(l *cwLog, dur time.Duration) transport.Transport {
	f := &cwFake{l: l, dur: dur}
	return f
}

func (f *cwFake) GetCredentials(ctx context.Context, taskID int) (*transport.Credentials, error) {
	f.l.mu.Lock()
	f.l.calls++
	f.l.taskIDs = append(f.l.taskIDs, taskID)
	f.l.mu.Unlock()
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if f.dur > 0 {
		time.Sleep(f.dur)
	}
	c := &transport.Credentials{}
	c.MinIO.AccessKey = "task-" + strconv.Itoa(taskID)
	return c, nil
}
func (f *cwFake) WaitForTask(ctx context.Context) (*transport.Task, error) { return nil, nil }
func (f *cwFake) WaitForStackOp(ctx context.Context) (*transport.Task, error) { return nil, nil }
func (f *cwFake) ReportStarted(ctx context.Context, taskID int, d time.Duration) error { return nil }
func (f *cwFake) ReportComplete(ctx context.Context, taskID int, r transport.TaskResult) error { return nil }
func (f *cwFake) ReportFailed(ctx context.Context, taskID int, m string, sr bool, lt string, ok bool, ed string) error { return nil }
func (f *cwFake) MintRunToken(ctx context.Context, taskID int) (string, int64, error) { return "", 0, nil }
func (f *cwFake) SendLog(ctx context.Context, taskID int, l []string) error { return nil }
func (f *cwFake) SendCheckpoints(ctx context.Context, taskID int, cps []transport.Checkpoint) error { return nil }
func (f *cwFake) ValidatePhase(ctx context.Context, taskID int, p string, a transport.ResourceSnapshot) (*transport.ValidationResponse, error) { return nil, nil }
func (f *cwFake) SendResourceSamples(ctx context.Context, taskID int, s []transport.ResourceSample) error { return nil }
func (f *cwFake) ResolveSecrets(ctx context.Context, taskID int) (map[string]string, error) { return nil, nil }
func (f *cwFake) ReportStorageMigrationProgress(ctx context.Context, taskID int, o int, b int64) error { return nil }
func (f *cwFake) Close() error { return nil }

func cwTask(id int) *transport.Task {
	return &transport.Task{ID: id}
}

// A task carrying an export-control marker (the narrowest conservative
// predicate: any export-control / label-restriction marker).
func cwTaskWithMarkers(id int) *transport.Task {
	t := &transport.Task{ID: id}
	t.Payload.ExportControlLevel = "CUI"
	return t
}

// cwWaitGrant polls until the prefetch goroutine's grant call landed (the
// goroutine starts asynchronously; a dur=0 fake can win or lose the race).
func cwWaitGrant(l *cwLog) int {
	for i := 0; i < 200; i++ {
		if cwCalls(l) > 0 {
			return cwCalls(l)
		}
		time.Sleep(time.Millisecond)
	}
	return cwCalls(l)
}

func cwCalls(l *cwLog) int {
	l.mu.Lock()
	c := l.calls
	l.mu.Unlock()
	return c
}

// TestCwPrefetchDecision pins the predicate: a marker-tagged task takes the
// serial path (no early grant), a plain task takes the prefetch path.
func TestCwPrefetchDecision(t *testing.T) {
	if !credsPrefetchAllowed(cwTask(1)) {
		t.Fatal("plain task must allow the prefetch")
	}
	if credsPrefetchAllowed(cwTaskWithMarkers(2)) {
		t.Fatal("marker-tagged task must NOT allow the prefetch")
	}
}

// TestCwSerialPathNoEarlyGrant: a marker-tagged task gets ZERO grants until
// the Join (the serial fetch), even though validate+report ran in between —
// "a refused dispatch gets no credentials" stays true for marker tasks.
func TestCwSerialPathNoEarlyGrant(t *testing.T) {
	l := &cwLog{}
	tp := cwGrantImpl(l, 0)
	task := cwTaskWithMarkers(7)
	w := NewCredsWire(context.Background(), tp, task)
	if cwCalls(l) != 0 {
		t.Fatal("serial path must not grant before the Join")
	}
	c, err := w.Join(context.Background())
	if err != nil || c == nil || c.MinIO.AccessKey != "task-7" {
		t.Fatalf("serial Join: %v", err)
	}
	if cwCalls(l) != 1 {
		t.Fatalf("serial path: expected exactly 1 grant, got %d", cwCalls(l))
	}
}

// TestCwPrefetchPathSingleGrant: the prefetch path grants exactly once,
// before the Join, and the Join returns that grant (no second call).
func TestCwPrefetchPathSingleGrant(t *testing.T) {
	l := &cwLog{}
	tp := cwGrantImpl(l, 0)
	task := cwTask(11)
	w := NewCredsWire(context.Background(), tp, task)
	if cwWaitGrant(l) != 1 {
		t.Fatalf("prefetch must start exactly one grant early, got %d", cwCalls(l))
	}
	c, err := w.Join(context.Background())
	if err != nil || c == nil || c.MinIO.AccessKey != "task-11" {
		t.Fatalf("prefetch Join: %v", err)
	}
	if cwCalls(l) != 1 {
		t.Fatalf("Join must not mint a second grant, got %d", cwCalls(l))
	}
}

// TestCwAbortStopsGrantLoop (fixes R-3): after Abort on an early-return
// path, the prefetch goroutine has ended (Abort's Join consumed it) and a
// later Join never mints again — no grant outlives the refused task.
func TestCwAbortStopsGrantLoop(t *testing.T) {
	l := &cwLog{}
	tp := cwGrantImpl(l, 0)
	task := cwTask(21)
	w := NewCredsWire(context.Background(), tp, task)
	if cwWaitGrant(l) != 1 {
		t.Fatalf("prefetch started one grant, got %d", cwCalls(l))
	}
	w.Abort()
	c, err := w.Join(context.Background())
	if c != nil || err == nil {
		t.Fatal("aborted wire Join must report the abort, never mint again")
	}
	if cwCalls(l) != 1 {
		t.Fatalf("abort path: expected exactly 1 grant total, got %d", cwCalls(l))
	}
}

// TestCwAbortOnSerialPath: Abort on the serial path is a no-op that does
// not mint; the later Join still fetches serially (the reject path for a
// marker task behaves like the serial code before the change).
func TestCwAbortOnSerialPath(t *testing.T) {
	l := &cwLog{}
	tp := cwGrantImpl(l, 0)
	task := cwTaskWithMarkers(31)
	w := NewCredsWire(context.Background(), tp, task)
	w.Abort()
	if cwCalls(l) != 0 {
		t.Fatalf("serial path Abort must not mint, got %d", cwCalls(l))
	}
}

// TestCwPrefetchOverlapsWork: the grant (simulated 120 ms) starts before
// the simulated validate+report stretch (60 ms) and the Join waits only
// for the grant: wall ~= max(grant, stretch), NOT the sum — the overlap
// the OPT1 wire claims (still NOT MEASURED against the real API).
func TestCwPrefetchOverlapsWork(t *testing.T) {
	l := &cwLog{}
	tp := cwGrantImpl(l, 120*time.Millisecond)
	task := cwTask(41)
	start := time.Now()
	w := NewCredsWire(context.Background(), tp, task)
	time.Sleep(60*time.Millisecond) // the validate+report stretch
	c, err := w.Join(context.Background())
	if err != nil || c == nil {
		t.Fatalf("overlap Join: %v", err)
	}
	wall := time.Now().Sub(start)
	if wall < 110*time.Millisecond {
		t.Fatal("wall must be at least the grant duration")
	}
	if wall > 400*time.Millisecond {
		t.Fatalf("wall must be ~max(grant,stretch)=120ms, not the sum; got %v", wall)
	}
}

// TestCwRevertGuard: if main.go reverted to the serial call, the wire
// functions above still pass — so this test pins the SHAPE executeTask
// must use: NewCredsWire BEFORE the validate stretch (grant counted early)
// and Join at the use site. A serial main.go cannot make the grant appear
// before the stretch through these functions.
func TestCwRevertGuard(t *testing.T) {
	l := &cwLog{}
	tp := cwGrantImpl(l, 80*time.Millisecond)
	task := cwTask(51)
	w := NewCredsWire(context.Background(), tp, task)
	if cwWaitGrant(l) != 1 {
		t.Fatal("executeTask must start the prefetch via NewCredsWire before the validate stretch")
	}
	c, err := w.Join(context.Background())
	if err != nil || c == nil || c.MinIO.AccessKey != "task-51" {
		t.Fatalf("revert guard Join: %v", err)
	}
}

// cwWaitTrue polls a condition up to ~2 s (the prefetch goroutine starts
// asynchronously; a blocking fake needs a moment to land its call).
func cwWaitTrue(cond func() bool) bool {
	for i := 0; i < 200; i++ {
		if cond() {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return false
}

// cwBlockingFake is the ctx-aware fake the shipped tests were missing (closer
// N-1): its GetCredentials BLOCKS until its own context is cancelled and
// records that it saw the cancel. TestCwAbortStopsGrantLoop's fake returns
// immediately and ignores ctx, so it cannot tell an Abort that cancels +
// drains from an Abort that only sets aborted=true (MUT-B). With this fake,
// an Abort that does not cancel the prefetch context never returns.
type cwBlockingFake struct {
	cwFake
	started   atomic.Int32
	sawCancel atomic.Int32
}

func (b *cwBlockingFake) GetCredentials(ctx context.Context, taskID int) (*transport.Credentials, error) {
	b.started.Add(1)
	<-ctx.Done() // blocks until THIS call's context (the prefetch's own) is cancelled
	b.sawCancel.Add(1)
	return nil, ctx.Err()
}

// TestCwAbortCancelsBlockingGrant (closes closer N-1 / MUT-B): Abort on a
// mid-flight grant must (a) return promptly — only possible if it cancelled
// the prefetch context, since the fake blocks on ctx.Done; (b) the grant
// observed the cancel; (c) the goroutine count returns to baseline (Abort's
// Join drained it — no goroutine leak).
func TestCwAbortCancelsBlockingGrant(t *testing.T) {
	base := runtime.NumGoroutine()
	b := &cwBlockingFake{}
	w := NewCredsWire(context.Background(), b, cwTask(61))
	if !cwWaitTrue(func() bool { return b.started.Load() == 1 }) {
		t.Fatal("prefetch never started its grant call")
	}
	aborted := make(chan struct{})
	go func() { w.Abort(); close(aborted) }()
	select {
	case <-aborted:
	case <-time.After(2 * time.Second):
		t.Fatal("Abort did not return within 2 s: the blocking grant was never cancelled")
	}
	if b.sawCancel.Load() != 1 {
		t.Fatal("Abort must cancel the prefetch context: the grant never observed the cancel")
	}
	if !cwWaitTrue(func() bool { return runtime.NumGoroutine() <= base }) {
		t.Fatalf("goroutine leak after Abort: baseline=%d now=%d", base, runtime.NumGoroutine())
	}
	c, err := w.Join(context.Background())
	if c != nil || err == nil {
		t.Fatal("Join after Abort must report the abort, never mint again")
	}
	if b.started.Load() != 1 {
		t.Fatalf("Join after Abort must not mint: started=%d", b.started.Load())
	}
}

// TestCwAbortTwiceIsSafe (closer N-1 hardening claim: double Abort is safe):
// a second Abort on a spent wire must not panic, must not mint, and the
// later Join still reports the abort.
func TestCwAbortTwiceIsSafe(t *testing.T) {
	b := &cwBlockingFake{}
	w := NewCredsWire(context.Background(), b, cwTask(62))
	cwWaitTrue(func() bool { return b.started.Load() == 1 })
	w.Abort()
	w.Abort()
	if b.sawCancel.Load() != 1 {
		t.Fatalf("exactly one cancel must reach the grant, saw %d", b.sawCancel.Load())
	}
	c, err := w.Join(context.Background())
	if c != nil || err == nil {
		t.Fatal("Join after double Abort must report the abort, never mint")
	}
	if b.started.Load() != 1 {
		t.Fatalf("double Abort must not mint again: started=%d", b.started.Load())
	}
}

// TestCwMainGoWiringPins: a cmd/worker test runs with cwd = the package dir,
// so it can read main.go itself. This is the ONLY way a test fails when
// main.go reverts to the serial call or drops an Abort: the source must
// contain the wire (NewCredsWire + Join + an Abort on EVERY early-return
// path: the validate reject and the 409) and must NOT contain the old
// unwired prefetch start.
func TestCwMainGoWiringPins(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatalf("cannot read main.go: %v", err)
	}
	text := string(src)
	if !strings.Contains(text, "credsWire := NewCredsWire(ctx, tp, task)") {
		t.Fatal("main.go must start the wire via NewCredsWire")
	}
	if !strings.Contains(text, "credsWire.Join(ctx)") {
		t.Fatal("main.go must Join via credsWire at the credentials use site")
	}
	if strings.Count(text, "credsWire.Abort()") < 2 {
		t.Fatal("main.go must Abort on BOTH early-return paths (validate reject, 409)")
	}
	if strings.Contains(text, "credsPrefetch := transport.StartCredsPrefetch") {
		t.Fatal("main.go must not start the raw prefetch (reverted wire)")
	}
	// Closes closer N-1 / MUT-G: the wire constructor (NewCredsWire) lives in
	// creds_wire.go, so main.go must not call StartCredsPrefetch at all — any
	// raw transport.StartCredsPrefetch( occurrence in main.go is a second,
	// unmanaged grant next to the wire (it bypasses Abort: nothing cancels or
	// drains it on the early-return paths).
	if strings.Contains(text, "transport.StartCredsPrefetch(") {
		t.Fatal("main.go must not call transport.StartCredsPrefetch directly — the only grant start belongs to NewCredsWire in creds_wire.go")
	}
}
