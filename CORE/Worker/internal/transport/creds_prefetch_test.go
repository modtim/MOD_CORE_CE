package transport

// OPT1-DISPATCH-AND-CLAIM (lane gpu1-1, 11-24) — pins the credential
// prefetch contract:
//   * exactly ONE GetCredentials call per prefetch (no retry storm, no
//     double grant);
//   * Join() returns the SAME (creds, error) pair a serial fetch would
//     have returned (behaviour unchanged for every consumer);
//   * the grant overlaps the work between start and Join: total wall time
//     is max(grant, work), not grant+work (the ~0.38 s the embedded worker
//     pays in series today);
//   * an error result propagates unwrapped (the caller's failure report
//     path is unchanged).
import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

type grantLog struct {
	calls int
	mu    sync.Mutex
}

type fakeGrant struct {
	l    *grantLog
	dur  time.Duration
	creds *Credentials
	err  error
}

func (f *fakeGrant) GetCredentials(ctx context.Context, taskID int) (*Credentials, error) {
	f.l.mu.Lock()
	f.l.calls++
	f.l.mu.Unlock()
	time.Sleep(f.dur)
	return f.creds, f.err
}

func TestCredsPrefetchSingleCall(t *testing.T) {
	l := &grantLog{}
	f := StartCredsPrefetch(context.Background(), &fakeGrant{l: l, dur: time.Millisecond}, 42)
	c, e := f.Join()
	if l.calls != 1 {
		t.Errorf("expected exactly one grant call, got %d", l.calls)
	}
	if c != nil || e != nil {
		t.Errorf("nil-bundle provider must yield exactly (nil, nil): c=%v e=%v", c, e)
	}
}

func TestCredsPrefetchOverlapsWork(t *testing.T) {
	l := &grantLog{}
	grant := 60 * time.Millisecond
	f := StartCredsPrefetch(context.Background(), &fakeGrant{l: l, dur: grant}, 7)
	// The work the serial path would have done BEFORE the grant (phase
	// validation + /started report): ~40 ms here.
	time.Sleep(40 * time.Millisecond)
	start := time.Now()
	c, e := f.Join()
	_ = c
	_ = e
	elapsed := time.Since(start)
	if elapsed+10*time.Millisecond >= grant {
		t.Errorf("join must overlap the 40ms of prior work: elapsed %v >= grant %v (serial, not overlapped)", elapsed, grant)
	}
}

func TestCredsPrefetchErrorPropagates(t *testing.T) {
	l := &grantLog{}
	boom := errors.New("boom")
	f := StartCredsPrefetch(context.Background(), &fakeGrant{l: l, dur: time.Millisecond, err: boom}, 8)
	c, e := f.Join()
	if c != nil {
		t.Errorf("error result must carry no creds bundle")
	}
	if e != boom {
		t.Errorf("error must propagate unwrapped (same object the serial call returned)")
	}
}
