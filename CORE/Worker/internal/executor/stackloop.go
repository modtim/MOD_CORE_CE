package executor

// Worker-controlled stack start (11-20, owner decision "lets do the loop").
//
// Docker must not be the thing that restarts user stacks: after a host reboot
// it starts them before the worker is up, so a stack whose secret files lived
// on tmpfs (/run/mod-secrets) fails to start, and several GPU stacks all grab
// their cards at once. Instead:
//
//  1. Every container of a MOD-managed stack project (modstack<N>g<G>; never a
//     core platform container) gets restart policy `no`, set by THIS worker
//     (docker update, no recreate): once at worker start (DisableStackAutostart)
//     and after every deploy/migrate/start/boot_restore (runStackOpSerialized).
//     The API render keeps the Docker policy, so a worker WITHOUT the loop
//     keeps Docker restarts (review H6, 11-20); the API stamps the policy the
//     author meant as the label `com.example.restart`. A loop worker
//     advertises `stack_loop: true` in its heartbeat (heartbeat.SetStackLoop,
//     main.go); the API trusts an empty `hosted_stacks` only from such a
//     worker (review2 L6). Unlabelled (pre-loop)
//     containers have their old policy recorded BEFORE the update, in a file
//     on the named volume mod_worker_stacks (/var/lib/mod-worker/stacks), so a
//     worker recreate keeps it (review H5).
//  2. The worker reports, per project, how many containers SHOULD be running
//     but are not (`down`, see stackDownCount). The API (the authority on what
//     must run) answers with one `boot_restore` task at a time for this worker:
//     write the secret/variable files, then start ONLY the down services, by
//     name, even when the whole project is down (review2 M1: a bare
//     `compose start` would also run one-shot and never-started containers,
//     which Docker would not). Never down,
//     never -v, never --force-recreate: volumes and writable layers are kept.
//     See runStackOp case "boot_restore".
//
// Gated by MOD_STACK_WORKER_START (CE overlay sets 1). Unset = the old Docker
// restart behaviour, so a paid install is unchanged unless it opts in.

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// restartIntentLabel carries the restart policy the stack author meant. A
// service with no `restart:` key is stamped `default` by the API (review2 M4):
// a crash (non-zero exit) or a stop by the host shutdown is restarted, a clean
// exit 0 after this boot (a finished init/migration job) stays down and the
// stack stays `running`. A container with neither the label nor a recorded
// pre-loop policy is treated as `no` (Docker's own default).
const restartIntentDefault = "default"
const restartIntentLabel = "com.example.restart"

const stackServiceLabel = "com.docker.compose.service"

// managedStackProjectRe is the exact project shape MOD renders (review L3): a
// user's own compose project that merely starts with "modstack" is never
// switched to restart `no` or restored.
var managedStackProjectRe = regexp.MustCompile(`^modstack\d+g\d+$`)

// StackWorkerStartEnabled reports whether the worker (not Docker) restarts
// stacks. Default OFF in code; the CE overlay compose sets it to 1.
func StackWorkerStartEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("MOD_STACK_WORKER_START"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// stackPolicyDocker is the slice of the docker client the loop needs;
// *client.Client satisfies it and tests pass a fake.
type stackPolicyDocker interface {
	ContainerList(ctx context.Context, options container.ListOptions) ([]types.Container, error)
	ContainerInspect(ctx context.Context, containerID string) (types.ContainerJSON, error)
	ContainerUpdate(ctx context.Context, containerID string, updateConfig container.UpdateConfig) (container.ContainerUpdateOKBody, error)
}

var _ stackPolicyDocker = (*client.Client)(nil)

// restartIntentPath persists the pre-update policy of unlabelled (pre-loop)
// containers so a reboot after the switch still knows they meant to run. It
// sits on the mod_worker_stacks named volume in the CE overlay.
var restartIntentPath = func() string {
	return filepath.Join(filepath.Dir(stackRootDir("x")), ".restart-intent.json")
}

var restartIntentMu sync.Mutex

func loadRestartIntent() map[string]string {
	m := map[string]string{}
	if b, err := os.ReadFile(restartIntentPath()); err == nil {
		_ = json.Unmarshal(b, &m)
	}
	return m
}

func saveRestartIntent(m map[string]string) error {
	b, err := json.Marshal(m)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(restartIntentPath()), 0o700); err != nil {
		return err
	}
	tmp := restartIntentPath() + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, restartIntentPath())
}

func isManagedStackContainer(c types.Container) bool {
	return managedStackProjectRe.MatchString(strings.TrimSpace(c.Labels[composeProjectLabel]))
}

// DisableStackAutostart sets restart policy `no` on every container of every
// managed stack project (docker update, no recreate). Returns how many were
// changed. Core platform containers are never touched (project shape).
func (e *Executor) DisableStackAutostart(ctx context.Context) (int, error) {
	if e.docker == nil {
		return 0, nil
	}
	return disableStackAutostart(ctx, e.docker, "")
}

// disableStackAutostart: project "" = every managed project. The old policy of
// an unlabelled container is written to the intent file FIRST; if that write
// fails the container keeps its Docker policy (never lose the intent).
func disableStackAutostart(ctx context.Context, d stackPolicyDocker, project string) (int, error) {
	list, err := d.ContainerList(ctx, stackListOptions(false))
	if err != nil {
		return 0, err
	}
	restartIntentMu.Lock()
	defer restartIntentMu.Unlock()
	intent := loadRestartIntent()
	type pending struct {
		id       string
		labelled bool
	}
	var todo []pending
	dirty := false
	present := map[string]bool{}
	for _, c := range list {
		if !isManagedStackContainer(c) {
			continue
		}
		present[c.ID] = true
		if project != "" && strings.TrimSpace(c.Labels[composeProjectLabel]) != project {
			continue
		}
		info, err := d.ContainerInspect(ctx, c.ID)
		if err != nil || info.ContainerJSONBase == nil || info.HostConfig == nil {
			continue
		}
		cur := string(info.HostConfig.RestartPolicy.Name)
		if cur == "" || cur == string(container.RestartPolicyDisabled) {
			continue
		}
		_, labelled := c.Labels[restartIntentLabel]
		if !labelled && intent[c.ID] != cur {
			intent[c.ID] = cur
			dirty = true
		}
		todo = append(todo, pending{c.ID, labelled})
	}
	if project == "" {
		for id := range intent { // forget containers that no longer exist
			if !present[id] {
				delete(intent, id)
				dirty = true
			}
		}
	}
	intentSaved := true
	if dirty {
		intentSaved = saveRestartIntent(intent) == nil
	}
	changed := 0
	for _, t := range todo {
		if !t.labelled && !intentSaved {
			continue
		}
		if _, err := d.ContainerUpdate(ctx, t.id, container.UpdateConfig{
			RestartPolicy: container.RestartPolicy{Name: container.RestartPolicyDisabled},
		}); err == nil {
			changed++
		}
	}
	if !intentSaved {
		return changed, fmt.Errorf("restart intent not saved; unlabelled containers keep their docker policy")
	}
	return changed, nil
}

// exitCodeFromStatus parses docker's "Exited (137) 3 minutes ago"; -1 if absent.
func exitCodeFromStatus(status string) int {
	s := strings.TrimSpace(status)
	if !strings.HasPrefix(s, "Exited (") {
		return -1
	}
	s = strings.TrimPrefix(s, "Exited (")
	if i := strings.Index(s, ")"); i > 0 {
		if n, err := strconv.Atoi(s[:i]); err == nil {
			return n
		}
	}
	return -1
}

// containerWantsRunning: would Docker itself, with the container's ORIGINAL
// restart policy, start this stopped container again? (owner 11-20: no
// behavioural difference between the loop and Docker.)
//   - always / unless-stopped: yes, on ANY exit (0, 1, 137 ...);
//   - on-failure[:N]: only after a non-zero exit (N is enforced by the API,
//     which counts the attempts per container);
//   - no / false: never (an exited init container stays down quietly);
//   - a never-started (`created`) container: never, as with Docker;
//   - `default` (the service declared no `restart:`, review2 M4): after a
//     non-zero exit, or an exit 0 from BEFORE the current host boot (stopped
//     by the shutdown); a clean exit 0 after this boot is a finished job and
//     stays down. exitedBeforeBoot is only consulted for that case;
//   - anything else (unknown value): never (Docker's own default is `no`).
func containerWantsRunning(intent, state, status string, exitedBeforeBoot func() bool) bool {
	if state == "running" || state == "restarting" || state == "paused" || state == "created" {
		return false
	}
	code := exitCodeFromStatus(status)
	switch {
	case intent == "no" || intent == "false" || intent == "":
		return false
	case intent == "always" || intent == "unless-stopped":
		return true
	case strings.HasPrefix(intent, "on-failure"):
		return code > 0
	case intent != restartIntentDefault:
		return false
	}
	if code > 0 {
		return true
	}
	return code == 0 && exitedBeforeBoot != nil && exitedBeforeBoot()
}

// DownService is one container the loop wants started (heartbeat
// `down_services`): its compose service, original policy and exit code.
type DownService struct {
	Service string `json:"service"`
	Intent  string `json:"intent"`
	Exit    int    `json:"exit"`
}

// hostBootTime is the kernel boot time (/proc/stat btime; a container shares
// the host kernel). Zero when unknown: then no exit counts as "before boot".
var hostBootTime = func() func() time.Time {
	var once sync.Once
	var bt time.Time
	return func() time.Time {
		once.Do(func() {
			f, err := os.Open("/proc/stat")
			if err != nil {
				return
			}
			defer f.Close()
			sc := bufio.NewScanner(f)
			for sc.Scan() {
				if v, ok := strings.CutPrefix(sc.Text(), "btime "); ok {
					if n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64); err == nil {
						bt = time.Unix(n, 0)
					}
					return
				}
			}
		})
		return bt
	}
}()

// exitedBeforeBootFn returns a per-container check: the container's FinishedAt
// is earlier than the host boot time.
func exitedBeforeBootFn(ctx context.Context, d stackPolicyDocker) func(id string) bool {
	return func(id string) bool {
		boot := hostBootTime()
		if d == nil || boot.IsZero() {
			return false
		}
		info, err := d.ContainerInspect(ctx, id)
		if err != nil || info.ContainerJSONBase == nil || info.State == nil {
			return false
		}
		fin, err := time.Parse(time.RFC3339Nano, info.State.FinishedAt)
		if err != nil || fin.IsZero() || fin.Year() < 2000 {
			return false
		}
		return fin.Before(boot)
	}
}

// intentOf: the label, else the recorded pre-loop policy, else `no` (review2
// M4: no label and no record means the container's own Docker policy was
// already `no` when the loop first saw it, so it was never recorded).
func intentOf(c types.Container, recorded map[string]string) string {
	if v, ok := c.Labels[restartIntentLabel]; ok && strings.TrimSpace(v) != "" {
		return strings.TrimSpace(v)
	}
	if v := strings.TrimSpace(recorded[c.ID]); v != "" {
		return v
	}
	return "no"
}

// stackDownCount returns, per managed project, the containers that should be
// running but are not (every managed project has an entry, possibly empty).
func stackDownCount(containers []types.Container, recorded map[string]string, beforeBoot func(id string) bool) map[string][]DownService {
	out := map[string][]DownService{}
	for _, c := range containers {
		if !isManagedStackContainer(c) {
			continue
		}
		p := strings.TrimSpace(c.Labels[composeProjectLabel])
		id := c.ID
		intent := intentOf(c, recorded)
		if containerWantsRunning(intent, c.State, c.Status, func() bool {
			return beforeBoot != nil && beforeBoot(id)
		}) {
			out[p] = append(out[p], DownService{Service: strings.TrimSpace(c.Labels[stackServiceLabel]),
				Intent: intent, Exit: exitCodeFromStatus(c.Status)})
		} else if _, seen := out[p]; !seen {
			out[p] = []DownService{}
		}
	}
	return out
}

// stackDownServices lists the compose services of `project` that should run
// but do not, and how many of its containers are running now (review H2).
func stackDownServices(ctx context.Context, d stackPolicyDocker, project string) ([]string, int, error) {
	if d == nil {
		return nil, 0, fmt.Errorf("no docker client")
	}
	list, err := d.ContainerList(ctx, stackListOptions(false))
	if err != nil {
		return nil, 0, err
	}
	restartIntentMu.Lock()
	recorded := loadRestartIntent()
	restartIntentMu.Unlock()
	before := exitedBeforeBootFn(ctx, d)
	var down []string
	seen := map[string]bool{}
	running := 0
	for _, c := range list {
		if strings.TrimSpace(c.Labels[composeProjectLabel]) != project {
			continue
		}
		if c.State == "running" || c.State == "restarting" || c.State == "paused" {
			running++
			continue
		}
		id := c.ID
		if !containerWantsRunning(intentOf(c, recorded), c.State, c.Status, func() bool { return before(id) }) {
			continue
		}
		if svc := strings.TrimSpace(c.Labels[stackServiceLabel]); svc != "" && !seen[svc] {
			seen[svc] = true
			down = append(down, svc)
		}
	}
	return down, running, nil
}

// Per-project serialisation of stack lifecycle ops on a loop worker (review
// H4): a stop/teardown bumps the project's stop generation the moment it
// arrives; a boot_restore that was waiting behind it sees the new generation
// and does not start anything.
//
// Review2 N1 (data loss): the volume/data ops that stop or read a stack on
// purpose (in-place `restore`, `snapshot` with its stop fallback, `rehearse`,
// `logical_restore`, `logical_backup`) take the same per-project lock. While
// one is queued or running the project is HELD: the heartbeat reports no
// `down` for it (stackobserve.go), so the API queues no boot_restore, and a
// boot_restore already waiting behind it is skipped (restore/snapshot bump the
// stop generation like a stop). A restore that fails after stopping the stack
// keeps it held (Docker's unless-stopped would not restart a container that
// was stopped on purpose either) until the next deploy/migrate/start/stop/
// teardown of that project.
var (
	stackOpMu     sync.Mutex
	stackOpLocks  = map[string]*sync.Mutex{}
	stackStopGens = map[string]uint64{}
	stackHeld     = map[string]int{}  // volume ops queued or running
	stackHeldStop = map[string]bool{} // a failed in-place restore left it stopped
)

// stackVolumeOps stop, freeze or read a stack's data on purpose.
var stackVolumeOps = map[string]bool{
	"restore": true, "snapshot": true, "rehearse": true,
	"logical_restore": true, "logical_backup": true,
}

// stackLifecycleOps are the operator/API lifecycle ops (review H4).
var stackLifecycleOps = map[string]bool{
	"deploy": true, "migrate": true, "start": true, "stop": true,
	"teardown": true, "boot_restore": true,
}

func stackProjectLock(project string, bumpStop bool) (*sync.Mutex, uint64) {
	stackOpMu.Lock()
	defer stackOpMu.Unlock()
	mu, ok := stackOpLocks[project]
	if !ok {
		mu = &sync.Mutex{}
		stackOpLocks[project] = mu
	}
	if bumpStop {
		stackStopGens[project]++
	}
	return mu, stackStopGens[project]
}

func stackHoldAdd(project string, delta int) {
	stackOpMu.Lock()
	defer stackOpMu.Unlock()
	stackHeld[project] += delta
	if stackHeld[project] <= 0 {
		delete(stackHeld, project)
	}
}

func stackHoldStopped(project string, on bool) {
	stackOpMu.Lock()
	defer stackOpMu.Unlock()
	if on {
		stackHeldStop[project] = true
	} else {
		delete(stackHeldStop, project)
	}
}

// stackProjectHeld: a volume op is queued/running for the project, or a failed
// in-place restore left it stopped. The loop must not count it as down.
func stackProjectHeld(project string) bool {
	stackOpMu.Lock()
	defer stackOpMu.Unlock()
	return stackHeld[project] > 0 || stackHeldStop[project]
}

func stackStopGen(project string) uint64 {
	stackOpMu.Lock()
	defer stackOpMu.Unlock()
	return stackStopGens[project]
}

// runStackOpSerialized wraps runStackOp. Loop disabled (paid default) or an op
// without a project: exactly runStackOp.
func (e *Executor) runStackOpSerialized(ctx context.Context, task *transport.Task, creds *transport.Credentials, started time.Time, cb Callbacks) (*Result, error) {
	p := task.Payload
	project := strings.TrimSpace(p.StackProject)
	op := p.StackOp
	volumeOp := stackVolumeOps[op]
	if !StackWorkerStartEnabled() || project == "" || !(stackLifecycleOps[op] || volumeOp) {
		return e.runStackOp(ctx, task, creds, started, cb)
	}
	if volumeOp {
		stackHoldAdd(project, 1) // held from the moment it is queued here
		defer stackHoldAdd(project, -1)
	}
	mu, gen := stackProjectLock(project, op == "stop" || op == "teardown" || op == "restore" || op == "snapshot")
	mu.Lock()
	defer mu.Unlock()
	if op == "boot_restore" && (stackStopGen(project) != gen || stackProjectHeld(project)) {
		return &Result{LogTail: "boot_restore: skipped, a stop/teardown/restore/snapshot for " + project +
			" arrived while it waited or left it stopped", Duration: time.Since(started)}, nil
	}
	if !volumeOp && op != "boot_restore" {
		stackHoldStopped(project, false) // an operator lifecycle op ends the hold
	}
	res, err := e.runStackOp(ctx, task, creds, started, cb)
	if op == "restore" && err != nil {
		// The in-place restore stopped the stack and did not finish: never let
		// the loop start it on a half-written data dir.
		stackHoldStopped(project, true)
	}
	// Also after a FAILED deploy/migrate (review2 L6): containers it created
	// must not keep a Docker restart policy until the next worker start.
	if e.docker != nil && (op == "deploy" || op == "migrate" || op == "start" || op == "boot_restore") {
		uctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		if _, uerr := disableStackAutostart(uctx, e.docker, project); uerr != nil {
			fmt.Fprintf(os.Stderr, "stack autostart: restart-policy update for %s: %v\n", project, uerr)
		}
		cancel()
	}
	return res, err
}

// StackStateChecker asks the manager whether the stack of a boot_restore task
// may be started now (start, "desired/status" for logs, error). A func rather
// than an import, matching CheckpointReaper.
type StackStateChecker func(ctx context.Context, taskID int) (bool, string, error)

var (
	stackStateMu    sync.Mutex
	stackStateCheck StackStateChecker
)

// OnStackState wires the live desired-state check (main.go, API transport).
func (e *Executor) OnStackState(fn StackStateChecker) {
	stackStateMu.Lock()
	stackStateCheck = fn
	stackStateMu.Unlock()
}

// stackStartAllowed: fail closed (owner 11-20). No checker wired, an
// unreachable API, an auth/not-found answer, or "not running" all mean: do
// not start anything for this restore.
func stackStartAllowed(ctx context.Context, taskID int) (bool, string) {
	stackStateMu.Lock()
	fn := stackStateCheck
	stackStateMu.Unlock()
	if fn == nil {
		return false, "no stack-state channel"
	}
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	ok, state, err := fn(cctx, taskID)
	if err != nil {
		return false, "stack-state check failed: " + err.Error()
	}
	if !ok {
		return false, "operator state " + state
	}
	return true, state
}

// stackDockerFor returns the docker client the loop uses (tests swap it).
var stackDockerFor = func(e *Executor) stackPolicyDocker {
	if e.docker == nil {
		return nil
	}
	return e.docker
}

// bootRestoreServices is the exact service list a boot_restore starts (review2
// M1): the API's `restore_services` when given, else the worker's own down set.
// Never empty-means-all: an empty result means nothing is started.
func bootRestoreServices(want, down []string) []string {
	clean := func(src []string) []string {
		out := make([]string, 0, len(src))
		seen := map[string]bool{}
		for _, s := range src {
			s = strings.TrimSpace(s)
			if s == "" || s == "*" || strings.HasPrefix(s, "-") || seen[s] {
				continue
			}
			seen[s] = true
			out = append(out, s)
		}
		return out
	}
	if out := clean(want); len(out) > 0 {
		return out
	}
	return clean(down)
}

// bootRestoreStart is the start half of a boot_restore, after the stack files
// were written (owner 11-20, per-container Docker semantics): `compose start
// <services>` for exactly the containers Docker would have restarted (the
// API's `restore_services`, else the worker's own down set), also when the
// whole project is down after a reboot (review2 M1: never a bare `compose
// start`, which would run one-shot and never-started containers too; the old
// `compose stop` before it was a no-op with nothing running and is gone).
// Compose orders them by depends_on. Healthy siblings are never stopped.
//
// Immediately before any start it asks the API whether the stack may start
// (fail closed), and once more afterwards to report the truth.
func (e *Executor) bootRestoreStart(ctx context.Context, cb Callbacks, taskID int, project string, want, secrets []string, secretsWritten bool, started time.Time) (*Result, error) {
	var down []string
	running := -1
	if d := stackDockerFor(e); d != nil {
		if ds, r, err := stackDownServices(ctx, d, project); err == nil {
			down, running = ds, r
		}
	}
	svcs := bootRestoreServices(want, down)
	if len(svcs) == 0 {
		return &Result{LogTail: "boot_restore: files written, nothing down in " + project, Duration: time.Since(started)}, nil
	}
	if ok, why := stackStartAllowed(ctx, taskID); !ok {
		return &Result{LogTail: "boot_restore: not started for " + project + ": " + why, Duration: time.Since(started)}, nil
	}
	args := append([]string{"start"}, svcs...)
	what := "started " + strings.Join(svcs, " ") + " in " + project
	if running == 0 && secretsWritten {
		what += " (whole project was down)"
	}
	out, err := e.composeCmdRedacted(ctx, cb, project, "", secrets, nil, args...)
	if err != nil {
		return nil, fmt.Errorf("boot_restore: compose %s failed: %w\n%s", strings.Join(args, " "), err, tail(out))
	}
	_, after := stackStartAllowed(ctx, taskID)
	return &Result{LogTail: tail("boot_restore: files written, " + what + "; after: " + after + "\n" + out), Duration: time.Since(started)}, nil
}
