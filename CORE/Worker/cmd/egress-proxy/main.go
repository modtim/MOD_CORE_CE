// Command mod-egress-proxy is the per-task egress sidecar (GB.EGR.D.4). It
// runs the allowlist-enforcing forward proxy (internal/egressproxy.Proxy) on
// MOD_EGRESS_HTTP_ADDR (default :3128) and the NXDOMAIN-only DNS stub on
// MOD_EGRESS_DNS_ADDR (default :53). Decisions are written to stdout as JSON
// lines stamped with MOD_EGRESS_TASK_ID; the worker reads them at teardown.
//
// The image runs as uid 65534. Binding :53 therefore needs NET_BIND_SERVICE,
// which the runtime (GB.EGR.D.6 setupEgressProxy) grants via
// HostConfig.CapAdd=["NET_BIND_SERVICE"]. Without it, override
// MOD_EGRESS_DNS_ADDR with an unprivileged port.
//
// Env:
//
//	MOD_EGRESS_ALLOW      comma-separated allowlist (egress.ParseList); a parse
//	                      error exits 2 (fail closed)
//	MOD_EGRESS_TASK_ID    stamped on every log line
//	MOD_EGRESS_HTTP_ADDR  proxy listen address (default :3128)
//	MOD_EGRESS_DNS_ADDR   DNS stub listen address (default :53)
//
// `-healthcheck` dials 127.0.0.1:<proxy port> and exits 0 (up) or 1 (down).
// It reads no files, so it works in a FROM scratch image.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egress"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egressproxy"
)

const (
	defaultHTTPAddr    = ":3128"
	defaultDNSAddr     = egressproxy.DefaultDNSAddr
	healthcheckTimeout = 2 * time.Second
	shutdownTimeout    = 5 * time.Second
	readHeaderTimeout  = 15 * time.Second
)

// lockedWriter serializes whole-line writes from the proxy and the DNS stub
// onto one stream so JSON lines never interleave.
type lockedWriter struct {
	mu sync.Mutex
	w  io.Writer
}

func (l *lockedWriter) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.w.Write(p)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// healthcheckAddr maps a listen address to a loopback dial target.
func healthcheckAddr(listen string) (string, error) {
	_, port, err := net.SplitHostPort(listen)
	if err != nil {
		return "", err
	}
	return net.JoinHostPort("127.0.0.1", port), nil
}

func healthcheck(listen string) int {
	target, err := healthcheckAddr(listen)
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: bad MOD_EGRESS_HTTP_ADDR %q: %v\n", listen, err)
		return 1
	}
	c, err := net.DialTimeout("tcp", target, healthcheckTimeout)
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: %v\n", err)
		return 1
	}
	_ = c.Close()
	return 0
}

func main() {
	os.Exit(run())
}

func run() int {
	hc := flag.Bool("healthcheck", false, "dial the local proxy port and exit 0 (up) or 1 (down)")
	flag.Parse()

	httpAddr := envOr("MOD_EGRESS_HTTP_ADDR", defaultHTTPAddr)
	dnsAddr := envOr("MOD_EGRESS_DNS_ADDR", defaultDNSAddr)
	if *hc {
		return healthcheck(httpAddr)
	}

	allow, err := egress.ParseList(os.Getenv("MOD_EGRESS_ALLOW"))
	if err != nil {
		fmt.Fprintf(os.Stderr, "mod-egress-proxy: MOD_EGRESS_ALLOW: %v\n", err)
		return 2
	}
	taskID := os.Getenv("MOD_EGRESS_TASK_ID")
	out := &lockedWriter{w: os.Stdout}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	dns, err := egressproxy.ListenDNSStub(dnsAddr, out)
	if err != nil {
		fmt.Fprintf(os.Stderr, "mod-egress-proxy: %v\n", err)
		return 1
	}
	dns.TaskID = taskID

	ln, err := net.Listen("tcp", httpAddr)
	if err != nil {
		_ = dns.Close()
		fmt.Fprintf(os.Stderr, "mod-egress-proxy: listen %s: %v\n", httpAddr, err)
		return 1
	}
	srv := &http.Server{
		Handler:           &egressproxy.Proxy{Allow: allow, Log: out, TaskID: taskID},
		ReadHeaderTimeout: readHeaderTimeout,
	}

	fmt.Fprintf(os.Stderr, "mod-egress-proxy: task=%q rules=%d http=%s dns=%s\n",
		taskID, allow.Len(), ln.Addr(), dns.Addr())

	var wg sync.WaitGroup
	errc := make(chan error, 2)
	wg.Add(2)
	go func() {
		defer wg.Done()
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errc <- fmt.Errorf("http: %w", err)
		}
	}()
	dnsCtx, dnsCancel := context.WithCancel(ctx)
	defer dnsCancel()
	go func() {
		defer wg.Done()
		_ = dns.Serve(dnsCtx)
	}()

	code := 0
	select {
	case <-ctx.Done():
		fmt.Fprintln(os.Stderr, "mod-egress-proxy: shutting down")
	case err := <-errc:
		fmt.Fprintf(os.Stderr, "mod-egress-proxy: %v\n", err)
		code = 1
	}
	dnsCancel()
	sctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(sctx); err != nil {
		_ = srv.Close()
	}
	wg.Wait()
	return code
}
