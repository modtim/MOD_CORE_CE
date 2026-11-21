package hostexec

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeRun records the argv sequences it was called with.
type fakeRun struct {
	calls [][]string
	fn    RunFunc
}

func newFakeRun() *fakeRun {
	f := &fakeRun{}
	f.fn = func(ctx context.Context, argv []string, cwd string) (int, []byte, error) {
		f.calls = append(f.calls, argv)
		return 0, nil, nil
	}
	return f
}

func (f *fakeRun) asRunFunc() RunFunc { return f.fn }

func TestNewWorkspace_Valid(t *testing.T) {
	tmp := t.TempDir()
	root := filepath.Join(tmp, "ws")
	w, err := NewWorkspace(root)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	if w.Root != root {
		t.Fatalf("Root = %q, want %q", w.Root, root)
	}
	info, err := os.Stat(root)
	if err != nil {
		t.Fatalf("workspace root not created: %v", err)
	}
	if info.Mode().Perm()&0750 != 0750 {
		t.Fatalf("root perm = %o, want 0750", info.Mode().Perm())
	}
}

func TestNewWorkspace_RelativeRejects(t *testing.T) {
	_, err := NewWorkspace("relative/path")
	if err == nil {
		t.Fatal("expected error for relative path, got nil")
	}
	if !strings.Contains(err.Error(), "absolute") {
		t.Fatalf("error should mention 'absolute': %v", err)
	}
}

func TestNewWorkspace_MkdirAll(t *testing.T) {
	tmp := t.TempDir()
	root := filepath.Join(tmp, "a", "b", "c", "ws")
	_, err := NewWorkspace(root)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Fatalf("nested root not created: %v", err)
	}
}

func TestTenantDir_Named(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	d, err := ws.TenantDir(42)
	if err != nil {
		t.Fatalf("TenantDir(42): %v", err)
	}
	want := filepath.Join(ws.Root, "t42")
	if d != want {
		t.Fatalf("TenantDir(42) = %q, want %q", d, want)
	}
}

func TestTenantDir_PlatformZero(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	d, err := ws.TenantDir(0)
	if err != nil {
		t.Fatalf("TenantDir(0): %v", err)
	}
	want := filepath.Join(ws.Root, "platform")
	if d != want {
		t.Fatalf("TenantDir(0) = %q, want %q", d, want)
	}
}

func TestTenantDir_Negative(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	d, err := ws.TenantDir(-1)
	if err != nil {
		t.Fatalf("TenantDir(-1): %v", err)
	}
	want := filepath.Join(ws.Root, "t-1")
	if d != want {
		t.Fatalf("TenantDir(-1) = %q, want %q", d, want)
	}
}

func TestResolve_ValidPath(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	path, err := ws.Resolve(tenantDir, "sub/file.txt")
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	want := filepath.Join(tenantDir, "sub", "file.txt")
	if path != want {
		t.Fatalf("Resolve = %q, want %q", path, want)
	}
}

func TestResolve_AbsoluteRejects(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	_, err = ws.Resolve(tenantDir, "/etc/passwd")
	if err == nil {
		t.Fatal("expected error for absolute path, got nil")
	}
	if !strings.Contains(err.Error(), "absolute") {
		t.Fatalf("error should mention 'absolute': %v", err)
	}
}

func TestResolve_DotDotRejects(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	_, err = ws.Resolve(tenantDir, "../etc/passwd")
	if err == nil {
		t.Fatal("expected error for ../ path, got nil")
	}
	if !strings.Contains(err.Error(), "..") {
		t.Fatalf("error should mention '..': %v", err)
	}
}

func TestResolve_SymlinkEscape(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	// Create a symlink inside tenantDir that points outside.
	outside := filepath.Join(os.TempDir(), "outside-symlink-target")
	os.Mkdir(outside, 0750)
	if err := os.Symlink(outside, filepath.Join(tenantDir, "badlink")); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}
	_, err = ws.Resolve(tenantDir, "badlink")
	if err == nil {
		t.Fatal("expected error for symlink escape, got nil")
	}
	if !strings.Contains(err.Error(), "symlink") {
		t.Fatalf("error should mention 'symlink': %v", err)
	}
}

func TestResolve_NoSymlinkEscape(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	// Create a symlink inside tenantDir that points inside.
	subdir := filepath.Join(tenantDir, "subdir")
	os.Mkdir(subdir, 0750)
	if err := os.Symlink(subdir, filepath.Join(tenantDir, "goodlink")); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}
	_, err = ws.Resolve(tenantDir, "goodlink/file.txt")
	if err != nil {
		t.Fatalf("good symlink should not error: %v", err)
	}
}

func TestResolve_NormalPathThroughSymlink(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	// Create a symlink to an actual subdirectory.
	subdir := filepath.Join(tenantDir, "realdir")
	os.Mkdir(subdir, 0750)
	os.WriteFile(filepath.Join(subdir, "file.txt"), []byte("data"), 0644)
	if err := os.Symlink(subdir, filepath.Join(tenantDir, "linkdir")); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}
	path, err := ws.Resolve(tenantDir, "linkdir/file.txt")
	if err != nil {
		t.Fatalf("valid symlink path: %v", err)
	}
	// The path should be resolved through the symlink but stay inside tenantDir.
	if !strings.HasPrefix(path, tenantDir) {
		t.Fatalf("resolved path %q not under tenantDir %q", path, tenantDir)
	}
}

func TestEnsureRepo_Clone(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	fake := newFakeRun()
	repoDir, err := ws.EnsureRepo(context.Background(), tenantDir, "myrepo", "https://example.com/repo.git", "main", fake.asRunFunc())
	if err != nil {
		t.Fatalf("EnsureRepo: %v", err)
	}
	if !strings.HasSuffix(repoDir, "myrepo") {
		t.Fatalf("EnsureRepo = %q, want suffix 'myrepo'", repoDir)
	}
	if len(fake.calls) != 1 {
		t.Fatalf("expected 1 call, got %d", len(fake.calls))
	}
	firstCall := fake.calls[0]
	if firstCall[0] != "clone" {
		t.Fatalf("first command = %q, want clone", firstCall[0])
	}
}

func TestEnsureRepo_FetchCheckout(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	// Pre-create the repo directory so it thinks it exists.
	repoDir := filepath.Join(tenantDir, "myrepo")
	os.Mkdir(repoDir, 0750)

	fake := newFakeRun()
	_, err = ws.EnsureRepo(context.Background(), tenantDir, "myrepo", "https://example.com/repo.git", "main", fake.asRunFunc())
	if err != nil {
		t.Fatalf("EnsureRepo: %v", err)
	}
	if len(fake.calls) != 2 {
		t.Fatalf("expected 2 calls, got %d", len(fake.calls))
	}
	// First: fetch --prune.
	if fake.calls[0][0] != "-C" || fake.calls[0][2] != "fetch" {
		t.Fatalf("first call = %v, want fetch", fake.calls[0])
	}
	// Second: checkout --detach.
	if fake.calls[1][0] != "-C" || fake.calls[1][2] != "checkout" {
		t.Fatalf("second call = %v, want checkout", fake.calls[1])
	}
}

func TestEnsureRepo_BadURLEscape(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	_, err = ws.EnsureRepo(context.Background(), tenantDir, "myrepo", "git@github.com:org/repo.git", "main", newFakeRun().asRunFunc())
	if err == nil {
		t.Fatal("expected error for unsupported URL scheme, got nil")
	}
	if !strings.Contains(err.Error(), "https://") {
		t.Fatalf("error should mention scheme: %v", err)
	}
}

func TestEnsureRepo_BadDirName(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	_, err = ws.EnsureRepo(context.Background(), tenantDir, "bad name!", "https://example.com/repo.git", "main", newFakeRun().asRunFunc())
	if err == nil {
		t.Fatal("expected error for bad dir name, got nil")
	}
}

func TestEnsureWorktree_Create(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	repoDir := filepath.Join(tmp, "repo")
	os.Mkdir(repoDir, 0750)
	fake := newFakeRun()
	wtPath, err := ws.EnsureWorktree(context.Background(), repoDir, "my-wt", "main", fake.asRunFunc())
	if err != nil {
		t.Fatalf("EnsureWorktree: %v", err)
	}
	if !strings.HasSuffix(wtPath, filepath.Join("repo", "worktrees", "my-wt")) {
		t.Fatalf("EnsureWorktree = %q, want worktrees/my-wt suffix", wtPath)
	}
	if len(fake.calls) != 1 {
		t.Fatalf("expected 1 call, got %d", len(fake.calls))
	}
	if fake.calls[0][0] != "-C" || fake.calls[0][2] != "worktree" {
		t.Fatalf("first call = %v, want worktree add", fake.calls[0])
	}
}

func TestEnsureWorktree_CheckoutExisting(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	repoDir := filepath.Join(tmp, "repo")
	os.Mkdir(repoDir, 0750)
	wtPath := filepath.Join(repoDir, "worktrees", "my-wt")
	os.MkdirAll(wtPath, 0750)

	fake := newFakeRun()
	_, err = ws.EnsureWorktree(context.Background(), repoDir, "my-wt", "v2.0", fake.asRunFunc())
	if err != nil {
		t.Fatalf("EnsureWorktree: %v", err)
	}
	if len(fake.calls) != 1 {
		t.Fatalf("expected 1 call, got %d", len(fake.calls))
	}
	if fake.calls[0][0] != "-C" || fake.calls[0][2] != "checkout" {
		t.Fatalf("first call = %v, want checkout", fake.calls[0])
	}
}

func TestEnsureWorktree_BadName(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	repoDir := filepath.Join(tmp, "repo")
	os.Mkdir(repoDir, 0750)
	_, err = ws.EnsureWorktree(context.Background(), repoDir, "bad name!", "main", newFakeRun().asRunFunc())
	if err == nil {
		t.Fatal("expected error for bad worktree name, got nil")
	}
}

func TestEnsureWorktree_NilRun(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	_, err = ws.EnsureWorktree(context.Background(), "/tmp/repo", "wt", "main", nil)
	if err == nil {
		t.Fatal("expected error for nil RunFunc, got nil")
	}
}

func TestEnsureRepo_NilRun(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	_, err = ws.EnsureRepo(context.Background(), tenantDir, "repo", "https://example.com/repo.git", "main", nil)
	if err == nil {
		t.Fatal("expected error for nil RunFunc, got nil")
	}
}

func TestResolve_EmptyRel(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	path, err := ws.Resolve(tenantDir, "")
	if err != nil {
		t.Fatalf("Resolve with empty rel: %v", err)
	}
	if path != tenantDir {
		t.Fatalf("Resolve(\"\") = %q, want tenantDir %q", path, tenantDir)
	}
}

// TestEnsureRepo_LocalPathUnderRoot verifies that a local path under the
// workspace root is accepted, while one outside is rejected.
func TestEnsureRepo_LocalPathUnderRoot(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}

	// Path under workspace root — should be accepted.
	localPath := filepath.Join(tmp, "repos", "myrepo")
	os.MkdirAll(localPath, 0750)
	fake := newFakeRun()
	_, err = ws.EnsureRepo(context.Background(), tenantDir, "local", localPath, "main", fake.asRunFunc())
	if err != nil {
		t.Fatalf("EnsureRepo local path under root: %v", err)
	}

	// Path outside workspace root — should be rejected.
	outsidePath := filepath.Join(tmp, "..", "tmp")
	_, err = ws.EnsureRepo(context.Background(), tenantDir, "outside", outsidePath, "main", newFakeRun().asRunFunc())
	if err == nil {
		t.Fatal("expected error for path outside workspace root, got nil")
	}
}

// TestEnsureRepo_URLValidation tests various URL formats.
func TestEnsureRepo_URLValidation(t *testing.T) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}

	testCases := []struct {
		url    string
		expect bool
	}{
		{"https://github.com/org/repo.git", true},
		{"ssh://git@github.com/org/repo.git", true},
		{"http://example.com/repo.git", false},
		{"git://example.com/repo.git", false},
		{"file:///path/to/repo", false},
		{"../outside/repo", false},
	}

	for _, tc := range testCases {
		_, err := ws.EnsureRepo(context.Background(), tenantDir, "repo", tc.url, "main", newFakeRun().asRunFunc())
		if tc.expect && err != nil {
			t.Errorf("EnsureRepo(%q): unexpected error: %v", tc.url, err)
		}
		if !tc.expect && err == nil {
			t.Errorf("EnsureRepo(%q): expected error, got nil", tc.url)
		}
	}
}

// TestEnsureWorktree_NameValidation tests various worktree name formats.
func TestEnsureWorktree_NameValidation(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	repoDir := filepath.Join(tmp, "repo")
	os.Mkdir(repoDir, 0750)

	validNames := []string{"main", "v1.0", "feature-x", "test_123", "a.b-c_d"}
	for _, name := range validNames {
		_, err := ws.EnsureWorktree(context.Background(), repoDir, name, "main", newFakeRun().asRunFunc())
		if err != nil {
			t.Errorf("EnsureWorktree(%q): unexpected error: %v", name, err)
		}
	}

	invalidNames := []string{"", fmt.Sprintf("%065d", 0), "bad name!", "bad/name", "bad%name"}
	for _, name := range invalidNames {
		_, err := ws.EnsureWorktree(context.Background(), repoDir, name, "main", newFakeRun().asRunFunc())
		if err == nil {
			t.Errorf("EnsureWorktree(%q): expected error for invalid name, got nil", name)
		}
	}
}

// TestWorkspace_TenantDirCreation verifies directories are actually created.
func TestWorkspace_TenantDirCreation(t *testing.T) {
	tmp := t.TempDir()
	ws, err := NewWorkspace(tmp)
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}

	// Create several tenant dirs.
	dirs := []int64{1, 2, 0, 100}
	for _, id := range dirs {
		d, err := ws.TenantDir(id)
		if err != nil {
			t.Fatalf("TenantDir(%d): %v", id, err)
		}
		if _, err := os.Stat(d); os.IsNotExist(err) {
			t.Fatalf("tenant dir %q not created", d)
		}
	}

	// Verify platform dir for tenant 0.
	platformDir, err := ws.TenantDir(0)
	if err != nil {
		t.Fatalf("TenantDir(0): %v", err)
	}
	if platformDir != filepath.Join(ws.Root, "platform") {
		t.Fatalf("TenantDir(0) = %q, want platform dir", platformDir)
	}
}

// --- error-text scrubbing (SECURITY) -------------------------------------
// FAKE sentinel credentials only; no real git, no network.

// runScript fails (exit 7, out, errors.New(msg)) on the calls whose 0-based
// index appears in failOn; every other call succeeds.
func runScript(out, msg string, failOn ...int) RunFunc {
	n := 0
	return func(_ context.Context, _ []string, _ string) (int, []byte, error) {
		idx := n
		n = n + 1
		for _, f := range failOn {
			if f == idx {
				return 7, []byte(out), errors.New(msg)
			}
		}
		return 0, []byte(out), nil
	}
}

// assertSafeError pins the contract on ONE error-returning path: no sentinel,
// no 'user:' userinfo, bounded length, and the step name plus the host/repo
// path survive so the error stays useful.
func assertSafeError(t *testing.T, what string, err error, step, keep string, sentinels []string) {
	if err == nil {
		t.Errorf("%s: expected an error, got nil", what)
		return
	}
	msg := err.Error()
	for _, s := range sentinels {
		if strings.Contains(msg, s) {
			t.Errorf("%s: leaked sentinel %q: %s", what, s, msg)
		}
	}
	if strings.Contains(msg, "user:") {
		t.Errorf("%s: userinfo 'user:' present: %s", what, msg)
	}
	if len(msg) >= 1500 {
		t.Errorf("%s: error text %d bytes, want <1500: %s", what, len(msg), msg)
	}
	if !strings.Contains(msg, step) {
		t.Errorf("%s: must keep step name %q: %s", what, step, msg)
	}
	if !strings.Contains(msg, keep) {
		t.Errorf("%s: must keep host/repo path %q: %s", what, keep, msg)
	}
}

// TestScrubURL_EdgeCases pins scrubURL's text-level behaviour.
func TestScrubURL_EdgeCases(t *testing.T) {
	if got := scrubURL("fatal: remote error"); got != "fatal: remote error" {
		t.Errorf("no-URL case: %q", got)
	}
	one := scrubURL("https://user:SENTINEL-PASS-123@git.example/org/repo.git")
	if !strings.Contains(one, "https://***@git.example/org/repo.git") || strings.Contains(one, "SENTINEL-PASS-123") {
		t.Errorf("userinfo case: %q", one)
	}
	tok := scrubURL("https://SENTINEL-TOKEN-456@git.example/org/repo.git")
	if !strings.Contains(tok, "https://***@git.example/org/repo.git") || strings.Contains(tok, "SENTINEL-TOKEN-456") {
		t.Errorf("token case: %q", tok)
	}
	q := scrubURL("https://git.example/org/repo.git?token=SENTINEL-QTOKEN-789&x=1")
	if !strings.Contains(q, "token="+redactedValuePlaceholder) || strings.Contains(q, "SENTINEL-QTOKEN-789") {
		t.Errorf("query case: %q", q)
	}
	q2 := scrubURL("ssh://x?a=1&access_token=SENTINEL-A&password=SENTINEL-P&key=SENTINEL-K")
	for _, leak := range []string{"SENTINEL-A", "SENTINEL-P", "SENTINEL-K"} {
		if strings.Contains(q2, leak) {
			t.Errorf("query multi: leaked %q in %q", leak, q2)
		}
	}
	both := scrubURL("warn 'https://a:b@h1/p.git' and https://TWO@h2/p.git")
	if strings.Contains(both, "a:b@") || strings.Contains(both, "TWO@") {
		t.Errorf("two-URL case: %q", both)
	}
	ssh := scrubURL("git@github.com:org/repo.git")
	if ssh != "git@github.com:org/repo.git" {
		t.Errorf("ssh form changed: %q", ssh)
	}
	pathAt := scrubURL("https://git.example/org/re@po.git")
	if pathAt != "https://git.example/org/re@po.git" {
		t.Errorf("@-in-path changed: %q", pathAt)
	}
	up := scrubURL("HTTPS://user:SENTINEL-UP@host/x.git")
	if strings.Contains(up, "SENTINEL-UP") {
		t.Errorf("uppercase scheme leaked: %q", up)
	}
	enc := scrubURL("https://user:p%40ssSENTINEL-ENC-789@git.example/org/repo.git")
	if strings.Contains(enc, "SENTINEL-ENC-789") {
		t.Errorf("percent-encoded password leaked: %q", enc)
	}
}

// TestCapGitOutput_CapsAndScrubs pins the cap and the scrub-before-cap
// ordering (a URL straddling the cut point must not leak its tail).
func TestCapGitOutput_CapsAndScrubs(t *testing.T) {
	fiveK := "fatal: unable to access 'https://user:SENTINEL-PASS-123@git.example/org/repo.git': " + strings.Repeat("x", 5000)
	capped := capGitOutput([]byte(fiveK))
	if strings.Contains(capped, "SENTINEL-PASS-123") {
		t.Errorf("5000-byte stderr leaked sentinel: %s", capped)
	}
	if !strings.Contains(capped, "...(truncated)") {
		t.Errorf("5000-byte stderr not marked truncated: %s", capped)
	}
	if len(capped) != maxGitErrBytes+len(truncatedSuffix) {
		t.Errorf("capped length %d, want %d", len(capped), maxGitErrBytes+len(truncatedSuffix))
	}
	early := strings.Repeat("y", 500) + "https://user:SENTINEL-PASS-123@git.example/org/repo.git"
	e := capGitOutput([]byte(early))
	if strings.Contains(e, "SENTINEL-PASS-123") {
		t.Errorf("straddling URL leaked after cap: %s", e)
	}
	trim := capGitOutput([]byte("\n\nCloning into 'x'\n\n"))
	if trim != "Cloning into 'x'" {
		t.Errorf("trim case: %q", trim)
	}
	if capGitOutput([]byte("")) != "" {
		t.Errorf("empty case: %q", capGitOutput([]byte("")))
	}
	utf := strings.Repeat("a", maxGitErrBytes-1) + "éend"
	u := capGitOutput([]byte(utf))
	if strings.Contains(u, "�") {
		t.Errorf("cut split a UTF-8 sequence: %s", u)
	}
}

// TestEnsureRepo_ScrubbedErrorsOnFailure covers EVERY error-returning path
// (clone, fetch, checkout, worktree create, worktree checkout) over the
// sentinel URL table: the returned text must carry no credential, stay
// bounded, and still name the step and the host/repo path.
func TestEnsureRepo_ScrubbedErrorsOnFailure(t *testing.T) {
	sentinels := []string{"SENTINEL-PASS-123", "SENTINEL-TOKEN-456", "SENTINEL-QTOKEN-789", "SENTINEL-ENC-789"}
	keep := "git.example/org/repo.git"
	fiveK := strings.Repeat("x", 5000)

	urls := []string{
		"https://user:SENTINEL-PASS-123@git.example/org/repo.git",
		"https://SENTINEL-TOKEN-456@git.example/org/repo.git",
		"https://git.example/org/repo.git?token=SENTINEL-QTOKEN-789",
		"https://user:p%40ssSENTINEL-ENC-789@git.example/org/repo.git",
	}

	for _, url := range urls {
		gitErr := "fatal: unable to access '" + url + "': Permission denied\n" + fiveK
		// A real runner can surface exec errors that quote argv (the URL).
		// Every case below gets its OWN run function: the counter inside
		// runScript is per-closure, so reusing one would advance the index.

		_, err := ensureRepoScrub(t, "clone-repo", url, runScript(gitErr, "spawn: "+url, 0), false)
		assertSafeError(t, "clone "+url, err, "clone", keep, sentinels)

		_, err = ensureRepoScrub(t, "fetch-repo", url, runScript(gitErr, "spawn: "+url, 0), true)
		assertSafeError(t, "fetch "+url, err, "fetch", keep, sentinels)

		_, err = ensureRepoScrub(t, "checkout-repo", url, runScript(gitErr, "spawn: "+url, 1), true)
		assertSafeError(t, "checkout "+url, err, "checkout", keep, sentinels)

		_, err = ensureWorktreeScrub(t, "wt-repo", url, runScript(gitErr, "spawn: "+url, 0), false)
		assertSafeError(t, "worktree-create "+url, err, "create worktree", keep, sentinels)

	_, err = ensureWorktreeScrub(t, "wt-repo2", url, runScript(gitErr, "spawn: "+url, 0), true)
		assertSafeError(t, "worktree-checkout "+url, err, "checkout worktree", keep, sentinels)
	}
}

// ensureRepoScrub runs EnsureRepo (repoDir optionally pre-created so the
// fetch/checkout paths are reached) and hands back the error.
func ensureRepoScrub(t *testing.T, sub, url string, run RunFunc, repoExists bool) (string, error) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	repoDir := filepath.Join(tenantDir, sub)
	if repoExists {
		if err := os.MkdirAll(repoDir, 0750); err != nil {
			t.Fatalf("MkdirAll %q: %v", repoDir, err)
		}
	}
	return ws.EnsureRepo(context.Background(), tenantDir, sub, url, "main", run)
}

// ensureWorktreeScrub runs EnsureWorktree under an existing repoDir (the
// worktree dir optionally pre-created so the checkout path is reached).
func ensureWorktreeScrub(t *testing.T, sub, url string, run RunFunc, wtExists bool) (string, error) {
	ws, err := NewWorkspace(t.TempDir())
	if err != nil {
		t.Fatalf("NewWorkspace: %v", err)
	}
	tenantDir, err := ws.TenantDir(1)
	if err != nil {
		t.Fatalf("TenantDir(1): %v", err)
	}
	repoDir := filepath.Join(tenantDir, sub)
	if err := os.MkdirAll(repoDir, 0750); err != nil {
		t.Fatalf("MkdirAll %q: %v", repoDir, err)
	}
	wtPath := filepath.Join(repoDir, "worktrees", "wt1")
	if wtExists {
		if err := os.MkdirAll(wtPath, 0750); err != nil {
			t.Fatalf("MkdirAll %q: %v", wtPath, err)
		}
	}
	_ = url
	return ws.EnsureWorktree(context.Background(), repoDir, "wt1", "main", run)
}
