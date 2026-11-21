package transport

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// BUG.B83 - the fetch-loop liveness stamp (heartbeat.MarkFetchLoopIteration,
// read by cmd/worker's green-but-silent watchdog as hb.FetchLoopAge()) must be
// refreshed on EVERY completed /workers/tasks/poll round-trip, whatever its
// outcome. It used to be touched only by cmd/worker's job loop, and an idle
// EMBEDDED worker never returns control there: DirectTransport.WaitForTask
// owns the whole idle wait (pub/sub + its own ticker + the API long-poll), so
// the watchdog measured ~11 minutes of "no fetch" on a worker the access log
// shows polling every ~30s, force-exited it, and orphaned the claim the API
// had just handed out (tasks 49958/49959, persistent-stack-deploy, EXPIRED
// "Claim expired after max attempts").
//
// newStampingTransport wires an APITransport to an httptest server whose reply
// is scripted by status/body, and registers a marker that counts refreshes -
// exactly what heartbeat.OnFetchLoopMarker hands the transport (a closure over
// the heartbeat's own atomic). polls records every request body that reached
// the server, i.e. every poll the transport actually made.
func newStampingTransport(t *testing.T, status int, body string, polls *[][]byte) (*APITransport, *int) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		*polls = append(*polls, b)
		if status != 0 {
			w.WriteHeader(status)
		}
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)

	marks := new(int)
	transport := NewAPITransport(srv.URL, "test-worker", "tok", nil, slog.Default())
	transport.SetFetchLoopMarker(func() { *marks += 1 })
	return transport, marks
}

// TestPollNoTaskRefreshesStamp is the field false positive: an idle worker
// polling an empty queue every ~30s (all HTTP 200) kept the watchdog's stamp
// stale, so it killed a healthy worker every ~11 minutes.
func TestPollNoTaskRefreshesStamp(t *testing.T) {
	var polls [][]byte
	transport, marks := newStampingTransport(t, http.StatusOK, "{}", &polls)
	task, err := transport.WaitForTask(context.Background())
	if err != nil {
		t.Fatalf("WaitForTask on empty queue: %v", err)
	}
	if task != nil {
		t.Fatalf("empty-queue poll must return no task: task=%v", task)
	}
	if len(polls) != 1 {
		t.Fatalf("expected one poll, got %d", len(polls))
	}
	if *marks != 1 {
		t.Fatalf("empty-queue poll must refresh the stamp: marks=%d, want 1", *marks)
	}
}

// TestPollTaskRefreshesStamp covers the claim path: the stamp must be fresh
// BEFORE main.go starts the job goroutine, otherwise a claim handed out on
// this poll can be orphaned by a watchdog exit (BUG.B83's 49958/49959).
func TestPollTaskRefreshesStamp(t *testing.T) {
	var polls [][]byte
	body := `{"task_id": 49959, "queue_name": "build_farm"}`
	transport, marks := newStampingTransport(t, http.StatusOK, body, &polls)
	task, err := transport.WaitForTask(context.Background())
	if err != nil {
		t.Fatalf("WaitForTask with a task: %v", err)
	}
	if task == nil {
		t.Fatalf("poll returned no task, want 49959")
	}
	if task.ID != 49959 {
		t.Fatalf("task.ID = %d, want 49959", task.ID)
	}
	if *marks != 1 {
		t.Fatalf("claimed-task poll must refresh the stamp: marks=%d, want 1", *marks)
	}
}

// TestPollHTTPErrorRefreshesStamp is the API-restart case: nginx 502/5xx
// while the API restarts or deploys. The API answered, so the loop is
// alive and the stamp must move; only main.go's poll-error backoff decides
// retry cadence. A 5xx left the stamp stale before this fix.
func TestPollHTTPErrorRefreshesStamp(t *testing.T) {
	var polls [][]byte
	transport, marks := newStampingTransport(t, http.StatusBadGateway, "upstream busy", &polls)
	task, err := transport.WaitForTask(context.Background())
	if err == nil {
		t.Fatalf("a 502 poll must surface as an error, got task=%v", task)
	}
	if task != nil {
		t.Fatalf("a 502 poll must not return a task")
	}
	if len(polls) != 1 {
		t.Fatalf("expected one poll, got %d", len(polls))
	}
	if *marks != 1 {
		t.Fatalf("HTTP-error poll must still refresh the stamp: marks=%d, want 1", *marks)
	}
}

// TestNonPollCallsDoNotRefreshStamp keeps the watchdog's teeth: the refresh
// is poll-scoped, so a loop wedged on anything else keeps a stale stamp and
// still trips. A report round-trip must not be mistaken for fetch-loop life.
func TestNonPollCallsDoNotRefreshStamp(t *testing.T) {
	var polls [][]byte
	transport, marks := newStampingTransport(t, http.StatusOK, "{}", &polls)
	if err := transport.ReportStarted(context.Background(), 7, 0); err != nil {
		t.Fatalf("ReportStarted: %v", err)
	}
	if *marks != 0 {
		t.Fatalf("a non-poll call must not refresh the stamp: marks=%d, want 0", *marks)
	}
}

// emptyOKHandler answers 200 with an empty JSON object - the API's no-task
// poll reply shape - for tests that need a transport with NO stamp wired.
func emptyOKHandler(w http.ResponseWriter, r *http.Request) {
	w.Write([]byte("{}"))
}

// TestUnwiredTransportPollIsInert covers the nil-marker case: the dedicated
// run-token minter transport in main.go is deliberately NOT wired to a
// stamp (it never polls, and refreshing from it would mask a wedged fetch
// loop), and unit-test transports are never wired either. A poll through
// one must be inert, not a nil-call panic.
func TestUnwiredTransportPollIsInert(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(emptyOKHandler))
	t.Cleanup(srv.Close)
	transport := NewAPITransport(srv.URL, "test-worker", "tok", nil, slog.Default())
	task, err := transport.WaitForTask(context.Background())
	if err != nil {
		t.Fatalf("unwired poll: %v", err)
	}
	if task != nil {
		t.Fatalf("unwired poll on an empty queue must return no task")
	}
}

// TestBlockedLoopStillTrips is the watchdog's real job, which this fix must
// NOT blunt: work that is NOT a completed poll round-trip (a wedged
// executor hop, a blocking call, a hung lock) leaves the stamp exactly where
// the last poll left it, so hb.FetchLoopAge() keeps growing and
// fetchLoopStalled still reports the stall. Poll-scoped refresh is the
// whole contract - a broader refresh would be the threshold raise that
// would have hidden this bug instead of fixing it.
func TestBlockedLoopStillTrips(t *testing.T) {
	var polls [][]byte
	transport, marks := newStampingTransport(t, http.StatusOK, "{}", &polls)
	task, err := transport.WaitForTask(context.Background())
	if err != nil {
		t.Fatalf("first poll: %v", err)
	}
	if task != nil {
		t.Fatalf("first poll on an empty queue must return no task: task=%v", task)
	}
	if *marks != 1 {
		t.Fatalf("first poll must refresh once: marks=%d, want 1", *marks)
	}
	for i := 0; i < 20; i++ {
		if err := transport.ReportStarted(context.Background(), 7, 0); err != nil {
			t.Fatalf("ReportStarted: %v", err)
		}
	}
	if *marks != 1 {
		t.Fatalf("stamps must stay frozen while the loop is blocked: marks=%d, want 1", *marks)
	}
	// The stamp is frozen at 1 refresh, i.e. exactly as stale as the last
	// poll left it: hb.FetchLoopAge() keeps growing and cmd/worker's
	// fetchLoopStalled(age, threshold) still trips on it (covered there by
	// fetch_loop_watchdog_test.go).
	_ = time.Minute
}
