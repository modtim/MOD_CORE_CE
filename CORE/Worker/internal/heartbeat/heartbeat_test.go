package heartbeat

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// TestWorkerIDDecode verifies that the worker_id field decodes correctly from
// a heartbeat response payload, matching the wire shape the API sends.
func TestWorkerIDDecode(t *testing.T) {
	t.Run("non-zero worker_id decodes", func(t *testing.T) {
		var hbResp struct {
			WorkerID int64 `json:"worker_id"`
		}
		if err := json.Unmarshal([]byte(`{"worker_id": 42}`), &hbResp); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if hbResp.WorkerID != 42 {
			t.Fatalf("expected 42, got %d", hbResp.WorkerID)
		}
	})

	t.Run("absent worker_id decodes as zero", func(t *testing.T) {
		var hbResp struct {
			WorkerID int64 `json:"worker_id"`
		}
		if err := json.Unmarshal([]byte(`{}`), &hbResp); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if hbResp.WorkerID != 0 {
			t.Fatalf("expected absent field to decode as 0, got %d", hbResp.WorkerID)
		}
	})
}

// TestAdoptWorkerID exercises the real production guard (adoptWorkerID, the
// function send() calls) rather than re-implementing its logic in the test.
func TestAdoptWorkerID(t *testing.T) {
	t.Run("first positive id is adopted", func(t *testing.T) {
		workerID.Store(0)
		if ok := adoptWorkerID(42); !ok {
			t.Fatalf("adoptWorkerID(42) = false, want true")
		}
		if got := WorkerID(); got != 42 {
			t.Fatalf("WorkerID() = %d, want 42", got)
		}
	})

	t.Run("zero id is ignored and reports no adoption", func(t *testing.T) {
		workerID.Store(0)
		if ok := adoptWorkerID(0); ok {
			t.Fatalf("adoptWorkerID(0) = true, want false")
		}
		if got := WorkerID(); got != 0 {
			t.Fatalf("WorkerID() = %d, want 0", got)
		}
	})

	t.Run("known id is never overwritten by a later zero", func(t *testing.T) {
		workerID.Store(99)
		if ok := adoptWorkerID(0); ok {
			t.Fatalf("adoptWorkerID(0) = true, want false (should not touch a known id)")
		}
		if got := WorkerID(); got != 99 {
			t.Fatalf("WorkerID() = %d, want 99 (old API zero should not overwrite)", got)
		}
	})

	t.Run("known id is never overwritten by a different positive id", func(t *testing.T) {
		workerID.Store(99)
		if ok := adoptWorkerID(7); ok {
			t.Fatalf("adoptWorkerID(7) = true, want false (id already known)")
		}
		if got := WorkerID(); got != 99 {
			t.Fatalf("WorkerID() = %d, want 99 (known id must stick)", got)
		}
	})
}

// TestWorkerIDAtomicConcurrency verifies adoptWorkerID/WorkerID are safe for
// concurrent access and exactly one writer's id wins.
func TestWorkerIDAtomicConcurrency(t *testing.T) {
	workerID.Store(0)
	const goroutines = 100

	done := make(chan struct{})
	for i := 0; i < goroutines; i++ {
		go func(v int64) {
			adoptWorkerID(v)
			_ = WorkerID()
			done <- struct{}{}
		}(int64(i + 1))
	}

	timeout := time.After(5 * time.Second)
	for i := 0; i < goroutines; i++ {
		select {
		case <-done:
		case <-timeout:
			t.Fatal("timeout waiting for goroutines")
		}
	}

	if got := WorkerID(); got == 0 {
		t.Fatalf("WorkerID() = 0 after concurrent writes, expected a known id")
	}
}

// TestWorkerIDInitialZero verifies that WorkerID() starts at 0.
func TestWorkerIDInitialZero(t *testing.T) {
	workerID.Store(0)
	if got := WorkerID(); got != 0 {
		t.Fatalf("WorkerID() = %d, want 0 (initial)", got)
	}
}

// TestHostExecStatusProvider (993a): the omit-don't-fake contract for the
// new `host_exec` heartbeat key. Unset provider -> hostExecReport() is nil,
// which send() turns into KEY ABSENCE (never a zero-valued map): an older
// binary must not read as available or unavailable. A wired provider's map
// must round-trip into the payload unchanged.
func TestHostExecStatusProvider(t *testing.T) {
	t.Run("unset provider reports nil (key omitted)", func(t *testing.T) {
		orig := hostExecStatusProvider
		defer func() { hostExecStatusProvider = orig }()
		hostExecStatusProvider = nil
		if got := hostExecReport(); got != nil {
			t.Fatalf("hostExecReport() with no provider = %v, want nil (omit the key)", got)
		}
	})

	t.Run("wired provider map round-trips", func(t *testing.T) {
		orig := hostExecStatusProvider
		defer func() { hostExecStatusProvider = orig }()
		hostExecStatusProvider = func() map[string]interface{} {
			return map[string]interface{}{
				"available": false,
				"mode":      "",
				"code":      "helper_missing",
				"message":   "containerized worker without the mod-host-exec helper socket",
			}
		}
		got := hostExecReport()
		if got == nil {
			t.Fatalf("hostExecReport() with provider = nil, want the map")
		}
		if got["code"] != "helper_missing" || got["available"] != false {
			t.Fatalf("round-trip lost fields: %v", got)
		}
		// send() embeds it as data["host_exec"]; prove the shape encodes.
		encoded, err := json.Marshal(map[string]interface{}{"host_exec": got})
		if err != nil {
			t.Fatalf("encode: %v", err)
		}
		var payload map[string]interface{}
		if err := json.Unmarshal(encoded, &payload); err != nil {
			t.Fatalf("decode: %v", err)
		}
		inner, ok := payload["host_exec"].(map[string]interface{})
		if !ok || inner["code"] != "helper_missing" {
			t.Fatalf("payload key present with wrong shape: %v", payload)
		}
	})

	t.Run("nil provider omits the key in an encoded payload", func(t *testing.T) {
		orig := hostExecStatusProvider
		defer func() { hostExecStatusProvider = orig }()
		hostExecStatusProvider = nil
		data := map[string]interface{}{"worker_name": "w"}
		if hx := hostExecReport(); hx != nil {
			data["host_exec"] = hx
		}
		encoded, err := json.Marshal(data)
		if err != nil {
			t.Fatalf("encode: %v", err)
		}
		if strings.Contains(string(encoded), "host_exec") {
			t.Fatalf("encoded payload contains host_exec for nil provider: %s", encoded)
		}
	})
}
