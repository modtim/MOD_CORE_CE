//go:build darwin

// MAC.1.s12 — darwin (macOS) build of native-task confinement. Thin wrapper:
// every DECISION lives in native_confine_policy.go (untagged, so the linux
// test runner can exercise the darwin policy); this file only applies it with
// darwin's syscalls.
//
// What MOD does on macOS, stated honestly:
//  1. tenant-supplied code is REFUSED (nativeTenantCodeGate) unless the
//     worker is the tenant's own TENANT_OWNED/BYO box — the existing
//     worker.native_allow_unsandboxed_tenant_code platform setting, no new
//     trust model;
//  2. trusted curated recipes (TenantID == 0) run as host processes, as a
//     dedicated less-privileged user when the operator configured one
//     (worker.native_darwin_uid) and this worker is root, else as the
//     worker's own user;
//  3. sandbox-exec is deliberately NOT used — Apple deprecated it (off by
//     default in current system-configuration policy, its deny-all profile
//     breaks the GUI apps these recipes drive, and it needs a shell wrapper
//     native recipes do not have). We do not claim it as a substitute.
//
// What darwin does NOT have and never claims: Pdeathsig (the field does not
// exist in darwin's SysProcAttr, so a task tree can outlive the worker) and
// the CAP_CHOWN gate (/proc/self/status is Linux-only). Because of that the
// posture reported for this OS is nativeConfinementPosture("darwin") ==
// "unsupported"; the uid the process runs as is reported separately.
package executor

import (
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
)

// confineNativeProcess on darwin sets ONLY the Credential uid drop. No Pdeathsig
// (absent from darwin's SysProcAttr) — that missing guarantee is exactly why
// nativeConfinementPosture reports "unsupported" here.
func confineNativeProcess(cmd *exec.Cmd, dropUID uint32) {
	postureOnce.Do(func() {
		slog.Default().Info("native-confinement-report",
			"os", "darwin",
			"layer", nativeConfinementPosture("darwin"),
			"uid", int64(dropUID),
			"note", "no Pdeathsig on darwin; sandbox-exec deprecated by Apple and unused; "+
				"tenant code refused unless this worker is the tenant's own TENANT_OWNED/BYO box")
	})
	if dropUID == 0 || dropUID == uint32(os.Getuid()) {
		return
	}
	// A non-root macOS worker cannot setuid; the recipe already runs as the
	// worker's own user (nativeDropUIDForOS returns 0 here, so this is a
	// belt-and-braces guard, not a policy).
	if os.Getuid() != 0 {
		return
	}
	if cmd.SysProcAttr == nil {
		// configureNativeProcess already allocated Setpgid, which applies on
		// every OS; only fill the attrs if a caller skipped it.
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Credential = &syscall.Credential{Uid: dropUID, Gid: dropUID}
}

// chownNativeTree on darwin chowns the scratch tree to the dropped uid. Go's
// darwin exec path applies Credential via setuid/setgid, so the task process
// genuinely runs as that uid and must own its scratch to write outputs. There
// is no CAP_CHOWN gate to consult: root can chown, and a non-root worker never
// gets a non-zero dropUID.
func chownNativeTree(root string, uid uint32) error {
	if uid == 0 {
		return nil
	}
	// Dirs and regular files only — a socket/fifo in someone's tree is never
	// touched, matching native_confine_linux.go's walk.
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() || info.Mode().IsRegular() {
			return os.Chown(path, int(uid), int(uid))
		}
		return nil
	})
}

// hasCapChown on darwin is a constant gate: there is no capability machinery,
// and the only confinement step here (Credential) needs root, which os.Getuid
// already reports. True keeps native_backend.go's cross-platform readers
// compiling without capability plumbing.
var hasCapChown func() bool = func() bool { return true }

// postureOnce guards the one audit-trail line per process that states, honestly,
// what this OS's native backend does and does not do. The machine-readable twin
// is the heartbeat's `native_confinement` block (internal/heartbeat/
// native_confinement.go), whose layer value for darwin is "unsupported".
var postureOnce sync.Once

var noCapWarnOnce sync.Once

// logNoCapOnce on darwin is a no-op: the gate above never fails.
func logNoCapOnce() {}
