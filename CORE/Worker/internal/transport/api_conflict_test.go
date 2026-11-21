package transport

// A 409 from POST /started is terminal: the manager already finalised the task
// (cancelled/expired/failed, or the Fortress compliance gate refused it with
// detail="compliance_gate_refused"). Retrying it is pointless AND, worse, the
// caller would otherwise go on to start the container for a task the API said
// must not run. These tests pin the retry policy of the /started path:
//
//   * 409 -> exactly ONE request, and the raw HTTPError survives UNWRAPPED so
//     the caller can see it (IsConflict true) and read the refusal reason
//     (ConflictDetail) without logging the rest of the body;
//   * 503 -> still retried (the manager may be restarting);
//   * 429 -> still retried (a rate limit is an invitation to try again);
//   * 408 -> still retried (same class as 429).

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

// conflictLogger keeps the retry WARN lines the loop emits out of test output.
func conflictLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// postServer serves any path with the given status sequence: statuses[i] for
// the i-th request (the last one repeats forever). It reports how many
// requests actually arrived, which is the whole point — a 409 must produce
// exactly one, while 503/429/408 must produce more than one.
func postServer(t *testing.T, statuses []int) (string, *[]int) {
	hits := []int{}
	cycle := statuses
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen := len(hits)
		hits = append(hits, seen)
		status := cycle[len(cycle)-1]
		if seen < len(cycle) {
			status = cycle[seen]
		}
		w.WriteHeader(status)
		if status == http.StatusConflict {
			_, _ = io.WriteString(w, `{"detail":"compliance_gate_refused"}`)
		} else {
			_, _ = io.WriteString(w, `{"detail":"transient"}`)
		}
	}))
	t.Cleanup(srv.Close)
	return srv.URL, &hits
}

// TestReportStarted409IsTerminal drives the real ReportStarted path against a
// manager that answers 409 + the compliance-gate detail. Exactly one request
// must arrive and the caller must be able to recognise the refusal.
func TestReportStarted409IsTerminal(t *testing.T) {
	url, hits := postServer(t, []int{http.StatusConflict})
	tp := NewAPITransport(url, "test-worker", "tok", nil, conflictLogger())
	err := tp.ReportStarted(context.Background(), 41, 0)
	if err == nil {
		t.Fatalf("a 409 from /started must surface as an error")
	}
	if !IsConflict(err) {
		t.Fatalf("IsConflict must recognise the 409, got: %v", err)
	}
	if ConflictDetail(err) != "compliance_gate_refused" {
		t.Errorf("ConflictDetail must surface the refusal reason, got %q", ConflictDetail(err))
	}
	if len(*hits) != 1 {
		t.Errorf("a 409 must produce exactly 1 request, got %d", len(*hits))
	}
}

// TestReportStarted503StillRetried: a 5xx is "the manager may be restarting",
// so the retry loop must still run — the second attempt succeeds here.
func TestReportStarted503StillRetried(t *testing.T) {
	url, hits := postServer(t, []int{http.StatusServiceUnavailable, http.StatusOK})
	tp := NewAPITransport(url, "test-worker", "tok", nil, conflictLogger())
	if err := tp.ReportStarted(context.Background(), 42, 0); err != nil {
		t.Errorf("the retry must eventually land, got: %v", err)
	}
	if len(*hits) < 2 {
		t.Errorf("a 503 must be retried (got %d requests)", len(*hits))
	}
}

// TestReportStarted429StillRetried: 429 is a rate limit, an invitation to try
// again — the 4xx no-retry rule must NOT swallow it.
func TestReportStarted429StillRetried(t *testing.T) {
	url, hits := postServer(t, []int{http.StatusTooManyRequests, http.StatusOK})
	tp := NewAPITransport(url, "test-worker", "tok", nil, conflictLogger())
	if err := tp.ReportStarted(context.Background(), 43, 0); err != nil {
		t.Errorf("the retry must eventually land, got: %v", err)
	}
	if len(*hits) < 2 {
		t.Errorf("a 429 must be retried (got %d requests)", len(*hits))
	}
}

// TestReportStarted408StillRetried: 408 (request timeout) belongs to the same
// "try again" class as 429.
func TestReportStarted408StillRetried(t *testing.T) {
	url, hits := postServer(t, []int{http.StatusRequestTimeout, http.StatusOK})
	tp := NewAPITransport(url, "test-worker", "tok", nil, conflictLogger())
	if err := tp.ReportStarted(context.Background(), 44, 0); err != nil {
		t.Errorf("the retry must eventually land, got: %v", err)
	}
	if len(*hits) < 2 {
		t.Errorf("a 408 must be retried (got %d requests)", len(*hits))
	}
}
