package executor

// OPT4B (lane dell-1) - the Harbor/paid posture pays THREE throwaway sidecar
// containers after the node container exits: the scan-staging pass, the durable
// upload, and the standalone scan-staging cleanup. This change folds the cleanup
// into the END of the durable upload container, so the posture pays TWO.
// These tests pin, WITHOUT a daemon:
//   - the upload BODY is byte-for-byte unchanged by the fold (same objects,
//     same keys, same manifest lines => the uploaded bytes stay exactly the
//     scanned bytes);
//   - the cleanup rides strictly AFTER the upload body, and only when the
//     upload's own exit status is 0, and the container's exit status stays the
//     upload's (fail-closed, retry rules untouched);
//   - the staging pass itself still never deletes anything (the consult must
//     be able to fetch what it staged);
//   - the fold decision and the standalone-cleanup fallback decision, as the
//     exact truth tables Run() depends on (mutation checks: flipping either
//     predicate, or reordering the cleanup before the upload, fails a test).

import (
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func opt4bTask(id int) *transport.Task {
	return &transport.Task{ID: id, Payload: transport.TaskPayload{
		MinioBucket: "tenant-7-files",
	}}
}

// The upload BODY must be identical with and without the fold: the fold only
// appends the cleanup, so the bytes uploaded, the <prefix><hash> keys and the
// UPLOAD manifest lines are exactly what the scan-staging pass staged and the
// API's consult scanned.
func TestOpt4B_UploadBodyUnchangedByFold(t *testing.T) {
	plain := uploadScriptParamsFromTask(opt4bTask(9101))
	folded := durableUploadParamsFromTask(opt4bTask(9101))
	if plain.FoldCleanupPrefix != "" {
		t.Errorf("the plain durable params must never carry a fold, got %q", plain.FoldCleanupPrefix)
	}
	if folded.FoldCleanupPrefix != "scan-staging/9101/" {
		t.Errorf("fold prefix must be THIS task's own staging prefix, got %q", folded.FoldCleanupPrefix)
	}
	bodyPlain := buildUploadScriptBody(plain)
	bodyFolded := buildUploadScriptBody(folded)
	if bodyPlain != bodyFolded {
		t.Fatalf("fold changed the upload body:\n plain:  %s\nfolded: %s", bodyPlain, bodyFolded)
	}
	// The no-fold contract is unchanged; the FOLDED script is the suffix's
	// wrapped form ONLY (FOLD COMPOSITION FIX, found by this job's real-docker
	// run on cpu4): the suffix already wraps the body in a subshell, so the
	// old `body + suffix` rendered `...done(CAP=0 ...` - a /bin/sh syntax error
	// (exit 2) that would also have run the upload twice. The body must appear
	// EXACTLY ONCE, wrapped.
	fullPlain := buildUploadScript(plain)
	fullFolded := buildUploadScript(folded)
	if fullPlain != bodyPlain {
		t.Errorf("no-fold caller must get the body verbatim (golden contract), got drift")
	}
	if fullFolded != stagingCleanupFoldSuffix(folded) {
		t.Errorf("folded script must be the wrapped body + cleanup, nothing else")
	}
	if !strings.HasPrefix(fullFolded, "("+bodyFolded+")") {
		t.Errorf("folded script must wrap the WHOLE body in one subshell, got %q",
			fullFolded[:40])
	}
	if strings.Count(fullFolded, bodyFolded) != 1 {
		t.Errorf("the upload body must appear EXACTLY ONCE in the folded script "+
		 "(a double run would upload everything twice), got %d",
			strings.Count(fullFolded, bodyFolded))
	}
}

// ORDERING (mutation check): the cleanup must ride strictly AFTER the upload
// body inside the folded script, and must be gated on the upload's own exit
// status with that status re-exited. Any reorder fails this test.
func TestOpt4B_CleanupRidesAfterUploadAndKeepsExitStatus(t *testing.T) {
	folded := durableUploadParamsFromTask(opt4bTask(9101))
	body := buildUploadScriptBody(folded)
	suffix := stagingCleanupFoldSuffix(folded)
	if !strings.Contains(suffix, `mc rm -r --force myminio/tenant-7-files/scan-staging/9101/ || true`) {
		t.Errorf("fold must carry the exact standalone cleanup command, got %q", suffix)
	}
	if !strings.HasPrefix(suffix, "("+body+")") {
		t.Errorf("fold must wrap the WHOLE upload body in one subshell, got %q", suffix[:40])
	}
	if !strings.Contains(suffix, `st=$?`) || !strings.Contains(suffix, `if [ "$st" -eq 0 ]`) {
		t.Errorf("cleanup must run only when the upload exited 0")
	}
	if !strings.HasSuffix(suffix, `exit $st`) {
		t.Errorf("container exit status must stay the upload's own (fail-closed)")
	}
	full := buildUploadScript(folded)
	if strings.Index(full, "mc rm -r") < strings.LastIndex(full, "mc cp") {
		t.Errorf("cleanup must come AFTER every upload cp (would delete unscanned bytes otherwise)")
	}
	if strings.Count(full, "mc rm -r") != 1 {
		t.Errorf("exactly one cleanup command, got %d", strings.Count(full, "mc rm -r"))
	}
}

// The staging pass must NEVER carry the fold: it would delete the blobs before
// the API's pre-store consult (an HTTP call made after this sidecar returns)
// ever fetched them.
func TestOpt4B_StagingPassNeverCarriesFold(t *testing.T) {
	p := stagingUploadParamsFromTask(opt4bTask(9101))
	if p.FoldCleanupPrefix != "" {
		t.Errorf("staging params must not fold a cleanup, got %q", p.FoldCleanupPrefix)
	}
	got := buildUploadScript(p)
	if strings.Contains(got, "mc rm") {
		t.Errorf("staging command must not delete anything, got %q", got)
	}
	// The K8s stage-out script and the CE in-process path are untouched.
	if strings.Contains(buildUploadCmd(opt4bTask(9101)), "mc rm -r") {
		t.Errorf("k8s stage-out script must not carry the scan-staging cleanup")
	}
}

// Golden: every caller that leaves the fold empty still gets the pre-OPT4B
// script byte-for-byte (the W17 golden strings in upload_script_test.go).
func TestOpt4B_GoldenUnfoldedScriptUnchanged(t *testing.T) {
	got := buildUploadScript(uploadScriptParamsFromTask(opt4bTask(9101)))
	if !strings.Contains(got, `mc cp "$file" myminio/tenant-7-files/$hash &&`) {
		t.Errorf("durable upload script drifted, got %q", got)
	}
	if stagingCleanupFoldSuffix(uploadScriptParamsFromTask(opt4bTask(9101))) != "" {
		t.Errorf("empty fold prefix must render no suffix")
	}
}

// FOLD DECISION truth table: fold only under the sidecar posture AND only when
// the staging pass succeeded (the same condition under which the standalone
// cleanup was ever scheduled).
func TestOpt4B_FoldDecision(t *testing.T) {
	if foldStagingCleanupIntoUpload(true, true) {
		t.Errorf("CE posture cleanup is in-process minio-go; never a container fold")
	}
	if foldStagingCleanupIntoUpload(false, false) {
		t.Errorf("staging failed: nothing was staged, so there is nothing to fold")
	}
	if !foldStagingCleanupIntoUpload(false, true) {
		t.Errorf("sidecar posture + staging ok must fold the cleanup into the upload")
	}
}

// FALLBACK truth table: the standalone cleanup sidecar is skipped ONLY when the
// upload actually carried the fold and exited 0. Every other path keeps it, so
// a failed/retried-out upload, a scan-blocked early return, or an unfolded
// posture still cleans the staged blobs (no orphan leak).
func TestOpt4B_StandaloneCleanupFallback(t *testing.T) {
	if !standaloneStagingCleanupStillNeeded(false, false) {
		t.Errorf("no fold + failed upload must still run the standalone cleanup")
	}
	if !standaloneStagingCleanupStillNeeded(false, true) {
		t.Errorf("CE posture (no fold) must keep its in-process cleanup verbatim")
	}
	if !standaloneStagingCleanupStillNeeded(true, false) {
		t.Errorf("folded upload that failed must still run the standalone cleanup")
	}
	if standaloneStagingCleanupStillNeeded(true, true) {
		t.Errorf("folded upload that succeeded already cleaned up; a second pass is the cost being cut")
	}
}

// The folded cleanup's own stdout (mc's "Removed ..." lines) rides in the SAME
// container's log, so the manifest parser must ignore it: the outputs the API
// sees stay exactly the UPLOAD lines the unchanged body printed.
func TestOpt4B_ManifestIgnoresCleanupOutput(t *testing.T) {
	bodyOut := "UPLOAD,abc123,f1.bin,4096,bin\n"
	mixed := bodyOut+"Removed `tenant-7-files/scan-staging/9101/abc123'\n"
	got := parseUploadManifest(mixed)
	if len(got) != 1 || got[0].ContentHash != "abc123" || got[0].FileName != "f1.bin" {
		t.Errorf("cleanup output must not enter the manifest, got %d row(s)", len(got))
	}
}

// The fold prefix must be THIS task's own staging prefix, never another task's:
// two tasks of the same tenant never share a cleanup target.
func TestOpt4B_FoldPrefixIsPerTask(t *testing.T) {
	a := durableUploadParamsFromTask(opt4bTask(101)).FoldCleanupPrefix
	b := durableUploadParamsFromTask(opt4bTask(102)).FoldCleanupPrefix
	if a == b || a != "scan-staging/101/" {
		t.Errorf("fold prefix must be per-task, got %q / %q", a, b)
	}
	// A wizard task keeps its own durable prefix AND its own staging prefix.
	w := &transport.Task{ID: 55, Payload: transport.TaskPayload{
		MinioBucket: "tenant-7-files", Purpose: "wizard_test_run",
		OutputPrefix: "_wizard_test_runs/s1/2/", SandboxMaxOutputMB: 5,
	}}
	wp := durableUploadParamsFromTask(w)
	if wp.Prefix != "_wizard_test_runs/s1/2/" || wp.CapBytes != 5*1024*1024 {
		t.Errorf("fold must not disturb the durable prefix/cap, got %+v", wp)
	}
	if wp.FoldCleanupPrefix != "scan-staging/55/" {
		t.Errorf("fold prefix must stay the staging prefix, got %q", wp.FoldCleanupPrefix)
	}
}
