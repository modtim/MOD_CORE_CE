package executor

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// fakeS3 is a minimal in-memory S3 endpoint (path-style, PUT/GET/404) for
// the native staging tests. No live S3, no versitygw — just the wire shape
// minio-go needs.
type fakeS3 struct {
	mu      sync.Mutex
	bkt     string
	objs    map[string][]byte
	puts    int
	gets    int
	auth403 bool // when set, GET/PUT answer 403 until cleared
	// authFailOnce, when >0, makes the NEXT object PUT/GET answer 403 and
	// then decrements — a deterministic one-shot auth failure for the
	// re-mint-retry tests (no wall-clock racing).
	authFailOnce int
}

func newFakeS3(bucket string) *fakeS3 {
	return &fakeS3{bkt: bucket, objs: make(map[string][]byte)}
}

func (f *fakeS3) key(r *http.Request) string {
	return strings.TrimPrefix(r.URL.Path, "/"+f.bkt+"/")
}

// readAWSChunked returns the request body as raw object bytes. minio-go
// PUTs small objects as AWS signed-chunked: each chunk is
// "<hex-size>;chunk-signature=<hex>\r\n<data>\r\n" and the stream ends
// with a zero-size chunk. It sends that with an explicit Content-Length
// and no Transfer-Encoding header, so net/http does NOT de-chunk it --
// read the whole body and strip the framing. Ordinary (non-chunked)
// bodies pass through unchanged.
func readAWSChunked(r *http.Request) ([]byte, error) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	return decodeMaybeAWSChunked(raw), nil
}

// decodeMaybeAWSChunked strips AWS signed-chunk framing from raw if
// present; otherwise returns raw unchanged.
func decodeMaybeAWSChunked(raw []byte) []byte {
	nl := bytes.Index(raw, []byte("\n"))
	if nl < 0 {
		return raw
	}
	firstLine := string(raw[:nl])
	semi := strings.Index(firstLine, ";")
	if semi < 0 {
		return raw // ordinary body, first line is content
	}
	sizePart := firstLine[:semi]
	fn, err := strconv.ParseUint(sizePart, 16, 64)
	if err != nil || fn == 0 || nl+1 >= len(raw) {
		return raw
	}
	n := int(fn)
	// off now points at the first chunk's DATA (after the first size line's
	// CRLF); n is that chunk's byte count. Layout of the rest:
	//   <data>\r\n<size-line>\r\n<data>\r\n ... 0;<sig>\r\n
	off := nl + 1
	if raw[off] == '\r' {
		off++
	}
	var out []byte
	for {
		if off+n > len(raw) {
			return raw
		}
		out = append(out, raw[off:off+n]...)
		off += n
		// skip the \r\n terminator after the data
		if off+1 < len(raw) && raw[off] == '\r' && raw[off+1] == '\n' {
			off += 2
		} else if off < len(raw) && raw[off] == '\n' {
			off++
		}
		// read the next size line
		nl = bytes.Index(raw[off:], []byte("\n"))
		if nl < 0 {
			break
		}
		line := string(raw[off : off+nl])
		nextOff := off + nl + 1
		if nextOff < len(raw) && raw[nextOff] == '\r' {
			nextOff++
		}
		s2 := strings.Index(line, ";")
		if s2 < 0 {
			return raw
		}
		nn, perr := strconv.ParseUint(line[:s2], 16, 64)
		if perr != nil {
			return raw
		}
		n = int(nn)
		if n == 0 {
			break
		}
		off = nextOff
	}
	return out
}

func (f *fakeS3) serveError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("X-Amz-Request-Id", "test")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(fmt.Sprintf(`<Error><Code>%s</Code><Message>%s</Message></Error>`, code, msg)))
}

// should403 reports whether this request gets a 403: the sticky auth403
// flag, or a one-shot authFailOnce (consumed exactly once). Caller holds
// f.mu.
func (f *fakeS3) should403() bool {
	if f.auth403 {
		return true
	}
	if f.authFailOnce > 0 {
		f.authFailOnce--
		return true
	}
	return false
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	// The minio-go client resolves a bucket's region on first use via
	// `GET /<bucket>/?location` (the param is present but empty). Answer it
	// with valid S3 XML or every subsequent call fails to parse the
	// response. Test for the param's PRESENCE, not a non-empty value.
	// Note: the probe's path is "/<bucket>/" WITH a trailing slash.
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
		if f.should403() {
			f.serveError(w, http.StatusForbidden, "AccessDenied", "denied")
			return
		}
		body, _ := readAWSChunked(r)
		f.objs[key] = body
		f.puts++
		// S3 PUT returns an empty body.
		w.Header().Set("ETag", `"abc"`)
		w.WriteHeader(http.StatusOK)
	case http.MethodHead:
		if f.should403() {
			f.serveError(w, http.StatusForbidden, "AccessDenied", "denied")
			return
		}
		data, ok := f.objs[key]
		if !ok {
			f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		// HEAD returns headers only; Content-Length is the object size.
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		w.WriteHeader(http.StatusOK)
	case http.MethodGet:
		if f.should403() {
			f.serveError(w, http.StatusForbidden, "AccessDenied", "denied")
			return
		}
		data, ok := f.objs[key]
		if !ok {
			f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		f.gets++
		w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func fakeS3Creds(server *httptest.Server) transport.MinIOCredentials {
	return transport.MinIOCredentials{
		AccessKey:    "ak",
		SecretKey:    "sk",
		Endpoint:     strings.TrimPrefix(server.URL, "http://"),
		MCHost:       "http://ak:sk@" + strings.TrimPrefix(server.URL, "http://"),
		ExpiresAt:    time.Now().Add(time.Hour),
		SessionToken: "tok",
	}
}

func clientForFake(t *testing.T, c transport.MinIOCredentials) *minio.Client {
	t.Helper()
	client, err := s3ClientFor(c)
	if err != nil {
		t.Fatalf("s3ClientFor: %v", err)
	}
	return client
}

func sha256s(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

// ---------------------------------------------------------------------------
// Staging into a host dir
// ---------------------------------------------------------------------------

func TestStageNativeInputsSingleFile(t *testing.T) {
	const content = "the one and only input"
	hash := sha256s(content)
	fake := newFakeS3("tenant-1-files")
	fake.objs[hash] = []byte(content)
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := fakeS3Creds(srv)
	client := clientForFake(t, creds)

	dir := t.TempDir()
	payload := transport.TaskPayload{
		MinioBucket:        "tenant-1-files",
		SourceFileLocation: hash,
		FileExtension:      "jpg",
		NodeKind:           "resize_image_process",
	}
	if err := StageNativeInputs(context.Background(), client, payload, dir); err != nil {
		t.Fatalf("StageNativeInputs: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dir, "input_resize_image_process.jpg"))
	if err != nil {
		t.Fatalf("staged file missing: %v", err)
	}
	if string(got) != content {
		t.Fatalf("staged bytes = %q, want %q", got, content)
	}
	if fake.puts > 0 {
		t.Fatalf("unexpected puts: %d", fake.puts)
	}
}

func TestStageNativeInputsMultiFileNamesByCog(t *testing.T) {
	fake := newFakeS3("tenant-1-files")
	h1, h2 := sha256s("a"), sha256s("b")
	fake.objs[h1] = []byte("a")
	fake.objs[h2] = []byte("b")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	client := clientForFake(t, fakeS3Creds(srv))
	dir := t.TempDir()
	payload := transport.TaskPayload{
		MinioBucket: "tenant-1-files",
		InputFiles: []transport.InputFile{
			{CogID: 3, ContentHash: h1, FileExtension: "jpg"},
			{CogID: 4, ContentHash: h2, FileExtension: "dng"},
		},
	}
	if err := StageNativeInputs(context.Background(), client, payload, dir); err != nil {
		t.Fatalf("StageNativeInputs: %v", err)
	}
	for _, want := range []string{"3.jpg", "4.dng"} {
		if _, err := os.Stat(filepath.Join(dir, want)); err != nil {
			t.Fatalf("expected staged %s: %v", want, err)
		}
	}
}

// ---------------------------------------------------------------------------
// Hash-mismatch rejection
// ---------------------------------------------------------------------------

func TestStageNativeInputsHashMismatchRejected(t *testing.T) {
	// The object store holds bytes whose sha256 is NOT the promised hash.
	// Staging must fail and must not leave a staged file behind.
	fake := newFakeS3("tenant-1-files")
	const promised = "deadbeef"
	const corrupted = "bytes that do not match the promised hash"
	fake.objs[promised] = []byte(corrupted)
	srv := httptest.NewServer(fake)
	defer srv.Close()

	client := clientForFake(t, fakeS3Creds(srv))
	dir := t.TempDir()
	payload := transport.TaskPayload{
		MinioBucket:        "tenant-1-files",
		SourceFileLocation: promised,
		FileExtension:      "jpg",
		NodeKind:           "resize_image_process",
	}
	err := StageNativeInputs(context.Background(), client, payload, dir)
	if err == nil {
		t.Fatal("expected a hash-mismatch error, got nil")
	}
	if !strings.Contains(err.Error(), "content hash mismatch") {
		t.Fatalf("error should name the mismatch, got: %v", err)
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		t.Fatalf("staged file left behind after mismatch: %s", e.Name())
	}
}

// ---------------------------------------------------------------------------
// R-154: tenant-supplied FileExtension / node kind must not escape staging
// ---------------------------------------------------------------------------

// stagingRejectCase is one hostile payload piece; staging must refuse it
// before touching the object store and must leave the staging dir empty.
type stagingRejectCase struct {
	name       string
	ext        string                // single-file payload FileExtension ("" = use nodeKind)
	nodeKind   string                // single-file payload NodeKind ("" = default)
	inputFiles []transport.InputFile // when set, exercises the multi-file branch
	wantErr    string                // substring that must appear in the error
}

func TestStageNativeInputsRejectsHostileStagingNames(t *testing.T) {
	cases := []stagingRejectCase{
		{name: "dotdot", ext: "../evil", wantErr: "refusing"},
		{name: "dotdot-only", ext: "..", wantErr: "refusing"},
		{name: "dotdot-suffix", ext: "png/..", wantErr: "refusing"},
		{name: "absolute", ext: "/etc/passwd", wantErr: "refusing"},
		{name: "sep-slash", ext: "a/b", wantErr: "refusing"},
		{name: "sep-backslash", ext: `a\b`, wantErr: "refusing"},
		{name: "nul", ext: "a\x00b", wantErr: "refusing"},
		{name: "embedded-dot", ext: "png.png", wantErr: "refusing"},
		{name: "very-long", ext: strings.Repeat("a", 17), wantErr: "refusing"},
		{name: "unicode", ext: "aé", wantErr: "refusing"},
		{name: "space", ext: "p ng", wantErr: "refusing"},
		{name: "dash", ext: "a-b", wantErr: "refusing"},
		{name: "underscore", ext: "a_b", wantErr: "refusing"},
		{
			name: "nodekind-traversal", ext: "png",
			nodeKind: "a/../../evil", wantErr: "is not a plain file inside staging dir",
		},
		{
			name: "multifile-hostile", ext: "",
			inputFiles: []transport.InputFile{
				{CogID: 1, ContentHash: "h1", FileExtension: "png"},
				{CogID: 2, ContentHash: "h2", FileExtension: "../x"},
			},
			wantErr: "input_files[1]",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake := newFakeS3("tenant-1-files")
			fake.objs["h1"] = []byte("h1-bytes")
			fake.objs["h2"] = []byte("h2-bytes")
			srv := httptest.NewServer(fake)
			defer srv.Close()
			client := clientForFake(t, fakeS3Creds(srv))

			dir := t.TempDir()
			payload := transport.TaskPayload{MinioBucket: "tenant-1-files"}
			if tc.inputFiles != nil {
				payload.InputFiles = tc.inputFiles
			} else {
				payload.SourceFileLocation = sha256s("real-bytes")
				fake.objs[payload.SourceFileLocation] = []byte("real-bytes")
				payload.FileExtension = tc.ext
				payload.NodeKind = tc.nodeKind
				if payload.NodeKind == "" {
					payload.NodeKind = "resize_image_process"
				}
			}
			err := StageNativeInputs(context.Background(), client, payload, dir)
			if err == nil {
				t.Fatalf("expected rejection, got nil error")
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("error %q does not name the refusal (want %q)", err, tc.wantErr)
			}
			if fake.gets != 0 {
				t.Fatalf("rejected payload still fetched %d objects", fake.gets)
			}
			entries, _ := os.ReadDir(dir)
			if len(entries) != 0 {
				t.Fatalf("staging dir not empty after rejection: %v", entries)
			}
		})
	}
}

func TestStageNativeInputsAcceptsNormalExtensions(t *testing.T) {
	cases := []struct {
		name string
		ext  string // as supplied by the tenant
		want string // final staged filename
	}{
		{name: "bare-png", ext: "png", want: "input_resize_image_process.png"},
		{name: "dotted-mp4", ext: ".mp4", want: "input_resize_image_process.mp4"},
		{name: "empty", ext: "", want: "input_resize_image_process"},
		{name: "mixed-case-digits", ext: "A1B2", want: "input_resize_image_process.A1B2"},
		{name: "sixteen-chars", ext: strings.Repeat("a", 16), want: "input_resize_image_process." + strings.Repeat("a", 16)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			const content = "input bytes"
			hash := sha256s(content)
			fake := newFakeS3("tenant-1-files")
			fake.objs[hash] = []byte(content)
			srv := httptest.NewServer(fake)
			defer srv.Close()
			client := clientForFake(t, fakeS3Creds(srv))

			dir := t.TempDir()
			payload := transport.TaskPayload{
				MinioBucket:        "tenant-1-files",
				SourceFileLocation: hash,
				FileExtension:      tc.ext,
				NodeKind:           "resize_image_process",
			}
			if err := StageNativeInputs(context.Background(), client, payload, dir); err != nil {
				t.Fatalf("StageNativeInputs: %v", err)
			}
			if _, err := os.Stat(filepath.Join(dir, tc.want)); err != nil {
				t.Fatalf("staged file %q missing: %v", tc.want, err)
			}
		})
	}
}

func TestAssertStagingDestInside(t *testing.T) {
	const dir = "/scratch/task-1/input"
	cases := []struct {
		dest   string
		inside bool
	}{
		{dest: "42.png", inside: true},
		{dest: "input_kind", inside: true},
		{dest: "input_kind.X1", inside: true},
		{dest: "../evil.png", inside: false},
		{dest: "a/../../evil.png", inside: false},
		{dest: "a/b.png", inside: false},     // sub-dir: not a plain file in dir
		{dest: `a\b.png`, inside: false},     // separator char anywhere
		{dest: "/etc/passwd", inside: false}, // absolute
		{dest: ".", inside: false},           // the dir itself
		{dest: "a\x00b", inside: false},      // NUL is not alphanumeric
	}
	for _, tc := range cases {
		t.Run("dest="+tc.dest, func(t *testing.T) {
			err := assertStagingDestInside(dir, tc.dest)
			if tc.inside && err != nil {
				t.Fatalf("expected %q inside, got %v", tc.dest, err)
			}
			if !tc.inside && err == nil {
				t.Fatalf("expected %q outside staging dir, got nil", tc.dest)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Manifest produced for native outputs
// ---------------------------------------------------------------------------

func TestBuildNativeOutputManifest(t *testing.T) {
	outDir := t.TempDir()
	write := func(name, content string) {
		if err := os.WriteFile(filepath.Join(outDir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("render.png", "png-bytes")
	write("report.txt", "text-bytes")
	// A subdirectory must be ignored (the sidecar script only uploads files).
	if err := os.Mkdir(filepath.Join(outDir, "nested"), 0o755); err != nil {
		t.Fatal(err)
	}

	outs, err := BuildNativeOutputManifest(outDir, uploadScriptParams{Bucket: "tenant-1-files"})
	if err != nil {
		t.Fatalf("BuildNativeOutputManifest: %v", err)
	}
	if len(outs) != 2 {
		t.Fatalf("expected 2 outputs, got %d: %+v", len(outs), outs)
	}
	want := map[string]transport.OutputFile{
		sha256s("png-bytes"):  {ContentHash: sha256s("png-bytes"), FileName: "render.png", SizeBytes: 9, FileExtension: "png"},
		sha256s("text-bytes"): {ContentHash: sha256s("text-bytes"), FileName: "report.txt", SizeBytes: 10, FileExtension: "txt"},
	}
	for _, o := range outs {
		w, ok := want[o.ContentHash]
		if !ok {
			t.Fatalf("unexpected output %+v (want hashes %+v)", o, want)
		}
		if o != w {
			t.Fatalf("output mismatch: got %+v want %+v", o, w)
		}
	}
}

func TestBuildNativeOutputManifestSandboxCap(t *testing.T) {
	outDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outDir, "big.bin"), []byte(strings.Repeat("x", 512)), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := BuildNativeOutputManifest(outDir, uploadScriptParams{Bucket: "b", CapBytes: 100})
	if err == nil || !strings.Contains(err.Error(), "sandbox output too large") {
		t.Fatalf("expected the cap to trip, got: %v", err)
	}
}

// ---------------------------------------------------------------------------
// The re-mint helper
// ---------------------------------------------------------------------------

func TestEnsureCredsFreshFreshNoRemint(t *testing.T) {
	called := false
	creds := &transport.Credentials{MinIO: transport.MinIOCredentials{ExpiresAt: time.Now().Add(time.Hour)}}
	got, err := EnsureCredsFresh(context.Background(), creds, 7, 60*time.Second, func(ctx context.Context, id int) (*transport.Credentials, error) {
		called = true
		return nil, fmt.Errorf("should not be called")
	})
	if err != nil {
		t.Fatalf("EnsureCredsFresh: %v", err)
	}
	if got != creds {
		t.Fatalf("fresh creds must pass through untouched")
	}
	if called {
		t.Fatal("re-mint was called for fresh credentials")
	}
}

func TestEnsureCredsFreshNearExpiryRemints(t *testing.T) {
	reminted := 0
	stale := &transport.Credentials{MinIO: transport.MinIOCredentials{ExpiresAt: time.Now().Add(5 * time.Second)}}
	fresh := &transport.Credentials{MinIO: transport.MinIOCredentials{ExpiresAt: time.Now().Add(time.Hour)}}
	got, err := EnsureCredsFresh(context.Background(), stale, 7, 60*time.Second, func(ctx context.Context, id int) (*transport.Credentials, error) {
		reminted++
		if id != 7 {
			t.Fatalf("re-mint got task %d, want 7", id)
		}
		return fresh, nil
	})
	if err != nil {
		t.Fatalf("EnsureCredsFresh: %v", err)
	}
	if got != fresh {
		t.Fatalf("expected the freshly minted credentials back")
	}
	if reminted != 1 {
		t.Fatalf("expected exactly one re-mint, got %d", reminted)
	}
}

func TestEnsureCredsFreshNearExpiryNoRemintValue(t *testing.T) {
	stale := &transport.Credentials{MinIO: transport.MinIOCredentials{ExpiresAt: time.Now().Add(5 * time.Second)}}
	_, err := EnsureCredsFresh(context.Background(), stale, 7, 60*time.Second, nil)
	if err == nil {
		t.Fatal("expected an error when near-expiry creds can't be re-minted")
	}
}

// ---------------------------------------------------------------------------
// Upload path: near expiry re-mints before upload; auth error retries once
// ---------------------------------------------------------------------------

type mintCount struct {
	mu    sync.Mutex
	count int
}

func (m *mintCount) bump() {
	m.mu.Lock()
	m.count++
	m.mu.Unlock()
}

func (m *mintCount) value() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.count
}

func TestUploadNativeOutputsNearExpiryRemintsFirst(t *testing.T) {
	outDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outDir, "out.png"), []byte("payload"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := fakeS3Creds(srv)
	creds.ExpiresAt = time.Now().Add(5 * time.Second) // within the 60s margin
	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}

	mints := &mintCount{}
	reMint := func(ctx context.Context, id int) (*transport.Credentials, error) {
		mints.bump()
		fresh := fakeS3Creds(srv)
		return &transport.Credentials{MinIO: fresh}, nil
	}
	outs, err := UploadNativeOutputs(context.Background(), task, &transport.Credentials{MinIO: creds}, outDir, reMint)
	if err != nil {
		t.Fatalf("UploadNativeOutputs: %v", err)
	}
	if mints.value() != 1 {
		t.Fatalf("expected one proactive re-mint, got %d", mints.value())
	}
	if len(outs) != 1 || outs[0].ContentHash != sha256s("payload") {
		t.Fatalf("unexpected manifest: %+v", outs)
	}
	if got := string(fake.objs[sha256s("payload")]); got != "payload" {
		t.Fatalf("uploaded bytes = %q", got)
	}
}

func TestUploadNativeOutputsAuthErrorOneRetry(t *testing.T) {
	outDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outDir, "out.png"), []byte("payload"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeS3("tenant-1-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := fakeS3Creds(srv) // fresh expiry: no proactive re-mint
	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}

	mints := &mintCount{}
	reMint := func(ctx context.Context, id int) (*transport.Credentials, error) {
		mints.bump()
		return &transport.Credentials{MinIO: fakeS3Creds(srv)}, nil
	}

	// The first object PUT deterministically hits a 403 (consumed once);
	// after the single re-mint the retry must succeed — no wall-clock
	// racing.
	fake.authFailOnce = 1

	outs, err := UploadNativeOutputs(context.Background(), task, &transport.Credentials{MinIO: creds}, outDir, reMint)
	if err != nil {
		t.Fatalf("UploadNativeOutputs after one retry: %v", err)
	}
	if mints.value() != 1 {
		t.Fatalf("expected exactly one re-mint on the auth path, got %d", mints.value())
	}
	if len(outs) != 1 || outs[0].ContentHash != sha256s("payload") {
		t.Fatalf("unexpected manifest: %+v", outs)
	}
}

func TestUploadNativeOutputsAuthErrorNoSecondRetry(t *testing.T) {
	outDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outDir, "out.png"), []byte("payload"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := newFakeS3("tenant-1-files")
	fake.auth403 = true // everything is 403, always
	srv := httptest.NewServer(fake)
	defer srv.Close()

	creds := fakeS3Creds(srv)
	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}

	mints := &mintCount{}
	reMint := func(ctx context.Context, id int) (*transport.Credentials, error) {
		mints.bump()
		return &transport.Credentials{MinIO: fakeS3Creds(srv)}, nil
	}
	_, err := UploadNativeOutputs(context.Background(), task, &transport.Credentials{MinIO: creds}, outDir, reMint)
	if err == nil {
		t.Fatal("expected the upload to fail when the retried upload also gets a 403")
	}
	if mints.value() != 1 {
		t.Fatalf("re-mint must happen at most once, got %d mints", mints.value())
	}
}

// ---------------------------------------------------------------------------
// NativeBackend wiring: staging into the host scratch dir
// ---------------------------------------------------------------------------

type fakeStager struct {
	mu       sync.Mutex
	inputs   []string
	outs     []transport.OutputFile
	stageErr error
}

func (f *fakeStager) StageInputs(_ context.Context, _ *transport.Task, _ *transport.Credentials, inputDir string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.inputs = append(f.inputs, inputDir)
	if f.stageErr != nil {
		return f.stageErr
	}
	return os.MkdirAll(inputDir, 0o755)
}

func (f *fakeStager) CollectOutputs(_ context.Context, _ *transport.Task, _ *transport.Credentials, outputDir string) ([]transport.OutputFile, error) {
	return f.outs, nil
}

func TestNativeBackendSubmitStagedStagesInputs(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	fs := &fakeStager{}
	nb.SetStager(fs)

	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "exit 0"},
		Env:           map[string]string{"MOD_TASK_ID": "42"},
	}
	task := &transport.Task{ID: 42, Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}
	h, err := nb.SubmitStaged(context.Background(), spec, task, &transport.Credentials{})
	if err != nil {
		t.Fatalf("SubmitStaged: %v", err)
	}
	fs.mu.Lock()
	inputDir := fs.inputs[len(fs.inputs)-1]
	fs.mu.Unlock()
	wantInput := filepath.Join(scratch, "42", "input")
	if inputDir != wantInput {
		t.Fatalf("staged into %q, want %q", inputDir, wantInput)
	}
	if _, err := os.Stat(inputDir); err != nil {
		t.Fatalf("input dir was not created: %v", err)
	}
	defer nb.Cancel(context.Background(), h)
}

func TestNativeBackendCollectOutputsRequiresSuccess(t *testing.T) {
	scratch := t.TempDir()
	nb := NewNativeBackend(scratch)
	fs := &fakeStager{outs: []transport.OutputFile{{ContentHash: "h", FileName: "f", SizeBytes: 1}}}
	nb.SetStager(fs)

	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "exit 0"},
		Env:           map[string]string{"MOD_TASK_ID": "42"},
	}
	h, err := nb.SubmitStaged(context.Background(), spec, &transport.Task{ID: 42}, &transport.Credentials{})
	if err != nil {
		t.Fatalf("SubmitStaged: %v", err)
	}
	// Poll until the `true` process finishes.
	deadline := time.Now().Add(10 * time.Second)
	var st *TaskStatus
	for time.Now().Before(deadline) {
		st, err = nb.Poll(context.Background(), h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if st.Phase != TaskSucceeded {
		t.Fatalf("task did not succeed: %+v", st)
	}

	task := &transport.Task{ID: 42}
	outs, err := nb.CollectOutputs(context.Background(), h, task, &transport.Credentials{})
	if err != nil {
		t.Fatalf("CollectOutputs: %v", err)
	}
	if len(outs) != 1 || outs[0].ContentHash != "h" {
		t.Fatalf("unexpected outputs: %+v", outs)
	}
}

// ---------------------------------------------------------------------------
// s3ClientFor endpoint handling
// ---------------------------------------------------------------------------

func TestS3ClientForEndpoints(t *testing.T) {
	cases := []struct {
		endpoint string
		want     string
	}{
		{"versitygw:7070", "versitygw:7070"},
		{"http://versitygw:7070", "versitygw:7070"},
		{"https://s3.example.com", "s3.example.com"},
		{"", "127.0.0.1:9000"}, // from MCHost
	}
	for i, c := range cases {
		creds := transport.MinIOCredentials{
			AccessKey:    "ak",
			SecretKey:    "sk",
			Endpoint:     c.endpoint,
			MCHost:       "http://ak:sk@127.0.0.1:9000",
			SessionToken: "t",
		}
		client, err := s3ClientFor(creds)
		if err != nil {
			t.Fatalf("case %d (%q): %v", i, c.endpoint, err)
		}
		if client == nil {
			t.Fatalf("case %d: nil client", i)
		}
	}
	if _, err := s3ClientFor(transport.MinIOCredentials{}); err == nil {
		t.Fatal("expected an error when both endpoint and mc_host are empty")
	}
}

// Keep the JSON contract of the credentials endpoint exercised: the re-mint
// path must accept exactly what /workers/tasks/{id}/credentials returns.
func TestCredentialsShapeRoundTrip(t *testing.T) {
	raw := `{
		"minio": {
			"access_key": "ak", "secret_key": "sk", "session_token": "st",
			"endpoint": "s3:7070", "mc_host": "http://ak:sk@s3:7070",
			"expires_at": "2030-01-01T00:00:00Z"
		}
	}`
	var c transport.Credentials
	if err := json.Unmarshal([]byte(raw), &c); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if c.MinIO.ExpiresAt.IsZero() {
		t.Fatal("expires_at did not parse")
	}
}
