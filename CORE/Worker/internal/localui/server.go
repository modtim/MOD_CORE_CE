package localui

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
)

// Config configures a Server. Host/Port are validated by New (loopback
// only); StateDir, if non-empty, is where the auth token file is written.
type Config struct {
	// Host must be a loopback address. Empty defaults to "127.0.0.1"; any
	// other non-loopback value (including "0.0.0.0" or a LAN IP) makes New
	// return an error rather than start a server that a network peer could
	// reach.
	Host string
	// Port to listen on. 0 asks the OS for an ephemeral port (used by
	// tests); the caller (cmd/worker) is responsible for treating a
	// configured 0 as "disabled" and never calling New/Start at all — see
	// PortFromEnv's doc comment.
	Port int
	// StateDir is the directory the auth token file is written into. If
	// empty, New still succeeds (loopback-only + token-gated request
	// handling is still enforced) but the token is never persisted to
	// disk — only available via Server.Token() for the process that
	// started the server to hand to its own UI-launching code (e.g. a
	// future GP.9 tray "Open manager").
	StateDir string
	Provider Provider
	Log      *slog.Logger
}

// Server is the loopback-only local management web UI (D-P4).
type Server struct {
	cfg       Config
	token     string
	tokenPath string
	mux       *http.ServeMux
	ln        net.Listener
	httpSrv   *http.Server
}

// New validates cfg, generates a fresh auth token (writing it to
// cfg.StateDir if set), and binds a loopback listener. It does not start
// serving — call Start for that. New returning an error means "do not
// start the local UI"; callers should log and continue (this is a
// convenience feature, not load-bearing for task execution).
func New(cfg Config) (*Server, error) {
	if cfg.Provider == nil {
		return nil, fmt.Errorf("localui: nil Provider")
	}
	if cfg.Log == nil {
		cfg.Log = slog.Default()
	}
	host := cfg.Host
	if host == "" {
		host = "127.0.0.1"
	}
	if !isLoopbackHost(host) {
		return nil, fmt.Errorf("localui: refusing to bind non-loopback host %q (only 127.0.0.1/::1 are allowed)", host)
	}
	if cfg.Port < 0 {
		return nil, fmt.Errorf("localui: invalid port %d", cfg.Port)
	}

	token, err := generateToken()
	if err != nil {
		return nil, err
	}

	s := &Server{cfg: cfg, token: token}

	if cfg.StateDir != "" {
		path, err := writeTokenFile(cfg.StateDir, token)
		if err != nil {
			return nil, err
		}
		s.tokenPath = path
	}

	addr := net.JoinHostPort(host, strconv.Itoa(cfg.Port))
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("localui: listen %s: %w", addr, err)
	}
	// Belt-and-suspenders: re-check what we actually bound, in case a
	// caller passed a hostname that resolved to something unexpected (New
	// only checks the literal string above). A loopback string always
	// resolves to a loopback address, so this should never trip; if it
	// somehow does, refuse rather than silently serve a bindable-but-wrong
	// address.
	if tcpAddr, ok := ln.Addr().(*net.TCPAddr); ok && !tcpAddr.IP.IsLoopback() {
		ln.Close()
		return nil, fmt.Errorf("localui: bound address %s is not loopback", ln.Addr())
	}

	s.ln = ln
	s.mux = s.routes()
	s.httpSrv = &http.Server{Handler: s.mux}
	return s, nil
}

// isLoopbackHost reports whether host (a literal IP string) is a loopback
// address. Non-IP strings (hostnames, including "localhost") are rejected —
// D-P4 requires 127.0.0.1/::1 specifically, not "whatever DNS resolves
// nearby".
func isLoopbackHost(host string) bool {
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	return ip.IsLoopback()
}

// Start begins serving in a background goroutine. Safe to call once per
// Server.
func (s *Server) Start() {
	go func() {
		if err := s.httpSrv.Serve(s.ln); err != nil && err != http.ErrServerClosed {
			s.cfg.Log.Error("localui: serve failed", "error", err)
		}
	}()
	s.cfg.Log.Info("local UI listening", "addr", s.Addr(), "token_file", s.tokenPath)
}

// Stop gracefully shuts down the server, waiting up to ctx's deadline.
func (s *Server) Stop(ctx context.Context) error {
	return s.httpSrv.Shutdown(ctx)
}

// Addr returns the actual bound address (host:port), useful when Port was 0
// (OS-assigned).
func (s *Server) Addr() string {
	if s.ln == nil {
		return ""
	}
	return s.ln.Addr().String()
}

// TokenPath returns the path the auth token was written to, or "" if
// cfg.StateDir was empty.
func (s *Server) TokenPath() string { return s.tokenPath }

// Token returns the current auth token. Exported only for the process that
// started this Server (e.g. to hand to a tray icon's "Open manager" —
// GP.9); never log or transmit it anywhere off-loopback.
func (s *Server) Token() string { return s.token }
