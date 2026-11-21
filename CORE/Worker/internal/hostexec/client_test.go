//go:build linux

package hostexec

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// The fake helper records what it decoded here so tests can assert the client
// actually signed the request and defaulted worker_id.
var gotSig string
var gotWorkerID string
var gotSigOK bool

// fakeHelper listens on a real unix socket and speaks the helper's wire
// protocol: one WireRequest in, newline-delimited OutFrames out. mode:
// "ok" = verify sig, stream two output frames, then exit_code 7;
// "reject" = answer an Error frame; "silent" = accept, never reply, and leave
// the conn open, so a cancelled ctx must unblock the client.
func fakeHelper(socket string, key []byte, mode string) (net.Listener, error) {
	l, err := net.Listen("unix", socket)
	if err != nil {
		return l, err
	}
	go func() {
		conn, err := l.Accept()
		if err != nil {
			return
		}
		var req WireRequest
		if err := json.NewDecoder(conn).Decode(&req); err != nil {
			return
		}
		gotSig = req.Sig
		gotWorkerID = req.WorkerID
		gotSigOK = VerifySig(key, req)
		if mode == "silent" {
			return
		}
		if mode == "reject" || !gotSigOK {
			_ = WriteFrame(conn, OutFrame{Error: "bad signature"})
			return
		}
		_ = WriteFrame(conn, OutFrame{Stream: "stdout", Data: base64.StdEncoding.EncodeToString([]byte("hello "))})
		_ = WriteFrame(conn, OutFrame{Stream: "stderr", Data: base64.StdEncoding.EncodeToString([]byte("world"))})
		code := 7
		_ = WriteFrame(conn, OutFrame{ExitCode: &code})
	}()
	return l, nil
}

// newTempSocket returns a path inside a fresh temp dir (no socket yet).
func newTempSocket(t *testing.T) (string, error) {
	dir, err := os.MkdirTemp("", "mod-hostexec-relay")
	if err != nil {
		t.Errorf("temp socket dir: %v", err)
		return "", err
	}
	return filepath.Join(dir, "host-exec.sock"), nil
}

// makeSocketFile leaves a real socket file at path for the Mode() matrix. The
// caller must keep the returned listener open (Close unlinks the socket file)
// and close it when the test ends.
func makeSocketFile(t *testing.T, path string) (net.Listener, error) {
	l, err := net.Listen("unix", path)
	if l == nil {
		t.Errorf("makeSocketFile: %v", err)
	}
	return l, err
}

func TestExec_HappyPathFramesAndExitCode(t *testing.T) {
	socket, err := newTempSocket(t)
	if err != nil {
		return
	}
	key := []byte("relay-test-key")
	l, lerr := fakeHelper(socket, key, "ok")
	if l == nil {
		t.Errorf("fake helper: %v", lerr)
		return
	}
	defer l.Close()

	r := Relay{Socket: socket, Key: key, WorkerID: "w-1"}
	req := WireRequest{JobID: "j1", Argv: []string{"/bin/true"}, TimeoutS: 5}
	seen := make([]OutFrame, 0, 2)
	emit := func(f OutFrame) error {
		seen = append(seen, f)
		return nil
	}
	code, err := r.Exec(context.Background(), req, emit)
	if err != nil {
		t.Errorf("exec err: %v", err)
		return
	}
	if code != 7 {
		t.Errorf("exit code %d, want 7", code)
	}
	if len(seen) != 2 {
		t.Errorf("got %d frames, want 2", len(seen))
		return
	}
	first, derr := DecodeFrameData(seen[0])
	if derr != nil {
		t.Errorf("frame 0 decode: %v", derr)
	}
	if seen[0].Stream != "stdout" || string(first) != "hello " {
		t.Errorf("frame 0: stream=%q data=%q", seen[0].Stream, seen[0].Data)
	}
	second, derr2 := DecodeFrameData(seen[1])
	if derr2 != nil {
		t.Errorf("frame 1 decode: %v", derr2)
	}
	if seen[1].Stream != "stderr" || string(second) != "world" {
		t.Errorf("frame 1: stream=%q data=%q", seen[1].Stream, seen[1].Data)
	}
	if gotSig == "" || !gotSigOK {
		t.Errorf("signature missing or unverifiable: %q", gotSig)
	}
	if gotWorkerID != "w-1" {
		t.Errorf("worker_id %q, want w-1 (relay must default it)", gotWorkerID)
	}
}

func TestExec_HelperRejectsRequest(t *testing.T) {
	socket, err := newTempSocket(t)
	if err != nil {
		return
	}
	key := []byte("relay-test-key")
	l, lerr := fakeHelper(socket, key, "reject")
	if l == nil {
		t.Errorf("fake helper: %v", lerr)
		return
	}
	defer l.Close()

	r := Relay{Socket: socket, Key: key, WorkerID: "w-1"}
	req := WireRequest{JobID: "j1", Argv: []string{"/bin/true"}, TimeoutS: 5}
	code, err := r.Exec(context.Background(), req, nil)
	if err == nil {
		t.Errorf("expected the helper's rejection, got exit code %d", code)
		return
	}
	if !strings.Contains(err.Error(), "bad signature") {
		t.Errorf("error %q does not carry the helper's rejection text", err.Error())
	}
}

func TestExec_MissingSocketIsActionable(t *testing.T) {
	socket, err := newTempSocket(t)
	if err != nil {
		return
	}
	r := Relay{Socket: socket, Key: []byte("k"), WorkerID: "w-1"}
	req := WireRequest{JobID: "j1", Argv: []string{"/bin/true"}, TimeoutS: 5}
	code, err := r.Exec(context.Background(), req, nil)
	if err == nil {
		t.Errorf("expected a missing-socket error, got exit code %d", code)
		return
	}
	if code != -1 {
		t.Errorf("exit code %d, want -1", code)
	}
	if !strings.Contains(err.Error(), "no helper socket at") {
		t.Errorf("error %q is not the actionable missing-socket message", err.Error())
	}
	if !strings.Contains(err.Error(), socket) {
		t.Errorf("error %q does not name the socket path", err.Error())
	}
}

// TestExec_InjectedDialMapsMissingSocketError covers the gap where an
// injected Dial (prod wrapper, not the test's own net.Pipe doubles) skips the
// pre-dial SocketExists stat in Exec: dialError must still turn ENOENT/
// ECONNREFUSED-shaped dial failures into the same actionable message instead
// of leaking a bare "connect: ..." error.
func TestExec_InjectedDialMapsMissingSocketError(t *testing.T) {
	cases := []struct {
		name string
		err  error
	}{
		{"enoent", &net.OpError{Op: "dial", Net: "unix", Err: &os.SyscallError{Syscall: "connect", Err: syscall.ENOENT}}},
		{"econnrefused", &net.OpError{Op: "dial", Net: "unix", Err: &os.SyscallError{Syscall: "connect", Err: syscall.ECONNREFUSED}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			socket := "/tmp/mod-hostexec-relay-injected.sock"
			r := Relay{
				Socket: socket, Key: []byte("k"), WorkerID: "w-1",
				Dial: func(ctx context.Context, path string) (net.Conn, error) { return nil, c.err },
			}
			req := WireRequest{JobID: "j1", Argv: []string{"/bin/true"}, TimeoutS: 5}
			code, err := r.Exec(context.Background(), req, nil)
			if err == nil {
				t.Errorf("expected a missing-socket error, got exit code %d", code)
				return
			}
			if !strings.Contains(err.Error(), "no helper socket at") {
				t.Errorf("error %q is not the actionable missing-socket message", err.Error())
			}
		})
	}
}

func TestExec_CancellationReturnsPromptly(t *testing.T) {
	socket, err := newTempSocket(t)
	if err != nil {
		return
	}
	key := []byte("relay-test-key")
	l, lerr := fakeHelper(socket, key, "silent")
	if l == nil {
		t.Errorf("fake helper: %v", lerr)
		return
	}
	defer l.Close()

	r := Relay{Socket: socket, Key: key, WorkerID: "w-1"}
	req := WireRequest{JobID: "j1", Argv: []string{"/bin/sleep"}, TimeoutS: 30}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	started := time.Now()
	code, err := r.Exec(ctx, req, nil)
	elapsed := time.Since(started)
	cancel()
	if err == nil {
		t.Errorf("expected a cancellation error, got exit code %d", code)
		return
	}
	if !strings.Contains(err.Error(), "relay cancelled") {
		t.Errorf("error %q does not say the relay was cancelled", err.Error())
	}
	if elapsed > 5*time.Second {
		t.Errorf("cancellation took %v, want prompt (helper never replied)", elapsed)
	}
}

// Mode() matrix: socket present wins everywhere; no socket + not in a
// container = direct; no socket + in a container = the actionable error.
// SocketExists/RunningInContainer are vars so the matrix is injectable.
func TestExec_ModeMatrix(t *testing.T) {
	socket, err := newTempSocket(t)
	if err != nil {
		return
	}
	keep, kerr := makeSocketFile(t, socket)
	if keep == nil {
		t.Errorf("mode matrix: %v", kerr)
		return
	}
	defer keep.Close()
	if m, _ := Mode(socket, false); m != "helper" {
		t.Errorf("socket present, not containerized: mode %q, want helper", m)
	}
	if m, _ := Mode(socket, true); m != "helper" {
		t.Errorf("socket present, containerized: mode %q, want helper", m)
	}
	if m, _ := Mode(socket+".missing", false); m != "direct" {
		t.Errorf("no socket, not containerized: mode %q, want direct", m)
	}
	m, err := Mode(socket+".missing", true)
	if m != "" {
		t.Errorf("no socket, containerized: mode %q, want empty", m)
	}
	want := "host-exec unavailable: running in a container without the mod-host-exec helper socket (see install docs)"
	if err == nil || err.Error() != want {
		t.Errorf("no socket, containerized: error %v, want %q", err, want)
	}
}

func TestExec_ModeUsesInjectedStat(t *testing.T) {
	socket, err := newTempSocket(t)
	if err != nil {
		return
	}
	saved := SocketExists
	SocketExists = func(path string) bool { return path == "injected.sock" }
	defer func() { SocketExists = saved }()
	if m, _ := Mode("injected.sock", true); m != "helper" {
		t.Errorf("injected stat hit: mode %q, want helper", m)
	}
	if m, _ := Mode(socket, true); m != "" {
		t.Errorf("injected stat miss in container: mode %q, want error", m)
	}
}

// 992b/hx.9w: DefaultSocket (unix/macOS) and DefaultPipe (Windows) must both
// be set and must not collide, or Mode()/SocketExists on one platform could
// silently probe the other platform's transport.
func TestDefaultTransportPaths_NonEmptyAndDistinct(t *testing.T) {
	if DefaultSocket == "" {
		t.Error("DefaultSocket is empty")
	}
	if DefaultPipe == "" {
		t.Error("DefaultPipe is empty")
	}
	if DefaultSocket == DefaultPipe {
		t.Errorf("DefaultSocket and DefaultPipe must differ, both are %q", DefaultSocket)
	}
}

// TestPickPath table-tests the GOOS branch as a pure function of an injected
// goos string (not runtime.GOOS), so every branch — including the Windows
// one — is exercised on a Linux CI runner with no real named pipe involved.
func TestPickPath(t *testing.T) {
	cases := []struct {
		goos string
		want string
	}{
		{"windows", DefaultPipe},
		{"linux", DefaultSocket},
		{"darwin", DefaultSocket},
		{"freebsd", DefaultSocket},
	}
	for _, c := range cases {
		if got := PickPath(c.goos); got != c.want {
			t.Errorf("PickPath(%q) = %q, want %q", c.goos, got, c.want)
		}
	}
}

// ComputeStatus matrix (993a): same branches as Mode() plus the local codes.
// IsRoot is a var so the matrix is injectable; ComputeStatus checks it the
// same way on every platform, including Windows's elevation check (993g).
func TestComputeStatusMatrix(t *testing.T) {
	socket, err := newTempSocket(t)
	if err != nil {
		return
	}
	keep, kerr := makeSocketFile(t, socket)
	if keep == nil {
		t.Errorf("compute-status matrix: %v", kerr)
		return
	}
	defer keep.Close()

	savedRoot := IsRoot
	defer func() { IsRoot = savedRoot }()

	IsRoot = func() bool { return true }
	if s := ComputeStatus(socket, true); s.Code != "available" || s.Mode != "helper" || !s.Available {
		t.Errorf("helper socket present: %+v, want available/helper", s)
	}
	if s := ComputeStatus(socket+".missing", false); s.Code != "available" || s.Mode != "direct" || !s.Available {
		t.Errorf("no socket, not containerized, root: %+v, want available/direct", s)
	}
	if s := ComputeStatus(socket+".missing", true); s.Code != "helper_missing" || s.Available {
		t.Errorf("no socket, containerized: %+v, want helper_missing/unavailable", s)
	}
	IsRoot = func() bool { return false }
	if s := ComputeStatus(socket+".missing", false); s.Code != "not_root" || s.Available {
		t.Errorf("no socket, not containerized, non-root: %+v, want not_root/unavailable", s)
	}
}
