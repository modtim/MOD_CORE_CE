//go:build linux

package heartbeat

// FX.123.s2 — the full-disk-encryption heartbeat contract, pinned against
// captured probe output so the whole tri-state is testable without a real
// LUKS / BitLocker / FileVault host.
//
// The bug this pins: the API (FX.123.s3) carries a worker's previous
// disk_encryption reading forward when the field is absent, so a worker that
// GUESSES poisons that carry-forward forever — a WSL worker answering
// "unencrypted" from its own loop-image block device would mark a
// BitLocker-locked Windows host as unprotected. Every test below asserts the
// tri-state: a verdict only when the captured probe output proves it,
// "unknown" otherwise.
import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"errors"
)

// luksFixture is a real lsblk -J shape: the LUKS container (type=crypt) is
// the PARENT of the ext4 filesystem that is actually mounted, so the crypto
// marker is in the mount's ancestry, not on the mount node itself.
func luksFixture() string {
 return `{
  "blockdevices": [
   {"name":"sda","type":"disk","fstype":null,"mountpoints":[null],"children":[
     {"name":"sda3","type":"part","fstype":"LUKS1","mountpoints":[null],"children":[
       {"name":"dm0","type":"crypto","fstype":"ext4","mountpoints":["/"],"children":[]}
     ]}
   ]}
  ]
}`
}

// plainFixture is the same tree with no crypto anywhere: a real "unencrypted"
// answer, not a probe artefact.
func plainFixture() string {
 return `{
  "blockdevices": [
   {"name":"sda","type":"disk","fstype":null,"mountpoints":[null],"children":[
     {"name":"sda1","type":"part","fstype":"ext4","mountpoints":["/"],"children":[]}
   ]}
  ]
}`
}

// withDiskRunner swaps the probe seam for a fake replaying out (the same
// pattern capabilities' devices_unified_test.go uses for nvidiaSMIRunner),
// and restores the real runner after the test.
func withDiskRunner(t *testing.T, out string, err error) {
 t.Helper()
 old := diskEncRunner
 diskEncRunner = func([]string) (string, error) { return out, err }
 // The probe tests are about the probe, not about the host they happen to
 // run on: CI may itself be a WSL kernel, whose /proc/version would
 // short-circuit every verdict to unknown. Pin a native kernel so the
 // fixture decides the answer; the WSL tests override this below.
 oldVer := wslVersionReader
 wslVersionReader = func() (string, error) { return "Linux 6.6.40-generic #1 SMP", nil }
 t.Cleanup(func() {
  diskEncRunner = old
  wslVersionReader = oldVer
 })
}

// withWSLVersion pins /proc/version so the WSL short-circuit can be driven
// without owning a WSL kernel.
func withWSLVersion(t *testing.T, version string) {
 t.Helper()
 old := wslVersionReader
 wslVersionReader = func() (string, error) { return version, nil }
 t.Cleanup(func() { wslVersionReader = old })
}

// forceReprobe clears the ten-minute cache for one test: the verdict is
// asserted per probe, not per process.
func forceReprobe(t *testing.T) {
 t.Helper()
 old := diskEncReprobeEvery
 diskEncReprobeEvery = time.Duration(0)
 t.Cleanup(func() { diskEncReprobeEvery = old })
}

// diskStatus runs this build's probe and returns the wire status/method pair.
func diskStatus() (string, string) {
 v := diskEncProbeOS()
 return v.Status, v.Method
}

// TestLSBLKLUKSIsEncrypted pins the positive: a crypto node in the mount's
// ancestry is encryption, and the method must be named.
func TestLSBLKLUKSIsEncrypted(t *testing.T) {
 withDiskRunner(t, luksFixture(), nil)
 forceReprobe(t)
 status, method := diskStatus()
 if status != "encrypted" || method != "luks" {
  t.Fatalf("expected encrypted/luks, got %s/%s", status, method)
 }
}

// TestLSBLKPlainIsUnencrypted pins the honest negative: a listed mount with
// no crypto ancestor really is plain, and Linux names no foreign method.
func TestLSBLKPlainIsUnencrypted(t *testing.T) {
 withDiskRunner(t, plainFixture(), nil)
 forceReprobe(t)
 status, method := diskStatus()
 if status != "unencrypted" || method != "" {
  t.Fatalf("expected unencrypted with no method, got %s/%s", status, method)
 }
}

// TestLSBLKGarbageIsUnknown pins the artefact case: unparseable output is
// "could not tell", never the "unencrypted" the API would act on.
func TestLSBLKGarbageIsUnknown(t *testing.T) {
 withDiskRunner(t, "lsblk: unknown column MOUNTPOINTS", nil)
 forceReprobe(t)
 if status, _ := diskStatus(); status != "unknown" {
  t.Fatalf("garbage probe must be unknown, got %s", status)
 }
}

// TestLSBLKRunnerErrorIsUnknown pins the probe-failure case: a missing
// binary (a container without lsblk) must not read as "unencrypted".
func TestLSBLKRunnerErrorIsUnknown(t *testing.T) {
 withDiskRunner(t, "", errors.New("exec: lsblk: not found"))
 forceReprobe(t)
 if status, _ := diskStatus(); status != "unknown" {
  t.Fatalf("runner error must be unknown, got %s", status)
 }
}

// TestWSLIsUnknownEvenWhenLUKSShaped pins the WSL rule: the Linux view is a
// kernel hosted by Windows, so even a LUKS-shaped listing must not be
// reported — the Windows host's BitLocker state is invisible here.
func TestWSLIsUnknownEvenWhenLUKSShaped(t *testing.T) {
 // Order matters: withDiskRunner pins a NATIVE kernel (so the fixture, not
 // the CI host, decides), and withWSLVersion overrides that pin afterwards.
 withDiskRunner(t, luksFixture(), nil)
 withWSLVersion(t, "Linux 5.15.146.1-microsoft-standard-WSL2 #1 SMP")
 forceReprobe(t)
 if status, method := diskStatus(); status != "unknown" || method != "" {
  t.Fatalf("WSL must be unknown/empty, got %s/%s", status, method)
 }
}

// TestWSLNativeKernelStillProbes pins the other half of that rule: a plain
// (non-microsoft) /proc/version is a real Linux box and must still be probed,
// so the WSL short-circuit cannot silently disable detection everywhere.
func TestWSLNativeKernelStillProbes(t *testing.T) {
 withWSLVersion(t, "Linux 6.6.40-generic #1 SMP")
 withDiskRunner(t, plainFixture(), nil)
 forceReprobe(t)
 if status, _ := diskStatus(); status != "unencrypted" {
  t.Fatalf("native linux must report unencrypted, got %s", status)
 }
}

// TestSend_DiskEncryptionAlwaysPresent pins the wire contract the API's
// carry-forward depends on: a binary that carries this code ALWAYS sends the
// block, even when the probe cannot tell — so "unknown" is distinguishable
// from "older binary", and the manager never mistakes a broken probe for an
// absent field it would silently keep stale.
func TestSend_DiskEncryptionAlwaysPresent(t *testing.T) {
 srv, got := captureHeartbeatBody(t)
 defer srv.Close()

 h := newSendTestHeartbeat(srv)
 withDiskRunner(t, "", errors.New("exec: lsblk: not found"))
 forceReprobe(t)

 h.send(context.Background())

 v, ok := (*got)["disk_encryption"]
 if !ok {
  t.Fatalf("disk_encryption must always be present, got keys %#v", *got)
 }
 b, err := json.Marshal(v)
 if err != nil {
  t.Fatalf("marshal captured value: %v", err)
 }
 if string(b) != `{"method":"","status":"unknown"}` {
  t.Fatalf("a failed probe must serialise as unknown with no method, got %s", b)
 }
}

// TestSend_DiskEncryptionCarriesMethod pins the positive on the wire: an
// encrypted verdict must name its method, since the API gates compliance
// work on the method, not just the status.
func TestSend_DiskEncryptionCarriesMethod(t *testing.T) {
 srv, got := captureHeartbeatBody(t)
 defer srv.Close()

 h := newSendTestHeartbeat(srv)
 withDiskRunner(t, luksFixture(), nil)
 forceReprobe(t)

 h.send(context.Background())

 v, ok := (*got)["disk_encryption"]
 if !ok {
  t.Fatalf("disk_encryption must be present, got keys %#v", *got)
 }
 b, err := json.Marshal(v)
 if err != nil {
  t.Fatalf("marshal captured value: %v", err)
 }
 if string(b) != `{"method":"luks","status":"encrypted"}` {
  t.Fatalf("encrypted verdict must name its method, got %s", b)
 }
}

// bitlockerOnFixture is manage-bde -status C: on a locked volume.
func bitlockerOnFixture() string {
 return `
Device = C:
Size = 931 GB (979,969,535,744 bytes)
Used BitSpace Size = 141 GB (151,622,017,024 bytes)
Available BitSpace Size = 789 GB (847,347,518,208 bytes)
Encrypted by User:
Status: Encrypted
Encrypted: Yes
Auto-Unlock: Disabled
Algorithm: AES-CBC with HMAC-SHA256
Conversion Date: 2026-09-30T12:00:00
Locked: Yes
Recovery Type: Text Numeric
Sector 0: Not Encrypted
Encryption Key (Differential):
Encryption Key (Diffuser):
Protection On:
`
}

// bitlockerOffFixture is the same probe on a plain volume.
func bitlockerOffFixture() string {
 return `
Device = C:
Size = 931 GB (979,969,535,744 bytes)
Used BitSpace Size = 0 bytes
Available BitSpace Size = 931 GB (999,999,999,999 bytes)
Status: Not Encrypted
Encrypted: No
Auto-Unlock: Disabled
Algorithm: None
Conversion Date: 2026-09-30T12:00:00
Locked: No
Recovery Type: Text Numeric
Sector 0: Not Encrypted
Protection Off:
`
}

// TestBitlockerOnIsEncrypted pins the Windows positive, including the
// PowerShell Get-BitLockerVolume spelling of the same fact.
func TestBitlockerOnIsEncrypted(t *testing.T) {
 v := parseBitlocker(bitlockerOnFixture(), "C:")
 if v.Status != "encrypted" || v.Method != "bitlocker" {
  t.Fatalf("expected encrypted/bitlocker, got %s/%s", v.Status, v.Method)
 }
 v = parseBitlocker("Volume : C:\n ProtectionStatus : On\n", "C:")
 if v.Status != "encrypted" || v.Method != "bitlocker" {
  t.Fatalf("Get-BitLockerVolume shape expected encrypted/bitlocker, got %s/%s", v.Status, v.Method)
 }
}

// TestBitlockerOffIsUnencrypted pins the Windows honest negative.
func TestBitlockerOffIsUnencrypted(t *testing.T) {
 v := parseBitlocker(bitlockerOffFixture(), "C:")
 if v.Status != "unencrypted" || v.Method != "" {
  t.Fatalf("expected unencrypted with no method, got %s/%s", v.Status, v.Method)
 }
}

// TestBitlockerGarbageIsUnknown pins the artefact cases: a refused probe
// (non-admin), a suspended volume, and a listing of some other volume are
// all "could not tell", never "unencrypted".
func TestBitlockerGarbageIsUnknown(t *testing.T) {
 garbage := []string{
  "Access denied. Manage-BDE requires administrator privileges.",
  "Device = C:\n Status: Suspended\n Protection Suspended:\n",
  "Device = D:\n Size = 1 TB\n Protection On:\n",
 }
 for _, g := range garbage {
  v := parseBitlocker(g, "C:")
  if v.Status != "unknown" || v.Method != "" {
   t.Fatalf("bitlocker artefact %q must be unknown, got %s/%s", g, v.Status, v.Method)
  }
 }
}

// TestFileVaultOnIsEncrypted pins the macOS positive.
func TestFileVaultOnIsEncrypted(t *testing.T) {
 v := parseFileVault("FileVault is On.")
 if v.Status != "encrypted" || v.Method != "filevault" {
  t.Fatalf("expected encrypted/filevault, got %s/%s", v.Status, v.Method)
 }
}

// TestFileVaultOffIsUnencrypted pins the macOS honest negative.
func TestFileVaultOffIsUnencrypted(t *testing.T) {
 v := parseFileVault("FileVault is Off.")
 if v.Status != "unencrypted" || v.Method != "" {
  t.Fatalf("expected unencrypted with no method, got %s/%s", v.Status, v.Method)
 }
}

// TestFileVaultGarbageIsUnknown pins the artefact cases: fdesetup's
// locked-to-another-user answer and a permission error are not verdicts.
func TestFileVaultGarbageIsUnknown(t *testing.T) {
 garbage := []string{
  "FileVault is On, but locked to another user.",
  "sudo: fdesetup may only be run by root",
  "",
 }
 for _, g := range garbage {
  v := parseFileVault(g)
  if v.Status != "unknown" || v.Method != "" {
   t.Fatalf("filevault artefact %q must be unknown, got %s/%s", g, v.Status, v.Method)
  }
 }
}
