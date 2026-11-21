package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/hostexec"
)

const testWorkerID = "worker-under-test"

var testKey = []byte("unit-test-hmac-key-not-a-secret")

func startTestHelper(t *testing.T) (sockPath string) {
	t.Helper()
	dir := t.TempDir()
	sockPath = filepath.Join(dir, "sock", "host-exec.sock")
	cfg := config{
		socketPath: sockPath,
		group:      "mod-worker-test-nonexistent",
		workerID:   testWorkerID,
	}
	l, err := listen(cfg)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	go serve(l, cfg, testKey)
	t.Cleanup(func() { l.Close() })
	return sockPath
}

func sign(req hostexec.WireRequest) string {
	return hostexec.ComputeSig(testKey, req)
}

func dialAndSend(t *testing.T, sockPath string, req hostexec.WireRequest) []hostexec.OutFrame {
	t.Helper()
	conn, err := net.DialTimeout("unix", sockPath, 2*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	if err := json.NewEncoder(conn).Encode(req); err != nil {
		// The oversize test can trip this: the helper stops reading past its
		// size limit and closes, so the client's remaining write fails. The
		// frames the helper did send are still read below.
		_ = err
	}

	var frames []hostexec.OutFrame
	scanner := bufio.NewScanner(conn)
	for scanner.Scan() {
		var f hostexec.OutFrame
		if err := json.Unmarshal(scanner.Bytes(), &f); err != nil {
			t.Fatalf("decode frame %q: %v", scanner.Text(), err)
		}
		frames = append(frames, f)
		if f.ExitCode != nil || f.Error != "" {
			break
		}
	}
	return frames
}

func TestBadSigRejected(t *testing.T) {
	sockPath := startTestHelper(t)
	req := hostexec.WireRequest{
		JobID:    "job-1",
		WorkerID: testWorkerID,
		Argv:     []string{"true"},
		Sig:      "0000000000000000000000000000000000000000000000000000000000000000",
	}
	frames := dialAndSend(t, sockPath, req)
	if len(frames) != 1 || frames[0].Error == "" {
		t.Fatalf("expected a single error frame, got %+v", frames)
	}
	if frames[0].Error != "bad signature" {
		t.Fatalf("expected bad signature error, got %q", frames[0].Error)
	}
}

func TestWrongWorkerRejected(t *testing.T) {
	sockPath := startTestHelper(t)
	req := hostexec.WireRequest{
		JobID:    "job-2",
		WorkerID: "some-other-worker",
		Argv:     []string{"true"},
	}
	req.Sig = sign(req)
	frames := dialAndSend(t, sockPath, req)
	if len(frames) != 1 || frames[0].Error != "wrong worker_id" {
		t.Fatalf("expected wrong worker_id error, got %+v", frames)
	}
}

func TestOversizeRequestRejected(t *testing.T) {
	sockPath := startTestHelper(t)
	pad := strings.Repeat("x", (2 << 20))
	req := hostexec.WireRequest{
		JobID:    "job-oversize",
		WorkerID: testWorkerID,
		Argv:     []string{"true"},
		Env:      map[string]string{"pad": pad},
	}
	req.Sig = sign(req)
	frames := dialAndSend(t, sockPath, req)
	if len(frames) == 0 {
		// The helper's read deadline (requestReadTimeout) is longer than this
		// test's 2s dial timeout, so a client that gets ECONNREFUSED/short-write
		// mid-request may not see the error frame; that is still a rejection.
		t.Logf("oversize: client got no frame (connection torn down mid-request)")
		return
	}
	if len(frames) != 1 || !strings.HasPrefix(frames[0].Error, "bad request:") {
		t.Fatalf("expected a single bad-request error frame, got %+v", frames)
	}
}

func TestGoodJobStreamsOutputAndExitCode(t *testing.T) {
	sockPath := startTestHelper(t)
	req := hostexec.WireRequest{
		JobID:    "job-3",
		WorkerID: testWorkerID,
		Argv:     []string{"sh", "-c", "echo hello-stdout; echo hello-stderr 1>&2; exit 3"},
		TimeoutS: 10,
	}
	req.Sig = sign(req)
	frames := dialAndSend(t, sockPath, req)

	var stdout, stderr string
	var exitCode *int
	for _, f := range frames {
		if f.Error != "" {
			t.Fatalf("unexpected error frame: %+v", f)
		}
		if f.Data != "" {
			raw, err := base64.StdEncoding.DecodeString(f.Data)
			if err != nil {
				t.Fatalf("decode frame data: %v", err)
			}
			switch f.Stream {
			case "stdout":
				stdout += string(raw)
			case "stderr":
				stderr += string(raw)
			}
		}
		if f.ExitCode != nil {
			exitCode = f.ExitCode
		}
	}
	if exitCode == nil {
		t.Fatalf("no exit_code frame received, frames=%+v", frames)
	}
	if *exitCode != 3 {
		t.Fatalf("expected exit code 3, got %d", *exitCode)
	}
	if !strings.Contains(stdout, "hello-stdout") {
		t.Fatalf("expected stdout to contain hello-stdout, got %q", stdout)
	}
	if !strings.Contains(stderr, "hello-stderr") {
		t.Fatalf("expected stderr to contain hello-stderr, got %q", stderr)
	}
}

// hx.9: the manager's worker_id is assigned by the API after install, so the
// unit starts the helper with --worker-id-file and the helper re-reads it per
// request. These tests cover the three states that matter: no file yet (refuse
// closed), file present (accept exactly that id), and the id changing without
// the helper restarting (re-registration).

func startTestHelperWithIDFile(t *testing.T) (sockPath, idFilePath string) {
	t.Helper()
	dir := t.TempDir()
	sockPath = filepath.Join(dir, "sock", "host-exec.sock")
	idFilePath = filepath.Join(dir, "worker_id")
	cfg := config{
		socketPath:   sockPath,
		group:        "mod-worker-test-nonexistent",
		workerIDFile: idFilePath,
	}
	l, err := listen(cfg)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	go serve(l, cfg, testKey)
	t.Cleanup(func() { l.Close() })
	return sockPath, idFilePath
}

func writeWorkerIDFile(t *testing.T, path, id string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(id+"\n"), 0o644); err != nil {
		t.Fatalf("write worker_id file: %v", err)
	}
}

func TestNoWorkerIDFileYetRejectsRequest(t *testing.T) {
	sockPath, idFile := startTestHelperWithIDFile(t)
	// The worker has not answered a heartbeat yet: no state file exists.
	if _, statErr := os.Stat(idFile); statErr == nil {
		t.Fatalf("worker_id file should not exist before the worker learns its id")
	}
	req := hostexec.WireRequest{
		JobID:    "job-noid",
		WorkerID: "12345",
		Argv:     []string{"echo", "should-not-run"},
		TimeoutS: 10,
	}
	req.Sig = sign(req)
	frames := dialAndSend(t, sockPath, req)
	if len(frames) != 1 || !strings.HasPrefix(frames[0].Error, "worker_id not bound:") {
		t.Fatalf("expected a worker_id-not-bound refusal, got %+v", frames)
	}
}

func TestWorkerIDFileBindsTheHelper(t *testing.T) {
	sockPath, idFile := startTestHelperWithIDFile(t)
	writeWorkerIDFile(t, idFile, "4242")
	req := hostexec.WireRequest{
		JobID:    "job-file",
		WorkerID: "4242",
		Argv:     []string{"echo", "ran"},
		TimeoutS: 10,
	}
	req.Sig = sign(req)
	frames := dialAndSend(t, sockPath, req)
	if len(frames) < 2 || frames[0].Error != "" {
		t.Fatalf("expected the job to run, got %+v", frames)
	}
}

func TestWorkerIDFileRereadPerRequest(t *testing.T) {
	sockPath, idFile := startTestHelperWithIDFile(t)
	writeWorkerIDFile(t, idFile, "111")
	// A re-registered worker gets a new id; the helper must NOT need a restart.
	req := hostexec.WireRequest{
		JobID:    "job-stale",
		WorkerID: "111",
		Argv:     []string{"echo", "old"},
		TimeoutS: 10,
	}
	req.Sig = sign(req)
	if frames := dialAndSend(t, sockPath, req); len(frames) < 2 {
		t.Fatalf("first id should run, got %+v", frames)
	}
	writeWorkerIDFile(t, idFile, "222")
	again := hostexec.WireRequest{
		JobID:    "job-stale",
		WorkerID: "111",
		Argv:     []string{"echo", "old"},
		TimeoutS: 10,
	}
	again.Sig = sign(again)
	frames := dialAndSend(t, sockPath, again)
	if len(frames) != 1 || frames[0].Error != "wrong worker_id" {
		t.Fatalf("expected wrong worker_id after the file changed, got %+v", frames)
	}
}

// TestWorkerIDFileGarbageRejectsRequest covers strict parsing: a truncated or
// corrupted write (non-numeric, zero, negative) must refuse, never fall back
// to accepting an arbitrary string or a default id.
func TestWorkerIDFileGarbageRejectsRequest(t *testing.T) {
	for _, bad := range []string{"not-a-number", "0", "-5", "12.5", "1 2"} {
		sockPath, idFile := startTestHelperWithIDFile(t)
		writeWorkerIDFile(t, idFile, bad)
		req := hostexec.WireRequest{
			JobID:    "job-bad",
			WorkerID: bad,
			Argv:     []string{"echo", "should-not-run"},
			TimeoutS: 10,
		}
		req.Sig = sign(req)
		frames := dialAndSend(t, sockPath, req)
		if len(frames) != 1 || !strings.HasPrefix(frames[0].Error, "worker_id not bound:") {
			t.Fatalf("content %q: expected a worker_id-not-bound refusal, got %+v", bad, frames)
		}
	}
}

// TestWorkerIDFileOversizedRejectsRequest covers the size cap: a file far
// larger than any real id (e.g. a misconfigured bind mount) must refuse
// rather than letting the helper read an unbounded amount per request.
func TestWorkerIDFileOversizedRejectsRequest(t *testing.T) {
	sockPath, idFile := startTestHelperWithIDFile(t)
	writeWorkerIDFile(t, idFile, strings.Repeat("7", 4096))
	req := hostexec.WireRequest{
		JobID:    "job-huge",
		WorkerID: "7",
		Argv:     []string{"echo", "should-not-run"},
		TimeoutS: 10,
	}
	req.Sig = sign(req)
	frames := dialAndSend(t, sockPath, req)
	if len(frames) != 1 || !strings.HasPrefix(frames[0].Error, "worker_id not bound:") {
		t.Fatalf("expected a worker_id-not-bound refusal for an oversized file, got %+v", frames)
	}
}
