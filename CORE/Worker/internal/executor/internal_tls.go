// GSEC FX.13 (R-059) — enforcement gate for the node-container API_URL
// override, plus forwarding the platform CA bundle into node containers.
//
// See the API_URL override block in executor.go (runContainerWithCredsInternal)
// for the call sites. Everything here is inert by default: tunable absent
// (or "off") ⇒ same injected URL as today; MOD_INTERNAL_CA_BUNDLE unset ⇒
// no extra env entry, no extra bind.
package executor

import (
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tlsconf"
)

// internalTLSModeTunableKey is the GS.PSR.7 heartbeat tunable carrying the
// platform setting gsec.transport.internal_tls_enforce (off | report |
// require) to the worker.
const internalTLSModeTunableKey = "gsec.transport.internal_tls_enforce"

// decideNodeAPIURL is the pure decision for the API_URL the worker injects
// into node containers. mode is the gsec.transport.internal_tls_enforce
// tunable value; an error (returned only for "require" + plain http) means
// the caller must fail the task BEFORE creating the container. "report"
// and "off"/absent pass the URL through unchanged.
func decideNodeAPIURL(apiURL, mode string) (string, error) {
	if mode == "require" && strings.HasPrefix(strings.TrimSpace(apiURL), "http://") {
		return "", fmt.Errorf(
			"%s=require: refusing to give node containers a plain-http API_URL (%s); serve the API over TLS (internal CA bundle via %s) before re-enabling require",
			internalTLSModeTunableKey, apiURL, tlsconf.EnvVar)
	}
	return apiURL, nil
}

// reportPlainAPIURLOnce logs at most once per worker process that a plain
// http API_URL is being injected into node containers (mode == "report").
var reportPlainAPIURLOnce sync.Once

// reportPlainAPIURL logs once when mode is "report" and the URL being
// injected is plain http; no-op in every other case.
func reportPlainAPIURL(log *slog.Logger, mode, apiURL string) {
	if mode != "report" || !strings.HasPrefix(strings.TrimSpace(apiURL), "http://") {
		return
	}
	reportPlainAPIURLOnce.Do(func() {
		log.Warn("R-059 report: node containers are receiving a plain-http API_URL",
			"api_url", apiURL, "tunable", internalTLSModeTunableKey)
	})
}

// internalCABundleMount returns the container path and Docker bind spec for
// forwarding the platform CA bundle into node containers read-only, or
// ("", "") when MOD_INTERNAL_CA_BUNDLE is unset or names no file on this
// host — in which case BOTH the bind and the env entry are skipped (a
// missing source path would make the docker daemon auto-create an empty
// host directory).
func internalCABundleMount() (containerPath, bind string) {
	path := strings.TrimSpace(os.Getenv(tlsconf.EnvVar))
	if path == "" {
		return "", ""
	}
	if _, err := os.Stat(path); err != nil {
		return "", ""
	}
	return tlsconf.BundleContainerPath,
		path + ":" + tlsconf.BundleContainerPath + ":ro"
}
