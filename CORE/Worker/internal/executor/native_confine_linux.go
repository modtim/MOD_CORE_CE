//go:build linux

package executor

import (
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
)

// GSEC FX.17 / R-069 + Q21.9 — cheap confinement for native processes that
// the tenant gate allowed to run. Applied when operator policy set a
// non-zero drop uid. On Linux the worker may run as root OR as the
// non-root mod-worker user (docker group, ambient CAP_CHOWN only). Either
// way we:
//   - only set Credential (drop uid) when we are root AND can chown — a
//     non-root worker cannot setuid, so its tasks just run as itself;
//   - chown the scratch tree only when we can (root or CAP_CHOWN);
//     otherwise we warn once and let the task run as the worker user
//     instead of failing.

// hasCapChown reports whether this process may chown files: root, or the
// CAP_CHOWN bit set in the effective capability mask. Injectable for tests;
// defaults to reading /proc/self/status.
var hasCapChown func() bool = defaultHasCapChown

func defaultHasCapChown() bool {
	if os.Getuid() == 0 {
		return true
	}
	data, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "CapEff:") {
			continue
		}
		var mask uint64
		if _, err := fmt.Sscanf(line, "CapEff: %x", &mask); err != nil {
			return false
		}
		return mask&(1<<0) != 0 // bit 0 = CAP_CHOWN
	}
	return false
}

var noCapWarnOnce sync.Once

// logNoCapOnce warns a single time per process that the worker lacks the
// capability for a confinement step. Not fatal: the task runs as the
// worker's own user.
func logNoCapOnce() {
	noCapWarnOnce.Do(func() {
		slog.Warn("mod-worker lacks root/CAP_CHOWN; skipping native uid confinement step",
			"note", "task runs as the worker's own user")
	})
}

// confineNativeProcess sets Pdeathsig (worker death kills the task tree)
// and, when dropUID > 0 and we are root with CAP_CHOWN, drops the process
// to that uid/gid. A non-root worker never sets Credential (no
// CAP_SETUID/SETGID) and runs the task as itself.
func confineNativeProcess(cmd *exec.Cmd, dropUID uint32) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Pdeathsig = syscall.SIGKILL
	if dropUID > 0 && dropUID != uint32(os.Getuid()) {
		if os.Getuid() == 0 && hasCapChown() {
			cmd.SysProcAttr.Credential = &syscall.Credential{Uid: dropUID, Gid: dropUID}
		} else {
			logNoCapOnce()
		}
	}
}

// chownNativeTree walks root (dirs + files) and chowns everything to uid.
// Requires root or CAP_CHOWN; without it we warn once and no-op so the
// task can still run as the worker's own user.
func chownNativeTree(root string, uid uint32) error {
	if !hasCapChown() {
		logNoCapOnce()
		return nil
	}
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return os.Chown(path, int(uid), int(uid))
		}
		if info.Mode().IsRegular() {
			return os.Chown(path, int(uid), int(uid))
		}
		return nil
	})
}
