package sockproxy

import (
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"os"
)

// bodyCapBytes caps how much of a request body the proxy reads to
// inspect. Create/exec payloads are small JSON documents; anything
// bigger is refused in enforce mode (it cannot be vetted) and flagged
// in report mode. Build contexts are never buffered (see wantsBody).
const bodyCapBytes = 10 << 20

// Server is a policy-filtering reverse proxy from a client unix
// socket to the Docker daemon's unix socket. It forwards streaming
// and hijacked connections (attach, exec -it, logs -f, events, build
// output) via httputil.ReverseProxy.
type Server struct {
	listenPath string
	upstream   string
	mode       Mode
	policy     *Policy
	log        *slog.Logger
	rproxy     *httputil.ReverseProxy
	http       *http.Server
	ln         net.Listener
}

// New builds a Server. listenPath is where the proxy socket will be
// created (parent dir must exist); upstream is the daemon socket to
// forward to. mode must be ModeReport or ModeEnforce — ModeOff means
// "do not build a Server at all" (the executor's job).
func New(listenPath, upstream string, mode Mode, policy *Policy, log *slog.Logger) *Server {
	var dialer net.Dialer
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return dialer.DialContext(ctx, "unix", upstream)
		},
		DisableCompression: true, // stream logs/build output verbatim
	}
	rp := &httputil.ReverseProxy{
		Transport: transport,
		Rewrite: func(rc *httputil.ProxyRequest) {
			// The unix DialContext below ignores network/addr, so the
			// host is a placeholder — but http.Transport rejects an
			// empty URL.Host.
			rc.Out.URL.Scheme = "http"
			rc.Out.URL.Host = "docker-local"
			rc.Out.Host = "docker-local"
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			log.Warn("docker proxy: upstream error", "err", err)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			b, _ := json.Marshal(map[string]string{"message": "docker proxy upstream error"})
			w.Write(b)
		},
	}
	return &Server{
		listenPath: listenPath,
		upstream:   upstream,
		mode:       mode,
		policy:     policy,
		log:        log,
		rproxy:     rp,
	}
}

// ListenPath returns the proxy socket path.
func (s *Server) ListenPath() string { return s.listenPath }

// socketMode is the final unix-socket mode the proxy exposes: owner rw
// only. The socket is created inside a private 0700 directory and the
// listen syscall additionally runs under a 0177 umask, so no group/world
// accessible inode is ever observable (see Start); the chmod afterwards
// sets the documented mode for consumers that traverse the dir.
const socketMode = 0o600

// privateDirMode is the mode the socket's parent directory keeps while
// the socket is being created (owner-only: nobody can traverse it, so
// nobody can connect even in the create->chmod window).
const privateDirMode = 0o700

// ensurePrivateDir and Server.Start are the unix half of this package:
// they live in proxy_unix.go (//go:build !windows) because they reach
// syscall.Stat_t and syscall.Umask, which do not exist under GOOS=windows
// (the Windows cross-build of ./cmd/worker hit "undefined: syscall.Stat_t"
// there). proxy_windows.go carries the matching stubs. The socket proxy is
// unix-only: on a Windows host dockerSocketBind gets a clear error from
// Start and mounts no docker socket at all (fail closed), never the raw
// daemon socket.

// serveFailed classifies a Serve loop error: the errors a normal
// Stop()-driven shutdown produces (http.ErrServerClosed, net.ErrClosed)
// stay silent, anything else is logged at error level so a dead proxy
// never looks healthy. Returns whether it logged (tests assert).
func (s *Server) serveFailed(err error) bool {
	if err == http.ErrServerClosed || err == net.ErrClosed {
	 return false
	}
	s.log.Error("docker proxy: serve loop failed", "err", err)
	return true
}

// Stop closes the listener and removes the socket file.
func (s *Server) Stop() {
	if s.http != nil {
		s.http.Close()
	}
	if s.ln != nil {
		s.ln.Close()
	}
	os.Remove(s.listenPath)
}
