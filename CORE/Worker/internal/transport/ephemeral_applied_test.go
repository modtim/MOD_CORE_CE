package transport

import (
	"encoding/json"
	"testing"
)

// TestTaskResultEphemeralAppliedPresent covers WS1.38c: a task result
// carrying the post-clamp ephemeral values the worker applied serializes
// ephemeral_applied_shm_mb / ephemeral_applied_tmpfs_mb / ephemeral_clamped.
func TestTaskResultEphemeralAppliedPresent(t *testing.T) {
	result := TaskResult{
		Outputs:                 []OutputFile{},
		LogTail:                 "done",
		EphemeralAppliedShmMB:   512,
		EphemeralAppliedTmpfsMB: 300,
		EphemeralClamped:        true,
	}
	raw, err := json.Marshal(result)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if v, ok := got["ephemeral_applied_shm_mb"]; !ok || v != float64(512) {
		t.Errorf("ephemeral_applied_shm_mb = %v (ok=%v), want 512; raw=%s", v, ok, raw)
	}
	if v, ok := got["ephemeral_applied_tmpfs_mb"]; !ok || v != float64(300) {
		t.Errorf("ephemeral_applied_tmpfs_mb = %v (ok=%v), want 300; raw=%s", v, ok, raw)
	}
	if v, ok := got["ephemeral_clamped"]; !ok || v != true {
		t.Errorf("ephemeral_clamped = %v (ok=%v), want true; raw=%s", v, ok, raw)
	}
}

// TestTaskResultEphemeralAppliedAbsent covers the task-declared-no-ephemeral
// case: zero values must omit all three keys (omitempty), so a run that
// never applied an ephemeral override sends none of them.
func TestTaskResultEphemeralAppliedAbsent(t *testing.T) {
	result := TaskResult{
		Outputs: []OutputFile{},
		LogTail: "done",
	}
	raw, err := json.Marshal(result)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, key := range []string{"ephemeral_applied_shm_mb", "ephemeral_applied_tmpfs_mb", "ephemeral_clamped"} {
		if _, ok := got[key]; ok {
			t.Errorf("expected %s omitted when zero, got %s", key, raw)
		}
	}
}
