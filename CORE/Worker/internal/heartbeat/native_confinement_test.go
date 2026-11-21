package heartbeat

// MAC.1.s12 — the honesty of the heartbeat's native-confinement report.
//
// The report is computed from the OS NAME so the darwin verdict is asserted on
// a linux runner: a macOS native worker must never be reported as confined.

import (
	"runtime"
	"testing"
)

func TestNativeConfinementLayerDarwinUnsupported(t *testing.T) {
	if got := nativeConfinementLayer("darwin"); got != nativeConfinementUnsupported {
		t.Fatalf("darwin must report %q, got %q", nativeConfinementUnsupported, got)
	}
}

func TestNativeConfinementLayerLinuxNamesLayer(t *testing.T) {
	if got := nativeConfinementLayer("linux"); got != "pdeathsig" {
		t.Fatalf("linux must name its layer, got %q", got)
	}
}

func TestNativeConfinementLayerFailsClosed(t *testing.T) {
	for _, osName := range unknownOSNames() {
		if got := nativeConfinementLayer(osName); got != nativeConfinementUnsupported {
			t.Fatalf("os %q must report %q, got %q", osName, nativeConfinementUnsupported, got)
		}
	}
}

func TestNativeConfinementReportNeverClaimsConfinement(t *testing.T) {
	// Whatever OS this binary runs on, the layer value is never a made-up
	// "confinement" claim: only linux names a layer, everything else is
	// "unsupported".
	report := nativeConfinementReport()
	layer, ok := report["layer"].(string)
	if !ok || layer == "" {
		t.Fatalf("layer must be a non-empty string, got %v", report["layer"])
	}
	if report["os"] != runtime.GOOS {
		t.Fatalf("os must be the running OS (%s), got %v", runtime.GOOS, report["os"])
	}
	if runtime.GOOS != "linux" && layer != nativeConfinementUnsupported {
		t.Fatalf("a non-linux worker must report %q, got %q", nativeConfinementUnsupported, layer)
	}
}

// unknownOSNames are the OSes that are not a confinement layer MOD verified.
func unknownOSNames() []string { return []string{"windows", "darwin", "plan9", ""} }
