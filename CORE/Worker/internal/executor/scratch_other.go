//go:build !darwin

package executor

// MAC.1.s13 - the no-op twin of scratch_darwin.go for every non-darwin OS.
//
// Same shape as native_confine_other.go: the DECISIONS live in scratch_policy.
// go (untagged, so the linux test runner exercises the darwin policy without a
// Mac) and the build-tagged files only apply them. On linux the native backend
// already has the confinement layer (MAC.1.s12's Pdeathsig + uid drop) and the
// scratch tree sits under MOD's own configured scratch root, which cleanup/
// osfs.go already owns and reaps; there is no Time Machine, no Spotlight and
// no iCloud daemon to appease. Windows keeps its own scratch hygiene in the
// existing diskcleanup_* files, which this file must not duplicate.
//
// The env redirect is the ONE thing applied on every OS, and native_staging.go
// applies it through the untagged scratch_policy.go helpers - not here - so
// linux keeps its per-task cache/temp isolation byte for byte and this file
// stays a true no-op for the OS-daemon half.

// applyScratchDiscipline is the non-darwin hook: no OS daemons watch a linux/
// windows scratch tree, so there is nothing to exclude and nothing to refuse.
func applyScratchDiscipline(scratchDir string, spec *TaskSpec) error {
	_ = scratchDir
	_ = spec
	return nil
}
