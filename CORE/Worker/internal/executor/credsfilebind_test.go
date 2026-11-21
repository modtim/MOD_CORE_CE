package executor

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/credsrefresher"
)

// TestCredsFileBind pins the GSEC Q21.7 read-only bind: no file => no bind
// line (never a docker-created directory); file present => the same host
// path mounted read-only.
func TestCredsFileBind(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(credsrefresher.StateDirEnv, dir)

	path := credsrefresher.TaskCredsPath(42)
	if _, err := os.Stat(path); err == nil {
		t.Fatalf("creds file must not pre-exist at %s", path)
	}
	if b := credsFileBind(42); b != "" {
		t.Fatalf("absent file must not bind, got %q", b)
	}

	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"task_id":42}`), 0o600); err != nil {
		t.Fatal(err)
	}
	want := path + ":" + path + ":ro"
	if b := credsFileBind(42); b != want {
		t.Fatalf("bind = %q, want %q", b, want)
	}
	if b := credsFileBind(0); b != "" {
		t.Fatalf("taskID 0 must not bind, got %q", b)
	}
}
