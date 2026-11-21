package localui

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/availability"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/monitor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/nativeapps"
)

// fakeProvider is a minimal in-memory Provider for handler tests — no
// Docker daemon, no live manager connection, no real NOAH sampler.
type fakeProvider struct {
	mu     sync.Mutex
	paused bool

	status    StatusInfo
	noah      NOAHInfo
	logLines  []string
	logsOK    bool
	apps      []nativeapps.DetectedApp
	du        monitor.DiskUsage
	duOK      bool
	setPaused int // call count, for the toggle test

	// pinned is the fake's stand-in for the availability gate's pinned local
	// override (WIN.1.s13 lend/pause); setNoahMode counts the pins so a test
	// can assert the handler drove the provider exactly once.
	pinned      availability.LocalMode
	setNoahMode int
}

func (f *fakeProvider) Status() StatusInfo {
	f.mu.Lock()
	defer f.mu.Unlock()
	s := f.status
	s.Paused = f.paused
	return s
}
func (f *fakeProvider) Paused() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.paused
}
func (f *fakeProvider) SetPaused(v bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.paused = v
	f.setPaused++
}
func (f *fakeProvider) NOAH() NOAHInfo {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := f.noah
	// The real provider reads Pinned off the gate; the fake mirrors that so a
	// test can see a pin through the existing NOAH() getter.
	n.Pinned = f.pinned
	return n
}
func (f *fakeProvider) Logs() ([]string, bool) {
	return f.logLines, f.logsOK
}
func (f *fakeProvider) Apps() []nativeapps.DetectedApp { return f.apps }
func (f *fakeProvider) DiskUsage() (monitor.DiskUsage, bool) {
	return f.du, f.duOK
}
func (f *fakeProvider) PinnedNoahMode() availability.LocalMode {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.pinned
}
func (f *fakeProvider) SetNoahMode(mode availability.LocalMode) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.pinned = mode
	f.setNoahMode++
}

func newTestServer(t *testing.T) (*Server, *fakeProvider) {
	t.Helper()
	fp := &fakeProvider{
		status: StatusInfo{WorkerName: "w1", Version: "dev", Status: "idle", Connected: true, LastHeartbeat: time.Now()},
		noah:   NOAHInfo{Enabled: true, Available: true, Reason: "quiet", CanClaimNow: true},
		apps:   []nativeapps.DetectedApp{{Name: "Blender", Version: "4.1", Path: "/usr/bin/blender"}},
		du:     monitor.DiskUsage{ImagesBytes: 1024, At: time.Now()},
		duOK:   true,
	}
	srv, err := New(Config{
		Host:     "127.0.0.1",
		Port:     0,
		StateDir: t.TempDir(),
		Provider: fp,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { srv.ln.Close() })
	return srv, fp
}

func TestNonLoopbackBindRefused(t *testing.T) {
	for _, host := range []string{"0.0.0.0", "203.0.113.50", "10.0.0.1", "not-an-ip"} {
		t.Run(host, func(t *testing.T) {
			_, err := New(Config{Host: host, Port: 0, Provider: &fakeProvider{}})
			if err == nil {
				t.Fatalf("New(host=%q): expected error, got nil", host)
			}
		})
	}
}

func TestLoopbackBindAccepted(t *testing.T) {
	for _, host := range []string{"127.0.0.1", "::1", ""} {
		t.Run(host, func(t *testing.T) {
			srv, err := New(Config{Host: host, Port: 0, Provider: &fakeProvider{}})
			if err != nil {
				t.Fatalf("New(host=%q): unexpected error: %v", host, err)
			}
			defer srv.ln.Close()
		})
	}
}

func req(method, path, token string) *http.Request {
	r := httptest.NewRequest(method, "http://testworker.local"+path, nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	return r
}

func TestMissingTokenUnauthorized(t *testing.T) {
	srv, _ := newTestServer(t)
	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, req(http.MethodGet, "/", ""))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

func TestBadTokenUnauthorized(t *testing.T) {
	srv, _ := newTestServer(t)

	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, req(http.MethodGet, "/", "not-the-real-token"))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("bearer: status = %d, want 401", rec.Code)
	}

	rec2 := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec2, httptest.NewRequest(http.MethodGet, "http://testworker.local/?token=wrong", nil))
	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("query: status = %d, want 401", rec2.Code)
	}
}

func TestGoodTokenEachPage200(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, path := range []string{"/", "/pause", "/noah", "/logs", "/apps", "/disk"} {
		t.Run(path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			srv.mux.ServeHTTP(rec, req(http.MethodGet, path, srv.token))
			if rec.Code != http.StatusOK {
				t.Fatalf("GET %s: status = %d, want 200; body=%s", path, rec.Code, rec.Body.String())
			}
		})
	}
}

// TestCSPStrictEveryPage (tasks.md 24.4.s1): every HTML page must carry the
// strict Content-Security-Policy header, and no page may contain a <script>
// element (the pages are pure HTML + inline <style>; there is no JS at all,
// which is stricter than the spec's "separate embedded .js" allowance).
func TestCSPStrictEveryPage(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, path := range []string{"/", "/pause", "/noah", "/logs", "/apps", "/disk"} {
		t.Run(path, func(t *testing.T) {
			rec := httptest.NewRecorder()
			srv.mux.ServeHTTP(rec, req(http.MethodGet, path, srv.token))
			csp := rec.Header().Get("Content-Security-Policy")
			if !strings.Contains(csp, "default-src 'none'") {
				t.Fatalf("GET %s: CSP header = %s, want default-src 'none'", path, csp)
			}
			if body := rec.Body.String(); strings.Contains(body, "<script") {
				t.Fatalf("GET %s: page contains a <script> element", path)
			}
		})
	}
}

func TestQueryTokenSetsCookieAndStripsFromRedirect(t *testing.T) {
	srv, _ := newTestServer(t)
	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "http://testworker.local/?token="+srv.token, nil))
	if rec.Code != http.StatusFound {
		t.Fatalf("status = %d, want 302", rec.Code)
	}
	loc := rec.Header().Get("Location")
	if loc != "/" {
		t.Fatalf("Location = %q, want %q (token stripped)", loc, "/")
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Value != srv.token || !cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteStrictMode {
		t.Fatalf("unexpected cookie: %+v", cookies)
	}
}

func TestPausePOSTWithoutSameOriginForbidden(t *testing.T) {
	srv, fp := newTestServer(t)
	r := req(http.MethodPost, "/pause/on", srv.token)
	r.Header.Set("Origin", "http://evil.example")
	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	if fp.Paused() {
		t.Fatalf("provider was paused despite cross-origin POST")
	}
}

func TestPauseResumeTogglesProvider(t *testing.T) {
	srv, fp := newTestServer(t)

	on := req(http.MethodPost, "/pause/on", srv.token)
	on.Header.Set("Origin", "http://testworker.local")
	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, on)
	if rec.Code != http.StatusFound {
		t.Fatalf("pause/on: status = %d, want 302", rec.Code)
	}
	if !fp.Paused() {
		t.Fatalf("provider not paused after /pause/on")
	}

	off := req(http.MethodPost, "/pause/off", srv.token)
	off.Header.Set("Origin", "http://testworker.local")
	rec2 := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec2, off)
	if rec2.Code != http.StatusFound {
		t.Fatalf("pause/off: status = %d, want 302", rec2.Code)
	}
	if fp.Paused() {
		t.Fatalf("provider still paused after /pause/off")
	}
	if fp.setPaused != 2 {
		t.Fatalf("SetPaused called %d times, want 2", fp.setPaused)
	}
}

func TestPausePOSTSameOriginViaReferer(t *testing.T) {
	srv, fp := newTestServer(t)
	r := req(http.MethodPost, "/pause/on", srv.token)
	r.Header.Set("Referer", "http://testworker.local/pause")
	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, r)
	if rec.Code != http.StatusFound {
		t.Fatalf("status = %d, want 302; body=%s", rec.Code, rec.Body.String())
	}
	if !fp.Paused() {
		t.Fatalf("provider not paused")
	}
}

func TestTokenFilePerms(t *testing.T) {
	dir := t.TempDir()
	srv, err := New(Config{Host: "127.0.0.1", Port: 0, StateDir: dir, Provider: &fakeProvider{}})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer srv.ln.Close()

	path := srv.TokenPath()
	if path == "" {
		t.Fatalf("TokenPath is empty")
	}
	if filepath.Dir(path) != dir {
		t.Fatalf("token file %q not under state dir %q", path, dir)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat token file: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("token file perms = %o, want 0600", perm)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read token file: %v", err)
	}
	if string(data) != srv.token {
		t.Fatalf("token file content does not match server token")
	}
}

// --- NOAH lend/pause setter (WIN.1.s13) ---------------------------------
//
// postMode builds a POST to /noah/mode with a raw body (JSON or form-encoded)
// and, when token is non-empty, the local-UI Bearer credential.
func postMode(body, token, origin string) *http.Request {
	r := httptest.NewRequest(http.MethodPost, "http://testworker.local/noah/mode", strings.NewReader(body))
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	if origin != "" {
		r.Header.Set("Origin", origin)
	}
	return r
}

func TestNoahModeLendPinsAndIsVisible(t *testing.T) {
	srv, fp := newTestServer(t)
	r := postMode(`{"mode": "lend"}`, srv.token, "http://testworker.local")
	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, r)
	if rec.Code != http.StatusFound {
		t.Fatalf("lend: status = %d, want 302; body=%s", rec.Code, rec.Body.String())
	}
	if loc := rec.Header().Get("Location"); loc != "/noah" {
		t.Fatalf("lend: Location = %q, want /noah", loc)
	}
	if fp.setNoahMode != 1 {
		t.Fatalf("lend: SetNoahMode called %d times, want 1", fp.setNoahMode)
	}
	// The pin must be visible through the EXISTING NOAH() getter (the page the
	// redirect lands on), not a new one.
	n := fp.NOAH()
	if n.Pinned != availability.LocalModeLend {
		t.Fatalf("lend: NOAH().Pinned = %q, want lend", n.Pinned)
	}
}

func TestNoahModePausePins(t *testing.T) {
	srv, fp := newTestServer(t)
	r := postMode(`{"mode": "pause"}`, srv.token, "http://testworker.local")
	rec := httptest.NewRecorder()
	srv.mux.ServeHTTP(rec, r)
	if rec.Code != http.StatusFound {
		t.Fatalf("pause: status = %d, want 302", rec.Code)
	}
	if got := fp.PinnedNoahMode(); got != availability.LocalModePause {
	 t.Fatalf("pause: pinned = %q, want pause", got)
	}
}

func TestNoahModeNoneClearsPin(t *testing.T) {
	srv, fp := newTestServer(t)
	// Pin first, then hand the decision back to the automatic rules.
	srv.mux.ServeHTTP(httptest.NewRecorder(), postMode(`{"mode": "pause"}`, srv.token, "http://testworker.local"))
	if got := fp.PinnedNoahMode(); got != availability.LocalModePause {
	 t.Fatalf("setup: pinned = %q, want pause", got)
	}
	srv.mux.ServeHTTP(httptest.NewRecorder(), postMode(`{"mode": "none"}`, srv.token, "http://testworker.local"))
	if got := fp.PinnedNoahMode(); got != availability.LocalModeNone {
	 t.Fatalf("none: pinned = %q, want none", got)
	}
}

func TestNoahModeBadMode400(t *testing.T) {
	srv, fp := newTestServer(t)
	bodies := []string{
		`{"mode": "resume"}`,
		`{"mode": "LEND"}`,
		`{"mode": ""}`,
		`{"nope": "lend"}`,
		`not json at all`,
		`{"mode": "lend", "extra": `,
	}
	for _, b := range bodies {
	 rec := httptest.NewRecorder()
	 srv.mux.ServeHTTP(rec, postMode(b, srv.token, "http://testworker.local"))
	 if rec.Code != http.StatusBadRequest {
		 t.Fatalf("bad mode %q: status = %d, want 400", b, rec.Code)
	 }
	 if got := fp.PinnedNoahMode(); got != availability.LocalModeNone {
		 t.Fatalf("bad mode %q pinned %q: a rejected body must not pin", b, got)
	 }
	}
}

func TestNoahModeOversizeBody400(t *testing.T) {
	srv, fp := newTestServer(t)
	 big := `{"mode": "lend", "pad": "` + strings.Repeat("x", 4096) + `"}`
	 rec := httptest.NewRecorder()
	 srv.mux.ServeHTTP(rec, postMode(big, srv.token, "http://testworker.local"))
	 if rec.Code != http.StatusBadRequest {
	 t.Fatalf("oversize body: status = %d, want 400", rec.Code)
	 }
	 if got := fp.PinnedNoahMode(); got != availability.LocalModeNone {
	 t.Fatalf("oversize body pinned %q", got)
	 }
}

func TestNoahModeFormEncodedAccepted(t *testing.T) {
	// The plain HTML form on the /noah page can only post form-encoded, so the
	// browser path must work too — same vocabulary either way.
	srv, fp := newTestServer(t)
	 r := postMode("mode=lend", srv.token, "http://testworker.local")
	 r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	 rec := httptest.NewRecorder()
	 srv.mux.ServeHTTP(rec, r)
	 if rec.Code != http.StatusFound {
	 t.Fatalf("form lend: status = %d, want 302; body=%s", rec.Code, rec.Body.String())
	 }
	 if got := fp.PinnedNoahMode(); got != availability.LocalModeLend {
	 t.Fatalf("form lend: pinned = %q, want lend", got)
	 }
}

func TestNoahModeMissingToken401(t *testing.T) {
	srv, fp := newTestServer(t)
	 rec := httptest.NewRecorder()
	 srv.mux.ServeHTTP(rec, postMode(`{"mode": "lend"}`, "", "http://testworker.local"))
	 if rec.Code != http.StatusUnauthorized {
	 t.Fatalf("no token: status = %d, want 401", rec.Code)
	 }
	 if got := fp.PinnedNoahMode(); got != availability.LocalModeNone {
	 t.Fatalf("no token pinned %q", got)
	 }
}

func TestNoahModeCrossOrigin403(t *testing.T) {
	srv, fp := newTestServer(t)
	 rec := httptest.NewRecorder()
	 srv.mux.ServeHTTP(rec, postMode(`{"mode": "lend"}`, srv.token, "http://evil.example"))
	 if rec.Code != http.StatusForbidden {
	 t.Fatalf("cross-origin: status = %d, want 403", rec.Code)
	 }
	 if got := fp.PinnedNoahMode(); got != availability.LocalModeNone {
	 t.Fatalf("cross-origin pinned %q", got)
	 }
}

func TestNoahModeUnreachableOffLoopback(t *testing.T) {
	// The setter is only reachable where the server itself is: a non-loopback
	// bind is refused outright, so no caller (LAN peer or app on another box)
	// can ever reach /noah/mode.
	for _, host := range []string{"0.0.0.0", "203.0.113.50"} {
	 srv, err := New(Config{Host: host, Port: 0, Provider: &fakeProvider{}})
	 if err == nil {
	 srv.ln.Close()
	 t.Fatalf("New(host=%q): expected error, got nil", host)
	 }
	}
}
