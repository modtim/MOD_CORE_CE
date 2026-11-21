package sockproxy

import (
	"log/slog"
	"os"
	"strings"
	"sync"
)

// DockerSocketDefault is the conventional Linux daemon path. It doubles
// as the mount point inside a task container: whatever daemon socket the
// worker discovers on its host is always bind-mounted here, so the docker
// CLI inside the container needs no --socket-url flag.
const DockerSocketDefault = "/var/run/docker.sock"

// dockerSocketCandidates lists the daemon-socket paths to try, in
// priority order, for a given DOCKER_HOST and HOME. Docker Desktop on
// macOS keeps the socket under the user's home (~/.docker/run/docker.sock;
// the /var/run/docker.sock symlink only exists when the "allow default
// socket" option is enabled), Apple's container runtime and OrbStack/Colima
// each have their own path. DOCKER_HOST is the Docker CLI's own standard
// variable, not a MOD tunable, so reading it here is allowed.
func dockerSocketCandidates(dockerHost, home string) []string {
	var out []string
	// unix:// is the only form that names a socket; tcp://, ssh:// and
	// systemd:// hosts are not paths and are skipped.
	rest := strings.TrimPrefix(dockerHost, "unix://")
	if dockerHost != "" && rest != dockerHost && rest != "" {
		out = append(out, rest)
	}
	out = append(out, DockerSocketDefault)
	if home != "" {
		out = append(out,
			home + "/.docker/run/docker.sock",
			home + "/.orbstack/run/docker.sock",
			home + "/.colima/default/docker.sock")
	}
	return out
}

// isSocket reports whether path exists and is a unix socket (a plain
// file or directory left at one of the candidate paths must not win).
func isSocket(path string) bool {
	st, err := os.Stat(path)
	if err != nil {
		// Missing, unreadable, or a dangling symlink: not usable.
		return false
	}
	return st.Mode()&os.ModeSocket == os.ModeSocket
}

// resolveDockerSocket returns the first candidate that exists and is a
// unix socket, or "" when none does.
func resolveDockerSocket(dockerHost, home string) string {
	for _, p := range dockerSocketCandidates(dockerHost, home) {
		if isSocket(p) {
			return p
		}
	}
	return ""
}

// ResolveDockerSocket reads the two standard environment variables and
// resolves the daemon socket for this box.
func ResolveDockerSocket() string {
	return resolveDockerSocket(os.Getenv("DOCKER_HOST"), os.Getenv("HOME"))
}

var (
	daemonMu sync.Mutex
	// daemonPath/daemonKnown memoize the resolution: the daemon socket
	// does not move during a process lifetime, and the choice is logged
	// exactly once (at first resolution, i.e. at startup).
	daemonPath  string
	daemonKnown bool
)

// DockerSocket returns the worker host's docker daemon socket, resolved
// and logged once per process. "" means this box has no usable daemon
// socket; callers decide whether to mount nothing or fall back.
//
// Only a successful resolution is memoized: a Docker Desktop/OrbStack/
// Colima daemon that hasn't finished starting yet at worker boot must not
// strand the worker for its whole lifetime, so a "not found" result is
// retried (and re-logged) on every call instead of being cached forever.
func DockerSocket(log *slog.Logger) string {
	daemonMu.Lock()
	defer daemonMu.Unlock()
	if daemonKnown {
		return daemonPath
	}
	p := ResolveDockerSocket()
	if p != "" {
		daemonKnown = true
		daemonPath = p
		if log != nil {
			log.Info("docker daemon socket resolved", "socket", daemonPath)
		}
		return daemonPath
	}
	if log != nil {
		log.Warn("no docker daemon socket found on this box (DOCKER_HOST, /var/run/docker.sock, ~/.docker/run/docker.sock, ~/.orbstack/run/docker.sock, ~/.colima/default/docker.sock all absent or not sockets) — docker-dependent tasks run without one; will retry")
	}
	return ""
}
