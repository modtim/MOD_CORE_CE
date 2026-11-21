package main

// hx.9b — the worker hands its adopted worker_id to the helper's
// --worker-id-file state file: atomically (tmp + rename in the same dir), mode
// 0644, dir 0750, rewritten when the id changes, skipped when it already
// matches, NEVER written in direct/systemd mode, and a write failure is one
// warning (logged once) rather than a crash.

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/heartbeat"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/hostexec"
)

func hx9bLog() *slog.Logger { return slog.New(slog.NewTextHandler(new(bytes.Buffer), nil)) }

func hx9bCfg() *config.Config { return &config.Config{} }

var hx9bRealSocketExists = hostexec.SocketExists
var hx9bRealContainer = hostexec.RunningInContainer
var hx9bRealAdapterSet = adapterSet
var hx9bRealIDFile = hostExecWorkerIDFile

func hx9bRestore() {
	hostExecWorkerIDProvider = heartbeat.WorkerID
	wireHostExecFn = wireHostExec
	hostExecKeyFile = "/etc/mod-worker/host-exec.key"
	hostExecSocketPath = hostexec.DefaultSocket
	hostExecWorkerIDFile = hx9bRealIDFile
	hostExecIDWarned = false
	adapterFn = nil
	adapterSet = hx9bRealAdapterSet
	hostexec.SocketExists = hx9bRealSocketExists
	hostexec.RunningInContainer = hx9bRealContainer
}

// hx9bHelper makes hostexec.Mode pick the relay path: the bind-mounted helper
// socket is present, so wireHostExec must write the id state file.
func hx9bHelper() {
	hostexec.SocketExists = func(_ string) bool { return true }
	hostexec.RunningInContainer = func() bool { return true }
}

// hx9bNative makes hostexec.Mode pick the direct path: no helper socket, so
// wireHostExec must NOT touch the state file.
func hx9bNative() {
	hostexec.SocketExists = func(_ string) bool { return false }
	hostexec.RunningInContainer = func() bool { return false }
}

func hx9bNamed(id int64) {
	hostExecWorkerIDProvider = func() int64 { return id }
}

// hx9bStubSeams keeps wireHostExec from mutating the executor's real seams.
func hx9bStubSeams() {
	adapterSet = func(_ func(executor.HostExecRequest) (executor.HostExecRequest, error)) {}
}

func hx9bLogTo(buf *bytes.Buffer) *slog.Logger {
	return slog.New(slog.NewTextHandler(buf, nil))
}

func hx9bIDFileMode(path string) int {
	info, err := os.Stat(path)
	if err != nil {
		return -1
	}
	return int(info.Mode().Perm())
}

// hx9bInstall points the key/state-file at a fresh temp dir and returns the
// state-file path. The key is 0600 so hostExecReadKey accepts it.
func hx9bInstall(t *testing.T) string {
	root, err := os.MkdirTemp("", "hx9b")
	if err != nil {
		t.Fatalf("MkdirTemp failed: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	key := filepath.Join(root, "host-exec.key")
	if err := os.WriteFile(key, []byte("0123456789abcdef"), 0o600); err != nil {
		t.Fatalf("WriteFile key failed: %v", err)
	}
	hostExecKeyFile = key
	idFile := filepath.Join(root, "host-exec", "worker_id")
	hostExecWorkerIDFile = idFile
	return idFile
}

func hx9bWire(log *slog.Logger) { wireHostExec(hx9bCfg(), log) }

func hx9bRead(t *testing.T, path string) string {
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s failed: %v", path, err)
	}
	return string(got)
}

func hx9bExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func hx9bMtime(path string) int64 {
	info, err := os.Stat(path)
	if err != nil {
		return -1
	}
	return info.ModTime().UnixNano()
}

func hx9bWarnLines(buf *bytes.Buffer) int {
	n := 0
	for _, line := range strings.Split(buf.String(), "\n") {
		if strings.Contains(line, "worker_id state file not written") {
			n += 1
		}
	}
	return n
}

// 1) relay mode: the adopted id lands on disk, 0644, no tmp residue.
func TestHostExecIDFile_relayModeWritesIDAtomically(t *testing.T) {
	t.Cleanup(hx9bRestore)
	idFile := hx9bInstall(t)
	hx9bHelper()
	hx9bNamed(41)
	hx9bStubSeams()
	hx9bWire(hx9bLog())

	if got := hx9bRead(t, idFile); got != "41\n" {
		t.Fatalf("state file holds %q (want \"41\\n\")", got)
	}
	if mode := hx9bIDFileMode(idFile); mode != 0o644 {
		t.Fatalf("state file mode is %o (want 644)", mode)
	}
	tmp := filepath.Join(filepath.Dir(idFile), ".worker_id.tmp")
	if hx9bExists(tmp) {
		t.Fatalf("tmp file %s survived the rename (not atomic)", tmp)
	}
	// The state dir is created 0750 by the writer.
	if mode := hx9bIDFileMode(filepath.Dir(idFile)); mode != 0o750 {
		t.Fatalf("state dir mode is %o (want 750)", mode)
	}
}

// 2) re-registration adopts a NEW id: the file is rewritten, still 0644.
func TestHostExecIDFile_rewriteOnIDChange(t *testing.T) {
	t.Cleanup(hx9bRestore)
	idFile := hx9bInstall(t)
	hx9bHelper()
	hx9bStubSeams()

	hx9bNamed(41)
	hx9bWire(hx9bLog())
	if got := hx9bRead(t, idFile); got != "41\n" {
		t.Fatalf("first wire left %q (want \"41\\n\")", got)
	}

	hx9bNamed(77)
	hx9bWire(hx9bLog())
	if got := hx9bRead(t, idFile); got != "77\n" {
		t.Fatalf("re-registered id not adopted: file holds %q (want \"77\\n\")", got)
	}
	if mode := hx9bIDFileMode(idFile); mode != 0o644 {
		t.Fatalf("rewritten state file mode is %o (want 644)", mode)
	}
}

// 3) same id again: the file is left alone (one read, no rewrite).
func TestHostExecIDFile_noOpWhenEqual(t *testing.T) {
	t.Cleanup(hx9bRestore)
	idFile := hx9bInstall(t)
	hx9bHelper()
	hx9bStubSeams()

	hx9bNamed(41)
	hx9bWire(hx9bLog())
	stamp := hx9bMtime(idFile)
	if stamp < 0 {
		t.Fatalf("no state file after relay wiring")
	}
	hx9bWire(hx9bLog())
	if hx9bMtime(idFile) != stamp {
		t.Fatalf("equal id rewrote the state file (must be a no-op)")
	}
}

// 4) direct/systemd mode: no helper is involved, so no state file is written.
func TestHostExecIDFile_directModeWritesNothing(t *testing.T) {
	t.Cleanup(hx9bRestore)
	idFile := hx9bInstall(t)
	hx9bNative()
	hx9bNamed(41)
	hx9bStubSeams()
	hx9bWire(hx9bLog())

	if hx9bExists(idFile) {
		t.Fatalf("direct mode wrote a worker_id state file (helper is not involved)")
	}
	if hx9bExists(filepath.Dir(idFile)) {
		t.Fatalf("direct mode created the state dir (no relay, no file needed)")
	}
}

// 5) the state dir cannot be created (a regular file sits where the dir
// belongs — root-proof, unlike a chmod'd dir): one warning, no crash, and the
// seams still get wired so the helper's own fail-closed path owns the job.
func TestHostExecIDFile_unwritableStateDir_warnsOnceWithoutPanic(t *testing.T) {
	t.Cleanup(hx9bRestore)
	idFile := hx9bInstall(t)
	hx9bHelper()
	hx9bStubSeams()

	// hx9bInstall made <root>/host-exec/worker_id the target; block its parent.
	if err := os.WriteFile(filepath.Dir(idFile), []byte("not a dir"), 0o644); err != nil {
		t.Fatalf("could not plant the blocking file: %v", err)
	}

	var seen int
	adapterSet = func(_ func(executor.HostExecRequest) (executor.HostExecRequest, error)) { seen += 1 }
	buf := new(bytes.Buffer)
	log := hx9bLogTo(buf)

	hx9bNamed(41)
	hx9bWire(log)
	hx9bWire(log)

	if hx9bExists(idFile) {
		t.Fatalf("a blocked state dir still produced a worker_id file")
	}
	if n := hx9bWarnLines(buf); n != 1 {
		t.Fatalf("write failure logged %d times (want exactly 1)", n)
	}
	if seen != 2 {
		t.Fatalf("wiring aborted after the id-file failure (adapter handed %d times, want 2)", seen)
	}
}

// 6) hx.7b relay seam: helper mode must build the relay and hand the executor
// its runner + auth headers + worker_id, and the runner must reach Relay.Exec.
// With the relay assignment commented out (the mutant) the runner must fail
// closed, never dial and never run unsandboxed.
func TestHostExecRelay_helperModeBuildsRelayAndNilRelayFailsClosed(t *testing.T) {
	t.Cleanup(hx9bRestore)
	hx9bInstall(t)
	hx9bHelper()
	hx9bStubSeams()
	buf := new(bytes.Buffer)
	log := hx9bLogTo(buf)

	hx9bNamed(29)
	hx9bWire(log)

	if hostExecRelay == nil {
		t.Fatalf("helper mode left hostExecRelay nil (relay never built)")
	}
	if hostExecRelay.Socket != hostexec.DefaultSocket {
		t.Fatalf("relay socket = %q, want %q", hostExecRelay.Socket, hostexec.DefaultSocket)
	}
	if hostExecRelay.WorkerID != "29" {
		t.Fatalf("relay worker_id = %q, want %q", hostExecRelay.WorkerID, "29")
	}
	if len(hostExecRelay.Key) == 0 {
		t.Fatalf("relay signing key empty")
	}
	// The executor's own seams (hostExecWorkerID / hostExecSetAuth) are package-
	// private, so this package-main test asserts the cmd/worker side of the hand-
	// over only: the relay itself and the runner function main.go hands over.

	// Empty argv reaches Relay.Exec's own guard (before any dial), which proves
	// the runner seam IS the relay, not hostexec_task.go's "not wired" default.
	req := executor.HostExecRequest{Argv: []string{}, Cwd: "/tmp", Timeout: time.Second}
	_, err := hostExecRelayRunner(context.Background(), req, func(_ executor.HostExecFrame) {})
	if err == nil {
		t.Fatalf("empty argv through the relay runner returned no error")
	}
	if !strings.Contains(err.Error(), "empty argv") {
		t.Fatalf("relay runner error = %q, want Relay.Exec's empty-argv refusal", err.Error())
	}

	// Mutant: relay never built -> the runner must fail closed.
	hostExecRelay = nil
	_, nilErr := hostExecRelayRunner(context.Background(), req, func(_ executor.HostExecFrame) {})
	if nilErr == nil {
		t.Fatalf("nil relay ran without error (unsandboxed exec)")
	}
	if !strings.Contains(nilErr.Error(), "relay not built") {
		t.Fatalf("nil relay error = %q, want the fail-closed relay-not-built message", nilErr.Error())
	}
}
