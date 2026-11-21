// Package sockproxy is a policy-filtering proxy for the Docker daemon
// API (owner decision Q20). Containers that need a Docker socket get
// this proxy's unix socket bind-mounted instead of the raw
// /var/run/docker.sock, so tenant-originated docker calls are limited
// to a read-mostly allowlist while GPU passthrough keeps working.
//
// The proxy is inert by default: worker tunable
// worker.docker_proxy_mode = "off" (the default) means no proxy is
// started and the raw socket is mounted exactly as before. "report"
// forwards every request but logs what the policy would have denied;
// "enforce" answers denied requests with a 403.
package sockproxy

import (
	"net/http"
	"strings"
)

// Mode selects the proxy's enforcement behaviour.
type Mode string

const (
	// ModeOff means the executor never starts the proxy: the raw
	// socket is mounted, exactly like pre-Q20.
	ModeOff Mode = "off"
	// ModeReport forwards everything; requests the policy would deny
	// are logged (one structured line, never the body) but still
	// reach the daemon.
	ModeReport Mode = "report"
	// ModeEnforce answers denied requests with a 403 and a JSON
	// {"message": reason} body.
	ModeEnforce Mode = "enforce"
)

// defaultCapAllowlist is the set of capabilities a container-create
// request may add. The worker itself runs node containers with
// CapDrop=ALL, so anything broader (SYS_ADMIN, NET_ADMIN, SYS_PTRACE,
// BPF, ...) is never required by a MOD task — and is exactly the
// escape a hostile tenant node would reach for.
var defaultCapAllowlist = []string{"NET_BIND_SERVICE", "SYS_NICE", "IPC_LOCK"}

// defaultDeviceAllowlist is the set of host device paths that may be
// passed into a container (GPU + WSL-GPU passthrough). A source matches
// an entry when it equals the entry, sits below it, or is an entry
// followed by a digit (e.g. /dev/nvidia0 against /dev/nvidia).
var defaultDeviceAllowlist = []string{"/dev/dri", "/dev/kfd", "/dev/dxg", "/dev/nvidia"}

// Policy configures Decide. The zero value is not usable — use
// DefaultPolicy().
type Policy struct {
	// AllowHostNetwork permits NetworkMode "host" on container-create
	// when true (the stack-mode allowlist flag from the Q20 spec).
	AllowHostNetwork bool
	// CapAllowlist overrides defaultCapAllowlist when non-empty.
	CapAllowlist []string
	// DeviceAllowlist overrides defaultDeviceAllowlist when non-empty.
	DeviceAllowlist []string
}

// DefaultPolicy returns the Q20 baseline policy.
func DefaultPolicy() *Policy {
	return &Policy{
		CapAllowlist:    defaultCapAllowlist,
		DeviceAllowlist: defaultDeviceAllowlist,
	}
}

// splitPath separates a Docker API request URI into the unversioned
// API path and the raw query string. Docker clients may pin a /vX.YY
// prefix (/v1.44/containers/json); the daemon accepts bare paths too,
// so both must be decided identically.
func splitPath(uri string) (apiPath, rawQuery string) {
	if i := strings.IndexByte(uri, '?'); i >= 0 {
		rawQuery = uri[i+1:]
		uri = uri[:i]
	}
	if parts := strings.SplitN(uri, "/", 3); len(parts) == 3 && isVersion(parts[1]) {
		uri = "/" + parts[2]
	}
	return uri, rawQuery
}

func isVersion(seg string) bool {
	if len(seg) < 3 || seg[0] != 'v' {
		return false
	}
	dot := false
	for i := 1; i < len(seg); i++ {
		switch c := seg[i]; {
		case c >= '0' && c <= '9':
		case c == '.' && !dot && i > 1:
			dot = true
		default:
			return false
		}
	}
	return true
}

// wantsBody reports whether the policy needs the raw request body to
// decide this request (container-create, exec-create). The proxy uses
// this to decide whether to buffer a body at all: stream-style
// endpoints (POST /build, logs, events) forward their bodies untouched.
func wantsBody(method, apiPath string) bool {
	if method != http.MethodPost {
		return false
	}
	if apiPath == "/containers/create" || apiPath == "/volumes/create" {
		// /volumes/create carries the DriverOpts.device host path that
		// makes a "local" volume a host bind (DIAG C3) - without the
		// body the check would be blind.
		return true
	}
	parts := strings.Split(strings.Trim(apiPath, "/"), "/")
	return len(parts) == 3 && parts[0] == "containers" && parts[2] == "exec"
}
