package executor

import "strings"

// workerAPIPrefix restates transport.apiPrefix (package-local there): the API
// is single-mounted under /v1, there is no unprefixed backward-compat route.
const workerAPIPrefix = "/v1"

// workerAPIBase returns the executor's API base with the /v1 mount prefix
// appended exactly once (idempotent when cfg.APIURL already ends in /v1).
// e.apiURL is the BARE cfg.APIURL; every worker->API call the executor builds
// by hand (output-scan-check, checkpoint-export-uploaded) must go through
// this, the same way transport.NewAPITransport and hostexec_task.go do.
// Returns "" when there is no API URL.
func workerAPIBase(apiURL string) string {
	base := strings.TrimRight(strings.TrimSpace(apiURL), "/")
	if base == "" {
		return ""
	}
	if !strings.HasSuffix(base, workerAPIPrefix) {
		base += workerAPIPrefix
	}
	return base
}
