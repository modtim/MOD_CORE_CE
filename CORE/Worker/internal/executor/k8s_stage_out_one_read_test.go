package executor

// K8S-STAGING-ONE-READ-TWIN - the daemonless unit tests for the merged stage-out
// scan, in the style of result_files_one_read_test.go (which does the same job
// for the Docker path's splitHelperCatOutput). There is no helper POD to split
// here: the k8s path already rides ONE stage-out container (c11-J3b put result.json
// in the SAME log stream), so what the merge removes is the SECOND Scanner pass
// over the same log bytes. parseStageOutManifest is pure (no clientset, no ctx),
// so the merge is testable directly, and T6 is the behaviour-equivalence proof
// against the two public wrappers on a battery of log shapes.

import (
	"encoding/base64"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

const doneMarkerLogs = "MOD_STAGE_OUT_DONE"

// T1: all three files present -> ONE scan yields the outputs AND the usage.
func TestK1_AllThreePresent(t *testing.T) {
	logs := "noise line\n" +
		"MOD_RESULT_JSON_B64:validation_result.json:" +
		b64(`{"outputs":[{"content_hash":"h1","file_name":"a.bin","file_extension":"bin","size_bytes":3}]}`) + "\n" +
		"MOD_RESULT_JSON_B64:export_result.json:" +
		b64(`{"outputs":[{"content_hash":"h2","file_name":"b.bin"}]}`) + "\n" +
		stageOutUsagePrefix + b64(`{"model":"m1","usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}`) + "\n" +
		"UPLOAD,zzz,ignored,1,bin\n" + doneMarkerLogs + "\n"
	m := parseStageOutManifest(logs)
	if !m.done {
		t.Fatal("K1: done marker must be seen")
	}
	if len(m.outs) != 1 || m.outs[0].ContentHash != "h1" || m.outs[0].FileName != "a.bin" {
		t.Fatalf("K1: first result file with outputs must win, got %+v", m.outs)
	}
	if m.usage == nil || len(m.usage.Models) != 1 {
		t.Fatalf("K1: usage must be read in the same pass, got %v", m.usage)
	}
	k := m.usage.Models[0]
	if k.Model != "m1" || k.PromptTokens != 3 || k.CompletionTokens != 4 || k.TotalTokens != 7 {
		t.Fatalf("K1: counts must survive the S9 gate, got model=%q p=%d c=%d t=%d",
			k.Model, k.PromptTokens, k.CompletionTokens, k.TotalTokens)
	}
	// The UPLOAD manifest line is shadowed by the result JSON, exactly as before.
	outsW, errW := ParseStageOutLogs(logs)
	if errW != nil || len(outsW) != 1 || outsW[0].ContentHash != "h1" {
		t.Fatalf("K1: wrapper outputs differ, got %+v", outsW)
	}
}

// T2: only result.json present (usage-only stage-out) -> empty non-nil outputs,
// usage read. Never fails the task.
func TestK2_OnlyResultJSON(t *testing.T) {
	logs := stageOutUsagePrefix + b64(`{"model":"m2","usage":{"total_tokens":11}}`) + "\n" + doneMarkerLogs + "\n"
	m := parseStageOutManifest(logs)
	if !m.done {
		t.Fatal("K2: done marker must be seen")
	}
	if m.outs != nil {
		t.Fatalf("K2: no result file means no result outputs, got %+v", m.outs)
	}
	if m.usage == nil || m.usage.Models[0].TotalTokens != 11 {
		t.Fatalf("K2: usage must still be metered, got %v", m.usage)
	}
	// result.json is never captured as an output: the manifest stays empty non-nil.
	outs2, err2 := ParseStageOutLogs(logs)
	if err2 != nil || outs2 == nil || len(outs2) != 0 {
		t.Fatalf("K2: want empty non-nil outputs, got %#v (%v)", outs2, err2)
	}
}

// T3: none of the three files -> empty non-nil outputs, nil usage, done seen.
func TestK3_NonePresent(t *testing.T) {
	logs := "task exited 0\n" + doneMarkerLogs + "\n"
	m := parseStageOutManifest(logs)
	if !m.done {
		t.Fatal("K3: done marker must be seen")
	}
	if m.outs != nil || m.usage != nil {
		t.Fatalf("K3: want nil outs and nil usage, got %+v / %v", m.outs, m.usage)
	}
	outs3, err3 := ParseStageOutLogs(logs)
	if err3 != nil || outs3 == nil || len(outs3) != 0 {
		t.Fatalf("K3: want empty non-nil outputs, got %#v (%v)", outs3, err3)
	}
	if ParseStageOutUsage(logs) != nil {
		t.Fatal("K3: want nil usage")
	}
}

// T4: planted separator text. The k8s prefixes are FIXED constants (unlike the
// Docker path's per-run random marker), so this test pins what the merge does NOT
// change: a planted line whose payload is not valid base64 is ignored, planted
// text INSIDE a file's base64 payload rides through as content, and a planted
// line that IS a valid prefixed base64 line is honoured - exactly as the old
// two-scan code honoured the same bytes (T6 proves the equality). No new
// forgeability, no new defence: the tenant node owns /data/output either way.
func TestK4_PlantedSeparatorText(t *testing.T) {
	planted := "MOD_USAGE_JSON_B64:not-base64-at-all\nMOD_RESULT_JSON_B64:evil.json:also-not-base64!!\n"
	logs := "MOD_RESULT_JSON_B64:validation_result.json:" + b64(planted) + "\n" +
		"MOD_USAGE_JSON_B64:!!!not base64!!!\n" + doneMarkerLogs + "\n"
	m := parseStageOutManifest(logs)
	if !m.done {
		t.Fatal("K4: done marker must be seen")
	}
	// The planted payload is not JSON with outputs, so no outputs - the same
	// outcome the old per-line scan produced for the same bytes.
	if m.outs != nil {
		t.Fatalf("K4: planted payload must not yield outputs, got %+v", m.outs)
	}
	if m.usage != nil {
		t.Fatalf("K4: bad base64 usage line must yield nil, got %v", m.usage)
	}
	outs4, err4 := ParseStageOutLogs(logs)
	if err4 != nil || outs4 == nil || len(outs4) != 0 {
		t.Fatalf("K4: want empty non-nil outputs, got %#v (%v)", outs4, err4)
	}
	// A planted line with a near-miss prefix is ignored entirely.
	m2 := parseStageOutManifest("MOD_RESULT_JSON_B64X:validation_result.json:" +
		b64(`{"outputs":[{"content_hash":"zz"}]}`) + "\n" + doneMarkerLogs + "\n")
	if m2.outs != nil {
		t.Fatalf("K4: unknown prefix must not match, got %+v", m2.outs)
	}
}

// T5: empty output (no logs at all) -> not done, so the collect reports StageOutFailed
// and usage is nil; nothing throws.
func TestK5_EmptyOutput(t *testing.T) {
	m := parseStageOutManifest("")
	if m.done || m.outs != nil || m.usage != nil {
		t.Fatalf("K5: want not-done, nil outs, nil usage, got done=%v %+v %v",
			m.done, m.outs, m.usage)
	}
	err := stageOutErrOrNil(ParseStageOutLogs(""))
	if err == "" || !strings.Contains(err, stageOutFailedErr) {
		t.Fatalf("K5: want StageOutFailed, got %q", err)
	}
	if ParseStageOutUsage("") != nil {
		t.Fatal("K5: want nil usage")
	}
}

// T6: the ONE-pass scan equals the two public wrappers on every shape the stage-out
// script can emit. This is the "same outputs list, same usage tokens, same error"
// proof for the merge.
func TestK6_OnePassEqualsOldWrappers(t *testing.T) {
	cases := []string{
		"",
		doneMarkerLogs,
		"UPLOAD,abc,out.png,12,png\nUPLOAD,def,b.txt,3,txt\n" + doneMarkerLogs,
		"UPLOAD,a,b,1,c\n", // no marker
		"MOD_RESULT_JSON_B64:validation_result.json:" +
			b64(`{"outputs":[{"location":"minio://bkt/hash9","file_name":"v.json","file_extension":"json","size_bytes":5}]}`) +
			"\nUPLOAD,zzz,x,1,x\n" + doneMarkerLogs,
		"MOD_RESULT_JSON_B64:export_result.json:" + b64(`{"outputs":[]}`) +
			"\nUPLOAD,k,f.bin,9,bin\n" + doneMarkerLogs,
		stageOutUsagePrefix + b64(`{"model":"m1","usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}`) +
			"\n" + doneMarkerLogs,
		stageOutUsagePrefix + "!!!not base64!!!\n" + doneMarkerLogs,
		stageOutUsagePrefix + b64(`{"model":"bad name","usage":{"total_tokens":7}}`) + "\n" + doneMarkerLogs,
		// usage line BEFORE the result line (order independence).
		stageOutUsagePrefix + b64(`{"model":"m3","usage":{"total_tokens":1}}`) + "\n" +
			"MOD_RESULT_JSON_B64:validation_result.json:" +
			b64(`{"outputs":[{"content_hash":"h9","file_name":"z.bin","file_extension":"bin","size_bytes":1}]}`) + "\n" + doneMarkerLogs,
		// two usage lines: the FIRST wins even when unusable.
		stageOutUsagePrefix + "!!!bad!!!\n" +
			stageOutUsagePrefix + b64(`{"model":"m4","usage":{"total_tokens":2}}`) + "\n" + doneMarkerLogs,
		// two result lines, first unusable: the second wins.
		"MOD_RESULT_JSON_B64:validation_result.json:not-base64\n" +
			"MOD_RESULT_JSON_B64:export_result.json:" +
			b64(`{"outputs":[{"content_hash":"h5","file_name":"q.bin"}]}`) + "\n" + doneMarkerLogs,
	}
	for i := range cases {
		logs := cases[i]
		m := parseStageOutManifest(logs)
		outsW, errW := ParseStageOutLogs(logs)
		usageW := ParseStageOutUsage(logs)
		if m.done != (errW == nil) {
			t.Fatalf("K6[%d]: done vs wrapper error disagree: done=%v err=%v", i, m.done, errW)
		}
		if (m.usage == nil) != (usageW == nil) {
			t.Fatalf("K6[%d]: usage nil disagree: merged=%v wrapper=%v", i, m.usage, usageW)
		}
		if m.usage != nil && usageW != nil {
			a, b2 := m.usage.Models[0], usageW.Models[0]
			if a.Model != b2.Model || a.PromptTokens != b2.PromptTokens ||
				a.CompletionTokens != b2.CompletionTokens || a.TotalTokens != b2.TotalTokens {
				t.Fatalf("K6[%d]: usage tokens differ: %+v vs %+v", i, a, b2)
			}
		}
		if m.done {
			merged := stageOutOutputs(m, logs)
			if len(merged) != len(outsW) {
				t.Fatalf("K6[%d]: output count differs: %d vs %d", i, len(merged), len(outsW))
			}
			for j := range merged {
				x, y := merged[j], outsW[j]
				if x.ContentHash != y.ContentHash || x.FileName != y.FileName ||
					x.FileExtension != y.FileExtension || x.SizeBytes != y.SizeBytes {
					t.Fatalf("K6[%d]: outputs differ at %d: %+v vs %+v", i, j, x, y)
				}
			}
		}
	}
}

// stageOutErrOrNil renders ParseStageOutLogs' error as text for the K5 assertion
// (the error is the StageOutFailed marker text classifyReason maps to
// ReasonStageOutFailed; the collect path is covered by
// TestK8sCollectStagedOutputsNoMarker with a real fake clientset).
func stageOutErrOrNil(outs []transport.OutputFile, err error) string {
	_ = outs
	if err == nil {
		return ""
	}
	return err.Error()
}
