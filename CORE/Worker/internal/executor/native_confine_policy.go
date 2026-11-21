// MAC.1.s12 — the OS-neutral half of native-task confinement POLICY.
//
// Every DECISION here takes the OS name as an argument and is pure, so the
// linux test runner can exercise the darwin policy without a Mac. The
// build-tagged files are thin wrappers that apply the decision:
//
//	native_confine_linux.go   (linux:  Pdeathsig + Credential uid drop)
//	native_confine_darwin.go  (darwin: Credential uid drop, NO Pdeathsig)
//	native_confine_other.go   (everything else: no-op)
//
// Why darwin got its own file instead of the old `!linux` no-op: macOS native
// workers are exactly the boxes MOD runs curated recipes on, and the no-op
// silently claimed "no confinement layer here" while the tenant gate still
// refused tenant code. The darwin build now states what MOD actually does:
// refuse tenant-supplied code, run trusted curated recipes as a dedicated
// less-privileged user when the operator configured one, else as the worker's
// own user — and report the posture honestly.
//
// sandbox-exec is deliberately NOT used: Apple deprecated it (disabled by
// default in current system-configuration policy, its deny-all profile breaks
// the GUI apps these recipes drive, and it needs a shell wrapper native
// recipes do not have). Nothing here pretends a darwin native task is
// container-equivalent: it is an unconfined host process, and the report says
// so.
package executor

import (
	"fmt"
	"runtime"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// darwinDropUIDKey names the dedicated less-privileged uid a darwin native
// worker runs its curated-recipe processes as. 0 (the default) = "no
// dedicated user configured", so the recipe runs as the worker's own user.
// Registry-backed like every other tunable (GS.PSR.7) — never an env var.
const darwinDropUIDKey = "worker.native_darwin_uid"

// darwinConfinementUnsupported is the honesty token for an OS whose native
// backend has no real confinement layer (no parent-death guarantee, no
// capability gate, no sandbox). The manager must read it as "I run host
// processes unconfined", never as "confined".
const darwinConfinementUnsupported = "unsupported"

// nativeConfinementPosture names the confinement layer the native backend can
// ACTUALLY apply on an OS, for the heartbeat's `native_confinement` key and
// the per-task posture line. Fail-closed: only linux, where Pdeathsig is
// unconditionally set, names a layer; darwin (Credential uid drop only — no
// parent-death guarantee, sandbox-exec deprecated and unused) reports
// "unsupported", as does every other OS.
func nativeConfinementPosture(osName string) string {
	if osName == "linux" {
		return "pdeathsig"
	}
	return darwinConfinementUnsupported
}

// darwinDropUID is the uid a darwin native process runs as: the dedicated
// less-privileged uid the operator configured, but only when this worker is
// root (a non-root macOS worker cannot setuid and is already unprivileged —
// the same "run as the worker's own user" fallback the linux path uses).
func darwinDropUID(configuredUID, workerUID uint32) uint32 {
	if configuredUID == 0 || workerUID != 0 {
		return 0
	}
	return configuredUID
}

// nativeTenantCodeGate is the single GSEC FX.17 / R-069 tenant gate every
// native backend shares: tenant-supplied code (TenantID > 0) is refused
// outright unless the worker is the tenant's own TENANT_OWNED/BYO box, which
// is exactly what worker.native_allow_unsandboxed_tenant_code means in the
// platform-settings registry ("set True only on single-tenant / trusted
// on-prem workers that cannot run containers"). Platform/system tasks
// (TenantID == 0 — the curated recipes) pass. No new trust model is invented
// here: the same tunable, the same sentinel, the same fail-closed defaults.
//
// The darwin spelling wraps ErrNativeTenantRefused (errors.Is still matches,
// so dispatchNativeTask keeps failing the task without pointless retries) and
// names the macOS reason, because "enable container backend" is not the only
// answer on a Mac.
func nativeTenantCodeGate(osName string, spec *TaskSpec, t *tunables.Store) error {
	if spec == nil || spec.TenantID == 0 {
		return nil
	}
	allowed := false
	if t != nil {
		allowed = t.Bool(nativeAllowUnsandboxedTenantCodeKey, "", false)
	}
	if allowed {
		return nil
	}
	if osName == "darwin" {
		return fmt.Errorf(
			"macOS native worker refuses tenant-supplied code: it would run as an unconfined host process "+
				"(no parent-death guarantee, no capability gate; sandbox-exec is deprecated by Apple and unused). "+
				"Run it on the container backend, or allow it only on this tenant's own TENANT_OWNED/BYO "+
				"worker via the platform setting native_allow_unsandboxed_tenant_code: %w",
			ErrNativeTenantRefused,
		)
	}
	return ErrNativeTenantRefused
}

// nativeDropUIDForOS is the uid the native backend runs its task process as
// on an OS (0 = the worker's own user). Linux keeps its existing
// worker.native_drop_uid reading; darwin uses its dedicated-user key; every
// other OS has no uid concept here.
func nativeDropUIDForOS(osName string, t *tunables.Store, workerUID uint32) uint32 {
	if workerUID != 0 || t == nil {
		return 0
	}
	switch osName {
	case "linux":
		return uint32(t.Int(nativeDropUIDKey, 0))
	case "darwin":
		return darwinDropUID(uint32(t.Int(darwinDropUIDKey, 0)), workerUID)
	default:
		return 0
	}
}

// nativeOSName is the running OS, the single place the untagged native_backend
// .go consults so the pure policy above stays testable on any platform.
func nativeOSName() string { return runtime.GOOS }
