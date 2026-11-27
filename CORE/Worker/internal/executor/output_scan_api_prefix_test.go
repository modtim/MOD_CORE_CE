package executor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// OUTPUT-SCAN-CHECK-404-SILENT-NOOP: the API is single-mounted under /v1, but
// e.apiURL is the BARE cfg.APIURL. The consult used to POST
// {bare}/workers/tasks/N/output-scan-check, which the API answers 404 on every
// call, so the pre-store scan was a silent no-op. These tests pin the path the
// worker really sends (with the /v1 prefix, exactly once) and that the
// 200/404/5xx handling is unchanged.

type scanPathRecorder struct {
	mu    sync.Mutex
	paths []string
}

func (r *scanPathRecorder) add(p string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.paths = append(r.paths, p)
}

func (r *scanPathRecorder) all() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.paths...)
}

// strictV1Server mimics the real API: only /v1/workers/tasks/N/<tail> exists;
// the unprefixed path 404s. Any other status is forced via status/body.
func strictV1Server(t *testing.T, rec *scanPathRecorder, tail string, status int, body string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.add(r.URL.Path)
		if !strings.HasPrefix(r.URL.Path, "/v1/workers/tasks/") || !strings.HasSuffix(r.URL.Path, tail) {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func scanDirect(t *testing.T) {
	t.Helper()
	old := outputScanHTTPDo
	outputScanHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
		return http.DefaultClient.Do(req)
	}
	t.Cleanup(func() { outputScanHTTPDo = old })
}

func TestWorkerAPIBaseAddsPrefixOnce(t *testing.T) {
	cases := map[string]string{
		"":                      "",
		"  ":                    "",
		"http://api":            "http://api/v1",
		"http://api/":           "http://api/v1",
		"http://api/v1":         "http://api/v1",
		"https://h.example/v1/": "https://h.example/v1",
		"http://api-host:8000":  "http://api-host:8000/v1",
	}
	for in, want := range cases {
		if got := workerAPIBase(in); got != want {
			t.Errorf("workerAPIBase(%q)=%q want %q", in, got, want)
		}
	}
}

func TestScanConsultPostsToV1PathForBareAndPrefixedURL(t *testing.T) {
	scanDirect(t)
	for _, suffix := range []string{"", "/v1"} {
		rec := &scanPathRecorder{}
		srv := strictV1Server(t, rec, "/output-scan-check", 200,
			`{"mode":"enforce","blocked":true,"findings":[{"category":"secret"}]}`)
		e := exportScanExecutor(srv.URL+suffix, "enforce")
		blocked, reason := e.scanOutputBeforeStore(context.Background(),
			exportScanTask(77), nil, false)
		if got := rec.all(); len(got) != 1 || got[0] != "/v1/workers/tasks/77/output-scan-check" {
			t.Fatalf("suffix %q: path=%v want exactly /v1/workers/tasks/77/output-scan-check", suffix, got)
		}
		if !blocked || !strings.HasPrefix(reason, outputScanBlockedReason) {
			t.Errorf("suffix %q: real 200 blocked verdict must refuse: blocked=%v reason=%q", suffix, blocked, reason)
		}
	}
}

func TestScanConsultStatusHandlingUnchanged(t *testing.T) {
	scanDirect(t)
	type tc struct {
		name, mode string
		status     int
		body       string
		wantBlock  bool
	}
	for _, c := range []tc{
		{"200 shadow allow", "shadow", 200, `{"mode":"shadow","blocked":false,"findings":[]}`, false},
		{"200 enforce allow", "enforce", 200, `{"mode":"enforce","blocked":false,"findings":[]}`, false},
		{"200 enforce block", "enforce", 200, `{"mode":"enforce","blocked":true,"findings":[]}`, true},
		{"404 shadow fail-open", "shadow", 404, ``, false},
		{"404 enforce fail-closed", "enforce", 404, ``, true},
		{"500 shadow fail-open", "shadow", 500, ``, false},
		{"503 enforce fail-closed", "enforce", 503, ``, true},
	} {
		rec := &scanPathRecorder{}
		srv := strictV1Server(t, rec, "/output-scan-check", c.status, c.body)
		e := exportScanExecutor(srv.URL, c.mode)
		blocked, _ := e.scanOutputBeforeStore(context.Background(), exportScanTask(5), []transport.OutputFile{}, true)
		if blocked != c.wantBlock {
			t.Errorf("%s: blocked=%v want %v", c.name, blocked, c.wantBlock)
		}
		if got := rec.all(); len(got) != 1 || got[0] != "/v1/workers/tasks/5/output-scan-check" {
			t.Errorf("%s: path=%v", c.name, got)
		}
	}
}

func TestScanConsultOffMakesNoCall(t *testing.T) {
	scanDirect(t)
	rec := &scanPathRecorder{}
	srv := strictV1Server(t, rec, "/output-scan-check", 200, `{}`)
	e := exportScanExecutor(srv.URL, "off")
	if blocked, _ := e.scanOutputBeforeStore(context.Background(), exportScanTask(5), nil, false); blocked {
		t.Fatal("off must allow")
	}
	if got := rec.all(); len(got) != 0 {
		t.Fatalf("off must make zero calls, got %v", got)
	}
}

func TestCheckpointExportReportPostsToV1Path(t *testing.T) {
	scanDirect(t)
	rec := &scanPathRecorder{}
	srv := strictV1Server(t, rec, "/checkpoint-export-uploaded", 200, `{"status":"uploaded"}`)
	e := exportScanExecutor(srv.URL, "shadow")
	e.reportCheckpointExportUploaded(context.Background(), exportScanTask(88), 123)
	if got := rec.all(); len(got) != 1 || got[0] != "/v1/workers/tasks/88/checkpoint-export-uploaded" {
		t.Fatalf("report path=%v want /v1/workers/tasks/88/checkpoint-export-uploaded", got)
	}
}
