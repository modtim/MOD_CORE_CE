//go:build darwin

package executor

// MAC.1.s13 - darwin build of native-task scratch side-channel discipline.
//
// Thin wrapper, same shape as native_confine_darwin.go: every DECISION lives
// in scratch_policy.go (untagged, pure, exercised on linux); this file only
// applies it with darwin's commands and filesystem calls.
//
// What MOD does on a Mac before a native task touches its scratch dir:
//  1. REFUSE a scratch root inside a path iCloud syncs (the one hard failure -
//     task bytes under ~/Desktop, ~/Documents or ~/Library/Mobile Documents
//     leave the box);
//  2. `tmutil addexclusion -p <dir>` so Time Machine never backs the tree up;
//  3. drop a .metadata_never_index marker so Spotlight skips the tree (mdutil -i
//     off is volume-wide and would turn search off for the whole disk);
//  4. create the tmp/cache subdirs the redirected environment points at;
//  5. suppress crash reports for the task process through its own environment
//     (scratchCrashReportKeys) - never a global launchctl/daemon change.
//
// Steps 2-5 are fail-soft: a missing tmutil, a permission denial, an
// unrecognised macOS release - log and continue. Only step 1 fails a task,
// and it fails it because the alternative (proceeding) is the leak.
//
// The per-task encrypted sparsebundle GP built is NOT re-added here: encrypted
// scratch stays exactly where encrypted_scratch.go + the scratchFactory hook in
// native_staging.go keep it.

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
)

// scratchPostureOnce guards the one audit-trail line per process that states,
// honestly, which side-channel layers this darwin worker applied (and which it
// could not).
var scratchPostureOnce sync.Once

// applyScratchDiscipline is the hook native_staging.go calls once per staged
// native task, before any byte is staged. It returns an error ONLY for the
// iCloud-synced scratch root; every other failure mode is a log line and the
// task runs.
func applyScratchDiscipline(scratchDir string, spec *TaskSpec) error {
	if scratchDir == "" {
		return nil
	}
	home := os.Getenv("HOME")
	offender := iCloudRefusal(scratchDir, home)
	if offender != "" {
		// The one hard refusal. The text names the synced root and the fix, never
		// the task's bytes.
		return scratchRootInCloud(scratchDir, offender)
	}
	// Fail-soft side: hygiene, never a gate.
	_ = runScratchExclusion(scratchDir)
	_ = writeScratchNeverIndexMarker(scratchDir)
	_ = makeScratchEnvDirs(scratchDir)
	_ = spec
	announceScratchPosture()
	return nil
}

// scratchRootInCloud is the refusal. No sentinel error type: there is no retry
// worth attempting (the scratch root is worker configuration, not task data), so
// a plain message the caller wraps is the right shape.
func scratchRootInCloud(scratchDir, offender string) error {
	msg := scratchRootInCloudMessage(scratchDir, offender)
	return fmt.Errorf("executor: darwin scratch discipline: %s", msg)
}

// writeScratchNeverIndexMarker drops the Spotlight opt-out file in the
// directory handed to it (the per-task scratch dir, which is what native_staging
// passes): the indexer's convention is "a directory holding this file is
// skipped", so the marker sits at the head of the tree MOD is about to write
// under, and a task dir created later by another staging pass drops its own.
// Fail-soft: a read-only mount or a pre-existing marker is a warn.
func writeScratchNeverIndexMarker(scratchRoot string) error {
	// "w" + close, never a truncate-then-write of task bytes: the file is a zero-
	// byte flag whose only meaning is its name.
	path := filepath.Join(scratchRoot, scratchNeverIndexMarker())
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		slog.Default().Warn("macos-scratch-never-index-failed",
			"dir", scratchRoot,
			"note", "Spotlight may index this scratch tree; the task continues")
		return err
	}
	if cerr := f.Close(); cerr != nil {
		return cerr
	}
	return nil
}

// makeScratchEnvDirs creates the tmp/cache roots the redirected environment
// points at, so a tool that trusts its TMPDIR/XDG_CACHE_HOME does not fail on a
// missing directory. Best-effort: a tool that cannot write its cache just loses
// its cache.
func makeScratchEnvDirs(scratchDir string) error {
	redirects := scratchEnvRedirects(scratchDir)
	var lastErr error
	for _, k := range scratchEnvDirKeys() {
		dir := redirects[k]
		if dir == "" {
			continue
		}
		err := os.MkdirAll(dir, 0755)
		if err != nil {
			lastErr = err
		}
	}
	if lastErr != nil {
		slog.Default().Warn("macos-scratch-env-dirs-failed",
			"dir", scratchDir,
			"note", "a tool may fall back to its default cache; the task continues")
	}
	return lastErr
}

// announceScratchPosture logs the honest posture once per process: which layers
// this darwin worker applied, and the two it could NOT (the kernel core-dump
// limit needs a shell wrapper native recipes do not have; Spotlight is opted
// out per directory, not per volume).
func announceScratchPosture() {
	scratchPostureOnce.Do(func() {
		slog.Default().Info("macos-scratch-discipline-report",
			"layers", scratchPostureForOS("darwin"),
			"crash_reports", "per-process CRASH_REPORTING=0 (no global launchctl change)",
			"not_claimed", "kernel core-dump ulimit (no shell wrapper); mdutil -i off is volume-wide and unused")
	})
}
