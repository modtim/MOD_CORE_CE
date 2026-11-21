package executor

// Fortress 3.5 (943) — NATIVE-path pre-store scan staging (native_staging.go).
//
// What must hold: nativeScanStagingParams renders the prefix the API's
// task_output_scan.py:_validate_staging_key pins (`scan-staging/{task_id}/`,
// golden string); stageNativeOutputsForScan writes ONLY that transient prefix
// (never the durable output key) and returns descriptors carrying StagingKey;
// cleanupNativeStagedOutputs removes ONLY this task's own prefix; and
// CollectNativeOutputsScanned runs stage -> consult -> durable collect with the
// cleanup DEFERRED, so the rm is strictly AFTER the consult fetched the bytes.
// Ordering proof: the consult seam snapshots the bucket and MUST see the staged
// object; once CollectNativeOutputsScanned returns the staged object MUST be
// gone while the durable object survives. Staging is best-effort: a staging
// failure degrades to the metadata-only consult and never fails the task. With
// the consult seam unset (every pre-943 caller) the native path performs ZERO
// staging hops.
//
// The Docker path's staging (executor.go stageOutputsForScan /
// cleanupStagedOutputs / stagingCleanupCmd, 926/ae6230c55) is asserted by
// output_scan_staging_test.go; nothing here touches it.
import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// scanFakeS3 is the native-path S3 fixture. Unlike native_staging_test.go's
// fakeS3 it must also answer LIST (minio-go ListObjects V2) and DELETE
// (RemoveObject), because the staging pass's cleanup is a list+remove loop.
// putDenyPrefix makes PUTs under one prefix fail (403) so a test can make the
// STAGING pass fail while the durable pass succeeds — the best-effort proof.
type scanFakeS3 struct {
	mu            sync.Mutex
	bkt           string
	objs          map[string][]byte
	puts          int
	gets          int
	dels          int
	lists         []string
	consultKeys   []string
	consults      int
	putDenyPrefix string
	delDeny       bool
}

func newScanFakeS3(bucket string) *scanFakeS3 {
	return &scanFakeS3{bkt: bucket, objs: make(map[string][]byte), lists: []string{},
		consultKeys: []string{}, consults: 0, putDenyPrefix: "", delDeny: false}
}

func scanKeyOf(r *http.Request, bkt string) string {
	return strings.TrimPrefix(r.URL.Path, "/"+bkt+"/")
}

func (f *scanFakeS3) snapshotKeys() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var ks []string
	for k := range f.objs {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

func scanServeError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("X-Amz-Request-Id", "test")
	w.WriteHeader(status)
	_, _ = w.Write([]byte("<Error><Code>" + code + "</Code><Message>" + msg + "</Message></Error>"))
}

// scanDecodeChunked strips AWS signed-chunk framing from a PUT body (minio-go
// sends small objects signed-chunked with an explicit Content-Length, so
// net/http does not de-chunk them).
func scanDecodeChunked(raw []byte) []byte {
	nl := bytes.Index(raw, []byte("\n"))
	if nl < 0 {
		return raw
	}
	first := string(raw[:nl])
	semi := strings.Index(first, ";")
	if semi < 0 {
		return raw
	}
	n, perr := strconv.ParseUint(first[:semi], 16, 64)
	if perr != nil || n == 0 || nl+1 >= len(raw) {
		return raw
	}
	size := int(n)
	out := make([]byte, 0, len(raw))
	off := nl + 1
	if raw[off] == 0x0d {
		off++
	}
	for {
		if off+size > len(raw) {
			return raw
		}
		out = append(out, raw[off:off+size]...)
		off += size
		if off+1 < len(raw) && raw[off] == 0x0d && raw[off+1] == 0x0a {
			off += 2
		} else if off < len(raw) && raw[off] == 0x0a {
			off += 1
		}
		nl2 := bytes.Index(raw[off:], []byte("\n"))
		if nl2 < 0 {
			break
		}
		line := string(raw[off : off+nl2])
		off += nl2 + 1
		s2 := strings.Index(line, ";")
		if s2 < 0 {
			break
		}
		nn, perr2 := strconv.ParseUint(line[:s2], 16, 64)
		if perr2 != nil || nn == 0 {
			break
		}
		size = int(nn)
	}
	return out
}

// scanS3 is the wire shape minio-go needs: region probe, PUT, GET, HEAD,
// LIST (list-type=2 XML) and DELETE (204).
func (f *scanFakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodGet && (r.URL.Path == "/"+f.bkt || r.URL.Path == "/"+f.bkt+"/") {
		if _, present := r.URL.Query()["location"]; present {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>`))
			return
		}
		if _, present := r.URL.Query()["list-type"]; present {
			prefix := ""
			if vs := r.URL.Query()["prefix"]; len(vs) > 0 {
				prefix = vs[0]
			}
			f.lists = append(f.lists, prefix)
			var ks []string
			for k := range f.objs {
				if prefix == "" || strings.HasPrefix(k, prefix) {
					ks = append(ks, k)
				}
			}
			sort.Strings(ks)
			var sb strings.Builder
			sb.WriteString(`<?xml version="1.0" encoding="UTF-8"?>`)
			sb.WriteString(`<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">`)
			sb.WriteString(`<Name>` + f.bkt + `</Name><Prefix>` + prefix + `</Prefix>`)
			sb.WriteString(`<EncodingType>url</EncodingType><IsTruncated>false</IsTruncated>`)
			for _, k := range ks {
				sb.WriteString(`<Contents><Key>` + k + `</Key><LastModified>2024-01-01T00:00:00.000Z</LastModified>`)
				sb.WriteString(`<Size>` + strconv.Itoa(len(f.objs[k])) + `</Size><ETag>"abc"</ETag></Contents>`)
			}
			sb.WriteString(`</ListBucketResult>`)
			w.Header().Set("Content-Type", "application/xml")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(sb.String()))
			return
		}
	}
	key := scanKeyOf(r, f.bkt)
	switch r.Method {
	case http.MethodPut:
		if f.putDenyPrefix != "" && strings.HasPrefix(key, f.putDenyPrefix) {
			scanServeError(w, http.StatusForbidden, "AccessDenied", "staging put denied")
			return
		}
		body, _ := io.ReadAll(r.Body)
		f.objs[key] = scanDecodeChunked(body)
		f.puts++
		w.Header().Set("ETag", `"abc"`)
		w.WriteHeader(http.StatusOK)
	case http.MethodHead:
		data, ok := f.objs[key]
		if !ok {
			scanServeError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Content-Length", strconv.Itoa(len(data)))
		// minio-go's object stat parses Last-Modified with a fixed set of HTTP
		// date layouts; an absent header fails that parse (surfaced all the way
		// up through uploadAndVerifyOutput's post-upload GET).
		w.Header().Set("Last-Modified", time.Unix(0, 0).UTC().Format(http.TimeFormat))
		w.WriteHeader(http.StatusOK)
	case http.MethodGet:
		data, ok := f.objs[key]
		if !ok {
			scanServeError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		f.gets++
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Content-Length", strconv.Itoa(len(data)))
		w.Header().Set("Last-Modified", time.Unix(0, 0).UTC().Format(http.TimeFormat))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
	case http.MethodDelete:
		if f.delDeny {
			scanServeError(w, http.StatusForbidden, "AccessDenied", "delete denied")
			return
		}
		delete(f.objs, key)
		f.dels++
		w.WriteHeader(http.StatusNoContent)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func scanCreds(srv *httptest.Server) transport.MinIOCredentials {
	host := strings.TrimPrefix(srv.URL, "http://")
	return transport.MinIOCredentials{
		AccessKey:    "ak",
		SecretKey:    "sk",
		Endpoint:     host,
		MCHost:       "http://ak:sk@" + host,
		ExpiresAt:    time.Now().Add(time.Hour),
		SessionToken: "tok",
	}
}

func scanSha256(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

func scanStagedTask(id, tenant int) *transport.Task {
	return &transport.Task{
		ID:       id,
		TenantID: tenant,
		Payload: transport.TaskPayload{
			NodeKind:    "image_output",
			MinioBucket: "tenant-" + strconv.Itoa(tenant) + "-files",
		},
	}
}

// scanHas reports whether the fixture still holds an object at key.
func scanHas(f *scanFakeS3, key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.objs[key]
	return ok
}

// ---------------------------------------------------------------------------
// The transient prefix: golden string, task-scoped.
// ---------------------------------------------------------------------------

func TestNativeScanStagingGoldenPrefix(t *testing.T) {
	p := nativeScanStagingParams(scanStagedTask(9101, 7))
	if p.Prefix != "scan-staging/9101/" {
		t.Errorf("prefix must be scan-staging/{task_id}/, got %q", p.Prefix)
	}
	if p.Bucket != "tenant-7-files" {
		t.Errorf("staging lands in the task\u2019s own tenant bucket, got %q", p.Bucket)
	}
	if p.CapBytes != 0 {
		t.Errorf("staging pass has no size cap, got %d", p.CapBytes)
	}
	if p.LoanAttr != "" {
		t.Errorf("staging blobs are never loan-stamped, got %q", p.LoanAttr)
	}
	// task.ID is load-bearing: two tasks never share a staging prefix.
	if other := nativeScanStagingParams(scanStagedTask(9102, 7)); other.Prefix == p.Prefix {
		t.Errorf("staging prefixes must be task-scoped, both %q", p.Prefix)
	}
}

// ---------------------------------------------------------------------------
// The staging pass writes ONLY the transient prefix, and stamps the key.
// ---------------------------------------------------------------------------

func TestNativeStageWritesOnlyTransientPrefix(t *testing.T) {
	outDir := t.TempDir()
	content := "the bytes the API must scan"
	if err := os.WriteFile(filepath.Join(outDir, "out.bin"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	task := scanStagedTask(9103, 7)
	creds := &transport.Credentials{MinIO: scanCreds(srv)}

	staged, err := stageNativeOutputsForScan(context.Background(), task, creds, outDir)
	if err != nil {
		t.Fatalf("stageNativeOutputsForScan: %v", err)
	}
	if len(staged) != 1 {
		t.Fatalf("expected one staged descriptor, got %+v", staged)
	}
	want := "scan-staging/9103/" + scanSha256(content)
	if staged[0].StagingKey != want {
		t.Fatalf("staging key must be <transient prefix><hash>, got %q want %q", staged[0].StagingKey, want)
	}
	if staged[0].ContentHash != scanSha256(content) {
		t.Fatalf("the descriptor must still carry the real hash, got %q", staged[0].ContentHash)
	}
	if !scanHas(fake, want) {
		t.Fatalf("staged object missing at %q", want)
	}
	// The durable key (no prefix) must NOT have been written by the staging
	// pass — that is the durable upload\u2019s job, with its own verify.
	if scanHas(fake, scanSha256(content)) {
		t.Fatalf("staging pass must not write the durable output key")
	}
	if fake.puts != 1 {
		t.Fatalf("staging pass is one PutObject per output, got %d puts", fake.puts)
	}
}

// ---------------------------------------------------------------------------
// Cleanup removes ONLY this task\u2019s own staging prefix.
// ---------------------------------------------------------------------------

func TestNativeCleanupRemovesOnlyOwnPrefix(t *testing.T) {
	fake := newScanFakeS3("tenant-7-files")
	fake.objs["scan-staging/9104/aaa"] = []byte("mine")
	fake.objs["scan-staging/9105/bbb"] = []byte("another task")
	fake.objs["abc"] = []byte("durable output")
	fake.objs["scan-staging"] = []byte("not a prefix match")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}

	if err := cleanupNativeStagedOutputs(context.Background(), scanStagedTask(9104, 7), creds); err != nil {
		t.Fatalf("cleanupNativeStagedOutputs: %v", err)
	}
	if scanHas(fake, "scan-staging/9104/aaa") {
		t.Fatalf("this task\u2019s staged blob must be removed")
	}
	if !scanHas(fake, "scan-staging/9105/bbb") {
		t.Fatalf("another task\u2019s staging prefix must not be touched")
	}
	if !scanHas(fake, "abc") {
		t.Fatalf("the durable output must survive the cleanup")
	}
	if !scanHas(fake, "scan-staging") {
		t.Fatalf("a key that merely starts like the prefix must not be removed")
	}
	if fake.dels != 1 {
		t.Fatalf("cleanup must remove exactly one object, got %d", fake.dels)
	}
	for _, asked := range fake.lists {
		if asked != "scan-staging/9104/" {
			t.Fatalf("cleanup must list only this task\u2019s own prefix, got %q", asked)
		}
	}
}

// ---------------------------------------------------------------------------
// ORDERING: stage -> consult -> durable, cleanup strictly after the consult.
//
// The consult seam snapshots the bucket the moment it is called: the staged
// blob MUST be visible there. After CollectNativeOutputsScanned RETURNS the
// staged blob MUST be gone and the durable blob MUST survive — so the rm ran
// after the consult fetched the bytes, never before.
// ---------------------------------------------------------------------------

func scanNativeRun(t *testing.T, scratch string, taskID int, creds *transport.Credentials) (*NativeBackend, *Handle) {
	t.Helper()
	nb := NewNativeBackend(scratch)
	// No Stager yet: these fixtures only exercise OUTPUT collection/scanning,
	// so SubmitStaged's own input-staging branch (which needs a real
	// MinioBucket + input_files/source_file_location on the task) is skipped
	// entirely — with Stager == nil, SubmitStaged falls straight through to
	// Submit. The stager is attached right after, before the caller collects
	// outputs.
	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "printf staged > output/out.bin"},
		Env:           map[string]string{"MOD_TASK_ID": strconv.Itoa(taskID)},
	}
	h, err := nb.SubmitStaged(context.Background(), spec, &transport.Task{ID: taskID}, creds)
	nb.SetStager(&MinIONativeStager{})
	if err != nil {
		t.Fatalf("SubmitStaged: %v", err)
	}
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		st, perr := nb.Poll(context.Background(), h)
		if perr != nil {
			t.Fatalf("Poll: %v", perr)
		}
		if st.Phase != TaskRunning {
			if st.Phase != TaskSucceeded {
				t.Fatalf("run did not succeed: %+v", st)
			}
			return nb, h
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("run did not reach a terminal phase in time")
	return nb, h
}

func TestNativeScanSequenceOrdering(t *testing.T) {
	fake := newScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}
	scratch := t.TempDir()
	nb, h := scanNativeRun(t, scratch, 9106, creds)
	task := scanStagedTask(9106, 7)

	seen := ""
	SetNativeScanConsult(func(_ context.Context, _ *transport.Task, _ []transport.OutputFile, _ bool) (bool, string) {
		seen = strings.Join(fake.snapshotKeys(), ",")
		fake.consults++
		return false, ""
	})
	t.Cleanup(func() { SetNativeScanConsult(nil) })

	outs, err := CollectNativeOutputsScanned(context.Background(), nb, h, task, creds)
	if err != nil {
		t.Fatalf("a non-refused sequence must collect: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("expected one durable output, got %+v", outs)
	}
	hash := scanSha256("staged")
	stagedKey := "scan-staging/9106/" + hash
	if !strings.Contains(seen, stagedKey) {
		t.Fatalf("the consult must SEE the staged bytes (consult before rm); bucket at consult: %q", seen)
	}
	if scanHas(fake, stagedKey) {
		t.Fatalf("the staged blob must be gone once the sequence returns (cleanup ran)")
	}
	if !scanHas(fake, hash) {
		t.Fatalf("the durable output must survive the staging cleanup")
	}
	if fake.consults != 1 {
		t.Fatalf("exactly one consult per sequence, got %d", fake.consults)
	}
	if fake.dels != 1 {
		t.Fatalf("cleanup removed exactly the staged blob, got %d deletes", fake.dels)
	}
	// staging put + durable put, and the durable write happened AFTER the
	// consult (the staged key was visible at consult time).
	if fake.puts != 2 {
		t.Fatalf("expected one staging put and one durable put, got %d", fake.puts)
	}
}

func TestNativeScanSequenceRefusalWritesNothingDurable(t *testing.T) {
	scratch := t.TempDir()
	fake := newScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}
	nb, h := scanNativeRun(t, scratch, 9107, creds)
	task := scanStagedTask(9107, 7)

	SetNativeScanConsult(func(_ context.Context, _ *transport.Task, _ []transport.OutputFile, _ bool) (bool, string) {
		fake.consults++
		return true, "output_scan_blocked: csprng_payload"
	})
	t.Cleanup(func() { SetNativeScanConsult(nil) })

	outs, err := CollectNativeOutputsScanned(context.Background(), nb, h, task, creds)
	if err == nil || len(outs) != 0 {
		t.Fatalf("a refused scan must fail the task with no outputs, got %+v err=%v", outs, err)
	}
	if !strings.Contains(err.Error(), "output_scan_blocked") {
		t.Fatalf("the refusal must carry the scan reason, got %q", err.Error())
	}
	hash := scanSha256("staged")
	if scanHas(fake, hash) {
		t.Fatalf("a refused task must write NOTHING durable (ISO 27001: the object never lands)")
	}
	if scanHas(fake, "scan-staging/9107/"+hash) {
		t.Fatalf("the staged transient blob must still be cleaned after the refusal")
	}
	if fake.dels != 1 {
		t.Fatalf("cleanup must still run on the refusal path, got %d deletes", fake.dels)
	}
	if fake.puts != 1 {
		t.Fatalf("only the staging put may have happened, got %d", fake.puts)
	}
}

// ---------------------------------------------------------------------------
// Best-effort: staging failure degrades to the metadata-only consult and never
// fails the task; cleanup failure only leaves an orphan for the API sweep.
// ---------------------------------------------------------------------------

func TestNativeScanStagingFailureDegradesToMetadataOnly(t *testing.T) {
	fake := newScanFakeS3("tenant-7-files")
	fake.putDenyPrefix = "scan-staging/"
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}
	scratch := t.TempDir()
	nb, h := scanNativeRun(t, scratch, 9109, creds)
	task := scanStagedTask(9109, 7)

	seen := ""
	SetNativeScanConsult(func(_ context.Context, _ *transport.Task, staged []transport.OutputFile, _ bool) (bool, string) {
		seen = strconv.Itoa(len(staged))
		fake.consults++
		return false, ""
	})
	t.Cleanup(func() { SetNativeScanConsult(nil) })

	outs, err := CollectNativeOutputsScanned(context.Background(), nb, h, task, creds)
	if err != nil {
		t.Fatalf("a staging failure must NEVER fail the task: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("the durable output must still be collected, got %+v", outs)
	}
	if seen != "0" {
		t.Fatalf("the consult must fall back to metadata-only (no descriptors), got %s staged", seen)
	}
	if fake.consults != 1 {
		t.Fatalf("the consult must still run, got %d", fake.consults)
	}
	if fake.dels != 0 {
		t.Fatalf("nothing was staged, so nothing may be cleaned up, got %d deletes", fake.dels)
	}
	if fake.puts != 1 {
		t.Fatalf("only the durable put may have happened, got %d", fake.puts)
	}
}

func TestNativeScanCleanupFailureLeavesOrphanForSweep(t *testing.T) {
	fake := newScanFakeS3("tenant-7-files")
	fake.delDeny = true
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}
	scratch := t.TempDir()
	nb, h := scanNativeRun(t, scratch, 9110, creds)
	task := scanStagedTask(9110, 7)

	SetNativeScanConsult(func(_ context.Context, _ *transport.Task, _ []transport.OutputFile, _ bool) (bool, string) {
		return false, ""
	})
	t.Cleanup(func() { SetNativeScanConsult(nil) })

	outs, err := CollectNativeOutputsScanned(context.Background(), nb, h, task, creds)
	if err != nil {
		t.Fatalf("a cleanup failure must never fail the task: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("the durable output must still be collected, got %+v", outs)
	}
	hash := scanSha256("staged")
	if !scanHas(fake, "scan-staging/9110/"+hash) {
		t.Fatalf("a denied delete leaves the transient blob for the API-side sweep")
	}
	if !scanHas(fake, hash) {
		t.Fatalf("the durable output must still land")
	}
}

// ---------------------------------------------------------------------------
// Unset seam = the pre-943 native path, byte-for-byte (zero staging hops).
// ---------------------------------------------------------------------------

func TestNativeScanNoSeamIsPre943Path(t *testing.T) {
	fake := newScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}
	scratch := t.TempDir()
	nb, h := scanNativeRun(t, scratch, 9111, creds)
	task := scanStagedTask(9111, 7)

	outs, err := CollectNativeOutputsScanned(context.Background(), nb, h, task, creds)
	if err != nil {
		t.Fatalf("the pre-943 path must collect as before: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("expected one durable output, got %+v", outs)
	}
	hash := scanSha256("staged")
	if scanHas(fake, "scan-staging/9111/"+hash) {
		t.Fatalf("no seam means NO staging hop at all")
	}
	if !scanHas(fake, hash) {
		t.Fatalf("the durable output must land exactly as before")
	}
	if fake.puts != 1 || fake.dels != 0 {
		t.Fatalf("pre-943 shape is one durable put and no deletes, got %d puts / %d dels", fake.puts, fake.dels)
	}
}

// ---------------------------------------------------------------------------
// The seam is the Executor's OWN consult: the staging_key must cross the wire
// in the exact JSON field task_output_scan.py:_validate_staging_key reads.
// ---------------------------------------------------------------------------

// scanConsultLog records the raw JSON the Executor posts to the consult route.
type scanConsultLog struct {
	mu     sync.Mutex
	bodies []string
}

func (l *scanConsultLog) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	l.mu.Lock()
	l.bodies = append(l.bodies, string(body))
	l.mu.Unlock()
	_, _ = w.Write([]byte(`{"mode":"shadow","blocked":false,"exempted":false,"error":false,"findings":[]}`))
}

func newScanConsultLog() *scanConsultLog {
	return &scanConsultLog{bodies: []string{}}
}

func TestNativeConsultCarriesStagingKeyWire(t *testing.T) {
	fake := newScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}
	scratch := t.TempDir()
	nb, h := scanNativeRun(t, scratch, 9112, creds)
	task := scanStagedTask(9112, 7)

	consultLog := newScanConsultLog()
	consultSrv := httptest.NewServer(consultLog)
	defer consultSrv.Close()
	oldDo := outputScanHTTPDo
	outputScanHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
		_ = ctx
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("output-scan http: %w", err)
		}
		return resp, nil
	}
	t.Cleanup(func() { outputScanHTTPDo = oldDo })

	store := tunables.New()
	store.Apply(map[string]interface{}{outputScanModeTunableKey: "shadow"})
	exec := &Executor{
		apiURL:   consultSrv.URL,
		tunables: store,
		log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	SetNativeScanConsult(func(ctx context.Context, tsk *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		return exec.scanOutputBeforeStore(ctx, tsk, staged, uploadPending)
	})
	t.Cleanup(func() { SetNativeScanConsult(nil) })

	outs, err := CollectNativeOutputsScanned(context.Background(), nb, h, task, creds)
	if err != nil {
		t.Fatalf("shadow posture must not refuse: %v", err)
	}
	if len(outs) != 1 {
		t.Fatalf("expected one durable output, got %+v", outs)
	}
	if len(consultLog.bodies) != 1 {
		t.Fatalf("exactly one consult, got %d", len(consultLog.bodies))
	}
	hash := scanSha256("staged")
	want := "scan-staging/9112/" + hash
	if !strings.Contains(consultLog.bodies[0], `"staging_key":"`+want+`"`) {
		t.Fatalf("the consult body must carry staging_key for the API to fetch; body=%s", consultLog.bodies[0])
	}
	if !strings.Contains(consultLog.bodies[0], `"upload_pending":true`) {
		t.Fatalf("the native consult is the PRE-store consult; body=%s", consultLog.bodies[0])
	}
	if scanHas(fake, want) {
		t.Fatalf("cleanup must still run after the consult fetched the bytes")
	}
}
