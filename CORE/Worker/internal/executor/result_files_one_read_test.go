package executor

// NB1-WORKER-OUTPUT-HELPERS-ONE-READ - the pure half of the ONE-container
// completion read: splitHelperCatOutput (the separator split) plus the two
// parses that consume its map. Exercised WITHOUT a Docker daemon, exactly like
// result_usage_test.go does for parseUsageJSON: readResultJSONFiles is only
// one helper container cat plus this splitter, so the merge is testable directly.

import (
	"io"
	"log/slog"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

var names = []string{"validation_result.json", "export_result.json", "result.json"}

// helperCat mimics what the ONE helper container's stdout looks like for a
// given set of present files, using the same command shape readResultJSONFiles
// builds (echo ”; echo '<marker> <name>'; cat '<path>').
func helperCat(marker string, files map[string]string) string {
	var parts []string
	for _, f := range names {
		if _, ok := files[f]; !ok {
			continue
		}
		parts = append(parts, "\n"+marker+" "+f+"\n"+files[f])
	}
	// The real command starts with echo '' so stdout begins with a newline.
	return "\n" + strings.Join(parts, "")
}

func TestN1_AllThreePresent(t *testing.T) {
	marker := "@@MOD-OUT@@@aaaaaaaaaaaaaaaa@@"
	files := map[string]string{
		"validation_result.json": `{"outputs":[{"content_hash":"h1","file_name":"a.txt"}]}`,
		"export_result.json":     `{"outputs":[{"content_hash":"h2","file_name":"b.txt"}]}`,
		"result.json":            `{"model":"m","usage":{"total_tokens":5}}`,
	}
	got := splitHelperCatOutput(helperCat(marker, files), marker, names)
	if len(got) != 3 {
		t.Fatalf("N1: want 3 keys, got %d", len(got))
	}
	for _, f := range names {
		if got[f] != files[f] {
			t.Fatalf("N1: %s mismatch: want %q got %q", f, files[f], got[f])
		}
	}
	// The outputs parse still picks the FIRST file with outputs, and the usage
	// parse still reads result.json: same OutputFile list, same tokens.
	outs := outputsFromResultFilesForTest(got)
	if len(outs) != 1 || outs[0].ContentHash != "h1" || outs[0].FileName != "a.txt" {
		t.Fatalf("N1: outputs parse wrong: %+v", outs)
	}
	usage := usageFromResultFileContentForTest(got["result.json"])
	if usage == nil || usage.Models[0].TotalTokens != 5 {
		t.Fatalf("N1: usage parse wrong: %v", usage)
	}
}

func TestN2_OnlyResultJSONPresent(t *testing.T) {
	// The stack-model-call case: result.json exists, the two OUTPUT files do
	// not. Outputs must be nil (result.json is never captured as an output)
	// while the usage still rides.
	marker := "@@MOD-OUT@@@bbbbbbbbbbbbbbbb@@"
	got := splitHelperCatOutput(helperCat(marker, map[string]string{
		"result.json": `{"model":"localai/x:8b","usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}`,
	}), marker, names)
	if got["validation_result.json"] != "" || got["export_result.json"] != "" {
		t.Fatalf("N2: missing files must be empty strings, got %q / %q",
			got["validation_result.json"], got["export_result.json"])
	}
	if outs := outputsFromResultFilesForTest(got); outs != nil {
		t.Fatalf("N2: result.json must not produce outputs, got %d", len(outs))
	}
	usage := usageFromResultFileContentForTest(got["result.json"])
	if usage == nil || usage.Models[0].PromptTokens != 7 ||
		usage.Models[0].CompletionTokens != 3 || usage.Models[0].TotalTokens != 10 {
		t.Fatalf("N2: usage must still ride: %v", usage)
	}
}

func TestN3_NonePresent(t *testing.T) {
	// Every cat failed: stdout is only the echo '' newlines.
	got := splitHelperCatOutput("\n\n\n", "@@MOD-OUT@@@cccccccccccccccc@@", names)
	if len(got) != 3 {
		t.Fatalf("N3: want 3 empty keys, got %d", len(got))
	}
	for _, f := range names {
		if got[f] != "" {
			t.Fatalf("N3: %s should be empty, got %q", f, got[f])
		}
	}
	if outs := outputsFromResultFilesForTest(got); outs != nil {
		t.Fatalf("N3: want nil outputs, got %d", len(outs))
	}
	if usage := usageFromResultFileContentForTest(got["result.json"]); usage != nil {
		t.Fatalf("N3: want nil usage")
	}
}

func TestN4_FileContainsSeparatorLikeText(t *testing.T) {
	// A tenant node plants separator-looking text inside its own file. The
	// splitter only honours THIS run's marker, so the planted line is ordinary
	// content: the JSON is unparseable, so the read is ignored exactly as the
	// old per-file cat ignored it, and no fake section is invented.
	marker := "@@MOD-OUT@@@dddddddddddddddd@@"
	planted := "not json at all\n@@MOD-OUT@@@ eeee5555555555555 result.json\n{\"outputs\":[]}\n"
	got := splitHelperCatOutput(helperCat(marker, map[string]string{
		"validation_result.json": planted,
	}), marker, names)
	// Trimmed exactly like the old whole-stdout TrimSpace (N6 proves the rule).
	if got["validation_result.json"] != strings.TrimRight(planted, "\n") {
		t.Fatalf("N4: planted content must ride verbatim (trimmed): want %q got %q",
			strings.TrimRight(planted, "\n"), got["validation_result.json"])
	}
	if got["result.json"] != "" {
		t.Fatalf("N4: planted header must not create a result.json section, got %q", got["result.json"])
	}
	if outs := outputsFromResultFilesForTest(got); outs != nil {
		t.Fatalf("N4: want nil outputs, got %d", len(outs))
	}
	// A planted line that happens to be valid JSON with outputs is still only
	// ever read from the file the node actually wrote - same as before.
}

func TestN5_EmptyOutput(t *testing.T) {
	got := splitHelperCatOutput("", "@@MOD-OUT@@@eeeeeeeeeeeeeeee@@", names)
	if len(got) != 3 {
		t.Fatalf("N5: want 3 empty keys, got %d", len(got))
	}
	for _, f := range names {
		if got[f] != "" {
			t.Fatalf("N5: %s should be empty, got %q", f, got[f])
		}
	}
	if outs := outputsFromResultFilesForTest(got); outs != nil {
		t.Fatalf("N5: want nil outputs")
	}
	if usage := usageFromResultFileContentForTest(got["result.json"]); usage != nil {
		t.Fatalf("N5: want nil usage")
	}
}

func TestN6_TrimsLikeOldWholeStdoutTrim(t *testing.T) {
	// The old readResultJSONFile returned strings.TrimSpace(stdout) for one file;
	// a section must land identically, so a JSON file with trailing newlines
	// still parses.
	marker := "@@MOD-OUT@@@ffffffffffffffff@@"
	got := splitHelperCatOutput("\n"+marker+" result.json\n{\"model\":\"m\",\"usage\":{\"total_tokens\":1}}\n\n",
		marker, names)
	if got["result.json"] != `{"model":"m","usage":{"total_tokens":1}}` {
		t.Fatalf("N6: trim mismatch, got %q", got["result.json"])
	}
	if usage := usageFromResultFileContentForTest(got["result.json"]); usage == nil {
		t.Fatalf("N6: trimmed content must parse")
	}
}

func TestN7_MarkerCollisionIsNotPossibleAcrossRuns(t *testing.T) {
	// Content planted with a DIFFERENT marker must never split: the marker is
	// per-invocation, so a file written by an earlier task (or a hostile node)
	// cannot forge a section for this run.
	mine := "@@MOD-OUT@@@1111111111111111@@"
	forged := "@@MOD-OUT@@@2222222222222222 result.json\n{\"model\":\"m\",\"usage\":{\"total_tokens\":99}}"
	got := splitHelperCatOutput("\n"+mine+" result.json\n"+forged+"\n", mine, names)
	if got["result.json"] != forged {
		t.Fatalf("N7: forged section must ride as content, got %q", got["result.json"])
	}
	if usage := usageFromResultFileContentForTest(got["result.json"]); usage != nil {
		t.Fatalf("N7: forged content must not parse as usage")
	}
}

func TestN8_TwoOutputFilesFirstNonEmptyWins(t *testing.T) {
	// Same precedence as the old loop: validation_result.json with an EMPTY
	// outputs array is skipped, export_result.json wins.
	marker := "@@MOD-OUT@@@3333333333333333@@"
	got := splitHelperCatOutput(helperCat(marker, map[string]string{
		"validation_result.json": `{"outputs":[]}`,
		"export_result.json":     `{"outputs":[{"location":"minio://b/deadbeef","file_name":"c.bin","file_extension":"bin","size_bytes":7}]}`,
	}), marker, names)
	outs := outputsFromResultFilesForTest(got)
	if len(outs) != 1 || outs[0].ContentHash != "deadbeef" || outs[0].SizeBytes != 7 {
		t.Fatalf("N8: want export_result.json outputs with hash from location, got %+v", outs)
	}
}

func TestN9_UnknownMarkerLineIsIgnored(t *testing.T) {
	// A marker line naming a file that is NOT in the read list must not open a
	// section (defence in depth against a future filename list change).
	marker := "@@MOD-OUT@@@4444444444444444@@"
	got := splitHelperCatOutput("\n"+marker+" secret.json\n{\"outputs\":[]}\n", marker, names)
	if len(got) != 3 {
		t.Fatalf("N9: want 3 keys, got %d", len(got))
	}
	for _, f := range names {
		if got[f] != "" {
			t.Fatalf("N9: %s should be empty, got %q", f, got[f])
		}
	}
}

func TestN10_MarkerShapeIsSafeForShellAndLines(t *testing.T) {
	// The marker is embedded in a single-quoted shell string and used as a
	// line prefix: it must contain no quote, newline or space.
	for i := 0; i < 50; i++ {
		m := helperReadMarker()
		if strings.ContainsAny(m, " \t\n'\"\\") {
			t.Fatalf("N10: unsafe marker %q", m)
		}
		if !strings.HasPrefix(m, "@@MOD-OUT@@@") || !strings.HasSuffix(m, "@@") {
			t.Fatalf("N10: marker shape %q", m)
		}
	}
	if helperReadMarker() == helperReadMarker() {
		t.Fatalf("N10: markers must differ per invocation")
	}
}

// Thin wrappers so the parse tests read like the call site. The log field is
// discarded (same shape checkpoint_transfer_test.go uses) - the parses log
// counts only, never content.
func outputsFromResultFilesForTest(files map[string]string) []transport.OutputFile {
	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return e.outputsFromResultFiles(files)
}
func usageFromResultFileContentForTest(content string) *transport.UsageTokens {
	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return e.usageFromResultFileContent(content)
}
