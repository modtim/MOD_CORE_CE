//go:build darwin

// Command host-exec-helper: darwin transport (992b/hx.9w). darwin has unix
// sockets, so listen() is the same portable body as platform_linux.go (no
// linux-specific syscalls in it). peerUID has no equivalent here: darwin's
// SO_PEERCRED analog is LOCAL_PEERCRED / unix.GetsockoptXucred, which
// golang.org/x/sys/unix does expose, but wiring it up is out of scope for
// this transport-only brief (exec on darwin is tracked separately) — a
// stub that returns an error is the same non-fatal contract
// platform_windows.go uses: handleConn already logs-and-continues on a
// peerUID error rather than treating it as a failure.
package main

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
)

// listen creates the socket directory with the requested group and mode
// 0750, then binds a unix socket there at mode 0660.
func listen(cfg config) (net.Listener, error) {
	dir := filepath.Dir(cfg.socketPath)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("mkdir %s: %w", dir, err)
	}
	if gid, err := groupGID(cfg.group); err == nil {
		_ = os.Chown(dir, -1, gid)
	}
	if err := os.Chmod(dir, 0o750); err != nil {
		return nil, err
	}
	_ = os.Remove(cfg.socketPath)
	l, err := net.Listen("unix", cfg.socketPath)
	if err != nil {
		return nil, fmt.Errorf("listen %s: %w", cfg.socketPath, err)
	}
	if gid, err := groupGID(cfg.group); err == nil {
		_ = os.Chown(cfg.socketPath, -1, gid)
	}
	if err := os.Chmod(cfg.socketPath, 0o660); err != nil {
		l.Close()
		return nil, err
	}
	return l, nil
}

// peerUID has no SO_PEERCRED equivalent wired up on darwin in this brief's
// scope (992b is transport-only). handleConn already treats this error as a
// non-fatal "unavailable" log line, matching platform_windows.go.
func peerUID(_ net.Conn) (uint32, error) {
	return 0, errors.New("peer credentials are not implemented on darwin")
}
