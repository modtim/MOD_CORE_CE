package executor

// HELPER-CONTAINERS-LOCKDOWN tests: every helper the size-probe audit flagged
// (result-JSON read, mc sidecars, volume prep, GPU scrub) plus the created-
// never-started archive-read twins must carry the shared lockdown posture
// through the REAL create path: a fake Engine over HTTP records the create
// body the docker client sends, so the assertions run on what the DAEMON
// would receive. Each guard also has a mutation check (the sizeprobe_lockdown
// pattern): the same posture with ONE guard stripped must fail its assertion.

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// --- posture checker over a HostConfig struct --------------------------------
// One checker, every helper: it returns the FIRST missing guard ("" = clean)
// so a failing test names the guard, and the mutation checks reuse it.
// needNet is true only for the mc sidecars (mc must reach MinIO/S3).
func postureGap(h *container.HostConfig, needNet bool, netName string) string {
	if h == nil {
		return "no HostConfig"
	}
	if !needNet && h.NetworkMode != container.NetworkMode("none") {
		return "NetworkMode must be none, got " + fmt.Sprint(h.NetworkMode)
	}
	if needNet && h.NetworkMode != container.NetworkMode(netName) {
		return "NetworkMode must be the task network " + netName +
			", got " + fmt.Sprint(h.NetworkMode)
	}
	if !hasOne(h.CapDrop, "ALL") {
		return "CapDrop must contain ALL"
	}
	if !hasOne(h.SecurityOpt, "no-new-privileges:true") {
		return "SecurityOpt must contain no-new-privileges:true"
	}
	if h.Privileged {
		return "Privileged must be false"
	}
	if h.Resources.Memory <= 0 || h.Resources.PidsLimit == nil || *h.Resources.PidsLimit <= 0 {
		return "memory + pids safety limits required"
	}
	return ""
}

func hasOne(list []string, want string) bool {
	for _, x := range list {
		if x == want {
			return true
		}
	}
	return false
}

// --- posture over the RECORDED create JSON -----------------------------------
// The docker client serialises HostConfig to the daemon; these readers assert
// on that wire form, so a change at a call site that bypasses the builders
// is caught too.
func jsonGap(m map[string]any, needNet bool, netName string) string {
	hc := asMap(m["HostConfig"])
	if len(hc) == 0 {
		return "no HostConfig in the create body"
	}
	nm := asStr(hc["NetworkMode"])
	if !needNet && nm != "none" {
		return "wire NetworkMode must be none, got " + nm
	}
	if needNet && nm != netName {
		return "wire NetworkMode must be " + netName + ", got " + nm
	}
	if !hasOne(asStrings(hc["CapDrop"]), "ALL") {
		return "wire CapDrop must contain ALL"
	}
	if !hasOne(asStrings(hc["SecurityOpt"]), "no-new-privileges:true") {
		return "wire SecurityOpt must contain no-new-privileges:true"
	}
	// The Go SDK embeds Resources in HostConfig, so the wire form carries
	// Memory / PidsLimit flattened at the HostConfig level (not nested).
	if asNum(hc["Memory"]) <= 0 || asNum(hc["PidsLimit"]) <= 0 {
		return "wire memory + pids limits required"
	}
	return ""
}

func asMap(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return map[string]any{}
}
func asStrings(v any) []string {
	out := []string{}
	if l, ok := v.([]any); ok {
		for _, x := range l {
			if s, ok := x.(string); ok {
				out = append(out, s)
			}
		}
	}
	return out
}
func asStr(v any) string {
	s, ok := v.(string)
	if ok {
		return s
	}
	return ""
}
func asNum(v any) int64 {
	if n, ok := v.(float64); ok {
		return int64(n)
	}
	if n, ok := v.(int); ok {
		return int64(n)
	}
	return 0
}

// --- the fake Engine: records every create body and every call path ---------
type fakeEng struct {
	mu        sync.Mutex
	creates   []map[string]any
	calls     []string
	tarGET    []byte // body served for GET /containers/<id>/archive
	logBody   string // payload served (framed) for /logs
	statMode  os.FileMode
	waitCode  int
	startFail bool
}

func (f *fakeEng) snapshot() ([]map[string]any, []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var cs []map[string]any
	for _, m := range f.creates {
		cs = append(cs, m)
	}
	return cs, append([]string(nil), f.calls...)
}

func (f *fakeEng) handler(w http.ResponseWriter, r *http.Request) {
	p := r.URL.Path
	meth := r.Method
	f.mu.Lock()
	f.calls = append(f.calls, meth+" "+p)
	f.mu.Unlock()
	switch {
	case strings.HasSuffix(p, "/containers/create"):
		var m map[string]any
		json.NewDecoder(r.Body).Decode(&m)
		f.mu.Lock()
		f.creates = append(f.creates, m)
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		io.WriteString(w, `{"Id":"hlctr","Warnings":[]}`)
	case strings.HasSuffix(p, "/start"):
		if f.startFail {
			http.Error(w, `{"message":"start refused"}`, 500)
			return
		}
		w.WriteHeader(200)
		io.WriteString(w, "{}")
	case strings.HasSuffix(p, "/wait"):
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		io.WriteString(w, `{"StatusCode":`+strconv.Itoa(f.waitCode)+`}`)
	case strings.Contains(p, "/logs"):
		w.WriteHeader(200)
		io.Copy(w, bytes.NewBuffer(hlFrame(f.logBody)))
	case strings.Contains(p, "/stats"):
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"CPUStats":{"CPUUsage":{"TotalUsage":1}},"Networks":{},`+
			`"BlkioStats":{"IoServiceBytesRecursive":[]}}`)
	case strings.Contains(p, "/images/") && strings.HasSuffix(p, "/json"):
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"Id":"sha256:deadbeef","Config":{"Image":"img"}}`)
	case strings.Contains(p, "/archive") && meth == http.MethodPut:
		io.Copy(io.Discard, r.Body)
		w.WriteHeader(200)
	case strings.Contains(p, "/archive") && meth == http.MethodHead:
		mode := f.statMode | os.ModeDir
		st, _ := json.Marshal(map[string]any{"name": "x", "size": 4096,
			"mode": uint32(mode), "mtime": "2026-11-25T15:18:00Z", "linkTarget": ""})
		w.Header().Set("X-Docker-Container-Path-Stat",
			base64.StdEncoding.EncodeToString(st))
		w.WriteHeader(200)
	case strings.Contains(p, "/archive") && meth == http.MethodGet:
		// The client decodes the path stat from this header too (output_archive_read
		// checks stat.Mode), so serve a regular-file mode here.
		st, _ := json.Marshal(map[string]any{"name": "result.json", "size": 12,
			"mode": uint32(0o100644), "mtime": "2026-11-25T15:18:00Z", "linkTarget": ""})
		w.Header().Set("X-Docker-Container-Path-Stat",
			base64.StdEncoding.EncodeToString(st))
		w.Header().Set("Content-Type", "application/x-tar")
		w.WriteHeader(200)
		f.mu.Lock()
		body := f.tarGET
		f.mu.Unlock()
		io.Copy(w, bytes.NewBuffer(body))
	case strings.Contains(p, "/containers/hlctr") && meth == http.MethodDelete:
		w.WriteHeader(204)
	case strings.Contains(p, "/volumes/") && meth == http.MethodDelete:
		w.WriteHeader(204)
	default:
		w.WriteHeader(404)
	}
}

// hlFrame multiplexes a payload the way a non-TTY daemon does (8-byte big-
// endian frame header, stream id 1 = stdout), so the stdcopy demux the helpers
// use is exercised rather than a raw-string shortcut.
func hlFrame(payload string) []byte {
	b := []byte(payload)
	// 8-byte header: stream id, 3 zero bytes, 4-byte big-endian length (the
	// old 12-byte header was malformed, so stdcopy silently yielded nothing).
	head := []byte{1, 0, 0, 0,
		byte(len(b) >> 24), byte(len(b) >> 16), byte(len(b) >> 8), byte(len(b))}
	return append(head, b...)
}

func hlServer(t *testing.T, f *fakeEng, store *tunables.Store) *Executor {
	srv := httptest.NewServer(http.HandlerFunc(f.handler))
	t.Cleanup(srv.Close)
	cli, err := client.NewClientWithOpts(
		client.WithHost("tcp://"+strings.TrimPrefix(srv.URL, "http://")),
		client.WithVersion("1.45"))
	if err != nil {
		t.Fatal(err)
	}
	var logs bytes.Buffer
	return &Executor{docker: cli, workerName: "w1", instanceID: "i1",
		network: "mod_modtex-net1", tunables: store,
		log: slog.New(slog.NewTextHandler(&logs, nil))}
}

// hlTar renders a tar with one regular file per (name, body) pair.
func hlTar(entries ...[]string) []byte {
	var b bytes.Buffer
	tw := tar.NewWriter(&b)
	for _, e := range entries {
		hdr := tar.Header{Typeflag: tar.TypeReg, Name: e[0], Mode: 0o644,
			Size: int64(len(e[1]))}
		if err := tw.WriteHeader(&hdr); err != nil {
			panic(err)
		}
		if _, err := tw.Write([]byte(e[1])); err != nil {
			panic(err)
		}
	}
	if err := tw.Close(); err != nil {
		panic(err)
	}
	return b.Bytes()
}

// --- 1. the builders themselves + mutation checks --------------------------
func TestHelperPosture_Builders(t *testing.T) {
	e := &Executor{network: "mod_modtex-net1"}

	type helper struct {
		name    string
		build   func() *container.HostConfig
		needNet bool
		bind    string // expected /data bind form
	}
	helpers := []helper{
		{"result-read", func() *container.HostConfig {
			return e.resultReadHostConfig("vol-9")
		}, false, "vol-9:/data:ro"},
		{"prep", func() *container.HostConfig {
			return e.prepHostConfig("vol-9")
		}, false, "vol-9:/data:rw"},
		{"archive-read", func() *container.HostConfig {
			return e.archiveReadHostConfig("vol-9")
		}, false, "vol-9:/data:ro"},
		{"scrub", func() *container.HostConfig {
			return e.scrubHostConfig()
		}, false, ""},
		{"mc-sidecar", func() *container.HostConfig {
			return e.sidecarHostConfig("vol-9")
		}, true, "vol-9:/data:rw"},
	}
	for _, h := range helpers {
		hc := h.build()
		if gap := postureGap(hc, h.needNet, e.network); gap != "" {
			t.Fatalf("helper %q: %s", h.name, gap)
		}
		if h.bind != "" && (hc.Binds == nil || hc.Binds[0] != h.bind) {
			t.Fatalf("helper %q: bind must be %q, got %v", h.name, h.bind, hc.Binds)
		}
	}
	// The read helper additionally keeps a read-only rootfs + /tmp tmpfs;
	// the mc sidecar keeps the /root/.mc tmpfs gpu5 proved it needs.
	if gap := postureGap(e.resultReadHostConfig("v"), false, e.network); gap != "" {
		t.Fatalf("result-read: %s", gap)
	}
	if e.resultReadHostConfig("v").ReadonlyRootfs != true ||
		e.resultReadHostConfig("v").Tmpfs["/tmp"] == "" {
		t.Fatalf("result-read must be read-only rootfs + /tmp tmpfs")
	}
	if e.sidecarHostConfig("v").Tmpfs["/root/.mc"] == "" {
		t.Fatalf("mc sidecar needs the /root/.mc tmpfs under a read-only rootfs")
	}
	if e.sidecarHostConfig("v").AutoRemove {
		t.Fatalf("sidecar AutoRemove must stay false (logs are read before remove)")
	}
	// The scrub helper must NOT be privileged: GPU access is the DeviceRequest
	// passthrough the caller sets, never a privileged flag.
	if e.scrubHostConfig().Privileged {
		t.Fatalf("scrub helper must never be privileged")
	}

	// Mutation checks: strip ONE guard, the checker must report it.
	base := e.resultReadHostConfig("vol-9")
	mutants := []struct {
		name  string
		build func() *container.HostConfig
		what string
	}{
		{"network", func() *container.HostConfig {
			c := *base
			c.NetworkMode = container.NetworkMode("mod_modtex-net1")
			return &c
		}, "NetworkMode none"},
		{"capdrop", func() *container.HostConfig {
			c := *base
			c.CapDrop = []string{}
			return &c
		}, "CapDrop ALL"},
		{"noprivs", func() *container.HostConfig {
			c := *base
			c.SecurityOpt = []string{}
			return &c
		}, "no-new-privileges:true"},
		{"limits", func() *container.HostConfig {
			c := *base
			c.Resources = container.Resources{}
			return &c
		}, "memory+pids limits"},
		{"privileged", func() *container.HostConfig {
			c := *base
			c.Privileged = true
			return &c
		}, "not privileged"},
		{"sidecar-net", func() *container.HostConfig {
			c := *e.sidecarHostConfig("vol-9")
			c.NetworkMode = container.NetworkMode("none")
			return &c
		}, "sidecar keeps the task network"},
		{"sidecar-mc-tmpfs", func() *container.HostConfig {
			c := *e.sidecarHostConfig("vol-9")
			c.Tmpfs = map[string]string{}
			return &c
		}, "mc config tmpfs"},
	}
	// Each mutant is judged by the guard it strips: the two sidecar mutants
	// break sidecar-specific guards (the task network, the mc config tmpfs),
	// so they are checked against the sidecar posture, not the read-helper
	// one. Every other mutant strips a guard postureGap checks for all helpers.
	for _, m := range mutants {
		switch m.name {
		case "sidecar-net":
			if postureGap(m.build(), true, e.network) == "" {
				t.Fatalf("mutation check %q: removing %s was NOT detected", m.name, m.what)
			}
		case "sidecar-mc-tmpfs":
			if m.build().Tmpfs["/root/.mc"] != "" {
				t.Fatalf("mutation check %q: removing %s was NOT detected", m.name, m.what)
			}
		default:
			if postureGap(m.build(), false, e.network) == "" {
				t.Fatalf("mutation check %q: removing %s was NOT detected", m.name, m.what)
			}
		}
		// The real helper must always pass its own guard, or the mutant is
		// meaningless.
		switch m.name {
		case "sidecar-net":
			if postureGap(e.sidecarHostConfig("vol-9"), true, e.network) != "" {
				t.Fatalf("mutation check %q: the real sidecar fails its own check", m.name)
			}
		case "sidecar-mc-tmpfs":
			if e.sidecarHostConfig("vol-9").Tmpfs["/root/.mc"] == "" {
				t.Fatalf("mutation check %q: the real sidecar fails its own check", m.name)
			}
		default:
			if postureGap(e.resultReadHostConfig("vol-9"), false, e.network) != "" {
				t.Fatalf("mutation check %q: the real helper fails its own check", m.name)
			}
		}
	}
}

// --- FIX 1: the 0600-read capability on the two file-READING helpers -------
// CapDrop ALL also drops CAP_DAC_OVERRIDE, which the pre-lockdown containers
// relied on to read output files a NON-ROOT tenant container wrote mode 0600
// (the closer's reproduction: `sha256sum: Permission denied`, rc 1, 0 uploaded
// for the mc sidecar; `cat: Permission denied` for the read helper). The fix
// adds exactly ONE capability, DAC_READ_SEARCH (read-only file access), to
// the mc upload sidecar and the result-JSON read helper, and to NO other
// helper: the never-started prep/archive twins and the GPU scrub never read
// another uid's private files, so they must stay CapAdd-free.
func TestHelperPosture_ReadCapAdd(t *testing.T) {
	e := &Executor{network: "mod_modtex-net1"}
	const want = "DAC_READ_SEARCH"
	type readHelper struct {
		name string
		hc   *container.HostConfig
	}
	for _, h := range []readHelper{
		{"result-read", e.resultReadHostConfig("vol-9")},
		{"mc-sidecar", e.sidecarHostConfig("vol-9")}} {
		if !hasOne(h.hc.CapAdd, want) {
			t.Fatalf("helper %q: CapAdd must contain %s (0600 outputs from a "+
				"non-root tenant container are unreadable without it)", h.name, want)
		}
		// The minimal capability only: no DAC_OVERRIDE (which would also grant
		// WRITE), no catch-all.
		for _, bad := range []string{"DAC_OVERRIDE", "ALL", "SYS_ADMIN",
			"NET_RAW", "CHOWN", "FSETID"} {
			if hasOne(h.hc.CapAdd, bad) {
				t.Fatalf("helper %q: CapAdd must NOT contain %s (minimal grant only)",
					h.name, bad)
			}
		}
		// CapDrop ALL still stands next to the single CapAdd (the docker
		// daemon applies drop-then-add; the lockdown is not being walked back).
		if !hasOne(h.hc.CapDrop, "ALL") {
			t.Fatalf("helper %q: CapDrop ALL must stay", h.name)
		}
	}
	// The helpers that never read foreign-uid private files stay CapAdd-free.
	for _, h := range []readHelper{
		{"prep", e.prepHostConfig("vol-9")},
		{"archive-read", e.archiveReadHostConfig("vol-9")},
		{"gpu-scrub", e.scrubHostConfig()}} {
		if len(h.hc.CapAdd) != 0 {
			t.Fatalf("helper %q: must carry NO CapAdd (never reads another uid's "+
				"private files), got %v", h.name, h.hc.CapAdd)
		}
	}

	// Mutation checks: drop the CapAdd, or widen it, and the guard must fire.
	if capAddGap(e.resultReadHostConfig("v"), true) != "" {
		t.Fatalf("real result-read fails its own CapAdd guard")
	}
	if capAddGap(e.sidecarHostConfig("v"), true) != "" {
		t.Fatalf("real sidecar fails its own CapAdd guard")
	}
	mutNoCap := *e.sidecarHostConfig("v")
	mutNoCap.CapAdd = []string{}
	if capAddGap(&mutNoCap, true) == "" {
		t.Fatalf("mutation check: removing the sidecar CapAdd was NOT detected")
	}
	mutWide := *e.resultReadHostConfig("v")
	mutWide.CapAdd = []string{"DAC_READ_SEARCH", "DAC_OVERRIDE"}
	if capAddGap(&mutWide, true) == "" {
		t.Fatalf("mutation check: widening CapAdd past DAC_READ_SEARCH was NOT detected")
	}
	mutPrep := *e.prepHostConfig("v")
	mutPrep.CapAdd = []string{"DAC_READ_SEARCH"}
	if capAddGap(&mutPrep, false) == "" {
		t.Fatalf("mutation check: adding CapAdd to a never-started helper was NOT detected")
	}
}

// capAddGap is the CapAdd half of the posture checker, for the two file-READING
// helpers (wantAdd = true) and the never-started / GPU helpers (wantAdd = false).
func capAddGap(h *container.HostConfig, wantAdd bool) string {
	if h == nil {
		return "no HostConfig"
	}
	if wantAdd {
		if !hasOne(h.CapAdd, "DAC_READ_SEARCH") {
			return "CapAdd must contain DAC_READ_SEARCH"
		}
		for _, c := range h.CapAdd {
			if c != "DAC_READ_SEARCH" {
				return "CapAdd must be exactly DAC_READ_SEARCH, got extra " + c
			}
		}
		return ""
	}
	if len(h.CapAdd) != 0 {
		return "CapAdd must be empty for this helper"
	}
	return ""
}

// --- 2. the REAL create paths: the daemon receives the locked HostConfig ----
// Each helper is driven through its production entry point against the fake
// Engine; the assertion runs on the create body the docker client actually
// sent (jsonGap), so a call-site change that bypasses the builders fails.

func TestHelperPosture_ResultReadCreate(t *testing.T) {
	f := &fakeEng{logBody: "irrelevant"}
	e := hlServer(t, f, nil)
	got := e.readResultJSONFiles(context.Background(), "vol-rr", "alpine:3.20",
		resultFileReadNames)
	cs, _ := f.snapshot()
	if len(cs) != 1 {
		t.Fatalf("expected one create, got %d", len(cs))
	}
	if gap := jsonGap(cs[0], false, e.network); gap != "" {
		t.Fatalf("result-JSON read create: %s", gap)
	}
	hc := asMap(cs[0]["HostConfig"])
	if asStrings(hc["Binds"])[0] != "vol-rr:/data:ro" {
		t.Fatalf("read helper must keep the ro /data bind: %v", asStrings(hc["Binds"]))
	}
	if asMap(hc["Tmpfs"])["/tmp"] == nil {
		t.Fatalf("read helper needs the /tmp tmpfs under a read-only rootfs")
	}
	if hc["ReadonlyRootfs"] != true {
		t.Fatalf("read helper rootfs must be read-only")
	}
	// FIX 1 on the WIRE: the daemon must receive the DAC_READ_SEARCH CapAdd,
	// or a 0600 output file from a non-root tenant container is unreadable.
	if !hasOne(asStrings(hc["CapAdd"]), "DAC_READ_SEARCH") {
		t.Fatalf("result-read create must carry CapAdd DAC_READ_SEARCH: %v",
			asStrings(hc["CapAdd"]))
	}
	if strings.Join(asStrings(hc["CapAdd"]), ",") != "DAC_READ_SEARCH" {
		t.Fatalf("result-read CapAdd must be exactly DAC_READ_SEARCH, got %v",
			asStrings(hc["CapAdd"]))
	}
	// The never-fail contract still holds: every requested name is present.
	for _, n := range resultFileReadNames {
		if _, ok := got[n]; !ok {
			t.Fatalf("read helper must always return every requested name (%s missing)", n)
		}
	}
}

func TestHelperPosture_SidecarCreate(t *testing.T) {
	f := &fakeEng{logBody: "Added myminio successfully."}
	e := hlServer(t, f, nil)
	if _, err := e.runSidecarWithOutput(context.Background(), "minio-client:1",
		"vol-sc", "mc ls myminio/x", transport.MinIOCredentials{}); err != nil {
		t.Fatalf("sidecar run under the locked posture must succeed: %v", err)
	}
	cs, _ := f.snapshot()
	if len(cs) != 1 {
		t.Fatalf("expected one create, got %d", len(cs))
	}
	// The mc sidecar is the ONE helper that KEEPS the network (mc <-> MinIO).
	if gap := jsonGap(cs[0], true, e.network); gap != "" {
		t.Fatalf("mc sidecar create: %s", gap)
	}
	hc := asMap(cs[0]["HostConfig"])
	if asStrings(hc["Binds"])[0] != "vol-sc:/data:rw" {
		t.Fatalf("sidecar must keep the rw /data bind: %v", asStrings(hc["Binds"]))
	}
	if asMap(hc["Tmpfs"])["/root/.mc"] == nil {
		t.Fatalf("sidecar needs the /root/.mc config tmpfs gpu5 proved mc requires")
	}
	if hc["ReadonlyRootfs"] != true {
		t.Fatalf("sidecar rootfs must be read-only (gpu5-proven with the tmpfs)")
	}
	// FIX 1 on the WIRE: the upload sidecar must carry exactly DAC_READ_SEARCH,
	// or a 0600 output file written by a non-root tenant container fails the
	// whole task's upload (closer's repro: sha256sum Permission denied, rc 1).
	if strings.Join(asStrings(hc["CapAdd"]), ",") != "DAC_READ_SEARCH" {
		t.Fatalf("sidecar create must carry CapAdd exactly [DAC_READ_SEARCH], got %v",
			asStrings(hc["CapAdd"]))
	}
	// Per-task creds env handling is unchanged: the create still carries the
	// MC_HOST/MINIO_* env keys (values come from the caller's creds).
	env := asStrings(cs[0]["Env"])
	for _, want := range []string{"MC_HOST_myminio=", "MINIO_ENDPOINT=", "MINIO_ACCESS=", "MINIO_SECRET="} {
		found := false
		for _, kv := range env {
			if strings.HasPrefix(kv, want) {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("sidecar env must keep %q (per-task creds unchanged): %v", want, env)
		}
	}
	// sidecarLabels unchanged on the create.
	lb := asMap(cs[0]["Labels"])
	if lb[sidecarLabel] != "1" || lb[sidecarWorkerLabel] != "w1" {
		t.Fatalf("sidecar labels missing: %v", lb)
	}
}

func TestHelperPosture_PrepCreate(t *testing.T) {
	f := &fakeEng{statMode: 0o777}
	e := hlServer(t, f, nil)
	if _, err := e.prepareDataVolumeAPI(context.Background(), "alpine:3.20", "vol-p"); err != nil {
		t.Fatalf("prep under the locked posture must succeed: %v", err)
	}
	cs, _ := f.snapshot()
	if len(cs) != 1 {
		t.Fatalf("expected one create, got %d", len(cs))
	}
	// Created-never-started: network none (a container that never runs must
	// never touch a network) + cap drop + limits.
	if gap := jsonGap(cs[0], false, e.network); gap != "" {
		t.Fatalf("volume-prep create: %s", gap)
	}
	hc := asMap(cs[0]["HostConfig"])
	if asStrings(hc["Binds"])[0] != "vol-p:/data:rw" {
		t.Fatalf("prep must keep the rw bind the archive PUT extracts into: %v",
			asStrings(hc["Binds"]))
	}
	// No /start was ever issued (the seed is one archive PUT).
	_, calls := f.snapshot()
	for _, c := range calls {
		if strings.HasSuffix(c, "/start") {
			t.Fatalf("the prep container must never be started: %v", calls)
		}
	}
}

func TestHelperPosture_ArchiveReadCreates(t *testing.T) {
	// Both created-never-started archive-read twins.
	f := &fakeEng{tarGET: hlTar([]string{"export_result.json", `{"outputs":[]}`})}
	e := hlServer(t, f, nil)
	if _, err := e.ceReadOutputTarViaArchive(context.Background(), "vol-ce", "alpine:3.20"); err != nil {
		t.Fatalf("ce archive read under the locked posture must succeed: %v", err)
	}
	f2 := &fakeEng{tarGET: hlTar([]string{"result.json", `{"usage":{}}`}), statMode: 0o100644}
	e2 := hlServer(t, f2, nil)
	if _, err := e2.readResultFilesViaArchive(context.Background(), "vol-out", "alpine:3.20",
		[]string{"result.json"}); err != nil {
		t.Fatalf("output archive read under the locked posture must succeed: %v", err)
	}
	for _, pair := range []struct {
		name string
		f    *fakeEng
	}{{"ce_archive_read", f}, {"output_archive_read", f2}} {
		cs, _ := pair.f.snapshot()
		if len(cs) != 1 {
			t.Fatalf("%s: expected one create, got %d", pair.name, len(cs))
		}
		if gap := jsonGap(cs[0], false, e.network); gap != "" {
			t.Fatalf("%s create: %s", pair.name, gap)
		}
		hc := asMap(cs[0]["HostConfig"])
		if asStrings(hc["Binds"])[0] == "" || !strings.HasSuffix(asStrings(hc["Binds"])[0], ":/data:ro") {
			t.Fatalf("%s: archive read must keep the ro /data bind: %v", pair.name, asStrings(hc["Binds"]))
		}
		// Config-level NetworkDisabled stays (belt-and-braces with HostConfig
		// NetworkMode none).
		if cs[0]["NetworkDisabled"] != true && asMap(cs[0]["Config"])["NetworkDisabled"] != true {
			t.Fatalf("%s: Config.NetworkDisabled must stay true", pair.name)
		}
	}
}

func TestHelperPosture_ScrubCreate(t *testing.T) {
	// prefer-local ON so the public pull is skipped (fake serves the inspect);
	// the scrub then reaches ContainerCreate with the locked posture.
	store := tunables.New()
	store.Apply(map[string]interface{}{
		tenantScrubImageTunableKey: "img:1",
		preferLocalImagesTunableKey: true,
	})
	f := &fakeEng{logBody: "12345", waitCode: 0}
	e := hlServer(t, f, store)
	if _, _, err := e.launchScrubHelper(context.Background(), []string{"GPU-abc"}); err != nil {
		t.Fatalf("scrub helper under the locked posture must succeed: %v", err)
	}
	cs, _ := f.snapshot()
	if len(cs) != 1 {
		t.Fatalf("expected one create, got %d", len(cs))
	}
	// No network, caps dropped, no-new-privileges, limits, NOT privileged.
	if gap := jsonGap(cs[0], false, e.network); gap != "" {
		t.Fatalf("gpu scrub create: %s", gap)
	}
	// The GPU passthrough (DeviceRequests) MUST survive the lockdown: that is
	// how the helper reaches the card, and it is not a capability. DeviceRequests
	// lives in the embedded Resources struct, so it is flattened at the
	// HostConfig level on the wire.
	dr := asMap(cs[0]["HostConfig"])["DeviceRequests"]
	if dr == nil {
		t.Fatalf("scrub create must keep the DeviceRequests GPU passthrough: %v",
			cs[0]["HostConfig"])
	}
	if asList1(dr)["Driver"] != "nvidia" {
		t.Fatalf("scrub DeviceRequest driver must stay nvidia: %v", dr)
	}
}

// asList1 indexes a JSON array element as a map (for the DeviceRequests check).
func asList1(v any) map[string]any {
	if l, ok := v.([]any); ok && len(l) > 0 {
		if m, ok := l[0].(map[string]any); ok {
			return m
		}
	}
	return map[string]any{}
}

// --- FIX 2: the `--force` flag on BOTH staging-cleanup commands -------------
// The HELPER-CONTAINERS-LOCKDOWN closer found a pre-existing bug the lockdown
// did not cause: the current minio-client refuses a recursive prefix removal
// without --force ("Removal requires --force flag") and deletes NOTHING, and
// the `|| true` hid the rc=1, so scan-staging/{task_id}/ prefixes were never
// removed. Proven on cpu4 against a throwaway versitygw (hlfix-*): the OLD
// text leaves both task prefixes intact (rc 1, hidden), the NEW text removes
// exactly the named prefix and never the bucket root or a sibling task's
// prefix. Both cleanup sites must carry --force, and the best-effort `|| true`
// plus the fold's exit-status re-exit must stay.
func TestStagingCleanupCarriesForceFlag(t *testing.T) {
	const want = "mc rm -r --force myminio/tenant-7-files/scan-staging/9101/ || true"

	// (1) the standalone cleanup helper's command (cleanupStagedOutputs in
	// executor.go renders this through stagingCleanupCmd).
	standalone := stagingCleanupCmd(stagingUploadParamsFromTask(
		&transport.Task{ID: 9101, Payload: transport.TaskPayload{
			MinioBucket: "tenant-7-files"}}))
	if standalone != want {
		t.Fatalf("standalone cleanup command = %q, want %q", standalone, want)
	}

	// (2) the folded cleanup inside the durable upload script.
	folded := durableUploadParamsFromTask(&transport.Task{ID: 9101,
		Payload: transport.TaskPayload{MinioBucket: "tenant-7-files"}})
	suffix := stagingCleanupFoldSuffix(folded)
	if !strings.Contains(suffix, want) {
		t.Fatalf("folded cleanup must carry the same --force command, got %q", suffix)
	}

	// (3) the checkpoint-export mirror (cleanupCheckpointExportStaging) must
	// carry it too: asserted on the argv the `docker run` child would get.
	var rmArgv []string
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if strings.Contains(strings.Join(arg, " "), "mc rm") {
			rmArgv = arg
		}
		return exec.CommandContext(ctx, "true")
	})
	defer restore()
	e := &Executor{network: "mod_modtex-net1"}
	if err := e.cleanupCheckpointExportStaging(context.Background(), "minio-client:1",
		&transport.Credentials{MinIO: transport.MinIOCredentials{}},
		&transport.Task{ID: 9101, Payload: transport.TaskPayload{
			MinioBucket: "tenant-7-files"}}); err != nil {
		t.Fatalf("checkpoint staging cleanup must stay best-effort, got %v", err)
	}
	if len(rmArgv) == 0 {
		t.Fatalf("no mc rm invocation reached the docker CLI for the checkpoint mirror")
	}
	if !strings.Contains(strings.Join(rmArgv, " "), want) {
		t.Fatalf("checkpoint cleanup argv must carry the --force command, got %v", rmArgv)
	}

	// Mutation checks: each cleanup site without --force must be caught by the
	// assertions above (the shared renderer is the only place that adds it, so
	// a revert of either site fails its own test).
	if !strings.Contains(mcRemovePrefixCmd("b", "p/"), "mc rm -r --force myminio/b/p/") {
		t.Fatalf("shared renderer lost --force or the prefix scoping")
	}
	// The prefix scoping the cpu4 proof showed: the command always names a
	// task's own prefix, never the bucket root.
	if mcRemovePrefixCmd("tenant-7-files", "scan-staging/9101/") ==
		"mc rm -r --force myminio/tenant-7-files/ || true" {
		t.Fatalf("cleanup must never target the bucket root")
	}
}
