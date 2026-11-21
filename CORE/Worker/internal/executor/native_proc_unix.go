//go:build !windows

package executor

import (
	"errors"
	"fmt"
	"os/exec"
	"syscall"
	"time"
)

// nativeHandle tracks a running native process. The process leads its own
// process group, so killing -PID reaches every child it spawned.
type nativeHandle struct {
	PID int
}

func configureNativeProcess(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func attachNativeProcess(cmd *exec.Cmd) (*nativeHandle, error) {
	return &nativeHandle{PID: cmd.Process.Pid}, nil
}

// releaseNativeHandle reaps anything the task left running in its group
// once the leader has exited, matching container teardown semantics.
func releaseNativeHandle(h *nativeHandle) {
	if h != nil && h.PID > 0 {
		_ = syscall.Kill(-h.PID, syscall.SIGKILL)
	}
}

// killProcessGroup sends SIGTERM to the whole group, then SIGKILL after a
// short grace period if anything in the group is still alive.
func killProcessGroup(h *nativeHandle) error {
	if h == nil || h.PID <= 0 {
		return fmt.Errorf("invalid native handle")
	}
	pgid := -h.PID
	if err := syscall.Kill(pgid, syscall.SIGTERM); err != nil {
		if errors.Is(err, syscall.ESRCH) {
			return nil
		}
		return fmt.Errorf("signalling process group: %w", err)
	}
	deadline := time.Now().Add(nativeKillGrace)
	for time.Now().Before(deadline) {
		if err := syscall.Kill(pgid, 0); errors.Is(err, syscall.ESRCH) {
			return nil
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err := syscall.Kill(pgid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return fmt.Errorf("killing process group: %w", err)
	}
	return nil
}
