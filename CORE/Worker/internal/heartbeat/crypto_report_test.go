// GY.C2b — unit tests for the heartbeat's crypto-mode block.
package heartbeat

import (
	"os"
	"strings"
	"testing"
)

// The four keys must all be present: the manager treats the ABSENCE of the
// block (older binary) as "cannot say", so a partially-populated block from
// this binary would be worse than nothing.
func TestCryptoReport_AllKeysPresent(t *testing.T) {
	rep := cryptoReport()
	for _, k := range []string{"go_version", "fips140_mode", "fips140_enabled", "boringcrypto"} {
		if _, ok := rep[k]; !ok {
			t.Fatalf("crypto report missing key %q: %v", k, rep)
		}
	}
	if rep["fips140_mode"] != fips140Mode() {
		t.Fatalf("fips140_mode %v disagrees with fips140Mode() %v",
			rep["fips140_mode"], fips140Mode())
	}
	wantEnabled := fips140Mode() != "off"
	if rep["fips140_enabled"] != wantEnabled {
		t.Fatalf("fips140_enabled %v inconsistent with mode %v",
			rep["fips140_enabled"], rep["fips140_mode"])
	}
}

// The expected mode depends on how the test binary was launched: a stock
// run is "off"; `GODEBUG=fips140=on go test ./internal/heartbeat/...` runs
// the binary in "on" mode and `GODEBUG=fips140=only go test ...` in "only"
// mode (GODEBUG is fixed at process start, so the whole binary runs in the
// mode the caller chose — the report must match it).
func expectedMode() string {
	v := os.Getenv("GODEBUG")
	switch {
	case strings.Contains(v, "fips140=only"):
		return "only"
	case strings.Contains(v, "fips140=on"):
		return "on"
	default:
		return "off"
	}
}

func TestCryptoReport_ModeMatchesGodebug(t *testing.T) {
	want := expectedMode()
	if got := fips140Mode(); got != want {
		t.Fatalf("fips140Mode() = %q, want %q (GODEBUG=%q)",
			got, want, os.Getenv("GODEBUG"))
	}
}

// This test binary is built without the boringcrypto tag, so the flag is
// false in every mode. (A boringcrypto-tagged build flips the source file,
// not the test.)
func TestCryptoReport_StockBuild(t *testing.T) {
	if got := cryptoReport()["boringcrypto"]; got != false {
		t.Fatalf("boringcrypto = %v, want false (untagged test build)", got)
	}
}
