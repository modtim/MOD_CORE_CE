package executor

// c11-J3b (30.6.11): the k8s task path reports stack-model-call token usage.
// The Docker path's twin (c11-J3) already reads /data/output/result.json off the
// processing container; here stage-out prints the SAME file under a SEPARATE
// prefix (stageOutUsagePrefix) so ParseStageOutUsage can meter it WITHOUT
// touching ParseStageOutLogs' outputs or the UPLOAD manifest. K4 is the proof
// that the outputs really are unaffected.

import (
	"encoding/base64"
	"strings"
	"testing"
)

func usageFixtureJSON() string {
	return `{"model":"m1","usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}`
}

func usageLogs(withUsage bool) string {
	// A result line so ParseStageOutLogs has real outputs to compare (K4), plus
	// the done marker it insists on.
	inner := base64.StdEncoding.EncodeToString([]byte(
		`{"outputs":[{"content_hash":"h1","file_name":"a.bin","file_extension":"bin","size_bytes":3}]}`))
	// The usage line is base64(result.json) exactly as stageOutScript emits it.
	b := base64.StdEncoding.EncodeToString([]byte(usageFixtureJSON()))
	s := "MOD_RESULT_JSON_B64:validation_result.json:" + inner + "\n"
	if withUsage {
		s += stageOutUsagePrefix + b + "\n"
	}
	return s + "MOD_STAGE_OUT_DONE\n"
}

// K1: a valid usage line yields the counts.
func TestK8sUsageReadsCounts(t *testing.T) {
	u := ParseStageOutUsage(usageLogs(true))
	if u == nil || len(u.Models) != 1 {
		t.Fatalf("usage must be read, got %v", u)
	}
	m := u.Models[0]
	if m.Model != "m1" || m.PromptTokens != 3 || m.CompletionTokens != 4 || m.TotalTokens != 7 {
		t.Fatalf("counts must survive the S9 gate, got model=%q p=%d c=%d t=%d",
			m.Model, m.PromptTokens, m.CompletionTokens, m.TotalTokens)
	}
}

// K2: no usage line at all -> nil.
func TestK8sUsageAbsentIsNil(t *testing.T) {
	if ParseStageOutUsage(usageLogs(false)) != nil {
		t.Fatal("no usage line must yield nil")
	}
}

// K3: bad base64, and a model name the S9 charset gate rejects, both -> nil.
func TestK8sUsageMalformedIsNil(t *testing.T) {
	bad := usageLogs(false) + stageOutUsagePrefix + "!!!not base64!!!\n" +
		"MOD_STAGE_OUT_DONE\n"
	if ParseStageOutUsage(bad) != nil {
		t.Fatal("bad base64 must yield nil")
	}
	// The model gate: "bad name" has a space, outside [A-Za-z0-9._:/-].
	inner := base64.StdEncoding.EncodeToString([]byte(
		`{"model":"bad name","usage":{"total_tokens":7}}`))
	gated := usageLogs(false) + stageOutUsagePrefix + inner + "\nMOD_STAGE_OUT_DONE\n"
	if ParseStageOutUsage(gated) != nil {
		t.Fatal("a model name the S9 gate rejects must yield nil")
	}
}

// K4: the usage line is invisible to ParseStageOutLogs - the outputs for the
// same logs with and without it are identical (the UPLOAD manifest path too).
func TestK8sUsageLineDoesNotChangeOutputs(t *testing.T) {
	withU, errA := ParseStageOutLogs(usageLogs(true))
	withoutU, errB := ParseStageOutLogs(usageLogs(false))
	if errA != nil || errB != nil {
		t.Fatalf("manifest must still parse, got %v / %v", errA, errB)
	}
	if len(withU) != len(withoutU) {
		t.Fatalf("output count changed: %d vs %d", len(withU), len(withoutU))
	}
	for i := range withU {
		a, b2 := withU[i], withoutU[i]
		if a.ContentHash != b2.ContentHash || a.FileName != b2.FileName ||
			a.FileExtension != b2.FileExtension || a.SizeBytes != b2.SizeBytes {
			t.Fatalf("outputs differ at %d: %+v vs %+v", i, a, b2)
		}
	}
}

// K5: the generated stage-out script carries the usage step (prefix + the 64 KiB
// shell gate) and the original result/export loop is untouched. stagingFixture()
// is the fixture k8s_staging_test.go already feeds stageOutScript.
func TestK8sStageOutScriptCarriesUsageStep(t *testing.T) {
	st := stagingFixture()
	// The loop text the builder must still emit verbatim.
	origLoop := `for f in validation_result.json export_result.json; do `
	// The usage step, as a builder-independent check: prefix + the 64 KiB gate.
	script := stageOutScript(st)
	if !strings.Contains(script, stageOutUsagePrefix) ||
		!strings.Contains(script, `-le 65536`) ||
		!strings.Contains(script, origLoop) {
		t.Fatalf("stage-out script must carry the usage prefix, -le 65536 and the original loop, got %q", script)
	}
}
