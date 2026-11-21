package executor

// MAC.1.s13 - OS side-channel discipline for native task scratch on a Mac.
//
// A native (imageless) task runs as a host process, so its scratch dir is a
// directory on the machine the worker owns - the one place MOD writes
// tenant-shaped bytes onto the owner's filesystem. macOS has daemons that
// will happily follow that directory, and none of them are MOD's:
//
//   - Time Machine backs up every regular directory it can see,
//   - Spotlight indexes directory contents for search,
//   - iCloud syncs ~/Desktop, ~/Documents and ~/Library/Mobile Documents to
//     the cloud,
//   - ReportCrashes writes a full crash report (arguments + environment: the
//     task's file paths and its per-task creds) to
//     ~/Library/Logs/DiagnosticReports when a task process dies.
//
// Every DECISION here is pure and takes the OS-relevant values as arguments,
// exactly like native_confine_policy.go, so the linux CI runner exercises the
// darwin policy without a Mac. scratch_darwin.go applies the decisions with
// darwin's commands; scratch_other.go is the no-op twin for every other OS.
//
// This is HYGIENE, NOT SECURITY, and it is fail-soft by design: a missing
// tmutil, a permission denial, an unknown macOS release - all log and
// continue. A task is never blocked because Spotlight could not be told to
// skip a directory. The one hard refusal is the scratch ROOT sitting inside a
// path iCloud syncs: that is a real cross-box leak and no "keep going" fixes
// it. The security posture of a darwin native task is MAC.1.s12's
// (native_confine_policy.go); the per-task encrypted sparsebundle GP built
// stays exactly where encrypted_scratch.go + the scratchFactory hook keep it
// and is NOT re-added here.

import (
	"fmt"
	"log/slog"
	"os/exec"
	"path/filepath"
	"strings"
)

// scratchCmdRunner is the mockable seam for the macOS side-channel commands
// (tmutil) - the same injectable-runner pattern capabilities/detect.go uses
// for nvidia-smi and detect_darwin_parse.go uses for system_profiler, so CI
// feeds a recorder instead of needing a Mac. It is a var, not a const:
// scratch_darwin_test.go swaps it to assert the exact argv MOD would run.
var scratchCmdRunner = func(argv []string) error {
	if len(argv) == 0 {
		return nil
	}
	// The seam exists so CI can feed a recorder, so the DEFAULT carries an
	// allow-list: without it the var is an execution path for arbitrary argv
	// (a swapped runner, a future caller), and a hygiene hook has no business
	// running anything but the macOS side-channel daemons' commands. A refusal
	// is an error the fail-soft callers already log.
	if !scratchKnownCommand(argv[0]) {
		return fmt.Errorf("executor: scratch discipline runner refuses %q (not a macOS side-channel command)", argv[0])
	}
	// Output() (not Run()) so a non-zero exit becomes an error the caller logs;
	// tmutil's stdout is never interesting to the worker.
	_, err := exec.Command(argv[0], argv[1:]...).Output()
	return err
}

// scratchKnownCommand is the allow-list behind that seam: the only OS-daemon
// commands MAC.1.s13 issues. tmutil is the Time Machine exclusion; mdutil is
// listed so a release that grows a scoped Spotlight command changes one place
// (MOD does not run it today - scratchNeverIndexMarker is the per-directory
// opt-out instead).
func scratchKnownCommand(name string) bool {
	switch name {
	case "tmutil", "mdutil":
		return true
	}
	return false
}

// scratchEnvRedirects is the environment a native task process gets so its
// tool caches and temp files land INSIDE the task's scratch dir instead of the
// shared per-user locations they default to (~/.cache, ~/Library/Caches, the
// HF hub cache, pip's wheel cache, /var/tmp). Two independent reasons, both
// real: a Mac's home directory belongs to the OWNER and MOD must not scatter
// one task's bytes into it where Time Machine and Spotlight will see them; and
// two tasks on the same box must not fight over one shared cache (a stale HF
// snapshot from task A silently satisfying task B).
//
// The map is OS-neutral - the same keys a linux worker wants - and PURE, so
// the linux test runner asserts it byte for byte. darwin differs in the DAEMONS
// that watch a directory, not in what the tools call their cache.
func scratchEnvRedirects(scratchDir string) map[string]string {
	cache := filepath.Join(scratchDir, "cache")
	return map[string]string{
		// Temp: the task's own scratch, never the shared /var/tmp.
		"TMPDIR": filepath.Join(scratchDir, "tmp"),
		"TEMP":   filepath.Join(scratchDir, "tmp"),
		"TMP":    filepath.Join(scratchDir, "tmp"),
		// The shared "my tools put stuff here" root.
		"XDG_CACHE_HOME": cache,
		// Per-tool caches the curated darwin recipes actually write.
		"PIP_CACHE_DIR":      filepath.Join(cache, "pip"),
		"HF_HOME":            filepath.Join(cache, "hf"),
		"HF_HUB_CACHE":       filepath.Join(cache, "hf", "hub"),
		"TORCH_HOME":         filepath.Join(cache, "torch"),
		"TRITON_CACHE_DIR":   filepath.Join(cache, "triton"),
		"NUMBA_CACHE_DIR":    filepath.Join(cache, "numba"),
		"DOT_LOCAL_LOCATION": filepath.Join(cache, "local"),
		// Crash suppression for the task process (see scratchCrashReportKeys):
		// CRASH_REPORTING=0 is the per-process knob ReportCrashes honors, so a
		// dying native recipe writes no report into the owner's
		// ~/Library/Logs/DiagnosticReports - those reports quote the process's
		// arguments and environment, i.e. the task's paths and its per-task
		// creds. GTEST_DISABLE_CORE_DUMP is the tool-facing core-dump knob:
		// MOD cannot wrap a native recipe in `sh -c 'ulimit -c 0; ...'`
		// (MAC.1.s12 says native recipes have no shell wrapper), so the
		// kernel-side ulimit is NOT claimed here - only what the tools honor.
		"CRASH_REPORTING":         "0",
		"GTEST_DISABLE_CORE_DUMP": "1",
	}
}

// scratchCrashReportKeys are the environment keys whose ONLY purpose is crash
// suppression, kept separate from the cache/temp redirects so the test runner
// can assert "crash suppression is present" without re-listing every cache
// path, and so a future OS that grows a real per-process crash-report knob
// changes one place.
func scratchCrashReportKeys() []string {
	return []string{"CRASH_REPORTING", "GTEST_DISABLE_CORE_DUMP"}
}

// scratchSpecKeys lists the environment keys the TASK spec itself set (Env +
// NativeEnv), which is what the redirect must not overwrite. Pure over
// *TaskSpec so the linux test runner can exercise precedence without a darwin
// build.
func scratchSpecKeys(spec *TaskSpec) []string {
	if spec == nil {
		return []string{}
	}
	keys := make([]string, 0, len(spec.Env)+len(spec.NativeEnv))
	for k := range spec.Env {
		keys = append(keys, k)
	}
	for k := range spec.NativeEnv {
		keys = append(keys, k)
	}
	return keys
}

// scratchEnvOverlay merges the redirect map into the ordered "k=v" environment
// buildEnv assembled. Precedence mirrors native_backend.go's buildEnv order
// (host allow-list, then spec.Env, then spec.NativeEnv): MOD's redirect beats
// the INHERITED host value - that is the whole point, the host's TMPDIR and
// ~/.cache defaults must not leak in - but never a key the task spec itself
// named, because a curated recipe that wants its cache on another volume says
// so in NativeEnv and wins. specKeys is exactly that: the keys the spec set.
//
// Pure and allocation-only: it never mutates the caller's list, so the
// staging/submit path can call it on a shared env without a lock dance.
func scratchEnvOverlay(env []string, redirects map[string]string, specKeys []string) []string {
	reserved := make(map[string]bool, len(specKeys))
	for _, k := range specKeys {
		// exec.Cmd's Env list is last-wins, so appending the overlay after the
		// spec entries would already be enough; skipping reserved keys is what
		// keeps the spec's own value (not MOD's) in the list at all.
		reserved[k] = true
	}
	out := make([]string, 0, len(env)+len(redirects))
	for _, kv := range env {
		out = append(out, kv)
	}
	for k, v := range redirects {
		if reserved[k] {
			continue
		}
		out = append(out, k+"="+v)
	}
	return out
}

// ---------------------------------------------------------------------------
// iCloud refusal
// ---------------------------------------------------------------------------

// iCloudSyncedRoots are the darwin paths iCloud (and, for ~/Desktop +
// ~/Documents, the iCloud Desktop/Documents feature) syncs to the cloud. Task
// bytes under any of them leave the box - a cross-tenant/cross-box leak that
// no amount of "log and continue" can undo, so this is the ONE hard refusal in
// MAC.1.s13. The list is a function (not a const array) so the test runner can
// exercise it against a synthetic home without editing the policy.
func iCloudSyncedRoots(home string) []string {
	if home == "" {
		// No home: nothing to refuse against (a worker with HOME unset runs its
		// scratch under an explicit configured root, which the caller controls).
		return []string{}
	}
	return []string{
		// ~/Library/Mobile Documents is the iCloud Drive mount (the space in
		// the name is real and must be preserved).
		home + "/Library/Mobile Documents",
		home + "/Desktop",
		home + "/Documents",
	}
}

// iCloudNosyncOptOut is the naming half of the iCloud opt-out: a directory
// inside iCloud Drive whose name ends in ".nosync" is NOT synced (the iCloud
// daemon's own convention, the same reason a user names a folder that way). A
// scratch root that carries such a component is already out of the cloud by the
// owner's own hand, so the refusal must not fire on it - refusing it would
// punish a configuration that is already correct. Only components at or below
// the synced root count (a ".nosync" ABOVE ~/Documents does nothing for the
// tree under it).
func iCloudNosyncOptOut(scratchRoot, syncedRoot string) bool {
	np := normalizeDarwinPath(scratchRoot)
	pp := normalizeDarwinPath(syncedRoot)
	if np == "" || pp == "" || !darwinInside(np, pp) {
		return false
	}
	for _, part := range strings.Split(np[len(pp)+1:], "/") {
		if strings.HasSuffix(part, ".nosync") {
			return true
		}
	}
	return false
}

// iCloudRefusal is the pure decision: does a scratch root sit inside a path
// iCloud syncs? Case-insensitive on darwin (APFS default is case-insensitive,
// so ~/desktop and ~/Desktop are the SAME directory and both sync). Returns
// the offending synced root (for the error text) or "" when the scratch root
// is safe.
func iCloudRefusal(scratchRoot, home string) string {
	needle := normalizeDarwinPath(scratchRoot)
	if needle == "" {
		return ""
	}
	for _, root := range iCloudSyncedRoots(home) {
		inside := normalizeDarwinPath(root)
		if inside == "" {
			continue
		}
		if sameDarwinPath(needle, inside) || darwinInside(needle, inside) {
			if iCloudNosyncOptOut(needle, inside) {
				continue
			}
			return root
		}
	}
	return ""
}

// normalizeDarwinPath collapses a path for comparison: absolute, no trailing
// separator, no doubled separators. filepath.Abs is NOT used (it consults the
// process cwd, which is irrelevant to a darwin home-relative decision), so the
// caller passes an absolute scratch root - which is exactly what main.go:1803
// hands NewNativeBackend.
func normalizeDarwinPath(p string) string {
	if p == "" {
		return ""
	}
	trimmed := p
	for len(trimmed) > 1 && strings.HasSuffix(trimmed, "/") {
		trimmed = trimmed[:len(trimmed)-1]
	}
	// filepath.Join collapses doubled separators without touching the cwd.
	if strings.HasPrefix(trimmed, "/") {
		return "/" + filepath.Join(trimmed[1:])
	}
	return filepath.Join(trimmed)
}

// sameDarwinPath compares two paths the way APFS does by default: case- and
// unicode-insensitive enough that "Desktop" and "desktop" are one directory.
func sameDarwinPath(a, b string) bool {
	return strings.ToLower(normalizeDarwinPath(a)) == strings.ToLower(normalizeDarwinPath(b))
}

// darwinInside is the containment half of the refusal: is p at or below
// parent (p == parent is handled by sameDarwinPath, so this is the strict
// "under" case).
func darwinInside(p, parent string) bool {
	np := normalizeDarwinPath(p)
	pp := normalizeDarwinPath(parent)
	if pp == "/" || np == "" || pp == "" {
		return false
	}
	needle := pp + "/"
	if len(np) < len(needle) {
		return false
	}
	return strings.ToLower(np[:len(needle)]) == strings.ToLower(needle)
}

// scratchExclusionArgs is the tmutil invocation that keeps Time Machine off a
// scratch tree: `tmutil addexclusion -p <dir>` (-p = path form, which works on
// a directory that is not yet a volume-root child; the bare positional form is
// the interactive one). Returned as argv, never a shell string: MOD does not
// shell out to tmutil, so a scratch dir name containing a quote cannot break
// out of the command.
func scratchExclusionArgs(scratchDir string) []string {
	if scratchDir == "" {
		return []string{}
	}
	return []string{"tmutil", "addexclusion", "-p", scratchDir}
}

// runScratchExclusion is the fail-soft wrapper: it runs the tmutil argv through
// scratchCmdRunner and turns EVERY failure (tmutil missing, already-excluded,
// permission denied) into a warn. Untagged on purpose - the runner seam is
// injectable, so the linux test runner proves the "log and continue" half
// without a Mac, and scratch_darwin.go just calls this.
func runScratchExclusion(scratchDir string) error {
	argv := scratchExclusionArgs(scratchDir)
	if len(argv) == 0 {
		return nil
	}
	err := scratchCmdRunner(argv)
	if err == nil {
		return nil
	}
	slog.Default().Warn("macos-scratch-exclusion-failed",
		"dir", scratchDir,
		"cmd", strings.Join(argv, " "),
		"note", "Time Machine may back this scratch tree up; the task continues")
	return err
}

// scratchCrashReportArgs is the OS-level half of crash-report suppression. It
// is deliberately EMPTY: ReportCrashes is a launchd-spawned daemon MOD must
// not stop (a root daemon off the box silences every real crash on the machine,
// and `launchctl setenv CRASH_REPORTING 0` is process-tree-global - it would
// also silence the GUI apps the curated recipes drive). The per-process handle
// macOS gives a host process is its own environment, which is
// scratchCrashReportKeys. Kept as a function returning argv so the test runner
// can assert "MOD never runs a global crash-report command" and a future
// release that grows a scoped command changes one place.
func scratchCrashReportArgs() []string {
	return []string{}
}

// scratchNeverIndexMarker is the Spotlight opt-out. `mdutil -i off` is VOLUME-
// wide (it would turn search off for the whole disk, exactly the wrong thing on
// the owner's machine), so the per-directory knob is the dot-prefix convention
// the indexing daemon honors: a directory holding a .metadata_never_index file
// is skipped. The marker file is used instead of a leading-dot directory name
// because the scratch root's name is a contract with cleanup/osfs.go's native
// Subdir and main.go's scratch layout - renaming it would break that.
func scratchNeverIndexMarker() string { return ".metadata_never_index" }

// scratchRootInCloudMessage is the refusal's text, kept in the untagged policy
// so the linux test runner asserts the exact wording without compiling a darwin
// build. It names the synced root and the fix; it never names the task's files,
// keys or creds (the scratch root is worker configuration, already in the log).
func scratchRootInCloudMessage(scratchRoot, syncedRoot string) string {
	_ = strings.TrimSpace(scratchRoot)
	return "native scratch root sits inside a path iCloud syncs to the cloud (" +
		syncedRoot + "): task bytes would leave this box. Move the worker scratch root " +
		"off ~/Desktop, ~/Documents and ~/Library/Mobile Documents."
}

// scratchPostureForOS is the pure half of the scratch-discipline posture token
// (OS as an argument, so the linux test runner asserts the darwin wording
// without a Mac). It names only layers the build ACTUALLY applies: darwin gets
// the tmutil exclusion, the Spotlight never-index marker, the env redirect and
// the per-process crash suppression; every other OS has no OS daemons to
// appease, so it reports "none-required" rather than pretending layers ran.
func scratchPostureForOS(osName string) string {
	if osName == "darwin" {
		return "tmutil-exclusion+never-index+env-redirect+crash-suppressed"
	}
	return "none-required"
}

// scratchEnvDirKeys are the redirect keys whose VALUE is a directory the worker
// has to create (as opposed to a flag like CRASH_REPORTING=0). The darwin build
// mkdirs exactly this set, so the two can never disagree about which
// directories the redirected environment promises exist.
func scratchEnvDirKeys() []string {
	return []string{
		"TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME",
		"PIP_CACHE_DIR", "HF_HOME", "HF_HUB_CACHE", "TORCH_HOME",
		"TRITON_CACHE_DIR", "NUMBA_CACHE_DIR", "DOT_LOCAL_LOCATION",
	}
}

// applyScratchEnvRedirect merges the cache/temp redirect into a TaskSpec's
// NativeEnv, which is the map native_backend.go's buildEnv appends LAST when it
// assembles the process environment - so the redirect beats the inherited host
// TMPDIR/~/.cache defaults WITHOUT touching nativeHostEnv (the host allow-list
// stays a single source of truth). Keys the spec itself already set (Env or
// NativeEnv) are left alone: a curated recipe that wants its cache on another
// volume says so and wins.
//
// Untagged and OS-neutral on purpose: the tools call their cache the same way
// on every OS, so linux gets the same per-task isolation (and the linux test
// runner asserts it), while darwin additionally has the daemons to appease.
func applyScratchEnvRedirect(spec *TaskSpec, scratchDir string) {
	if spec == nil || scratchDir == "" {
		return
	}
	reserved := make(map[string]bool, len(spec.Env)+len(spec.NativeEnv))
	for k := range spec.Env {
		reserved[k] = true
	}
	for k := range spec.NativeEnv {
		reserved[k] = true
	}
	// A fresh map, never an in-place write: a TaskSpec built by an older caller
	// has a NIL NativeEnv map and Go panics on assigning into one. Replacing the
	// field keeps every existing native caller byte-for-byte unchanged.
	merged := make(map[string]string, len(spec.NativeEnv)+len(scratchEnvRedirects(scratchDir)))
	for k, v := range spec.NativeEnv {
		merged[k] = v
	}
	for k, v := range scratchEnvRedirects(scratchDir) {
		if reserved[k] {
			continue
		}
		merged[k] = v
	}
	spec.NativeEnv = merged
}
