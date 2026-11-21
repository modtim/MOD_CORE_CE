package executor

// hx.7c — host-exec task routing tests (opsx worker-host-exec).
//
// What must hold before the relay is wired: a host-exec task never reaches the
// container path; the off switch, sandboxed mode and a containerized worker all
// FAIL the task rather than running anything; and once the seams ARE wired
// (which is what these tests do), output batches into <=32KB chunks with
// strictly increasing seq, ends in a final frame carrying the exit code, and
// every chunk is scrubbed before it leaves.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// ingestBody is one POST to the API's output ingest endpoint
// (routers/worker_host_exec_output.py OutputChunkRequest).
type ingestBody struct {
	Seq      int    `json:"seq"`
	Stream   string `json:"stream"`
	DataB64  string `json:"data_b64"`
	Final    *bool  `json:"final"`
	ExitCode *int   `json:"exit_code"`
}

// ingestLog accumulates what the fake ingest endpoint received, in order.
type ingestLog struct {
	mu     sync.Mutex
	frames []ingestBody
}

// stubHostExecHTTP redirects hostExecHTTPDo at the httptest server, restoring the
// real one on cleanup (gatewayscrape_test.go's stubHTTPDo shape).
func stubHostExecHTTP(t *testing.T) {
	old := hostExecHTTPDo
	hostExecHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("host-exec http: %w", err)
		}
		return resp, nil
	}
	t.Cleanup(func() { hostExecHTTPDo = old })
}

// newIngestServer answers 200 (rejectFirst requests answer 502 first) and
// records each body in ingestLog.
func newIngestServer(t *testing.T, log *ingestLog, rejectFirst int) *httptest.Server {
	remaining := rejectFirst
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var got ingestBody
		if err := json.Unmarshal(raw, &got); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		log.mu.Lock()
		defer log.mu.Unlock()
		if remaining > 0 {
			remaining--
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		log.frames = append(log.frames, got)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// wireSeams points the auth/worker-id seams at a wired relay (hx.7b's job).
func wireSeams(workerID string) {
	hostExecWorkerID = workerID
	hostExecSetAuth = func(req *http.Request) {
		req.Header.Set("X-Worker-Name", "test-worker")
		req.Header.Set("X-Bootstrap-Token", "tok")
	}
}

// withPayload wires the payload seam so a test can hand the handler an
// explicit host-exec payload.
func withPayload(p *HostExecPayload) func() {
	orig := hostExecPayloadOf
	hostExecPayloadOf = func(task *transport.Task) *HostExecPayload { return p }
	return func() { hostExecPayloadOf = orig }
}

// withHostExecContainerized forces the container verdict (the real check reads /proc).
func withHostExecContainerized(v bool) func() {
	orig := hostExecContainerized
	hostExecContainerized = func() bool { return v }
	return func() { hostExecContainerized = orig }
}

// withSandboxWrapper wires the hx.17 sandbox-wrapper seam: wrapped is what the
// wrapper hands the runner (empty Argv = the wrapper refused to wrap), err is
// what it answers instead.
func withSandboxWrapper(wrapped HostExecRequest, err error) func() {
	orig := hostExecSandboxWrap
	hostExecSandboxWrap = func(req HostExecRequest) (HostExecRequest, error) {
		return wrapped, err
	}
	return func() { hostExecSandboxWrap = orig }
}

// withRunner wires the exec seam to a script that emits the given frames and
// returns the given exit code.
func withRunner(frames [][]string, exit int) func() {
	orig := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		for _, f := range frames {
			emit(HostExecFrame{Stream: f[0], Data: []byte(f[1])})
		}
		return exit, nil
	}
	return func() { hostExecRunner = orig }
}

func newExecutor(tunablesOn bool) *Executor {
	var store *tunables.Store
	if tunablesOn {
		store = tunables.New()
		store.Apply(map[string]any{hostExecEnabledTunableKey: true})
	}
	return &Executor{apiURL: "http://api.test", tunables: store}
}

func newPayload(jobID int, mode string) *HostExecPayload {
	return &HostExecPayload{
		JobID:    jobID,
		Argv:     []string{"echo", "hi"},
		Cwd:      "/srv/scratch",
		Env:      map[string]string{"K": "V"},
		RunAs:    "service",
		TimeoutS: 90,
		Mode:     mode,
	}
}

func hostExecTask(id int, taskType, nodeID string) *transport.Task {
	return &transport.Task{ID: id, NodeID: nodeID, Payload: transport.TaskPayload{TaskType: taskType}}
}

func TestHostExecPayloadParse_DirectModeRuns(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreRunner := withRunner([][]string{{"stdout", "hello"}}, 0)
	defer restoreRunner()
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	var log ingestLog
	srv := newIngestServer(t, &log, 0)
	stubHostExecHTTP(t)
	e.apiURL = srv.URL
	p := newPayload(42, "direct")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(11, "host-exec", "host-exec-42")
	res, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("a wired direct host-exec task must succeed, got %q", err.Error())
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

func TestHostExecTaskDisabled_FailsClosed(t *testing.T) {
	e := newExecutor(false)
	task := hostExecTask(12, "host-exec", "host-exec-42")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil || !strings.Contains(err.Error(), hostExecDisabledMsg) {
		t.Fatalf("expected %q with the tunable off, got %q", hostExecDisabledMsg, err.Error())
	}
}

func TestHostExecTaskSandboxed_FailsClosed(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	ran := false
	origRunner := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		ran = true
		return 0, nil
	}
	defer func() { hostExecRunner = origRunner }()
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	p := newPayload(42, "sandboxed")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(13, "host-exec", "host-exec-42")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if ran {
		t.Fatal("a sandboxed host-exec task must NEVER run before hx.17 wires the wrapper")
	}
	if err == nil || !strings.Contains(err.Error(), hostExecSandboxMsg) {
		t.Fatalf("expected %q, got %q", hostExecSandboxMsg, err.Error())
	}
}

func TestHostExecSandboxed_WrapperErrorFailsClosed(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	ran := false
	origRunner := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		ran = true
		return 0, nil
	}
	defer func() { hostExecRunner = origRunner }()
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	restoreWrap := withSandboxWrapper(HostExecRequest{}, errors.New("sandbox setup failed"))
	defer restoreWrap()
	p := newPayload(43, "sandboxed")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(21, "host-exec", "host-exec-43")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if ran {
		t.Fatal("a sandboxed task whose wrapper errored must run NOTHING")
	}
	if err == nil || !strings.Contains(err.Error(), "sandbox wrapper") {
		t.Fatalf("expected a sandbox-wrapper failure, got %q", err.Error())
	}
}

func TestHostExecSandboxed_EmptyWrappedArgvFailsClosed(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	ran := false
	origRunner := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		ran = true
		return 0, nil
	}
	defer func() { hostExecRunner = origRunner }()
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	restoreWrap := withSandboxWrapper(HostExecRequest{}, nil)
	defer restoreWrap()
	p := newPayload(44, "sandboxed")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(22, "host-exec", "host-exec-44")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if ran {
		t.Fatal("a wrapped req with empty argv must never reach the runner")
	}
	if err == nil || !strings.Contains(err.Error(), "empty argv") {
		t.Fatalf("expected an empty-argv wrapper failure, got %q", err.Error())
	}
}

func TestHostExecSandboxed_WrappedArgvRunsToSink(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	wrapped := HostExecRequest{
		Argv:    []string{"systemd-run", "--wait", "--", "echo", "hi"},
		Cwd:     "/srv/scratch",
		Env:     map[string]string{"K": "V"},
		RunAs:   "service",
		Timeout: 90 * time.Second,
	}
	restoreWrap := withSandboxWrapper(wrapped, nil)
	defer restoreWrap()
	var seen []string
	origRunner := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		seen = req.Argv
		emit(HostExecFrame{Stream: "stdout", Data: []byte("hi")})
		return 0, nil
	}
	defer func() { hostExecRunner = origRunner }()
	var log ingestLog
	srv := newIngestServer(t, &log, 0)
	stubHostExecHTTP(t)
	e.apiURL = srv.URL
	p := newPayload(45, "sandboxed")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(23, "host-exec", "host-exec-45")
	res, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("a wired sandboxed host-exec task must succeed, got %q", err.Error())
	}
	want := []string{"systemd-run", "--wait", "--", "echo", "hi"}
	if len(seen) != len(want) {
		t.Fatalf("runner must receive the WRAPPED argv %+v, got %+v", want, seen)
	}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("runner must receive the WRAPPED argv %+v, got %+v", want, seen)
		}
	}
	if res == nil || res.LogTail != "hi" {
		t.Fatalf("expected log_tail %q, got %+v", "hi", res)
	}
	log.mu.Lock()
	defer log.mu.Unlock()
	if len(log.frames) != 2 {
		t.Fatalf("expected one chunk frame + one final frame, got %d", len(log.frames))
	}
	if log.frames[0].Seq != 0 || log.frames[1].Seq != 1 {
		t.Fatalf("expected seq 0 then 1, got %d then %d", log.frames[0].Seq, log.frames[1].Seq)
	}
	chunk, derr := base64.StdEncoding.DecodeString(log.frames[0].DataB64)
	if derr != nil || string(chunk) != "hi" {
		t.Fatalf("chunk data = %q (decode err %v), want %q", string(chunk), derr, "hi")
	}
	if log.frames[1].Final == nil || *log.frames[1].Final != true {
		t.Fatalf("final frame must carry final=true, got %+v", log.frames[1])
	}
	if log.frames[1].ExitCode == nil || *log.frames[1].ExitCode != 0 {
		t.Fatalf("final frame must carry exit_code 0, got %+v", log.frames[1])
	}
}

func TestHostExecTaskContainerized_FailsClosed(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(true)
	defer restoreContainer()
	p := newPayload(42, "direct")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(14, "host-exec", "host-exec-42")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil || !strings.Contains(err.Error(), hostExecContainerMsg) {
		t.Fatalf("expected %q on a containerized worker, got %q", hostExecContainerMsg, err.Error())
	}
}

func TestHostExecChunking_SeqOrderAndFinalFrame(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	stubHostExecHTTP(t)
	var log ingestLog
	srv := newIngestServer(t, &log, 0)
	e.apiURL = srv.URL

	// 70KB of stdout in one emitted frame: the sink must split it into
	// <=32KB chunks (3 chunks) and never reorder or reuse a seq.
	big := strings.Repeat("x", 70*1024)
	restoreRunner := withRunner([][]string{{"stdout", big}}, 7)
	defer restoreRunner()
	p := newPayload(43, "direct")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(15, "host-exec", "host-exec-43")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatal("a non-zero host-exec exit must fail the task, got a success")
	}
	if !strings.Contains(err.Error(), "exited 7") {
		t.Fatalf("expected exit code 7 in the failure, got %q", err.Error())
	}
	log.mu.Lock()
	defer log.mu.Unlock()
	if len(log.frames) != 4 {
		t.Fatalf("expected 3 chunks + final frame, got %d", len(log.frames))
	}
	for i, f := range log.frames[:3] {
		if f.Seq != i {
			t.Fatalf("frame %d has seq %d (must increase strictly)", i, f.Seq)
		}
		if len(f.DataB64) == 0 {
			t.Fatalf("frame %d is empty", i)
		}
	}
	last := log.frames[3]
	if last.Seq != 3 || last.Final == nil || *last.Final != true || last.ExitCode == nil || *last.ExitCode != 7 {
		t.Fatalf("final frame = %+v, want seq 3 final exit 7", last)
	}
}

func TestHostExecRetry_BackoffSurvivesTransient502(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	stubHostExecHTTP(t)
	var log ingestLog
	srv := newIngestServer(t, &log, 2) // first two attempts are 502
	e.apiURL = srv.URL
	restoreRunner := withRunner([][]string{{"stdout", "line-1"}}, 0)
	defer restoreRunner()
	p := newPayload(44, "direct")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(16, "host-exec", "host-exec-44")
	res, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("a transient 502 must be retried, not fail the stream: %q", err.Error())
	}
	if res == nil {
		t.Fatal("expected a Result")
	}
	log.mu.Lock()
	defer log.mu.Unlock()
	if len(log.frames) != 2 {
		t.Fatalf("expected chunk + final after retries, got %d", len(log.frames))
	}
}

func TestHostExecScrub_AppliedBeforeLeavingWorker(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	stubHostExecHTTP(t)
	var log ingestLog
	srv := newIngestServer(t, &log, 0)
	e.apiURL = srv.URL
	restoreRunner := withRunner([][]string{{"stdout", "auth: Bearer supersecrettoken"}}, 0)
	defer restoreRunner()
	p := newPayload(45, "direct")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(17, "host-exec", "host-exec-45")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("expected success, got %q", err.Error())
	}
	log.mu.Lock()
	defer log.mu.Unlock()
	if len(log.frames) == 0 {
		t.Fatal("expected at least one chunk")
	}
	chunk, derr := base64.StdEncoding.DecodeString(log.frames[0].DataB64)
	if derr != nil {
		t.Fatalf("chunk decode: %v", derr)
	}
	if strings.Contains(string(chunk), "supersecrettoken") {
		t.Fatalf("chunk must be scrubbed before it leaves the worker, got %q", string(chunk))
	}
	if !strings.Contains(string(chunk), redactedTokenPlaceholder) {
		t.Fatalf("expected the scrubber's %q in the chunk, got %q", redactedTokenPlaceholder, string(chunk))
	}
}

func TestHostExecRouting_RunDispatchesOnTaskType(t *testing.T) {
	e := newExecutor(true)
	task := hostExecTask(18, "host-exec", "host-exec-46")
	// Nothing is wired (hx.7b pending), so Run must route into the host-exec
	// handler and fail there — never fall through to the container path.
	_, err := e.Run(context.Background(), task, &transport.Credentials{}, Callbacks{})
	if err == nil {
		t.Fatal("expected Run() to fail a host-exec task with nothing wired")
	}
	if !strings.Contains(err.Error(), hostExecPayloadMissingMsg) {
		t.Fatalf("expected the host-exec handler's payload refusal, got %q", err.Error())
	}
}

func TestHostExecRouting_NonHostExecTaskUnaffected(t *testing.T) {
	e := &Executor{}
	task := &transport.Task{ID: 19, Payload: transport.TaskPayload{TaskType: "processing"}}
	_, err := e.Run(context.Background(), task, &transport.Credentials{}, Callbacks{})
	if err == nil {
		t.Fatal("expected an ordinary task to fail for an unrelated reason")
	}
	if strings.Contains(err.Error(), hostExecDisabledMsg) || strings.Contains(err.Error(), hostExecPayloadMissingMsg) {
		t.Fatalf("a non-host-exec task must not route into the host-exec handler, got %q", err.Error())
	}
}

// TestHostExecTask_CtxCancelStopsRun is the routing half of hx.11: a task
// cancel / worker shutdown must not leave handleHostExecTask parked inside the
// runner. The stub blocks on ctx.Done() exactly where the real runner's cmd.Wait
// sits, the test cancels the parent ctx once the stub has parked, and the
// handler must come back promptly. req.TimeoutS is 600s, far beyond every bound
// here, so only the cancel can end the run.
func TestHostExecTask_CtxCancelStopsRun(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	stubHostExecHTTP(t)
	var log ingestLog
	srv := newIngestServer(t, &log, 0)
	e.apiURL = srv.URL

	// parked fires once the stub has emitted its frame and is blocking on the
	// ctx — the cancel must happen after that, so the run is genuinely mid-flight.
	parked := make(chan struct{})
	origRunner := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		// one small frame: it stays buffered in the sink (below the chunk size)
		// and only reaches the wire via flushFinal after the run ends.
		emit(HostExecFrame{Stream: "stdout", Data: []byte("partial before cancel\n")})
		parked <- struct{}{}
		<-ctx.Done()
		// the real runner's child took SIGTERM from the group kill: exit 143
		return 143, nil
	}
	defer func() { hostExecRunner = origRunner }()

	p := newPayload(44, "direct")
	p.TimeoutS = 600
	restorePayload := withPayload(p)
	defer restorePayload()

	type outcome struct {
		res *Result
		err error
	}
	outer, cancel := context.WithCancel(context.Background())
	ran := make(chan outcome, 1)
	task := hostExecTask(21, "host-exec", "host-exec-44")
	go func() {
		res, err := e.handleHostExecTask(outer, task, nil, time.Now(), Callbacks{})
		ran <- outcome{res: res, err: err}
	}()

	<-parked
	cancel()
	start := time.Now()
	// handleHostExecTask must return on its own after the cancel — a handler
	// that only honoured timeout_s would still be sitting in the runner for
	// ~600s.
	got := make(chan outcome, 1)
	select {
	case o := <-ran:
		got <- o
	case <-time.After(5 * time.Second):
		t.Fatal("handleHostExecTask still unreturned 5s after ctx cancel (timeout_s was 600)")
	}
	elapsed := time.Since(start)
	o := <-got
	if elapsed > 5*time.Second {
		t.Fatalf("handleHostExecTask took %v after cancel, want well under the 600s timeout", elapsed)
	}
	if o.err == nil {
		t.Fatal("a cancelled host-exec run must not report success")
	}
	// hx.11b: the run's cancellation is reported, and the sink's END-OF-RUN
	// posts (the pending flush + the closing frame) survive the cancelled task
	// ctx — they run on a detached context — so the job is closed on the wire,
	// never left RUNNING.
	why := o.err.Error()
	if !strings.Contains(why, "exited 143") {
		t.Fatalf("cancellation reported as %q, want the cancelled run's exit status", why)
	}
	log.mu.Lock()
	defer log.mu.Unlock()
	if len(log.frames) != 2 {
		t.Fatalf("expected the flushed pending chunk + the closing frame after cancel, got %d frames", len(log.frames))
	}
	if log.frames[0].Final != nil {
		t.Fatalf("frame 0 is the pending-chunk flush, want final unset: %+v", log.frames[0])
	}
	decoded, derr := base64.StdEncoding.DecodeString(log.frames[0].DataB64)
	if derr != nil || !strings.Contains(string(decoded), "partial before cancel") {
		t.Fatalf("the output buffered before the cancel never reached the wire: %+v %v", log.frames[0], derr)
	}
	last := log.frames[1]
	if last.Final == nil || *last.Final != true {
		t.Fatalf("no closing frame posted after ctx cancel — the job would stay RUNNING: %+v", last)
	}
	if last.ExitCode == nil || *last.ExitCode != 143 {
		t.Fatalf("closing frame must carry the honest cancelled-run exit status (143), got %+v", last)
	}
}

// TestHostExecFinalFrame409_NotRetried: the API may already have ended the job
// when the closing frame arrives (a task cancel closes it server-side first).
// A 404/409 on the FINAL frame is terminal: log at info, post it exactly once,
// do not burn the retry/backoff ladder, and do not fail the task louder than
// the run's own status.
func TestHostExecFinalFrame409_NotRetried(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	stubHostExecHTTP(t)
	var log ingestLog
	var finalAttempts sync.Mutex
	finalPosts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var got ingestBody
		if err := json.Unmarshal(raw, &got); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got.Final != nil && *got.Final {
			finalAttempts.Lock()
			defer finalAttempts.Unlock()
			finalPosts++
			w.WriteHeader(http.StatusConflict)
			return
		}
		log.mu.Lock()
		defer log.mu.Unlock()
		log.frames = append(log.frames, got)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	e.apiURL = srv.URL

	origRunner := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		emit(HostExecFrame{Stream: "stdout", Data: []byte("job cancelled server-side first\n")})
		return 143, nil
	}
	defer func() { hostExecRunner = origRunner }()

	p := newPayload(45, "direct")
	p.TimeoutS = 600
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(22, "host-exec", "host-exec-45")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatal("a non-zero host-exec exit must still fail the task")
	}
	if !strings.Contains(err.Error(), "exited 143") {
		t.Fatalf("want the run's own exit status, got %q", err.Error())
	}
	if strings.Contains(err.Error(), "final output frame") {
		t.Fatalf("a 409 on the closing frame must not be reported as a final-frame failure: %q", err.Error())
	}
	var posts int
	finalAttempts.Lock()
	defer finalAttempts.Unlock()
	posts = finalPosts
	if posts != 1 {
		t.Fatalf("closing frame posted %d times, want exactly 1 (409 is terminal, no retry ladder)", posts)
	}
}

// blockedIngestServer answers 200 to every POST; the request whose seq equals
// blockAt gets the 958a enforce verdict (accepted=false, blocked=true,
// blocked_reason="output_scan_blocked"). Every received body is logged.
func blockedIngestServer(t *testing.T, log *ingestLog, blockAt int) *httptest.Server {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var got ingestBody
		if err := json.Unmarshal(raw, &got); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		log.mu.Lock()
		defer log.mu.Unlock()
		log.frames = append(log.frames, got)
		if got.Seq == blockAt {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte(`{"accepted": false, "status": "failed", ` +
				`"blocked": true, "blocked_reason": "output_scan_blocked"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"accepted": true, "status": "ok"}`))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestHostExecOutputScanBlocked_StopsStreamAndCancelsRun(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	defer restoreContainer()
	stubHostExecHTTP(t)
	var log ingestLog
	srv := blockedIngestServer(t, &log, 1) // chunk #2 (seq 1) is blocked
	e.apiURL = srv.URL

	// The stub runner watches its ctx: when the sink cancels the run (the
	// existing cancel/timeout path) ctx is done, which is how a real runner's
	// SIGTERM/SIGKILL watcher fires.
	var cancelled atomic.Bool
	origRunner := hostExecRunner
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		// 70KB in one frame: the sink flushes 3 whole chunks as they fill.
		emit(HostExecFrame{Stream: "stdout", Data: []byte(strings.Repeat("x", 70*1024))})
		select {
		case <-ctx.Done():
			cancelled.Store(true)
		case <-time.After(200 * time.Millisecond):
		}
		return 0, nil
	}
	defer func() { hostExecRunner = origRunner }()

	p := newPayload(60, "direct")
	restorePayload := withPayload(p)
	defer restorePayload()

	task := hostExecTask(30, "host-exec", "host-exec-60")
	_, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatal("a blocked output chunk must fail the task, got a success")
	}
	if !strings.Contains(err.Error(), "output_scan_blocked") {
		t.Fatalf("terminal reason must be output_scan_blocked, got %q", err.Error())
	}
	if !cancelled.Load() {
		t.Fatal("the host run must be stopped through the cancel path after a blocked chunk")
	}
	log.mu.Lock()
	posts := len(log.frames)
	// Only the blocked chunk's own POST and everything before it may have
	// landed: 2 chunks, no closing frame, no retry of the blocked chunk.
	thirdStream := false
	for _, f := range log.frames {
		if f.Final != nil && *f.Final {
			t.Fatal("a blocked chunk must stop the sink before the closing frame")
		}
	}
	_ = thirdStream
	if posts != 2 {
		t.Fatalf("expected exactly 2 chunk POSTs (stop after the blocked one), got %d", posts)
	}
	log.mu.Unlock()
}

func TestHostExecOutputScanVerdictAbsent_BehaviourUnchanged(t *testing.T) {
	e := newExecutor(true)
	wireSeams("7")
	restoreContainer := withHostExecContainerized(false)
	_ = restoreContainer
	// no blocked chunk: blockAt is out of range, every POST answers the plain
	// verdict body with no blocked keys at all.
	origContainer := withHostExecContainerized(false)
	defer origContainer()
	stubHostExecHTTP(t)
	var log ingestLog
	srv := blockedIngestServer(t, &log, -1)
	e.apiURL = srv.URL
	origRunner := hostExecRunner
	defer func() { hostExecRunner = origRunner }()
	hostExecRunner = func(ctx context.Context, req HostExecRequest, emit func(HostExecFrame)) (int, error) {
		emit(HostExecFrame{Stream: "stdout", Data: []byte(strings.Repeat("y", 70*1024))})
		return 0, nil
	}
	p := newPayload(61, "direct")
	restorePayload := withPayload(p)
	defer restorePayload()
	task := hostExecTask(31, "host-exec", "host-exec-61")
	res, err := e.handleHostExecTask(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("an absent blocked key must not change anything: %q", err.Error())
	}
	if res == nil {
		t.Fatal("expected a Result")
	}
	log.mu.Lock()
	defer log.mu.Unlock()
	if len(log.frames) != 4 {
		t.Fatalf("expected 3 chunks + closing frame, got %d", len(log.frames))
	}
}
