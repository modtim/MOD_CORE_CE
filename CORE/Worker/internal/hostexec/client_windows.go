//go:build windows

// Package hostexec: Windows named-pipe transport for the relay client
// (992b/hx.9w). Windows has no unix-socket semantics equivalent to
// /run/mod-host-exec/host-exec.sock, so the relay dials a named pipe
// instead; cmd/host-exec-helper's platform_windows.go ACLs that pipe to
// SYSTEM only via an SDDL SecurityDescriptor, matching the root-only-socket
// trust boundary the unix transport gets from directory/socket file modes.
package hostexec

import (
	"context"
	"net"
	"time"

	winio "github.com/Microsoft/go-winio"
	windows "golang.org/x/sys/windows"
)

// pipeExistsTimeout bounds the dial-and-close probe platformExists uses to
// decide whether a pipe is being served, mirroring the unix os.Stat check's
// near-instant result.
const pipeExistsTimeout = 250 * time.Millisecond

// platformIsRoot reports whether this Windows worker process holds an
// elevated (UAC "yes"-equivalent) access token — the Windows analogue of
// geteuid()==0 on linux/darwin (993g). LocalSystem service tokens report
// elevated, so a SYSTEM-installed worker still reports available/direct;
// a plain non-elevated user reports not_root instead, matching what
// runner_windows.go needs to actually run the DIRECT runner.
func platformIsRoot() bool {
	return windows.GetCurrentProcessToken().IsElevated()
}

// platformExists reports whether path is an existing, served named pipe. A
// named pipe has no stat-able filesystem presence the way a unix socket
// does, so existence is proven the only way go-winio exposes: dial it with a
// short timeout and close immediately on success.
func platformExists(path string) bool {
	timeout := pipeExistsTimeout
	conn, err := winio.DialPipe(path, &timeout)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// platformDial connects to path as a Windows named pipe. ctx is unused here,
// matching the unix transport's platformDial: the connect itself is bounded
// by dialTimeout, and Relay.Exec's own context.AfterFunc hook (client.go)
// closes the conn on ctx cancellation once dialing has succeeded.
func platformDial(_ context.Context, path string) (net.Conn, error) {
	timeout := dialTimeout
	return winio.DialPipe(path, &timeout)
}
