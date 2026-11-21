// 2400BUG-1028h J1 — resume fenced stacks IN PLACE on confirmed recontact.
//
// The bug (owner: HUGE): a site network loss fences every stack and nothing
// comes back. fence.go stops the host's own stacks at lease expiry, and
// noteFenceRecovered used to clear the fence state while restarting nothing —
// leaving the reconciler's 2-minute sweep to redeploy a stack that never left
// this host. These tests drive applyHeartbeatResult across a fake partition
// and a reconnecting beat, which is the only way to exercise the decision:
// the live proof (sever a worker's route to its manager) cannot run on a
// deployment where worker and API share a compose network.
//
// The safety property under test is the AUTHORITY rule, not the restart
// itself: the worker may restart ONLY the projects the manager named in the
// reconnecting response (`stack_resume_projects`), intersected with what it
// actually stopped this episode. Its own fencedStacks list is evidence of
// what it stopped, never authority to start it.

package heartbeat

import (
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A 5-minute outage: past the 120s lease, and past the 5-minute fenceBackoff
// ceiling, so the episode is unambiguous.
const partitionSeconds = 300

// lostContactBeat is the ENETUNREACH-classified error — no route to host, so
// countsAsLostContact returns true and the lease advances.
func lostContactBeat() *net.OpError {
	return &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ENETUNREACH}
}

// okResponse is a decoded 200; err==nil is what applyHeartbeatResult sees.
func okResponse() *http.Response {
	return &http.Response{StatusCode: 200}
}

// testLogger comes from fence_test.go (same package) — no redeclaration here.

// logSink collects the worker's own log lines so a test can assert WHY a
// resume was refused, not merely that it was.
type logSink struct {
	text []byte
}

func (l *logSink) Write(b []byte) (int, error) {
	l.text = append(l.text, b...)
	return len(b), nil
}

func captureLogger(sink *logSink) *slog.Logger {
	return slog.New(slog.NewTextHandler(sink, nil))
}

// runLog mirrors what main.go's OnResumeStacks callback asks of the executor:
// one `compose start` per named project (the executor's `start` op,
// stackrunner.go `case "start":`). Asserting on the recorded op is what pins
// the resume to `start` — never stop, up or down.
type runLog struct {
	calls   int
	ops     []string
}

func (l *runLog) recordStart(projects []string) (started []string, err error) {
	l.calls++
	for _, p := range projects {
		l.ops = append(l.ops, "start:" + p)
		started = append(started, p)
	}
	return started, nil
}

// join renders recorded ops so a test can compare them against an exact
// expectation without slice ==, which Go forbids on slices.
func join(ops []string) string {
	return strings.Join(ops, ",")
}

// partitioned drives `beats` lost-contact attempts 10s apart from start.
func partitioned(h *Heartbeat, start time.Time, beats int) {
	for i := 0; i < beats; i++ {
		h.applyHeartbeatResult(start.Add(time.Duration(i+1)*10*time.Second), nil, lostContactBeat())
	}
}

// TestFenceRecontact_ResumeAfterFiveMinutePartition is the bug itself, closed:
// a real 5-minute partition fences (the fencer ran, `fenced` latched), then
// ONE successful beat whose response names the project this worker stopped
// must restart it in place, immediately, with no manual step.
func TestFenceRecontact_ResumeAfterFiveMinutePartition(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	var ran int
	h.OnFenceStacks(func() ([]string, error) {
		ran++
		return []string{"alpha"}, nil
	})
	var ops runLog
	h.OnResumeStacks(func(projects []string) ([]string, error) {
		return ops.recordStart(projects)
	})
	h.SetFenceLease(120)

	start := time.Now()
	h.noteContact(start)
	// 30 beats * 10s = 300s of ENETUNREACH: a genuine 5-minute partition.
	partitioned(h, start, partitionSeconds/10)
	if !h.Fenced() {
		t.Fatal("did not fence across a 5-minute partition; the resume path is " +
			"pointless unless the fence actually happened")
	}
	if ran != 1 {
		t.Fatal("fencer ran "+fmt.Sprint(ran)+" times; expected exactly one stop " +
			"for one episode (backoff must not turn the fence into a hot loop)")
	}

	// The reconnecting beat: the manager decoded a 200 AND named the project
	// this worker stopped. Stash first — sendAPI does exactly this, before
	// applyHeartbeatResult runs on the same tick.
	h.stashResumeCandidates([]string{"alpha"})
	h.applyHeartbeatResult(start.Add(time.Duration(partitionSeconds+10)*time.Second), okResponse(), nil)

	if h.Fenced() {
		t.Fatal("still fenced after contact restored")
	}
	if ops.calls != 1 {
		t.Fatal("resume callback ran "+fmt.Sprint(ops.calls)+" times; expected exactly " +
			"one on the reconnecting beat — this is the manual step the bug left")
	}
	if join(ops.ops) != "start:alpha" {
		t.Fatal("executor recorded "+fmt.Sprint(ops.ops)+"; expected exactly one " +
			"`compose start` for alpha (never stop/up/down)")
	}
}

// TestFenceRecontact_NoResumeWhenPlacementMoved is the split-brain MUST: the
// same partition, but the reconnecting response names a project this worker
// never stopped (or omits the key entirely). Placement moved, so nothing here
// may start — starting it would be the two-live-copies state fencing exists
// to prevent.
func TestFenceRecontact_NoResumeWhenPlacementMoved(t *testing.T) {
	var sink logSink
	log := captureLogger(&sink)
	for _, named := range [][]string{{"beta"}, {}} {
		h := &Heartbeat{log: log}
		h.OnFenceStacks(func() ([]string, error) { return []string{"alpha"}, nil })
		var ops runLog
		h.OnResumeStacks(func(projects []string) ([]string, error) {
			return ops.recordStart(projects)
		})
		h.SetFenceLease(120)

		start := time.Now()
		h.noteContact(start)
		partitioned(h, start, partitionSeconds/10)
		if !h.Fenced() {
			t.Fatal("did not fence across a 5-minute partition")
		}

		h.stashResumeCandidates(named)
		h.applyHeartbeatResult(start.Add(time.Duration(partitionSeconds+10)*time.Second), okResponse(), nil)

		if ops.calls != 0 || ops.ops != nil {
			t.Fatal("resumed a project the manager did not name for this worker; " +
				"placement moved and this host must stay stopped")
		}
		text := string(sink.text)
		if len(named) > 0 && !strings.Contains(text, "resume skipped") {
			t.Fatal("no log naming the skipped project: " + text)
		}
		if len(named) == 0 && strings.Contains(text, "resumed fenced stacks") {
			t.Fatal("resumed with no stack_resume_projects key at all")
		}
	}
}


// TestFenceRecontact_ResumeResetsBackoff: the reconnecting beat must clear
// the fence retry bookkeeping (fence.go:350/353) so the NEXT partition fences
// immediately, and must not fire a second `compose stop`.
func TestFenceRecontact_ResumeResetsBackoff(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	var stops int
	h.OnFenceStacks(func() ([]string, error) {
		stops++
		return []string{"alpha"}, nil
	})
	var ops runLog
	h.OnResumeStacks(func(projects []string) ([]string, error) {
		return ops.recordStart(projects)
	})
	h.SetFenceLease(120)

	start := time.Now()
	h.noteContact(start)
	partitioned(h, start, partitionSeconds/10)
	if h.fenceAttempts.Load() != 0 || h.nextFenceAttemptUnix.Load() != 0 {
		t.Fatal("a successful fence left retry bookkeeping set; the next partition " +
			"would be delayed by a stale backoff")
	}

	h.stashResumeCandidates([]string{"alpha"})
	h.applyHeartbeatResult(start.Add(time.Duration(partitionSeconds+10)*time.Second), okResponse(), nil)
	if h.fenceAttempts.Load() != 0 || h.nextFenceAttemptUnix.Load() != 0 {
		t.Fatal("recontact did not zero fenceAttempts/nextFenceAttemptUnix")
	}
	if stops != 1 {
		t.Fatal("compose stop fired "+fmt.Sprint(stops)+" times; the reconnecting " +
			"tick must not stop again")
	}
	if ops.calls != 1 {
		t.Fatal("resume callback ran "+fmt.Sprint(ops.calls)+" times; expected 1")
	}

	// A fresh partition right after the resume fences on its own lease again.
	h.noteContact(start.Add(time.Duration(partitionSeconds+10)*time.Second))
	partitioned(h, start.Add(time.Duration(partitionSeconds+10)*time.Second), 13)
	if stops != 2 {
		t.Fatal("the next partition did not fence immediately; backoff leaked past " +
			"the resume")
	}
}
