package releases

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/selfupdate"
)

func newTestLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// TestPublishIfNewer_RefusesPlaceholderVersion guards against the most
// important failure mode: a locally-built image with no git tag has
// Version == "docker" (see Dockerfile `|| echo docker`). If we published
// that, every remote worker would "update" to a binary tagged "docker"
// and next startup would loop because the manifest and the running
// version would mismatch forever.
func TestPublishIfNewer_RefusesPlaceholderVersion(t *testing.T) {
	for _, ver := range []string{"docker", "dev", "", "  "} {
		t.Run("version="+ver, func(t *testing.T) {
			_, published, err := PublishIfNewer(
				context.Background(), nil, ver, newTestLogger(),
			)
			if err != nil {
				t.Fatalf("unexpected error for placeholder version %q: %v", ver, err)
			}
			if published {
				t.Errorf("published=true for placeholder version %q; should skip", ver)
			}
		})
	}
}

// TestPublishIfNewer_RefusesWhenNoAdminCreds covers the case where a
// remote worker (which doesn't have MINIO_ROOT_USER/PASSWORD) somehow
// calls this path. Publish must be a no-op, never an error — the remote
// worker should just run, not blow up on startup.
func TestPublishIfNewer_RefusesWhenNoAdminCreds(t *testing.T) {
	t.Setenv("STORAGE_BACKEND", "")
	t.Setenv("MINIO_ROOT_USER", "")
	t.Setenv("MINIO_ROOT_PASSWORD", "")
	t.Setenv("MINIO_INTERNAL_URL", "http://minio:9000")
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "")
	_, published, err := PublishIfNewer(
		context.Background(), nil, "v1.2.3", newTestLogger(),
	)
	if err != nil {
		t.Fatalf("unexpected error when creds absent: %v", err)
	}
	if published {
		t.Error("published=true without MinIO admin creds; should no-op")
	}
}

// TestManifestJSONRoundtrip ensures the Go Manifest struct serialises
// to a shape compatible with the Python reader
// (CORE/API/utils/worker_releases.py). Field names must stay lowercase
// snake_case and match exactly. Regression guard in case someone
// refactors the JSON tags.
func TestManifestJSONRoundtrip(t *testing.T) {
	m := Manifest{
		Version:     "v1.0.1",
		Amd64SHA256: "a0123456789",
		Arm64SHA256: "b0123456789",
	}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	s := string(data)
	for _, field := range []string{
		`"version"`,
		`"released_at"`,
		`"amd64_sha256"`,
		`"arm64_sha256"`,
	} {
		if !strings.Contains(s, field) {
			t.Errorf("manifest JSON missing field %s; got: %s", field, s)
		}
	}

	// Round-trip: back to struct → values preserved.
	var got Manifest
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Version != m.Version || got.Amd64SHA256 != m.Amd64SHA256 || got.Arm64SHA256 != m.Arm64SHA256 {
		t.Errorf("roundtrip mismatch: got %+v want %+v", got, m)
	}
}

// TestReleaseObjectName_MatchesAPINaming is a table-driven guard against
// drift from CORE/API/routers/worker_provisioning.py's
// _worker_object_name() (see its own table-driven test,
// Test_worker_object_name_* in CORE/API/tests/test_worker_provisioning.py).
// windows keeps `.exe`; linux/darwin never do.
func TestReleaseObjectName_MatchesAPINaming(t *testing.T) {
	cases := []struct {
		os, arch, want string
	}{
		{"linux", "amd64", "mod-worker-linux-amd64"},
		{"linux", "arm64", "mod-worker-linux-arm64"},
		{"windows", "amd64", "mod-worker-windows-amd64.exe"},
		{"darwin", "amd64", "mod-worker-darwin-amd64"},
		{"darwin", "arm64", "mod-worker-darwin-arm64"},
	}
	for _, tc := range cases {
		t.Run(tc.os+"/"+tc.arch, func(t *testing.T) {
			if got := releaseObjectName(tc.os, tc.arch); got != tc.want {
				t.Errorf("releaseObjectName(%q, %q) = %q, want %q", tc.os, tc.arch, got, tc.want)
			}
		})
	}
}

// TestManifestJSONRoundtrip_AllPlatforms extends the original
// Amd64/Arm64-only roundtrip to the new additive windows/darwin fields,
// confirming they serialise with the exact snake_case names the Python
// reader (CORE/API/utils/worker_releases.py) would need to pick up if it
// grows support for them, without disturbing the original two fields
// that already-released workers/API builds depend on.
func TestManifestJSONRoundtrip_AllPlatforms(t *testing.T) {
	m := Manifest{
		Version:            "v1.2.3",
		Amd64SHA256:        "linux-amd64-sum",
		Arm64SHA256:        "linux-arm64-sum",
		WindowsAmd64SHA256: "windows-amd64-sum",
		DarwinAmd64SHA256:  "darwin-amd64-sum",
		DarwinArm64SHA256:  "darwin-arm64-sum",
	}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	s := string(data)
	for _, field := range []string{
		`"amd64_sha256"`,
		`"arm64_sha256"`,
		`"windows_amd64_sha256"`,
		`"darwin_amd64_sha256"`,
		`"darwin_arm64_sha256"`,
	} {
		if !strings.Contains(s, field) {
			t.Errorf("manifest JSON missing field %s; got: %s", field, s)
		}
	}

	var got Manifest
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got != m {
		t.Errorf("roundtrip mismatch: got %+v want %+v", got, m)
	}
}

// TestPublishIfNewer_RefusesPlaceholderVersionWithFivePlatformBinaries
// re-runs the placeholder-version guard with a full five-binary list
// (the real shape cmd/worker/main.go now passes) to confirm the early
// return happens before any of the new OS branches are touched — adding
// windows/darwin binaries must not bypass the placeholder-version guard.
func TestPublishIfNewer_RefusesPlaceholderVersionWithFivePlatformBinaries(t *testing.T) {
	binaries := []ReleaseBinary{
		{OS: "linux", Arch: "amd64", Path: "/releases/mod-worker-linux-amd64"},
		{OS: "linux", Arch: "arm64", Path: "/releases/mod-worker-linux-arm64"},
		{OS: "windows", Arch: "amd64", Path: "/releases/mod-worker-windows-amd64.exe"},
		{OS: "darwin", Arch: "amd64", Path: "/releases/mod-worker-darwin-amd64"},
		{OS: "darwin", Arch: "arm64", Path: "/releases/mod-worker-darwin-arm64"},
	}
	_, published, err := PublishIfNewer(context.Background(), binaries, "dev", newTestLogger())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if published {
		t.Error("published=true for placeholder version; should skip before touching any binary")
	}
}

func TestTruncate(t *testing.T) {
	if truncate("abcdef", 3) != "abc" {
		t.Errorf("truncate(abcdef, 3) != abc")
	}
	if truncate("abc", 10) != "abc" {
		t.Errorf("short string must not be padded")
	}
}

// TestArtifactObjectName_MatchesInstallerBuild is the WIN.1.s15 drift guard:
// the bucket object names publish.go uploads under MUST match what the
// installer/app build scripts emit (CORE/release/windows-installer/build.sh
// -> MOD-Setup-<version>.msi, CORE/WindowsApp/build.sh -> MODTray-<version>.exe)
// and what CORE/release/build-bundle.sh's --windows-artifact check enforces.
func TestArtifactObjectName_MatchesInstallerBuild(t *testing.T) {
	cases := []struct {
		kind, version, want string
	}{
		{"installer", "1.0.71", "MOD-Setup-1.0.71.msi"},
        {"app", "1.0.71", "MODTray-1.0.71.exe"},
	}
	for _, tc := range cases {
        t.Run(tc.kind+"/"+tc.version, func(t *testing.T) {
            got, err := artifactObjectName(tc.kind, tc.version)
            if err != nil {
                t.Errorf("artifactObjectName(%q, %q) errored: %v", tc.kind, tc.version, err)
                return
            }
            if got != tc.want {
                t.Errorf("artifactObjectName(%q, %q) = %q, want %q", tc.kind, tc.version, got, tc.want)
            }
        })
    }
    for _, bad := range []string{"", "msi", "Installer"} {
        if _, err := artifactObjectName(bad, "1.0.0"); err == nil {
            t.Errorf("unknown artifact kind %q must be rejected", bad)
        }
    }
}

// TestManifestJSONRoundtrip_Artifacts extends the roundtrip to the WIN.1.s15
// additive installer_sha256 / app_sha256 keys, and guards that they are ABSENT
// when no artifact was published (omitempty) — nothing may be advertised before
// the MSI/app actually exists.
func TestManifestJSONRoundtrip_Artifacts(t *testing.T) {
    withArt := Manifest{
        Version:         "v1.2.3",
        Amd64SHA256:     "linux-amd64-sum",
        InstallerSHA256: "installer-sum",
        AppSHA256:       "app-sum",
    }
    data, err := json.MarshalIndent(withArt, "", "  ")
    if err != nil {
        t.Fatalf("marshal: %v", err)
    }
    s := string(data)
    for _, field := range []string{`"installer_sha256"`, `"app_sha256"`} {
        if !strings.Contains(s, field) {
            t.Errorf("manifest JSON missing artifact field %s; got: %s", field, s)
        }
    }
    var got Manifest
    if err := json.Unmarshal(data, &got); err != nil {
        t.Fatalf("unmarshal: %v", err)
    }
    if got.InstallerSHA256 != withArt.InstallerSHA256 || got.AppSHA256 != withArt.AppSHA256 {
        t.Errorf("artifact roundtrip mismatch: got %+v want %+v", got, withArt)
    }

    plain := Manifest{Version: "v1.2.3", Amd64SHA256: "linux-amd64-sum"}
    plainData, err := json.MarshalIndent(plain, "", "  ")
    if err != nil {
        t.Fatalf("marshal plain: %v", err)
    }
    if strings.Contains(string(plainData), "sha256\"") &&
        (strings.Contains(string(plainData), `"installer_sha256"`) ||
            strings.Contains(string(plainData), `"app_sha256"`)) {
        t.Errorf("artifact keys must be absent when no artifact was published; got: %s", string(plainData))
    }
}

func TestExistingVersionForLog(t *testing.T) {
	if existingVersionForLog(nil) != "(none)" {
		t.Error("nil manifest should render (none)")
	}
	if existingVersionForLog(&Manifest{Version: "v9"}) != "v9" {
		t.Error("non-nil manifest should expose its version")
	}
}

// ---------------------------------------------------------------------------
// NEWER-ONLY (FIX-GO-RELEASES-NEWER-ONLY): fake object store + tests.
//
// fakeStore is an in-process S3-shaped responder good enough for the calls
// PublishIfNewer makes (BucketExists HEAD, GetObject GET, PutObject PUT): it
// records every request in order so tests can assert "zero uploads happened"
// - the downgrade refusal must not orphan a bucket. Same shape as the
// executor tests' fakeS3 (native_staging_test.go): 127.0.0.1 hosts force
// minio-go path-style URLs, so keys arrive as bucket/name.
type fakeStore struct {
	mu      sync.Mutex
	bkt     string
		 objs  map[string][]byte
	 hits    []string // "<METHOD> <key>" in request order
}

func (f *fakeStore) key(r *http.Request) string {
	 return strings.TrimPrefix(r.URL.Path, "/"+f.bkt+"/")
}

func (f *fakeStore) serveError(w http.ResponseWriter, status int, code, msg string) {
	 w.Header().Set("X-Amz-Request-Id", "test")
	 w.WriteHeader(status)
	 _, _ = w.Write([]byte(fmt.Sprintf(`<Error><Code>%s</Code><Message>%s</Message></Error>`, code, msg)))
}

// readAWSChunked returns the request body as raw object bytes: minio-go PUTs
// small objects as AWS signed-chunked (same framing the executor fakes strip;
// copy of their proven decoder).
func readAWSChunked(r *http.Request) ([]byte, error) {
	 raw, err := io.ReadAll(r.Body)
	 if err != nil {
		 return nil, err
	 }
	 return decodeMaybeAWSChunked(raw), nil
}

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
	 fn, err := strconv.ParseUint(firstLine[:semi], 16, 64)
	 if err != nil || fn == 0 || nl+1 >= len(raw) {
		 return raw
	 }
	 n := int(fn)
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
		 if off+1 < len(raw) && raw[off] == '\r' && raw[off+1] == '\n' {
			 off += 2
		 } else if off < len(raw) && raw[off] == '\n' {
			 off++
		 }
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

func (f *fakeStore) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	 f.mu.Lock()
	 defer f.mu.Unlock()
	 root := r.URL.Path == "/"+f.bkt || r.URL.Path == "/"+f.bkt+"/"
	 // The minio-go client resolves a bucket's region on first use via
	 // `GET /<bucket>/?location`; answer with valid S3 XML (same rule the
	 // executor fakes learned).
	 if r.Method == http.MethodGet && root {
		 if _, present := r.URL.Query()["location"]; present {
			 w.WriteHeader(http.StatusOK)
			 _, _ = w.Write([]byte(`<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>`))
			 return
		 }
	 }
	 if r.Method == http.MethodHead && root {
		 // BucketExists: the bucket always exists in the fake.
		 w.WriteHeader(http.StatusOK)
		 return
	 }
	 key := f.key(r)
	 switch r.Method {
	 case http.MethodPut:
		 if root {
			 // MakeBucket (only when BucketExists said no; it does not here).
			 w.WriteHeader(http.StatusOK)
			 return
		 }
		 body, _ := readAWSChunked(r)
		 f.hits = append(f.hits, "PUT "+key)
		 f.objs[key] = body
		 w.Header().Set("ETag", `"abc"`)
		 w.WriteHeader(http.StatusOK)
	 case http.MethodHead:
		 data, ok := f.objs[key]
		 if !ok {
			 f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
			 return
		 }
		 w.Header().Set("ETag", `"abc"`)
		 w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		 w.WriteHeader(http.StatusOK)
	 case http.MethodGet:
		 data, ok := f.objs[key]
		 if !ok {
			 f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
			 return
		 }
		 f.hits = append(f.hits, "GET "+key)
		 w.Header().Set("Content-Length", fmt.Sprint(len(data)))
		 w.Header().Set("ETag", `"abc"`)
		 w.Header().Set("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
		 w.WriteHeader(http.StatusOK)
		 _, _ = w.Write(data)
	 default:
		 w.WriteHeader(http.StatusMethodNotAllowed)
	 }
}

// startFakeStore seeds the bucket and returns the fake plus its minio host.
func startFakeStore(t *testing.T, seed map[string][]byte) (*fakeStore, string) {
	 f := &fakeStore{bkt: bucket, objs: make(map[string][]byte)}
	 for k, v := range seed {
		 f.objs[k] = append([]byte{}, v...)
	 }
	 srv := httptest.NewServer(f)
	 t.Cleanup(func() { srv.Close() })
	 return f, strings.TrimPrefix(srv.URL, "http://")
}

// putCount counts the PUTs the fake served (the "no upload happened" guard).
func (f *fakeStore) putCount() int {
	 f.mu.Lock()
	 defer f.mu.Unlock()
	 n := 0
	 for _, h := range f.hits {
		 if strings.HasPrefix(h, "PUT ") {
			 n++
		 }
	 }
	 return n
}

// sinkDump renders captured log lines for failure messages.
func sinkDump(c *capturedLog) string {
	c.mu.Lock()
	 defer c.mu.Unlock()
	 return strings.Join(c.lines, " | ")
}

// hitsText dumps the request log for failure messages.
func (f *fakeStore) hitsText() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return strings.Join(f.hits, " ; ")
}

// lookup returns a copy of an object's bytes and whether the key exists.
func (f *fakeStore) lookup(key string) ([]byte, bool) {
	 f.mu.Lock()
	 defer f.mu.Unlock()
	 if v, ok := f.objs[key]; ok {
		 return append([]byte{}, v...), true
	 }
	 return nil, false
}

// pointAtFakeStore wires the storage helpers at the fake (the same env names
// internal/storage resolves; the values below are dummies, never real creds).
func pointAtFakeStore(t *testing.T, host string) {
	 t.Setenv("STORAGE_BACKEND", "versitygw")
	 t.Setenv("VERSITYGW_INTERNAL_URL", "http://"+host)
	 t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "testaccesskey")
	 t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "testsecretkey0123456789abcdefghij0123456789ab")
}

// manifestBytes renders the manifest JSON the fake seeds/serves.
func manifestBytes(version string) []byte {
	 return []byte("{\n  \"version\": \""+version+"\",\n  \"released_at\": \"2026-11-01T00:00:00Z\"\n}")
}

// oneBinary returns a single linux/amd64 ReleaseBinary backed by a real temp
// file (uploadBinary opens it from disk).
func oneBinary(t *testing.T) []ReleaseBinary {
	 dir := t.TempDir()
	 path := dir + "/mod-worker-linux-amd64"
	 f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o755)
	 if err != nil {
		 t.Fatalf("temp binary: %v", err)
	 }
	 io.WriteString(f, "fake worker bytes")
	 f.Close()
	 return []ReleaseBinary{{OS: "linux", Arch: "amd64", Path: path}}
}

// capturedLog is a slog.Handler that records message + attrs so tests can
// assert on the structured lines PublishIfNewer emits (same shape as
// internal/sockproxy/proxy_test.go's capturedLog).
type capturedLog struct {
	mu    sync.Mutex
	lines []string
}

func (c *capturedLog) Enabled(_ context.Context, _ slog.Level) bool { return true }

func (c *capturedLog) Handle(_ context.Context, r slog.Record) error {
	var b strings.Builder
	b.WriteString(r.Message)
	r.Attrs(func(a slog.Attr) bool {
		b.WriteByte(' ')
		b.WriteString(a.Key)
		b.WriteByte('=')
		b.WriteString(a.Value.String())
		return true
	})
	c.mu.Lock()
	c.lines = append(c.lines, b.String())
	c.mu.Unlock()
	return nil
}

func (c *capturedLog) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *capturedLog) WithGroup(string) slog.Handler { return c }

func (c *capturedLog) Has(sub string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, l := range c.lines {
		if strings.Contains(l, sub) {
			return true
		}
	}
	return false
}

func capturingLogger(c *capturedLog) *slog.Logger { return slog.New(c) }

// TestPublishIfNewer_PublishesStrictlyNewer is the golden path under the new
// rule: 1.2.4 over a 1.2.3 manifest publishes.
func TestPublishIfNewer_PublishesStrictlyNewer(t *testing.T) {
	 store, host := startFakeStore(t, map[string][]byte{
	 manifestObjectName: manifestBytes("1.2.3"),
	 })
	 pointAtFakeStore(t, host)
	 v, published, err := PublishIfNewer(context.Background(), oneBinary(t), "1.2.4", newTestLogger())
	 if err != nil {
		 t.Errorf("publish 1.2.4 over 1.2.3: %v", err)
		 return
	 }
	 if !published {
		 t.Error("1.2.4 over 1.2.3 must publish")
		 return
	 }
	 if v != "1.2.4" {
		 t.Errorf("returned version %q", v)
	 }
	 m, ok := store.lookup(manifestObjectName)
	 if !ok || !strings.Contains(string(m), "\"1.2.4\"") {
		 t.Errorf("manifest in fake store is not the new one: %s", string(m))
	 }
}

// TestPublishIfNewer_SkipsEqualVersion keeps the OLD behaviour: an equal
// version is a no-op (now with an explicit log line).
func TestPublishIfNewer_SkipsEqualVersion(t *testing.T) {
	 store, host := startFakeStore(t, map[string][]byte{
	 manifestObjectName: manifestBytes("1.2.3"),
	 })
	 pointAtFakeStore(t, host)
	 sink := &capturedLog{}
	 _, published, err := PublishIfNewer(context.Background(), oneBinary(t), "1.2.3", capturingLogger(sink))
	 if err != nil {
		 t.Errorf("equal version must not error: %v", err)
	 }
	 if published {
		 t.Errorf("equal version 1.2.3 must skip, not publish; log=%s", sinkDump(sink))
	 }
	 if store.putCount() != 0 {
		 t.Errorf("equal version wrote %d objects; hits=%s", store.putCount(), store.hitsText())
	 }
	 m, ok := store.lookup(manifestObjectName)
	 if !ok || !strings.Contains(string(m), "\"1.2.3\"") {
		 t.Errorf("existing manifest was rewritten: %s", string(m))
	 }
}

// TestPublishIfNewer_RefusesOlderVersion is the regression the owner decided
// on 11-11: 1.2.2 over a 1.2.3 manifest must NOT publish (it used to, and
// rolled every remote worker back). No upload, no manifest write, and the
// log names both versions plus the override.
func TestPublishIfNewer_RefusesOlderVersion(t *testing.T) {
	 sink := &capturedLog{}
	 store, host := startFakeStore(t, map[string][]byte{
	 manifestObjectName: manifestBytes("1.2.3"),
	 })
	 pointAtFakeStore(t, host)
	 _, published, err := PublishIfNewer(context.Background(), oneBinary(t), "1.2.2", capturingLogger(sink))
	 if err == nil {
		 t.Error("downgrade 1.2.3 -> 1.2.2 must be refused")
		 return
	 }
	 if published {
		 t.Error("downgrade must not report published=true")
	 }
	 if !strings.Contains(err.Error(), "1.2.3") || !strings.Contains(err.Error(), "1.2.2") {
		 t.Errorf("refusal must name both versions: %v", err)
	 }
	 if !strings.Contains(err.Error(), "AllowDowngrade") {
		 t.Errorf("refusal must name the override: %v", err)
	 }
	 if !sink.Has("1.2.3") || !sink.Has("1.2.2") {
		 t.Error("log line must name both versions")
	 }
	 if !sink.Has("allow-downgrade") {
		 t.Error("log line must name the override")
	 }
	 if store.putCount() != 0 {
		 t.Errorf("refused downgrade wrote %d objects; must write zero", store.putCount())
	 }
	 m, ok := store.lookup(manifestObjectName)
	 if !ok || !strings.Contains(string(m), "\"1.2.3\"") {
		 t.Errorf("refused downgrade touched the manifest: %s", string(m))
	 }
	 if _, has := store.lookup("mod-worker-linux-amd64"); has {
		 t.Error("refused downgrade uploaded a binary")
	 }
}

// TestPublishIfNewer_OperatorOverrideAllowsDowngrade proves the override is
// explicit and default-off: with WithAllowDowngrade(true) the rollback ships.
func TestPublishIfNewer_OperatorOverrideAllowsDowngrade(t *testing.T) {
	 store, host := startFakeStore(t, map[string][]byte{
	 manifestObjectName: manifestBytes("1.2.3"),
	 })
	 pointAtFakeStore(t, host)
	 _, published, err := PublishIfNewer(
	 context.Background(), oneBinary(t), "1.2.2", newTestLogger(),
	 WithAllowDowngrade(true),
	 )
	 if err != nil {
		 t.Errorf("override publish failed: %v", err)
		 return
	 }
	 if !published {
		 t.Error("with the override, 1.2.2 over 1.2.3 must publish")
		 return
	 }
	 m, ok := store.lookup(manifestObjectName)
	 if !ok || !strings.Contains(string(m), "\"1.2.2\"") {
		 t.Errorf("manifest after override publish: %s", string(m))
	 }
}

// TestPublishIfNewer_PrereleaseOrdersBelowRelease: 1.3.0-rc1 is OLDER than
// 1.3.0 (refused) but NEWER than 1.2.9 (published).
func TestPublishIfNewer_PrereleaseOrdersBelowRelease(t *testing.T) {
	 cases := []struct{ existing, next string; wantPublish bool }{
	 {"1.3.0", "1.3.0-rc1", false},
	 {"1.2.9", "1.3.0-rc1", true},
	 }
	 for _, tc := range cases {
		 t.Run(tc.existing+"->"+tc.next, func(t *testing.T) {
			 store, host := startFakeStore(t, map[string][]byte{
				 manifestObjectName: manifestBytes(tc.existing),
			 })
			 pointAtFakeStore(t, host)
			 _, published, err := PublishIfNewer(context.Background(), oneBinary(t), tc.next, newTestLogger())
			 if tc.wantPublish {
				 if err != nil || !published {
					 t.Errorf("%s -> %s must publish: err=%v published=%v", tc.existing, tc.next, err, published)
				 }
				 return
			 }
			 if err == nil {
				 t.Errorf("%s -> %s (prerelease older) must be refused", tc.existing, tc.next)
			 }
			 if store.putCount() != 0 {
				 t.Errorf("refused prerelease wrote %d objects", store.putCount())
			 }
		 })
	 }
}

// TestPublishIfNewer_LeadingVIsTrimmed: CORE/Worker/VERSION ships "v1.0.84",
// so a leading v must not break the compare.
func TestPublishIfNewer_LeadingVIsTrimmed(t *testing.T) {
	 cases := []struct{ existing, next string; wantPublish, wantErr bool }{
	 {"1.2.3", "v1.2.4", true, false},
	 {"v1.2.3", "1.2.3", false, false}, // equal after v-trim: skip, no error
	 {"v1.2.4", "v1.2.3", false, true}, // older after v-trim: refused (error)
	 }
	 for _, tc := range cases {
		 t.Run(tc.existing+"->"+tc.next, func(t *testing.T) {
			 store, host := startFakeStore(t, map[string][]byte{
				 manifestObjectName: manifestBytes(tc.existing),
			 })
			 pointAtFakeStore(t, host)
			 _, published, err := PublishIfNewer(context.Background(), oneBinary(t), tc.next, newTestLogger())
			 if tc.wantPublish {
				 if err != nil || !published {
					 t.Errorf("%s -> %s must publish: err=%v published=%v", tc.existing, tc.next, err, published)
				 }
				 return
			 }
			 if published {
				 t.Errorf("%s -> %s must skip/refuse, not publish", tc.existing, tc.next)
			 }
			 if store.putCount() != 0 {
				 t.Errorf("skip wrote %d objects", store.putCount())
			 }
		 })
	 }
}

// TestPublishIfNewer_UnparseableNewVersionFailsClosed: an unparseable payload
// version is an ERROR, never a silent publish.
func TestPublishIfNewer_UnparseableNewVersionFailsClosed(t *testing.T) {
	 store, host := startFakeStore(t, map[string][]byte{
	 manifestObjectName: manifestBytes("1.2.3"),
	 })
	 pointAtFakeStore(t, host)
	 for _, bad := range []string{"1.2", "not-a-version", "1.2.3.4", "v1.x.3"} {
		 _, published, err := PublishIfNewer(context.Background(), oneBinary(t), bad, newTestLogger())
		 if err == nil {
			 t.Errorf("unparseable new version %q must fail closed", bad)
		 }
		 if published {
			 t.Errorf("unparseable new version %q must not report published", bad)
		 }
		 if store.putCount() != 0 {
			 t.Errorf("unparseable new version %q wrote %d objects", bad, store.putCount())
		 }
	 }
}

// TestPublishIfNewer_UnparseableExistingVersionFailsClosed: a manifest whose
// version cannot be parsed must not be overwritten silently.
func TestPublishIfNewer_UnparseableExistingVersionFailsClosed(t *testing.T) {
	 store, host := startFakeStore(t, map[string][]byte{
	 manifestObjectName: manifestBytes("nightly-2026-11"),
	 })
	 pointAtFakeStore(t, host)
	 _, published, err := PublishIfNewer(context.Background(), oneBinary(t), "1.2.4", newTestLogger())
	 if err == nil {
		 t.Error("unparseable EXISTING manifest version must fail closed")
		 return
	 }
	 if published {
		 t.Error("unparseable existing version must not publish")
	 }
	 if store.putCount() != 0 {
		 t.Errorf("fail-closed wrote %d objects; must write zero", store.putCount())
	 }
	 m, ok := store.lookup(manifestObjectName)
	 if !ok || !strings.Contains(string(m), "nightly-2026-11") {
		 t.Errorf("fail-closed must leave the manifest untouched: %s", string(m))
	 }
}

// TestParseSemver pins the parser on the shapes the pipeline really emits.
func TestParseSemver(t *testing.T) {
	 cases := []struct{ in string; want bool }{
	 {"1.2.3", true},
	 {"v1.0.84", true},
	 {"1.3.0-rc1", true},
	 {"1.3.0+build7", true},
	 {"1.2", false},
	 {"", false},
	 {"docker", false},
	 {"dev", false},
	 {"1.2.3.4", false},
	 {"v1.x.3", false},
	 {"1.2.3-", false},
	 {"1.2.3-rc!", false},
	 }
	 for _, tc := range cases {
		 _, ok := parseSemver(tc.in)
		 if ok != tc.want {
			 t.Errorf("parseSemver(%q) ok=%v want %v", tc.in, ok, tc.want)
		 }
	 }
}

// TestCompareSemver pins the ordering rules the publish tests rely on.
func TestCompareSemver(t *testing.T) {
	 cases := []struct{ a, b string; want int }{
	 {"1.2.4", "1.2.3", 1},
	 {"1.2.3", "1.2.3", 0},
	 {"1.2.2", "1.2.3", -1},
	 {"1.3.0-rc1", "1.3.0", -1},
	 {"1.3.0-rc1", "1.2.9", 1},
	 {"v1.2.4", "1.2.3", 1},
	 {"1.10.0", "1.9.9", 1},
	 {"1.2.3+build7", "1.2.3", 0},
	 {"1.2.3-rc2", "1.2.3-rc1", 1},
	 {"1.2.3-rc1", "1.2.3-rc10", -1},
	 {"1.2.3-rc", "1.2.3-1", 1},
	 }
	 for _, tc := range cases {
		 pa, okA := parseSemver(tc.a)
		 pb, okB := parseSemver(tc.b)
		 if !okA || !okB {
			 t.Errorf("compare case %q/%q unparseable", tc.a, tc.b)
			 continue
		 }
		 got := compareSemver(pa, pb)
		 sign := 0
		 if got > 0 {
			 sign = 1
		 } else if got < 0 {
			 sign = -1
		 }
		 if sign != tc.want {
			 t.Errorf("compareSemver(%q, %q) = %d want %d", tc.a, tc.b, got, tc.want)
		 }
	 }
}
// Manifest-read hardening (FIX-GO-RELEASES-MANIFEST-READ-ERROR).
// manifestS3 is the in-memory wire shape minio-go needs (same shape as
// internal/executor/native_staging_test.go's fakeS3): no live object store,
// no versitygw. It answers the bucket-location probe, HEAD (bucket exists),
// GET (200 / 404 NoSuchKey / one-shot 403) and PUT, and counts PUTs per
// object name so a test can prove NOTHING was uploaded on a refused publish.
// ---------------------------------------------------------------------------

type manifestS3 struct {
	mu sync.Mutex
	bkt string
	objs map[string][]byte
	// denyNextGet, when >0, makes the NEXT manifest GET answer 403 (a
	// transient/auth failure, NOT a missing key) and then decrements.
	denyNextGet int
	manifestPuts int
	otherPuts int
}

func newManifestS3() *manifestS3 {
	return &manifestS3{bkt: "worker-releases", objs: make(map[string][]byte)}
}

func (f *manifestS3) key(r *http.Request) string {
	return strings.TrimPrefix(r.URL.Path, "/"+f.bkt+"/")
}

func (f *manifestS3) serveError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("X-Amz-Request-Id", "test-req")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(fmt.Sprintf("<Error><Code>%s</Code><Message>%s</Message></Error>", code, msg)))
}

func (f *manifestS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	// minio-go resolves a bucket's region on first use via
	// `GET /<bucket>/?location` (param present, value empty).
	if r.Method == http.MethodGet && (r.URL.Path == "/"+f.bkt || r.URL.Path == "/"+f.bkt+"/") {
		if _, present := r.URL.Query()["location"]; present {
			_, _ = w.Write([]byte(`<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>`))
		 return
		}
	}
	// BucketExists: HEAD on the bucket path — answer 200 so publish.go never
	// takes its MakeBucket branch (that PUT would otherwise muddy the counts).
	if (r.URL.Path == "/"+f.bkt || r.URL.Path == "/"+f.bkt+"/") && (r.Method == http.MethodHead || r.Method == http.MethodGet) {
		w.WriteHeader(http.StatusOK)
	 return
	}
	key := f.key(r)
	switch r.Method {
	case http.MethodPut:
		body, _ := io.ReadAll(r.Body)
	 f.objs[key] = body
	 if key == manifestObjectName {
		 f.manifestPuts++
	 } else {
		 f.otherPuts++
	 }
	 w.Header().Set("ETag", `"abc"`)
	 w.WriteHeader(http.StatusOK)
	case http.MethodHead:
	 if _, ok := f.objs[key]; !ok {
		 f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
		 return
	 }
	 w.WriteHeader(http.StatusOK)
	case http.MethodGet:
	 if f.denyNextGet > 0 && key == manifestObjectName {
		 f.denyNextGet--
		 f.serveError(w, http.StatusForbidden, "AccessDenied", "transient read failure")
		 return
	 }
	 data, ok := f.objs[key]
	 if !ok {
		 f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
		 return
	 }
	 w.Header().Set("Content-Length", fmt.Sprint(len(data)))
	 w.Header().Set("ETag", `"abc"`)
	 w.Header().Set("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
	 w.WriteHeader(http.StatusOK)
	 _, _ = w.Write(data)
 default:
	 w.WriteHeader(http.StatusMethodNotAllowed)
 }
}

// fakeManifestEnv points internal/storage at the fake server above. The
// values are test placeholders, never real credentials.
func fakeManifestEnv(t *testing.T, srv *httptest.Server) {
	t.Setenv("STORAGE_BACKEND", "versitygw")
	t.Setenv("VERSITYGW_INTERNAL_URL", srv.URL)
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "ak")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "sk")
	t.Setenv("VAULT_ADDR", "")
}

// oneBinaryOnDisk writes a tiny worker binary into dir and returns the
// ReleaseBinary list publish.go would upload.
func oneBinaryOnDisk(t *testing.T, dir string) []ReleaseBinary {
	p := filepath.Join(dir, "mod-worker-linux-amd64")
	if err := os.WriteFile(p, []byte("fake-elf-bytes"), 0o755); err != nil {
	 t.Fatalf("write fake binary: %v", err)
	}
	return []ReleaseBinary{{OS: "linux", Arch: "amd64", Path: p}}
}

// TestPublishIfNewer_TransientManifestReadRefusesToOverwrite is the fail-
// open guard (S-go-worker-releases row 2): a manifest GET that fails for a
// reason OTHER than "the key does not exist" (here: a 403) must return an
// error, leave the stored manifest BYTE-IDENTICAL, and upload nothing at
// all. The old code logged at Debug and published fresh, which overwrites
// the live manifest on a hiccup and orphans the release the API already
// advertises.
func TestPublishIfNewer_TransientManifestReadRefusesToOverwrite(t *testing.T) {
 const manifest = `{"version":"v1.0.42","amd64_sha256":"deadbeef"}`
 fake := newManifestS3()
 fake.objs[manifestObjectName] = []byte(manifest)
 srv := httptest.NewServer(fake)
 defer srv.Close()
 fakeManifestEnv(t, srv)
 fake.denyNextGet = 1

 dir := t.TempDir()
 binaries := oneBinaryOnDisk(t, dir)
 _, published, err := PublishIfNewer(context.Background(), binaries, "v1.0.99", newTestLogger())
 if err == nil {
	 t.Errorf("transient manifest-read failure must return an error, got none (fail-open regression)")
 }
 if published {
	 t.Errorf("published=true on a transient manifest-read failure; must refuse")
 }
 if got, ok := fake.objs[manifestObjectName]; !ok || string(got) != manifest {
	 t.Errorf("stored manifest changed: got %q, want byte-identical %q", string(got), manifest)
 }
 if fake.manifestPuts != 0 || fake.otherPuts != 0 {
	 t.Errorf("uploads on a refused publish: manifestPuts=%d otherPuts=%d; want 0/0", fake.manifestPuts, fake.otherPuts)
 }
}

// TestPublishIfNewer_MissingManifestPublishesFresh pins the ONE case that
// still publishes fresh: a genuine NoSuchKey (the object does not exist
// yet). Behaviour must be unchanged from today.
func TestPublishIfNewer_MissingManifestPublishesFresh(t *testing.T) {
 fake := newManifestS3()
 srv := httptest.NewServer(fake)
 defer srv.Close()
 fakeManifestEnv(t, srv)

 dir := t.TempDir()
 binaries := oneBinaryOnDisk(t, dir)
 v, published, err := PublishIfNewer(context.Background(), binaries, "v1.0.42", newTestLogger())
 if err != nil {
	 t.Fatalf("fresh publish with no manifest yet: %v", err)
 }
 if !published || v != "v1.0.42" {
	 t.Errorf("missing manifest must publish fresh: got published=%v version=%q", published, v)
 }
 if fake.manifestPuts != 1 {
	 t.Errorf("manifest PUTs = %d, want 1", fake.manifestPuts)
 }
}

// TestPublishIfNewer_CorruptManifestIsNotSilentlyOverwritten states the
// decision: a manifest that exists but does not parse must NOT be
// overwritten silently. Return the error and tell the operator (publish.go
// logs at Warn with the object name) so a human fixes the object.
func TestPublishIfNewer_CorruptManifestIsNotSilentlyOverwritten(t *testing.T) {
 fake := newManifestS3()
 fake.objs[manifestObjectName] = []byte("{ not json at all")
 srv := httptest.NewServer(fake)
 defer srv.Close()
 fakeManifestEnv(t, srv)

 dir := t.TempDir()
 binaries := oneBinaryOnDisk(t, dir)
 _, published, err := PublishIfNewer(context.Background(), binaries, "v1.0.42", newTestLogger())
 if err == nil {
	 t.Errorf("corrupt manifest must be an error, got none (silent fresh publish regression)")
 }
 if published {
	 t.Errorf("published=true over a corrupt manifest; must refuse")
 }
 if fake.manifestPuts != 0 || fake.otherPuts != 0 {
	 t.Errorf("uploads over a corrupt manifest: manifestPuts=%d otherPuts=%d; want 0/0", fake.manifestPuts, fake.otherPuts)
 }
}

// TestPublishIfNewer_ManifestLargerThanCapIsAnError guards the size cap
// (row 3): an object near a megabyte is not a manifest; refuse it rather
// than slurp and parse it.
func TestPublishIfNewer_ManifestLargerThanCapIsAnError(t *testing.T) {
 fake := newManifestS3()
 fake.objs[manifestObjectName] = []byte(strings.Repeat("x", (1 << 20) + 1))
 srv := httptest.NewServer(fake)
 defer srv.Close()
 fakeManifestEnv(t, srv)

 dir := t.TempDir()
 binaries := oneBinaryOnDisk(t, dir)
 _, published, err := PublishIfNewer(context.Background(), binaries, "v1.0.42", newTestLogger())
 if err == nil {
	 t.Errorf("oversized manifest object must be an error, got none")
 }
 if published {
	 t.Errorf("published=true over an oversized manifest; must refuse")
 }
 if fake.manifestPuts != 0 || fake.otherPuts != 0 {
	 t.Errorf("uploads over an oversized manifest: manifestPuts=%d otherPuts=%d; want 0/0", fake.manifestPuts, fake.otherPuts)
 }
}

// TestPublishIfNewer_ValidManifestUnderCapIsReadAndPublished is the SUCCESS
// path the package had no test for (review h1285, row-3 gap): a VALID
// existing manifest UNDER the cap must actually be read and compared, and a
// strictly newer version must publish over it. The log's previous= attr is
// what proves the existing manifest's version reached the compare (a
// fail-open read would log previous=(none)).
func TestPublishIfNewer_ValidManifestUnderCapIsReadAndPublished(t *testing.T) {
	fake := newManifestS3()
	fake.objs[manifestObjectName] = []byte(`{"version":"1.2.3","amd64_sha256":"deadbeef"}`)
	srv := httptest.NewServer(fake)
	 defer srv.Close()
	 fakeManifestEnv(t, srv)

	 dir := t.TempDir()
	 binaries := oneBinaryOnDisk(t, dir)
	 sink := &capturedLog{}
	 v, published, err := PublishIfNewer(context.Background(), binaries, "1.2.4", capturingLogger(sink))
	if err != nil {
	 t.Errorf("valid manifest under the cap must publish 1.2.4 fresh-clean: %v (log=%s)", fmt.Sprint(err), sinkDump(sink))
	 return
	}
	if !published || v != "1.2.4" {
	 t.Errorf("valid existing manifest 1.2.3 + new 1.2.4 must publish: got published=%v version=%q", published, v)
	}
	if fake.manifestPuts != 1 {
	 t.Errorf("manifest PUTs = %d, want 1 (the new manifest must be written)", fake.manifestPuts)
	}
	if !sink.Has("previous=1.2.3") {
	 t.Errorf("compare did not see the existing manifest: no previous=1.2.3 in log=%s", sinkDump(sink))
	}
	if got, ok := fake.objs[manifestObjectName]; !ok || !strings.Contains(string(got), "\"1.2.4\"") {
	 t.Errorf("stored manifest after publish: %q", string(got))
	}
}

// TestPublishIfNewer_ValidManifestLargerThanCapIsRejected closes the row-3
// gap the review found: the old cap test's body was NOT valid JSON, so a
// parse error also failed the read and that test passed even with the cap
// REMOVED. This body is syntactically VALID JSON over the cap, so the ONLY
// thing that can refuse it is the size cap itself: the error must name the
// cap, and under a no-cap mutant this test must FAIL (the read would
// succeed and 1.2.4 would publish over 1.2.3).
func TestPublishIfNewer_ValidManifestLargerThanCapIsRejected(t *testing.T) {
	fake := newManifestS3()
	pad := strings.Repeat("x", (1 << 20) + 8)
	fake.objs[manifestObjectName] = []byte(`{"version":"1.2.3","pad":"` + pad + `"}`)
	srv := httptest.NewServer(fake)
	 defer srv.Close()
	 fakeManifestEnv(t, srv)

	 dir := t.TempDir()
	 binaries := oneBinaryOnDisk(t, dir)
	 _, published, err := PublishIfNewer(context.Background(), binaries, "1.2.4", newTestLogger())
	if err == nil {
	 t.Errorf("valid-JSON manifest over the cap must be refused by the size cap, got none (cap removed?)")
	} else if !strings.Contains(fmt.Sprint(err), "larger than") {
	 t.Errorf("over-cap refusal must name the cap, got %v", err)
	}
	if published {
	 t.Errorf("published=true over an over-cap manifest; must refuse")
	}
	if fake.manifestPuts != 0 || fake.otherPuts != 0 {
	 t.Errorf("uploads over an over-cap manifest: manifestPuts=%d otherPuts=%d; want 0/0", fake.manifestPuts, fake.otherPuts)
	}
}

// TestPublishIfNewer_UnknownArtifactKindReturnsError is row 4: an unknown
// Artifact is a caller bug and must surface as an error, not a silent
// skip that main.go then logs as "release manifest already current".
func TestPublishIfNewer_UnknownArtifactKindReturnsError(t *testing.T) {
 binaries := []ReleaseBinary{
	 {OS: "windows", Arch: "amd64", Path: "/releases/mod-worker-windows-amd64.exe", Artifact: "msi"},
 }
 _, published, err := PublishIfNewer(context.Background(), binaries, "v1.0.42", newTestLogger())
 if err == nil {
	 t.Errorf("unknown artifact kind %q must return an error, got nil", "msi")
 }
 if published {
	 t.Errorf("published=true for an unknown artifact kind; must refuse")
 }
}
// decodeAWSChunked strips minio-go's AWS signed-chunk framing from a PUT body
// (each chunk is "<hex-size>;chunk-signature=<hex>\r\n<data>\r\n", stream ends
// with a zero-size chunk; net/http does not de-chunk because minio sends an
// explicit Content-Length). Same rule the executor fake's readAWSChunked uses.
func decodeAWSChunked(raw []byte) []byte {
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
		if off+1 < len(raw) && raw[off] == '\r' && raw[off+1] == '\n' {
			off += 2
		} else if off < len(raw) && raw[off] == '\n' {
			off++
		}
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

// decodeBase64Test is a tiny base64 decoder for the fixture's sig bytes (the
// toolchain's base64.Decode shape varies; a 12-line decoder keeps the test
// self-contained).
func decodeBase64Test(s string) []byte {
	const alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	var val int
	var nbits int
	var out []byte
	for _, c := range []byte(s) {
		if c == '=' || c == '\n' || c == '\r' {
			continue
		}
		v := strings.IndexByte(alpha, c)
		if v < 0 {
			continue
		}
		val = (val << 6) | v
		nbits += 6
		if nbits >= 8 {
			nbits -= 8
			out = append(out, uint8((val >> nbits) & 0xff))
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// FIX-GO-RELEASES-STALE-SIG (S-go-worker-releases.md row 12): the stale-.sig
// fixture + tests. fakeReleasesS3 is the in-memory object store for this
// package (publish_test.go had none before): it answers the minio-go wire
// shape (region probe, HEAD bucket for BucketExists, PUT/GET/HEAD object,
// DELETE for RemoveObject) with no live S3 and no versitygw.
// ---------------------------------------------------------------------------

type fakeReleasesS3 struct {
	mu       sync.Mutex
	bkt      string
	objs     map[string][]byte
	puts     int
	gets     int
	dels     int
	delDeny  bool // when set, DELETE answers 403 (removal-failure tests)
	putDeny  string // when set, PUTs of this exact key answer 403 (sig-upload failure)
}

func newFakeReleasesS3() *fakeReleasesS3 {
	return &fakeReleasesS3{bkt: bucket, objs: make(map[string][]byte)}
}

func (f *fakeReleasesS3) keyOf(r *http.Request) string {
	return strings.TrimPrefix(r.URL.Path, "/"+f.bkt+"/")
}

func (f *fakeReleasesS3) serveError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("X-Amz-Request-Id", "test")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(fmt.Sprintf(`<Error><Code>%s</Code><Message>%s</Message></Error>`, code, msg)))
}

func (f *fakeReleasesS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodGet && (r.URL.Path == "/"+f.bkt || r.URL.Path == "/"+f.bkt+"/") {
		if _, present := r.URL.Query()["location"]; present {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>`))
			return
		}
	}
	key := f.keyOf(r)
	switch r.Method {
	case http.MethodPut:
		if f.putDeny != "" && key == f.putDeny {
			f.serveError(w, http.StatusForbidden, "AccessDenied", "sig put denied")
			return
		}
		body, _ := io.ReadAll(r.Body)
		f.objs[key] = decodeAWSChunked(body)
		f.puts++
		w.Header().Set("ETag", `"abc"`)
		w.WriteHeader(http.StatusOK)
	case http.MethodHead:
		if key == "" { // BucketExists probe
			w.Header().Set("Content-Length", "0")
			w.WriteHeader(http.StatusOK)
			return
		}
		data, ok := f.objs[key]
		if !ok {
			f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Content-Length", strconv.Itoa(len(data)))
		w.Header().Set("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
		w.WriteHeader(http.StatusOK)
	case http.MethodGet:
		data, ok := f.objs[key]
		if !ok {
			f.serveError(w, http.StatusNotFound, "NoSuchKey", "not found")
			return
		}
		f.gets++
		w.Header().Set("ETag", `"abc"`)
		w.Header().Set("Content-Length", strconv.Itoa(len(data)))
		w.Header().Set("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
	case http.MethodDelete:
		if f.delDeny {
			f.serveError(w, http.StatusForbidden, "AccessDenied", "delete denied")
			return
		}
		delete(f.objs, key)
		f.dels++
		w.WriteHeader(http.StatusNoContent)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func (f *fakeReleasesS3) has(key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.objs[key] != nil
}

func (f *fakeReleasesS3) bytes(key string) []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	v, ok := f.objs[key]
	if !ok {
		return nil
	}
	return v
}

// releasesFakeClient points publish.go's minio client at the fake store.
func releasesFakeClient(t *testing.T, srv *httptest.Server) *minio.Client {
	t.Helper()
	client, err := minio.New(strings.TrimPrefix(srv.URL, "http://"), &minio.Options{
		Creds:  credentials.NewStaticV4("ak", "sk", ""),
		Secure: false,
	})
	if err != nil {
		t.Fatalf("minio.New: %v", err)
	}
	return client
}

// seedStaleSig pre-seeds the store with the PREVIOUS release's "<obj>.sig"
// (the stale state row 12 describes) and returns the binary's object name.
func seedStaleSig(f *fakeReleasesS3, objName string) {
	f.objs[selfupdate.SignatureURL(objName)] = []byte("OLD-SIGNATURE-BYTES")
}

func writeReleaseBinaryFile(t *testing.T) (string, []byte) {
	t.Helper()
	dir := t.TempDir()
	path := dir + "/mod-worker-linux-amd64"
	data := []byte("the new release bytes")
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatalf("write binary: %v", err)
	}
	return path, data
}

func newSigningKey(t *testing.T) *ecdsa.PrivateKey {
	t.Helper()
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	return k
}

// expectedSigB64 is the base64 DER ECDSA P-256 signature over SHA-256(data)
// that signAndUploadSig must upload (same scheme internal/selfupdate.VerifyArtifact
// checks).
func expectedSigB64(priv *ecdsa.PrivateKey, data []byte) string {
	digest := sha256.Sum256(data)
	der, err := ecdsa.SignASN1(rand.Reader, priv, digest[:])
	if err != nil {
		return ""
	}
	return base64.StdEncoding.EncodeToString(der)
}

// TestUploadBinary_NilSigningKeyRemovesStaleSig is stale-sig path 1: signingKey
// nil (Vault unreachable / no key) must NOT leave the previous release's .sig
// beside the new binary.
func TestUploadBinary_NilSigningKeyRemovesStaleSig(t *testing.T) {
	fake := newFakeReleasesS3()
	srv := httptest.NewServer(fake)
	defer srv.Close()
	client := releasesFakeClient(t, srv)

	objName := releaseObjectName("linux", "amd64")
	seedStaleSig(fake, objName)
	path, _ := writeReleaseBinaryFile(t)
	log := newTestLogger()
	sum, err := uploadBinary(context.Background(), client, ReleaseBinary{OS: "linux", Arch: "amd64", Path: path}, nil, log)
	if err != nil {
		t.Fatalf("uploadBinary: %v", err)
	}
	if sum == "" {
		t.Error("expected a sha256 for the uploaded binary")
	}
	if fake.has(selfupdate.SignatureURL(objName)) {
		t.Errorf("stale .sig still present after an unsigned publish; the new binary would be rejected against it")
	}
	if !fake.has(objName) {
		t.Error("new binary must still be uploaded")
	}
	if fake.dels != 1 {
		t.Errorf("expected exactly 1 RemoveObject, got %d", fake.dels)
	}
}

// TestUploadBinary_SignUploadFailureRemovesStaleSig is stale-sig path 2: the
// .sig PUT fails (or signing fails) -> the old .sig is still removed, the
// publish of the BINARY still succeeds, and a warning is logged.
func TestUploadBinary_SignUploadFailureRemovesStaleSig(t *testing.T) {
	fake := newFakeReleasesS3()
	objName := releaseObjectName("linux", "amd64")
	sigName := selfupdate.SignatureURL(objName)
	seedStaleSig(fake, objName)
	fake.putDeny = sigName // the new sig upload fails
	srv := httptest.NewServer(fake)
	defer srv.Close()
	client := releasesFakeClient(t, srv)

	var logBuf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&logBuf, nil))
	path, data := writeReleaseBinaryFile(t)
	key := newSigningKey(t)
	sum, err := uploadBinary(context.Background(), client, ReleaseBinary{OS: "linux", Arch: "amd64", Path: path}, key, log)
	if err != nil {
		t.Fatalf("a failed sig upload must not fail the binary publish: %v", err)
	}
	if sum == "" {
		t.Error("expected a sha256 for the uploaded binary")
	}
	if fake.has(sigName) {
		t.Errorf("old .sig must be gone when the new sig upload failed")
	}
	if fake.dels != 1 {
		t.Errorf("expected exactly 1 RemoveObject, got %d", fake.dels)
	}
	if !strings.Contains(logBuf.String(), "skipping signature: upload failed") {
		t.Errorf("a warning must be logged when the sig upload fails; log was: %s", logBuf.String())
	}
	_ = data
}

// TestUploadBinary_SigningSucceedsOverwritesSig (c): signing succeeds -> the sig
// object holds the NEW signature, never the pre-seeded old bytes.
func TestUploadBinary_SigningSucceedsOverwritesSig(t *testing.T) {
	fake := newFakeReleasesS3()
	objName := releaseObjectName("linux", "amd64")
	sigName := selfupdate.SignatureURL(objName)
	seedStaleSig(fake, objName)
	srv := httptest.NewServer(fake)
	defer srv.Close()
	client := releasesFakeClient(t, srv)

	path, _ := writeReleaseBinaryFile(t)
	key := newSigningKey(t)
	sum, err := uploadBinary(context.Background(), client, ReleaseBinary{OS: "linux", Arch: "amd64", Path: path}, key, newTestLogger())
	if err != nil {
		t.Fatalf("uploadBinary: %v", err)
	}
	if sum == "" {
		t.Error("expected a sha256 for the uploaded binary")
	}
	got := fake.bytes(sigName)
	if got == nil {
		t.Error("a signed publish must leave a .sig object")
		return
	}
	if string(got) == "OLD-SIGNATURE-BYTES" {
		t.Errorf("sig object still holds the OLD bytes; the new signature must overwrite it")
	}
	sigBytes := decodeBase64Test(string(got))
	if len(sigBytes) == 0 || sigBytes[0] != 0x30 {
		t.Errorf("sig object must be base64 DER (ASN.1 SEQUENCE); got %q", string(got))
	}
	if fake.dels != 0 {
		t.Errorf("a successful signing path must not RemoveObject; got %d", fake.dels)
	}
	if fake.dels != 0 {
		t.Errorf("a successful signing path must not RemoveObject; got %d", fake.dels)
	}
}

// TestUploadBinary_StaleSigRemovalFailureDoesNotAbort (d): RemoveObject fails ->
// publish still succeeds; the failure is logged at Warn.
func TestUploadBinary_StaleSigRemovalFailureDoesNotAbort(t *testing.T) {
	fake := newFakeReleasesS3()
	objName := releaseObjectName("linux", "amd64")
	seedStaleSig(fake, objName)
	fake.delDeny = true // RemoveObject answers 403
	srv := httptest.NewServer(fake)
	defer srv.Close()
	client := releasesFakeClient(t, srv)

	path, _ := writeReleaseBinaryFile(t)
	var logBuf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&logBuf, nil))
	sum, err := uploadBinary(context.Background(), client, ReleaseBinary{OS: "linux", Arch: "amd64", Path: path}, nil, log)
	if err != nil {
		t.Fatalf("a failed stale-sig removal must not abort the publish: %v", err)
	}
	if sum == "" {
		t.Error("expected a sha256 for the uploaded binary")
	}
	if !fake.has(selfupdate.SignatureURL(objName)) {
		t.Error("when removal fails the old .sig stays (best-effort); it must not be dropped by luck")
	}
	if !strings.Contains(logBuf.String(), "stale release signature removal failed") {
		t.Errorf("a warning must be logged when removal fails; log was: %s", logBuf.String())
	}
}

// TestUploadArtifact_NilSigningKeyRemovesStaleSig: the artifact path (installer/app)
// carries the identical stale-sig rule.
func TestUploadArtifact_NilSigningKeyRemovesStaleSig(t *testing.T) {
	fake := newFakeReleasesS3()
	objName, err := artifactObjectName("installer", "v9.9.9")
	if err != nil {
		t.Fatalf("artifactObjectName: %v", err)
	}
	seedStaleSig(fake, objName)
	srv := httptest.NewServer(fake)
	defer srv.Close()
	client := releasesFakeClient(t, srv)

	path, _ := writeReleaseBinaryFile(t)
	var m Manifest
	err = uploadArtifact(context.Background(), client, ReleaseBinary{OS: "windows", Arch: "amd64", Artifact: "installer", Path: path}, "v9.9.9", &m, nil, newTestLogger())
	if err != nil {
		t.Fatalf("uploadArtifact: %v", err)
	}
	if fake.has(selfupdate.SignatureURL(objName)) {
		t.Errorf("stale artifact .sig still present after an unsigned publish")
	}
	if m.InstallerSHA256 == "" {
		t.Error("artifact checksum must still be recorded")
	}
}
