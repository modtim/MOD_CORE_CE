package executor

// Fortress 3.5 — worker STAGES output bytes to scan-staging/{task_id}/ before
// the pre-store consult (926).
//
// What must hold: stagingUploadParamsFromTask renders the prefix the API's
// _validate_staging_key pins (`scan-staging/{task_id}/`, task.ID being the API
// task row id the consult URL uses); the staging upload is the SHARED
// buildUploadScript (unchanged) run in ITS OWN sidecar invocation, with the
// transient-blob cleanup (`mc rm -r --force myminio/<bucket>/scan-staging/<id>/ ||
// true`) as a SEPARATE command (stagingCleanupCmd) that the caller only runs
// AFTER the pre-store consult — never appended to the staging upload, which
// would delete the bytes before the API's HTTP consult ever fetches them;
// scanOutputBeforeStore carries one staging_key per staged descriptor so the
// API can fetch real bytes; and OutputFile with an empty StagingKey marshals
// WITHOUT the key, i.e. every pre-staging wire shape (notably /complete's
// manifest-parsed outputs) stays byte-identical.
//
// Docker path only: native_staging.go / k8s_staging.go / loan_staging.go are
// untouched this pass, so nothing here asserts on them.
import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// stagingTask is a task whose staged keys must carry its OWN id in the prefix.
func stagingTask(id int) *transport.Task {
	return &transport.Task{
		ID:       id,
		TenantID: 7,
		Payload: transport.TaskPayload{
			NodeKind:    "image_output",
			MinioBucket: "tenant-7-files",
		},
	}
}

// stagingExecutor is the bare-Executor shape output_scan_test.go uses, with the
// posture pushed through the heartbeat tunables map (the ONLY posture source).
func stagingExecutor(apiURL, mode string) *Executor {
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

// stagingHTTPDo swaps the consult transport for the stdlib one so the httptest
// server is reachable (same seam-redirect shape output_scan_test.go uses).
func stagingHTTPDo(t *testing.T) {
	old := outputScanHTTPDo
	outputScanHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
		_ = ctx
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("output-scan http: %w", err)
		}
		return resp, nil
	}
	t.Cleanup(func() { outputScanHTTPDo = old })
}

// stagingParams pins the golden prefix string, not just the shape.
func TestStagingParamsGoldenPrefix(t *testing.T) {
	p := stagingUploadParamsFromTask(stagingTask(9101))
	if p.Prefix != "scan-staging/9101/" {
		t.Errorf("prefix must be scan-staging/{task_id}/, got %q", p.Prefix)
	}
	if p.Bucket != "tenant-7-files" {
		t.Errorf("staging lands in the task's own tenant bucket, got %q", p.Bucket)
	}
	if p.CapBytes != 0 {
		t.Errorf("staging pass has no size cap, got %d", p.CapBytes)
	}
	if p.LoanAttr != "" {
		t.Errorf("staging blobs are never loan-stamped, got %q", p.LoanAttr)
	}
	// A wizard_test_run task must NOT inherit the wizard OutputPrefix: staging
	// keys are pinned by the API validator to scan-staging/{id}/ only.
	p = stagingUploadParamsFromTask(&transport.Task{
		ID: 42,
		Payload: transport.TaskPayload{
			MinioBucket:  "b",
			Purpose:      "wizard_test_run",
			OutputPrefix: "leak/",
		},
	})
	if p.Prefix != "scan-staging/42/" {
		t.Errorf("staging prefix ignores OutputPrefix, got %q", p.Prefix)
	}
}

// ORDERING: the staging upload command must be EXACTLY buildUploadScript —
// no cleanup suffix — so the upload sidecar never deletes what it just staged
// before the API's consult (a separate HTTP call made by the caller AFTER
// this sidecar returns) has a chance to fetch it. Cleanup is a wholly separate
// command (stagingCleanupCmd), run by a later sidecar invocation.
func TestStagingUploadCommandHasNoCleanup(t *testing.T) {
	p := stagingUploadParamsFromTask(stagingTask(9101))
	got := buildUploadScript(p)
	if strings.Contains(got, "mc rm") {
		t.Errorf("the staging upload command must NOT delete anything itself (would race the consult), got %q", got)
	}
	if !strings.Contains(got, "myminio/tenant-7-files/scan-staging/9101/") {
		t.Errorf("staged objects land as scan-staging/{id}/<hash> in the tenant bucket, got %q", got)
	}
}

// stagingCleanupCmd is the standalone cleanup command, pinned on its exact
// shape. It must be a SEPARATE command string from the upload (never
// concatenated with " && "), so a caller cannot run them in one sidecar.
func TestStagingCleanupCmdGolden(t *testing.T) {
	p := stagingUploadParamsFromTask(stagingTask(9101))
	cmd := stagingCleanupCmd(p)
	want := "mc rm -r --force myminio/tenant-7-files/scan-staging/9101/ || true"
	if cmd != want {
		t.Errorf("cleanup command = %q, want %q", cmd, want)
	}
	// The durable upload path must never carry this cleanup either.
	durable := buildUploadScript(uploadScriptParamsFromTask(stagingTask(9101)))
	if strings.Contains(durable, "mc rm -r") {
		t.Errorf("the durable upload script must never carry the scan-staging cleanup")
	}
}

// OutputFile wire shape: empty StagingKey is omitted (omitempty), so every
// pre-staging payload — notably /complete's manifest-parsed outputs — is
// byte-identical.
func TestStagingKeyOmittedWhenEmpty(t *testing.T) {
	plainBytes, plainErr := json.Marshal(parseUploadManifest("UPLOAD,abc123,f.png,10,png"))
	if plainErr != nil {
		t.Fatal(plainErr)
	}
	plain := string(plainBytes)
	if strings.Contains(plain, "staging_key") {
		t.Errorf("manifest-parsed outputs must not carry staging_key, got %s", plain)
	}
	oneBytes, oneErr := json.Marshal([]transport.OutputFile{
		{
			ContentHash:   "abc123",
			FileName:      "f.png",
			FileExtension: "png",
			StagingKey:    "scan-staging/9101/abc123",
		},
	})
	if oneErr != nil {
		t.Fatal(oneErr)
	}
	one := string(oneBytes)
	if !strings.Contains(one, `"staging_key":"scan-staging/9101/abc123"`) {
		t.Errorf("staged descriptor must carry staging_key, got %s", one)
	}
}

// scanOutputBeforeStore must carry the staged descriptors through to the
// consult body (the API fetches their bytes via MinioClient(task.tenant_id)).
func TestStagingConsultCarriesStagingKey(t *testing.T) {
	var gotBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		gotBody = strings.TrimSpace(string(raw))
		w.Write([]byte(`{"mode":"shadow","blocked":false,"findings":[]}`))
	}))
	t.Cleanup(func() { srv.Close() })
	stagingHTTPDo(t)
	e := stagingExecutor(srv.URL, "shadow")
	task := stagingTask(9101)
	staged := []transport.OutputFile{
		{
			ContentHash: "scan-staging/9101/abc123",
			FileName:    "f.png",
			StagingKey:  "scan-staging/9101/abc123",
		},
	}
	blocked, reason := e.scanOutputBeforeStore(context.Background(), task, staged, true)
	if blocked || reason != "" {
		t.Errorf("shadow allow must not refuse: blocked=%v reason=%q", blocked, reason)
	}
	if !strings.Contains(gotBody, `"staging_key":"scan-staging/9101/abc123"`) {
		t.Errorf("consult body must carry staging_key per staged output, got %s", gotBody)
	}
}
