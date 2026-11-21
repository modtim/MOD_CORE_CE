package transport

import (
	"encoding/json"
	"testing"
)

// TestEgressReport covers JSON round-tripping of the egress report shape and
// that a nil TaskResult.Egress omits the key entirely.
func TestEgressReport(t *testing.T) {
	report := &EgressReport{
		Mode:          "egress-gated",
		AllowedHosts:  []string{"api:443", "registry:5000"},
		Denied:        []EgressDenied{{Host: "example.com", Port: 443, Reason: "not in allowlist", Count: 3}},
		DeniedTruncated: true,
	}

	raw, err := json.Marshal(report)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	for _, key := range []string{"mode", "allowed_hosts", "denied", "denied_truncated"} {
		if _, ok := got[key]; !ok {
			t.Errorf("expected JSON key %q present, got %s", key, raw)
		}
	}
	if got["denied_truncated"] != true {
		t.Errorf("denied_truncated should serialize to true, got %v", got["denied_truncated"])
	}

	// snake_case key names must match the EGR.11a API shape.
	if _, ok := got["deniedTruncated"]; ok {
		t.Errorf("expected snake_case key denied_truncated, found camelCase deniedTruncated")
	}

	// Round-trip into the concrete type.
	var out EgressReport
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("round-trip unmarshal: %v", err)
	}
	if out.Mode != report.Mode || len(out.AllowedHosts) != 2 || out.AllowedHosts[0] != "api:443" || out.AllowedHosts[1] != "registry:5000" {
		t.Errorf("allowed hosts mismatch: %+v", out.AllowedHosts)
	}
	if len(out.Denied) != 1 || out.Denied[0].Host != "example.com" || out.Denied[0].Port != 443 || out.Denied[0].Reason != "not in allowlist" || out.Denied[0].Count != 3 {
		t.Errorf("denied mismatch: %+v", out.Denied)
	}
	if !out.DeniedTruncated {
		t.Errorf("DeniedTruncated should survive round-trip")
	}

	// Nil Egress on TaskResult must omit the key.
	task := TaskResult{Outputs: []OutputFile{{FileName: "a.txt"}}, LogTail: "log"}
	taskRaw, err := json.Marshal(task)
	if err != nil {
		t.Fatalf("marshal task: %v", err)
	}
	if containsKey(string(taskRaw), "egress") {
		t.Errorf("nil Egress should omit \"egress\" key, got %s", taskRaw)
	}

	// An EgressReport with no denies should omit denied / denied_truncated.
	empty := &EgressReport{Mode: "open", AllowedHosts: []string{"api:443"}}
	emptyRaw, err := json.Marshal(empty)
	if err != nil {
		t.Fatalf("marshal empty: %v", err)
	}
	var emptyMap map[string]any
	if err := json.Unmarshal(emptyRaw, &emptyMap); err != nil {
		t.Fatalf("unmarshal empty: %v", err)
	}
	if _, ok := emptyMap["denied"]; ok {
		t.Errorf("empty Denied should omit \"denied\" key, got %s", emptyRaw)
	}
	if _, ok := emptyMap["denied_truncated"]; ok {
		t.Errorf("false DeniedTruncated should omit \"denied_truncated\" key, got %s", emptyRaw)
	}
}

// containsKey reports whether a marshaled JSON object string contains the given
// top-level key (as a quoted key, not as a substring of a value).
func containsKey(data, key string) bool {
	needle := "\"" + key + "\":"
	for i := 0; i+len(needle) <= len(data); i++ {
		if data[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
