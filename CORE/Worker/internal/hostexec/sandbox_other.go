//go:build !linux

// Package hostexec: non-linux sandbox stub. The linux sandbox (sandbox.go) is
// built on systemd-run + systemd-owned PID 1, neither of which exists on darwin
// / windows worker builds, so those GOOSes get this file instead: the same
// exported surface, every entry point failing closed. cmd/worker wires
// hostexec.SandboxArgv / hostexec.SandboxOptions unconditionally (hostexec_wire
// .go), so both must exist here for the cross-compile to link at all; they
// deliberately offer no sandbox, only the refusal the caller already handles.
package hostexec

import (
	"fmt"
	"runtime"
)

// SandboxOptions describes a sandboxed host exec. Kept in sync with sandbox.go's
// linux definition field-for-field: cmd/worker builds one literal (hostExecSandbox
// Opts) and passes it to SandboxArgv, so the shape must match or the non-linux
// build stops compiling. There is no RunAs field on either side: DynamicUser=yes
// always runs the command as a transient unprivileged user on linux, and nothing
// here honours a request at all.
type SandboxOptions struct {
	Workspace string
	// Inaccessible: worker scratch / credential paths kept unreadable.
	// TimeoutS: RuntimeMaxSec; <=0 means no cap. Env: --setenv pairs.
	Inaccessible []string
	TimeoutS     int
	Env          map[string]string
}

// SandboxAvailable fails closed on non-linux hosts: there is no systemd-run and
// no systemd-owned PID 1, so sandboxed host mode is unavailable and callers MUST
// refuse rather than run unsandboxed.
func SandboxAvailable() error {
	return fmt.Errorf("host-exec sandbox unsupported on %s", runtime.GOOS)
}

// SandboxArgv builds the full systemd-run argv for a sandboxed host exec on linux.
// Here it never builds anything: it returns nil + the same refusal as SandboxAvailable
// so a caller can never fall back to an unsandboxed exec. The arguments are unused
// on purpose - there is nothing to validate, only to refuse.
func SandboxArgv(_ SandboxOptions, _ []string) ([]string, error) {
	return nil, SandboxAvailable()
}
