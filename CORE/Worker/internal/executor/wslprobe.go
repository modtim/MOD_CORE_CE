package executor

import (
	"log/slog"
	"os"
	"strings"
	"sync"
)

// GSEC Q21.9 — WSL non-root worker probe. When the worker runs as the
// non-root mod-worker user on a WSL host, the NVIDIA WSL user-mode libraries
// live under a root-owned dir and the worker cannot open them without extra
// access. WSLLibReadable checks that access and returns a fix hint so the
// operator can act on it.

// defaultWSLLibDir is the standard WSL NVIDIA user-mode library directory.
const defaultWSLLibDir = "/usr/lib/wsl/lib"

// readProcFile is the injectable reader for WSL detection probes; tests swap
// it to feed synthetic /proc contents without any WSL host or mocking of os.
var readProcFile func(path string) (string, bool) = func(path string) (string, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	return string(data), true
}

// listDir is the injectable directory reader for the lib-dir readability
// probe; tests swap it to simulate unreadable/absent dirs on any host.
var listDir = os.ReadDir

// IsWSL reports whether this host is running under Windows Subsystem for
// Linux. Detection: the WSLInterop binfmt_misc entry exists, or /proc/version
// mentions "microsoft". A WSL host with neither marker (e.g. very early WSL
// builds) is not detected; both primary markers ship in every current WSL2.
func IsWSL() bool {
	if _, ok := readProcFile("/proc/sys/fs/binfmt_misc/WSLInterop"); ok {
		return true
	}
	if ver, ok := readProcFile("/proc/version"); ok {
		return strings.Contains(ver, "microsoft")
	}
	return false
}

var wslWarnOnce sync.Once

// wslFixHint is the operator-facing remediation for an unreadable WSL lib dir.
func wslFixHint(dir string) string {
	return "mod-worker cannot read " + dir +
		": add mod-worker to the group owning it (usermod -aG <group> mod-worker) " +
		"or grant read access: setfacl -m u:mod-worker:rx " + dir
}

// WSLLibReadable reports whether dir (default /usr/lib/wsl/lib) is readable
// by this process, along with a fix hint when it is not. On the first call
// that finds the dir unreadable it logs one warning.
func WSLLibReadable(dir string) (bool, string) {
	if dir == "" {
		dir = defaultWSLLibDir
	}
	if _, err := listDir(dir); err != nil {
		if os.IsNotExist(err) {
			// Not a WSL host (or dir absent): nothing to fix.
			return true, ""
		}
		wslWarnOnce.Do(func() {
			slog.Warn("WSL NVIDIA user-mode lib dir not readable by worker",
				"dir", dir, "hint", wslFixHint(dir))
		})
		return false, wslFixHint(dir)
	}
	return true, ""
}

// WSLLibReadableDefault is a convenience wrapper using the standard dir.
func WSLLibReadableDefault() (bool, string) {
	return WSLLibReadable(defaultWSLLibDir)
}

// wslGpuLibBind returns the read-only bind that exposes the WSL NVIDIA
// user-mode lib dir to a GPU container. When the dir is present but unreadable
// by the worker (non-root install on a WSL host) it logs ONE clear warning
// naming the fix and returns "" — the GPU task keeps running on the host's
// other GPU bridge mounts (Level Zero / librocdxg), it never fails because of
// this. On non-WSL hosts the dir is absent and the result is "" with no log.
func wslGpuLibBind() string {
	const bind = "/usr/lib/wsl/lib:/usr/lib/wsl/lib:ro"
	ok, _ := WSLLibReadable("")
	if ok {
		return bind
	}
	return ""
}
