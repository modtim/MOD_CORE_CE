package executor

import (
	"os"
	"path/filepath"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/sockproxy"
)

// dockerProxyModeTunableKey is the platform_settings registry key for
// the Docker socket proxy mode (owner decision Q20: our own Go Docker
// socket proxy that inspects request bodies). Must match
// CORE/API/services/platform_settings.py's register() call and
// worker_management.py's _WORKER_TUNABLE_KEYS mapping.
const dockerProxyModeTunableKey = "worker.docker_proxy_mode"

// dockerSocketBind resolves the bind string a task container gets for
// its Docker socket:
//
//   - tunable off (default) -> the raw daemon socket, exactly as before,
//     wherever this box keeps it (Linux /var/run/docker.sock, macOS
//     ~/.docker/run/docker.sock, OrbStack/Colima paths — see
//     sockproxy.ResolveDockerSocket);
//   - report/enforce        -> the proxy socket, started once per
//     process and reused by every subsequent task;
//   - report/enforce but the proxy failed to start -> ok=false, no
//     bind at all. Failing closed (no socket) rather than silently
//     falling back to the raw one: an operator who explicitly armed
//     the proxy must not get raw-socket escalation behind their back.
//
// Whatever socket is mounted always lands on
// sockproxy.DockerSocketDefault inside the task container, so the
// docker CLI needs no --socket-url flag.
func (e *Executor) dockerSocketBind() (bind string, ok bool) {
	// The daemon socket is resolved (and logged once) per process; the
	// mount point stays the Linux default on every platform.
	daemon := sockproxy.DockerSocket(e.log)
	if daemon == "" {
		// No daemon socket on this box at all — same as off.
		return "", false
	}
	mode := sockproxy.Mode(sockproxy.ModeOff)
	if e.tunables != nil {
		mode = sockproxy.Mode(e.tunables.String(
			dockerProxyModeTunableKey, "",
			string(sockproxy.ModeOff),
			string(sockproxy.ModeOff), string(sockproxy.ModeReport), string(sockproxy.ModeEnforce)))
	}
	if mode == sockproxy.ModeOff {
		return daemon + ":" + sockproxy.DockerSocketDefault, true
	}
	e.dockProxyMu.Lock()
	defer e.dockProxyMu.Unlock()
	if e.dockProxy != nil {
		return e.dockProxy.ListenPath() + ":" + sockproxy.DockerSocketDefault, true
	}
	dir, err := os.MkdirTemp("", "mod-sockproxy-")
	if err != nil {
		e.log.Warn("docker socket proxy: temp dir failed", "err", err)
		return "", false
	}
	// The proxy's own listen socket must sit on a path the sockproxy
	// policy will not deny as a tenant bind; the temp dir is fine.
	// The policy's denied-prefix list still names the Linux default,
	// which is where every mount lands, so tenant binds stay blocked
	// regardless of where the host daemon socket lives.
	lp := filepath.Join(dir, "docker.sock")
	s := sockproxy.New(lp, daemon, mode, sockproxy.DefaultPolicy(),
		e.log.With("component", "docker_socket_proxy"))
	if err := s.Start(); err != nil {
		os.RemoveAll(dir)
		e.log.Error("docker socket proxy failed to start; mounting no docker socket",
			"mode", string(mode), "err", err)
		return "", false
	}
	e.dockProxy = s
	e.log.Info("docker socket proxy started", "mode", string(mode), "socket", lp)
	return lp + ":" + sockproxy.DockerSocketDefault, true
}
