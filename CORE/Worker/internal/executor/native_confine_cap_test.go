//go:build linux

package executor

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
)

// GSEC Q21.9 — capability-gated uid confinement for a non-root mod-worker
// (docker group + ambient CAP_CHOWN only). The injectable hasCapChown lets
// these tests exercise both sides of the gate regardless of the test runner
// user.

func withCapChown(t *testing.T, v bool) {
	t.Helper()
	orig := hasCapChown
	hasCapChown = func() bool { return v }
	t.Cleanup(func() { hasCapChown = orig })
}

// defaultDropUID picks a uid distinct from the current one so the
// drop-branch is actually taken.
func defaultDropUID(t *testing.T) uint32 {
	t.Helper()
	uid := uint32(4242)
	if uid == uint32(os.Getuid()) {
		uid = 4243
	}
	if uid == uint32(os.Getuid()) {
		t.Skipf("cannot find a uid distinct from %d", os.Getuid())
	}
	return uid
}

func TestConfineNativeProcessSkipsCredentialWithoutCapChown(t *testing.T) {
	withCapChown(t, false)
	dropUID := defaultDropUID(t)

	cmd := exec.Command("true")
	confineNativeProcess(cmd, dropUID)

	if cmd.SysProcAttr == nil {
		t.Fatal("SysProcAttr not set")
	}
	if cmd.SysProcAttr.Credential != nil {
		t.Fatalf("Credential must not be set without root/CAP_CHOWN, got %+v", cmd.SysProcAttr.Credential)
	}
	if cmd.SysProcAttr.Pdeathsig != syscall.SIGKILL {
		t.Fatalf("Pdeathsig = %v, want SIGKILL", cmd.SysProcAttr.Pdeathsig)
	}
}

func TestConfineNativeProcessSetsCredentialWithCapChown(t *testing.T) {
	if os.Getuid() != 0 {
		t.Skip("setting Credential requires root (CAP_SETUID)")
	}
	withCapChown(t, true)
	dropUID := defaultDropUID(t)

	cmd := exec.Command("true")
	confineNativeProcess(cmd, dropUID)

	if cmd.SysProcAttr.Credential == nil {
		t.Fatal("Credential must be set for a root worker with CAP_CHOWN")
	}
	if cmd.SysProcAttr.Credential.Uid != dropUID || cmd.SysProcAttr.Credential.Gid != dropUID {
		t.Fatalf("Credential = %+v, want uid/gid %d", cmd.SysProcAttr.Credential, dropUID)
	}
}

func TestConfineNativeProcessNoDropIsUnchanged(t *testing.T) {
	withCapChown(t, false)
	cmd := exec.Command("true")
	confineNativeProcess(cmd, 0)
	if cmd.SysProcAttr.Credential != nil {
		t.Fatalf("no drop requested, Credential = %+v", cmd.SysProcAttr.Credential)
	}
}

func TestChownNativeTreeSkipsWithoutCapChown(t *testing.T) {
	withCapChown(t, false)
	dropUID := defaultDropUID(t)

	dir := t.TempDir()
	f := filepath.Join(dir, "in.txt")
	if err := os.WriteFile(f, []byte("x"), 0644); err != nil {
		t.Fatal(err)
	}
	// Must not walk+chown (would fail as non-root, and be pointless):
	// with the gate closed the call is a documented no-op.
	if err := chownNativeTree(dir, dropUID); err != nil {
		t.Fatalf("chownNativeTree with closed gate = %v, want nil", err)
	}
}

func TestChownNativeTreeWithCapChown(t *testing.T) {
	if os.Getuid() != 0 {
		t.Skip("chown requires root or CAP_CHOWN; running unprivileged")
	}
	withCapChown(t, true)
	dropUID := defaultDropUID(t)

	dir := t.TempDir()
	if err := chownNativeTree(dir, dropUID); err != nil {
		t.Fatalf("chownNativeTree = %v", err)
	}
	fi, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Sys().(*syscall.Stat_t).Uid != dropUID {
		t.Fatalf("dir uid = %d, want %d", fi.Sys().(*syscall.Stat_t).Uid, dropUID)
	}
}

func TestDefaultHasCapChown(t *testing.T) {
	got := defaultHasCapChown()
	if os.Getuid() == 0 && !got {
		t.Fatal("root must always report CAP_CHOWN")
	}
	if os.Getuid() != 0 && got {
		t.Log("running unprivileged with CAP_CHOWN (ambient); fine")
	}
}
