//go:build !windows

// Package hostexec: unix (linux/macOS) transport for the relay client
// (992b/hx.9w). go-winio's named-pipe API (pipe.go) only compiles under
// GOOS=windows, so the dial/exists implementations can't live in client.go
// behind a runtime.GOOS branch — Go still has to type-check the windows
// branch for every GOOS, and winio.DialPipe simply doesn't exist here. The
// split mirrors the existing runner.go/runner_other.go and
// sandbox.go/sandbox_other.go pattern in this package.
package hostexec

import (
	"context"
	"io/fs"
	"net"
	"os"
)

// platformIsRoot reports whether this process runs as uid 0 (993a). Used on
// both linux and macOS (this file's build tag is !windows): runner_darwin.go
// documents that a macOS worker installed as a root launchd daemon has the
// same root-equivalent privilege model as a linux systemd worker, so a
// non-root macOS worker must claim "not_root" exactly like linux does. This
// deliberately does NOT parse /proc/self/status — procfs doesn't exist on
// macOS, so that approach silently reported "unreadable ⇒ root" there,
// letting a non-root macOS worker falsely claim available/direct.
// os.Geteuid() is a plain libc getuid(2)/getegid-family syscall on both
// platforms, no file I/O involved.
func platformIsRoot() bool {
	return os.Geteuid() == 0
}

// platformExists reports whether path is an existing unix socket.
func platformExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().Type() == fs.ModeSocket
}

// platformDial connects to path as a unix socket.
func platformDial(_ context.Context, path string) (net.Conn, error) {
	return net.DialTimeout("unix", path, dialTimeout)
}
