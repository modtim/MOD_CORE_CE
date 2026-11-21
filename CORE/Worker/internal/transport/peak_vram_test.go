package transport

import (
	"encoding/json"
	"testing"
)

// TestTaskResultPeakVRAMPresent covers 14.6b-2: a task result carrying a
// sampled VRAM peak serializes peak_vram_mb.
func TestTaskResultPeakVRAMPresent(t *testing.T) {
	result := TaskResult{
		Outputs:    []OutputFile{},
		LogTail:    "done",
		PeakVRAMMB: 8192,
	}
	raw, err := json.Marshal(result)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	v, ok := got["peak_vram_mb"]
	if !ok {
		t.Fatalf("expected peak_vram_mb present, got %s", raw)
	}
	if v != float64(8192) {
		t.Errorf("peak_vram_mb = %v, want 8192", v)
	}
}

// TestTaskResultPeakVRAMAbsent covers the non-GPU / never-sampled case: the
// zero value must omit the key entirely (omitempty), not send 0 — a task
// on a machine with no GPU should not read as "GPU task used 0 MiB".
func TestTaskResultPeakVRAMAbsent(t *testing.T) {
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
	if _, ok := got["peak_vram_mb"]; ok {
		t.Errorf("expected peak_vram_mb omitted when zero, got %s", raw)
	}
}
