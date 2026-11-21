package executor

// 985b — worker-side pre-PUT scan for the checkpoint EXPORT path, the write
// mirror of output_scan_test.go's read-side coverage. Reuses this package's
// existing seams: execCommandContext (stackrunner_test.go's withFakeExec)
// stands in for every `docker run` (the staging tar, the ExportCheckpointVolume
// tar, and the mc cp/rm calls), checkpointHTTPDo for the real presigned PUT,
// and outputScanHTTPDo (output_scan_test.go) for BOTH the pre-PUT consult and
// the 985f post-PUT report — the same seam, discriminated here by request
// path since they are genuinely different concerns (scan posture vs. loan
// transfer accounting): a report fires on every successful export regardless
// of scan posture, so "off = zero hops" below means zero CONSULT hops, not
// zero report hops.

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// hostDirFor finds the host-side path of a `-v host:containerPath` bind in a
// faked `docker run` argv.
func hostDirFor(arg []string, containerPath string) string {
	suffix := ":" + containerPath
	for _, a := range arg {
		if strings.HasSuffix(a, suffix) {
			return strings.TrimSuffix(a, suffix)
		}
	}
	return ""
}

// exportScanFakeExec fakes every docker invocation exportCheckpointWithScan
// makes: volume existence (checkpointVolumeExists), the staging tar (writes a
// REAL file so stageCheckpointExportForScan's os.Stat sees a non-zero size),
// the export tar (streamed to stdout for ExportCheckpointVolume's own PUT),
// and the mc cp/rm calls (no-ops — argv shape is covered by
// output_scan_staging_test.go's sibling coverage, not re-asserted here).
func exportScanFakeExec(t *testing.T, srcDir string) func() {
	t.Helper()
	return withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		joined := strings.Join(arg, " ")
		switch {
		case strings.Contains(joined, "volume") && strings.Contains(joined, "inspect"):
			return exec.CommandContext(ctx, "true")
		case strings.Contains(joined, "czf") && strings.Contains(joined, "/out/checkpoint.tar"):
			outDir := hostDirFor(arg, "/out")
			if outDir == "" {
				t.Fatalf("staging tar invocation missing -v host:/out bind: %v", arg)
			}
			return exec.CommandContext(ctx, "tar", "czf", filepath.Join(outDir, "checkpoint.tar"), "-C", srcDir, ".")
		case strings.Contains(joined, "czf -"):
			return exec.CommandContext(ctx, "tar", "czf", "-", "-C", srcDir, ".")
		case strings.Contains(joined, "mc cp"), strings.Contains(joined, "mc rm"):
			return exec.CommandContext(ctx, "true")
		default:
			t.Fatalf("unexpected docker invocation: %v", arg)
			return exec.CommandContext(ctx, "false")
		}
	})
}

func exportScanTask(id int) *transport.Task {
	return &transport.Task{
		ID: id,
		Payload: transport.TaskPayload{
			NodeKind:                   "generic",
			MinioBucket:                "tenant-7-files",
			LoanID:                     "42",
			CheckpointExportTransferID: 99,
		},
	}
}

func exportScanExecutor(apiURL, mode string) *Executor {
	store := tunables.New()
	if mode != "" {
		store.Apply(map[string]interface{}{outputScanModeTunableKey: mode})
	}
	return &Executor{
		apiURL:   apiURL,
		tunables: store,
		log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
		network:  "bridge",
	}
}

func writeExportFixture(t *testing.T, dir string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "state.bin"), []byte("checkpoint-bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// stubExportPUT swaps checkpointHTTPDo so the test observes whether the
// presigned PUT actually happened, without a real listener.
func stubExportPUT(t *testing.T) *bool {
	t.Helper()
	called := false
	old := checkpointHTTPDo
	checkpointHTTPDo = func(req *http.Request) (*http.Response, error) {
		called = true
		_, _ = io.Copy(io.Discard, req.Body)
		return nopResponse(200, ""), nil
	}
	t.Cleanup(func() { checkpointHTTPDo = old })
	return &called
}

// exportScanServerHits counts consult vs. report hits separately (they share
// outputScanHTTPDo but hit different paths), and captures the report body so
// a test can assert its byte_size.
type exportScanServerHits struct {
	consultHits int
	reportBody  []byte
}

// newExportScanServer answers the consult at verdictJSON and the 985f report
// with a 200 {"status":"ok"}, recording hits/body as above.
func newExportScanServer(t *testing.T, hits *exportScanServerHits, verdictJSON string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.Contains(r.URL.Path, "output-scan-check"):
			hits.consultHits++
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(verdictJSON))
		case strings.Contains(r.URL.Path, "checkpoint-export-uploaded"):
			body, _ := io.ReadAll(r.Body)
			hits.reportBody = body
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"loan_id":42,"transfer_id":99,"status":"uploaded","scanned":true}`))
		default:
			t.Fatalf("unexpected request path: %s", r.URL.Path)
		}
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

// stubExportScanHTTP redirects outputScanHTTPDo at srv for the duration of
// the test, restoring the real client on cleanup.
func stubExportScanHTTP(t *testing.T) {
	t.Helper()
	old := outputScanHTTPDo
	outputScanHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
		return http.DefaultClient.Do(req)
	}
	t.Cleanup(func() { outputScanHTTPDo = old })
}

// OFF: no consult hop at all (scanOutputBeforeStore's own off-mode short
// circuit), but the export still PUTs and still reports the upload — the
// report is loan-transfer accounting, not an output-scan concern.
func TestCheckpointExportOffSkipsConsultButStillReportsAndPUTs(t *testing.T) {
	srcDir := t.TempDir()
	writeExportFixture(t, srcDir)
	defer exportScanFakeExec(t, srcDir)()
	putCalled := stubExportPUT(t)
	stubExportScanHTTP(t)

	hits := &exportScanServerHits{}
	url := newExportScanServer(t, hits, `{"mode":"off","blocked":false,"findings":[]}`)
	e := exportScanExecutor(url, "off")
	task := exportScanTask(1001)

	blocked, reason := e.exportCheckpointWithScan(context.Background(), task,
		&transport.Credentials{}, "minio-client:latest", "ckpt-vol-1001",
		"https://storage.example.com/ckpt.tar.gz?sig=x")

	if blocked {
		t.Fatalf("off posture must never block: reason=%q", reason)
	}
	if hits.consultHits != 0 {
		t.Errorf("off posture must make ZERO consult hops: hits=%d", hits.consultHits)
	}
	if !*putCalled {
		t.Error("off posture must still PUT the checkpoint export")
	}
	if hits.reportBody == nil {
		t.Error("a successful off-posture export must still report the upload")
	}
}

// SHADOW: the consult runs and returns a non-blocking verdict -> the PUT
// happens, the task is not failed, and the report POST carries a non-zero
// byte_size (the staged tar's real size).
func TestCheckpointExportShadowAllowsPUTAndReportsByteSize(t *testing.T) {
	srcDir := t.TempDir()
	writeExportFixture(t, srcDir)
	defer exportScanFakeExec(t, srcDir)()
	putCalled := stubExportPUT(t)
	stubExportScanHTTP(t)

	hits := &exportScanServerHits{}
	url := newExportScanServer(t, hits, `{"mode":"shadow","blocked":false,"findings":[]}`)
	e := exportScanExecutor(url, "shadow")
	task := exportScanTask(1002)

	blocked, reason := e.exportCheckpointWithScan(context.Background(), task,
		&transport.Credentials{}, "minio-client:latest", "ckpt-vol-1002",
		"https://storage.example.com/ckpt.tar.gz?sig=x")

	if blocked {
		t.Fatalf("shadow must never block the store: reason=%q", reason)
	}
	if hits.consultHits != 1 {
		t.Errorf("shadow consults exactly once: hits=%d", hits.consultHits)
	}
	if !*putCalled {
		t.Error("shadow (allow) must still PUT the checkpoint export")
	}
	if hits.reportBody == nil {
		t.Fatal("a successful shadow export must report the upload")
	}
	var got struct {
		LoanID     int   `json:"loan_id"`
		TransferID int   `json:"transfer_id"`
		ByteSize   int64 `json:"byte_size"`
	}
	if err := json.Unmarshal(hits.reportBody, &got); err != nil {
		t.Fatalf("report body not JSON: %v (%s)", err, hits.reportBody)
	}
	if got.LoanID != 42 || got.TransferID != 99 {
		t.Errorf("report identity mismatch: %+v", got)
	}
	if got.ByteSize <= 0 {
		t.Errorf("report must carry the staged tar's non-zero byte_size: got %d", got.ByteSize)
	}
}

// ENFORCE + blocked verdict: the PUT is skipped entirely and the task fails
// with the shared output_scan_blocked reason — the export path can never
// bypass the posture the result/upload paths already honour.
func TestCheckpointExportEnforceBlockSkipsPUT(t *testing.T) {
	srcDir := t.TempDir()
	writeExportFixture(t, srcDir)
	defer exportScanFakeExec(t, srcDir)()
	putCalled := stubExportPUT(t)
	stubExportScanHTTP(t)

	hits := &exportScanServerHits{}
	url := newExportScanServer(t, hits,
		`{"mode":"enforce","blocked":true,"findings":[{"scanner":"pii","category":"person_name","severity":"block"}]}`)
	e := exportScanExecutor(url, "enforce")
	task := exportScanTask(1003)

	blocked, reason := e.exportCheckpointWithScan(context.Background(), task,
		&transport.Credentials{}, "minio-client:latest", "ckpt-vol-1003",
		"https://storage.example.com/ckpt.tar.gz?sig=x")

	if !blocked {
		t.Fatal("enforce block verdict must refuse the export")
	}
	if !strings.HasPrefix(reason, outputScanBlockedReason) {
		t.Errorf("refusal reason must name %q: got %q", outputScanBlockedReason, reason)
	}
	if *putCalled {
		t.Error("a blocked verdict must skip the PUT entirely")
	}
	if hits.reportBody != nil {
		t.Error("a skipped PUT must never report an upload")
	}
}
