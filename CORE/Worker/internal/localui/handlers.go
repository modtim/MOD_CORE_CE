package localui

import (
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/availability"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/nativeapps"
)

// pageHead/pageFoot bracket every page's own content. Minimal inline CSS,
// no external assets (fonts, JS, images) — this server has no other route
// to serve them from, and D-P4 doesn't ask for more than a plain
// operator-facing control panel.
const pageHead = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MOD Worker — local UI</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fafafa;line-height:1.5}
nav{margin-bottom:1rem}
nav a{margin-right:1rem;text-decoration:none;color:#2054a6}
nav a:hover{text-decoration:underline}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
td,th{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;vertical-align:top}
.pill{display:inline-block;padding:.1rem .6rem;border-radius:1rem;font-size:.85em;font-weight:600}
.ok{background:#d7f5dd;color:#0a5c2b}
.warn{background:#fde9c8;color:#7a4a06}
.bad{background:#fadada;color:#7a1414}
button{padding:.5rem 1.1rem;margin-right:.6rem;cursor:pointer;font-size:1em}
pre{background:#1a1a1a;color:#d6d6d6;padding:.8rem;overflow-x:auto;border-radius:4px}
</style></head>
<body>
<h1>MOD Worker local UI</h1>
<nav>
<a href="/">Status</a>
<a href="/pause">Pause / Resume</a>
<a href="/noah">NOAH</a>
<a href="/logs">Logs</a>
<a href="/apps">Detected apps</a>
<a href="/disk">Disk usage</a>
</nav>
<hr>
`

const pageFoot = `</body></html>`

func mustPageTemplate(name, contentSrc string) *template.Template {
	return template.Must(template.New(name).Parse(pageHead + contentSrc + pageFoot))
}

// routes builds the request multiplexer. Every page (GET) route is wrapped
// in requireAuth; both state-changing (POST) routes additionally go
// through requireSameOrigin (CSRF).
func (s *Server) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.requireAuth(s.handleStatus))
	mux.HandleFunc("/pause", s.requireAuth(s.handlePausePage))
	mux.HandleFunc("/pause/on", s.requireAuth(s.requireSameOrigin(s.handlePauseOn)))
	mux.HandleFunc("/pause/off", s.requireAuth(s.requireSameOrigin(s.handlePauseOff)))
	mux.HandleFunc("/noah", s.requireAuth(s.handleNOAH))
mux.HandleFunc("/noah/mode", s.requireAuth(s.requireSameOrigin(s.handleNoahMode)))
	mux.HandleFunc("/logs", s.requireAuth(s.handleLogs))
	mux.HandleFunc("/apps", s.requireAuth(s.handleApps))
	mux.HandleFunc("/disk", s.requireAuth(s.handleDisk))
	return mux
}

// --- Status --------------------------------------------------------------

var statusTpl = mustPageTemplate("status", `
<h2>Status</h2>
<table>
<tr><th>Worker</th><td>{{.WorkerName}}</td></tr>
<tr><th>Version</th><td>{{.Version}}</td></tr>
<tr><th>Task status</th><td>{{.Status}}</td></tr>
<tr><th>Connected</th><td>{{if .Connected}}<span class="pill ok">connected</span>{{else}}<span class="pill bad">disconnected</span>{{end}}</td></tr>
<tr><th>Last heartbeat</th><td>{{.LastHeartbeatText}}</td></tr>
<tr><th>Paused</th><td>{{if .Paused}}<span class="pill warn">paused (not claiming)</span>{{else}}<span class="pill ok">claiming</span>{{end}}</td></tr>
<tr><th>Running tasks</th><td>{{.RunningTasksText}}</td></tr>
</table>
`)

type statusView struct {
	WorkerName        string
	Version           string
	Status            string
	Connected         bool
	LastHeartbeatText string
	Paused            bool
	RunningTasksText  string
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	st := s.cfg.Provider.Status()
	v := statusView{
		WorkerName:        st.WorkerName,
		Version:           st.Version,
		Status:            st.Status,
		Connected:         st.Connected,
		LastHeartbeatText: "never",
		Paused:            st.Paused,
		RunningTasksText:  "none",
	}
	if !st.LastHeartbeat.IsZero() {
		v.LastHeartbeatText = fmt.Sprintf("%s (%s ago)", st.LastHeartbeat.Format(time.RFC3339), time.Since(st.LastHeartbeat).Round(time.Second))
	}
	if len(st.RunningTasks) > 0 {
		v.RunningTasksText = fmt.Sprintf("%v", st.RunningTasks)
	}
	renderTemplate(w, statusTpl, v)
}

// --- Pause / resume --------------------------------------------------------

var pauseTpl = mustPageTemplate("pause", `
<h2>Pause / resume</h2>
<p>Current state: {{if .Paused}}<span class="pill warn">paused</span> — the worker is not claiming new tasks. Tasks already running continue.{{else}}<span class="pill ok">running</span> — the worker claims new tasks normally.{{end}}</p>
<form method="post" action="/pause/on"><button type="submit" {{if .Paused}}disabled{{end}}>Pause</button></form>
<form method="post" action="/pause/off"><button type="submit" {{if not .Paused}}disabled{{end}}>Resume</button></form>
`)

type pauseView struct{ Paused bool }

func (s *Server) handlePausePage(w http.ResponseWriter, r *http.Request) {
	renderTemplate(w, pauseTpl, pauseView{Paused: s.cfg.Provider.Paused()})
}

func (s *Server) handlePauseOn(w http.ResponseWriter, r *http.Request) {
	s.cfg.Provider.SetPaused(true)
	http.Redirect(w, r, "/pause", http.StatusFound)
}

func (s *Server) handlePauseOff(w http.ResponseWriter, r *http.Request) {
	s.cfg.Provider.SetPaused(false)
	http.Redirect(w, r, "/pause", http.StatusFound)
}

// --- NOAH ------------------------------------------------------------------

var noahTpl = mustPageTemplate("noah", `
<h2>NOAH (opportunistic availability harvest)</h2>
<p>The thresholds below are pushed by the manager (or set in this worker's static config) and stay read-only here. What a local user <em>can</em> change is whether this machine is being lent at all: "lend this machine" makes it available for opportunistic work right now, "pause lending" withholds it while you are using it, and "automatic" hands the decision back to the idle-detection rules below.</p>
<table>
<tr><th>Enabled</th><td>{{.Enabled}}</td></tr>
<tr><th>Local override</th><td>{{if .Pinned}}<span class="pill warn">{{.Pinned}}</span>{{else}}<span class="pill ok">(none — automatic)</span>{{end}}</td></tr>
<tr><th>Currently available (quiet)</th><td>{{.Available}}</td></tr>
<tr><th>Reason</th><td>{{.Reason}}</td></tr>
<tr><th>Can claim now</th><td>{{.CanClaimNow}}</td></tr>
<tr><th>Sustained-quiet threshold</th><td>{{.SustainedQuietS}}s</td></tr>
<tr><th>Pause grace</th><td>{{.PauseGraceS}}s</td></tr>
<tr><th>Plugged-in only</th><td>{{.PluggedInOnly}}</td></tr>
<tr><th>Schedule window</th><td>{{if .ScheduleStart}}{{.ScheduleStart}} – {{.ScheduleEnd}}{{else}}(none — always eligible){{end}}</td></tr>
</table>
<form method="post" action="/noah/mode"><input type="hidden" name="mode" value="lend"><button type="submit">Lend this machine</button></form>
<form method="post" action="/noah/mode"><input type="hidden" name="mode" value="pause"><button type="submit">Pause lending</button></form>
<form method="post" action="/noah/mode"><input type="hidden" name="mode" value="none"><button type="submit">Automatic</button></form>
`)

func (s *Server) handleNOAH(w http.ResponseWriter, r *http.Request) {
	renderTemplate(w, noahTpl, s.cfg.Provider.NOAH())
}

// handleNoahMode is the local NOAH lend/pause setter (WIN.1.s13, the worker
// half of tasks.md 24.5): POST /noah/mode with a small JSON body {"mode":
// "lend"|"pause"|"none"} pins the worker's local override on its existing
// availability verdict, so a local user can say "lend this machine" / "pause
// lending (I'm using it)" without waiting for the idle-detection rules.
//
// Protection is exactly the existing mutating-handler pair: requireAuth (the
// local-UI token) outside requireSameOrigin (CSRF), same as /pause/on and
// /pause/off; loopback-only comes from New's bind check. The body is parsed
// with availability.ParseLocalMode — the SAME vocabulary the availability
// package uses for its pinned reason strings — and anything else is a 400.
//
// It does not report anything itself: the pin lands on the gate's existing
// setAvailable path, which already feeds the heartbeat's noah_available/
// noah_reason fields and the yield/resume callbacks.
func (s *Server) handleNoahMode(w http.ResponseWriter, r *http.Request) {
	mode, err := parseModeBody(r)
	if err != nil {
		http.Error(w, "bad mode: expected JSON {\"mode\": \"lend\"|\"pause\"|\"none\"}", http.StatusBadRequest)
		return
	}
	s.cfg.Provider.SetNoahMode(mode)
	http.Redirect(w, r, "/noah", http.StatusFound)
}

// parseModeBody reads the setter's small body. The app/tray posts JSON
// ({"mode": "lend"}); the plain HTML form on the /noah page can only post
// form-encoded, so both shapes are accepted and both go through the SAME
// availability.ParseLocalMode vocabulary — one parser, one truth. Anything
// else (missing mode, unknown word, malformed JSON, oversize body) is an
// error the handler turns into a 400.
func parseModeBody(r *http.Request) (availability.LocalMode, error) {
	const maxModeBody = 1024
	if strings.HasPrefix(r.Header.Get("Content-Type"), "application/x-www-form-urlencoded") {
		raw, err := io.ReadAll(io.LimitReader(r.Body, maxModeBody+1))
		if err != nil {
			return availability.LocalModeNone, fmt.Errorf("localui: read mode body: %w", err)
		}
		if len(raw) > maxModeBody {
			return availability.LocalModeNone, fmt.Errorf("localui: mode body too large")
		}
		q, err := url.ParseQuery(string(raw))
		if err != nil {
			return availability.LocalModeNone, fmt.Errorf("localui: parse mode form: %w", err)
		}
		return availability.ParseLocalMode(strings.TrimSpace(q.Get("mode")))
	}
	var body modeBody
	if err := json.NewDecoder(io.LimitReader(r.Body, maxModeBody+1)).Decode(&body); err != nil {
		return availability.LocalModeNone, fmt.Errorf("localui: parse mode body: %w", err)
	}
	return availability.ParseLocalMode(strings.TrimSpace(body.Mode))
}

// modeBody is the setter's JSON shape: {"mode": "lend"}.
type modeBody struct {
	Mode string `json:"mode"`
}

// --- Logs --------------------------------------------------------------

var logsTpl = mustPageTemplate("logs", `
<h2>Recent logs</h2>
{{if .Available}}<pre>{{.Text}}</pre>{{else}}<p>Not available — this worker build has no in-memory log ring buffer wired up. Check the process's stdout/journal instead.</p>{{end}}
`)

type logsView struct {
	Available bool
	Text      string
}

func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	lines, ok := s.cfg.Provider.Logs()
	v := logsView{Available: ok}
	for _, l := range lines {
		v.Text += l + "\n"
	}
	renderTemplate(w, logsTpl, v)
}

// --- Detected apps -------------------------------------------------------

var appsTpl = mustPageTemplate("apps", `
<h2>Detected native apps</h2>
{{if .Apps}}<table><tr><th>Name</th><th>Version</th><th>Path</th></tr>
{{range .Apps}}<tr><td>{{.Name}}</td><td>{{.Version}}</td><td>{{.Path}}</td></tr>
{{end}}</table>{{else}}<p>No native apps detected on this worker.</p>{{end}}
`)

func (s *Server) handleApps(w http.ResponseWriter, r *http.Request) {
	renderTemplate(w, appsTpl, struct{ Apps []nativeapps.DetectedApp }{s.cfg.Provider.Apps()})
}

// --- Disk usage ------------------------------------------------------------

var diskTpl = mustPageTemplate("disk", `
<h2>Disk usage</h2>
{{if .Available}}<table>
<tr><th>Images</th><td>{{.Images}}</td></tr>
<tr><th>Model cache</th><td>{{.ModelCache}}</td></tr>
<tr><th>Work dirs</th><td>{{.Workdirs}}</td></tr>
<tr><th>Reclaimable</th><td>{{.Reclaimable}}</td></tr>
<tr><th>As of</th><td>{{.At}}</td></tr>
</table>{{else}}<p>Not available yet — the disk-usage collector hasn't completed its first pass.</p>{{end}}
`)

type diskView struct {
	Available                                     bool
	Images, ModelCache, Workdirs, Reclaimable, At string
}

func (s *Server) handleDisk(w http.ResponseWriter, r *http.Request) {
	du, ok := s.cfg.Provider.DiskUsage()
	v := diskView{Available: ok}
	if ok {
		v.Images = humanBytes(du.ImagesBytes)
		v.ModelCache = humanBytes(du.ModelCacheBytes)
		v.Workdirs = humanBytes(du.WorkdirsBytes)
		v.Reclaimable = humanBytes(du.ReclaimableBytes)
		v.At = du.At.Format(time.RFC3339)
	}
	renderTemplate(w, diskTpl, v)
}

func humanBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for n2 := n / unit; n2 >= unit; n2 /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(n)/float64(div), "KMGTPE"[exp])
}

func renderTemplate(w http.ResponseWriter, t *template.Template, data interface{}) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	// Strict CSP (D-P4 / tasks.md 24.4.s1): the pages are fully self-contained
	// (embedded inline <style>, no <script> at all, no external assets), so the
	// policy allows nothing but same-origin styles and inline styles. Anything
	// a template ever emits that would load or run third-party content is
	// blocked outright.
	w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'self' 'unsafe-inline';")
	if err := t.Execute(w, data); err != nil {
		http.Error(w, "render error: "+err.Error(), http.StatusInternalServerError)
	}
}
