package outbox

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"path/filepath"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func testLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeSender records calls and can be told to fail in a specific way.
type fakeSender struct {
	completeCalls int
	failedCalls   int
	failedEgress  *transport.EgressReport // last egress seen by ReportFailedWithEgress
	err           error // returned until cleared
}

func (f *fakeSender) ReportComplete(ctx context.Context, taskID int, r transport.TaskResult) error {
	f.completeCalls++
	return f.err
}

func (f *fakeSender) ReportFailed(ctx context.Context, taskID int, msg string, retry bool,
	logTail string, oom bool, errorDetail string) error {
	f.failedCalls++
	return f.err
}

func (f *fakeSender) ReportFailedWithEgress(ctx context.Context, taskID int, msg string, retry bool,
	logTail string, oom bool, errorDetail string, egress *transport.EgressReport) error {
	f.failedCalls++
	f.failedEgress = egress
	return f.err
}

func newTestReporter(t *testing.T, s *fakeSender) (*Reporter, *Store) {
	t.Helper()
	store, err := New(t.TempDir())
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return NewReporter(store, s, testLog(), time.Minute), store
}

// THE case this exists for: the manager is unreachable when a task finishes.
// The result must be kept, then delivered once the manager returns — not
// dropped, which would make the manager requeue and re-run the whole job.
func TestCompletionSurvivesManagerOutage(t *testing.T) {
	send := &fakeSender{err: errors.New("dial tcp: connection refused")}
	rep, store := newTestReporter(t, send)

	rep.Complete(context.Background(), 42, transport.TaskResult{LogTail: "done"})

	if store.Len() != 1 {
		t.Fatalf("result was not queued: len=%d", store.Len())
	}

	// Manager comes back.
	send.err = nil
	if n := rep.Drain(context.Background()); n != 1 {
		t.Fatalf("Drain delivered %d, want 1", n)
	}
	if store.Len() != 0 {
		t.Errorf("delivered entry still queued: len=%d", store.Len())
	}
}

// A refusal is a decision, not an outage. Retrying it forever would grow a
// queue that never empties.
func TestPermanentRejectionIsNotQueued(t *testing.T) {
	send := &fakeSender{err: &transport.HTTPError{
		Path: "/workers/tasks/42/complete", StatusCode: 403,
		Body: "Task not claimed by this worker",
	}}
	rep, store := newTestReporter(t, send)

	rep.Complete(context.Background(), 42, transport.TaskResult{})

	if store.Len() != 0 {
		t.Errorf("a 403 must not be queued for retry: len=%d", store.Len())
	}
}

// If the manager reassigned the task while we were away, the queued result is
// refused on delivery and must be dropped rather than retried forever.
func TestQueuedEntryDroppedWhenManagerRefuses(t *testing.T) {
	send := &fakeSender{err: errors.New("connection refused")}
	rep, store := newTestReporter(t, send)
	rep.Complete(context.Background(), 42, transport.TaskResult{})
	if store.Len() != 1 {
		t.Fatalf("setup: len=%d", store.Len())
	}

	send.err = &transport.HTTPError{Path: "/complete", StatusCode: 403, Body: "reassigned"}
	if n := rep.Drain(context.Background()); n != 0 {
		t.Fatalf("Drain reported %d delivered, want 0", n)
	}
	if store.Len() != 0 {
		t.Errorf("refused entry must be dropped: len=%d", store.Len())
	}
}

// A still-down manager must leave the entry queued.
func TestTransientFailureKeepsEntryQueued(t *testing.T) {
	send := &fakeSender{err: errors.New("connection refused")}
	rep, store := newTestReporter(t, send)
	rep.Complete(context.Background(), 7, transport.TaskResult{})

	for i := 0; i < 3; i++ {
		if n := rep.Drain(context.Background()); n != 0 {
			t.Fatalf("delivered while manager down")
		}
	}
	if store.Len() != 1 {
		t.Fatalf("entry lost across retries: len=%d", store.Len())
	}

	send.err = nil
	if n := rep.Drain(context.Background()); n != 1 {
		t.Errorf("Drain delivered %d, want 1", n)
	}
}

// 408 and 429 are invitations to try again, not refusals.
func TestRetryableStatusCodesAreQueued(t *testing.T) {
	for _, code := range []int{408, 429, 500, 502, 503} {
		send := &fakeSender{err: &transport.HTTPError{Path: "/complete", StatusCode: code}}
		rep, store := newTestReporter(t, send)
		rep.Complete(context.Background(), 1, transport.TaskResult{})
		if store.Len() != 1 {
			t.Errorf("HTTP %d should be queued for retry, len=%d", code, store.Len())
		}
	}
}

func TestFailureReportsAreQueuedToo(t *testing.T) {
	send := &fakeSender{err: errors.New("connection refused")}
	rep, store := newTestReporter(t, send)

	rep.Failed(context.Background(), 9, "boom", true, "tail", true, "")
	if store.Len() != 1 {
		t.Fatalf("failure report not queued: len=%d", store.Len())
	}

	send.err = nil
	if n := rep.Drain(context.Background()); n != 1 {
		t.Fatalf("Drain delivered %d, want 1", n)
	}
	if send.failedCalls < 2 {
		t.Errorf("expected a replayed ReportFailed, calls=%d", send.failedCalls)
	}
}

// Entries must survive the process that wrote them — a worker restart during a
// manager outage must not lose finished work.
func TestEntriesSurviveAcrossStoreInstances(t *testing.T) {
	dir := t.TempDir()
	s1, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	r1 := NewReporter(s1, &fakeSender{err: errors.New("down")}, testLog(), time.Minute)
	r1.Complete(context.Background(), 55, transport.TaskResult{LogTail: "persisted"})

	// A fresh process opening the same directory sees the queued result.
	s2, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if s2.Len() != 1 {
		t.Fatalf("entry did not survive: len=%d", s2.Len())
	}

	send := &fakeSender{}
	r2 := NewReporter(s2, send, testLog(), time.Minute)
	if n := r2.Drain(context.Background()); n != 1 {
		t.Fatalf("Drain delivered %d, want 1", n)
	}
	if send.completeCalls != 1 {
		t.Errorf("payload not replayed, calls=%d", send.completeCalls)
	}
}

// Delivery order matters: results should reach the manager oldest-first.
func TestDrainIsOldestFirst(t *testing.T) {
	store, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []int{1, 2, 3} {
		if err := store.Add(KindComplete, id, completePayload{}); err != nil {
			t.Fatal(err)
		}
		time.Sleep(2 * time.Millisecond)
	}
	entries, err := store.List()
	if err != nil {
		t.Fatal(err)
	}
	for i, want := range []int{1, 2, 3} {
		if entries[i].TaskID != want {
			t.Errorf("position %d = task %d, want %d", i, entries[i].TaskID, want)
		}
	}
}

// One unparseable file must not wedge delivery of everything behind it.
func TestCorruptEntryDoesNotBlockTheQueue(t *testing.T) {
	dir := t.TempDir()
	store, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Add(KindComplete, 1, completePayload{}); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(dir, "0000-corrupt.json", "{not json"); err != nil {
		t.Fatal(err)
	}

	send := &fakeSender{}
	rep := NewReporter(store, send, testLog(), time.Minute)
	if n := rep.Drain(context.Background()); n != 1 {
		t.Errorf("Drain delivered %d, want 1 (corrupt entry should be skipped)", n)
	}
}

func TestAgeReflectsHowLongAResultWasHeld(t *testing.T) {
	e := Entry{CreatedAt: time.Now().UTC().Add(-90 * time.Minute)}
	if got := e.Age(); got < 89*time.Minute || got > 91*time.Minute {
		t.Errorf("Age() = %v, want ~90m", got)
	}
}

// writeFile drops a raw file into an outbox dir, for the corrupt-entry case.
func writeFile(dir, name, body string) error {
	return os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600)
}

// A terminal-state 409 (cancelled/expired/completed) must be DROPPED — the
// manager already finalised the task so a retry can never succeed.
func TestDrain409TerminalStateDropsEntry(t *testing.T) {
	// queue under a transient error, then flip to the terminal 409 so Drain
	// (not Complete) makes the drop decision.
	send := &fakeSender{err: errors.New("connection refused")}
	rep, store := newTestReporter(t, send)
	rep.Complete(context.Background(), 42, transport.TaskResult{})
	if store.Len() != 1 {
		t.Fatalf("setup: len=%d", store.Len())
	}
	send.err = &transport.HTTPError{
		Path: "/workers/tasks/42/complete", StatusCode: http.StatusConflict,
		Body: `{"detail":"task already cancelled"}`,
	}
	if n := rep.Drain(context.Background()); n != 0 {
		t.Fatalf("Drain delivered %d, want 0 for terminal 409", n)
	}
	if store.Len() != 0 {
		t.Errorf("terminal 409 entry must be dropped: len=%d", store.Len())
	}

	// expired
	send2 := &fakeSender{err: errors.New("connection refused")}
	rep2, store2 := newTestReporter(t, send2)
	rep2.Complete(context.Background(), 43, transport.TaskResult{})
	if store2.Len() != 1 {
		t.Fatalf("setup: len=%d", store2.Len())
	}
	send2.err = &transport.HTTPError{
		Path: "/workers/tasks/42/complete", StatusCode: http.StatusConflict,
		Body: `{"detail":"task expired"}`,
	}
	if n := rep2.Drain(context.Background()); n != 0 {
		t.Fatalf("Drain delivered %d, want 0 for expired 409", n)
	}
	if store2.Len() != 0 {
		t.Errorf("expired 409 entry must be dropped: len=%d", store2.Len())
	}

	// completed (case-insensitive)
	send3 := &fakeSender{err: errors.New("connection refused")}
	rep3, store3 := newTestReporter(t, send3)
	rep3.Complete(context.Background(), 44, transport.TaskResult{})
	if store3.Len() != 1 {
		t.Fatalf("setup: len=%d", store3.Len())
	}
	send3.err = &transport.HTTPError{
		Path: "/workers/tasks/42/complete", StatusCode: http.StatusConflict,
		Body: `{"detail":"Task Already Completed"}`,
	}
	if n := rep3.Drain(context.Background()); n != 0 {
		t.Fatalf("Drain delivered %d, want 0 for Completed 409", n)
	}
	if store3.Len() != 0 {
		t.Errorf("completed 409 entry must be dropped: len=%d", store3.Len())
	}
}

// A transient 409 (lock contention, brief API restart) must be RETRIED
// — the manager will accept the report once the contention clears.
func TestDrain409TransientRetries(t *testing.T) {
	send := &fakeSender{err: errors.New("connection refused")}
	rep, store := newTestReporter(t, send)
	rep.Complete(context.Background(), 42, transport.TaskResult{})
	if store.Len() != 1 {
		t.Fatalf("setup: len=%d", store.Len())
	}
	send.err = &transport.HTTPError{
		Path: "/workers/tasks/42/complete", StatusCode: http.StatusConflict,
		Body: `{"detail":"database lock contention"}`,
	}
	if n := rep.Drain(context.Background()); n != 0 {
		t.Fatalf("Drain delivered %d, want 0 for transient 409", n)
	}
	if store.Len() != 1 {
		t.Fatalf("transient 409 must stay queued: len=%d", store.Len())
	}
}
// logCapture collects what a reporter logged so a test can assert on it.
type logCapture struct {
	buf []byte
}

func (c *logCapture) Write(p []byte) (int, error) {
	c.buf = append(c.buf, p...)
	return len(p), nil
}

func captureLog() (*slog.Logger, *logCapture) {
	c := &logCapture{}
	return slog.New(slog.NewTextHandler(c, nil)), c
}

// row 4: Add must fsync the entry file BEFORE the rename that commits it, and
// the directory after. Without the fsync a power loss can leave the renamed
// .json zero-length, and List skips a zero-length file silently, so the queued
// result would be gone with nothing to show for it. syncFile is the package
// seam; count its calls.
func TestAddFsyncsBeforeRename(t *testing.T) {
	orig := syncFile
	var names []string
	syncFile = func(f *os.File) error {
		names = append(names, f.Name())
		return orig(f)
	}
	t.Cleanup(func() { syncFile = orig })

	store, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Add(KindComplete, 5, completePayload{}); err != nil {
		t.Fatalf("Add: %v", err)
	}
	if len(names) != 2 {
		t.Fatalf("Add fsynced %d times, want 2 (entry file then dir): %v",
			len(names), names)
	}
	if !strings.HasSuffix(names[0], ".tmp") {
		t.Errorf("first fsync was %q, want the temp file before the rename", names[0])
	}
	if names[1] != store.Dir() {
		t.Errorf("second fsync was %q, want the outbox dir", names[1])
	}
}

// row 8: a 401 at report time is credential state (the token expired or was
// rotated while the worker ran), NOT a manager decision. The result must be
// queued, not discarded. 403 stays permanent (TestPermanentRejectionIsNotQueued).
func TestUnauthorizedAtReportTimeIsQueued(t *testing.T) {
	send := &fakeSender{err: &transport.HTTPError{
		Path: "/workers/tasks/42/complete", StatusCode: http.StatusUnauthorized,
		Body: "token expired",
	}}
	rep, store := newTestReporter(t, send)

	rep.Complete(context.Background(), 42, transport.TaskResult{LogTail: "done"})

	if store.Len() != 1 {
		t.Fatalf("a 401 must be queued for retry, not discarded: len=%d", store.Len())
	}
}

// row 9: same on replay. A worker that restarted with an expired token must
// keep the held result (Bump), not drop it.
func TestQueuedEntrySurvivesUnauthorizedOnReplay(t *testing.T) {
	send := &fakeSender{err: errors.New("connection refused")}
	rep, store := newTestReporter(t, send)
	rep.Complete(context.Background(), 42, transport.TaskResult{})
	if store.Len() != 1 {
		t.Fatalf("setup: len=%d", store.Len())
	}

	send.err = &transport.HTTPError{Path: "/complete", StatusCode: http.StatusUnauthorized,
		Body: "token expired"}
	if n := rep.Drain(context.Background()); n != 0 {
		t.Fatalf("Drain delivered %d, want 0 for a 401", n)
	}
	if store.Len() != 1 {
		t.Fatalf("a 401 on replay must leave the entry queued: len=%d", store.Len())
	}

	// The failure path keeps the entry too.
	rep2, store2 := newTestReporter(t, send)
	rep2.Failed(context.Background(), 43, "boom", true, "", false, "")
	if store2.Len() != 1 {
		t.Fatalf("a 401 on a failure report must be queued: len=%d", store2.Len())
	}
}

// The outbox predicate itself: 401 retryable, 403/404 still permanent, 408/429
// still invitations, 409 handled by its own case.
func TestOutboxPredicateClasses401Retryable(t *testing.T) {
	cases := []struct {
		status int
		want   bool
	}{{http.StatusUnauthorized, false}, {http.StatusForbidden, true},
		{http.StatusNotFound, true}, {http.StatusRequestTimeout, false},
		{http.StatusTooManyRequests, false}, {http.StatusConflict, true}}
	for _, c := range cases {
		err := &transport.HTTPError{Path: "/complete", StatusCode: c.status}
		if got := IsOutboxPermanentRejection(err); got != c.want {
			t.Errorf("HTTP %d: IsOutboxPermanentRejection=%v, want %v", c.status, got, c.want)
		}
		wrapped := fmt.Errorf("report complete: %w", &transport.HTTPError{
			Path: "/complete", StatusCode: c.status})
		if got := IsOutboxPermanentRejection(wrapped); got != c.want {
			t.Errorf("HTTP %d wrapped: IsOutboxPermanentRejection=%v, want %v",
				c.status, got, c.want)
		}
	}
}

// row 5: a corrupt entry is quarantined ONCE (renamed to *.bad) so it stops
// being re-read on every List forever, and the rest of the queue still drains.
func TestCorruptEntryIsQuarantined(t *testing.T) {
	dir := t.TempDir()
	store, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Add(KindComplete, 1, completePayload{}); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(dir, "0000-corrupt.json", "{not json"); err != nil {
		t.Fatal(err)
	}

	rep := NewReporter(store, &fakeSender{}, testLog(), time.Minute)
	if n := rep.Drain(context.Background()); n != 1 {
		t.Errorf("Drain delivered %d, want 1 (corrupt entry skipped)", n)
	}
	if _, err := os.Stat(filepath.Join(dir, "0000-corrupt.json.bad")); err != nil {
		t.Errorf("corrupt entry must be quarantined to *.bad: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "0000-corrupt.json")); err == nil {
		t.Errorf("the quarantined entry must not stay under its .json name")
	}
	// A second pass must not loop over it again: the queue is unchanged.
	if n := rep.Drain(context.Background()); n != 0 {
		t.Errorf("second Drain delivered %d, want 0", n)
	}
	if store.Len() != 0 {
		t.Errorf("valid entry should have been delivered: len=%d", store.Len())
	}
}

// row 6: a crash between the temp write and the rename leaves *.json.tmp that
// nothing else cleans. New sweeps those leftovers, and only those.
func TestNewRemovesStaleTempLeftovers(t *testing.T) {
	dir := t.TempDir()
	store, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Add(KindComplete, 1, completePayload{}); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(dir, "123-1-complete.json.tmp", "{half"); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(dir, "keep-me.txt", "not ours"); err != nil {
		t.Fatal(err)
	}

	reopened, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "123-1-complete.json.tmp")); err == nil {
		t.Errorf("stale *.json.tmp must be swept when the outbox opens")
	}
	if _, err := os.Stat(filepath.Join(dir, "keep-me.txt")); err != nil {
		t.Errorf("the sweep must not touch non-matching files: %v", err)
	}
	if n := reopened.Len(); n != 1 {
		t.Errorf("valid .json entries must survive the sweep: len=%d, want 1", n)
	}
}

// row 13: a failed drop on a terminal-state 409 must be logged, not swallowed.
// The store-level failure is forced deterministically: unlink never removes a
// directory, whatever the privileges, so Remove of such a path must report it.
func TestStoreSurfacesRemoveFailure(t *testing.T) {
	dir := t.TempDir()
	store, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	// Privilege-independent unlink failure: a name past PATH_MAX is ENAMETOOLONG,
	// not "already gone", so Remove must surface it (row 13 needs that error to
	// exist before the reporter can log it).
	tooLong := filepath.Join(dir, "0000-1-complete." + strings.Repeat("x", 4096) + ".json")
	e := Entry{file: tooLong}
	if err := store.Remove(e); err == nil {
		t.Errorf("Remove of an unlinkable path must report the error")
	}
}

// The terminal-409 drop path logs its decision and empties the queue.
func TestTerminalDropFailureIsLogged(t *testing.T) {
	send := &fakeSender{err: errors.New("connection refused")}
	store, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	log, cap := captureLog()
	rep := NewReporter(store, send, log, time.Minute)
	rep.Complete(context.Background(), 42, transport.TaskResult{})
	if store.Len() != 1 {
		t.Fatalf("setup: len=%d", store.Len())
	}
	send.err = &transport.HTTPError{Path: "/complete", StatusCode: http.StatusConflict,
		Body: `{"detail":"task already cancelled"}`}
	rep.Drain(context.Background())
	if !strings.Contains(string(cap.buf), "task terminal state confirmed") {
		t.Errorf("terminal 409 must be logged")
	}
	if store.Len() != 0 {
		t.Errorf("entry should have been dropped: len=%d", store.Len())
	}
}
