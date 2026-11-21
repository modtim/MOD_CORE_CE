package transport

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"log/slog"
	"testing"
	"time"
)

// newTestAPITransport wires an APITransport at an httptest server that records
// the last request body it received, so tests can inspect exactly what the
// transport POSTed to /workers/tasks/{id}/failed.
func newTestAPITransport(t *testing.T, captured *[]byte) *APITransport {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		*captured = body
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	return NewAPITransport(srv.URL, "test-worker", "tok", nil, slog.Default())
}

// TestReportFailedWithEgressNilMatchesReportFailed verifies that ReportFailed
// with a nil egress POSTs the exact same JSON shape as ReportFailed — the
// fail-path must be indistinguishable from before egress support was added.
func TestReportFailedWithEgressNilMatchesReportFailed(t *testing.T) {
	// Body produced by ReportFailed (the original contract).
	var expected []byte
	{
		t0 := newTestAPITransport(t, &expected)
		if err := t0.ReportFailed(context.Background(), 7, "boom", true, "logline", false, "stacktrace"); err != nil {
			t.Fatalf("ReportFailed: %v", err)
		}
	}

	var got []byte
	t1 := newTestAPITransport(t, &got)
	if err := t1.ReportFailedWithEgress(context.Background(), 7, "boom", true, "logline", false, "stacktrace", nil); err != nil {
		t.Fatalf("ReportFailedWithEgress: %v", err)
	}

	if string(got) != string(expected) {
		t.Errorf("nil-egress body differs from ReportFailed.\n expected: %s\n got:      %s", expected, got)
	}
	if containsKey(string(got), "egress") {
		t.Errorf("nil egress must omit \"egress\" key, got %s", got)
	}
}

// TestReportFailedWithEgressNonNilAddsKey verifies a non-nil EgressReport is
// folded into the JSON body under the "egress" key.
func TestReportFailedWithEgressNonNilAddsKey(t *testing.T) {
	var got []byte
	t1 := newTestAPITransport(t, &got)
	report := &EgressReport{Mode: "egress-gated", AllowedHosts: []string{"api:443"}}
	if err := t1.ReportFailedWithEgress(context.Background(), 9, "boom", false, "logline", true, "detail", report); err != nil {
		t.Fatalf("ReportFailedWithEgress: %v", err)
	}

	if !containsKey(string(got), "egress") {
		t.Errorf("non-nil egress must add \"egress\" key, got %s", got)
	}

	// The full body must still decode to a map carrying every failed-field plus
	// the egress object, and the egress value itself must round-trip.
	var payload map[string]any
	if err := json.Unmarshal(got, &payload); err != nil {
		t.Fatalf("body not valid JSON: %v\n%s", err, got)
	}
	if payload["error_message"] != "boom" || payload["should_retry"] != false || payload["oom_killed"] != true || payload["error_detail"] != "detail" {
		t.Errorf("core failed-fields mismatch: %v", payload)
	}
	egress, ok := payload["egress"].(map[string]any)
	if !ok {
		t.Fatalf("egress key missing or not an object, got %T", payload["egress"])
	}
	if egress["mode"] != "egress-gated" {
		t.Errorf("egress.mode mismatch: %v", egress["mode"])
	}
}

// TestReportFailedWithEgressRetryStops verifies the retry loop settles when the
// server returns 200 (no spurious retries, no hang).
func TestReportFailedWithEgressRetryStops(t *testing.T) {
	var got []byte
	t1 := newTestAPITransport(t, &got)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := t1.ReportFailedWithEgress(ctx, 1, "boom", true, "log", false, "", nil); err != nil {
		t.Fatalf("ReportFailedWithEgress: %v", err)
	}
	if !containsKey(string(got), "error_message") {
		t.Errorf("expected body to carry error_message, got %s", got)
	}
}
