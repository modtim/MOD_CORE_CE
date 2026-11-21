//go:build linux

// Command host-exec-helper: linux transport (992b/hx.9w). This is exactly
// the pre-992b listen()/peerUID() bodies from main.go, moved here unchanged.
// peerUID uses SO_PEERCRED, which golang.org/x/sys/unix only implements for
// linux (not darwin, which has no SO_PEERCRED and needs LOCAL_PEERCRED /
// Xucred instead — see platform_darwin.go — nor windows, which has no unix
// sockets at all — see platform_windows.go).
package main

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
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

// peerUID reads the connecting process's uid off the unix socket via
// SO_PEERCRED.
func peerUID(conn net.Conn) (uint32, error) {
	uc, ok := conn.(*net.UnixConn)
	if !ok {
		return 0, errors.New("not a unix socket connection")
	}
	raw, err := uc.SyscallConn()
	if err != nil {
		return 0, err
	}
	var cred *unix.Ucred
	var sockErr error
	ctlErr := raw.Control(func(fd uintptr) {
		cred, sockErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	})
	if ctlErr != nil {
		return 0, ctlErr
	}
	if sockErr != nil {
		return 0, sockErr
	}
	return cred.Uid, nil
}
