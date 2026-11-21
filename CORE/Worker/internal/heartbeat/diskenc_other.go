//go:build !linux && !windows && !darwin

// FX.123.s2 — the fallback half of full-disk-encryption detection. No probe
// exists for any other platform, and the contract forbids guessing, so the
// answer is always unknown and the manager carries whatever it already has.
package heartbeat

// diskEncProbeOS is this build's probe: no opinion.
func diskEncProbeOS() DiskEncryption { return diskUnknown() }
