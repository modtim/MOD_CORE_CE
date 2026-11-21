package transport

import "testing"

// k8s-reduction-engine W14: HTTP-poll and DirectTransport workers both claim
// through mapPollResponse, so executor_backend must survive the round-trip.
func TestMapPollResponseKeepsExecutorBackend(t *testing.T) {
	id := 7
	task, err := mapPollResponse(&id, nil, nil, nil, nil, nil, map[string]interface{}{
		"executor_backend": "k8s",
	})
	if err != nil {
		t.Fatal(err)
	}
	if task.Payload.ExecutorBackend != "k8s" {
		t.Fatalf("ExecutorBackend = %q, want k8s", task.Payload.ExecutorBackend)
	}

	task, _ = mapPollResponse(&id, nil, nil, nil, nil, nil, map[string]interface{}{})
	if task.Payload.ExecutorBackend != "" {
		t.Fatalf("ExecutorBackend = %q, want empty (docker)", task.Payload.ExecutorBackend)
	}
}
