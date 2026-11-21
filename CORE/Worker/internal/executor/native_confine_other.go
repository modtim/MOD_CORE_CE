//go:build !linux && !darwin

package executor

import (
	"os/exec"
	"sync"
)

// GSEC FX.17 / R-069 + Q21.9 — non-Linux no-op counterpart of
// native_confine_linux.go. Pdeathsig/Credential confinement and the CAP_CHOWN
// capability gate are Linux-specific; process-group isolation (Setpgid)
// already applies on every OS via configureNativeProcess.

func confineNativeProcess(_ *exec.Cmd, _ uint32) {}

func chownNativeTree(_ string, _ uint32) error { return nil }

// hasCapChown is a no-op gate on non-Linux: the uid-drop logic only runs on
// Linux, so this always reports "allowed" to keep native_backend.go compiling
// cross-platform without any capability plumbing here.
var hasCapChown func() bool = func() bool { return true }

var noCapWarnOnce sync.Once

// logNoCapOnce is a no-op on non-Linux (the gate never fails there).
func logNoCapOnce() {}
