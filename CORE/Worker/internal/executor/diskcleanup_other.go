//go:build !linux

package executor

// statfsUsedPct is unavailable off Linux (the worker only ships Linux
// binaries). Returning (0, false) makes the threshold sweep skip its tick;
// the post-build dangling/cache prune still works since it needs no disk
// stats. Keeps `go build`/`go vet` green on macOS dev machines.
func statfsUsedPct(path string) (int, bool) {
	return 0, false
}

// getDiskSpace is unavailable off Linux; the LRU prune then reclaims nothing.
func getDiskSpace(path string) (total, available uint64, ok bool) {
	return 0, 0, false
}
