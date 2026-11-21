//go:build windows

// FX.123.s2 — the Windows half of full-disk-encryption detection: the probe
// command and its target. All the decision logic is in diskenc_parse.go so
// the same captured-output fixtures pin the contract in CI.
package heartbeat

// manageBDEArgs asks BitLocker for the state of C:, the boot volume every
// worker on this box writes through. manage-bde needs no special privilege
// for -status; when it is refused (non-admin on some configurations) the
// child errors and the verdict is unknown, never "unencrypted".
var diskEncWindowsArgs = []string{"manage-bde", "-status", "C:"}

// diskEncProbeOS is this build's probe.
func diskEncProbeOS() DiskEncryption {
 out, err := diskEncRunner(diskEncWindowsArgs)
 if err != nil {
  return diskUnknown()
 }
 return parseBitlocker(out, "C:")
}
