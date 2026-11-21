package executor

// Fortress 3.5 (968a) — K8s-path pre-store scan staging (k8s_staging.go).
//
// What must hold: with the consult seam UNSET (and with mode off) the k8s path
// is byte-identical to today — K8sStagingUploadCmd is buildUploadCmd, zero
// staging PUTs, zero consults, zero cleanup. Under enforce+blocked the DURABLE
// key never exists, the staging prefix is gone (LIST+DELETE counters), the
// error names output_scan_blocked and the consult body carried staging_key +
// "upload_pending":true. Under enforce+allow the durable object exists, carries
// the loan marker when task.Payload.LoanID is set, the staging prefix is gone,
// and exactly one consult happened. Under shadow the durable object lands, the
// staging prefix is gone, and nothing refuses. A result-JSON task (stage-out
// skipped the upload) consults with upload_pending:false.
//
// Ordering proof: the consult seam snapshots the bucket the moment it runs and
// MUST see the staged object; once collectScannedOutputs returns the staged
// object MUST be gone while the durable object survives — so the cleanup ran
// strictly after the consult fetched the bytes, never before.
//
// The fake S3 below is native_scan_staging_test.go's scanFakeS3 (region probe,
// signed-chunk PUT, HEAD, GET, LIST list-type=2 XML, DELETE 204) plus the COPY
// verb: a PUT carrying x-amz-copy-source moves objs[src] to objs[dst] and
// replies the CopyObjectResult XML minio-go's api-copy-object.go decodes.
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
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

type k8sScanFakeS3 struct {
	mu            sync.Mutex
	bkt           string
	objs          map[string][]byte
	heads         map[string]map[string]string
	copies        []copyRec
	puts          int
	dels          int
	lists         []string
	putDenyPrefix string
}

type copyRec struct {
	src  string
	dst  string
	hdrs map[string]string
}

func newK8sScanFakeS3(bucket string) *k8sScanFakeS3 {
	return &k8sScanFakeS3{bkt: bucket, objs: make(map[string][]byte),
		heads: make(map[string]map[string]string), copies: []copyRec{},
		puts: 0, dels: 0, lists: []string{}, putDenyPrefix: ""}
}

func k8sScanKeyOf(r *http.Request, bkt string) string {
	return strings.TrimPrefix(r.URL.Path, "/"+bkt+"/")
}

func (f *k8sScanFakeS3) snapshotKeys() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var ks []string
	for k := range f.objs {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

func (f *k8sScanFakeS3) copyCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.copies)
}

func (f *k8sScanFakeS3) copyHdrs(dst string) map[string]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.copies {
		if c.dst == dst {
			return c.hdrs
		}
	}
	return map[string]string{}
}

func k8sScanServeError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("X-Amz-Request-Id", "test")
	w.WriteHeader(status)
	_, _ = w.Write([]byte("<Error><Code>" + code + "</Code><Message>" + msg + "</Message></Error>"))
}

// k8sScanDecodeChunked strips AWS signed-chunk framing from a PUT body (copied
// verbatim from native_scan_staging_test.go's scanDecodeChunked).
func k8sScanDecodeChunked(raw []byte) []byte {
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

// k8sScanS3 is scanFakeS3's wire shape plus the COPY verb.
func (f *k8sScanFakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodGet && (r.URL.Path == "/"+f.bkt || r.URL.Path == "/"+f.bkt+"/") {
		if _, present := r.URL.Query()["location"]; present {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>`))
			return
		}
		if _, present := r.URL.Query()["list-type"]; present {
			// LIST also snapshots the bucket for the ordering proof.
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
	key := k8sScanKeyOf(r, f.bkt)
	switch r.Method {
	case http.MethodPut:
		src := r.Header.Get("x-amz-copy-source")
		if src != "" {
			// COPY verb: minio-go's CopyObject is a PUT with x-amz-copy-source:
			// bucket/urlencoded-key. Move the bytes and record the metadata
			// directive + x-amz-meta-* headers so the loan-marker assertions can
			// read them; reply the CopyObjectResult XML api-copy-object.go decodes.
			srcKey := strings.TrimPrefix(strings.TrimPrefix(src, "/"), f.bkt+"/")
			data, ok := f.objs[srcKey]
			if !ok {
				k8sScanServeError(w, http.StatusNotFound, "NoSuchKey", "copy source missing")
				return
			}
			hdrs := map[string]string{}
			for hk, vs := range r.Header {
				hk = strings.ToLower(hk)
				if strings.HasPrefix(hk, "x-amz-meta-") && len(vs) > 0 {
					hdrs[hk] = vs[0]
				}
			}
			f.objs[key] = data
			f.copies = append(f.copies, copyRec{src: srcKey, dst: key, hdrs: hdrs})
			w.Header().Set("Content-Type", "application/xml")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`<?xml version="1.0" encoding="UTF-8"?>` +
				`<CopyObjectResult><ETag>"abc"</ETag>` +
				`<LastModified>2006-01-02T15:04:05.000Z</LastModified></CopyObjectResult>`))
			return
		}
		if f.putDenyPrefix != "" && strings.HasPrefix(key, f.putDenyPrefix) {
			k8sScanServeError(w, http.StatusForbidden, "AccessDenied", "staging put denied")
			return
		}
		body, _ := io.ReadAll(r.Body)
		f.objs[key] = k8sScanDecodeChunked(body)
		f.puts++
		w.Header().Set("ETag", `"abc"`)
		w.WriteHeader(http.StatusOK)
	case http.MethodHead:
		data, ok := f.objs[key]
		if !ok {
			k8sScanServeError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Content-Length", strconv.Itoa(len(data)))
		w.Header().Set("Last-Modified", time.Unix(0, 0).UTC().Format(http.TimeFormat))
		w.WriteHeader(http.StatusOK)
	case http.MethodGet:
		data, ok := f.objs[key]
		if !ok {
			k8sScanServeError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Content-Length", strconv.Itoa(len(data)))
		w.Header().Set("Last-Modified", time.Unix(0, 0).UTC().Format(http.TimeFormat))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
	case http.MethodDelete:
		delete(f.objs, key)
		f.dels++
		w.WriteHeader(http.StatusNoContent)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func k8sScanCreds(srv *httptest.Server) transport.MinIOCredentials {
	host := strings.TrimPrefix(srv.URL, "http://")
	return transport.MinIOCredentials{
		AccessKey: "ak", SecretKey: "sk", Endpoint: host,
		MCHost:    "http://ak:sk@" + host,
		ExpiresAt: time.Now().Add(time.Hour), SessionToken: "tok",
	}
}

func k8sScanSha256(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

func k8sScanTask(id, tenant int, loanID, outPrefix string) *transport.Task {
	purpose := ""
	if outPrefix != "" {
		purpose = "wizard_test_run"
	}
	return &transport.Task{
		ID: id, TenantID: tenant,
		Payload: transport.TaskPayload{
			NodeKind:     "image_output",
			MinioBucket:  "tenant-" + strconv.Itoa(tenant) + "-files",
			LoanID:       loanID,
			Purpose:      purpose,
			OutputPrefix: outPrefix,
		},
	}
}

func k8sScanHas(f *k8sScanFakeS3, key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.objs[key]
	return ok
}

// k8sScanManifestOuts is what ParseStageOutLogs yields under the staging
// posture: UPLOAD lines print <prefix><hash>, so ContentHash carries the
// transient prefix and no StagingKey yet.
func k8sScanManifestOuts(task *transport.Task, hash string) []transport.OutputFile {
	return []transport.OutputFile{{
		ContentHash: stagingUploadParamsFromTask(task).Prefix + hash,
		FileName:    "out.png", FileExtension: "png", SizeBytes: 6,
	}}
}

// scanConsultLog records the raw JSON the Executor posts to the consult route
// (copied from native_scan_staging_test.go). block=true answers the enforce
// refusal verdict; block=false the allow verdict.
type k8sScanConsultLog struct {
	mu       sync.Mutex
	bodies   []string
	seenKeys [][]string
	block    bool
}

func (l *k8sScanConsultLog) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	l.mu.Lock()
	l.bodies = append(l.bodies, string(body))
	l.mu.Unlock()
	if l.block {
		_, _ = w.Write([]byte(`{"mode":"enforce","blocked":true,"exempted":false,"error":false,` +
			`"findings":[{"category":"nsfw_image"}]}`))
		return
	}
	_, _ = w.Write([]byte(`{"mode":"enforce","blocked":false,"exempted":false,"error":false,"findings":[]}`))
}

func newK8sScanConsultLog(block bool) *k8sScanConsultLog {
	return &k8sScanConsultLog{bodies: []string{}, seenKeys: [][]string{}, block: block}
}

// k8sScanWire installs the seam exactly as native_scan_staging_test.go does
// (outputScanHTTPDo swap + tunables store + bare Executor), returning the
// Executor whose posture read decides K8sScanStagingActive.
func k8sScanWire(t *testing.T, log *k8sScanConsultLog, mode, apiURL string) *Executor {
	tunablesStore := tunables.New()
	tunablesStore.Apply(map[string]interface{}{outputScanModeTunableKey: mode})
	return &Executor{
		apiURL:   apiURL,
		tunables: tunablesStore,
		log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

// ---------------------------------------------------------------------------
// Unset seam = today's k8s path, byte-for-byte (zero staging hops).
// ---------------------------------------------------------------------------

func TestK8sScanNoSeamIsTodayPath(t *testing.T) {
	fake := newK8sScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	task := k8sScanTask(9201, 7, "", "")
	store := tunables.New()
	store.Apply(map[string]interface{}{outputScanModeTunableKey: "enforce"})
	exec := &Executor{apiURL: "http://api", tunables: store,
		log: slog.New(slog.NewTextHandler(io.Discard, nil))}

	if K8sScanStagingActive(exec) {
		t.Fatalf("unset seam must never activate staging")
	}
	if K8sStagingUploadCmd(task, exec) != buildUploadCmd(task) {
		t.Fatalf("unset seam must keep the stage-out upload cmd byte-identical")
	}
	hash := k8sScanSha256("staged")
	outs, err := collectScannedOutputs(context.Background(), k8sScanManifestOuts(task, hash),
		task, &transport.Credentials{MinIO: k8sScanCreds(srv)}, exec)
	if err != nil {
		t.Fatalf("unset seam must not fail: %v", err)
	}
	// The seam is unset, so collectScannedOutputs returns the manifest untouched
	// — the current direct-to-durable shape.
	if len(outs) != 1 || fake.puts != 0 || fake.dels != 0 || fake.copyCount() != 0 {
		t.Fatalf("no seam means NO staging hop at all: puts=%d dels=%d copies=%d outs=%d",
			fake.puts, fake.dels, fake.copyCount(), len(outs))
	}
}

// ---------------------------------------------------------------------------
// enforce + blocked: the durable key NEVER exists, staging is gone, the error
// names output_scan_blocked and the consult carried staging_key + upload_pending.
// ---------------------------------------------------------------------------

func TestK8sScanEnforceBlockedRefuses(t *testing.T) {
	fake := newK8sScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	log := newK8sScanConsultLog(true)
	consultSrv := httptest.NewServer(log)
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

	task := k8sScanTask(9202, 7, "", "")
	hash := k8sScanSha256("staged")
	stagingKey := stagingUploadParamsFromTask(task).Prefix + hash
	// stage-out already landed the transient blob in-pod.
	fake.objs[stagingKey] = []byte("staged")

	store := tunables.New()
	store.Apply(map[string]interface{}{outputScanModeTunableKey: "enforce"})
	exec := &Executor{apiURL: consultSrv.URL, tunables: store,
		log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	SetK8sScanConsult(func(ctx context.Context, tsk *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		log.seenKeys = append(log.seenKeys, fake.snapshotKeys())
		return exec.scanOutputBeforeStore(ctx, tsk, staged, uploadPending)
	})
	t.Cleanup(func() { SetK8sScanConsult(nil) })

	outs, err := collectScannedOutputs(context.Background(), k8sScanManifestOuts(task, hash),
		task, &transport.Credentials{MinIO: k8sScanCreds(srv)}, exec)
	if err == nil || !strings.Contains(err.Error(), outputScanBlockedReason) {
		t.Fatalf("enforce+blocked must refuse with output_scan_blocked, got %v", err)
	}
	if len(outs) != 0 {
		t.Fatalf("a refusal returns no descriptors, got %+v", outs)
	}
	if fake.copyCount() != 0 {
		t.Fatalf("a refusal never CopyObjects: copies=%d", fake.copyCount())
	}
	if scan2 := fake.snapshotKeys(); len(scan2) != 0 {
		t.Fatalf("the staging prefix must be gone after the refusal: %v", scan2)
	}
	if fake.dels < 1 || len(fake.lists) < 1 {
		t.Fatalf("cleanup (LIST+DELETE) must have run after the consult: dels=%d lists=%d",
			fake.dels, len(fake.lists))
	}
	if len(log.bodies) != 1 {
		t.Fatalf("exactly one consult, got %d", len(log.bodies))
	}
	if !strings.Contains(log.bodies[0], `"staging_key":"`+stagingKey+`"`) {
		t.Fatalf("consult body must carry staging_key; body=%s", log.bodies[0])
	}
	if !strings.Contains(log.bodies[0], `"upload_pending":true`) {
		t.Fatalf("file outputs are the PRE-store consult; body=%s", log.bodies[0])
	}
	// Ordering proof: the seam snapshot taken when the consult ran MUST show the
	// staged object, which the cleanup then removed only after.
	if !k8sScanContains(log.seenKeys[0], stagingKey) {
		t.Fatalf("the consult must have seen the staged bytes; saw=%v", log.seenKeys[0])
	}
}

func k8sScanContains(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// enforce + allow: the durable object exists (loan-stamped when LoanID set),
// staging is gone, exactly one consult.
// ---------------------------------------------------------------------------

func TestK8sScanEnforceAllowPromotes(t *testing.T) {
	fake := newK8sScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	log := newK8sScanConsultLog(false)
	consultSrv := httptest.NewServer(log)
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

	task := k8sScanTask(9203, 7, "loan-42", "")
	hash := k8sScanSha256("staged")
	stagingKey := stagingUploadParamsFromTask(task).Prefix + hash
	fake.objs[stagingKey] = []byte("staged")

	store := tunables.New()
	store.Apply(map[string]interface{}{outputScanModeTunableKey: "enforce"})
	exec := &Executor{apiURL: consultSrv.URL, tunables: store,
		log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	SetK8sScanConsult(func(ctx context.Context, tsk *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		log.seenKeys = append(log.seenKeys, fake.snapshotKeys())
		return exec.scanOutputBeforeStore(ctx, tsk, staged, uploadPending)
	})
	t.Cleanup(func() { SetK8sScanConsult(nil) })

	outs, err := collectScannedOutputs(context.Background(), k8sScanManifestOuts(task, hash),
		task, &transport.Credentials{MinIO: k8sScanCreds(srv)}, exec)
	if err != nil {
		t.Fatalf("allow must not refuse: %v", err)
	}
	if len(outs) != 1 || outs[0].StagingKey != "" {
		t.Fatalf("one durable descriptor WITHOUT StagingKey, got %+v", outs)
	}
	if outs[0].ContentHash != hash {
		t.Fatalf("durable key must be <prefix><hash> (empty prefix here), got %q", outs[0].ContentHash)
	}
	if !k8sScanHas(fake, hash) {
		t.Fatalf("the durable object must exist")
	}
	if k8sScanHas(fake, stagingKey) {
		t.Fatalf("the transient blob must be gone")
	}
	if fake.copyCount() != 1 {
		t.Fatalf("exactly one CopyObject, got %d", fake.copyCount())
	}
	hdrs := fake.copyHdrs(hash)
	if hdrs["x-amz-meta-mod-loan-status"] != LoanStatusUncollected ||
		hdrs["x-amz-meta-mod-loan-id"] != "loan-42" {
		t.Fatalf("the durable copy must stamp the loan marker, got %v", hdrs)
	}
	if len(log.bodies) != 1 {
		t.Fatalf("exactly one consult, got %d", len(log.bodies))
	}
	if !k8sScanContains(log.seenKeys[0], stagingKey) {
		t.Fatalf("the consult must have seen the staged bytes; saw=%v", log.seenKeys[0])
	}
}

// ---------------------------------------------------------------------------
// shadow: durable lands, staging gone, no refusal. A result-JSON task consults
// with upload_pending:false.
// ---------------------------------------------------------------------------

func TestK8sScanShadowAllows(t *testing.T) {
	fake := newK8sScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	log := newK8sScanConsultLog(false)
	consultSrv := httptest.NewServer(log)
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

	task := k8sScanTask(9204, 7, "", "_wizard_test_runs/s/1/")
	hash := k8sScanSha256("staged")
	stagingKey := stagingUploadParamsFromTask(task).Prefix + hash
	fake.objs[stagingKey] = []byte("staged")

	store := tunables.New()
	store.Apply(map[string]interface{}{outputScanModeTunableKey: "shadow"})
	exec := &Executor{apiURL: consultSrv.URL, tunables: store,
		log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	SetK8sScanConsult(func(ctx context.Context, tsk *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		return exec.scanOutputBeforeStore(ctx, tsk, staged, uploadPending)
	})
	t.Cleanup(func() { SetK8sScanConsult(nil) })

	outs, err := collectScannedOutputs(context.Background(), k8sScanManifestOuts(task, hash),
		task, &transport.Credentials{MinIO: k8sScanCreds(srv)}, exec)
	if err != nil {
		t.Fatalf("shadow posture must not refuse: %v", err)
	}
	if len(outs) != 1 || outs[0].StagingKey != "" {
		t.Fatalf("one durable descriptor WITHOUT StagingKey, got %+v", outs)
	}
	// wizard_test_run OutputPrefix is honoured on the durable copy.
	wantDurable := uploadScriptParamsFromTask(task).Prefix + hash
	if outs[0].ContentHash != wantDurable || !k8sScanHas(fake, wantDurable) {
		t.Fatalf("durable key must honour OutputPrefix, want %q got %q", wantDurable, outs[0].ContentHash)
	}
	if k8sScanHas(fake, stagingKey) {
		t.Fatalf("the transient blob must be gone")
	}
	if fake.copyCount() != 1 || len(log.bodies) != 1 {
		t.Fatalf("one copy + one consult, got %d / %d", fake.copyCount(), len(log.bodies))
	}
	if !strings.Contains(log.bodies[0], `"staging_key":"`+stagingKey+`"`) {
		t.Fatalf("consult body must carry staging_key; body=%s", log.bodies[0])
	}
	if !strings.Contains(log.bodies[0], `"upload_pending":true`) {
		t.Fatalf("file outputs are the PRE-store consult; body=%s", log.bodies[0])
	}
}

func TestK8sScanResultJSONConsultPendingFalse(t *testing.T) {
	fake := newK8sScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	log := newK8sScanConsultLog(false)
	consultSrv := httptest.NewServer(log)
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

	task := k8sScanTask(9205, 7, "", "")
	// Result JSON wins: stage-out skipped the upload, so the descriptor carries
	// NO staging prefix and nothing was staged.
	resultOuts := []transport.OutputFile{{
		ContentHash: k8sScanSha256("result"), FileName: "validation_result.json",
		FileExtension: "json", SizeBytes: 11,
	}}

	store := tunables.New()
	store.Apply(map[string]interface{}{outputScanModeTunableKey: "enforce"})
	exec := &Executor{apiURL: consultSrv.URL, tunables: store,
		log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	SetK8sScanConsult(func(ctx context.Context, tsk *transport.Task,
		staged []transport.OutputFile, uploadPending bool) (bool, string) {
		return exec.scanOutputBeforeStore(ctx, tsk, staged, uploadPending)
	})
	t.Cleanup(func() { SetK8sScanConsult(nil) })

	outs, err := collectScannedOutputs(context.Background(), resultOuts,
		task, &transport.Credentials{MinIO: k8sScanCreds(srv)}, exec)
	if err != nil {
		t.Fatalf("an allow verdict must not refuse: %v", err)
	}
	if len(outs) != 1 || outs[0].StagingKey != "" {
		t.Fatalf("the result-JSON descriptor passes through, got %+v", outs)
	}
	if fake.copyCount() != 0 || fake.puts != 0 {
		t.Fatalf("result JSON is never copied/re-uploaded by the worker, got %d/%d",
			fake.copyCount(), fake.puts)
	}
	if len(log.bodies) != 1 {
		t.Fatalf("exactly one consult, got %d", len(log.bodies))
	}
	if !strings.Contains(log.bodies[0], `"upload_pending":false`) {
		t.Fatalf("the result-JSON consult is Step-5 (upload_pending false); body=%s", log.bodies[0])
	}
}
