package localui

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// PortEnv is the environment variable that selects the local UI's listen
// port. Unset falls back to DefaultPort; "0" disables the UI entirely (the
// caller — cmd/worker's hook — must check for 0 itself and skip starting
// the server; this package does not treat 0 specially, so tests can still
// ask for an OS-assigned ephemeral port via Config.Port = 0 directly).
const PortEnv = "MOD_WORKER_LOCAL_UI_PORT"

// DefaultPort is used when PortEnv is unset.
const DefaultPort = 47821

// PortFromEnv resolves the configured local UI port: PortEnv unset ->
// DefaultPort; "0" -> 0 (disabled, caller's responsibility to skip Start);
// anything else parsed as a non-negative int, falling back to DefaultPort on
// a malformed value.
func PortFromEnv() int {
	raw := strings.TrimSpace(os.Getenv(PortEnv))
	if raw == "" {
		return DefaultPort
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		return DefaultPort
	}
	return n
}

// stateDirEnv mirrors internal/executor's imageGC stateDirEnv and
// internal/availability/history.go's historyStateDirEnv — duplicated here
// (rather than imported) to keep this package dependency-free of those
// implementation details, matching the existing convention in this repo.
const stateDirEnv = "MOD_WORKER_STATE_DIR"

// StateDir resolves the worker's local state directory, the same way the
// image-GC LRU cache and NOAH availability history do: stateDirEnv if set,
// else os.UserCacheDir()/mod-worker. Returns "" if neither is available
// (token persistence is then skipped — see Server.New).
func StateDir() string {
	dir := os.Getenv(stateDirEnv)
	if dir != "" {
		return dir
	}
	if home, err := os.UserCacheDir(); err == nil {
		return filepath.Join(home, "mod-worker")
	}
	return ""
}
