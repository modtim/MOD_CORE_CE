package main

// hx.18 - startup wiring of the host-exec seams. The worker learns its numeric
// worker_id only from the API's first answered heartbeat, so the wiring must
// WAIT for it, hand wireHostExec exactly once, install a fail-closed sandbox
// adapter (wrapper error -> empty request + error; success -> only Argv
// replaced), and never list "/" or an empty path as Inaccessible.

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/heartbeat"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/hostexec"
)

func hx18Log() *slog.Logger { return slog.New(slog.NewTextHandler(new(bytes.Buffer), nil)) }

func hx18Cfg() *config.Config { return &config.Config{} }

var hx18RealContainer = hostexec.RunningInContainer
var hx18RealSocket = hostexec.SocketExists
var hx18RealAdapterSet = adapterSet

func hx18Restore() {
	hostExecWorkerIDProvider = heartbeat.WorkerID
	wireHostExecFn = wireHostExec
	hostExecSandboxWrapFn = hostexec.SandboxArgv
	hostExecKeyFile = "/etc/mod-worker/host-exec.key"
	hostExecSocketPath = hostexec.DefaultSocket
	hostExecWireWaitS = 2 * time.Second
	adapterFn = nil
	adapterSet = hx18RealAdapterSet
	hostexec.RunningInContainer = hx18RealContainer
	hostexec.SocketExists = hx18RealSocket
}

// hx18Native makes hostexec.Mode pick the native (direct) path: no helper
// socket, not containerized, so wireHostExec never reads a signing key.
func hx18Native() {
	hostexec.SocketExists = func(_ string) bool { return false }
	hostexec.RunningInContainer = func() bool { return false }
}

// hx18Named makes the id provider report a real worker id immediately.
func hx18Named(id int64) {
	hostExecWorkerIDProvider = func() int64 { return id }
}

func TestStartHostExecWiring_waitsThenWiresExactlyOnce(t *testing.T) {
	t.Cleanup(hx18Restore)
	hostExecWireWaitS = time.Millisecond
	hx18Native()

	var beats atomic.Int64
	var wired atomic.Int64

	// The API answers three beats before it names this worker.
	hostExecWorkerIDProvider = func() int64 {
		n := beats.Add(1)
		if n < 4 {
			return 0
		}
		return 41
	}
	wireHostExecFn = func(_ *config.Config, _ *slog.Logger) { wired.Add(1) }

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	startHostExecWiring(ctx, hx18Cfg(), hx18Log())
	_ = cancel

	deadline := time.Now().Add(2 * time.Second)
	for wired.Load() == 0 {
		if time.Now().After(deadline) {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if got := wired.Load(); got != 1 {
		t.Fatalf("wireHostExec handed over %d times (want exactly 1), beats=%d", got, beats.Load())
	}
	if beats.Load() < 4 {
		t.Fatalf("wired before the provider flipped >0 (beats=%d)", beats.Load())
	}
}

func TestStartHostExecWiring_cancelStopsWithoutWiring(t *testing.T) {
	t.Cleanup(hx18Restore)
	hostExecWireWaitS = time.Millisecond
	hx18Native()

	var wired atomic.Int64
	var beats atomic.Int64
	hostExecWorkerIDProvider = func() int64 {
		beats.Add(1)
		return 0 // the API never names this worker
	}
	wireHostExecFn = func(_ *config.Config, _ *slog.Logger) { wired.Add(1) }

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	startHostExecWiring(ctx, hx18Cfg(), hx18Log())
	_ = cancel

	deadline := time.Now().Add(300 * time.Millisecond)
	for time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if got := wired.Load(); got != 0 {
		t.Fatalf("wired %d times while the id stayed 0 (want 0)", got)
	}
}

func TestHostExecSandboxAdapter_errorFailsClosed(t *testing.T) {
	t.Cleanup(hx18Restore)
	hx18Native()
	hx18Named(7)

	// A wrapper that always errors (e.g. no systemd-run on the host).
	hostExecSandboxWrapFn = func(_ hostexec.SandboxOptions, _ []string) ([]string, error) {
		return nil, errors.New("no systemd-run: refusing to run unsandboxed")
	}

	adapterFn = nil
	var seen func(executor.HostExecRequest) (executor.HostExecRequest, error)
	adapterSet = func(f func(executor.HostExecRequest) (executor.HostExecRequest, error)) { seen = f }
	wireHostExec(hx18Cfg(), hx18Log())

	adapter := adapterFn
	if adapter == nil || seen == nil {
		t.Fatal("no adapter recorded / handed to the seam")
	}
	req := executor.HostExecRequest{
		Argv:    []string{"ffmpeg", "-i", "in"},
		Cwd:     "/srv/mod/ws",
		Env:     map[string]string{"A": "B"},
		RunAs:   "worker",
		Timeout: 30 * time.Second,
	}
	out, err := adapter(req)
	if err == nil {
		t.Fatal("wrapper error must be returned, not swallowed")
	}
	if out.Argv != nil || out.Cwd != "" || out.Env != nil || out.RunAs != "" || out.Timeout != 0 {
		t.Errorf("wrapper error must yield an EMPTY request: got argv=%v cwd=%q runAs=%q", out.Argv, out.Cwd, out.RunAs)
	}
}

func TestHostExecSandboxAdapter_replacesOnlyArgv(t *testing.T) {
	t.Cleanup(hx18Restore)
	hx18Native()
	hx18Named(9)

	// A stub wrapper that reports what it was handed, so the test can check the
	// adapter passed the request's cwd/env/timeout through as SandboxOptions.
	var sawWorkspace string
	var sawEnvKeys []string
	var sawTimeoutS int
	hostExecSandboxWrapFn = func(opts hostexec.SandboxOptions, argv []string) ([]string, error) {
		sawWorkspace = opts.Workspace
		sawTimeoutS = opts.TimeoutS
		for k := range opts.Env {
			sawEnvKeys = append(sawEnvKeys, k)
		}
		wrapped := append([]string{"systemd-run", "--wait"}, argv...)
		return wrapped, nil
	}

	adapterFn = nil
	var seen func(executor.HostExecRequest) (executor.HostExecRequest, error)
	adapterSet = func(f func(executor.HostExecRequest) (executor.HostExecRequest, error)) { seen = f }
	wireHostExec(hx18Cfg(), hx18Log())

	adapter := adapterFn
	if adapter == nil || seen == nil {
		t.Fatal("no adapter recorded / handed to the seam")
	}
	req := executor.HostExecRequest{
		Argv:    []string{"ffmpeg", "-i", "in"},
		Cwd:     "/srv/mod/ws",
		Env:     map[string]string{"A": "B"},
		RunAs:   "worker",
		Timeout: 30 * time.Second,
	}
	out, err := adapter(req)
	if err != nil {
		t.Fatalf("stub wrapper cannot fail: %v", err)
	}
	if out.Argv == nil || len(out.Argv) != 5 || out.Argv[2] != "ffmpeg" {
		t.Errorf("argv not wrapped: %v", out.Argv)
	}
	if out.Cwd != req.Cwd || out.RunAs != req.RunAs || out.Timeout != req.Timeout {
		t.Errorf("adapter must preserve Cwd/RunAs/Timeout: got %q %q %v", out.Cwd, out.RunAs, out.Timeout)
	}
	if out.Env != nil {
		// req.Env is already baked into the wrapped argv as systemd-run
		// --setenv=K=V flags. The outer process the runner execs is
		// "systemd-run" itself (argv[0], on the host, not sandboxed): if the
		// adapter left Env set, hostexec.Run's cmd.Env = buildEnv(req.Env)
		// would hand the tenant job's env vars to that host-side process's
		// own environment too (double exposure). The adapter must clear it.
		t.Errorf("adapter must clear Env after baking it into --setenv: got %v", out.Env)
	}
	if sawWorkspace != req.Cwd {
		t.Errorf("opts.Workspace must be the job cwd: got %q", sawWorkspace)
	}
	if sawTimeoutS != 30 {
		t.Errorf("opts.TimeoutS must be the job timeout in seconds: got %d", sawTimeoutS)
	}
	if len(sawEnvKeys) != 1 || sawEnvKeys[0] != "A" {
		t.Errorf("opts.Env must be the job env: got %v", sawEnvKeys)
	}
}

func TestHostExecInaccessible_neverRootOrEmpty(t *testing.T) {
	t.Cleanup(hx18Restore)

	// A key file at the filesystem root would make its dir "/" - hiding the
	// whole filesystem must never be handed to the sandbox unit.
	hostExecKeyFile = "/host-exec.key"
	if got := hostExecInaccessible(hx18Cfg()); len(got) != 0 {
		t.Errorf("key at root must yield no Inaccessible entry (would hide /): %v", got)
	}

	// A relative / empty key path yields no usable directory to hide.
	hostExecKeyFile = "host-exec.key"
	if got := hostExecInaccessible(hx18Cfg()); len(got) != 0 {
		t.Errorf("relative key path must yield no Inaccessible entry: %v", got)
	}

	// The normal install: exactly the key file's directory, deduped.
	hostExecKeyFile = "/etc/mod-worker/host-exec.key"
	got := hostExecInaccessible(hx18Cfg())
	if len(got) != 1 || got[0] != "/etc/mod-worker" {
		t.Errorf("expected exactly [/etc/mod-worker], got %v", got)
	}
	for _, p := range got {
		if p == "" || p == "/" {
			t.Errorf("Inaccessible must never be empty or /: %q", p)
		}
	}
}
