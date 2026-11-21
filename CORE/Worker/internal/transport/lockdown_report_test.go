package transport

// GSEC FX.115 p2 — the per-container lockdown verdict rides the completion
// body as a flat "lockdown_report" object so the API can persist per
// (node kind, flag) what WOULD change under enforce without scraping the
// worker's log line. These tests pin the WIRE shape only (the verdict itself
// is node_lockdown.go's job, covered by its own tests):
//
//   * a TaskResult carrying a report serializes lockdown_report with the
//     exact keys planMap emits (node_kind + the per-flag verdict), so the
//     API's ingest sees the same spelling the log line uses;
//   * a TaskResult without one OMITS the key entirely (omitempty on a nil
//     map), so native/k8s/stack-op completions stay byte-for-byte unchanged;
//   * the report coexists with hf_cache_report — the old ReportComplete shape
//     rebuilt the body per optional field and would have dropped one of the
//     two, so the fold-into-one-map shape is pinned here too.

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestTaskResultLockdownReportPresent: the verdict serializes as an object
// under lockdown_report, keyed by node_kind like planMap/planFields.
func TestTaskResultLockdownReportPresent(t *testing.T) {
result := TaskResult{
Outputs: []OutputFile{},
LockdownReport: map[string]any{
"node_kind": "video_upscale",
"exempt": false,
"nonroot_missing": true,
"nonroot_would_apply": true,
"nonroot_applied": false,
},
}
encoded, err := json.Marshal(result)
if err != nil {
t.Fatalf("marshal: %v", err)
}
var got map[string]any
if err := json.Unmarshal(encoded, &got); err != nil {
t.Fatalf("unmarshal: %v", err)
}
inner, ok := got["lockdown_report"]
if !ok {
t.Errorf("lockdown_report missing, got %s", encoded)
return
}
m, ok := inner.(map[string]any)
if !ok {
t.Errorf("lockdown_report is not an object, got %s", encoded)
return
}
if v, _ := m["node_kind"].(string); v != "video_upscale" {
t.Errorf("lockdown_report.node_kind = %v, want video_upscale", m["node_kind"])
}
if v, has := m["nonroot_would_apply"]; !has || v != true {
t.Errorf("nonroot_would_apply = %v (ok=%v), want true", m["nonroot_would_apply"], has)
}
}

// TestTaskResultLockdownReportAbsent: no node container ran, so the key is
// omitted entirely (omitempty on a nil map) — the ordinary completion body
// is unchanged for native/k8s/stack-op tasks.
func TestTaskResultLockdownReportAbsent(t *testing.T) {
result := TaskResult{
Outputs: []OutputFile{},
LogTail: "done",
}
encoded, err := json.Marshal(result)
if err != nil {
t.Fatalf("marshal: %v", err)
}
if strings.Contains(string(encoded), "lockdown_report") {
t.Errorf("expected lockdown_report omitted when nil, got %s", encoded)
}
}

// TestTaskResultLockdownReportWithHFCache: both optional reports ride the
// SAME body. ReportComplete used to rebuild the body per field, so a task
// carrying both silently lost one of them; the fold-into-one-map shape must
// keep both keys.
func TestTaskResultLockdownReportWithHFCache(t *testing.T) {
result := TaskResult{
Outputs: []OutputFile{},
HFCacheReport: &HFCacheReport{Moved: 1, Verified: 1, Quarantined: 0},
LockdownReport: map[string]any{"node_kind": "hf_cache_relayout", "exempt": true},
}
encoded, err := json.Marshal(result)
if err != nil {
t.Fatalf("marshal: %v", err)
}
var got map[string]any
if err := json.Unmarshal(encoded, &got); err != nil {
t.Fatalf("unmarshal: %v", err)
}
for _, key := range []string{"hf_cache_report", "lockdown_report"} {
if _, ok := got[key]; !ok {
t.Errorf("%s missing when both reports present, got %s", key, encoded)
}
}
}
