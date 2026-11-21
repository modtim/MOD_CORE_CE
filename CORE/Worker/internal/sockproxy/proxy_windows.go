//go:build windows

package sockproxy

import "errors"

// windows-only half of the proxy (see the note in proxy.go): ensurePrivateDir
// and Server.Start are the unix pair in proxy_unix.go; this file exists so the
// Windows cross-build of ./cmd/worker (CORE/Worker/Dockerfile's
// CGO_ENABLED=0 GOOS=windows GOARCH=amd64 step, and the matching Makefile
// target) still links. The unix-only bits they use (syscall.Stat_t for the
// owner check, syscall.Umask around the listen syscall) have no Windows
// equivalent at all: unix domain sockets are not a thing here, so the socket
// proxy is not offered on Windows hosts.
//
// The entry point is Server.Start, and it fails closed: dockerSocketBind
// treats a Start error as "mount no docker socket at all" rather than
// falling back to the raw daemon socket, so an operator who armed the proxy
// on a Windows box gets no socket (fail closed) instead of raw-socket
// escalation behind their back. Nothing here ever touches the filesystem.

// ensurePrivateDir is the Windows stub: there is no unix socket to host, so
// no private directory is prepared. Kept for signature parity with proxy_unix.go.
func ensurePrivateDir(dir string) error {
	_ = dir
	return nil
}

// Start is the Windows stub: the socket proxy is unix-only, so it reports a
// clear error instead of listening. Callers (executor/dockproxy.go) already
// fail closed on a Start error: no docker socket is mounted for the task
// container.
func (s *Server) Start() error {
	_ = s
	return errors.New("sockproxy: the docker socket proxy is unix-only; not available on Windows hosts")
}
