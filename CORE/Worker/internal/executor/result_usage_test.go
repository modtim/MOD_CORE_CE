package executor

// 30.6.11 c11-J3 - the pure half of the stack-model-call usage read
// (parseUsageJSON) and the wire shape it feeds TaskResult.UsageTokens.
// Exercised WITHOUT a Docker daemon: readUsageJSON is only the readResultJSONFile
// cat plus this parser, so the S9 bounds are testable directly.

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestU1_ValidOpenAIUsageCounts(t *testing.T) {
	// The shape stack-model-call/entrypoint.py:200-203 actually writes:
	// {"model": ..., "usage": <raw OpenAI usage>, "finish_reason": ...}.
	body := []byte(`{"model":"localai/llama-3:8b","finish_reason":"stop",` +
		`"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}`)
	usage := parseUsageJSON(body)
	if usage == nil {
		t.Fatalf("U1: valid usage parsed as nil")
	}
	if len(usage.Models) != 1 {
		t.Fatalf("U1: want exactly 1 model entry, got %d", len(usage.Models))
	}
	m := usage.Models[0]
	if m.Model != "localai/llama-3:8b" || m.PromptTokens != 7 ||
		m.CompletionTokens != 3 || m.TotalTokens != 10 {
		t.Fatalf("U1: want 7/3/10 for localai/llama-3:8b, got %s %d/%d/%d",
			m.Model, m.PromptTokens, m.CompletionTokens, m.TotalTokens)
	}
}

func TestU2_MissingUsageKeyIsNil(t *testing.T) {
	// The _ok() helper in stack-model-call/tests/test_entrypoint.py fakes a 200
	// with no usage key at all - the entrypoint writes "usage": null.
	bodies := []string{
		`{"model":"m","usage":null,"finish_reason":"stop"}`,
		`{"model":"m","finish_reason":"stop"}`,
		`{"model":"m","usage":{},"finish_reason":"stop"}`,
	}
	for _, body := range bodies {
		if parseUsageJSON([]byte(body)) != nil {
			t.Fatalf("U2: usage-less result.json must parse as nil")
		}
	}
}

func TestU3_OversizedFileIsIgnored(t *testing.T) {
	// S9: a tenant-built node can write an arbitrarily large result.json.
	// 70 KiB (over the 64 KiB cap) is ignored outright, never truncated-and-parsed.
	big := []byte(`{"model":"m","usage":{"prompt_tokens":7,"completion_tokens":3,` +
		`"total_tokens":10},"note":"` + strings.Repeat("x", 70*1024) + `"}`)
	if parseUsageJSON(big) != nil {
		t.Fatalf("U3: 70 KiB result.json must be ignored")
	}
	// Just under the cap still parses (the cap is a cap, not a size budget).
	small := []byte(`{"model":"m","usage":{"prompt_tokens":1,"completion_tokens":1,` +
		`"total_tokens":2},"note":"` + strings.Repeat("y", 63*1024) + `"}`)
	if parseUsageJSON(small) == nil {
		t.Fatalf("U3: 63 KiB result.json must still parse")
	}
}

func TestU4_BadModelNameDropsWholeUsage(t *testing.T) {
	// S9/S8: the model name is the entry's identity. Outside [A-Za-z0-9._:/-]
	// (or over 128 chars) the WHOLE usage is dropped, not just the name -
	// counts with no trustworthy model would land on the wrong row.
	bad := []string{
		`{"model":"gpt\"},\"prompt_text\":\"secret","usage":{"prompt_tokens":7}}`,
		`{"model":"has space","usage":{"prompt_tokens":7}}`,
		`{"model":"","usage":{"prompt_tokens":7}}`,
		`{"model":"` + strings.Repeat("a", 129) + `","usage":{"prompt_tokens":7}}`,
	}
	for _, body := range bad {
		if parseUsageJSON([]byte(body)) != nil {
			t.Fatalf("U4: bad model name must drop the whole usage")
		}
	}
	// The allowed charset (including the CE default's repo@digest form) passes.
	if parseUsageJSON([]byte(`{"model":"A_b.9:/-x","usage":{"total_tokens":1}}`)) == nil {
		t.Fatalf("U4: allowed charset must parse")
	}
}

func TestU5_NegativeCountDropsThatFieldOnly(t *testing.T) {
	// A negative / >1e9 count drops THAT field; the rest still rides, and the
	// task is never failed over usage (the API's "422 for that field only" rule,
	// enforced worker-side so the API never sees the bad value).
	body := []byte(`{"model":"m","usage":{"prompt_tokens":-5,` +
		`"completion_tokens":3,"total_tokens":1000000000000}}`)
	usage := parseUsageJSON(body)
	if usage == nil {
		t.Fatalf("U5: partial usage must still report the in-range field")
	}
	m := usage.Models[0]
	if m.PromptTokens != 0 || m.CompletionTokens != 3 || m.TotalTokens != 0 {
		t.Fatalf("U5: want only completion_tokens=3 kept, got %d/%d/%d",
			m.PromptTokens, m.CompletionTokens, m.TotalTokens)
	}
	// A non-integer count (string / float / object) is dropped the same way,
	// and with nothing left in range there is nothing to report.
	weird := []byte(`{"model":"m","usage":{"prompt_tokens":"7","total_tokens":2.5}}`)
	if parseUsageJSON(weird) != nil {
		t.Fatalf("U5: non-integer counts must all be dropped => nil")
	}
	if parseUsageJSON([]byte(`{"model":"m","usage":{"prompt_tokens":-1}}`)) != nil {
		t.Fatalf("U5: all-out-of-range usage must be nil")
	}
	// images is optional and additive (the node's image count, when present).
	withImg := []byte(`{"model":"m","usage":{"prompt_tokens":7,"images":2}}`)
	u := parseUsageJSON(withImg)
	if u == nil || u.Models[0].Images != 2 {
		t.Fatalf("U5: images int must ride when present")
	}
}

func TestU6_NilUsageOmitsWireKey(t *testing.T) {
	// Old-API compat: a task that reported no usage must serialize EXACTLY as
	// before - no "usage_tokens" key at all (omitempty on a nil pointer).
	withoutUsage := transport.TaskResult{
		Outputs: []transport.OutputFile{},
		LogTail: "done",
	}
	withUsage := transport.TaskResult{
		Outputs:     []transport.OutputFile{},
		LogTail:     "done",
		UsageTokens: parseUsageJSON([]byte(`{"model":"m","usage":{"total_tokens":2}}`)),
	}
	plain, err := json.Marshal(withoutUsage)
	if err != nil {
		t.Fatalf("U6: marshal error: %v", err)
	}
	if strings.Contains(string(plain), "\"usage_tokens\"") {
		t.Fatalf("U6: nil usage must not emit a usage_tokens key")
	}
	data, err := json.Marshal(withUsage)
	if err != nil {
		t.Fatalf("U6: marshal error: %v", err)
	}
	wire := string(data)
	if !strings.Contains(wire, "\"usage_tokens\"") {
		t.Fatalf("U6: non-nil usage must emit usage_tokens")
	}
	want := `{"models":[{"model":"m","total_tokens":2}]}`
	usage := withUsage.UsageTokens
	usageJSON, err := json.Marshal(usage)
	if err != nil {
		t.Fatalf("U6: usage marshal error: %v", err)
	}
	if got := string(usageJSON); got != want {
		t.Fatalf("U6: usage_tokens wire shape want %s got %s", want, got)
	}
}
