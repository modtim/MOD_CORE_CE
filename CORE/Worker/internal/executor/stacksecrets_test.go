package executor

// GX.S10b — unit tests for stack file secrets + storage files. No docker daemon
// or network needed: the compose shell-out is faked via the execCommandContext
// seam (and dockerCLI stubbed), and storage downloads hit a local httptest server.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/mount"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// lastUpComposeBytes is the compose file content as it stood when the fake
// "up" ran; runStackOp removes the workspace before returning.
var lastUpComposeBytes []byte

// withFakeExecRecord swaps the exec seam for a fake that records the argv of
// every "up" call and runs a controlled `sh -c` for output. envFile, when set,
// captures the compose child's full environment to that path (so a test can
// assert MOD_SECRETS_DIR / MOD_FILES_DIR were exported). Returns restore.
func withFakeExecRecord(t *testing.T, envFile string, out string, upCalls *[][]string) func() {
	t.Helper()
	orig := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if strings.Contains(strings.Join(arg, " "), "compose") && strings.Contains(strings.Join(arg, " "), "up -d") {
			if upCalls != nil {
				*upCalls = append(*upCalls, arg)
			}
			for i, a := range arg {
				if a == "-f" && i+1 < len(arg) {
					lastUpComposeBytes, _ = os.ReadFile(arg[i+1])
				}
			}
		}
		prog := "sh"
		var script string
		if envFile != "" {
			script = "env > " + envFile + "; " + out
		} else {
			script = out
		}
		return exec.CommandContext(ctx, prog, "-c", script)
	}
	return func() { execCommandContext = orig }
}

// stubDockerCLI swaps the docker-CLI seam for a no-op (so the deploy/teardown
// path's network/inspect calls don't need a daemon) and returns restore.
func stubDockerCLI() func() {
	orig := dockerCLI
	dockerCLI = func(_ context.Context, args ...string) (string, error) { return "", nil }
	return func() { dockerCLI = orig }
}

// withContainerized swaps the 13.5b containerized-worker seam so a deploy test
// runs as if the worker is a host process (the go-test container's own cgroup
// would otherwise trip the fail-closed guard). Returns restore.
func withContainerized(containerized bool) func() {
	orig := detectContainerized
	detectContainerized = func() bool { return containerized }
	// The verified guard caches its decision per process; a test that flips the
	// containerized outcome must clear that cache or it reads the previous test's
	// verdict (an earlier allowed host-process run would leak into a deny test).
	resetFileSecretsStatusCache()
	return func() {
		detectContainerized = orig
		resetFileSecretsStatusCache()
	}
}

// withMountinfoFSType swaps the mountinfo-fstype seam to a fixed answer, so a
// test controls whether containerSecretsRoot looks like a tmpfs mount (or is
// not a mount point at all) without depending on the test host's real mounts.
func withMountinfoFSType(fstype string, ok bool) func() {
	orig := parseMountinfoFSType
	parseMountinfoFSType = func(string) (string, bool) { return fstype, ok }
	return func() { parseMountinfoFSType = orig }
}

// withContainerInspect swaps the self-inspect seam to a faked daemon response
// (or error) for the worker's own container.
func withContainerInspect(resp types.ContainerJSON, err error) func() {
	orig := containerInspect
	containerInspect = func(context.Context, string) (types.ContainerJSON, error) {
		return resp, err
	}
	return func() { containerInspect = orig }
}

// A. payload JSON round-trip: the wire shape the API sends must survive encode.
func TestStackSecretStoragePayloadRoundTrip(t *testing.T) {
	in := transport.TaskPayload{
		StackOp:      "deploy",
		StackProject: "strata",
		ComposeSpec:  "services: {}\nsecrets:\n  k: {file: \"${MOD_SECRETS_DIR}/k\"}",
		SecretFiles: []transport.StackSecretFile{
			{Name: "k", ContentB64: base64.StdEncoding.EncodeToString([]byte("v")), Mode: "0400"},
		},
		StorageFiles: []transport.StackStorageFile{
			{Name: "cfg.yaml", URL: "https://s/bucket/cfg.yaml?sig=ABC", Size: 5, SHA256: "deadbeef", Mode: "0444"},
		},
	}
	raw, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out transport.TaskPayload
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(out.SecretFiles) != 1 || out.SecretFiles[0].Name != "k" || out.SecretFiles[0].Mode != "0400" {
		t.Fatalf("secret_files round-trip wrong: %+v", out.SecretFiles)
	}
	sf := out.StorageFiles[0]
	if sf.Name != "cfg.yaml" || sf.Size != 5 || sf.SHA256 != "deadbeef" || sf.Mode != "0444" {
		t.Fatalf("storage_files round-trip wrong: %+v", sf)
	}
	// The query string (the presign signature) must be intact on the wire.
	if !strings.Contains(sf.URL, "sig=ABC") {
		t.Fatalf("URL lost its query on round-trip: %s", sf.URL)
	}
}

// B. secrets-dir fallback: with no tmpfs root present, the on-disk dir is used.
func TestStackSecretsDirForFallback(t *testing.T) {
	if _, err := os.Stat(stackSecretsDirBase); err == nil {
		t.Skipf("%s exists on this host; the tmpfs path is exercised elsewhere", stackSecretsDirBase)
	}
	// Force the host-process branch: a containerized worker always writes to the
	// same-path bind root, not the /dev/shm-or-on-disk fallback this test probes.
	defer withContainerized(false)()
	got := stackSecretsDirFor("proj-x", "/tmp/stack-root")
	want := filepath.Join("/tmp/stack-root", ".mod-secrets")
	if got != want {
		t.Fatalf("fallback dir = %q, want %q", got, want)
	}
}

// A containerized worker must write secrets to the SAME-PATH bind root, never
// the on-disk fallback — secret bytes must not touch durable disk.
func TestStackSecretsDirForContainerizedUsesBindRoot(t *testing.T) {
	defer withContainerized(true)()
	got := stackSecretsDirFor("proj-x", "/tmp/stack-root")
	want := filepath.Join(containerSecretsRoot, "proj-x")
	if got != want {
		t.Fatalf("containerized secrets dir = %q, want %q", got, want)
	}
}

// C. file modes: secret files land 0400, the dir 0700.
func TestWriteStackSecretsModes(t *testing.T) {
	dir := t.TempDir()
	d, vals, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "a", ContentB64: base64.StdEncoding.EncodeToString([]byte("secret-a")), Mode: "0400"},
	})
	if err != nil {
		t.Fatalf("writeStackSecrets: %v", err)
	}
	if d != dir {
		t.Fatalf("returned dir %q != %q", d, dir)
	}
	if len(vals) != 1 || vals[0] != "secret-a" {
		t.Fatalf("secret values not returned for redaction: %q", vals)
	}
	if fi, err := os.Stat(dir); err != nil {
		t.Fatalf("stat dir: %v", err)
	} else if fi.Mode().Perm() != 0o700 {
		t.Fatalf("dir mode = %o, want 0700", fi.Mode().Perm())
	}
	if fi, err := os.Stat(filepath.Join(dir, "a")); err != nil {
		t.Fatalf("stat secret: %v", err)
	} else if fi.Mode().Perm() != 0o400 {
		t.Fatalf("file mode = %o, want 0400", fi.Mode().Perm())
	}
}

// D. atomic overwrite: a redeploy that rewrites a secret replaces it cleanly.
func TestWriteStackSecretsAtomicOverwrite(t *testing.T) {
	dir := t.TempDir()
	if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "k", ContentB64: base64.StdEncoding.EncodeToString([]byte("old")), Mode: "0400"},
	}); err != nil {
		t.Fatalf("first write: %v", err)
	}
	if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "k", ContentB64: base64.StdEncoding.EncodeToString([]byte("new")), Mode: "0400"},
	}); err != nil {
		t.Fatalf("overwrite: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(dir, "k"))
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(data) != "new" {
		t.Fatalf("content = %q, want new value", data)
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".tmp-") {
			t.Fatalf("leftover temp file: %s", e.Name())
		}
	}
}

// B86i-g — a prior bad deploy (or docker auto-creating a bind-mount source
// that didn't exist yet) can leave the secret's target path as an empty
// directory. writeStackSecrets must replace EXACTLY that path with the real
// secret file, not error out and not touch anything else under dir.
func TestWriteStackSecretsReplacesStrayDirectory(t *testing.T) {
	dir := t.TempDir()
	strayPath := filepath.Join(dir, "MOD_CA_CERT")
	if err := os.MkdirAll(strayPath, 0o755); err != nil {
		t.Fatalf("seed stray dir: %v", err)
	}
	// An unrelated sibling file must survive untouched.
	sibling := filepath.Join(dir, "mod_identity_token")
	if err := os.WriteFile(sibling, []byte("untouched"), 0o400); err != nil {
		t.Fatalf("seed sibling: %v", err)
	}

	if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "MOD_CA_CERT", ContentB64: base64.StdEncoding.EncodeToString([]byte("-----BEGIN CERTIFICATE-----")), Mode: "0444"},
	}); err != nil {
		t.Fatalf("write over stray dir: %v", err)
	}

	fi, err := os.Lstat(strayPath)
	if err != nil {
		t.Fatalf("lstat: %v", err)
	}
	if fi.IsDir() {
		t.Fatalf("MOD_CA_CERT is still a directory")
	}
	data, err := os.ReadFile(strayPath)
	if err != nil {
		t.Fatalf("read replaced file: %v", err)
	}
	if string(data) != "-----BEGIN CERTIFICATE-----" {
		t.Fatalf("content = %q", data)
	}
	if siblingData, err := os.ReadFile(sibling); err != nil || string(siblingData) != "untouched" {
		t.Fatalf("sibling file disturbed: data=%q err=%v", siblingData, err)
	}
}

// B86i-g — /proc/1/cgroup's cgroup-v2 unified form ("0::/") never contains
// "docker"/"libcontainer" (only the legacy v1 hierarchy's path segment did),
// which made insideContainer() report false for an ACTUALLY-containerized
// worker on a cgroup-v2 host. /.dockerenv, present in every Docker container
// regardless of cgroup version, must take priority and decide it alone.
func TestInsideContainerFromSignalsCgroupV2(t *testing.T) {
	cases := []struct {
		name            string
		dockerenvExists bool
		cgroup          string
		want            bool
	}{
		{"dockerenv present, cgroup v2 unified (the regression)", true, "0::/\n", true},
		{"dockerenv present, empty cgroup file", true, "", true},
		{"no dockerenv, legacy cgroup v1 docker path", false, "1:name=systemd:/docker/abc123\n", true},
		{"no dockerenv, legacy cgroup v1 libcontainer path", false, "1:name=systemd:/libcontainer/abc123\n", true},
		{"no dockerenv, cgroup v2 unified — genuinely bare metal", false, "0::/\n", false},
		{"no dockerenv, unreadable cgroup", false, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := insideContainerFromSignals(tc.dockerenvExists, tc.cgroup); got != tc.want {
				t.Fatalf("insideContainerFromSignals(%v, %q) = %v, want %v",
					tc.dockerenvExists, tc.cgroup, got, tc.want)
			}
		})
	}
}

// E. storage download: sha verified + mode, skip-when-unchanged, sha mismatch,
// and size-cap all against a local httptest server.
func TestDownloadStackFiles(t *testing.T) {
	content := []byte("hello-storage")
	sum := sha256.Sum256(content)
	sumHex := hex.EncodeToString(sum[:])

	mux := http.NewServeMux()
	var hits int
	mux.HandleFunc("/ok", func(w http.ResponseWriter, r *http.Request) {
		hits++
		_, _ = w.Write(content)
	})
	mux.HandleFunc("/big", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(bytes.Repeat([]byte{1}, 100))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	// E1. happy path: sha verified, mode 0444.
	dir := t.TempDir() + "/.mod-files"
	if _, err := downloadStackFiles(context.Background(), dir, []transport.StackStorageFile{
		{Name: "f.txt", URL: srv.URL + "/ok", Size: int64(len(content)), SHA256: sumHex, Mode: "0444"},
	}); err != nil {
		t.Fatalf("happy path: %v", err)
	}
	if fi, err := os.Stat(filepath.Join(dir, "f.txt")); err != nil {
		t.Fatalf("stat: %v", err)
	} else if fi.Mode().Perm() != 0o444 {
		t.Fatalf("mode = %o, want 0444", fi.Mode().Perm())
	}
	if hits != 1 {
		t.Fatalf("downloaded %d times, want 1", hits)
	}

	// E2. skip-when-unchanged: same sha already on disk -> no second fetch.
	if _, err := downloadStackFiles(context.Background(), dir, []transport.StackStorageFile{
		{Name: "f.txt", URL: srv.URL + "/ok", Size: int64(len(content)), SHA256: sumHex, Mode: "0444"},
	}); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if hits != 1 {
		t.Fatalf("downloaded %d times after unchanged re-run, want still 1 (skipped)", hits)
	}

	// E3. sha mismatch: wrong sha must fail closed and name the file only.
	dir2 := t.TempDir() + "/.mod-files"
	_, err := downloadStackFiles(context.Background(), dir2, []transport.StackStorageFile{
		{Name: "f.txt", URL: srv.URL + "/ok", Size: int64(len(content)), SHA256: strings.Repeat("0", 64), Mode: "0444"},
	})
	if err == nil {
		t.Fatal("sha mismatch should fail")
	}
	if !strings.Contains(err.Error(), "sha256 mismatch") || !strings.Contains(err.Error(), "f.txt") {
		t.Fatalf("mismatch error should name the file: %v", err)
	}

	// E4. size cap: server sends more than the cap -> abort, and the error must
	// NOT leak the raw URL query string (the presign signature).
	dir3 := t.TempDir() + "/.mod-files"
	_, err = downloadStackFiles(context.Background(), dir3, []transport.StackStorageFile{
		{Name: "big.bin", URL: srv.URL + "/big?secret-sig=TOPSECRET", Size: 10, Mode: "0444"},
	})
	if err == nil {
		t.Fatal("size overrun should fail")
	}
	if strings.Contains(err.Error(), "TOPSECRET") || strings.Contains(err.Error(), "?secret-sig") {
		t.Fatalf("error leaked the presigned URL query: %v", err)
	}
	if !strings.Contains(err.Error(), "big.bin") {
		t.Fatalf("size error should name the file: %v", err)
	}
}

// F. URL redaction in error messages: scheme://host/path survives, query not.
func TestRedactURLStripsSignature(t *testing.T) {
	u := "https://example.com/tenant-7-files/model.yaml?X-Amz-Signature=abcd1234&X-Amz-Expires=900"
	got := redactURL(u)
	if strings.Contains(got, "X-Amz-Signature") || strings.Contains(got, "abcd1234") {
		t.Fatalf("redacted URL leaked the signature: %s", got)
	}
	if !strings.HasPrefix(got, "https://example.com/tenant-7-files/model.yaml") {
		t.Fatalf("redacted URL lost scheme/host/path: %s", got)
	}
}

// G. cleanup on teardown: the project's on-disk secret + file dirs are removed.
func TestCleanupStackFilesRemovesDirs(t *testing.T) {
	proj := fmt.Sprintf("cleanme-%d", os.Getpid())
	root := stackRootDir(proj)
	os.MkdirAll(filepath.Join(root, ".mod-secrets"), 0o700)
	os.WriteFile(filepath.Join(root, ".mod-secrets", "k"), []byte("x"), 0o400)
	os.MkdirAll(filepath.Join(root, ".mod-files"), 0o755)
	os.WriteFile(filepath.Join(root, ".mod-files", "f"), []byte("y"), 0o444)

	cleanupStackFiles(proj)

	if _, err := os.Stat(filepath.Join(root, ".mod-secrets")); !os.IsNotExist(err) {
		t.Fatalf(".mod-secrets still present after cleanup")
	}
	if _, err := os.Stat(filepath.Join(root, ".mod-files")); !os.IsNotExist(err) {
		t.Fatalf(".mod-files still present after cleanup")
	}
	os.RemoveAll(root)
}

// H. env vars on the compose command: MOD_SECRETS_DIR / MOD_FILES_DIR are
// appended to the child env, and secret values are redacted from output.
func TestComposeCmdGetsStackEnvAndRedacts(t *testing.T) {
	envFile := filepath.Join(t.TempDir(), "env.txt")
	restore := withFakeExecRecord(t, envFile, `echo "up ok"`, nil)
	defer restore()
	defer stubDockerCLI()()

	e := &Executor{}
	secretsDir := "/dev/shm/mod-stack-secrets/strata"
	filesDir := "/var/lib/mod-worker/stacks/strata/.mod-files"
	env := stackEnvForCompose(secretsDir, filesDir)
	out, err := e.composeCmdRedacted(context.Background(), Callbacks{},
		"strata", "/tmp/none.yml", []string{"super-secret-value"}, env, "up", "-d")
	if err != nil {
		t.Fatalf("compose up: %v", err)
	}
	if !strings.Contains(out, "up ok") {
		t.Fatalf("expected compose output, got %q", out)
	}
	raw, err := os.ReadFile(envFile)
	if err != nil {
		t.Fatalf("read captured env: %v", err)
	}
	childEnv := string(raw)
	if !strings.Contains(childEnv, "MOD_SECRETS_DIR="+secretsDir) {
		t.Fatalf("MOD_SECRETS_DIR not exported to compose env: %s", childEnv)
	}
	if !strings.Contains(childEnv, "MOD_FILES_DIR="+filesDir) {
		t.Fatalf("MOD_FILES_DIR not exported to compose env: %s", childEnv)
	}
	// The inherited env must still be present (we append, not replace).
	if !strings.Contains(childEnv, "PATH=") {
		t.Fatalf("inherited env lost; only got: %s", childEnv)
	}
}

// TestComposeCmdRedactsSecretValue proves a secret value in compose output is
// scrubbed before it reaches the caller (the redaction list is honoured).
func TestComposeCmdRedactsSecretValue(t *testing.T) {
	restore := withFakeExecRecord(t, "",
		`echo "ERROR: could not read /run/secrets/k super-secret-value"`, nil)
	defer restore()

	out, err := e_for_test().composeCmdRedacted(context.Background(), Callbacks{},
		"strata", "", []string{"super-secret-value"}, nil, "up", "-d")
	if err != nil {
		t.Fatalf("compose up: %v", err)
	}
	if strings.Contains(out, "super-secret-value") {
		t.Fatalf("secret value leaked in compose output: %s", out)
	}
	if !strings.Contains(out, "***REDACTED***") {
		t.Fatalf("expected redaction placeholder in output: %s", out)
	}
}

// TestDeployMaterializesSecretsAndEnv is the end-to-end deploy path: the
// payload's secret_files are written, MOD_SECRETS_DIR is exported to compose,
// and teardown removes the dir again.
func TestDeployMaterializesSecretsAndEnv(t *testing.T) {
	proj := fmt.Sprintf("deploysec-%d", os.Getpid())
	secretVal := "deploy-secret-" + fmt.Sprint(os.Getpid())
	const spec = "services:\n  app:\n    image: busybox\n    command: echo hi\n"

	envFile := filepath.Join(t.TempDir(), "env.txt")
	restore := withFakeExecRecord(t, envFile, `echo "deployed"`, nil)
	defer restore()
	defer stubDockerCLI()()
	defer withContainerized(false)()

	e := &Executor{}
	payload := transport.TaskPayload{
		StackOp: "deploy", StackProject: proj, ComposeSpec: spec,
		SecretFiles: []transport.StackSecretFile{
			{Name: "k", ContentB64: base64.StdEncoding.EncodeToString([]byte(secretVal)), Mode: "0400"},
		},
	}
	task := &transport.Task{Payload: payload}
	if _, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{}); err != nil {
		t.Fatalf("deploy runStackOp: %v", err)
	}

	raw, err := os.ReadFile(envFile)
	if err != nil {
		t.Fatalf("read captured env: %v", err)
	}
	dir := ""
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(line, "MOD_SECRETS_DIR=") {
			dir = strings.TrimPrefix(line, "MOD_SECRETS_DIR=")
		}
	}
	if dir == "" {
		t.Fatalf("MOD_SECRETS_DIR not exported to compose env: %s", string(raw))
	}
	data, err := os.ReadFile(filepath.Join(dir, "k"))
	if err != nil {
		t.Fatalf("secret file not materialized at %s: %v", dir, err)
	}
	if string(data) != secretVal {
		t.Fatalf("secret file content = %q, want %q", data, secretVal)
	}

	// Teardown removes the materialized dir.
	td := &transport.Task{Payload: transport.TaskPayload{StackOp: "teardown", StackProject: proj}}
	if _, err := e.runStackOp(context.Background(), td, nil, time.Now(), Callbacks{}); err != nil {
		t.Fatalf("teardown runStackOp: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "k")); !os.IsNotExist(err) {
		t.Fatalf("secret file still present after teardown: %s", filepath.Join(dir, "k"))
	}
	os.RemoveAll(stackRootDir(proj))
}

// TestStackEnvForCompose checks the pure helper produces the right var set.
func TestStackEnvForCompose(t *testing.T) {
	got := stackEnvForCompose("/dev/shm/mod-stack-secrets/strata", "/var/lib/mod-worker/stacks/strata/.mod-files")
	want := []string{
		"MOD_SECRETS_DIR=/dev/shm/mod-stack-secrets/strata",
		"MOD_FILES_DIR=/var/lib/mod-worker/stacks/strata/.mod-files",
	}
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("stackEnvForCompose = %v, want %v", got, want)
	}
	if gotEmpty := stackEnvForCompose("", ""); len(gotEmpty) != 0 {
		t.Fatalf("empty dirs should produce no env: %v", gotEmpty)
	}
}

// TestParseFileMode covers the mode parsing edge cases.
func TestParseFileMode(t *testing.T) {
	cases := map[string]os.FileMode{
		"0400":  0o400,
		"400":   0o400,
		"0o444": 0o444,
		"":      0o400,
		"bad":   0o400, // unparseable -> default
	}
	def := os.FileMode(0o400)
	for in, want := range cases {
		if got := parseFileMode(in, def); got != want {
			t.Errorf("parseFileMode(%q) = %o, want %o", in, got, want)
		}
	}
	if got := secretFileMode("0666"); got&0o222 != 0 {
		t.Errorf("secretFileMode(0666) kept a write bit: %o", got)
	}
}

// assertNoHostileBits fails when a clamped mode still carries a write bit, an
// exec bit, a setuid/setgid/sticky bit, or any bit above 0777.
func assertNoHostileBits(t *testing.T, in string, got os.FileMode) {
	t.Helper()
	if got&0o222 != 0 {
		t.Errorf("mode %q: write bit survived (%o)", in, got)
	}
	if got&0o111 != 0 {
		t.Errorf("mode %q: exec bit survived (%o)", in, got)
	}
	if got&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 || got&^0o777 != 0 {
		t.Errorf("mode %q: setid/sticky or high bit survived (%o)", in, got)
	}
}

// FIX-GO-STACKSECRETS-MODE-CLAMP: hostile secret-file modes are clamped to at
// most 0444. Expected values are derived by hand from the mask (& 0444), not by
// calling secretFileMode again. "" is unset, so the parse default 0400 applies.
func TestSecretFileModeClampsHostileModes(t *testing.T) {
	cases := []struct {
		mode string
		want os.FileMode
	}{
		{"0777", 0o444},
		{"04755", 0o444},
		{"0666", 0o444},
		{"0644", 0o444},
		{"0444", 0o444},
		// "0" strips to "" in parseFileMode and takes the default 0400.
		{"0", 0o400},
		{"", 0o400},
	}
	for _, c := range cases {
		got := secretFileMode(c.mode)
		if got != c.want {
			t.Errorf("secretFileMode(%q) = %o, want %o", c.mode, got, c.want)
		}
		assertNoHostileBits(t, c.mode, got)
	}
}

// FIX-GO-STACKSECRETS-MODE-CLAMP: storage-file modes are clamped to at most
// 0644 (no group/other write, no exec, no setid/sticky). Mask: & 0644.
func TestStorageFileModeClampsHostileModes(t *testing.T) {
	cases := []struct {
		mode string
		want os.FileMode
	}{
		{"0777", 0o644},
		{"0666", 0o644},
		{"0640", 0o640},
		{"04755", 0o644},
		// "0" strips to "" in parseFileMode and takes the default 0444.
		{"0", 0o444},
		{"", 0o444},
	}
	for _, c := range cases {
		got := storageFileMode(c.mode)
		if got != c.want {
			t.Errorf("storageFileMode(%q) = %o, want %o", c.mode, got, c.want)
		}
		// Owner rw is allowed; group/other write, exec, setid/sticky are not.
		if got&0o022 != 0 || got&0o111 != 0 || got&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 || got&^0o777 != 0 {
			t.Errorf("storageFileMode(%q) = %o: group/other write, exec or setid survived", c.mode, got)
		}
	}
}

// FIX-GO-STACKSECRETS-MODE-CLAMP: end to end. writeStackSecrets (the real
// function) writes a hostile-mode file; the on-disk mode must be the clamped
// value. The directory stays 0700 (unchanged by this fix).
func TestWriteStackSecretsClampsHostileModesOnDisk(t *testing.T) {
	cases := []struct {
		mode string
		want os.FileMode
	}{
		{"0777", 0o444},
		{"04755", 0o444},
	}
	for _, c := range cases {
		dir := filepath.Join(t.TempDir(), "secrets")
		content := base64.StdEncoding.EncodeToString([]byte("fake-value"))
		if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
			{Name: "MOD_HOSTILE_MODE", ContentB64: content, Mode: transport.StackFileMode(c.mode)},
		}); err != nil {
			t.Fatalf("mode %q: writeStackSecrets: %v", c.mode, err)
		}
		fi, err := os.Stat(filepath.Join(dir, "MOD_HOSTILE_MODE"))
		if err != nil {
			t.Fatalf("mode %q: stat: %v", c.mode, err)
		}
		if got := fi.Mode().Perm(); got != c.want {
			t.Errorf("mode %q: on-disk perm = %o, want %o", c.mode, got, c.want)
		}
		if fi.Mode()&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
			t.Errorf("mode %q: setid/sticky bit on disk (%v)", c.mode, fi.Mode())
		}
		di, err := os.Stat(dir)
		if err != nil {
			t.Fatalf("stat dir: %v", err)
		}
		if got := di.Mode().Perm(); got != 0o700 {
			t.Errorf("mode %q: dir perm = %o, want 700 (unchanged)", c.mode, got)
		}
	}
}

// 13.5b — the API's wire format sends each file secret's `mode` as a JSON
// NUMBER (python 0o400 marshals to 256). The string-typed Mode field used to
// make json.Unmarshal drop the whole element's mode (error swallowed at the
// task-decode site), so the API's explicit mode never reached the file.
func TestSecretFileModeSurvivesNumberAndStringWireForms(t *testing.T) {
	const b64 = "dg==" // "v"
	for name, raw := range map[string]string{
		"number": `{"name":"k","content_b64":"` + b64 + `","mode":256}`,
		"string": `{"name":"k","content_b64":"` + b64 + `","mode":"0400"}`,
		"0o-str": `{"name":"k","content_b64":"` + b64 + `","mode":"0o644"}`,
	} {
		var f transport.StackSecretFile
		if err := json.Unmarshal([]byte(raw), &f); err != nil {
			t.Fatalf("%s: unmarshal: %v", name, err)
		}
		if f.Name != "k" {
			t.Fatalf("%s: name = %q", name, f.Name)
		}
		if got, want := secretFileMode(string(f.Mode)), os.FileMode(0o400); name != "0o-str" {
			if got != want {
				t.Fatalf("%s: mode = %o, want %o (wire: %s)", name, got, want, f.Mode)
			}
		}
	}
}

// 13.5b — the secret value must never reach the compose bytes written to disk
// (only the ${MOD_SECRETS_DIR} interpolation), and a value that does show up
// in compose output must be redacted before it reaches the log batch or the
// task result. The fake compose command deliberately echoes the value to
// stderr to prove the redaction, not its absence.
func TestDeployFileSecretValueNotInComposeBytesOrLogs(t *testing.T) {
	proj := fmt.Sprintf("noval-%d", os.Getpid())
	secretVal := "leakcheck-" + fmt.Sprint(os.Getpid())
	spec := "services:\n  app:\n    image: busybox\n    secrets: [k]\nsecrets:\n  k:\n    file: ${MOD_SECRETS_DIR}/k\n"
	envFile := filepath.Join(t.TempDir(), "env.txt")
	var upCalls [][]string
	restore := withFakeExecRecord(t, envFile,
		`echo "compose-error: could not read value `+secretVal+`"`, &upCalls)
	defer restore()
	defer stubDockerCLI()()
	defer withContainerized(false)()

	var logLines []string
	e := &Executor{}
	payload := transport.TaskPayload{
		StackOp: "deploy", StackProject: proj, ComposeSpec: spec,
		SecretFiles: []transport.StackSecretFile{
			{Name: "k", ContentB64: base64.StdEncoding.EncodeToString([]byte(secretVal)), Mode: "0400"},
		},
	}
	res, err := e.runStackOp(context.Background(), &transport.Task{Payload: payload}, nil, time.Now(),
		Callbacks{OnLogBatch: func(msgs []string) { logLines = append(logLines, msgs...) }})
	if err != nil {
		t.Fatalf("deploy runStackOp: %v", err)
	}

	// Recover the compose file path from the recorded `up` argv.
	var composePath string
	for _, c := range upCalls {
		for i, a := range c {
			if a == "-f" && i+1 < len(c) {
				composePath = c[i+1]
			}
		}
	}
	if composePath == "" {
		t.Fatalf("no -f <composePath> in up argv: %v", upCalls)
	}
	composeBytes := lastUpComposeBytes
	if len(composeBytes) == 0 {
		t.Fatalf("compose file %s was not captured at up time", composePath)
	}
	if strings.Contains(string(composeBytes), secretVal) {
		t.Fatalf("secret value present in compose file bytes: %s", composeBytes)
	}
	if !strings.Contains(string(composeBytes), "${MOD_SECRETS_DIR}") {
		t.Fatalf("compose lost its MOD_SECRETS_DIR interpolation: %s", composeBytes)
	}

	logs := strings.Join(logLines, "\n") + "\n" + res.LogTail
	if strings.Contains(logs, secretVal) {
		t.Fatalf("secret value leaked into log output: %s", logs)
	}

	// Teardown clears the materialized file.
	td := &transport.Task{Payload: transport.TaskPayload{StackOp: "teardown", StackProject: proj}}
	if _, err := e.runStackOp(context.Background(), td, nil, time.Now(), Callbacks{}); err != nil {
		t.Fatalf("teardown runStackOp: %v", err)
	}
	os.RemoveAll(stackRootDir(proj))
}

// 13.5b — a containerized worker's /dev/shm is invisible to the docker daemon
// (probe: a bind source the daemon cannot find is auto-created as an EMPTY
// dir), so a file-secret deploy there would serve EMPTY secret files while
// reporting success. The deploy must fail closed and clean up instead.
func TestDeployFailsClosedForContainerizedWorkerWithFileSecrets(t *testing.T) {
	proj := fmt.Sprintf("containersec-%d", os.Getpid())
	restore := withFakeExecRecord(t, "", `echo "up ok"`, nil)
	defer restore()
	defer stubDockerCLI()()
	defer withContainerized(true)()
	// The worker is containerized but its secrets root is NOT a same-path host
	// bind (the daemon cannot see it) → the verified guard must DENY. Not a
	// mount point is the simplest unverified shape.
	defer withMountinfoFSType("", false)()

	e := &Executor{}
	payload := transport.TaskPayload{
		StackOp:      "deploy",
		StackProject: proj,
		ComposeSpec:  "services:\n  app:\n    image: busybox\n",
		SecretFiles: []transport.StackSecretFile{
			{Name: "k", ContentB64: base64.StdEncoding.EncodeToString([]byte("must-not-be-materialized"))},
		},
	}
	_, err := e.runStackOp(context.Background(), &transport.Task{Payload: payload}, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatalf("deploy on an unverified containerized worker with file secrets must fail closed")
	}
	// The message must name the exact bind to add (self-healing: operator fix).
	if !strings.Contains(err.Error(), containerSecretsRoot+":"+containerSecretsRoot) {
		t.Fatalf("error should name the required bind: %v", err)
	}
	// The materialized secret must be gone (fail-closed cleanup).
	if entries, _ := os.ReadDir(filepath.Join(containerSecretsRoot, proj)); len(entries) > 0 {
		t.Fatalf("secret files left behind after refused deploy: %v", entries)
	}
	os.RemoveAll(stackRootDir(proj))
}

// --- 13.5b verified-guard tests --------------------------------------------

// mountinfo parse: a tmpfs mount at the secrets root is recognized as tmpfs, an
// overlay mount is recognized as overlay (and thus NOT allowed), and a missing
// mount is reported as absent.
func TestFSTypeOfMountParsesTmpfsVsOverlay(t *testing.T) {
	cases := []struct {
		name      string
		mountinfo string
		wantFS    string
		wantOK    bool
	}{
		{
			name: "tmpfs at root",
			mountinfo: "123 1 8:1 / /run/mod-secrets rw,nosuid,nodev - tmpfs tmpfs rw\n" +
				"456 1 253:1 / / rw,relatime - ext4 /dev/sda1 rw\n",
			wantFS: "tmpfs", wantOK: true,
		},
		{
			name:      "overlay at root (container lower dir, no same-path tmpfs)",
			mountinfo: "123 1 0:42 / /run/mod-secrets rw,relatime - overlay overlay rw\n",
			wantFS:    "overlay", wantOK: true,
		},
		{
			name:      "root is a prefix of another mount, not itself a mount point",
			mountinfo: "123 1 8:1 / /run/mod-secrets/other rw - tmpfs tmpfs rw\n",
			wantFS:    "", wantOK: false,
		},
		{
			name:      "escaped mountpoint with a space is unescaped before matching",
			mountinfo: "123 1 8:1 / /run/mod%20secrets rw - tmpfs tmpfs rw\n",
			// This does NOT match containerSecretsRoot ("/run/mod-secrets") because
			// the mountpoint is literally "/run/mod secrets".
			wantFS: "", wantOK: false,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := writeMountinfoFixture(t, c.mountinfo)
			gotFS, gotOK := fstypeOfMountFile(f, containerSecretsRoot)
			if gotFS != c.wantFS || gotOK != c.wantOK {
				t.Fatalf("fstypeOfMountFile = (%q,%v), want (%q,%v)", gotFS, gotOK, c.wantFS, c.wantOK)
			}
		})
	}
}

// writeMountinfoFixture writes raw mountinfo text to a temp file and returns
// its path (removed on test cleanup).
func writeMountinfoFixture(t *testing.T, content string) string {
	t.Helper()
	f := filepath.Join(t.TempDir(), "mountinfo")
	if err := os.WriteFile(f, []byte(content), 0o600); err != nil {
		t.Fatalf("write mountinfo fixture: %v", err)
	}
	return f
}

// secretsRootBindSource replaced samePathHostBind (1564 — Docker Desktop/WSL2
// workers always report a translated proxy path, never the literal host
// path, as the bind's Source). See TestSecretsRootBindSourceMatchMismatch and
// the computeFileSecretsStatus probe tests in stacksecretbind_test.go.

// extractContainerID: a cgroup-v2 line carries the 64-hex id; a short name or
// non-hex token is rejected.
func TestExtractContainerID(t *testing.T) {
	const id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	if got := extractContainerID("0::/docker/" + id); got != id {
		t.Fatalf("extractContainerID(v2) = %q, want the full id", got)
	}
	if got := extractContainerID("12:cpuset:/"); got != "" {
		t.Fatalf("a non-hex cgroup line must yield no id, got %q", got)
	}
	if got := extractContainerID(""); got != "" {
		t.Fatalf("empty line must yield no id, got %q", got)
	}
}

// B86i-h — containerIDFromMountinfo: the cgroup v2 fallback for a host (this
// worker's own, observed) where /proc/self/cgroup is a bare "0::/" with no
// "/docker/<id>" segment at all, so extractContainerID has nothing to parse.
// dockerd's /etc/hostname (or hosts/resolv.conf) bind mount's root always
// carries the real 64-hex container id, regardless of cgroup version.
func TestContainerIDFromMountinfo(t *testing.T) {
	const id = "2ac6ee191732bc82e37579c2e7fb0f9e4cee22b03b3e59f80aada95471ddd2bd"
	if len(id) != 64 {
		t.Fatalf("test fixture id is %d hex chars, want 64", len(id))
	}
	mi := "14869 14860 8:48 /data/docker/containers/" + id + "/hostname /etc/hostname rw,relatime - ext4 /dev/sdd rw\n" +
		"14870 14860 8:48 /data/docker/containers/" + id + "/hosts /etc/hosts rw,relatime - ext4 /dev/sdd rw\n"
	f := filepath.Join(t.TempDir(), "mountinfo")
	if err := os.WriteFile(f, []byte(mi), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	if got := containerIDFromMountinfo(f); got != id {
		t.Fatalf("containerIDFromMountinfo = %q, want %q", got, id)
	}
	if got := containerIDFromMountinfo(filepath.Join(t.TempDir(), "missing")); got != "" {
		t.Fatalf("missing mountinfo file must yield no id, got %q", got)
	}
	if got := containerIDFromMountinfo(f + "-nonexistent"); got != "" {
		t.Fatalf("nonexistent path must yield no id, got %q", got)
	}
	noMatch := filepath.Join(t.TempDir(), "mountinfo2")
	if err := os.WriteFile(noMatch, []byte("123 1 8:1 / /run/mod-secrets rw - tmpfs tmpfs rw\n"), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	if got := containerIDFromMountinfo(noMatch); got != "" {
		t.Fatalf("mountinfo with none of the three target mounts must yield no id, got %q", got)
	}
}

// computeFileSecretsStatus: host process is always allowed; containerized is
// allowed only for tmpfs + verified same-path bind, denied otherwise, and the
// deny reason names the exact bind.
func TestComputeFileSecretsStatusAllowDeny(t *testing.T) {
	// (1) host process → allowed.
	{
		defer withContainerized(false)()
		st := computeFileSecretsStatus()
		if !st.Allowed {
			t.Fatalf("host process must be allowed, got %v", st.Reason)
		}
	}
	// (2) containerized, not a mount point → denied, naming the bind.
	{
		defer withContainerized(true)()
		defer withMountinfoFSType("", false)()
		st := computeFileSecretsStatus()
		if st.Allowed {
			t.Fatalf("non-mount-point secrets root must be denied")
		}
		if !strings.Contains(st.Reason, containerSecretsRoot+":"+containerSecretsRoot) {
			t.Fatalf("deny reason must name the bind, got %q", st.Reason)
		}
	}
	// (3) containerized, overlay (durable) → denied (secret must not hit disk).
	{
		defer withContainerized(true)()
		defer withMountinfoFSType("overlay", true)()
		if st := computeFileSecretsStatus(); st.Allowed {
			t.Fatalf("overlay (non-tmpfs) secrets root must be denied")
		}
	}
	// (4) containerized, tmpfs but NO bind to the secrets root at all → denied.
	{
		defer withContainerized(true)()
		defer withMountinfoFSType("tmpfs", true)()
		defer withContainerInspect(types.ContainerJSON{Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: "/elsewhere", Destination: "/elsewhere"},
		}}, nil)()
		if st := computeFileSecretsStatus(); st.Allowed {
			t.Fatalf("tmpfs with no bind to the secrets root must be denied")
		}
	}
	// (5) containerized, tmpfs + daemon error → denied (fail closed).
	{
		defer withContainerized(true)()
		defer withMountinfoFSType("tmpfs", true)()
		defer withContainerInspect(types.ContainerJSON{}, fmt.Errorf("daemon down"))()
		if st := computeFileSecretsStatus(); st.Allowed {
			t.Fatalf("daemon inspect error must fail closed")
		}
	}
	// (6) containerized, tmpfs, bind present (translated path) + probe proves
	// the daemon's view round-trips → allowed. 1564: the Source string is
	// deliberately NOT containerSecretsRoot (a Docker-Desktop-style translated
	// proxy path), proving the string shape is no longer what decides this.
	{
		defer withContainerized(true)()
		defer withMountinfoFSType("tmpfs", true)()
		defer withContainerInspect(types.ContainerJSON{Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: "/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/deadbeef", Destination: containerSecretsRoot},
		}}, nil)()
		defer withVerifySecretsRootProbe(func(context.Context, string, string) (bool, error) { return true, nil })()
		if st := computeFileSecretsStatus(); !st.Allowed {
			t.Fatalf("verified bind (probe success) must be allowed, got %q", st.Reason)
		}
	}
	// (7) containerized, tmpfs, bind present but the probe round-trip fails →
	// denied (fail closed: a bind that LOOKS present but doesn't actually see
	// the worker's files must not be trusted).
	{
		defer withContainerized(true)()
		defer withMountinfoFSType("tmpfs", true)()
		defer withContainerInspect(types.ContainerJSON{Mounts: []types.MountPoint{
			{Type: mount.TypeBind, Source: "/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/deadbeef", Destination: containerSecretsRoot},
		}}, nil)()
		defer withVerifySecretsRootProbe(func(context.Context, string, string) (bool, error) { return false, nil })()
		if st := computeFileSecretsStatus(); st.Allowed {
			t.Fatalf("a failed probe round-trip must be denied")
		}
	}
}

// fileSecretsDeployError reads the (cached) decision; on a deny it must surface
// the reason — which names the exact bind to add.
func TestFileSecretsDeployErrorNamesBind(t *testing.T) {
	defer withContainerized(true)()
	defer withMountinfoFSType("", false)()
	err := fileSecretsDeployError()
	if err == nil {
		t.Fatal("expected a fail-closed error")
	}
	if !strings.Contains(err.Error(), containerSecretsRoot+":"+containerSecretsRoot) {
		t.Fatalf("deploy error must name the required bind, got %q", err.Error())
	}
}

// e_for_test returns a bare executor for the seam-based tests (no docker client
// is touched: the compose shell-out is faked via execCommandContext).
func e_for_test() *Executor { return &Executor{} }

// ============================================================================
// 30.6.8 c8-W1 — identity/ secret sub-dir that survives a redeploy, plus the
// refresh_secret_files StackOp body. No docker needed: these drive the same
// helpers the op calls.
// ============================================================================

// inoOf returns the inode number of path (0 when the platform hides one, in
// which case the inode assertions below are skipped rather than faked).
func inoOf(path string) uint64 {
	fi, err := os.Stat(path)
	if err != nil {
		return 0
	}
	if st, ok := fi.Sys().(*syscall.Stat_t); ok {
		return st.Ino
	}
	return 0
}

func b64Of(s string) string {
	return base64.StdEncoding.EncodeToString([]byte(s))
}

func contentAt(dir, rel string) string {
	data, err := os.ReadFile(filepath.Join(dir, rel))
	if err != nil {
		return "<missing>"
	}
	return string(data)
}

func modeAt(dir, rel string) int {
	fi, err := os.Stat(filepath.Join(dir, rel))
	if err != nil {
		return -1
	}
	return int(fi.Mode().Perm())
}

// N1 accepts identity/mod_identity_token; N2 rejects every traversal form;
// N3 keeps today's rule for an unprefixed legacy name.
func TestC8W1SecretTargetName(t *testing.T) {
	want := filepath.Join(identitySubdir, "mod_identity_token")
	if secretTargetName("identity/mod_identity_token") != want {
		t.Fatalf("N1: identity/mod_identity_token must map to %q", want)
	}
	if secretTargetName("legacy_plain") != "legacy_plain" {
		t.Fatalf("N3: an unprefixed legacy name must keep today's rule")
	}
	for _, bad := range []string{
		"identity/a/b", "x/y", "identity/", "identity/..", "/identity/x",
		"identity\\x", "identity/../escape", "identity/./x", "IDENTITY/x",
		"identity/a b", "identity/../../etc/passwd", "identity/x\x00",
	} {
		if secretTargetName(bad) != "" {
			t.Fatalf("N2: %q must be rejected", bad)
		}
	}
}

// N1/N3. writeStackSecrets accepts a prefixed name (into the 0700 identity
// sub-dir) and an unprefixed legacy name side by side.
func TestC8W1WriteStackSecretsIdentitySubdir(t *testing.T) {
	dir := t.TempDir()
	_, vals, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-1"), Mode: "0400"},
		{Name: "legacy_plain", ContentB64: b64Of("leg-1"), Mode: "0400"},
	})
	if err != nil {
		t.Fatalf("writeStackSecrets: %v", err)
	}
	if len(vals) != 2 {
		t.Fatalf("redaction values: %d, want 2", len(vals))
	}
	sub := filepath.Join(dir, identitySubdir)
	if fi, err := os.Stat(sub); err != nil {
		t.Fatalf("identity sub-dir missing: %v", err)
	} else if fi.Mode().Perm() != 0o700 {
		t.Fatalf("identity sub-dir mode = %o, want 0700", fi.Mode().Perm())
	}
	rel := filepath.Join(identitySubdir, "mod_identity_token")
	if modeAt(dir, rel) != 0o400 {
		t.Fatalf("identity secret mode = %o, want 0400", modeAt(dir, rel))
	}
	if contentAt(dir, rel) != "tok-1" {
		t.Fatalf("identity secret content wrong")
	}
	if modeAt(dir, "legacy_plain") != 0o400 {
		t.Fatalf("legacy secret mode = %o, want 0400", modeAt(dir, "legacy_plain"))
	}
	if contentAt(dir, "legacy_plain") != "leg-1" {
		t.Fatalf("legacy secret content wrong")
	}
}

// C1. A redeploy over an existing secrets dir keeps the inodes of `dir` and
// `dir/identity` (the two a running container is bind-pinned to), removes a
// stale top-level secret and a stale `identity/old`, and writes the new set.
func TestC8W1RedeployKeepsBindInodes(t *testing.T) {
	dir := t.TempDir()
	if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-old"), Mode: "0400"},
		{Name: "identity/old", ContentB64: b64Of("stale-inner"), Mode: "0400"},
		{Name: "other", ContentB64: b64Of("stale-outer"), Mode: "0400"},
	}); err != nil {
		t.Fatalf("first generation: %v", err)
	}
	dirIno, subIno := inoOf(dir), inoOf(filepath.Join(dir, identitySubdir))
	if dirIno == 0 || subIno == 0 {
		t.Fatalf("no inode numbers on this platform — cannot prove the bind stays")
	}

	// The redeploy's generation clear + write, exactly as materializeStackFiles
	// now does it.
	newSet := []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-new"), Mode: "0400"},
	}
	if err := clearSecretsDirKeepingBinds(dir, newSet); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if _, _, err := writeStackSecrets(dir, newSet); err != nil {
		t.Fatalf("write: %v", err)
	}

	if inoOf(dir) != dirIno {
		t.Fatalf("C1: secrets dir inode changed %d -> %d (a dir bind would be orphaned)", dirIno, inoOf(dir))
	}
	if inoOf(filepath.Join(dir, identitySubdir)) != subIno {
		t.Fatalf("C1: identity/ inode changed %d -> %d", subIno, inoOf(filepath.Join(dir, identitySubdir)))
	}
	if modeAt(dir, "other") >= 0 {
		t.Fatalf("C1: stale top-level secret `other` survived the redeploy")
	}
	if modeAt(dir, filepath.Join(identitySubdir, "old")) >= 0 {
		t.Fatalf("C1: stale identity/old survived the redeploy")
	}
	if contentAt(dir, filepath.Join(identitySubdir, "mod_identity_token")) != "tok-new" {
		t.Fatalf("C1: new set not written")
	}
}

// R1. Refresh replaces the content of an existing identity/ secret and touches
// nothing else: a sibling's content AND mtime are unchanged.
func TestC8W1RefreshReplacesInPlace(t *testing.T) {
	dir := t.TempDir()
	if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-1"), Mode: "0400"},
		{Name: "identity/sibling", ContentB64: b64Of("sib-1"), Mode: "0400"},
	}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sib := filepath.Join(dir, identitySubdir, "sibling")
	fi, err := os.Stat(sib)
	if err != nil {
		t.Fatalf("stat sibling: %v", err)
	}
	sibMtime := fi.ModTime().UnixNano()
	n, vals, err := refreshStackSecretFiles(dir, []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-2"), Mode: "0400"},
	})
	if err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if n != 1 || len(vals) != 1 || vals[0] != "tok-2" {
		t.Fatalf("refresh count/values = %d/%d, want 1/1", n, len(vals))
	}
	if contentAt(dir, filepath.Join(identitySubdir, "mod_identity_token")) != "tok-2" {
		t.Fatalf("R1: content not replaced")
	}
	if contentAt(dir, filepath.Join(identitySubdir, "sibling")) != "sib-1" {
		t.Fatalf("R1: sibling content changed")
	}
	fi, err = os.Stat(sib)
	if err != nil {
		t.Fatalf("stat sibling after: %v", err)
	}
	if fi.ModTime().UnixNano() != sibMtime {
		t.Fatalf("R1: sibling mtime changed — refresh touched a file it should not")
	}
}

// R2. Refresh never creates a file: a missing target errors and leaves nothing
// behind.
func TestC8W1RefreshNeverCreates(t *testing.T) {
	dir := t.TempDir()
	if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-1"), Mode: "0400"},
	}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	_, _, err := refreshStackSecretFiles(dir, []transport.StackSecretFile{
		{Name: "identity/brand_new", ContentB64: b64Of("nope"), Mode: "0400"},
	})
	if err == nil {
		t.Fatalf("R2: refresh of a missing target must error")
	}
	if modeAt(dir, filepath.Join(identitySubdir, "brand_new")) >= 0 {
		t.Fatalf("R2: refresh created identity/brand_new")
	}
}

// R3. One bad entry in a batch fails the WHOLE op before anything is written:
// the good file keeps its old content and mtime (validate-first).
func TestC8W1RefreshValidatesFirst(t *testing.T) {
	dir := t.TempDir()
	if _, _, err := writeStackSecrets(dir, []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-1"), Mode: "0400"},
	}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	target := filepath.Join(dir, identitySubdir, "mod_identity_token")
	fi, err := os.Stat(target)
	if err != nil {
		t.Fatalf("stat target: %v", err)
	}
	before := fi.ModTime().UnixNano()
	_, _, err = refreshStackSecretFiles(dir, []transport.StackSecretFile{
		{Name: "identity/mod_identity_token", ContentB64: b64Of("tok-2"), Mode: "0400"},
		{Name: "identity/gone", ContentB64: b64Of("x"), Mode: "0400"},
	})
	if err == nil {
		t.Fatalf("R3: a batch with a missing target must error")
	}
	if contentAt(dir, filepath.Join(identitySubdir, "mod_identity_token")) != "tok-1" {
		t.Fatalf("R3: the good file was rewritten despite a bad entry")
	}
	fi, err = os.Stat(target)
	if err != nil {
		t.Fatalf("stat target after: %v", err)
	}
	if fi.ModTime().UnixNano() != before {
		t.Fatalf("R3: the good file was touched despite a bad entry")
	}
}

// R4. Refresh against a project that was never deployed (or whose secrets dir
// is gone) errors "stack not deployed" — the op never materializes a dir.
func TestC8W1RefreshNeedsDeployedDir(t *testing.T) {
	proj := fmt.Sprintf("c8w1-refresh-miss-%d", os.Getpid())
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp: "refresh_secret_files", StackProject: proj,
		SecretFiles: []transport.StackSecretFile{
			{Name: "identity/mod_identity_token", ContentB64: b64Of("tok"), Mode: "0400"},
		},
	}}
	restore := withContainerized(false)
	defer restore()
	_, err := e_for_test().runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatalf("R4: refresh on a missing project dir must error")
	}
	if !strings.Contains(err.Error(), "stack not deployed") {
		t.Fatalf("R4: error = %q, want it to say stack not deployed", err.Error())
	}
}
