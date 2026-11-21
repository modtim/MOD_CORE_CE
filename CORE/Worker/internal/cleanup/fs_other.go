//go:build !linux && !darwin

package cleanup

// fsStat is unavailable on platforms without a portable statfs; the
// cleanup then reports reclaimable space but can never trigger image GC.
func fsStat(path string) (int64, int64, bool) { return 0, 0, false }
