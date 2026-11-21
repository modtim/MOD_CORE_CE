package executor

// MAC.1.s13 - the pure half of the darwin scratch side-channel policy, on an
// UNTAGGED runner.
//
// scratch_darwin_test.go carries the same decisions, but its FILENAME ends in
// _darwin_test.go, which Go reads as an implicit darwin build constraint - so
// on the linux CI runner that file compiles for nothing and none of its
// assertions ever run. Every decision in MAC.1.s13 is pure and untagged
// (scratch_policy.go), so this file re-asserts them where the linux runner
// actually executes them; the darwin build-tagged application (scratch_darwin.go)
// is compile-checked by `GOOS=darwin go test -c`.
//
// Names are Policy-suffixed so the two files can coexist in one binary (a darwin
// run executes both).

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const pScratch = "/Volumes/MOD/scratch/native/4242"
const pHome = "/Users/tim"

// --- tool cache + temp redirection ----------------------------------------

func TestPolicyEnvRedirectsNameScratchSubdirs(t *testing.T) {
	got := scratchEnvRedirects(pScratch)
	want := map[string]string{
		"TMPDIR":                  pScratch + "/tmp",
		"TEMP":                    pScratch + "/tmp",
		"TMP":                     pScratch + "/tmp",
		"XDG_CACHE_HOME":          pScratch + "/cache",
		"PIP_CACHE_DIR":           pScratch + "/cache/pip",
		"HF_HOME":                 pScratch + "/cache/hf",
		"HF_HUB_CACHE":            pScratch + "/cache/hf/hub",
		"TORCH_HOME":              pScratch + "/cache/torch",
		"TRITON_CACHE_DIR":        pScratch + "/cache/triton",
		"NUMBA_CACHE_DIR":         pScratch + "/cache/numba",
		"DOT_LOCAL_LOCATION":      pScratch + "/cache/local",
		"CRASH_REPORTING":         "0",
		"GTEST_DISABLE_CORE_DUMP": "1",
	}
	if len(got) != len(want) {
		t.Fatalf("redirect map has %d keys, want %d", len(got), len(want))
	}
	for k, v := range want {
		if got[k] != v {
			t.Fatalf("redirect key %s must be %s, got %q", k, v, got[k])
		}
	}
}

func TestPolicyEnvRedirectsNeverEscapeScratch(t *testing.T) {
	// Only the DIR keys are paths; the crash-suppression keys are flags
	// ("CRASH_REPORTING=0"), so the dir-key set is the set to check.
	for _, k := range scratchEnvDirKeys() {
		v := scratchEnvRedirects(pScratch)[k]
		if !strings.HasPrefix(v, pScratch) {
			t.Fatalf("redirect %s escapes the scratch root: %q", k, v)
		}
	}
}

func TestPolicyEnvRedirectsArePerTask(t *testing.T) {
	a := scratchEnvRedirects(pScratch)
	b := scratchEnvRedirects("/Volumes/MOD/scratch/native/9001")
	// The flag keys (CRASH_REPORTING=0) are per-task by definition; only the
	// directory values must differ between two tasks on one box.
	for _, k := range scratchEnvDirKeys() {
		if b[k] == a[k] {
			t.Fatalf("tasks share the %s cache path %q", k, a[k])
		}
	}
}

func TestPolicyApplyScratchEnvRedirectRunsOnLinux(t *testing.T) {
	// The one thing MAC.1.s13 applies on EVERY OS, so this is the linux-visible
	// half: the helper is untagged and lands the redirect in NativeEnv.
	spec := &TaskSpec{Env: map[string]string{"MOD_TASK_ID": "4242"}, NativeEnv: map[string]string{}}
	applyScratchEnvRedirect(spec, pScratch)
	if spec.NativeEnv["TMPDIR"] != pScratch+"/tmp" {
		t.Fatalf("TMPDIR must land in NativeEnv: %q", spec.NativeEnv["TMPDIR"])
	}
	if spec.NativeEnv["XDG_CACHE_HOME"] != pScratch+"/cache" {
		t.Fatalf("XDG_CACHE_HOME must land in NativeEnv")
	}
	if spec.NativeEnv["CRASH_REPORTING"] != "0" {
		t.Fatalf("crash suppression must be present")
	}
	if len(spec.NativeEnv) != len(scratchEnvRedirects(pScratch)) {
		t.Fatalf("redirect must not invent keys: %d vs %d", len(spec.NativeEnv), len(scratchEnvRedirects(pScratch)))
	}
}

func TestPolicyApplyScratchEnvRedirectReservesSpecKeys(t *testing.T) {
	// A curated recipe that wants its cache on another volume says so and wins.
	spec := &TaskSpec{Env: map[string]string{}, NativeEnv: map[string]string{"HF_HOME": "/Volumes/OTHER/hf"}}
	applyScratchEnvRedirect(spec, pScratch)
	if spec.NativeEnv["HF_HOME"] != "/Volumes/OTHER/hf" {
		t.Fatalf("the spec's own HF_HOME must survive: %q", spec.NativeEnv["HF_HOME"])
	}
	// A nil NativeEnv map must not panic (Go panics on assigning into one).
	blank := &TaskSpec{Env: map[string]string{}, NativeEnv: nil}
	applyScratchEnvRedirect(blank, pScratch)
	if blank.NativeEnv == nil || len(blank.NativeEnv) == 0 {
		t.Fatalf("a nil NativeEnv must be replaced, not written into")
	}
	applyScratchEnvRedirect(nil, pScratch)
	applyScratchEnvRedirect(&TaskSpec{Env: map[string]string{}, NativeEnv: map[string]string{}}, "")
}

// --- iCloud refusal (the one hard failure) --------------------------------

func TestPolicyICloudRefusalRejectsSyncedRoots(t *testing.T) {
	for _, suffix := range []string{"/Desktop/native/1", "/Documents/native/2", "/Library/Mobile Documents/native/3"} {
		if iCloudRefusal(pHome+suffix, pHome) == "" {
			t.Fatalf("a scratch root under a synced path must be refused: %s", suffix)
		}
	}
	if iCloudRefusal(pHome+"/Documents", pHome) == "" {
		t.Fatalf("the synced root itself must be refused")
	}
}

func TestPolicyICloudRefusalIsCaseInsensitive(t *testing.T) {
	if iCloudRefusal(pHome+"/desktop/native/1", pHome) == "" {
		t.Fatalf("APFS is case-insensitive by default: ~/desktop is ~/Desktop")
	}
}

func TestPolicyICloudRefusalAcceptsSafeRoots(t *testing.T) {
	for _, safe := range []string{"/Volumes/MOD/scratch/native/1", pHome + "/mod-scratch/native/1", "/var/tmp/mod/native/1"} {
		if iCloudRefusal(safe, pHome) != "" {
			t.Fatalf("safe scratch root refused: %q", safe)
		}
	}
}

func TestPolicyICloudRefusalWithoutHomeIsNoOp(t *testing.T) {
	if iCloudRefusal(pScratch, "") != "" {
		t.Fatalf("no home means no synced roots")
	}
	if iCloudRefusal("", pHome) != "" {
		t.Fatalf("empty scratch root means nothing to check")
	}
}

func TestPolicyICloudNosyncNamingOptsOut(t *testing.T) {
	if iCloudRefusal(pHome+"/Documents/mod-scratch.nosync/native/42", pHome) != "" {
		t.Fatalf("a .nosync component under a synced root is already out of iCloud")
	}
	if iCloudRefusal(pHome+"/Documents/native/42", pHome) == "" {
		t.Fatalf("a plain path under ~/Documents must still be refused")
	}
	// A .nosync component ABOVE the synced root does nothing for the tree below
	// it: the home itself carries the suffix, so the synced root is
	// <home>.nosync/Documents and the scratch under it is still refused.
	if iCloudRefusal(pHome+".nosync/Documents/native/42", pHome+".nosync") == "" {
		t.Fatalf("a .nosync component above the synced root does not opt the tree out")
	}
	if !iCloudNosyncOptOut(pHome+"/Documents/a.nosync", pHome+"/Documents") {
		t.Fatalf("opt-out predicate must see the component")
	}
	if iCloudNosyncOptOut(pHome+"/Documents/a", pHome+"/Documents") {
		t.Fatalf("no .nosync component means no opt-out")
	}
	if iCloudNosyncOptOut(pHome+"/Documents", pHome+"/Documents") {
		t.Fatalf("the synced root itself is not a component below it")
	}
}

func TestPolicyScratchRootInCloudMessageNamesRootNotBytes(t *testing.T) {
	msg := scratchRootInCloudMessage(pHome+"/Desktop/native/1", pHome+"/Desktop")
	if !strings.Contains(msg, pHome+"/Desktop") {
		t.Fatalf("refusal must name the synced root: %q", msg)
	}
	if !strings.Contains(msg, "Mobile Documents") {
		t.Fatalf("refusal must tell the operator where to move the scratch root: %q", msg)
	}
}

// --- the commands darwin runs (mocked runner seam) ------------------------

func TestPolicyScratchExclusionArgsIsPathFormArgv(t *testing.T) {
	argv := scratchExclusionArgs(pScratch)
	want := []string{"tmutil", "addexclusion", "-p", pScratch}
	if len(argv) != len(want) {
		t.Fatalf("tmutil argv drifted: %d vs %d", len(argv), len(want))
	}
	for i, a := range want {
		if argv[i] != a {
			t.Fatalf("tmutil argv[%d] must be %s, got %q", i, a, argv[i])
		}
	}
	// MOD never shells out, so a path with a quote stays one argv element and
	// cannot break out of the command.
	tricky := "/Users/tim/scratch/it's here/42"
	if got := scratchExclusionArgs(tricky); len(got) != 4 || got[3] != tricky {
		t.Fatalf("quoted path must stay one argv element")
	}
	if len(scratchExclusionArgs("")) != 0 {
		t.Fatalf("empty scratch dir means no command")
	}
}

func TestPolicyScratchCrashReportArgsAreEmpty(t *testing.T) {
	// "Suppress crash reports" is honest only if MOD never runs a global
	// crash-report command: suppression is the task's own environment.
	if len(scratchCrashReportArgs()) != 0 {
		t.Fatalf("darwin must not run a global crash-report command")
	}
	keys := scratchCrashReportKeys()
	if len(keys) != 2 || keys[0] != "CRASH_REPORTING" || keys[1] != "GTEST_DISABLE_CORE_DUMP" {
		t.Fatalf("crash-suppression keys drifted: %v", keys)
	}
}

func TestPolicyRunScratchExclusionIsFailSoft(t *testing.T) {
	// The injectable runner proves the exact argv darwin would run, and proves a
	// missing/denied tmutil is REPORTED (the caller discards it) rather than
	// turning into a task failure.
	orig := scratchCmdRunner
	var ran []string
	scratchCmdRunner = func(argv []string) error {
		if len(argv) == 0 {
			t.Fatalf("runner must not be called with empty argv")
		}
		ran = argv
		return errors.New("tmutil: command not found")
	}
	err := runScratchExclusion(pScratch)
	if ran[0] != "tmutil" || ran[1] != "addexclusion" || ran[2] != "-p" || ran[3] != pScratch {
		t.Fatalf("runner saw the wrong argv: %v", ran)
	}
	if err == nil {
		t.Fatalf("a failing tmutil must be reported, not hidden")
	}
	// Fail-soft contract: the caller discards it (scratch_darwin.go does `_ =`).
	_ = err
	// Leave the seam as the production default: other tests (and the real worker)
	// must not inherit this recorder.
	scratchCmdRunner = orig
}

func TestPolicyScratchCmdRunnerRefusesUnknownCommands(t *testing.T) {
	// The seam is an execution path, so the DEFAULT runner carries an allow-list:
	// a hygiene hook has no business running arbitrary argv. The refused argv is
	// a harmless command so a broken guard cannot damage the test host.
	if err := scratchCmdRunner([]string{"echo", "mod-s13"}); err == nil {
		t.Fatalf("the default runner must refuse a command outside the side-channel allow-list")
	}
	// The accept side is the pure predicate, so no real tmutil runs here.
	for _, known := range []string{"tmutil", "mdutil"} {
		if !scratchKnownCommand(known) {
			t.Fatalf("a side-channel command must be allowed: %s", known)
		}
	}
	for _, unknown := range []string{"", "launchctl", "rm", "sh", "bash", "sudo"} {
		if scratchKnownCommand(unknown) {
			t.Fatalf("must not be allowed: %q", unknown)
		}
	}
}

func TestPolicyScratchPostureForOS(t *testing.T) {
	if scratchPostureForOS("darwin") != "tmutil-exclusion+never-index+env-redirect+crash-suppressed" {
		t.Fatalf("darwin posture token drifted")
	}
	if scratchPostureForOS("linux") != "none-required" {
		t.Fatalf("linux must not claim darwin layers")
	}
}

func TestPolicyScratchEnvDirKeysAreCreatableScratchDirs(t *testing.T) {
	root := filepath.Join("/tmp", "mod-s13-policy-scratch")
	redirects := scratchEnvRedirects(root)
	flags := scratchCrashReportKeys()
	for _, k := range scratchEnvDirKeys() {
		for _, f := range flags {
			if k == f {
				t.Fatalf("a flag key must not be in the dir-key set: %s", k)
			}
		}
		dir := redirects[k]
		if dir == "" || !strings.HasPrefix(dir, root) {
			t.Fatalf("dir key %s points outside the scratch root: %q", k, dir)
		}
		if err := os.MkdirAll(dir, 0755); err != nil {
			t.Fatalf("dir key %s is not creatable: %s", k, fmt.Sprintf("%v", err))
		}
	}
}

func TestPolicyScratchNeverIndexMarkerName(t *testing.T) {
	// The Spotlight opt-out is a marker FILE, not `mdutil -i off` (volume-wide:
	// it would turn search off for the whole disk on the owner's machine).
	marker := scratchNeverIndexMarker()
	if marker != ".metadata_never_index" {
		t.Fatalf("marker name drifted: %q", marker)
	}
	if !strings.HasPrefix(marker, ".") {
		t.Fatalf("marker must be dot-prefixed")
	}
}
