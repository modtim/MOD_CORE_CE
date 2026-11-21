//go:build darwin

package executor

// MAC.1.s12 — the darwin build-tagged twin of native_confine_cap_test.go.
//
// This file only compiles/runs on a macOS runner, so it exercises the DARWIN
// WRAPPER (native_confine_darwin.go) with real darwin syscalls: the Credential
// uid drop Go's darwin exec path actually applies, the scratch chown walk, and
// the tenant gate as the running darwin worker sees it (nativeOSName() ==
// "darwin" here, so the macOS refusal wording is asserted, not just the
// sentinel). The OS-neutral DECISIONS are covered on any runner by
// native_confine_policy_test.go.
//
// Everything here is uid-relative, never "assume root": a real macOS test
// runner is an unprivileged user, and the wrapper's honest answer to a
// non-root worker is "no drop" (you cannot setuid what you cannot become).

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func darwinSpecFor(tenantID int) *TaskSpec {
	return &TaskSpec{
		TenantID:      tenantID,
		NativeCommand: "true",
	}
}

// --- the uid drop Go's darwin exec path applies -----------------------------

func TestDarwinConfineNoDropWhenNoUIDConfigured(t *testing.T) {
	// dropUID 0 = "run as the worker": no Credential, no setuid attempt.
	cmd := exec.Command("true")
	configureNativeProcess(cmd)
	confineNativeProcess(cmd, 0)
	if cmd.SysProcAttr != nil && cmd.SysProcAttr.Credential != nil {
		t.Fatalf("dropUID 0 must not set a Credential, got %+v", *cmd.SysProcAttr.Credential)
	}
}

func TestDarwinConfineNoDropToOwnUID(t *testing.T) {
	// Dropping to the uid we already are is a no-op: Go's darwin Credential
	// path would setuid+setgid needlessly, and the wrapper skips it.
	cmd := exec.Command("true")
	configureNativeProcess(cmd)
	confineNativeProcess(cmd, uint32(os.Getuid()))
	if cmd.SysProcAttr != nil && cmd.SysProcAttr.Credential != nil {
		t.Fatalf("dropUID == getuid must not set a Credential, got %+v", *cmd.SysProcAttr.Credential)
	}
}

func TestDarwinConfineRefusesSetuidWhenNotRoot(t *testing.T) {
	// A non-root macOS worker cannot setuid: the wrapper must leave the
	// process as the worker's own user, never half-apply a Credential.
	if os.Getuid() != 0 {
		cmd := exec.Command("true")
		configureNativeProcess(cmd)
		confineNativeProcess(cmd, 61001)
		if cmd.SysProcAttr != nil && cmd.SysProcAttr.Credential != nil {
			t.Fatalf("non-root darwin worker must not set a Credential, got %+v", *cmd.SysProcAttr.Credential)
		}
	}
}

func TestDarwinConfineAppliesCredentialWhenRoot(t *testing.T) {
	// Only a root darwin worker gets the dedicated-user drop, and it is the
	// Credential field (setuid/setgid) — NOT Pdeathsig, which does not exist
	// in darwin's SysProcAttr (this file would not compile if we set one).
	if os.Getuid() == 0 {
		cmd := exec.Command("true")
		configureNativeProcess(cmd)
		// nativeDropUIDForOS only hands a non-zero uid to a root worker, so
		// the wrapper is fed the value native_backend.go would feed it.
		confineNativeProcess(cmd, 61001)
		cred := syscall.Credential{}
		if cmd.SysProcAttr != nil && cmd.SysProcAttr.Credential != nil {
			cred = *cmd.SysProcAttr.Credential
		}
		if cred.Uid != 61001 || cred.Gid != 61001 {
			t.Fatalf("root darwin worker must set Credential uid/gid 61001, got %+v", cred)
		}
	}
}

// --- the scratch chown walk -------------------------------------------------

func TestDarwinChownTreeNoOpWhenNoDrop(t *testing.T) {
	// uid 0 = "no dedicated user": the walk must not touch anything, so even
	// a nonexistent root is not an error here.
	if err := chownNativeTree("/definitely-not-a-mod-scratch", 0); err != nil {
		t.Fatalf("chownNativeTree(root, 0) must be a no-op, got %v", err)
	}
}

func TestDarwinChownTreeWalksRealScratch(t *testing.T) {
	// A real tree, chowned to the worker's own uid: permitted for an
	// unprivileged runner and a no-op for root, so it runs on any macOS user.
	root, err := os.MkdirTemp("", "mod-darwin-scratch")
	if err != nil {
		t.Fatalf("scratch tree setup failed, got %v", err)
	}
	t.Cleanup(func() { _rmTree(t, root) })
	if err := os.MkdirAll(filepath.Join(root, "in"), 0o755); err != nil {
		t.Fatalf("scratch subdir setup failed, got %v", err)
	}
	f, err := os.Create(filepath.Join(root, "in", "out.bin"))
	if err != nil {
		t.Fatalf("scratch file setup failed, got %v", err)
	}
	_ = f
	if err := chownNativeTree(root, uint32(os.Getuid())); err != nil {
		t.Fatalf("chownNativeTree must succeed for the worker's own uid, got %v", err)
	}
	// A missing scratch root IS an error (the walk reports it), so the
	// success above was a real traversal, not a silent skip. (Skipped for a
	// root runner, where uid 0 means "no drop" and the walk never runs.)
	if os.Getuid() != 0 {
		if err := chownNativeTree(filepath.Join(root, "missing-subtree"), uint32(os.Getuid())); err == nil {
			t.Fatalf("chownNativeTree must report a missing scratch root, got nil")
		}
	}
}

// --- the tenant gate as the running darwin worker sees it -------------------

func TestDarwinTenantGateRefusesOnThisWorker(t *testing.T) {
	// nativeOSName() == "darwin" on this runner, so native_backend.go's
	// tenantCodeGate takes the darwin spelling: the sentinel AND the macOS
	// wording (the policy test only sees the wording via an argument).
	err := nativeTenantCodeGate(nativeOSName(), darwinSpecFor(7), nil)
	if !errors.Is(err, ErrNativeTenantRefused) {
		t.Fatalf("darwin native worker must refuse tenant code, got %v", err)
	}
	msg := err.Error()
	if !strings.Contains(msg, "macOS") || !strings.Contains(msg, "TENANT_OWNED") {
		t.Fatalf("darwin refusal must name the macOS/TENANT_OWNED reason, got %q", msg)
	}
}

func TestDarwinTenantGateAllowsCuratedRecipe(t *testing.T) {
	// Trusted curated recipes (TenantID == 0) are exactly what a macOS native
	// worker exists to run.
	if err := nativeTenantCodeGate(nativeOSName(), darwinSpecFor(0), nil); err != nil {
		t.Fatalf("platform task must run on darwin, got %v", err)
	}
}

func TestDarwinPostureIsUnsupportedOnThisWorker(t *testing.T) {
	// The heartbeat's honesty claim, asserted against the RUNNING OS rather
	// than an argument: a macOS binary must never report a layer.
	if got := nativeConfinementPosture(nativeOSName()); got != darwinConfinementUnsupported {
		t.Fatalf("darwin posture must report %q, got %q", darwinConfinementUnsupported, got)
	}
}

// _rmTree deletes a scratch tree best-effort (test cleanup only).
func _rmTree(t *testing.T, root string) {
	if err := removeTree(root); err != nil {
		t.Logf("cleanup of %s failed: %v", root, err)
	}
}

// removeTree deletes a small scratch tree deepest-first (test cleanup only).
// filepath.Walk is top-down, which would rmdir a non-empty parent and fail
// ENOTEMPTY, so this recurses instead.
func removeTree(root string) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	for _, ent := range entries {
		p := filepath.Join(root, ent.Name())
		if ent.IsDir() {
			if err := removeTree(p); err != nil {
				return err
			}
		} else if err := os.Remove(p); err != nil {
			return err
		}
	}
	// os.Remove deletes an empty directory too (there is no os.Rmdir in Go).
	return os.Remove(root)
}
