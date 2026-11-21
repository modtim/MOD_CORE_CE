package executor

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// fakeLoanS3 is a minimal in-memory S3 endpoint (path-style PUT/GET, plus
// the bucket-location probe minio-go issues on first use — see fakeS3's own
// comment in native_staging_test.go for why that must answer with valid
// XML) that additionally captures every PUT's "X-Amz-Meta-*" request
// headers, which native_staging_test.go's own fakeS3 has no need to record.
// No live S3, no versitygw, ever.
type fakeLoanS3 struct {
	mu   sync.Mutex
	bkt  string
	objs map[string][]byte
	meta map[string]map[string]string
}

func newFakeLoanS3(bucket string) *fakeLoanS3 {
	return &fakeLoanS3{bkt: bucket, objs: map[string][]byte{}, meta: map[string]map[string]string{}}
}

func (f *fakeLoanS3) key(r *http.Request) string {
	return strings.TrimPrefix(r.URL.Path, "/"+f.bkt+"/")
}

// metaFor returns the captured X-Amz-Meta-* headers for key (nil if the
// object was never PUT).
func (f *fakeLoanS3) metaFor(key string) map[string]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	m, ok := f.meta[key]
	if !ok {
		return nil
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func (f *fakeLoanS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodGet && (r.URL.Path == "/"+f.bkt || r.URL.Path == "/"+f.bkt+"/") {
		if _, present := r.URL.Query()["location"]; present {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>`))
			return
		}
	}
	key := f.key(r)
	switch r.Method {
	case http.MethodPut:
		// readAWSChunked/decodeMaybeAWSChunked: unexported helpers defined
		// in native_staging_test.go, same package — strip the AWS
		// signed-chunk framing minio-go's small-object PUT uses.
		body, _ := readAWSChunked(r)
		f.objs[key] = body
		m := map[string]string{}
		for h, vals := range r.Header {
			if len(vals) == 0 {
				continue
			}
			if strings.HasPrefix(h, "X-Amz-Meta-") {
				m[strings.TrimPrefix(h, "X-Amz-Meta-")] = vals[0]
			}
		}
		f.meta[key] = m
		w.Header().Set("ETag", `"abc"`)
		w.WriteHeader(http.StatusOK)
	case http.MethodGet:
		data, ok := f.objs[key]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`<Error><Code>NoSuchKey</Code><Message>not found</Message></Error>`))
			return
		}
		w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
	case http.MethodHead:
		data, ok := f.objs[key]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		w.Header().Set("ETag", `"abc"`)
		w.WriteHeader(http.StatusOK)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

// ---------------------------------------------------------------------------
// Pure marker-builder / classifier tests
// ---------------------------------------------------------------------------

func TestLoanOutputMetadataMarksUncollected(t *testing.T) {
	meta := LoanOutputMetadata("77")
	if !IsLoanUncollected(meta) {
		t.Fatalf("expected the uncollected marker, got %+v", meta)
	}
	if IsPartialOutput(meta) {
		t.Fatalf("uncollected metadata must not also read as partial: %+v", meta)
	}
	if got := LoanIDFromMetadata(meta); got != "77" {
		t.Fatalf("loan id = %q, want 77", got)
	}
}

func TestLoanOutputMetadataEmptyLoanIDOmitsIDKey(t *testing.T) {
	meta := LoanOutputMetadata("")
	if !IsLoanUncollected(meta) {
		t.Fatalf("expected the uncollected marker even with no loan id: %+v", meta)
	}
	if _, ok := meta[LoanIDMetaKey]; ok {
		t.Fatalf("an empty loan id must not stamp %s: %+v", LoanIDMetaKey, meta)
	}
}

func TestPartialLoanOutputMetadataMarksPartial(t *testing.T) {
	meta := PartialOutputMetadata("77")
	if !IsPartialOutput(meta) {
		t.Fatalf("expected the partial marker, got %+v", meta)
	}
	if IsLoanUncollected(meta) {
		t.Fatalf("partial metadata must not also read as uncollected: %+v", meta)
	}
	if got := LoanIDFromMetadata(meta); got != "77" {
		t.Fatalf("loan id = %q, want 77", got)
	}
}

func TestPartialOutputMetadataNonLoanKillHasNoLoanID(t *testing.T) {
	meta := PartialOutputMetadata("")
	if !IsPartialOutput(meta) {
		t.Fatal("expected the partial marker")
	}
	if got := LoanIDFromMetadata(meta); got != "" {
		t.Fatalf("a non-loan kill must carry no loan id, got %q", got)
	}
}

func TestLoanMarkerHelpersNilAndOrdinarySafe(t *testing.T) {
	if IsLoanUncollected(nil) || IsPartialOutput(nil) || LoanIDFromMetadata(nil) != "" {
		t.Fatal("nil metadata must read as neither marker and no loan id")
	}
	ordinary := map[string]string{"Some-Other-Key": "x"}
	if IsLoanUncollected(ordinary) || IsPartialOutput(ordinary) {
		t.Fatalf("metadata with no loan-status key must not read as either marker: %+v", ordinary)
	}
}

// ---------------------------------------------------------------------------
// UploadLoanedNativeOutputs / UploadPartialNativeOutputs: real stamp on the
// wire, verified via fakeLoanS3's captured request headers.
// ---------------------------------------------------------------------------

func TestUploadLoanedNativeOutputsStagesUncollectedMarker(t *testing.T) {
	outDir := t.TempDir()
	const content = "loaned render bytes"
	if err := os.WriteFile(filepath.Join(outDir, "out.png"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeLoanS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := &transport.Credentials{MinIO: fakeS3Creds(srv)}
	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: "913"}}

	outs, err := UploadLoanedNativeOutputs(context.Background(), task, creds, outDir, "913", nil)
	if err != nil {
		t.Fatalf("UploadLoanedNativeOutputs: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("expected 1 output, got %+v", outs)
	}
	key := outs[0].ContentHash
	if key != sha256s(content) {
		t.Fatalf("content hash mismatch: got %s want %s", key, sha256s(content))
	}
	meta := fake.metaFor(key)
	if meta == nil {
		t.Fatalf("no metadata captured for uploaded object %s", key)
	}
	if !IsLoanUncollected(meta) {
		t.Fatalf("a completed loaned output must carry the uncollected marker, got %+v", meta)
	}
	if got := LoanIDFromMetadata(meta); got != "913" {
		t.Fatalf("loan id metadata = %q, want 913", got)
	}
	if IsPartialOutput(meta) {
		t.Fatalf("a completed loaned output must never also read as partial: %+v", meta)
	}
}

func TestUploadPartialNativeOutputsStagesPartialMarker(t *testing.T) {
	outDir := t.TempDir()
	const content = "half a render before SIGKILL"
	if err := os.WriteFile(filepath.Join(outDir, "partial.png"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeLoanS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := &transport.Credentials{MinIO: fakeS3Creds(srv)}
	task := &transport.Task{ID: 99, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: "913"}}

	outs, err := UploadPartialNativeOutputs(context.Background(), task, creds, outDir, "913", nil)
	if err != nil {
		t.Fatalf("UploadPartialNativeOutputs: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("expected the partial file to still be retained for the tenant, got %+v", outs)
	}
	meta := fake.metaFor(outs[0].ContentHash)
	if !IsPartialOutput(meta) {
		t.Fatalf("a killed run's output must carry the partial marker, got %+v", meta)
	}
	if IsLoanUncollected(meta) {
		t.Fatalf("a partial output must never also read as uncollected/complete: %+v", meta)
	}
	if got := LoanIDFromMetadata(meta); got != "913" {
		t.Fatalf("loan id metadata = %q, want 913", got)
	}
}

func TestUploadPartialNativeOutputsNonLoanKillHasNoLoanIDStamped(t *testing.T) {
	outDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outDir, "partial.png"), []byte("bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeLoanS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := &transport.Credentials{MinIO: fakeS3Creds(srv)}
	task := &transport.Task{ID: 99, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}

	outs, err := UploadPartialNativeOutputs(context.Background(), task, creds, outDir, "", nil)
	if err != nil {
		t.Fatalf("UploadPartialNativeOutputs: %v", err)
	}
	meta := fake.metaFor(outs[0].ContentHash)
	if !IsPartialOutput(meta) {
		t.Fatalf("expected the partial marker regardless of loan status, got %+v", meta)
	}
	if got := LoanIDFromMetadata(meta); got != "" {
		t.Fatalf("a non-loan kill must carry no loan id, got %q", got)
	}
}

// ---------------------------------------------------------------------------
// MinIONativeStager.CollectOutputs: automatic loan routing off
// task.Payload.LoanID (GC.MWL.12 wiring in native_staging.go).
// ---------------------------------------------------------------------------

func TestMinIONativeStagerCollectOutputsRoutesLoanedTasksToUncollectedMarker(t *testing.T) {
	outDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outDir, "out.bin"), []byte("bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeLoanS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := &transport.Credentials{MinIO: fakeS3Creds(srv)}
	task := &transport.Task{ID: 7, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files", LoanID: "42"}}

	stager := &MinIONativeStager{}
	outs, err := stager.CollectOutputs(context.Background(), task, creds, outDir)
	if err != nil {
		t.Fatalf("CollectOutputs: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("expected 1 output, got %+v", outs)
	}
	meta := fake.metaFor(outs[0].ContentHash)
	if !IsLoanUncollected(meta) {
		t.Fatalf("a loaned task's ordinary CollectOutputs call must stamp uncollected, got %+v", meta)
	}
	if LoanIDFromMetadata(meta) != "42" {
		t.Fatalf("expected loan id 42, got %+v", meta)
	}
}

func TestMinIONativeStagerCollectOutputsNonLoanTaskHasNoMarker(t *testing.T) {
	outDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outDir, "out.bin"), []byte("bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeLoanS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := &transport.Credentials{MinIO: fakeS3Creds(srv)}
	task := &transport.Task{ID: 7, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}} // LoanID empty

	stager := &MinIONativeStager{}
	outs, err := stager.CollectOutputs(context.Background(), task, creds, outDir)
	if err != nil {
		t.Fatalf("CollectOutputs: %v", err)
	}
	meta := fake.metaFor(outs[0].ContentHash)
	if IsLoanUncollected(meta) || IsPartialOutput(meta) {
		t.Fatalf("an ordinary (non-loan) task must never carry a loan marker, got %+v", meta)
	}
}

// ---------------------------------------------------------------------------
// NativeBackend.CollectPartialOutputs: end-to-end through a real killed
// (non-zero exit) native process.
// ---------------------------------------------------------------------------

func pollToTerminal(t *testing.T, nb *NativeBackend, h *Handle) *TaskStatus {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	var st *TaskStatus
	var err error
	for time.Now().Before(deadline) {
		st, err = nb.Poll(context.Background(), h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			return st
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("task did not reach a terminal phase in time: %+v", st)
	return st
}

func TestNativeBackendCollectPartialLoanOutputsAfterKill(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	fake := newFakeLoanS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	nb.SetStager(&MinIONativeStager{})

	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "exit 1"},
		Env:           map[string]string{"MOD_TASK_ID": "42"},
	}
	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}
	creds := &transport.Credentials{MinIO: fakeS3Creds(srv)}
	// Submit directly (not SubmitStaged): only output-collection is under
	// test here, and staging inputs would need a real source_file_location.
	h, err := nb.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if st := pollToTerminal(t, nb, h); st.Phase != TaskFailed {
		t.Fatalf("expected the task to fail, got %+v", st)
	}

	// The node had already written a partial file before it was killed.
	outputDir := filepath.Join(scratch, h.ID, "output")
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outputDir, "partial.png"), []byte("half done"), 0o644); err != nil {
		t.Fatal(err)
	}

	outs, err := nb.CollectPartialOutputs(context.Background(), h, task, creds, "")
	if err != nil {
		t.Fatalf("CollectPartialOutputs: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("expected the partial output to be retained, got %+v", outs)
	}
	meta := fake.metaFor(outs[0].ContentHash)
	if !IsPartialOutput(meta) {
		t.Fatalf("a killed run's collected output must carry the partial marker, got %+v", meta)
	}

	// A failed task must never be presented as a completed result.
	if _, err := nb.CollectOutputs(context.Background(), h, task, creds); err == nil {
		t.Fatal("expected CollectOutputs to still refuse a non-succeeded task")
	}
}

func TestNativeBackendCollectPartialLoanOutputsRefusesSucceededTask(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	nb.SetStager(&MinIONativeStager{})

	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "exit 0"},
		Env:           map[string]string{"MOD_TASK_ID": "42"},
	}
	task := &transport.Task{ID: 42}
	h, err := nb.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if st := pollToTerminal(t, nb, h); st.Phase != TaskSucceeded {
		t.Fatalf("task did not succeed: %+v", st)
	}
	if _, err := nb.CollectPartialOutputs(context.Background(), h, task, &transport.Credentials{}, ""); err == nil {
		t.Fatal("expected CollectPartialOutputs to refuse a succeeded task — that is CollectOutputs's job")
	}
}

func TestNativeBackendCollectPartialOutputsNoStagerNoOp(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch) // no Stager set
	outs, err := nb.CollectPartialOutputs(
		context.Background(),
		&Handle{ID: "x", Backend: "native"},
		&transport.Task{ID: 1},
		&transport.Credentials{},
		"",
	)
	if err != nil || outs != nil {
		t.Fatalf("expected a silent no-op with no Stager, got outs=%+v err=%v", outs, err)
	}
}
