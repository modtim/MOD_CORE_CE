package executor

// hx.7e — tests for the host-exec wiring surface (opsx worker-host-exec).
//
// What must hold: ConfigureHostExec sets every seam it is handed; nil/empty
// fields leave the fail-closed defaults in place (a half-wired install still
// refuses); the payload seam maps all seven flat HostExec* keys hx.7d added to
// transport.TaskPayload and returns nil for a non-host-exec task; and with a
// fake Runner + the httptest ingest server from hostexec_task_test.go, an
// end-to-end host-exec task streams scrubbed chunks and a final exit-code
// frame exactly like the hand-wired tests there.

import (
	"context"
	"encoding/base64"
	"net/http"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// restoreSeams puts hostexec_task.go's fail-closed defaults back after a test
// wired them (the seams are package globals; tests share one process).
func restoreSeams(t *testing.T) {
	origRunner := hostExecRunner
	origSetAuth := hostExecSetAuth
	origWorkerID := hostExecWorkerID
	origPayloadOf := hostExecPayloadOf
	t.Cleanup(func() {
		hostExecRunner = origRunner
		hostExecSetAuth = origSetAuth
		hostExecWorkerID = origWorkerID
		hostExecPayloadOf = origPayloadOf
	})
}

func TestHostExecWire_ConfigureSetsEachSeam(t *testing.T) {
	restoreSeams(t)
	wired := false
	ConfigureHostExec(HostExecWiring{
		Runner: func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
			wired = true
			return 3, nil
		},
		SetAuth: func(req *http.Request) {
			req.Header.Set("X-Worker-Name", "wired-worker")
			req.Header.Set("X-Bootstrap-Token", "tok")
		},
		WorkerID: "7",
	})
	if hostExecWorkerID != "7" {
		t.Errorf("hostExecWorkerID = %q, want %q", hostExecWorkerID, "7")
	}
	if hostExecWorkerIDOf(0) != "" || hostExecWorkerIDOf(42) != "42" {
		t.Errorf("hostExecWorkerIDOf: 0 -> %q, 42 -> %q", hostExecWorkerIDOf(0), hostExecWorkerIDOf(42))
	}
	code, err := hostExecRunner(context.Background(), HostExecRequest{Argv: []string{"x"}}, func(HostExecFrame) {})
	if !wired || code != 3 || err != nil {
		t.Errorf("runner seam after ConfigureHostExec: code=%d err=%v", code, err)
	}
	if hostExecSetAuth == nil {
		t.Fatalf("hostExecSetAuth still nil after ConfigureHostExec")
	}
	req, rerr := http.NewRequestWithContext(context.Background(), "POST", "http://api.test/x", nil)
	if rerr != nil {
		t.Fatalf("new request: %v", rerr)
	}
	hostExecSetAuth(req)
	if req.Header.Get("X-Worker-Name") != "wired-worker" || req.Header.Get("X-Bootstrap-Token") != "tok" {
		t.Errorf("auth headers not stamped")
	}
}

func TestHostExecWire_NilFieldsKeepFailClosedDefaults(t *testing.T) {
	// A half-wired install must keep whatever the seams already hold — the
	// A half-wired install must keep whatever the seams already hold — the
	// fail-closed defaults hostexec_task.go starts with, or a previously wired
	// value — never invent one. "unchanged" is the contract checked here
	// because sibling tests in hostexec_task_test.go wire the seams globally.
	restoreSeams(t)
	origWorkerID := hostExecWorkerID
	origSetAuthSet := hostExecSetAuth != nil
	origRunnerCode, origRunnerErr := hostExecRunner(context.Background(), HostExecRequest{Argv: []string{"probe"}}, func(HostExecFrame) {})
	ConfigureHostExec(HostExecWiring{})
	if hostExecWorkerID != origWorkerID || (hostExecSetAuth != nil) != origSetAuthSet {
		t.Errorf("empty wiring must not touch the auth/worker-id seams")
	}
	code, err := hostExecRunner(context.Background(), HostExecRequest{Argv: []string{"probe"}}, func(HostExecFrame) {})
	if code != origRunnerCode || (err == nil) != (origRunnerErr == nil) {
		t.Errorf("empty wiring must not replace the runner seam: before=%d/%v after=%d/%v",
			origRunnerCode, origRunnerErr != nil, code, err != nil)
	}
}

func TestHostExecWire_PayloadOfMapsAllSevenFields(t *testing.T) {
	task := &transport.Task{ID: 3, NodeID: "host-exec-9", Payload: transport.TaskPayload{
		TaskType:         "host-exec",
		HostExecJobID:    9,
		HostExecArgv:     []string{"ls", "-l"},
		HostExecCwd:      "/srv/scratch",
		HostExecEnv:      map[string]string{"A": "B"},
		HostExecRunAs:    "moduser",
		HostExecTimeoutS: 60,
		HostExecMode:     "direct",
	}}
	p := hostExecPayloadFromTask(task)
	if p == nil {
		t.Fatalf("a host-exec payload must map, got nil")
	}
	if p.JobID != 9 || len(p.Argv) != 2 || p.Cwd != "/srv/scratch" || p.RunAs != "moduser" ||
		p.TimeoutS != 60 || p.Mode != "direct" {
		t.Errorf("payload mapping wrong: %+v", p)
	}
	v, ok := p.Env["A"]
	if !ok || v != "B" {
		t.Errorf("env mapping wrong: %+v", p.Env)
	}
}

func TestHostExecWire_PayloadOfNilForNonHostExecTask(t *testing.T) {
	plain := &transport.Task{ID: 4, NodeID: "gen-1", Payload: transport.TaskPayload{
		TaskType:        "image-gen",
		ProcessingImage: "modtim/sdxl:latest",
	}}
	if hostExecPayloadFromTask(plain) != nil {
		t.Errorf("an ordinary container task must map to nil (no invented argv)")
	}
	if hostExecPayloadFromTask(nil) != nil {
		t.Errorf("nil task must map to nil")
	}
}

// restoreContainerSeam saves/restores hostExecContainerized around a test that
// calls ConfigureHostExec with ViaHelper set — that call mutates the global
// container-detection seam directly, unlike the other seams restoreSeams
// already tracks.
func restoreContainerSeam(t *testing.T) {
	orig := hostExecContainerized
	t.Cleanup(func() { hostExecContainerized = orig })
}

func TestHostExecWire_ViaHelperStandsDownContainerRefusal(t *testing.T) {
	restoreSeams(t)
	restoreContainerSeam(t)
	hostExecContainerized = func() bool { return true } // simulate "no helper" default
	ConfigureHostExec(HostExecWiring{
		Runner: func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
			return 0, nil
		},
		SetAuth:   func(req *http.Request) {},
		WorkerID:  "7",
		ViaHelper: true,
	})
	if hostExecContainerized() {
		t.Fatalf("ViaHelper+Runner must stand down the container refusal, still refusing")
	}
}

func TestHostExecWire_NativeRunnerInContainerStaysRefused(t *testing.T) {
	restoreSeams(t)
	restoreContainerSeam(t)
	hostExecContainerized = func() bool { return true }
	ConfigureHostExec(HostExecWiring{
		Runner: func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
			return 0, nil
		},
		SetAuth:  func(req *http.Request) {},
		WorkerID: "7",
		// ViaHelper left false: a native (non-helper) runner in a container
		// must still be refused.
	})
	if !hostExecContainerized() {
		t.Fatalf("native (non-ViaHelper) wiring must not touch the container refusal")
	}
}

func TestHostExecWire_ViaHelperIgnoredWithoutRunner(t *testing.T) {
	restoreSeams(t)
	restoreContainerSeam(t)
	hostExecContainerized = func() bool { return true }
	ConfigureHostExec(HostExecWiring{
		ViaHelper: true, // no Runner: must not stand down the refusal
	})
	if !hostExecContainerized() {
		t.Fatalf("ViaHelper without a Runner must not stand down the container refusal")
	}
}

func TestHostExecWire_EndToEndViaHelperRunnerInContainer(t *testing.T) {
	restoreSeams(t)
	restoreContainerSeam(t)
	e := newExecutor(true)
	var log ingestLog
	srv := newIngestServer(t, &log, 0)
	stubHostExecHTTP(t)
	e.apiURL = srv.URL

	hostExecContainerized = func() bool { return true } // "no helper socket" default
	ConfigureHostExec(HostExecWiring{
		Runner: func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
			emit(HostExecFrame{Stream: "stdout", Data: []byte("relayed")})
			return 0, nil
		},
		SetAuth: func(req *http.Request) {
			req.Header.Set("X-Worker-Name", "wired-worker")
			req.Header.Set("X-Bootstrap-Token", "tok")
		},
		WorkerID:  "7",
		ViaHelper: true,
	})

	task := &transport.Task{ID: 13, NodeID: "host-exec-43", Payload: transport.TaskPayload{
		TaskType:         "host-exec",
		HostExecJobID:    43,
		HostExecArgv:     []string{"echo", "hi"},
		HostExecTimeoutS: 30,
		HostExecMode:     "direct",
	}}
	res, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("a helper-relay runner must not be refused as containerized, got %q", err.Error())
	}
	if res == nil || res.LogTail != "relayed" {
		t.Fatalf("expected log_tail %q, got %+v", "relayed", res)
	}
}

func TestHostExecWire_EndToEndConfiguredRunner(t *testing.T) {
	restoreSeams(t)
	e := newExecutor(true)
	var log ingestLog
	srv := newIngestServer(t, &log, 0)
	stubHostExecHTTP(t)
	e.apiURL = srv.URL

	ConfigureHostExec(HostExecWiring{
		Runner: func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
			if len(req.Argv) == 0 || req.Argv[0] != "echo" {
				t.Errorf("runner got unexpected argv: %+v", req.Argv)
			}
			if req.Timeout != time.Duration(90)*time.Second {
				t.Errorf("runner got unexpected timeout: %v", req.Timeout)
			}
			emit(HostExecFrame{Stream: "stdout", Data: []byte("hello")})
			return 0, nil
		},
		SetAuth: func(req *http.Request) {
			req.Header.Set("X-Worker-Name", "wired-worker")
			req.Header.Set("X-Bootstrap-Token", "tok")
		},
		WorkerID: "7",
	})
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()

	task := &transport.Task{ID: 12, NodeID: "host-exec-42", Payload: transport.TaskPayload{
		TaskType:         "host-exec",
		HostExecJobID:    42,
		HostExecArgv:     []string{"echo", "hi"},
		HostExecCwd:      "/srv/scratch",
		HostExecEnv:      map[string]string{"K": "V"},
		HostExecRunAs:    "service",
		HostExecTimeoutS: 90,
		HostExecMode:     "direct",
	}}
	res, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("a fully configured host-exec task must succeed, got %q", err.Error())
	}
	if res == nil || res.LogTail != "hello" {
		t.Fatalf("expected log_tail %q, got %+v", "hello", res)
	}
	log.mu.Lock()
	defer log.mu.Unlock()
	if len(log.frames) != 2 {
		t.Fatalf("expected one chunk frame + one final frame, got %d", len(log.frames))
	}
	if log.frames[0].Seq != 0 || log.frames[1].Seq != 1 {
		t.Fatalf("expected seq 0 then 1, got %d then %d", log.frames[0].Seq, log.frames[1].Seq)
	}
	if log.frames[0].Stream != "stdout" {
		t.Fatalf("chunk stream = %q", log.frames[0].Stream)
	}
	chunk, derr := base64.StdEncoding.DecodeString(log.frames[0].DataB64)
	if derr != nil || string(chunk) != "hello" {
		t.Fatalf("chunk data = %q (decode err %v), want %q", string(chunk), derr, "hello")
	}
	if log.frames[1].Final == nil || *log.frames[1].Final != true {
		t.Fatalf("final frame must carry final=true, got %+v", log.frames[1])
	}
	if log.frames[1].ExitCode == nil || *log.frames[1].ExitCode != 0 {
		t.Fatalf("final frame must carry exit_code 0, got %+v", log.frames[1])
	}
}
