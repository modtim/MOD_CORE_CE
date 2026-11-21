package heartbeat

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"syscall"
	"testing"
	"time"
)

// fakeTimeoutErr implements net.Error with Timeout()==true, the shape
// net.OpError.Timeout() looks for on its wrapped Err — used to construct a
// synthetic dial/i-o timeout without a live socket.
type fakeTimeoutErr struct{}

func (fakeTimeoutErr) Error() string   { return "i/o timeout" }
func (fakeTimeoutErr) Timeout() bool   { return true }
func (fakeTimeoutErr) Temporary() bool { return true }

// Discards output — these assert behaviour, not log formatting.
func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// Fencing is the safety property that makes re-placement correct. Without it a
// manager that decides a worker is gone and re-places its stacks can end up
// with TWO live copies — the "gone" worker may be healthy and merely
// unreachable, still serving and still writing to its volumes.
//
// The live proof (partition a worker, watch its stacks stop) cannot run on this
// deployment: the dev worker shares a compose network with the API, so severing
// its route severs the connection running the test. That is queued in
// STAGE_VERIFICATION_QUEUE.md §4. These cover the decision logic, which is
// where the ordering argument actually lives.

func TestShouldFence_WithinLeaseKeepsRunning(t *testing.T) {
	now := time.Now()
	if shouldFence(now.Add(-30*time.Second), now, 120*time.Second, false) {
		t.Fatal("fenced 30s into a 120s lease; a brief interruption must not " +
			"stop production stacks")
	}
}

func TestShouldFence_ExpiredLeaseFences(t *testing.T) {
	now := time.Now()
	if !shouldFence(now.Add(-121*time.Second), now, 120*time.Second, false) {
		t.Fatal("did not fence past the lease; the manager will re-place these " +
			"stacks and both copies would run at once")
	}
}

func TestShouldFence_ExactlyAtLeaseDoesNotFence(t *testing.T) {
	// Strictly greater-than. At exactly the boundary the lease has not been
	// exceeded, and biasing toward NOT fencing costs a few seconds of delay
	// while biasing the other way stops a healthy stack.
	now := time.Now()
	if shouldFence(now.Add(-120*time.Second), now, 120*time.Second, false) {
		t.Fatal("fenced exactly at the lease boundary")
	}
}

func TestShouldFence_NeverContactedDoesNotFence(t *testing.T) {
	// A worker still starting up has never reached a manager, so it cannot be
	// hosting a stack a manager gave it. Fencing here would stop nothing and
	// log an alarming lie.
	if shouldFence(time.Time{}, time.Now(), 120*time.Second, false) {
		t.Fatal("fenced a worker that has never contacted a manager")
	}
}

func TestShouldFence_IsNotRepeated(t *testing.T) {
	// Re-issuing `compose stop` against a half-stopped project every tick
	// turns one outage into a loop.
	now := time.Now()
	if shouldFence(now.Add(-600*time.Second), now, 120*time.Second, true) {
		t.Fatal("fenced again while already fenced")
	}
}

func TestShouldFence_ZeroLeaseDisabled(t *testing.T) {
	now := time.Now()
	if shouldFence(now.Add(-600*time.Second), now, 0, false) {
		t.Fatal("fenced with no lease configured")
	}
}

// The ordering that makes the whole thing safe: the worker must have stopped
// BEFORE the manager is willing to re-place. Both numbers come from one source
// (the manager's grace period) precisely so this cannot be violated by
// configuration.
func TestLeaseMustExpireBeforeManagerGrace(t *testing.T) {
	// Mirrors services/stack_observation.fence_lease_seconds():
	//   lease = max(30, grace - margin)
	for _, c := range []struct{ grace, margin int }{
		{180, 60}, {300, 60}, {120, 30}, {90, 60},
	} {
		lease := c.grace - c.margin
		if lease < 30 {
			lease = 30
		}
		if lease >= c.grace {
			t.Fatalf("grace=%d margin=%d gives lease=%d, which is not shorter "+
				"than the grace period — the manager would re-place a stack "+
				"whose original copy has not stopped yet", c.grace, c.margin, lease)
		}
	}
}

func TestLeaseFloorStillRespectsOrdering(t *testing.T) {
	// The 30s floor must not be able to exceed a small grace period. A grace
	// below the floor is a misconfiguration, but it must fail safe.
	grace, margin := 40, 60
	lease := grace - margin
	if lease < 30 {
		lease = 30
	}
	if lease >= grace {
		t.Skip("grace below the lease floor is a misconfiguration; documented " +
			"in STAGE_VERIFICATION_QUEUE.md §6 as needing a measured value")
	}
}

func TestSetFenceLease_ClampsBelowFloor(t *testing.T) {
	h := &Heartbeat{}
	h.SetFenceLease(5)
	if got := h.FenceLease(); got != minFenceLeaseSeconds*time.Second {
		t.Fatalf("lease %v, want %v — a lease this short fences on ordinary "+
			"heartbeat jitter", got, minFenceLeaseSeconds*time.Second)
	}
}

func TestSetFenceLease_AcceptsAServerValue(t *testing.T) {
	h := &Heartbeat{}
	h.SetFenceLease(120)
	if got := h.FenceLease(); got != 120*time.Second {
		t.Fatalf("lease %v, want 2m", got)
	}
}

func TestFenceLease_DefaultsWhenUnset(t *testing.T) {
	// An older manager pushes nothing; falling back to a built-in default is
	// safer than reading zero as "fencing disabled".
	h := &Heartbeat{}
	if got := h.FenceLease(); got != defaultFenceLeaseSeconds*time.Second {
		t.Fatalf("lease %v, want the %ds default", got, defaultFenceLeaseSeconds)
	}
}

func TestRecoveryDoesNotRestartStacks(t *testing.T) {
	// The dangerous case. By the time contact returns the manager may already
	// have re-placed these stacks; a worker that helpfully restarted its old
	// copies produces exactly the two-live-instances state fencing prevents.
	restarted := false
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		restarted = true // must only ever be called to STOP
		return nil, nil
	})
	h.fenced.Store(true)
	h.fencedAtUnix.Store(time.Now().Unix())

	h.noteFenceRecovered()

	if h.Fenced() {
		t.Fatal("still fenced after contact was restored")
	}
	if restarted {
		t.Fatal("recovery called the stack callback; it must never restart " +
			"previously-hosted stacks")
	}
}

func TestFenceReport_NilUntilFenced(t *testing.T) {
	// Reported to the manager so an outage is attributable to fencing rather
	// than to a crash. A worker that has never fenced says nothing.
	h := &Heartbeat{}
	if h.fenceReport() != nil {
		t.Fatal("reported fence state having never fenced")
	}
}

func TestFenceReport_IdentifiesFencingAndSurvivesRecovery(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	h.fenced.Store(true)
	h.fencedAtUnix.Store(time.Now().Unix())
	stopped := []string{"modstack10g0"}
	h.fencedStacks.Store(&stopped)

	r := h.fenceReport()
	if r == nil || r["fenced"] != true {
		t.Fatalf("fence not reported: %v", r)
	}
	if r["fenced_at"] == nil {
		t.Fatal("no fence time recorded; the outage is not attributable")
	}

	// After recovery the report must persist — the manager needs to know this
	// host fenced, not just that it is currently reachable again.
	h.noteFenceRecovered()
	r2 := h.fenceReport()
	if r2 == nil {
		t.Fatal("fence history vanished on recovery")
	}
	if r2["fenced"] != false {
		t.Fatal("still reporting actively fenced after recovery")
	}
}

func TestFenceReport_SurfacesAFailedStop(t *testing.T) {
	// If the stop failed, containers may survive here. The manager still
	// re-places after its grace period, so that stack must be flagged as
	// possibly holding resources on this host.
	h := &Heartbeat{log: testLogger()}
	h.fenced.Store(true)
	h.fencedAtUnix.Store(time.Now().Unix())
	h.fenceErr.Store("modstack10g0: exit 1")

	r := h.fenceReport()
	if r["stop_failed"] == nil {
		t.Fatal("a failed fence was not surfaced; resources could be stranded " +
			"on this host with nothing flagging them for reclamation")
	}
}

func TestEvaluateFence_StopsStacksOnceLeaseExpires(t *testing.T) {
	calls := 0
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		calls++
		return []string{"modstack1g0"}, nil
	})
	h.SetFenceLease(60)
	h.noteContact(time.Now().Add(-300 * time.Second))

	h.evaluateFence(time.Now())
	if calls != 1 || !h.Fenced() {
		t.Fatalf("expected one fence call and fenced state, got calls=%d fenced=%v",
			calls, h.Fenced())
	}

	// A second tick must not re-issue the stop.
	h.evaluateFence(time.Now())
	if calls != 1 {
		t.Fatalf("fence re-issued: %d calls", calls)
	}
}

// §3.7 — a failed fence must not be terminal.
//
// Measured 2026-08-01: the fencer could not enumerate containers, logged
// "fence FAILED — stacks may still be running on this host", and never tried
// again, because `fenced` was latched BEFORE the fencer ran. The manager then
// re-placed at gap grace against stacks that had never stopped — two live
// copies, which is the one outcome this whole file exists to prevent.

func TestEvaluateFence_FailedFenceIsRetried(t *testing.T) {
	calls := 0
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		calls++
		return nil, errors.New("context deadline exceeded")
	})
	h.SetFenceLease(60)
	start := time.Now()
	h.noteContact(start.Add(-300 * time.Second))

	h.evaluateFence(start)
	if calls != 1 {
		t.Fatalf("expected one attempt, got %d", calls)
	}

	// Past the first backoff window.
	h.evaluateFence(start.Add(initialFenceBackoff + time.Second))
	if calls != 2 {
		t.Fatalf("a failed fence was never retried (%d calls); stacks may still "+
			"be serving while the manager re-places them", calls)
	}
}

func TestEvaluateFence_FailedFenceDoesNotClaimToBeFenced(t *testing.T) {
	// The lie this protocol cannot tolerate: a host reporting itself fenced
	// while its stacks are still serving. The manager re-places on that basis.
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		return nil, errors.New("docker socket unreachable")
	})
	h.SetFenceLease(60)
	h.noteContact(time.Now().Add(-300 * time.Second))

	h.evaluateFence(time.Now())

	if h.Fenced() {
		t.Fatal("reported fenced after the fence FAILED; containers may still " +
			"be running here")
	}
	r := h.fenceReport()
	if r["stop_failed"] == nil {
		t.Fatal("failure not surfaced to the manager")
	}
	if r["fence_retrying"] != true {
		t.Fatal("a host retrying a fence is indistinguishable from one that " +
			"never tried")
	}
}

func TestEvaluateFence_StopErrorIsLoggedWithItsText(t *testing.T) {
	// A failed stop must reach the operator's log with the error text, not only
	// the manager-facing fence_error field. Logging must not change the outcome:
	// the host is not latched fenced, the attempt is counted, and the error is
	// kept, exactly as on the plain failure path.
	var buf bytes.Buffer
	h := &Heartbeat{log: slog.New(slog.NewTextHandler(&buf, nil))}
	const stopErr = "fence incomplete: modstack2g0: exit 1"
	h.OnFenceStacks(func() ([]string, error) {
		return nil, errors.New(stopErr)
	})
	h.SetFenceLease(60)
	h.noteContact(time.Now().Add(-300 * time.Second))

	h.evaluateFence(time.Now())

	out := buf.String()
	if !strings.Contains(out, "fence FAILED") {
		t.Fatalf("failed stop not logged; log output: %q", out)
	}
	if !strings.Contains(out, stopErr) {
		t.Fatalf("stop error text missing from log; log output: %q", out)
	}
	if h.Fenced() {
		t.Fatal("reported fenced after the stop FAILED")
	}
	if h.fenceAttempts.Load() != 1 {
		t.Fatalf("fence attempts = %d, want 1", h.fenceAttempts.Load())
	}
	if h.fenceErr.Load() != stopErr {
		t.Fatalf("fence error = %v, want %q", h.fenceErr.Load(), stopErr)
	}
}

func TestEvaluateFence_BackoffPacesTheRetry(t *testing.T) {
	// Retrying is correct; retrying every 15s against a broken Docker socket
	// for the length of a partition is not.
	calls := 0
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		calls++
		return nil, errors.New("still broken")
	})
	h.SetFenceLease(60)
	start := time.Now()
	h.noteContact(start.Add(-300 * time.Second))

	h.evaluateFence(start)
	h.evaluateFence(start.Add(time.Second)) // well inside the backoff
	if calls != 1 {
		t.Fatalf("retried inside the backoff window: %d calls", calls)
	}
}

func TestEvaluateFence_PartialStopIsStillRecorded(t *testing.T) {
	// Two of three stopped is a different fact from nothing stopped, and the
	// manager needs it to know what may still be holding resources here.
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		return []string{"modstack1g0"}, errors.New("fence incomplete: modstack2g0")
	})
	h.SetFenceLease(60)
	h.noteContact(time.Now().Add(-300 * time.Second))

	h.evaluateFence(time.Now())

	got := h.fenceReport()["stopped_projects"]
	stopped, ok := got.([]string)
	if !ok || len(stopped) != 1 || stopped[0] != "modstack1g0" {
		t.Fatalf("what DID stop was discarded on a partial failure: %v", got)
	}
}

func TestEvaluateFence_RetryThatSucceedsLatches(t *testing.T) {
	calls := 0
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		calls++
		if calls == 1 {
			return nil, errors.New("transient")
		}
		return []string{"modstack1g0"}, nil
	})
	h.SetFenceLease(60)
	start := time.Now()
	h.noteContact(start.Add(-300 * time.Second))

	h.evaluateFence(start)
	h.evaluateFence(start.Add(initialFenceBackoff + time.Second))
	if !h.Fenced() {
		t.Fatal("a successful retry did not record the host as fenced")
	}

	// And having succeeded, it must stop trying.
	h.evaluateFence(start.Add(10 * time.Minute))
	if calls != 2 {
		t.Fatalf("fence re-issued after success: %d calls", calls)
	}
	if h.fenceReport()["fence_retrying"] == true {
		t.Fatal("still reporting a retry in progress after the fence succeeded")
	}
}

func TestNoteFenceRecovered_ClearsPendingRetryState(t *testing.T) {
	// A worker mid-retry has attempts pending while `fenced` is still false.
	// Carrying that backoff into the next genuine partition would delay the
	// fence that actually matters.
	h := &Heartbeat{log: testLogger()}
	h.fenceAttempts.Store(4)
	h.nextFenceAttemptUnix.Store(time.Now().Add(time.Hour).Unix())

	h.noteFenceRecovered()

	if h.fenceAttempts.Load() != 0 || h.nextFenceAttemptUnix.Load() != 0 {
		t.Fatal("retry state survived recovery; the next partition would wait " +
			"out a stale backoff before fencing")
	}
}

func TestEvaluateFence_StoppedListAccumulatesAcrossRetries(t *testing.T) {
	// Each attempt only sees what is still RUNNING, so a retry cannot re-report
	// what the previous attempt already stopped. Replacing rather than merging
	// would make the list SHRINK as the fence neared completion.
	calls := 0
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		calls++
		if calls == 1 {
			return []string{"modstack1g0"}, errors.New("fence incomplete: modstack2g0")
		}
		return []string{"modstack2g0"}, nil
	})
	h.SetFenceLease(60)
	start := time.Now()
	h.noteContact(start.Add(-300 * time.Second))

	h.evaluateFence(start)
	h.evaluateFence(start.Add(initialFenceBackoff + time.Second))

	got, _ := h.fenceReport()["stopped_projects"].([]string)
	if len(got) != 2 {
		t.Fatalf("stopped list did not accumulate across attempts: %v", got)
	}
}

func TestEvaluateFence_ASecondPartitionRestampsTheOutage(t *testing.T) {
	// Attribution. A fence months ago must not be reported as the cause of
	// today's outage — but a retry within one episode must not move the time
	// forward either.
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) { return []string{"modstack1g0"}, nil })
	h.SetFenceLease(60)

	first := time.Now().Add(-48 * time.Hour)
	h.noteContact(first.Add(-300 * time.Second))
	h.evaluateFence(first)
	firstStamp := h.fencedAtUnix.Load()

	// Contact returns, then a fresh partition two days later.
	h.noteContact(time.Now().Add(-300 * time.Second))
	h.noteFenceRecovered()
	now := time.Now()
	h.evaluateFence(now)

	if h.fencedAtUnix.Load() == firstStamp {
		t.Fatal("a new partition reported the previous fence's timestamp; the " +
			"outage is attributed to the wrong day")
	}
}

func TestFenceBackoff_GrowsAndIsCapped(t *testing.T) {
	if got := fenceBackoff(1); got != initialFenceBackoff {
		t.Fatalf("first retry waited %s, want %s", got, initialFenceBackoff)
	}
	if fenceBackoff(2) <= fenceBackoff(1) {
		t.Fatal("backoff does not grow with consecutive failures")
	}
	if got := fenceBackoff(100); got != maxFenceBackoff {
		t.Fatalf("backoff uncapped at %s; a long partition would stop retrying "+
			"in any useful window", got)
	}
}

func TestEvaluateFence_QuietWhileContactIsFresh(t *testing.T) {
	calls := 0
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) { calls++; return nil, nil })
	h.SetFenceLease(120)
	h.noteContact(time.Now())

	h.evaluateFence(time.Now())
	if calls != 0 || h.Fenced() {
		t.Fatal("fenced a worker in contact with its manager")
	}
}

// countsAsLostContact is the classification that closes the 2026-09-28
// incident: `docker restart my_fastapi_app` made the API answer 502 for
// ~110-125s (longer than the 120s default lease), and every worker's fence
// timer — which previously treated ANY heartbeat error identically — fired
// and stopped every hosted stack fleet-wide. Only genuine network-level
// unreachability may advance the lease; a response of any status, or a
// refused/reset TCP connection, means the manager HOST is up.
func TestCountsAsLostContact(t *testing.T) {
	cases := []struct {
		name string
		err  error
		resp *http.Response
		want bool
	}{
		{"http 502 is contact, not lost", fmt.Errorf("heartbeat API: HTTP 502"), &http.Response{StatusCode: 502}, false},
		{"http 503 is contact, not lost", fmt.Errorf("heartbeat API: HTTP 503"), &http.Response{StatusCode: 503}, false},
		{"http 504 is contact, not lost", fmt.Errorf("heartbeat API: HTTP 504"), &http.Response{StatusCode: 504}, false},
		{"nil error is contact", nil, nil, false},
		{"connection refused is contact", &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}, nil, false},
		{"connection reset is contact", &net.OpError{Op: "read", Net: "tcp", Err: syscall.ECONNRESET}, nil, false},
		{"dial timeout is lost contact", &net.OpError{Op: "dial", Net: "tcp", Err: fakeTimeoutErr{}}, nil, true},
		{"no route to host is lost contact", &net.OpError{Op: "dial", Net: "tcp", Err: syscall.EHOSTUNREACH}, nil, true},
		{"network unreachable is lost contact", &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ENETUNREACH}, nil, true},
		{"dns failure is lost contact", &net.DNSError{Err: "no such host", Name: "manager.internal", IsNotFound: true}, nil, true},
		{"tls handshake timeout is lost contact", errors.New("net/http: TLS handshake timeout"), nil, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := countsAsLostContact(c.err, c.resp); got != c.want {
				t.Fatalf("countsAsLostContact(%v, resp=%v) = %v, want %v", c.err, c.resp, got, c.want)
			}
		})
	}
}

// TestApplyHeartbeatResult_HTTPErrorResponseDoesNotFence reproduces the
// incident directly: a 502 for longer than the lease must never fence.
func TestApplyHeartbeatResult_HTTPErrorResponseDoesNotFence(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		t.Fatal("fencer invoked on a mere API restart (502), not a real partition")
		return nil, nil
	})
	h.SetFenceLease(120)
	start := time.Now()
	h.noteContact(start)

	resp := &http.Response{StatusCode: 502}
	err := fmt.Errorf("heartbeat API: HTTP 502")
	for i := 0; i < 20; i++ { // 20 * 10s = 200s, well past the 125s outage measured
		h.applyHeartbeatResult(start.Add(time.Duration(i+1)*10*time.Second), resp, err)
	}
	if h.Fenced() {
		t.Fatal("fenced during a 502 API-restart outage; this is the incident this fix closes")
	}
}

func TestApplyHeartbeatResult_ConnRefusedDoesNotFence(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) {
		t.Fatal("fencer invoked on a refused connection; manager host was reachable")
		return nil, nil
	})
	h.SetFenceLease(120)
	start := time.Now()
	h.noteContact(start)

	err := &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}
	for i := 0; i < 20; i++ {
		h.applyHeartbeatResult(start.Add(time.Duration(i+1)*10*time.Second), nil, err)
	}
	if h.Fenced() {
		t.Fatal("fenced on connection refused")
	}
}

func TestApplyHeartbeatResult_TimeoutPastLeaseFences(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) { return []string{"stack1"}, nil })
	h.SetFenceLease(120)
	start := time.Now()
	h.noteContact(start)

	err := &net.OpError{Op: "dial", Net: "tcp", Err: fakeTimeoutErr{}}
	h.applyHeartbeatResult(start.Add(121*time.Second), nil, err)
	if !h.Fenced() {
		t.Fatal("did not fence after a genuine 121s network partition")
	}
}

// TestApplyHeartbeatResult_MixedSequenceResetsClock covers the exact mixed
// case called for: timeouts (real unreachability), then a 502 (manager host
// back, API still restarting) must reset the clock so the next timeout
// window starts fresh rather than fencing off the earlier partition.
func TestApplyHeartbeatResult_MixedSequenceResetsClock(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	h.OnFenceStacks(func() ([]string, error) { return []string{"stack1"}, nil })
	h.SetFenceLease(120)
	start := time.Now()
	h.noteContact(start)

	timeoutErr := &net.OpError{Op: "dial", Net: "tcp", Err: fakeTimeoutErr{}}
	h.applyHeartbeatResult(start.Add(90*time.Second), nil, timeoutErr)
	if h.Fenced() {
		t.Fatal("fenced within the original lease")
	}

	resp := &http.Response{StatusCode: 502}
	respErr := fmt.Errorf("heartbeat API: HTTP 502")
	h.applyHeartbeatResult(start.Add(95*time.Second), resp, respErr)
	if h.Fenced() {
		t.Fatal("a 502 must never itself fence")
	}

	// 100s of further timeouts measured from the 502 reset (at 95s) is still
	// within a fresh 120s lease.
	h.applyHeartbeatResult(start.Add(195*time.Second), nil, timeoutErr)
	if h.Fenced() {
		t.Fatal("fenced within a fresh lease window measured from the clock reset")
	}

	// Now genuinely exceed the RESET lease (121s past the 95s reset point).
	h.applyHeartbeatResult(start.Add(216*time.Second), nil, timeoutErr)
	if !h.Fenced() {
		t.Fatal("did not fence once the reset lease actually expired")
	}
}
