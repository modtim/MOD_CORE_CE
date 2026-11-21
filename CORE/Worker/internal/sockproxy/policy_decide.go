package sockproxy

import (
	"net/url"
	"strings"
)

// Decide returns (allow, reason). reason is empty when allow is true.
// path is the full request URI (query included, version prefix
// optional). body carries the request body for the create/exec
// endpoints whose payload is inspected; it may be nil for
// stream-style endpoints (POST /build, attach, logs, events), for
// which only the query string is inspected.
//
// Everything not on the allowlist is denied: swarm, plugins, secrets,
// nodes, system prune, image deletion, and — until proven needed —
// PUT/GET /containers/{id}/archive (file copy in/out of a container,
// the main Docker-in-Docker file-theft primitive).
func (p *Policy) Decide(method, path string, body []byte) (bool, string) {
	apiPath, rawQuery := splitPath(path)
	switch method {
	case "GET":
		return p.decideGet(apiPath)
	case "POST":
		return p.decidePost(apiPath, rawQuery, body)
	}
	return false, "method " + method + " is not on the Docker API allowlist"
}

func (p *Policy) decideGet(apiPath string) (bool, string) {
	switch apiPath {
	case "/_ping", "/version", "/info", "/events",
		"/containers/json", "/images/json", "/networks", "/volumes":
		return true, ""
	}
	parts := strings.Split(strings.Trim(apiPath, "/"), "/")
	if len(parts) >= 2 {
		switch parts[0] {
		case "containers": // /containers/{id}/json, /containers/{id}/logs
			if len(parts) == 3 && (parts[2] == "json" || parts[2] == "logs") {
				return true, ""
			}
		case "images": // /images/{name...}/json (name may be library/foo)
			if parts[len(parts)-1] == "json" {
				return true, ""
			}
		case "networks", "volumes": // /networks/{id}, /volumes/{name}
			if len(parts) == 2 {
				return true, ""
			}
		}
	}
	return false, "GET " + apiPath + " is not on the Docker API allowlist"
}

func (p *Policy) decidePost(apiPath, rawQuery string, body []byte) (bool, string) {
	switch apiPath {
	case "/containers/create":
		return checkCreateHostConfig(body, p)
	case "/build":
		// build-task needs /build; the build context is a streamed
		// tar, never buffered — only the query params are inspected.
		if q, _ := url.ParseQuery(rawQuery); q.Get("networkmode") == "host" {
			return false, "build with networkmode=host"
		}
		return true, ""
	case "/images/create", "/networks/create":
		return true, ""
	case "/volumes/create":
		// A "local" volume's DriverOpts.device IS a host path: dockerd
		// mounts it, and a later containers/create Mounts volume entry
		// then puts the whole tree inside a container (DIAG C3). The
		// Binds spelling of the same trick is already closed by the
		// normalization gate, so the body must be vetted here.
		return checkVolumeCreate(body, p)
	}
	parts := strings.Split(strings.Trim(apiPath, "/"), "/")
	if len(parts) == 3 {
		switch parts[0] {
		case "containers":
			switch parts[2] {
			case "start", "stop", "kill", "wait", "remove", "attach":
				return true, ""
			case "exec": // exec create — inspect the body
				return checkExecCreate(body)
			}
		case "exec":
			if parts[2] == "start" {
				return true, ""
			}
		case "networks":
			if parts[2] == "connect" || parts[2] == "disconnect" {
				return true, ""
			}
		}
	}
	return false, "POST " + apiPath + " is not on the Docker API allowlist"
}
