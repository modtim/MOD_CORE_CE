// WslcBackend (GP.17d) implements Backend by shelling out to wslc.exe — the
// WSL container CLI that ships with Windows on WSL >= 2.9.3 — instead of the
// Docker daemon. Design: the internal design notes
// gp-17-wslc-design-DRAFT.md.
//
// Cross-platform on purpose: it reuses the native backend's process-group /
// job-object helpers (configureNativeProcess / attachNativeProcess /
// killProcessGroup / releaseNativeHandle), which are defined for both
// windows (Job Object) and !windows (process group). It therefore compiles
// on every GOOS — including linux, where the unit tests drive it against a
// fake `wslc` script. The real binary only ever runs on a Windows-native
// worker (design doc §4, option b: no cgo, no opaque WslcSessionSettings
// structs).
//
// This file is the CLI-shape layer only. The NOAH session-lifecycle glue
// (create-on-first-claim, terminate-on-yield, GP.17e) is a separate task;
// this backend is deliberately NOT registered in any backend factory.
// Registry auth for pulls (GP.17f) lives here too: before a run, the
// backend `wslc login`s the task image's registry (creds picked with the
// same PickRegistryAuth the docker path uses) so the run's implicit pull
// authenticates.
//
// Pause: no pause verb exists in the wslc API surface (design doc §2), so
// there is no soft-pause-then-resume path for wslc-backed tasks. Preemption
// is Cancel — a hard evict (SIGTERM to the wslc process group, SIGKILL
// after the grace window, then container teardown). A NOAH yield must
// therefore route wslc tasks through the pinned/hard-evict branch only.
//
// Egress (GP.17g): wslc exposes only None / Bridged container networking —
// no per-container firewall, no pause. The mapping is fail-closed:
// default-deny (and the zero value, treated as the platform default of
// deny — the docker backend's SelectNetworkMode also falls back to its
// internal-only, zero-outbound user network) maps to `--network none`;
// user-egress and allowlist are REFUSED at Submit because Bridged would be
// unrestricted. The only escape hatch is ForceEgressProxy — proxy-forcing,
// not enforcement (design doc §3.1): with it set, user-egress/allowlist run
// Bridged with HTTP(S)_PROXY/NO_PROXY env injected, advisory only unless the
// task image honors proxy env vars.
package executor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	// wslcBackendName is the Handle.Backend tag for wslc-backed tasks.
	wslcBackendName = "wslc"

	// wslcDefaultBinary is where the real wslc.exe lives on a WSL 2.9.3+
	// Windows install (System32 is on the worker's PATH, but the absolute
	// path avoids a PATH-resolution surprise on tenant desktops).
	// TODO(GP.17-live): the path is stated in no public doc — verify it on a
	// live box (the GP.17c probe should resolve the real path, not assume it).
	wslcDefaultBinary = `C:\Windows\System32\wslc.exe`

	// wslcKillGrace is how long Cancel waits after SIGTERM to the wslc
	// process group before SIGKILL — the container's own SIGTERM→grace→
	// Stop sequence (design doc §2) happens inside that window.
	wslcKillGrace = 2 * time.Second

	// wslcRunPollInterval / wslcRunWatchTimeout bound the submit watch's
	// container-ID probing. `wslc run` supports `-d/--detach`, but the
	// backend runs it ATTACHED (blocking) on purpose: the container runs
	// with --rm, so once the run process exits the container is deleted
	// and `wslc container logs`/inspect would be gone — the attached
	// process's streamed stdout (teed to <id>/task.log) and exit code are
	// the durable record. The watch goroutine only probes
	// `wslc container list` to learn the container ID for Poll's inspect
	// fallback until wslcRunWatchTimeout, then keeps waiting (without
	// probing) until the run process exits.
	wslcRunPollInterval = 250 * time.Millisecond
	wslcRunWatchTimeout = 5 * time.Second
)

// wslcEgressDecision is what a spec's egress policy means for wslc.
type wslcEgressDecision struct {
	// None means run with `--network none` (no outbound at all).
	None bool
	// ProxyEnv are extra env vars to inject when running Bridged with a
	// forced egress proxy (advisory only — see wslcEgressFor).
	ProxyEnv map[string]string
}

// wslcEgressFor decides how a spec's egress policy maps onto wslc's
// None/Bridged-only networking (GP.17g). Fail-closed: only the no-egress
// postures ever produce a decision; user-egress/allowlist are refused
// (returned error, no network mode) unless forceProxy is set.
//
// The zero/empty policy is treated as the platform default — default-deny:
// the docker backend's SelectNetworkMode falls through to its internal-only,
// zero-outbound user network for any task that declares no internet need
// and no allowlist, so deny is the correct mirror here (and the strictest
// option wslc offers).
func wslcEgressFor(spec *TaskSpec, forceProxy string) (wslcEgressDecision, error) {
	switch spec.Egress {
	case "", EgressDefaultDeny:
		return wslcEgressDecision{None: true}, nil
	case EgressUserEgress, EgressAllowlist:
		if forceProxy == "" {
			return wslcEgressDecision{}, fmt.Errorf(
				"executor: WslcBackend.Submit: egress policy %q cannot be enforced on the wslc backend (wslc has no per-container firewall and only None/Bridged networking); task needs a docker-capable worker",
				spec.Egress)
		}
		// Proxy-forcing, NOT enforcement (design doc §3.1): a Bridged
		// container likely gets a routable NAT'd address and can bypass
		// the proxy (raw sockets, non-HTTP protocols, direct egress).
		// This is only usable when the operator has explicitly wired a
		// host-side forward proxy for the wslc backend and accepts the
		// advisory risk; it is never the default.
		return wslcEgressDecision{
			None: false,
			ProxyEnv: map[string]string{
				"HTTP_PROXY":  forceProxy,
				"HTTPS_PROXY": forceProxy,
				"NO_PROXY":    "localhost,127.0.0.1",
			},
		}, nil
	default:
		// Unknown policy string: fail closed to deny rather than guess
		// (mirrors the zero-value treatment above).
		return wslcEgressDecision{None: true}, nil
	}
}

var _ Backend = (*WslcBackend)(nil)

// WslcBackend implements Backend on top of the wslc CLI.
type WslcBackend struct {
	// BinaryPath is the wslc executable to shell out to. Empty =
	// wslcDefaultBinary. Injectable so tests can point at a fake.
	BinaryPath string
	// ScratchRoot holds per-task dirs (<id>/task.log for run output,
	// <id>/container.json for the polled container ID).
	ScratchRoot string
	// ForceEgressProxy is an optional forced egress-proxy URL (e.g.
	// "http://127.0.0.1:8888"). When set, user-egress/allowlist tasks
	// run Bridged with HTTP_PROXY/HTTPS_PROXY/NO_PROXY injected instead
	// of being refused at Submit. This is proxy-forcing, not enforcement
	// (design doc §3.1) — Bridged is unrestricted NAT and the proxy is
	// advisory unless the task image honors proxy env vars. Empty (the
	// default) = strict refuse. Set by the caller at construction; there
	// is deliberately NO env var for this.
	ForceEgressProxy string

	mu   sync.Mutex
	runs map[string]*wslcRun

	// loginMu guards loginCache (GP.17f). Kept separate from mu: Submit
	// calls ensureRegistryLogin before touching runs, and ClearRegistryLogins
	// runs from the session-stop path while a task may be mid-Submit.
	loginMu    sync.Mutex
	loginCache map[string]struct{} // "host\x00user" keys already logged in
}

// wslcRun tracks one submitted task.
type wslcRun struct {
	mu        sync.Mutex
	status    TaskStatus
	handle    *nativeHandle // the wslc process group / job object
	cancelled bool
}

// NewWslcBackend creates a WslcBackend rooted at scratchRoot. binaryPath
// empty = wslcDefaultBinary.
func NewWslcBackend(binaryPath, scratchRoot string) *WslcBackend {
	if binaryPath == "" {
		binaryPath = wslcDefaultBinary
	}
	return &WslcBackend{
		BinaryPath:  binaryPath,
		ScratchRoot: scratchRoot,
		runs:        make(map[string]*wslcRun),
		loginCache:  make(map[string]struct{}),
	}
}

// runWslc shells out to the wslc binary and returns its combined
// stdout+stderr. Every invocation is appended to the backend's debug log
// so it survives the call (wslc's own stderr is the only diagnostic it
// gives us).
func (b *WslcBackend) runWslc(ctx context.Context, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, b.BinaryPath, args...)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	cmd.Env = os.Environ()
	configureNativeProcess(cmd)

	err := cmd.Start()
	if err != nil {
		return out.String(), fmt.Errorf("starting wslc: %w", err)
	}
	h, aerr := attachNativeProcess(cmd)
	werr := cmd.Wait()
	if aerr == nil && h != nil {
		releaseNativeHandle(h)
	}
	text := out.String()
	b.appendDebug(text)
	if werr != nil {
		if aerr != nil {
			return text, fmt.Errorf("attaching wslc process: %w", aerr)
		}
		var exitErr *exec.ExitError
		if errors.As(werr, &exitErr) {
			return text, fmt.Errorf("wslc %s: exit %d: %s", strings.Join(args, " "), exitErr.ExitCode(), text)
		}
		return text, fmt.Errorf("wslc %s: %w: %s", strings.Join(args, " "), werr, text)
	}
	return text, nil
}

// appendDebug records one wslc invocation in the backend's own log file,
// creating it lazily. Best-effort: a log that cannot be opened is dropped
// rather than failing the call.
func (b *WslcBackend) appendDebug(text string) {
	if text == "" {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	p := b.debugLogPath()
	f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	if err != nil {
		slog.Default().Debug("wslc debug log open failed", "error", err)
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "$ %s\n", b.BinaryPath)
	io.WriteString(f, text)
	if !strings.HasSuffix(text, "\n") {
		io.WriteString(f, "\n")
	}
}

func (b *WslcBackend) debugLogPath() string {
	return filepath.Join(b.ScratchRoot, "wslc-debug.log")
}

// runWslcStdin is runWslc plus a stdin pipe. The password for registry
// login is written to stdin and closed before Wait, so it never appears on
// the process argv (or in `ps`/debug-log argv lines).
func (b *WslcBackend) runWslcStdin(ctx context.Context, stdin io.Reader, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, b.BinaryPath, args...)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	cmd.Env = os.Environ()
	configureNativeProcess(cmd)

	// StdinPipe must be set up BEFORE Start; the writer is connected to the
	// child's stdin only once the process is running.
	var stdinW io.WriteCloser
	var stdinDone chan error
	if stdin != nil {
		var err error
		stdinW, err = cmd.StdinPipe()
		if err != nil {
			return "", fmt.Errorf("wslc stdin pipe: %w", err)
		}
		stdinDone = make(chan error, 1)
	}

	if err := cmd.Start(); err != nil {
		if stdin != nil {
			_ = stdinW.Close()
		}
		return out.String(), fmt.Errorf("starting wslc: %w", err)
	}
	if stdin != nil {
		go func() {
			_, cerr := io.Copy(stdinW, stdin)
			cerr = stdinW.Close() // close the write end so wslc sees EOF
			stdinDone <- cerr
		}()
	}
	h, aerr := attachNativeProcess(cmd)
	werr := cmd.Wait()
	if stdin != nil {
		<-stdinDone
	}
	if aerr == nil && h != nil {
		releaseNativeHandle(h)
	}
	text := out.String()
	b.appendDebug(text)
	if werr != nil {
		if aerr != nil {
			return text, fmt.Errorf("attaching wslc process: %w", aerr)
		}
		var exitErr *exec.ExitError
		if errors.As(werr, &exitErr) {
			return text, fmt.Errorf("wslc %s: exit %d: %s", strings.Join(args, " "), exitErr.ExitCode(), text)
		}
		return text, fmt.Errorf("wslc %s: %w: %s", strings.Join(args, " "), werr, text)
	}
	return text, nil
}

// ensureRegistryLogin (GP.17f) makes sure the wslc session is logged in to
// the registry the task image pulls from, so the run's implicit
// `wslc image pull` authenticates. Creds are picked with the SAME
// PickRegistryAuth the docker path uses (image host vs the task's
// platform/tenant Harbor creds); an empty pick = public/local image, no
// login. Login is cached per (session, registry host, username) — a WslcBackend
// instance IS the session's lifetime (the NOAH session manager's StopSession
// is the place that calls ClearRegistryLogins), so repeat tasks on the same
// registry skip the re-login.
//
// Security: the password goes to the login process's STDIN only (via
// --password-stdin) and is never placed on argv, logged, or echoed into
// error messages. A login failure fails the task with an error naming the
// registry host (never the credentials).
func (b *WslcBackend) ensureRegistryLogin(ctx context.Context, spec *TaskSpec) error {
	creds := PickRegistryAuth(spec.Image, spec.RegistryAuth.Platform, spec.RegistryAuth.Tenant)
	if creds.Registry == "" || creds.Username == "" {
		return nil // public or local-registry image: nothing to authenticate
	}
	host := normalizeHost(creds.Registry)
	if host == "" {
		return nil
	}
	key := host + "\x00" + creds.Username

	b.loginMu.Lock()
	if _, ok := b.loginCache[key]; ok {
		b.loginMu.Unlock()
		return nil // already logged in for this session
	}
	b.loginMu.Unlock()

	// TODO(GP.17-live): the login subcommand name is not in the confirmed
	// wslc subcommand list (design doc §2 table); `login` is the docker-shaped
	// guess. The flag set (--username/--password-stdin + a server positional)
	// IS confirmed to exist in the CLI's ArgumentDefinitions.h. Verify the
	// verb name + exact flag spellings on a live box.
	_, err := b.runWslcStdin(ctx, strings.NewReader(creds.Password), "login", host, "--username", creds.Username, "--password-stdin")
	if err != nil {
		return fmt.Errorf("wslc registry login failed for host %s (password is never logged): %w", host, err)
	}
	b.loginMu.Lock()
	b.loginCache[key] = struct{}{}
	b.loginMu.Unlock()
	return nil
}

// ClearRegistryLogins forgets every cached registry login. The session-stop
// path calls it when the wslc session terminates, because a new session
// starts with no stored credentials — and it is the safe direction even if
// the old session somehow survived (a re-login against it is harmless).
func (b *WslcBackend) ClearRegistryLogins() {
	b.loginMu.Lock()
	b.loginCache = make(map[string]struct{})
	b.loginMu.Unlock()
}

// wslcBuildRunArgs maps a TaskSpec to `wslc run` arguments. Isolated here
// so the CLI shape is testable and re-verifiable against the real
// wslc.exe --help output in one place. Confirmed flag surface (GP.17b,
// microsoft/WSL src/windows/wslc): run, --rm, --name, -e, --network,
// -d/--detach, --cpus, --memory, --gpus, --shm-size, --volume, --publish,
// --user, --workdir, --hostname, --stop-signal, --stop-timeout, healthcheck
// and DNS sets, --cidfile, --ulimit. There is NO --log-driver/--log-opt —
// capture comes from the attached run's stdout (Submit tees it to
// <id>/task.log) or `wslc container logs <name>`.
func wslcBuildRunArgs(spec *TaskSpec, id string, eg wslcEgressDecision) []string {
	args := []string{"run", "--name", "mod-" + id, "--rm"}
	if eg.None {
		// wslc's networking modes are None/Bridged only; no-egress
		// (default-deny, zero value, unknown) is a clean 1:1 with None.
		// The --network flag is confirmed in the CLI argument table; which
		// literal values it accepts is not (a network name or name=/alias=
		// options are shown, built-in keywords are not).
		// TODO(GP.17-live): confirm `--network none` on a live box.
		args = append(args, "--network", "none")
	} else {
		// Bridged: only reachable through the forced-proxy path (GP.17g).
		// TODO(GP.17-live): confirm `--network bridged` on a live box.
		args = append(args, "--network", "bridged")
	}
	for k, v := range spec.Env {
		args = append(args, "-e", k+"="+v)
	}
	for k, v := range eg.ProxyEnv {
		args = append(args, "-e", k+"="+v)
	}
	if spec.GPU.Count > 0 {
		// --gpus is a confirmed presence-only flag in the CLI (ContainerTasks.cpp:
		// if the arg is present, Gpu=true; any value is ignored). Per-container
		// GPU is a real flag, not just the session-wide ENABLE_GPU bit (GP.17b).
		args = append(args, "--gpus")
	}
	if spec.Resources.CPUCores > 0 {
		// --cpus/--memory are confirmed per-run flags in the CLI argument
		// table (GP.17b).
		args = append(args, "--cpus", formatCPUs(spec.Resources.CPUCores))
	}
	if spec.Resources.MemoryMB > 0 {
		// --memory is confirmed in the argument table, but the CLI parses it
		// as a raw int64 — the docker-style "M" suffix is unverified.
		// TODO(GP.17-live): confirm the accepted value form (4096M vs bytes)
		// on a live box before relying on the suffix.
		args = append(args, "--memory", fmt.Sprintf("%dM", spec.Resources.MemoryMB))
	}
	args = append(args, spec.Image)
	args = append(args, spec.Cmd...)
	return args
}

func formatCPUs(c float64) string {
	if c == float64(int64(c)) {
		return fmt.Sprintf("%d", int64(c))
	}
	return fmt.Sprintf("%.1f", c)
}

// syncBuffer is a bytes.Buffer wrapped in a mutex, usable as a Cmd.Stdout /
// Cmd.Stderr sink. os/exec copies the child's output into the sink from its
// own goroutines, so a plain bytes.Buffer is only readable after the
// matching cmd.Wait() has returned; Submit's watch goroutine reads the run's
// output while the process may still be alive (and the deferred appendDebug
// reads it at goroutine entry), which is a data race without this lock.
// Write and String both take the lock, so every read is race-free.
type syncBuffer struct {
	l sync.Mutex
	// denormalized name avoids shadowing the `bytes` package inside methods.
	data bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (n int, err error) {
	s.l.Lock()
	defer s.l.Unlock()
	return s.data.Write(p)
}

func (s *syncBuffer) String() string {
	s.l.Lock()
	defer s.l.Unlock()
	return s.data.String()
}

// Bytes returns a copy (the caller may still be writing into the buffer).
func (s *syncBuffer) Bytes() []byte {
	s.l.Lock()
	defer s.l.Unlock()
	return []byte(s.data.String())
}

// Submit creates and starts the run via `wslc run` (blocking form), then
// returns a Handle. The watch goroutine owns status from that point.
func (b *WslcBackend) Submit(ctx context.Context, spec *TaskSpec) (*Handle, error) {
	if spec == nil {
		return nil, fmt.Errorf("executor: WslcBackend.Submit: nil TaskSpec")
	}
	id, ok := spec.Env["MOD_TASK_ID"]
	if !ok {
		return nil, fmt.Errorf("executor: WslcBackend.Submit: MOD_TASK_ID not set in spec.Env")
	}
	eg, err := wslcEgressFor(spec, b.ForceEgressProxy)
	if err != nil {
		return nil, err
	}
	if spec.Image == "" {
		return nil, fmt.Errorf("executor: WslcBackend.Submit: empty Image (wslc always runs in a container)")
	}
	if len(spec.Cmd) == 0 {
		return nil, fmt.Errorf("executor: WslcBackend.Submit: empty Cmd")
	}

	// GP.17f: authenticate the run's implicit image pull BEFORE starting the
	// run — a login failure fails the task here (no orphaned run entry, no
	// container started against unauthenticated pulls).
	if err := b.ensureRegistryLogin(ctx, spec); err != nil {
		return nil, fmt.Errorf("executor: WslcBackend.Submit: %w", err)
	}

	taskDir := filepath.Join(b.ScratchRoot, id)
	for _, d := range []string{taskDir} {
		if err := os.MkdirAll(d, 0755); err != nil {
			return nil, fmt.Errorf("executor: WslcBackend.Submit: creating scratch dir: %w", err)
		}
	}

	b.mu.Lock()
	if _, exists := b.runs[id]; exists {
		b.mu.Unlock()
		return nil, fmt.Errorf("executor: WslcBackend.Submit: task %q already submitted", id)
	}
	run := &wslcRun{status: TaskStatus{Phase: TaskRunning}}
	b.runs[id] = run
	b.mu.Unlock()

	// `wslc run` supports -d/--detach, but we run it attached (blocking) on
	// purpose: with --rm the container is deleted when the run exits, so
	// `wslc container logs` would be gone by the time the worker asks for
	// the log tail. The attached process's stdout/stderr is the container
	// output and its exit code is the terminal signal — the watch tees the
	// output to <id>/task.log (the Logs() source) and settles status when
	// it exits. See wslcRunWatchTimeout.
	logPath := filepath.Join(taskDir, "task.log")
	runArgs := wslcBuildRunArgs(spec, id, eg)

	cmd := exec.Command(b.BinaryPath, runArgs...)
	var out syncBuffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	cmd.Dir = taskDir
	cmd.Env = os.Environ()
	configureNativeProcess(cmd)

	run.mu.Lock()
	if err := cmd.Start(); err != nil {
		run.mu.Unlock()
		b.forgetRun(id)
		return nil, fmt.Errorf("executor: WslcBackend.Submit: starting wslc run: %w", err)
	}
	nh, aerr := attachNativeProcess(cmd)
	if aerr != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		run.mu.Unlock()
		b.forgetRun(id)
		return nil, fmt.Errorf("executor: WslcBackend.Submit: %w", aerr)
	}
	run.handle = nh
	run.mu.Unlock()

	// Watch: learn the container ID (if it ever becomes visible, persisted
	// to container.json for Poll) and settle status when the run process
	// exits. The watch is owned by this goroutine — it stops probing once
	// the process is done, so a fast run settles in one poll interval.
	inspectID := filepath.Join(taskDir, "container.json")
	watchCtx, cancelWatch := context.WithCancel(ctx)
	go func() {
		defer cancelWatch()
		defer releaseNativeHandle(nh)
		defer b.appendDebug(out.String())

		done := make(chan error, 1)
		go func() { done <- cmd.Wait() }()

		settle := func(werr error) {
			run.mu.Lock()
			defer run.mu.Unlock()
			if run.status.Phase != TaskRunning {
				return // Cancel already recorded a terminal phase
			}
			switch {
			case run.cancelled:
				run.status = TaskStatus{Phase: TaskFailed, ExitCode: 128, Err: "cancelled"}
			case werr == nil:
				run.status = TaskStatus{Phase: TaskSucceeded, ExitCode: 0}
			default:
				code := 1
				var exitErr *exec.ExitError
				if errors.As(werr, &exitErr) {
					code = exitErr.ExitCode()
				}
				run.status = TaskStatus{Phase: TaskFailed, ExitCode: code, Err: fmt.Sprintf("wslc run exited: %s", strings.TrimSpace(out.String()))}
			}
		}

		learned := ""
		deadline := time.Now().Add(wslcRunWatchTimeout)
		for {
			if learned == "" && time.Now().Before(deadline) {
				if cid, err := b.findContainerID(watchCtx, "mod-"+id); err == nil {
					learned = cid
					if werr := os.WriteFile(inspectID, []byte(cid), 0644); werr != nil {
						slog.Default().Warn("wslc container id persist failed", "task", id, "error", werr)
					}
				}
			}
			select {
			case werr := <-done:
				// Tee the container's captured stdout/stderr to the task log
				// so Logs() has a durable source after the --rm container is
				// gone. This happens BEFORE settle: a caller that sees a
				// terminal status must already find the task log file.
				if lerr := os.WriteFile(logPath, out.Bytes(), 0644); lerr != nil {
					slog.Default().Warn("wslc task log persist failed", "task", id, "error", lerr)
				}
				settle(werr)
				return
			case <-watchCtx.Done():
				return
			case <-time.After(wslcRunPollInterval):
			}
		}
	}()

	return &Handle{Backend: wslcBackendName, ID: id}, nil
}

// findContainerID resolves a wslc container name to its ID via
// `wslc container list` — the verb spelling confirmed in the CLI source
// (ContainerListCommand; whether `ls` is an alias is unconfirmed). No
// `--format` is passed: Go-template support in wslc's list command is only
// inferred from the 2.9.10 docker-alignment notes, so the output is parsed
// defensively — a line containing a field that exactly equals the
// container name is treated as "id name" and the first field taken as the
// ID, which also matches the plain table output wslc would print if
// docker-style templates are not honored.
// TODO(GP.17-live): confirm the default output shape (columns/order) of
// `wslc container list` on a live box and tighten this parser.
func (b *WslcBackend) findContainerID(ctx context.Context, name string) (string, error) {
	text, err := b.runWslc(ctx, "container", "list", "--all")
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(text, "\n") {
		fields := strings.Fields(line)
		for i, f := range fields {
			if f == name && i > 0 {
				return fields[0], nil
			}
		}
	}
	return "", fmt.Errorf("container %q not found", name)
}

// Poll reports the current state. While the run process is still alive it
// reports running; after exit it reports the settled status. As a fallback
// for the window where the container exists but the watch has not yet
// settled status (or the watch was cut off), it probes
// `wslc container inspect` for a terminal state.
func (b *WslcBackend) Poll(ctx context.Context, h *Handle) (*TaskStatus, error) {
	if h == nil {
		return nil, fmt.Errorf("executor: WslcBackend.Poll: nil Handle")
	}
	if h.Backend != wslcBackendName {
		return nil, fmt.Errorf("executor: WslcBackend.Poll: handle is not wslc (backend=%q)", h.Backend)
	}

	b.mu.Lock()
	run, ok := b.runs[h.ID]
	b.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("executor: WslcBackend.Poll: unknown task %q", h.ID)
	}

	run.mu.Lock()
	st := run.status
	run.mu.Unlock()
	if st.Phase != TaskRunning {
		return &st, nil
	}

	// Still running per the watch — probe the container in case the watch
	// has not caught up (e.g. the run process lingers past container exit).
	cid, err := b.readContainerID(h.ID)
	if err != nil {
		return &st, nil // no ID yet; keep reporting running
	}
	state, code, ierr := b.inspectContainerState(ctx, cid)
	if ierr != nil {
		return &st, nil
	}
	if state == "exited" || state == "dead" {
		phase := TaskSucceeded
		if code != 0 {
			phase = TaskFailed
		}
		run.mu.Lock()
		if run.status.Phase == TaskRunning {
			run.status = TaskStatus{Phase: phase, ExitCode: code}
		}
		st = run.status
		run.mu.Unlock()
	}
	return &st, nil
}

func (b *WslcBackend) readContainerID(taskID string) (string, error) {
	data, err := os.ReadFile(filepath.Join(b.ScratchRoot, taskID, "container.json"))
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// inspectContainerState reads the container's State via
// `wslc container inspect`. It assumes docker-shaped output
// ([{State:{Status,ExitCode}}]) — wslc's docker-parity release notes make
// this plausible, but no public source shows the actual JSON shape, so a
// parse failure here is treated as "probe inconclusive" by Poll (it keeps
// reporting running) and must be verified live.
// TODO(GP.17-live): confirm the exact `wslc container inspect` output shape
// (array vs object, field names, terminal Status literals) on a live box.
func (b *WslcBackend) inspectContainerState(ctx context.Context, cid string) (string, int, error) {
	text, err := b.runWslc(ctx, "container", "inspect", cid)
	if err != nil {
		return "", 0, err
	}
	var doc []struct {
		State struct {
			Status   string `json:"Status"`
			ExitCode int    `json:"ExitCode"`
		} `json:"State"`
	}
	if err := json.Unmarshal([]byte(text), &doc); err != nil {
		return "", 0, fmt.Errorf("parsing inspect output: %w", err)
	}
	if len(doc) == 0 {
		return "", 0, fmt.Errorf("inspect returned no objects")
	}
	return doc[0].State.Status, doc[0].State.ExitCode, nil
}

// Logs returns the container's captured output (the <id>/task.log file the
// watch tees from the attached run's stdout/stderr — wslc has no
// --log-driver/--log-opt, and with --rm the container is gone after exit so
// `wslc container logs` is not a viable source). Falls back to the
// backend's own debug log when the task never produced a task log file
// (e.g. the run was cancelled before it could settle).
func (b *WslcBackend) Logs(_ context.Context, h *Handle) (io.ReadCloser, error) {
	if h == nil {
		return nil, fmt.Errorf("executor: WslcBackend.Logs: nil Handle")
	}
	if h.Backend != wslcBackendName {
		return nil, fmt.Errorf("executor: WslcBackend.Logs: handle is not wslc (backend=%q)", h.Backend)
	}
	b.mu.Lock()
	_, ok := b.runs[h.ID]
	b.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("executor: WslcBackend.Logs: unknown task %q", h.ID)
	}

	logPath := filepath.Join(b.ScratchRoot, h.ID, "task.log")
	if f, err := os.Open(logPath); err == nil {
		return f, nil
	}
	f, err := os.Open(b.debugLogPath())
	if err != nil {
		return nil, fmt.Errorf("executor: WslcBackend.Logs: no logs available: %w", err)
	}
	return f, nil
}

// Cancel hard-evicts the task: SIGTERM the wslc process group (its SIGTERM
// propagates toward the container process), SIGKILL after wslcKillGrace,
// then best-effort stop+delete of the named container. There is no pause
// verb in wslc — this IS the eviction (see package doc).
func (b *WslcBackend) Cancel(ctx context.Context, h *Handle) error {
	if h == nil {
		return fmt.Errorf("executor: WslcBackend.Cancel: nil Handle")
	}
	if h.Backend != wslcBackendName {
		return fmt.Errorf("executor: WslcBackend.Cancel: handle is not wslc (backend=%q)", h.Backend)
	}
	b.mu.Lock()
	run, ok := b.runs[h.ID]
	b.mu.Unlock()
	if !ok {
		return fmt.Errorf("executor: WslcBackend.Cancel: unknown task %q", h.ID)
	}

	run.mu.Lock()
	if run.status.Phase != TaskRunning {
		run.mu.Unlock()
		return nil // already finished
	}
	run.cancelled = true
	handle := run.handle
	run.mu.Unlock()

	if err := killProcessGroup(handle); err != nil {
		return fmt.Errorf("executor: WslcBackend.Cancel: %w", err)
	}

	// Best-effort teardown of the container itself: the wslc process is
	// dead, but the container may persist (no verified --rm semantics).
	if _, err := b.runWslc(ctx, "container", "stop", "mod-"+h.ID); err != nil {
		slog.Default().Warn("wslc container stop failed (best-effort)", "task", h.ID, "error", err)
	}
	if _, err := b.runWslc(ctx, "container", "rm", "mod-"+h.ID); err != nil {
		slog.Default().Warn("wslc container rm failed (best-effort)", "task", h.ID, "error", err)
	}
	return nil
}

// forgetRun drops a run registration after a Submit failure.
func (b *WslcBackend) forgetRun(taskID string) {
	b.mu.Lock()
	delete(b.runs, taskID)
	b.mu.Unlock()
}
