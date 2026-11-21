// MAC.1.s12 — the heartbeat's honest native-confinement posture.
//
// The executor's native backend has a per-OS confinement layer (see
// internal/executor/native_confine_policy.go + the build-tagged twins
// native_confine_linux.go / native_confine_darwin.go / native_confine_other.go).
// This file reports WHICH layer this binary actually has, so the manager can
// never read a native worker as "confined" when its OS provides no such layer.
//
// It is computed here rather than imported from internal/executor because
// heartbeat must not import executor (executor already imports transport; the
// wiring would be a cycle) — the same reason crypto_report.go and diskenc.go
// sit in this package. The token values are the executor's: "pdeathsig" for a
// layer that exists, "unsupported" for one that does not.
//
// darwin is the case this exists for: macOS native workers run curated recipes
// as host processes. Go's darwin exec path DOES apply SysProcAttr.Credential
// (setuid/setgid), so the uid the recipe runs as is reported — but darwin has
// no Pdeathsig (a task tree can outlive the worker), no CAP_CHOWN gate, and
// sandbox-exec is deprecated by Apple and deliberately unused. So the LAYER is
// reported "unsupported": the worker never claims confinement it does not have.
package heartbeat

import "runtime"

// nativeConfinementUnsupported mirrors internal/executor/native_confine_policy.go's
// darwinConfinementUnsupported. Kept as a literal here so a linux test runner
// can assert the darwin value without importing executor.
const nativeConfinementUnsupported = "unsupported"

// nativeConfinementLayer names the confinement layer the native backend has on
// an OS. Fail-closed: only linux (Pdeathsig is unconditionally set there) names
// a layer; darwin and everything else report "unsupported".
func nativeConfinementLayer(osName string) string {
	if osName == "linux" {
		return "pdeathsig"
	}
	return nativeConfinementUnsupported
}

// nativeConfinementReport is the heartbeat's `native_confinement` block:
// {"os": <worker OS>, "layer": "pdeathsig"|"unsupported"}.
//
// Report-only and additive: an older binary omits the block and the manager
// reads "unknown", never "confined". The layer value is the ONLY field the
// manager may gate on — a dedicated less-privileged user (worker.native_darwin_uid
// on darwin, worker.native_drop_uid on linux) is reported separately by the
// executor's own posture log line, because "runs as another uid" is not the
// same claim as "confined" (no parent-death guarantee, no sandbox).
func nativeConfinementReport() map[string]interface{} {
	return map[string]interface{}{
		"os":    runtime.GOOS,
		"layer": nativeConfinementLayer(runtime.GOOS),
	}
}
