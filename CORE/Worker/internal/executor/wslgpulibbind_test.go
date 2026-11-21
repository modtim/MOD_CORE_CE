package executor

import (
	"errors"
	"os"
	"testing"
)

// GSEC Q21.9b — the executor-facing WSL lib bind: readable dir ⇒ the read-only
// bind, unreadable dir (non-root worker) ⇒ no bind (never a task failure),
// absent dir (non-WSL host) ⇒ no bind. Uses the injectable listDir; no WSL
// host or permission tricks needed.

func TestWSLGpuLibBindReadable(t *testing.T) {
	dir := t.TempDir()
	orig := listDir
	listDir = func(name string) ([]os.DirEntry, error) {
		if name == "" {
			name = defaultWSLLibDir
		}
		if name == dir {
			return []os.DirEntry{}, nil
		}
		return nil, os.ErrNotExist
	}
	t.Cleanup(func() { listDir = orig })

	if got := wslGpuLibBind(); got != "/usr/lib/wsl/lib:/usr/lib/wsl/lib:ro" {
		t.Fatalf("readable bind = %q, want the ro bind", got)
	}
}

func TestWSLGpuLibBindUnreadableFallsBack(t *testing.T) {
	orig := listDir
	listDir = func(name string) ([]os.DirEntry, error) {
		return nil, &os.PathError{Op: "readdirent", Path: defaultWSLLibDir, Err: os.ErrPermission}
	}
	t.Cleanup(func() { listDir = orig })

	if got := wslGpuLibBind(); got != "" {
		t.Fatalf("unreadable bind = %q, want empty (fall back, never fail the task)", got)
	}
}

func TestWSLGpuLibBindAbsentOnNonWSL(t *testing.T) {
	orig := listDir
	listDir = func(name string) ([]os.DirEntry, error) {
		return nil, errors.New("no such file or directory: " + name)
	}
	t.Cleanup(func() { listDir = orig })

	if got := wslGpuLibBind(); got != "" {
		t.Fatalf("absent-dir bind = %q, want empty (non-WSL host, no behaviour change)", got)
	}
}
