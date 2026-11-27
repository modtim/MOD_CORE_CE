package main

// hx.7e — boot-time wiring of the executor's host-exec seams (opsx worker-host-exec).
//
// main.go calls wireHostExec(cfg, log) once the config is loaded; it hands
// executor.ConfigureHostExec the two things hostexec_task.go refuses to invent:
//
//   * HOW a host exec actually happens on THIS install — hostexec.Mode picks the
//     path (hx.7 / D1): the mod-host-exec unix-socket helper wins whenever its
//     bind-mounted socket is present (a containerized worker has no host
//     privileges and cannot exec host jobs itself), otherwise a systemd-binary
//     install execs through hostexec.Run directly. A container with no helper
//     socket gets no runner at all, so the task fails with the actionable
//     "install docs" error rather than running somewhere it cannot reach.
//   * WHO this worker is: the X-Worker-Name / X-Bootstrap-Token headers every
//     other worker->API call carries (transport.APITransport.setHeaders). They
//     are read from cfg AT CALL TIME, never captured by value at boot — GSEC
//     Q21.8 rotates cfg.BootstrapToken after Vault recovery (main.go's
//     requestGracefulRestart comment), and the heartbeat already reads
//     cfg.BootstrapToken per beat on this same *config.Config.
//
// mode "sandboxed" is NOT handled here: hostexec_task.go refuses it before the
// runner is ever called (hx.17-wire owns the systemd-run wrapper), so a sandboxed
// job can never reach either runner.
//
// internal/hostexec IS imported here (unlike the executor package, where
// hostexec/scrub.go importing back would be a cycle).

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/heartbeat"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/hostexec"
)

// hostExecKeyFile is where the helper's HMAC signing key lives (hx.4's mod-host-exec
// unit: HOST_EXEC_KEY_FILE=/etc/mod-worker/host-exec.key, root-owned 0600). The
// worker signs the request with the SAME key the helper verifies, so it needs a
// readable copy; a containerized worker gets one through the same bind-mount as
// the socket. A var (not a const) so an unusual install — and every test — can
// point elsewhere without touching the unit.
var hostExecKeyFile = "/etc/mod-worker/host-exec.key"

// hostExecReadKey loads the signing key. The helper refuses a group/world-readable
// key file (cmd/host-exec-helper/main.go's readKey); the worker applies the same
// check to its own copy so a chmod slip fails the wiring rather than signing with
// a key some other process can read. A var so tests can hand a key without a
// root-owned file on disk.
var hostExecReadKey = func(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if info.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("host-exec: key file %s must not be group/world readable (mode %o)",
			path, info.Mode().Perm())
	}
	return os.ReadFile(path)
}

// hostExecWorkerIDProvider reports this worker's numeric WorkerLicense id — the
// output endpoint's path parameter (/v1/workers/{worker_id}/exec/{job_id}/output,
// routers/worker_host_exec_output.py). 0 means "the API never told this worker its
// id": the wiring then leaves the relay unwired and the task fails closed.
//
// hx.18 (item 126b closed the FINDING): the id now comes from the heartbeat
// reply. heartbeat.go decodes the API's new worker_id field into its atomic
// workerID and adopts it once (adoptWorkerID never lets a later 0 clobber it),
// so heartbeat.WorkerID() is 0 until the API's FIRST heartbeat answer carries
// the id and is stable after that. wireHostExec must therefore NOT run at boot
// — at boot the id is always 0 and the wiring would fail closed — it runs
// startHostExecWiring's goroutine, which waits for the first beat that carries
// it and wires exactly once (see startHostExecWiring below).
var hostExecWorkerIDProvider = heartbeat.WorkerID

// hostExecSocketPath is the helper socket the worker dials. Defaults to the
// helper's own DefaultSocket (the bind mount in the unit).
var hostExecSocketPath = hostexec.DefaultSocket

// hostExecRelay is this worker's view of the helper, built once by wireHostExec.
// nil means "no relay" (no socket, unreadable key, or unknown worker id).
var hostExecRelay *hostexec.Relay

// hostExecStatusProvider (993a) reports this worker's local host-exec
// availability — the same hostexec.ComputeStatus wireHostExec computed at
// boot, as a var so tests can inject it and heartbeat.go can read it without
// importing cmd/worker (heartbeat.SetHostExecStatus receives the map form).
var hostExecStatusProvider func() hostexec.Status

// hostExecStatusMap converts the Status into the heartbeat wire shape
// {available, mode, code, message}. A var so tests can swap the shaping.
var hostExecStatusMap = func() map[string]interface{} {
	s := hostExecStatusProvider()
	return map[string]interface{}{
		"available": s.Available,
		"mode":      s.Mode,
		"code":      s.Code,
		"message":   s.Message,
	}
}

// hx.9b — the helper re-reads this file PER REQUEST and refuses with
// `worker_id not bound:` until it exists, so the worker must hand the id it
// adopted from the API's first answered heartbeat to disk. Direct/systemd mode
// needs no file: the worker signs nothing, the helper is not involved.
//
// No env override: the socket/key paths are never read from the environment
// anywhere in the Worker (verified: zero getenv in CORE/Worker/**/*.go —
// HOST_EXEC_KEY_FILE only appears in the systemd unit's comment), so the
// constant default mirrors how the install actually pins these paths. A var so
// an unusual install — and every test — can point elsewhere.
var hostExecWorkerIDFile = "/var/lib/mod-worker/host-exec/worker_id"

// hostExecIDWarned arms the write-failure warning exactly once: a read-only
// state volume would otherwise spam a warning per re-registration. wireHostExec
// runs from startHostExecWiring's single goroutine, so a plain var is enough.
// A var so a test can start a fresh one.
var hostExecIDWarned bool

// hostExecWriteIDFile atomically writes decimalID + "\n" to hostExecWorkerIDFile
// (0644 file, 0750 dir created if missing, tmp file in the SAME dir + rename so
// the helper never reads a half-written id). Idempotent: a file that already
// carries the id is left alone, so a re-registration that does not change the
// id costs one read. Returns the first error; the caller logs it and NEVER
// crashes — host-exec then fails closed on the helper side.
func hostExecWriteIDFile(decimalID string) error {
	if decimalID == "" {
		return nil
	}
	want := decimalID + "\n"
	if got, _ := os.ReadFile(hostExecWorkerIDFile); got != nil && string(got) == want {
		return nil
	}
	dir := filepath.Dir(hostExecWorkerIDFile)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	// MkdirAll masks the mode with the process umask; the helper's own listen()
	// chmods its socket dir to 0750, so do the same here. Best-effort: a
	// bind-mounted state dir owned by root must not fail the write itself.
	_ = os.Chmod(dir, 0o750)
	// os.CreateTemp opens O_EXCL with a random suffix: unlike a fixed
	// ".worker_id.tmp" name, nothing lets another process pre-place a symlink
	// at a predictable path and have this write follow it. Any failure below
	// removes the tmp file so no debris is left in the state dir.
	f, err := os.CreateTemp(dir, ".worker_id.*.tmp")
	if err != nil {
		return err
	}
	tmp := f.Name()
	_, writeErr := f.Write([]byte(want))
	closeErr := f.Close()
	if writeErr != nil {
		_ = os.Remove(tmp)
		return writeErr
	}
	if closeErr != nil {
		_ = os.Remove(tmp)
		return closeErr
	}
	// CreateTemp opens the file 0600; the helper's own readKey-style check
	// wants exactly 0644, so set the mode explicitly before the rename.
	if err := os.Chmod(tmp, 0o644); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, hostExecWorkerIDFile); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

// hostExecAdoptID is the call-site wrapper wireHostExec uses once the relay
// path is live. Idempotent, so a re-registration that adopts a NEW id (main.go
// restarts the worker, which re-runs the wiring) just overwrites the file.
func hostExecAdoptID(log *slog.Logger, numericID int64) {
	if numericID <= 0 {
		return
	}
	if err := hostExecWriteIDFile(strconv.FormatInt(numericID, 10)); err != nil {
		if !hostExecIDWarned {
			hostExecIDWarned = true
			log.Warn("host-exec worker_id state file not written; the helper will refuse host-exec until it exists",
				"path", hostExecWorkerIDFile, "error", err)
		}
	}
}

// hostExecDirectRunner is the native (systemd-binary) runner: hostexec.Run,
// adapted to the executor seam's frame type.
func hostExecDirectRunner(ctx context.Context, req executor.HostExecRequest, emit func(executor.HostExecFrame)) (int, error) {
	return hostexec.Run(ctx, hostexec.Request{
		Argv:    req.Argv,
		Cwd:     req.Cwd,
		Env:     req.Env,
		RunAs:   req.RunAs,
		Timeout: req.Timeout,
	}, func(f hostexec.Frame) {
		emit(executor.HostExecFrame{Stream: f.Stream, Data: f.Data})
	})
}

// hostExecRelayRunner is the containerized runner: sign + stream through the
// mod-host-exec helper over its unix socket. hostexec.Relay.Exec hands emit the
// helper's RAW OutFrames (base64 data, and the exit-code frame is consumed by
// Exec itself), so the bytes are decoded back into the seam's frame type here.
// A helper rejection arrives as an error from Exec, never as a frame.
func hostExecRelayRunner(ctx context.Context, req executor.HostExecRequest, emit func(executor.HostExecFrame)) (int, error) {
	r := hostExecRelay
	if r == nil {
		return -1, errors.New("host-exec: relay not built (no helper socket / signing key / worker_id)")
	}
	timeoutS := int(req.Timeout / time.Second)
	wire := hostexec.WireRequest{
		// The helper never reads job_id (it verifies the signature over it and
		// runs argv/cwd/env/run_as/timeout_s), so the field carries a per-call
		// sequence: two byte-identical jobs never share a replayable request.
		JobID:    strconv.FormatInt(atomic.AddInt64(&hostExecWireSeq, 1), 10),
		WorkerID: r.WorkerID,
		Argv:     req.Argv,
		Cwd:      req.Cwd,
		Env:      req.Env,
		RunAs:    req.RunAs,
		TimeoutS: timeoutS,
	}
	return r.Exec(ctx, wire, func(f hostexec.OutFrame) error {
		data, derr := hostexec.DecodeFrameData(f)
		if derr != nil {
			return derr
		}
		if len(data) == 0 {
			return nil
		}
		emit(executor.HostExecFrame{Stream: f.Stream, Data: data})
		return nil
	})
}

var hostExecWireSeq int64

// wireHostExec builds the runner + auth headers and hands them to the executor
// seams. Called once at boot by main.go, after cfg is loaded and after any
// Vault-recovery path has had a chance to run (the headers are closures over
// cfg, so even a later token rotation lands — see the header comment).
func wireHostExec(cfg *config.Config, log *slog.Logger) {
	numericID := hostExecWorkerIDProvider()
	if numericID <= 0 {
		// No numeric WorkerLicense id: the output endpoint's path parameter is
		// unknown, so output has nowhere authenticated to go. Leave the seams
		// alone; hostexec_task.go fails every host-exec task closed.
		log.Warn("host-exec not wired: this worker does not know its numeric worker_id (hx.7e)")
		return
	}

	socket := hostExecSocketPath
	mode, modeErr := hostexec.Mode(socket, hostexec.RunningInContainer())
	// 993a: the richer local status (available/mode/code/message) is reported
	// on every beat EVEN WHEN the seams below bail out — "not wired because
	// helper_missing/not_root" is exactly what the availability UX shows.
	// main.go is off-limits (988b touches it), so the heartbeat setter is
	// called here, same package, right after the status is computed.
	hostExecStatusProvider = func() hostexec.Status { return hostexec.ComputeStatus(socket, hostexec.RunningInContainer()) }
	heartbeat.SetHostExecStatus(hostExecStatusMap)
	if modeErr != nil {
		log.Warn("host-exec not wired: no execution path on this install", "error", modeErr)
		return
	}
	workerIDStr := strconv.FormatInt(numericID, 10)

	runner := hostExecDirectRunner
	viaHelper := false
	if mode == "helper" {
		key, keyErr := hostExecReadKey(hostExecKeyFile)
		if keyErr != nil {
			log.Warn("host-exec not wired: helper signing key unreadable", "error", keyErr)
			return
		}
		hostExecRelay = &hostexec.Relay{
			Socket:   socket,
			Key:      key,
			WorkerID: workerIDStr,
		}
		runner = hostExecRelayRunner
		// The relay IS a containerized worker's supported path (it signs and
		// streams through the mod-host-exec helper over its bind-mounted
		// socket), so hostexec_task.go's "no helper socket" container guard
		// must stand down for it. Native mode below leaves ViaHelper false,
		// so that guard still refuses a native runner inside a container.
		viaHelper = true
		// hx.9b: the relay is the only mode that consults the helper's
		// --worker-id-file, so the id state file is written here and only here.
		hostExecAdoptID(log, numericID)
	}

	// The identity headers every worker->API call carries
	// (transport.APITransport.setHeaders). Read from cfg AT CALL TIME: GSEC
	// Q21.8 rotates cfg.BootstrapToken after Vault recovery, and the heartbeat
	// reads the same field per beat on this same *config.Config.
	setAuth := func(req *http.Request) {
		req.Header.Set("X-Worker-Name", cfg.WorkerName)
		req.Header.Set("X-Bootstrap-Token", cfg.BootstrapToken)
	}

	executor.ConfigureHostExec(executor.HostExecWiring{
		Runner:    runner,
		SetAuth:   setAuth,
		WorkerID:  workerIDStr,
		ViaHelper: viaHelper,
	})

	// hx.17-wire: mode "sandboxed" is refused by hostexec_task.go unless the
	// sandbox-wrapper seam is wired. Build it on hostexec.SandboxArgv so a
	// sandboxed job gets the systemd-run unit (workspace writable, worker
	// credential/key paths Inaccessible) instead of the raw argv. A wrapper
	// error returns an empty request + error: hostexec_task.go then fails the
	// job closed and NEVER falls back to an unsandboxed exec.
	//
	// The seam is installed unconditionally — even for the native/relay runners
	// above, which never consult it — because hostexec_task.go only refuses
	// "sandboxed" when the seam is nil; wiring it here is what makes that mode
	// runnable at all.
	//
	// The opts are built PER CALL from the request (req.Cwd is the tenant
	// workspace, req.Env the job's env): cfg is read at call time for the same
	// reason the auth headers are (GSEC Q21.8 rotates cfg after Vault
	// recovery), and req fields are never captured at boot.
	//
	// hostExecSandboxWrapFn is a var so the test can hand a stub wrapper and
	// watch what the adapter does with its result/error.
	//
	// hx.18 FINDING: config.go has NO worker credential/state directory fields
	// (verified: grep "Dir|Path" internal/config/config.go only finds
	// KubeconfigPath + VaultSecretsPath, neither a directory to hide). The only
	// per-install secret path the worker knows is hostExecKeyFile's directory,
	// so that is the sole Inaccessible entry. Per-task S3 creds live in
	// process memory, not on disk, so there is nothing else to hide.
	//
	// hostExecSandboxOpts is a var so tests can steer the Inaccessible set
	// without touching the key file.
	//
	// hostExecSandboxAdapter is the adapter handed to the seam; built once per
	// wireHostExec call so the executor sees exactly one wrapper.
	adapter := func(req executor.HostExecRequest) (executor.HostExecRequest, error) {
		wrapped, err := hostExecSandboxWrapFn(hostExecSandboxOpts(cfg, req), req.Argv)
		if err != nil {
			return executor.HostExecRequest{}, err
		}
		out := req
		out.Argv = wrapped
		// req.Env is already baked into wrapped as systemd-run --setenv=K=V
		// flags (hostexec.SandboxArgv). The wrapped argv's argv[0] is
		// "systemd-run" itself, which hostexec.Run then execs with
		// cmd.Env = buildEnv(out.Env): leaving the job's Env on out would hand
		// the tenant's env vars to that HOST-SIDE systemd-run process's own
		// environment (readable via /proc/<pid>/environ by anything with host
		// access) in addition to the sandboxed unit that already got them via
		// --setenv. Clear it so the outer process only gets the worker's own
		// PATH/HOME/LANG.
		out.Env = nil
		return out, nil
	}
	adapterFn = adapter
	adapterSet(adapter)
	log.Info("host-exec seams wired", "path", mode, "socket", socket, "worker_id", workerIDStr)
}

// adapterSet is the seam-setter seam: the real wiring goes through
// executor.SetHostExecSandboxWrapper; a var so the test can watch the executor
// receive the adapter without reaching into executor's private field.
var adapterSet = executor.SetHostExecSandboxWrapper

// adapterFn is the adapter wireHostExec last installed (nil until then), so a
// test can call the exact function the executor received.
var adapterFn func(executor.HostExecRequest) (executor.HostExecRequest, error)

// hostExecSandboxWrapFn is the wrapper the adapter calls. A var so a test can
// hand a stub and steer both outcomes (error -> empty request, argv -> replaced).
var hostExecSandboxWrapFn = hostexec.SandboxArgv

// hostExecInaccessible lists paths a sandboxed host exec must NOT be able to
// read: the directory holding the host-exec signing key. Deduped, absolute,
// Clean()ed, never "/" and never empty — hostexec.sandboxPath refuses anything
// else, so a bad entry fails the unit closed rather than hiding the root.
// cfg is threaded through for the credential/state dirs a future config gains
// (config.go carries none today, see the FINDING in wireHostExec above).
func hostExecInaccessible(cfg *config.Config) []string {
	_ = cfg
	out := []string{}
	seen := map[string]bool{}
	candidates := []string{filepath.Dir(hostExecKeyFile)}
	for _, p := range candidates {
		if p == "" || p == "/" || p[0] != '/' || seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	return out
}

// hostExecSandboxOpts builds one request's sandbox unit options. Workspace is
// the job's cwd (hostexec.sandboxWorkspace then refuses empty / "/" / non-clean,
// so a task that did not name a workspace fails closed rather than getting a
// whole-filesystem-writable unit). TimeoutS is the job's timeout in seconds
// (<=0 = no cap). A var so tests can steer the Inaccessible set.
var hostExecSandboxOpts = func(cfg *config.Config, req executor.HostExecRequest) hostexec.SandboxOptions {
	// cfg is read at call time (see the header comment); today the only cfg-
	// derived path is the key-file directory, so cfg only feeds Inaccessible.
	_ = cfg
	return hostexec.SandboxOptions{
		Workspace:    req.Cwd,
		Inaccessible: hostExecInaccessible(cfg),
		TimeoutS:     int(req.Timeout / time.Second),
		Env:          req.Env,
	}
}

// hostExecWireWaitS is how long startHostExecWiring waits between checks for the
// worker id. 2s is plenty (the heartbeat beats every ~10s); a var so the test can
// shrink it and finish in milliseconds.
var hostExecWireWaitS = 2 * time.Second

// startHostExecWiring is main.go's ONE startup call for the host-exec seams. It
// spawns a goroutine that waits until the API's first answered heartbeat carries
// this worker's numeric id (hostExecWorkerIDProvider() > 0) and then runs
// wireHostExec exactly once. It cannot wire at boot: the id only exists after
// that first reply, and wireHostExec fails closed on id == 0.
//
// The goroutine exits when ctx is done (shutdown / restart) so a worker that
// never learns its id leaks no thread, and it never wires twice.
//
// The returned channel is closed when that goroutine has exited. main.go ignores
// it; the tests wait on it so the goroutine's reads of the package-level seams
// (hostExecWorkerIDProvider, wireHostExecFn) happen-before the test cleanup
// restores them (otherwise `go test -race` reports a data race).
func startHostExecWiring(ctx context.Context, cfg *config.Config, log *slog.Logger) <-chan struct{} {
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(hostExecWireWaitS)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if hostExecWorkerIDProvider() > 0 {
					wireHostExecFn(cfg, log)
					return
				}
			}
		}
	}()
	return done
}

// wireHostExecFn is the wiring entry point startHostExecWiring calls. A var so
// the test can count how many times the wait loop hands over (exactly once).
var wireHostExecFn = wireHostExec
