//go:build windows

// Command host-exec-helper: Windows named-pipe transport (992b/hx.9w).
// Windows has no unix-socket semantics equivalent to the directory-mode +
// socket-mode trust boundary listen() (platform_unix.go) relies on, so the
// pipe itself carries an explicit Windows security descriptor instead.
package main

import (
	"errors"
	"net"

	winio "github.com/Microsoft/go-winio"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/hostexec"
)

// pipeSDDL is the named pipe's security descriptor in SDDL form:
//   - "D:"      a DACL follows (access is whitelist-only; anything not
//     explicitly allowed is implicitly denied).
//   - "P"       protected: the DACL does not inherit ACEs from the pipe
//     filesystem namespace's default (which otherwise grants
//     Everyone/Authenticated Users local-system-wide connect access to new
//     named pipes — that default is exactly the "root-only socket" trust
//     boundary violation this brief exists to close).
//   - "(A;;GA;;;SY)" one allow ACE: Generic All, to SY (the well-known
//     SYSTEM SID). No other trustee (not Administrators, not
//     Authenticated Users, not the interactive user) gets an ACE, so no
//     other local account can open or dial the pipe.
//
// This matches the unix transport's trust boundary (helper socket dir mode
// 0750 + socket mode 0660, owned by a dedicated group) for the case where
// the worker service also runs as SYSTEM. If a future install runs the
// worker under its own Windows service SID instead, add a second
// "(A;;GA;;;<worker-sid>)" ACE here rather than widening this one.
const pipeSDDL = "D:P(A;;GA;;;SY)"

// listen binds the Windows named pipe at hostexec.DefaultPipe, ACL'd to
// SYSTEM only via pipeSDDL. cfg.socketPath / cfg.group (the unix transport's
// directory-mode trust boundary) are unix-only concepts and unused here.
func listen(_ config) (net.Listener, error) {
	return winio.ListenPipe(hostexec.DefaultPipe, &winio.PipeConfig{
		SecurityDescriptor: pipeSDDL,
		MessageMode:        false,
	})
}

// peerUID has no Windows named-pipe equivalent to unix SO_PEERCRED in this
// brief's scope (992b is transport-only; actual host-exec execution on
// Windows is tracked separately, per the brief). handleConn already treats
// this error as a non-fatal "unavailable" log line.
func peerUID(_ net.Conn) (uint32, error) {
	return 0, errors.New("peer credentials are not available on a Windows named pipe")
}
