package executor

// MAC.1.s13 - the darwin scratch side-channel POLICY. NOTE: this file's name
// ends in _darwin_test.go, which Go reads as an implicit darwin build
// constraint, so it only runs ON a Mac; the linux CI runner executes the same
// decisions from scratch_policy_test.go (untagged). Keep both in step.
//
// The decisions live in scratch_policy.go (untagged) and take the OS-relevant
// values as arguments, so the linux CI runner exercises the darwin policy
// without a Mac. The command half is exercised through scratchCmdRunner, the
// same injectable-runner seam capabilities/detect.go uses for nvidia-smi and
// detect_darwin_parse.go uses for system_profiler: the test records the argv
// MOD would run and the fail-soft behaviour when the command is missing or
// fails.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const taskScratch = "/Volumes/MOD/scratch/native/4242"
const macHome = "/Users/tim"

// --- tool cache + temp redirection ----------------------------------------

func TestScratchEnvRedirectsNamesScratchSubdirs(t *testing.T) {
	got := scratchEnvRedirects(taskScratch)
	want := map[string]string{
		"TMPDIR":                  taskScratch + "/tmp",
		"TEMP":                    taskScratch + "/tmp",
		"TMP":                     taskScratch + "/tmp",
		"XDG_CACHE_HOME":          taskScratch + "/cache",
		"PIP_CACHE_DIR":           taskScratch + "/cache/pip",
		"HF_HOME":                 taskScratch + "/cache/hf",
		"HF_HUB_CACHE":            taskScratch + "/cache/hf/hub",
		"TORCH_HOME":              taskScratch + "/cache/torch",
		"TRITON_CACHE_DIR":        taskScratch + "/cache/triton",
		"NUMBA_CACHE_DIR":         taskScratch + "/cache/numba",
		"DOT_LOCAL_LOCATION":      taskScratch + "/cache/local",
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

func TestScratchEnvRedirectsNeverEscapesScratch(t *testing.T) {
	// The whole point: no redirect value may land in the owner's home, which is
	// exactly where Time Machine and Spotlight are watching. Only the DIR keys
	// are paths - the crash-suppression keys are flags (CRASH_REPORTING=0).
	for _, k := range scratchEnvDirKeys() {
		if v := scratchEnvRedirects(taskScratch)[k]; !strings.HasPrefix(v, taskScratch) {
			t.Fatalf("redirect %s escapes the scratch root: %q", k, v)
		}
	}
}

func TestScratchEnvRedirectsIsPerTask(t *testing.T) {
	// Two tasks on one box must not share a cache: the DIRECTORY values are
	// disjoint (the crash-suppression flags are per-task by definition).
	a := scratchEnvRedirects(taskScratch)
	b := scratchEnvRedirects("/Volumes/MOD/scratch/native/9001")
	for _, k := range scratchEnvDirKeys() {
		if b[k] == a[k] {
			t.Fatalf("tasks share the %s cache path %q", k, a[k])
		}
	}
}

// --- precedence: the task spec wins over MOD's redirect -------------------

func TestScratchEnvOverlayBeatsHostAndKeepsSpecKeys(t *testing.T) {
	// buildEnv's order: host allow-list, then spec.Env, then spec.NativeEnv. The
	// host's TMPDIR must be overridden (that is the leak MAC.1.s13 is fixing)...
	env := []string{"PATH=/bin", "TMPDIR=/var/tmp", "HOME=" + macHome}
	redirects := scratchEnvRedirects(taskScratch)
	// ...but a curated recipe that named XDG_CACHE_HOME itself keeps ITS value.
	spec := &TaskSpec{NativeEnv: map[string]string{"XDG_CACHE_HOME": "/Volumes/RAMDISK/cache"}}
	out := scratchEnvOverlay(env, redirects, scratchSpecKeys(spec))
	seen := make(map[string]string)
	for _, kv := range out {
		eq := strings.Index(kv, "=")
		if eq < 0 {
			t.Fatalf("env entry %q has no separator", kv)
		}
		// Last wins in exec.Cmd's Env list, so later entries overwrite earlier ones.
		seen[kv[:eq]] = kv[eq+1:]
	}
	if seen["TMPDIR"] != taskScratch+"/tmp" {
		t.Fatalf("host TMPDIR %q must be overridden, got %q", "/var/tmp", seen["TMPDIR"])
	}
	if seen["XDG_CACHE_HOME"] != "/Volumes/RAMDISK/cache" {
		t.Fatalf("spec-named XDG_CACHE_HOME must survive, got %q", seen["XDG_CACHE_HOME"])
	}
	if seen["PATH"] != "/bin" || seen["HOME"] != macHome {
		t.Fatalf("overlay must not drop unrelated host entries")
	}
}

func TestApplyScratchEnvRedirectReservesSpecKeys(t *testing.T) {
	spec := &TaskSpec{Env: map[string]string{"HF_HOME": "/Volumes/RAMDISK/hf"}}
	applyScratchEnvRedirect(spec, taskScratch)
	if spec.NativeEnv["HF_HOME"] != "" {
		t.Fatalf("applyScratchEnvRedirect must not overwrite a spec-named HF_HOME")
	}
	if spec.NativeEnv["PIP_CACHE_DIR"] != taskScratch+"/cache/pip" {
		t.Fatalf("unset keys must be redirected, got %q", spec.NativeEnv["PIP_CACHE_DIR"])
	}
}

func TestApplyScratchEnvRedirectIgnoresNilSpec(t *testing.T) {
	applyScratchEnvRedirect(nil, taskScratch)
	applyScratchEnvRedirect(&TaskSpec{}, "")
}

// --- iCloud refusal -------------------------------------------------------

func TestICloudRefusalRejectsSyncedRoots(t *testing.T) {
	want := []string{
		macHome + "/Library/Mobile Documents",
		macHome + "/Desktop",
		macHome + "/Documents",
	}
	got := iCloudSyncedRoots(macHome)
	if len(got) != len(want) {
		t.Fatalf("synced-root list drifted: %d vs %d", len(got), len(want))
	}
	for i, r := range want {
		if got[i] != r {
			t.Fatalf("synced root %d must be %s, got %q", i, r, got[i])
		}
	}
	// The scratch root itself, and every task dir under it, are refused.
	for _, root := range want {
		if iCloudRefusal(root, macHome) != root {
			t.Fatalf("scratch root %q must be refused", root)
		}
		inside := root + "/native/4242/input"
		if iCloudRefusal(inside, macHome) != root {
			t.Fatalf("scratch under %q must be refused", root)
		}
	}
}

func TestICloudRefusalIsCaseInsensitive(t *testing.T) {
	// APFS default is case-insensitive, so ~/desktop IS ~/Desktop and syncs too.
	if iCloudRefusal(macHome+"/desktop/native/1", macHome) != macHome+"/Desktop" {
		t.Fatalf("case-variant of a synced root must still be refused")
	}
	if iCloudRefusal(macHome+"/LIBRARY/mobile documents/x", macHome) !=
		macHome+"/Library/Mobile Documents" {
		t.Fatalf("case-variant of the iCloud Drive mount must still be refused")
	}
}

func TestICloudRefusalAcceptsSafeRoots(t *testing.T) {
	// An external volume, the sibling of a synced dir, and a deeper path that
	// merely CONTAINS the name are all fine - the refusal is containment, not a
	// substring match.
	safe := []string{
		taskScratch,
		macHome + "/Downloads/native/1",
		macHome + "/Desktopish/native/1",
		macHome + "/Library/Containers/native/1",
		"/Volumes/RAMDISK/native/1",
	}
	for _, p := range safe {
		if iCloudRefusal(p, macHome) != "" {
			t.Fatalf("safe scratch root %q was refused", p)
		}
	}
}

func TestICloudRefusalWithoutHomeIsNoOp(t *testing.T) {
	// HOME unset: nothing to refuse against (the scratch root is explicit
	// configuration the caller controls), so the policy stays a no-op.
	if iCloudRefusal(taskScratch, "") != "" {
		t.Fatalf("no home means no synced roots")
	}
	if iCloudRefusal("", macHome) != "" {
		t.Fatalf("empty scratch root means nothing to check")
	}
}

// The ".nosync" naming convention is iCloud's own: a component at or below the
// synced root ending in .nosync is not synced, so such a scratch root is already
// out of the cloud and the refusal must not fire on it.
func TestICloudNosyncNamingOptsOut(t *testing.T) {
	if iCloudRefusal(macHome+"/Documents/mod-scratch.nosync/native/42", macHome) != "" {
		t.Fatalf("a .nosync component under a synced root is already out of iCloud")
	}
	if iCloudRefusal(macHome+"/Documents/native/42", macHome) == "" {
		t.Fatalf("a plain path under ~/Documents must still be refused")
	}
	// A .nosync component ABOVE the synced root does nothing for the tree below
	// it: the home itself carries the suffix, so the synced root is
	// <home>.nosync/Documents and the scratch under it is still refused.
	if iCloudRefusal(macHome+".nosync/Documents/native/42", macHome+".nosync") == "" {
		t.Fatalf("a .nosync component above the synced root does not opt the tree out")
	}
	if !iCloudNosyncOptOut(macHome+"/Documents/a.nosync", macHome+"/Documents") {
		t.Fatalf("opt-out predicate must see the component")
	}
	if iCloudNosyncOptOut(macHome+"/Documents/a", macHome+"/Documents") {
		t.Fatalf("no .nosync component means no opt-out")
	}
	if iCloudNosyncOptOut(macHome+"/Documents", macHome+"/Documents") {
		t.Fatalf("the synced root itself is not a component below it")
	}
}

func TestScratchRootInCloudMessageNamesRootNotBytes(t *testing.T) {
	msg := scratchRootInCloudMessage(macHome+"/Desktop/native/1", macHome+"/Desktop")
	if !strings.Contains(msg, macHome+"/Desktop") {
		t.Fatalf("refusal must name the synced root: %q", msg)
	}
	if !strings.Contains(msg, "Mobile Documents") {
		t.Fatalf("refusal must tell the operator where to move the scratch root: %q", msg)
	}
}

// --- the commands darwin runs (mocked runner seam) ------------------------

func TestScratchExclusionArgsIsPathFormArgv(t *testing.T) {
	argv := scratchExclusionArgs(taskScratch)
	want := []string{"tmutil", "addexclusion", "-p", taskScratch}
	if len(argv) != len(want) {
		t.Fatalf("tmutil argv drifted: %d vs %d", len(argv), len(want))
	}
	for i, a := range want {
		if argv[i] != a {
			t.Fatalf("tmutil argv[%d] must be %s, got %q", i, a, argv[i])
		}
	}
	// A scratch dir whose name contains a shell metacharacter must survive as ONE
	// argv element: MOD never shells out, so it cannot break out of the command.
	tricky := "/Users/tim/scratch/it's here/42"
	if got := scratchExclusionArgs(tricky); len(got) != 4 || got[3] != tricky {
		t.Fatalf("quoted path must stay one argv element")
	}
	if len(scratchExclusionArgs("")) != 0 {
		t.Fatalf("empty scratch dir means no command")
	}
}

func TestScratchCrashReportArgsAreEmpty(t *testing.T) {
	// The honesty check behind "suppress crash reports": MOD does it through the
	// task's OWN environment (CRASH_REPORTING=0), never a global launchctl/daemon
	// change that would silence every crash on the owner's machine.
	if len(scratchCrashReportArgs()) != 0 {
		t.Fatalf("darwin must not run a global crash-report command")
	}
	keys := scratchCrashReportKeys()
	if len(keys) != 2 || keys[0] != "CRASH_REPORTING" || keys[1] != "GTEST_DISABLE_CORE_DUMP" {
		t.Fatalf("crash-suppression keys drifted: %v", keys)
	}
}

func TestRunScratchExclusionIsFailSoft(t *testing.T) {
	// The seam: a recorder proves the argv, and a failing tmutil must NOT become a
	// task failure - runScratchExclusion returns the error, the CALLER ignores it.
	var ran []string
	scratchCmdRunner = func(argv []string) error {
		if len(argv) == 0 {
			t.Fatalf("runner must not be called with empty argv")
		}
		ran = argv
		return errors.New("tmutil: command not found")
	}
	err := runScratchExclusion(taskScratch)
	if ran[0] != "tmutil" || ran[2] != "-p" || ran[3] != taskScratch {
		t.Fatalf("runner saw the wrong argv: %v", ran)
	}
	if err == nil {
		t.Fatalf("a failing tmutil must be reported, not hidden")
	}
	// Fail-soft contract: the caller discards it (scratch_darwin.go does `_ =`).
	_ = err
}

func TestRunScratchExclusionSkipsEmptyDir(t *testing.T) {
	called := false
	scratchCmdRunner = func(argv []string) error {
		called = true
		return nil
	}
	if runScratchExclusion("") != nil {
		t.Fatalf("empty scratch dir must not fail")
	}
	if called {
		t.Fatalf("empty scratch dir must not run tmutil")
	}
}

func TestScratchPostureForOS(t *testing.T) {
	if scratchPostureForOS("darwin") !=
		"tmutil-exclusion+never-index+env-redirect+crash-suppressed" {
		t.Fatalf("darwin posture must name only layers it applies")
	}
	for _, other := range []string{"linux", "windows", ""} {
		if scratchPostureForOS(other) != "none-required" {
			t.Fatalf("%s has no OS daemons; posture must say none-required", other)
		}
	}
}

// The darwin build's mkdir step is build-tagged, so the linux runner exercises
// the SAME key set against a real filesystem: what scratchEnvDirKeys promises
// must be creatable, nested under the scratch root, and never a flag key.
func TestScratchEnvDirKeysAreCreatableScratchDirs(t *testing.T) {
	root := filepath.Join(os.TempDir(), "mod-s13-scratch-4242")
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
	// The marker path stays inside the scratch root too (it is written at the
	// ROOT, one level above the per-task dir).
	marker := filepath.Join(root, scratchNeverIndexMarker())
	if !strings.HasPrefix(marker, root) || marker == root {
		t.Fatalf("marker path %q escapes the scratch root", marker)
	}
}

// The Spotlight opt-out is a marker FILE (mdutil -i off is volume-wide and
// would turn search off for the whole disk), and it is dot-prefixed so the
// indexer's own convention skips it.
func TestScratchNeverIndexMarkerName(t *testing.T) {
	marker := scratchNeverIndexMarker()
	if marker != ".metadata_never_index" {
		t.Fatalf("marker name drifted: %q", marker)
	}
	if !strings.HasPrefix(marker, ".") {
		t.Fatalf("marker must be dot-prefixed")
	}
}
