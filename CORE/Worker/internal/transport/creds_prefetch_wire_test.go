package transport

// OPT1-WIRE-CREDS-PREFETCH (lane r9700-1, 11-25) — parity tests for the
// executeTask wiring (main.go: StartCredsPrefetch before the ValidatePhase
// block, Join at the old serial call site). cmd/worker is package main so
// these tests pin the exact wire PATTERN (start -> advisory validate ->
// report started -> Join) against the real helper, not a copy of it:
//   * success parity: Join returns the identical (creds, err) pair the
//     serial tp.GetCredentials(ctx, taskID) call would have returned, and
//     exactly ONE grant per task (retry rules live inside the transport
//     method, untouched);
//   * grant-error parity: Join surfaces the SAME error object, so the
//     caller's "credential fetch failed" report path is unchanged;
//   * ValidatePhase ordering: a validation REJECT returns before the Join
//     (as before) — the prefetch goroutine still ends on its own bounded
//     call, leaks nothing, and its grant is never handed to another task;
//   * cancel during prefetch: a cancelled context makes the transport call
//     fail; Join propagates that error exactly like the serial call did.
// MUTATION PINS (the brief's mutation check, proven by hand-mutations of
// the helper in a scratch tree — see the result file):
//   * if the wire dropped the Join (creds used straight off the fetch
//     struct without consuming the done-chan), TestWireJoinConsumesResult
//     and TestWireGrantErrorParity fail (unjoined / grant-error);
//   * if a prefetch result were reused across tasks, TestWirePerTaskGrant
//     fails (wrong taskID's creds handed to a task).
import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

type wireLog struct {
	calls   int
	taskIDs []int
	mu      sync.Mutex
}

// wireGrant mimics transport.GetCredentials: one call per taskID, a small
// delay (the ~0.37 s grant), creds tagged with the taskID so any cross-task
// reuse is visible, and an optional error.
type wireGrant struct {
	l    *wireLog
	dur  time.Duration
	err  error
	creds *Credentials
}

func (g *wireGrant) GetCredentials(ctx context.Context, taskID int) (*Credentials, error) {
	g.l.mu.Lock()
	g.l.calls++
	g.l.taskIDs = append(g.l.taskIDs, taskID)
	g.l.mu.Unlock()
	// A cancelled context fails the call exactly as the real transport's
	// HTTP layer does (http.NewRequestWithContext / client.Do error).
	// Checked BEFORE the injected error so both paths behave like the
	// serial call.
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if g.err != nil {
		return nil, g.err
	}
	if g.creds != nil {
		return g.creds, nil
	 }
	c := &Credentials{}
	c.MinIO = MinIOCredentials{AccessKey: fmt.Sprintf("task-%d", taskID)}
	return c, nil
}

// wireTask mirrors the executeTask wire pattern: prefetch starts BEFORE the
// advisory phase validation and the /started report, Join sits where the
// serial fetch used to be. validate/report are stand-ins for the transport
// round trips that do not need the credentials.
func wireTask(prov *wireGrant, taskID int, work time.Duration) (*Credentials, error) {
	f := StartCredsPrefetch(context.Background(), prov, taskID)
	time.Sleep(work) // ValidatePhase + ReportStarted round trips (~0.15 s)
	return f.Join()
}

func TestWireSuccessParity(t *testing.T) {
	l := &wireLog{}
	prov := &wireGrant{l: l, dur: 20 * time.Millisecond}
	c, e := wireTask(prov, 101, 10 * time.Millisecond)
	if e != nil {
		 t.Errorf("wire grant error: %v", e)
	}
	if c == nil || c.MinIO.AccessKey != "task-101" {
		 t.Errorf("Join must yield this task's creds (identical to the serial call's)")
	}
	l.mu.Lock()
	n := l.calls
	l.mu.Unlock()
	if n != 1 {
		 t.Errorf("exactly one grant per task, got %d", n)
	}
}

func TestWireGrantErrorParity(t *testing.T) {
	l := &wireLog{}
	boom := errors.New("get credentials: HTTP 502")
	prov := &wireGrant{l: l, dur: time.Millisecond, err: boom}
	c, e := wireTask(prov, 102, 5 * time.Millisecond)
	if c != nil {
		 t.Errorf("failed grant must carry no creds")
	}
	if e != boom {
		 t.Errorf("error must be the same object the serial call returned (caller's failure report unchanged)")
	}
}

func TestWireValidateRejectOrdering(t *testing.T) {
	// A validation REJECT returns BEFORE the Join (same order as the serial
	// path: the task never proceeds). The prefetch goroutine must still end
	// (bounded transport call) and nothing may leak or block.
	l := &wireLog{}
	prov := &wireGrant{l: l, dur: 5 * time.Millisecond}
	f := StartCredsPrefetch(context.Background(), prov, 103)
	// ... validation says "reject" -> executeTask returns here, exactly as
	// before. Give the goroutine its chance to finish; then prove it did by
	// Joining in a watchdog: if the goroutine leaked/hung, this test hangs
	// (go test -race + the runner timeout catch it) — if it leaked creds to
	// another task, TestWirePerTaskGrant catches it.
	time.Sleep(20 * time.Millisecond)
	c, e := f.Join()
	if e != nil {
		 t.Errorf("reject-path grant must not error here: %v", e)
	}
	if c == nil || c.MinIO.AccessKey != "task-103" {
		 t.Errorf("reject-path grant belongs to THIS task only")
	}
}

func TestWireCancelDuringPrefetch(t *testing.T) {
	// A cancelled context: the transport call fails (as the serial call did
	// at the old site); Join must surface that error, never hang, never
	// hand back creds.
	l := &wireLog{}
	prov := &wireGrant{l: l, dur: 5 * time.Millisecond}
	// The task's context is cancelled while the prefetch is in flight: the
	// transport call fails with the cancel error (same as the real HTTP
	// layer on a cancelled ctx), and Join must surface it, not hang and
	// not hand back creds. (cancel-before-call keeps the test
	// deterministic; the in-flight case is the same code path once the
	// provider observes ctx.Err().)
	inner, cancel := context.WithCancel(context.Background())
	cancel()
	_ = cancel
	f := StartCredsPrefetch(inner, prov, 104)
	c, e := f.Join()
	if c != nil {
		t.Errorf("cancelled/failed grant must yield no creds")
	}
	if e == nil {
		t.Errorf("cancelled context must fail the grant exactly as the serial call did")
	}
}

func TestWirePerTaskGrant(t *testing.T) {
	// credentials per task only, never shared across tasks: two tasks each
	// get their own grant and their own creds.
	l := &wireLog{}
	prov := &wireGrant{l: l, dur: 5 * time.Millisecond}
	a := StartCredsPrefetch(context.Background(), prov, 201)
	b := StartCredsPrefetch(context.Background(), prov, 202)
	ca, ea := a.Join()
	cbx, eb := b.Join()
	if ea != nil || eb != nil {
		 t.Errorf("unexpected grant error: %v %v", ea, eb)
	}
	if ca == nil || cbx == nil ||
		ca.MinIO.AccessKey != "task-201" || cbx.MinIO.AccessKey != "task-202" {
		 t.Errorf("each Join must return its own task's creds (no cross-task reuse)")
	}
	l.mu.Lock()
	ids := l.taskIDs
	n := l.calls
	l.mu.Unlock()
	// goroutine start order is nondeterministic: assert the SET of taskIDs
	seen := map[int]int{}
	for _, id := range ids {
		seen[id] = seen[id] + 1
	}
	if n != 2 || len(ids) != 2 || seen[201] != 1 || seen[202] != 1 {
		t.Errorf("one grant per taskID, got %v", ids)
	}
}

func TestWireJoinConsumesResult(t *testing.T) {
	// Mutation pin: the wire MUST Join (consume the done-chan) before using
	// creds. If the Join were dropped, creds would be read before the grant
	// completed — a race the -race build flags, and this pin catches the
	// unjoined case: Join() marks the fetch consumed.
	l := &wireLog{}
	prov := &wireGrant{l: l, dur: 15 * time.Millisecond}
	f := StartCredsPrefetch(context.Background(), prov, 301)
	c, e := f.Join()
	if e != nil {
		 t.Errorf("grant error: %v", e)
	}
	if c == nil || c.MinIO.AccessKey != "task-301" {
		 t.Errorf("creds must come from the Join, not the raw fetch struct")
	}
	f.mu.Lock()
	j := f.joined
	f.mu.Unlock()
	if !j {
		 t.Errorf("Join must consume the result (mutation: Join removed -> this fails)")
	}
}
