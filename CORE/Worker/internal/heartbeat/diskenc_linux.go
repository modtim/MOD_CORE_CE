//go:build linux

// FX.123.s2 — the Linux half of full-disk-encryption detection: the probe
// command and its target. All the decision logic is in diskenc_parse.go so
// the same captured-output fixtures pin the contract in CI.
package heartbeat

import (
	"os"
	"strings"
)

// lsblkArgs is the probe. MOUNTPOINTS (plural) is the util-linux 2.34+
// column; older builds only know MOUNTPOINT, and a probe that fails because
// of the column name yields an error, i.e. "unknown" — never a wrong
// verdict.
var lsblkArgs = []string{"lsblk", "-J", "-o", "NAME,TYPE,FSTYPE,MOUNTPOINTS"}

// diskEncTargetLinux is the path whose filesystem matters: the worker's own
// data dir when it exists (that is the volume its jobs write to), else the
// root filesystem.
func diskEncTargetLinux() string {
 const root = "/var/lib/mod-worker"
 if _, err := os.Stat(root); err == nil {
  return root
 }
 return "/"
}

// wslVersionReader is the mockable seam behind the WSL check — the same
// testability pattern as capabilities' nvidiaSMIRunner, so a test can pin a
// captured /proc/version without owning a WSL kernel.
var wslVersionReader = func() (string, error) {
 ver, err := os.ReadFile("/proc/version")
 if err != nil {
  return "", err
 }
 return string(ver), nil
}

// wslHost reports true under WSL, where the Linux view is a kernel hosted by
// Windows: the real boot volume is the Windows host's, whose BitLocker state
// is invisible here (the WSL block device is a loop/file image). Probing
// would answer "unencrypted" for a BitLocker-locked host, so the answer is
// unknown and the native Windows worker carries the truth.
func wslHost() bool {
 ver, err := wslVersionReader()
 if err != nil {
  return false
 }
 return strings.Contains(strings.ToLower(ver), "microsoft")
}

// diskEncProbeOS is this build's probe.
func diskEncProbeOS() DiskEncryption {
 if wslHost() {
  return diskUnknown()
 }
 out, err := diskEncRunner(lsblkArgs)
 if err != nil {
  return diskUnknown()
 }
 return parseLSBLKJSON(out, diskEncTargetLinux())
}
