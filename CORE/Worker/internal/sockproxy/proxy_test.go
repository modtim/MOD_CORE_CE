package sockproxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// capturedLog is a slog.Handler that records message + attrs so tests
// can assert on the structured deny/report lines.
type capturedLog struct {
	mu    sync.Mutex
	lines []string
}

func (c *capturedLog) Enabled(_ context.Context, _ slog.Level) bool { return true }

func (c *capturedLog) Handle(_ context.Context, r slog.Record) error {
	var b strings.Builder
	b.WriteString(r.Message)
	r.Attrs(func(a slog.Attr) bool {
		b.WriteByte(' ')
		b.WriteString(a.Key)
		b.WriteByte('=')
		b.WriteString(a.Value.String())
		return true
	})
	c.mu.Lock()
	c.lines = append(c.lines, b.String())
	c.mu.Unlock()
	return nil
}

func (c *capturedLog) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *capturedLog) WithGroup(string) slog.Handler      { return c }

func (c *capturedLog) Has(sub string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, l := range c.lines {
		if strings.Contains(l, sub) {
			return true
		}
	}
	return false
}

// upstream is a fake Docker daemon on a unix socket: it records what it
// receives and answers "ok" (or a raw 101 hijack for upgrade requests).
type upstream struct {
	sock     string
	ln       net.Listener
	mu       sync.Mutex
	calls    int
	method   string
	path     string
	body     string
	upgrade  bool
	upgradeN int
}

func startUpstream(t *testing.T) *upstream {
	dir := t.TempDir()
	u := &upstream{sock: filepath.Join(dir, "docker.sock")}
	ln, err := net.Listen("unix", u.sock)
	if err != nil {
		t.Fatal(err)
	}
	u.ln = ln
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		u.mu.Lock()
		u.calls++
		u.method = r.Method
		u.path = r.URL.RequestURI()
		u.body = string(body)
		upgrade := u.upgrade
		u.mu.Unlock()
		if upgrade {
			hj, ok := w.(http.Hijacker)
			if !ok {
				t.Error("upstream response is not a hijacker")
				return
			}
			conn, buf, err := hj.Hijack()
			if err != nil {
				t.Error("hijack failed:", err)
				return
			}
			u.mu.Lock()
			u.upgradeN++
			u.mu.Unlock()
			buf.WriteString("HTTP/1.1 101 UPGRADED\r\n" +
				"Content-Type: application/vnd.docker.raw-stream\r\n" +
				"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
			buf.Flush()
			io.WriteString(conn, "hello-from-daemon")
			conn.Close()
			return
		}
		io.WriteString(w, "ok")
	})
	go func() { http.Serve(ln, mux) }()
	t.Cleanup(func() {
		ln.Close()
		os.Remove(u.sock)
	})
	return u
}

func startProxy(t *testing.T, u *upstream, mode Mode) (*Server, *capturedLog) {
	dir := t.TempDir()
	lp := filepath.Join(dir, "proxy.sock")
	c := &capturedLog{}
	s := New(lp, u.sock, mode, DefaultPolicy(), slog.New(c))
	if err := s.Start(); err != nil {
		t.Fatal("proxy start:", err)
	}
	t.Cleanup(s.Stop)
	return s, c
}

func proxyClient(sock string) *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			Dial: func(_, _ string) (net.Conn, error) { return net.Dial("unix", sock) },
		},
	}
}

func post(t *testing.T, c *http.Client, url, body string) *http.Response {
	req, err := http.NewRequest(http.MethodPost, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal("request via proxy:", err)
	}
	return resp
}

// TestReportModeForwardsAndLogs: in report mode a policy-denied
// request still reaches the upstream daemon (body intact) and leaves
// exactly one structured deny line in the log.
func TestReportModeForwardsAndLogs(t *testing.T) {
	u := startUpstream(t)
	s, c := startProxy(t, u, ModeReport)
	body := `{"Image":"busybox","HostConfig":{"Privileged":true}}`
	resp := post(t, proxyClient(s.ListenPath()), "http://sockproxy/containers/create", body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("report mode must forward: got %d", resp.StatusCode)
	}
	u.mu.Lock()
	calls, gotBody := u.calls, u.body
	u.mu.Unlock()
	if calls != 1 || gotBody != body {
		t.Errorf("upstream saw calls=%d body=%q, want 1 call with body intact", calls, gotBody)
	}
	if !c.Has("request denied by policy") || !c.Has("Privileged=true") {
		t.Errorf("deny log line missing; lines: %v", c.lines)
	}
}

// TestEnforceMode403: in enforce mode a denied request gets a 403 JSON
// answer and never reaches the upstream.
func TestEnforceMode403(t *testing.T) {
	u := startUpstream(t)
	s, _ := startProxy(t, u, ModeEnforce)
	resp := post(t, proxyClient(s.ListenPath()), "http://sockproxy/swarm", `{"Foo":"bar"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("enforce mode must 403: got %d", resp.StatusCode)
	}
	out, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(out), `"message"`) || !strings.Contains(string(out), "allowlist") {
		t.Errorf("403 body must be JSON with the reason: %s", out)
	}
	u.mu.Lock()
	calls := u.calls
	u.mu.Unlock()
	if calls != 0 {
		t.Errorf("denied request must not reach upstream; calls=%d", calls)
	}
}

// TestAllowedCreateForwardsBodyIntact: an allowed create passes
// through with the body byte-identical.
func TestAllowedCreateForwardsBodyIntact(t *testing.T) {
	u := startUpstream(t)
	s, _ := startProxy(t, u, ModeEnforce)
	body := `{"Image":"busybox","Cmd":["sh"],"HostConfig":{"Binds":["/data:/data"],"CapAdd":["SYS_NICE"],"DeviceRequests":[{"Driver":"nvidia"}]}}`
	resp := post(t, proxyClient(s.ListenPath()), "http://sockproxy/containers/create?name=t1", body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("allowed create: got %d", resp.StatusCode)
	}
	u.mu.Lock()
	gotPath, gotBody := u.path, u.body
	u.mu.Unlock()
	if gotBody != body {
		t.Errorf("body not intact:\n got %s\nwant %s", gotBody, body)
	}
	if gotPath != "/containers/create?name=t1" {
		t.Errorf("upstream path %q, want /containers/create?name=t1", gotPath)
	}
}

// TestHijackUpgrade: attach/exec-style connections upgrade (101) and
// the raw stream from the daemon flows through the proxy.
func TestHijackUpgrade(t *testing.T) {
	u := startUpstream(t)
	u.upgrade = true
	s, _ := startProxy(t, u, ModeEnforce)
	req, err := http.NewRequest(http.MethodPost, "http://sockproxy/containers/abc/attach", strings.NewReader(`{"stream":true}`))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Upgrade", "tcp")
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Content-Type", "application/json")
	resp, err := proxyClient(s.ListenPath()).Do(req)
	if err != nil {
		t.Fatal("attach via proxy:", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("attach: want 101, got %d", resp.StatusCode)
	}
	out, _ := io.ReadAll(resp.Body)
	if string(out) != "hello-from-daemon" {
		t.Errorf("upgraded stream = %q, want hello-from-daemon", out)
	}
	u.mu.Lock()
	n := u.upgradeN
	u.mu.Unlock()
	if n != 1 {
		t.Errorf("upstream hijack count = %d, want 1", n)
	}
}

// TestSwarmDeniedInReport checks a non-body endpoint denial in report
// mode (denied + logged + forwarded), covering the GET path through the
// proxy.
func TestSwarmDeniedInReport(t *testing.T) {
	u := startUpstream(t)
	s, c := startProxy(t, u, ModeReport)
	req, err := http.NewRequest(http.MethodGet, "http://sockproxy/swarm", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := proxyClient(s.ListenPath()).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("report mode must forward GET /swarm: got %d", resp.StatusCode)
	}
	if !c.Has("GET /swarm") {
		t.Errorf("deny log must name the path; lines: %v", c.lines)
	}
}

// ---- FIX-GO-SOCKPROXY-CREATE-CHMOD-RACE (S-2 socket mode, S-3 serve
// error) ----

// TestSocketModeNoPermissiveWindow (a): the socket's parent dir is
// private 0700 and the socket's final mode is 0600; a watcher goroutine
// polling os.Stat from the moment the dir exists must never observe a
// group/world-accessible dir or socket before/around the chmod. 50
// rounds.
type modeWatch struct {
	bad  []string
	 mu   sync.Mutex
	 done bool
}

func (w *modeWatch) halt() {
	 w.mu.Lock()
	 w.done = true
	 w.mu.Unlock()
}

func (w *modeWatch) halted() bool {
	 w.mu.Lock()
	 d := w.done
	 w.mu.Unlock()
	 return d
}

func (w *modeWatch) note(s string) {
	 w.mu.Lock()
	 w.bad = append(w.bad, s)
	 w.mu.Unlock()
}

func (w *modeWatch) seen() []string {
	 w.mu.Lock()
	 defer w.mu.Unlock()
	 var out []string
	 for _, s := range w.bad {
	  out = append(out, s)
	}
	 return out
}

// modeBits renders permission bits as octal text for failure messages.
func modeBits(m os.FileMode) string {
	return fmt.Sprintf("%o", int(m & 0o777))
}

func watchModes(dir, lp string, w *modeWatch) {
	 for i := 0; i < 200000; i++ {
	  if ds, err := os.Stat(dir); err == nil && ds.Mode()&0o777&0o077 != 0 {
	   w.note("dir " + modeBits(ds.Mode()))
	 }
	  if st, err := os.Stat(lp); err == nil && st.Mode()&0o777&0o077 != 0 {
	   w.note("sock " + modeBits(st.Mode()))
	 }
	 }
}

func TestSocketModeNoPermissiveWindow(t *testing.T) {
	for round := 0; round < 50; round++ {
	 dir := t.TempDir()
	 if err := os.Chmod(dir, 0o700); err != nil {
	  t.Fatal("tempdir chmod:", err)
	}
	 lp := filepath.Join(dir, "proxy.sock")
	 u := startUpstream(t)
	 c := &capturedLog{}
	 s := New(lp, u.sock, ModeReport, DefaultPolicy(), slog.New(c))
	 w := &modeWatch{}
	 go watchModes(dir, lp, w)
	 if err := s.Start(); err != nil {
	  t.Fatal("proxy start:", err)
	}
	 st, err := os.Stat(lp)
	 if err != nil {
	  t.Fatal("stat socket:", err)
	}
	 if m := st.Mode() & 0o777; m != 0o600 {
	  t.Errorf("socket final mode must be 0600, got %s", modeBits(m))
	 return
	}
	 ds, err := os.Stat(dir)
	 if err != nil {
	  t.Fatal("stat dir:", err)
	}
	 if m := ds.Mode() & 0o777; m != 0o700 {
	  t.Errorf("socket dir mode must be 0700, got %s", modeBits(m))
	 return
	}
	 if seen := w.seen(); len(seen) > 0 {
	  t.Errorf("permissive mode observed around create: %v", seen)
	 return
	}
	 s.Stop()
	}
}

// TestServeErrorIsLogged (b): a Serve loop error that is not a normal
// shutdown must be logged at error level (a dead proxy must never look
// healthy); the expected shutdown errors must NOT be logged.
func TestServeErrorIsLogged(t *testing.T) {
	 u := startUpstream(t)
	 s, c := startProxy(t, u, ModeReport)
	 if s == nil {
	  return
	}
	 if !s.serveFailed(errors.New("accept boom")) {
	  t.Errorf("a non-shutdown Serve error must be logged")
	 return
	}
	 if !c.Has("serve loop failed") {
	  t.Errorf("serve error must be logged at error level; lines: %v", c.lines)
	 return
	}
	 if s.serveFailed(net.ErrClosed) || s.serveFailed(http.ErrServerClosed) {
	  t.Errorf("normal shutdown errors must not be logged")
	 return
	}
	 s.Stop()
}

// TestNormalShutdownDoesNotLogServeError: a proxy started and stopped
// normally logs no serve-loop failure.
func TestNormalShutdownDoesNotLogServeError(t *testing.T) {
	 u := startUpstream(t)
	 s, c := startProxy(t, u, ModeReport)
	 if s == nil {
	  return
	}
	 s.Stop()
	 if c.Has("serve loop failed") {
	  t.Errorf("normal shutdown must not log a serve error; lines: %v", c.lines)
	}
}
