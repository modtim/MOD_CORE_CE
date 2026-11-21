// GY.C2b — FIPS 140-3 crypto-mode reporting on the heartbeat.
//
// REPORT-ONLY: nothing in this package switches providers. Go 1.24+
// (this module is 1.25) ships crypto/fips140; which provider the binary
// actually runs on is a build-time / GODEBUG concern — the worker
// Dockerfile's GOFIPS140 build arg, or GODEBUG=fips140=on|only in the
// environment. This file just observes that fact so the manager's
// crypto-status view (CORE/API/services/crypto_status.py) can show each
// worker's reported mode instead of "unknown".
package heartbeat

import (
	"crypto/fips140"
	"crypto/md5"
	"runtime"
)

// fips140Mode reports the FIPS 140-3 mode this process actually runs
// under: "on" (the FIPS module is the default implementation), "only"
// (additionally non-approved algorithms are refused), or "off" (stock
// Go crypto, the default).
//
// There is no stdlib API to read the GODEBUG fips140 setting back
// directly, so this probes behaviour: fips140.Enabled() is true for
// BOTH on and only, and only "only" panics when a non-approved
// algorithm is digested (MD5's constructor is legal in every mode, the
// digest operation is what panics — "use of MD5 is not allowed in FIPS
// 140-only mode"). Probing behaviour also catches a build-time-baked
// GODEBUG (the Dockerfile's GOFIPS140 arg), which never appears in the
// process environment.
func fips140Mode() string {
	if !fips140.Enabled() {
		return "off"
	}
	refused := false
	func() {
		defer func() {
			if recover() != nil {
				refused = true
			}
		}()
		md5.Sum([]byte("mode probe"))
	}()
	if refused {
		return "only"
	}
	return "on"
}

// cryptoReport is the heartbeat's `crypto` block. All four keys are
// ALWAYS present on a worker that carries this code — the presence of
// the block is the "this binary reports its crypto mode" signal; an
// older binary omits the block entirely, and the manager keeps reporting
// "unknown" for it (never reads the absence as "off").
func cryptoReport() map[string]interface{} {
	return map[string]interface{}{
		"go_version":      runtime.Version(),
		"fips140_mode":    fips140Mode(),
		"fips140_enabled": fips140.Enabled(),
		"boringcrypto":    boringcryptoBuild(),
	}
}
