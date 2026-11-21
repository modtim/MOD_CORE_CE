package transport

// Retry and rejection semantics.
//
// The package had 1,430 lines and 0.0% statement coverage: its only test file
// round-trips TaskPayload through encoding/json, which exercises struct tags
// rather than any function here. That left the two decisions every API call the
// worker makes depends on — "should I try again?" and "how long do I keep
// trying?" — with nothing on them. Get `Permanent` wrong in one direction and
// the worker retries a refusal for ever; wrong in the other and it discards
// work the manager would have accepted a second later.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestHTTPError_Permanent(t *testing.T) {
	cases := []struct {
		status    int
		permanent bool
		why       string
	}{
		{http.StatusBadRequest, true, "malformed request will be malformed next time"},
		{http.StatusUnauthorized, true, "credentials do not improve by waiting"},
		{http.StatusForbidden, true, "not permitted stays not permitted"},
		{http.StatusNotFound, true, "the task is gone"},
		{http.StatusConflict, true, "already claimed by someone else"},
		{http.StatusUnprocessableEntity, true, "the payload is wrong, not late"},
		// The two 4xx that are invitations rather than refusals.
		{http.StatusRequestTimeout, false, "408 is explicitly try-again"},
		{http.StatusTooManyRequests, false, "429 is explicitly try-again-later"},
		// 5xx is the manager having a bad time, not a refusal.
		{http.StatusInternalServerError, false, "the manager may recover"},
		{http.StatusBadGateway, false, "a proxy hiccup is transient"},
		{http.StatusServiceUnavailable, false, "explicitly temporary"},
		{http.StatusGatewayTimeout, false, "upstream slow, not refusing"},
	}
	for _, c := range cases {
		e := &HTTPError{Path: "/workers/tasks/poll", StatusCode: c.status, Body: "x"}
		if got := e.Permanent(); got != c.permanent {
			t.Errorf("HTTP %d: Permanent()=%v, want %v — %s", c.status, got, c.permanent, c.why)
		}
	}
}

func TestIsPermanentRejection_UnwrapsWrappedErrors(t *testing.T) {
	// Callers wrap with %w on the way up, so the check has to survive nesting —
	// an unwrapped comparison would silently treat every wrapped 403 as
	// retryable.
	inner := &HTTPError{Path: "/p", StatusCode: http.StatusForbidden}
	wrapped := fmt.Errorf("report complete: %w", fmt.Errorf("transport: %w", inner))
	if !IsPermanentRejection(wrapped) {
		t.Error("a 403 wrapped twice must still read as permanent")
	}

	retryable := fmt.Errorf("poll: %w", &HTTPError{Path: "/p", StatusCode: http.StatusTooManyRequests})
	if IsPermanentRejection(retryable) {
		t.Error("429 must not read as permanent, however deeply wrapped")
	}
}

func TestIsPermanentRejection_NonHTTPErrorsAreNotPermanent(t *testing.T) {
	// A dial failure, a DNS blip or a closed connection is the network, not a
	// refusal. Treating those as permanent drops claimed work on a hiccup.
	for _, err := range []error{
		nil,
		errors.New("dial tcp: connection refused"),
		context.DeadlineExceeded,
		io.ErrUnexpectedEOF,
	} {
		if IsPermanentRejection(err) {
			t.Errorf("%v must not read as a permanent rejection", err)
		}
	}
}

func TestRetryBackoff_NoRetryOnFirstSuccess(t *testing.T) {
	calls := 0
	err := retryBackoff(context.Background(), time.Second, "op", quietLogger(), func() error {
		calls++
		return nil
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if calls != 1 {
		t.Errorf("called %d times, want exactly 1 — a success must not be retried", calls)
	}
}

func TestRetryBackoff_GivesUpAtDeadlineAndKeepsTheCause(t *testing.T) {
	// The deadline is checked after the attempt, so a maxDuration already in the
	// past returns immediately rather than sleeping a second first.
	cause := errors.New("the underlying reason")
	calls := 0
	start := time.Now()
	err := retryBackoff(context.Background(), time.Nanosecond, "claim", quietLogger(), func() error {
		calls++
		return cause
	})
	if err == nil {
		t.Fatal("expected failure once the deadline has passed")
	}
	if !errors.Is(err, cause) {
		t.Errorf("the original cause must survive to the caller, got: %v", err)
	}
	// The operator reading this line needs to know WHICH operation gave up.
	if !strings.Contains(err.Error(), "claim") {
		t.Errorf("error should name the operation, got: %v", err)
	}
	if calls != 1 {
		t.Errorf("called %d times, want 1", calls)
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Errorf("an expired deadline must not sleep first (took %v)", elapsed)
	}
}

func TestRetryBackoff_ContextCancellationBeatsTheBackoffSleep(t *testing.T) {
	// Shutdown must not wait out a 30s backoff. The select in the retry loop is
	// what makes worker shutdown prompt while an API call is failing.
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	start := time.Now()
	err := retryBackoff(ctx, time.Hour, "poll", quietLogger(), func() error {
		return errors.New("still failing")
	})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("want the context error back, got: %v", err)
	}
	if elapsed := time.Since(start); elapsed > 900*time.Millisecond {
		t.Errorf("cancellation waited out the 1s backoff (took %v)", elapsed)
	}
}

func TestRetryBackoff_RetriesUntilItSucceeds(t *testing.T) {
	// Costs one real backoff (1s) — the point is that a transient failure is
	// survived rather than surfaced, which is the whole reason this wrapper
	// exists.
	if testing.Short() {
		t.Skip("costs a 1s backoff")
	}
	calls := 0
	err := retryBackoff(context.Background(), 10*time.Second, "report", quietLogger(), func() error {
		calls++
		if calls < 2 {
			return errors.New("transient")
		}
		return nil
	})
	if err != nil {
		t.Fatalf("should have succeeded on the second attempt: %v", err)
	}
	if calls != 2 {
		t.Errorf("called %d times, want 2", calls)
	}
}
