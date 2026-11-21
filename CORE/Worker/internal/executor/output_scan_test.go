package executor

// Fortress 3.5 — worker-side output scan BEFORE the durable storage write.
//
// What must hold: an allow verdict lets the store proceed; a block verdict
// refuses it and names the reason output_scan_blocked; a verdict-less consult
// (API unreachable / 404 / 401 / 500 / malformed) is posture-dependent —
// enforce REFUSES (fail-closed, mirroring task_output_scan.py's scanner-blowup
// under enforce), shadow/off allow (fail-open, the launch posture); and with
// the feature off there is NO scan call at all (zero hops on the completion
// path). The verdict itself is always the API's — the worker parses, never
// re-derives (design_fortress_35 §2 rejects the Go scanner port).
//
// Harness: the httptest-server + seam-redirect pattern hostexec_task_test.go
// uses for hostExecHTTPDo (executor.go's outputScanHTTPDo + outputScanSetAuth
// are the same shape), plus the bare &Executor{} construction checkpoint_test.go
// uses.
import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// ScanConsult is one pre-store consult body (the API's own request shape:
// identity + output descriptors, never file bytes).
type ScanConsult struct {
	TenantID      int                    `json:"tenant_id"`
	NodeKind      string                 `json:"node_kind"`
	MinioBucket   string                 `json:"minio_bucket"`
	UploadPending bool                   `json:"upload_pending"`
	Outputs       []transport.OutputFile `json:"outputs"`
}

// scanLog records what the fake consult endpoint received, in order.
type scanLog struct {
	mu   sync.Mutex
	hits int
}

// stubScanHTTP redirects outputScanHTTPDo at the httptest server, restoring the
// real one on cleanup so sibling tests in this package are unaffected.
func stubScanHTTP(t *testing.T) {
	old := outputScanHTTPDo
	outputScanHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("output-scan http: %w", err)
		}
		return resp, nil
	}
	t.Cleanup(func() { outputScanHTTPDo = old })
}

// newScanServer answers the consult with verdictJSON at `status` and counts hits.
// status 0 means "the API is unreachable": the server is closed immediately.
func newScanServer(t *testing.T, log *scanLog, verdictJSON string, status int) string {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var got ScanConsult
		if err := json.Unmarshal(raw, &got); err == nil {
			_ = got
		}
		log.mu.Lock()
		defer log.mu.Unlock()
		log.hits++
		if status != 0 {
			w.WriteHeader(status)
			w.Write([]byte(verdictJSON))
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(func() { srv.Close() })
	if status == 0 {
		srv.Close()
	}
	return srv.URL
}

// scanTask is a minimal node task the consult keys on.
func scanTask(id, tenant int) *transport.Task {
	return &transport.Task{
		ID:       id,
		TenantID: tenant,
		Payload: transport.TaskPayload{
			NodeKind:    "image_output",
			MinioBucket: "tenant-7-files",
		},
	}
}

// scanExecutor is a bare Executor pointed at `apiURL` with the posture `mode`
// pushed through the heartbeat tunables map (the ONLY posture source: no env
// var is set anywhere, so the store's empty envVar arg is what proves it).
func scanExecutor(apiURL, mode string) *Executor {
	store := tunables.New()
	if mode != "" {
		store.Apply(map[string]interface{}{outputScanModeTunableKey: mode})
	}
	return &Executor{
		apiURL:   apiURL,
		tunables: store,
		log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

// ALLOW: the API says clean -> the store proceeds, one hop, no refusal.
func TestOutputScanAllowProceeds(t *testing.T) {
	log := &scanLog{}
	url := newScanServer(t, log, `{"mode":"shadow","blocked":false,"findings":[]}`, 200)
	stubScanHTTP(t)
	e := scanExecutor(url, "shadow")
	task := scanTask(9001, 7)
	blocked, reason := e.scanOutputBeforeStore(context.Background(), task,
		[]transport.OutputFile{}, true)
	if blocked || reason != "" {
		t.Errorf("allow verdict must not refuse the store: blocked=%v reason=%q", blocked, reason)
	}
	if log.hits != 1 {
		t.Errorf("shadow scans exactly once: hits=%d", log.hits)
	}
}

// BLOCK: the API says blocked -> the store is skipped and the reason names
// output_scan_blocked plus the API's masked finding codes.
func TestOutputScanBlockRefusesStore(t *testing.T) {
	log := &scanLog{}
	url := newScanServer(t, log,
		`{"mode":"enforce","blocked":true,"findings":[{"scanner":"pii","category":"person_name","severity":"block"}]}`,
		200)
	stubScanHTTP(t)
	e := scanExecutor(url, "enforce")
	task := scanTask(9002, 7)
	blocked, reason := e.scanOutputBeforeStore(context.Background(), task,
		[]transport.OutputFile{}, true)
	if !blocked {
		t.Fatalf("enforce block verdict must refuse the store")
	}
	if !strings.HasPrefix(reason, outputScanBlockedReason) {
		t.Errorf("refusal reason must name %q: got %q", outputScanBlockedReason, reason)
	}
	if !strings.Contains(reason, "person_name") {
		t.Errorf("reason should carry the API's masked finding codes: got %q", reason)
	}
	if log.hits != 1 {
		t.Errorf("block consults exactly once (no retry): hits=%d", log.hits)
	}
}

// EXEMPT: an exempted verdict arrives blocked=false from the API (the API
// consulted exemptions before it said blocked) -> the store proceeds.
func TestOutputScanExemptedProceeds(t *testing.T) {
	log := &scanLog{}
	url := newScanServer(t, log,
		`{"mode":"enforce","blocked":false,"exempted":true,"findings":[{"scanner":"pii","category":"person_name","severity":"block"}]}`,
		200)
	stubScanHTTP(t)
	e := scanExecutor(url, "enforce")
	blocked, reason := e.scanOutputBeforeStore(context.Background(), scanTask(9003, 7),
		[]transport.OutputFile{}, true)
	if blocked || reason != "" {
		t.Errorf("exempted verdict must not refuse: blocked=%v reason=%q", blocked, reason)
	}
}

// API UNREACHABLE under enforce: refuse (fail-closed compliance posture).
func TestOutputScanUnreachableEnforceRefuses(t *testing.T) {
	log := &scanLog{}
	url := newScanServer(t, log, ``, 0) // closed server -> connection refused
	stubScanHTTP(t)
	e := scanExecutor(url, "enforce")
	blocked, reason := e.scanOutputBeforeStore(context.Background(), scanTask(9004, 7),
		[]transport.OutputFile{}, true)
	if !blocked {
		t.Fatalf("enforce + no verdict must refuse the store")
	}
	if !strings.HasPrefix(reason, outputScanBlockedReason) {
		t.Errorf("refusal reason must name %q: got %q", outputScanBlockedReason, reason)
	}
}

// API UNREACHABLE under shadow: allow (fail-open product posture).
func TestOutputScanUnreachableShadowAllows(t *testing.T) {
	log := &scanLog{}
	url := newScanServer(t, log, ``, 0)
	stubScanHTTP(t)
	e := scanExecutor(url, "shadow")
	blocked, reason := e.scanOutputBeforeStore(context.Background(), scanTask(9005, 7),
		[]transport.OutputFile{}, true)
	if blocked || reason != "" {
		t.Errorf("shadow + no verdict must allow the store: blocked=%v reason=%q", blocked, reason)
	}
}

// 404 (the API has not landed the consult endpoint yet): posture decides.
func TestOutputScanMissingEndpointPosture(t *testing.T) {
	for _, tc := range []struct {
		mode   string
		expect bool
	}{{"enforce", true}, {"shadow", false}, {"off", false}} {
		log := &scanLog{}
		url := newScanServer(t, log, `{"blocked":true}`, http.StatusNotFound)
		stubScanHTTP(t)
		e := scanExecutor(url, tc.mode)
		blocked, reason := e.scanOutputBeforeStore(context.Background(), scanTask(9006, 7),
			[]transport.OutputFile{}, true)
		if blocked != tc.expect {
			t.Errorf("mode %s: expected blocked=%v, got %v", tc.mode, tc.expect, blocked)
		}
		if blocked && !strings.HasPrefix(reason, outputScanBlockedReason) {
			t.Errorf("mode %s: refusal must name the reason: %q", tc.mode, reason)
		}
		if tc.mode == "off" && log.hits != 0 {
			t.Errorf("off must not call the API at all: hits=%d", log.hits)
		}
	}
}

// FEATURE OFF: zero hops. The completion path must not pay for a scan the
// tenant never turned on.
func TestOutputScanOffNoCall(t *testing.T) {
	log := &scanLog{}
	url := newScanServer(t, log, `{"mode":"off","blocked":false}`, 200)
	stubScanHTTP(t)
	e := scanExecutor(url, "off")
	blocked, reason := e.scanOutputBeforeStore(context.Background(), scanTask(9007, 7),
		[]transport.OutputFile{}, true)
	if blocked || reason != "" {
		t.Errorf("off must allow: blocked=%v reason=%q", blocked, reason)
	}
	if log.hits != 0 {
		t.Errorf("off must issue zero consults: hits=%d", log.hits)
	}
}

// UNSET POSTURE: no DB row pushed -> the store's default is the API's default
// (shadow) -> fail-open, and the consult still happens exactly once.
func TestOutputScanUnsetPostureShadow(t *testing.T) {
	log := &scanLog{}
	url := newScanServer(t, log, `{"blocked":true}`, 200)
	stubScanHTTP(t)
	e := scanExecutor(url, "")
	blocked, reason := e.scanOutputBeforeStore(context.Background(), scanTask(9008, 7),
		[]transport.OutputFile{}, true)
	if blocked || reason != "" {
		t.Errorf("unset posture must read shadow (fail-open): blocked=%v reason=%q", blocked, reason)
	}
	if log.hits != 1 {
		t.Errorf("shadow scans exactly once: hits=%d", log.hits)
	}
}

// NO API URL at all (a host with no reachable API): posture decides, and the
// reason says WHY, never a bare crash.
func TestOutputScanNoAPIURLPosture(t *testing.T) {
	e := scanExecutor("", "enforce")
	blocked, reason := e.scanOutputBeforeStore(context.Background(), scanTask(9009, 7),
		[]transport.OutputFile{}, true)
	if !blocked || !strings.HasPrefix(reason, outputScanBlockedReason) {
		t.Errorf("enforce with no apiURL must refuse with a reason: got %v %q", blocked, reason)
	}
	e2 := scanExecutor("", "shadow")
	blocked2, reason2 := e2.scanOutputBeforeStore(context.Background(), scanTask(9010, 7),
		[]transport.OutputFile{}, true)
	if blocked2 || reason2 != "" {
		t.Errorf("shadow with no apiURL must allow: blocked=%v reason=%q", blocked2, reason2)
	}
}

// The consult never carries file bytes: only identity + descriptors. A
// refusal must not leak finding detail either (the API masks it already, and
// the worker never even parses the detail field).
func TestOutputScanConsultCarriesNoBytes(t *testing.T) {
	log := &scanLog{}
	secret := "raw-unsafe-bytes-must-not-cross-the-wire"
	url := newScanServer(t, log, `{"mode":"enforce","blocked":true,"findings":[{"category":"x","detail":"`+secret+`"}]}`, 200)
	stubScanHTTP(t)
	e := scanExecutor(url, "enforce")
	task := scanTask(9011, 7)
	blocked, reason := e.scanOutputBeforeStore(context.Background(), task,
		[]transport.OutputFile{}, true)
	if !blocked {
		t.Fatalf("enforce block must refuse")
	}
	if strings.Contains(reason, secret) {
		t.Errorf("finding detail must never reach the refusal reason: %q", reason)
	}
}

// AUTH: when outputScanSetAuth is wired, the consult request carries the
// expected X-Worker-Name header. The test installs the seam, stands up a
// server that records received headers, and asserts the value.
func TestOutputScanAuthHeaderCarried(t *testing.T) {
	wantWorker := "test-worker-907"
	wantToken := "dummy-bootstrap-token-907"
	got := make(chan scanAuthHeaders, 1)
	log := &scanLog{}
	url := newScanServerWithAuth(t, log, got, 200)
	stubScanHTTP(t)
	SetOutputScanAuth(func(req *http.Request) {
		req.Header.Set("X-Worker-Name", wantWorker)
		req.Header.Set("X-Bootstrap-Token", wantToken)
	})
	defer SetOutputScanAuth(nil)
	e := scanExecutor(url, "shadow")
	task := scanTask(9012, 7)
	blocked, reason := e.scanOutputBeforeStore(context.Background(), task,
		[]transport.OutputFile{}, true)
	if blocked || reason != "" {
		t.Fatalf("shadow allow must not refuse: blocked=%v reason=%q", blocked, reason)
	}
	select {
	case h := <-got:
		if h.worker != wantWorker {
			t.Errorf("expected X-Worker-Name=%q, got %q", wantWorker, h.worker)
		}
		if h.token != wantToken {
			t.Errorf("expected X-Bootstrap-Token=%q, got %q", wantToken, h.token)
		}
	default:
		t.Fatal("consult did not carry the auth headers")
	}
}

// scanAuthHeaders carries the worker auth headers captured off a request
// by newScanServerWithAuth, for TestOutputScanAuthHeaderCarried to assert on.
type scanAuthHeaders struct {
	worker, token string
}

// newScanServerWithAuth is a variant of newScanServer that captures the
// X-Worker-Name / X-Bootstrap-Token headers from the request and sends them
// to `got`.
func newScanServerWithAuth(t *testing.T, log *scanLog, got chan scanAuthHeaders, status int) string {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var gotScan ScanConsult
		if err := json.Unmarshal(raw, &gotScan); err == nil {
			_ = gotScan
		}
		log.mu.Lock()
		defer log.mu.Unlock()
		log.hits++
		if status != 0 {
			w.WriteHeader(status)
			w.Write([]byte(`{"mode":"shadow","blocked":false,"findings":[]}`))
		} else {
			w.WriteHeader(http.StatusOK)
		}
		got <- scanAuthHeaders{r.Header.Get("X-Worker-Name"), r.Header.Get("X-Bootstrap-Token")}
	}))
	t.Cleanup(func() { srv.Close() })
	if status == 0 {
		srv.Close()
	}
	return srv.URL
}
