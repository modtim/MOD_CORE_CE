//go:build darwin

// FX.123.s2 — the macOS half of full-disk-encryption detection: the probe
// command and its target. All the decision logic is in diskenc_parse.go so
// the same captured-output fixtures pin the contract in CI.
package heartbeat

// fdesetupArgs asks FileVault for its state. fdesetup answers on stdout for
// every status form, so no stderr merging is needed.
var diskEncDarwinArgs = []string{"fdesetup", "status"}

// diskEncProbeOS is this build's probe.
func diskEncProbeOS() DiskEncryption {
 out, err := diskEncRunner(diskEncDarwinArgs)
 if err != nil {
  return diskUnknown()
 }
 return parseFileVault(out)
}
