package executor

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

type fakePolicyDocker struct {
	list     []types.Container
	policies map[string]string
	updated  []string
}

func (f *fakePolicyDocker) ContainerList(ctx context.Context, o container.ListOptions) ([]types.Container, error) {
	return f.list, nil
}

func (f *fakePolicyDocker) ContainerInspect(ctx context.Context, id string) (types.ContainerJSON, error) {
	hc := &container.HostConfig{RestartPolicy: container.RestartPolicy{Name: container.RestartPolicyMode(f.policies[id])}}
	return types.ContainerJSON{ContainerJSONBase: &types.ContainerJSONBase{ID: id, HostConfig: hc}}, nil
}

func (f *fakePolicyDocker) ContainerUpdate(ctx context.Context, id string, u container.UpdateConfig) (container.ContainerUpdateOKBody, error) {
	if u.RestartPolicy.Name != container.RestartPolicyDisabled {
		panic("restart policy must be no")
	}
	f.updated = append(f.updated, id)
	return container.ContainerUpdateOKBody{}, nil
}

func withIntentFile(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	old := restartIntentPath
	restartIntentPath = func() string { return filepath.Join(dir, "intent.json") }
	t.Cleanup(func() { restartIntentPath = old })
}

func stackC(id, project, state, status string, labels map[string]string) types.Container {
	l := map[string]string{composeProjectLabel: project}
	for k, v := range labels {
		l[k] = v
	}
	return types.Container{ID: id, State: state, Status: status, Labels: l}
}

func TestDisableStackAutostartOnlyManagedStacks(t *testing.T) {
	withIntentFile(t)
	f := &fakePolicyDocker{
		list: []types.Container{
			stackC("a1", "modstack1g0", "running", "Up", nil),
			stackC("a2", "modstack1g0", "running", "Up", map[string]string{restartIntentLabel: "unless-stopped"}),
			stackC("a3", "modstack2g0", "exited", "Exited (0)", nil),
			stackC("core", "mod-core-ce", "running", "Up", nil),
		},
		policies: map[string]string{"a1": "unless-stopped", "a2": "unless-stopped", "a3": "no", "core": "unless-stopped"},
	}
	n, err := disableStackAutostart(context.Background(), f, "")
	if err != nil || n != 2 {
		t.Fatalf("changed=%d err=%v, want 2", n, err)
	}
	if strings.Join(f.updated, ",") != "a1,a2" {
		t.Fatalf("updated %v; core and already-no containers must be untouched", f.updated)
	}
	rec := loadRestartIntent()
	if rec["a1"] != "unless-stopped" || rec["a2"] != "" {
		t.Fatalf("intent record %v: only the unlabelled container is recorded", rec)
	}
}

// Every policy x exit code (0, 1, killed 137) x state, mirroring Docker
// (owner 11-20). The operator-stopped axis is the API's desired_state and the
// live stack-state check (TestBootRestore*Stopped* below).
func TestContainerWantsRunning(t *testing.T) {
	never := func() bool { return false }
	cases := []struct {
		intent, state, status string
		want                  bool
	}{
		{"always", "exited", "Exited (0) 1m", true},
		{"always", "exited", "Exited (1) 1m", true},
		{"always", "exited", "Exited (137) 1m", true},
		{"unless-stopped", "exited", "Exited (0) 1m", true},
		{"unless-stopped", "exited", "Exited (1) 1m", true},
		{"unless-stopped", "exited", "Exited (137) 1m", true},
		{"on-failure", "exited", "Exited (0) 1m", false},
		{"on-failure", "exited", "Exited (1) 1m", true},
		{"on-failure:3", "exited", "Exited (137) 1m", true},
		{"no", "exited", "Exited (0) 1m", false},
		{"no", "exited", "Exited (1) 1m", false},
		{"no", "exited", "Exited (137) 1m", false},
		{"", "exited", "Exited (0) 1m", false},
		{"", "exited", "Exited (1) 1m", false},
		{"", "exited", "Exited (137) 1m", false},
		{"default", "exited", "Exited (0) 1m", false},
		{"default", "exited", "Exited (1) 1m", true},
		{"default", "exited", "Exited (137) 1m", true},
		{"default", "created", "Created", false},
		{"bogus", "exited", "Exited (1) 1m", false},
		{"always", "created", "Created", false},
		{"unless-stopped", "running", "Up", false},
		{"always", "paused", "Up (Paused)", false},
	}
	for _, c := range cases {
		if got := containerWantsRunning(c.intent, c.state, c.status, never); got != c.want {
			t.Errorf("%+v: got %v", c, got)
		}
	}
	// review2 M4: `default` (no restart: key): an exit 0 from before this host
	// boot (stopped by the shutdown) counts; after this boot it is a finished job.
	if !containerWantsRunning("default", "exited", "Exited (0) 1h", func() bool { return true }) {
		t.Error("pre-boot exit 0 of a default-policy container must be restored")
	}
	if containerWantsRunning("", "exited", "Exited (0) 1h", func() bool { return true }) {
		t.Error("no label and no record means Docker's default `no`")
	}
}

func TestStackDownCount(t *testing.T) {
	list := []types.Container{
		stackC("m", "modstack1g0", "exited", "Exited (127) 1m", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "web"}),
		stackC("s", "modstack1g0", "running", "Up", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "side"}),
		stackC("job", "modstack1g0", "exited", "Exited (0) 1m", map[string]string{restartIntentLabel: "no", stackServiceLabel: "init"}),
		stackC("old", "modstack2g0", "exited", "Exited (0) 1m", nil),
		stackC("core", "mod-core-ce", "exited", "Exited (1) 1m", nil),
		stackC("user", "modstackfoo", "exited", "Exited (1) 1m", nil),
		stackC("norec", "modstack2g0", "exited", "Exited (1) 1m", nil),
	}
	got := stackDownCount(list, map[string]string{"old": "unless-stopped"}, nil)
	if len(got["modstack1g0"]) != 1 || got["modstack1g0"][0].Service != "web" ||
		got["modstack1g0"][0].Exit != 127 || len(got["modstack2g0"]) != 1 || len(got) != 2 {
		t.Fatalf("down=%v", got)
	}
}

func runBootRestore(t *testing.T, psOut string) ([]string, error) {
	t.Helper()
	var cmds []string
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
		cmds = append(cmds, strings.Join(args, " "))
		if args[0] == "ps" {
			return psOut, nil
		}
		return "", nil
	}
	task := &transport.Task{Payload: transport.TaskPayload{StackOp: "boot_restore", StackProject: "modstack1g0"}}
	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	return cmds, err
}

func TestBootRestoreNoContainersFallsBack(t *testing.T) {
	cmds, err := runBootRestore(t, "")
	if err == nil || !strings.Contains(err.Error(), "no containers") {
		t.Fatalf("want no-containers error, got %v", err)
	}
	if len(cmds) != 1 {
		t.Fatalf("must not stop/start when containers are absent: %v", cmds)
	}
}

// bootRestoreCase runs bootRestoreStart against a fake daemon list and a fake
// API answer, returning the exact compose commands issued.
func bootRestoreCase(t *testing.T, list []types.Container, want []string, secrets bool,
	check StackStateChecker) []string {
	t.Helper()
	withIntentFile(t)
	var cmds []string
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, s, extraEnv []string, args ...string) (string, error) {
		cmds = append(cmds, strings.Join(args, " "))
		return "", nil
	}
	oldD := stackDockerFor
	stackDockerFor = func(*Executor) stackPolicyDocker { return &fakePolicyDocker{list: list} }
	t.Cleanup(func() { stackDockerFor = oldD })
	e.OnStackState(check)
	t.Cleanup(func() { e.OnStackState(nil) })
	if _, err := e.bootRestoreStart(context.Background(), Callbacks{}, 7, "modstack1g0", want, nil, secrets, time.Now()); err != nil {
		t.Fatalf("bootRestoreStart: %v", err)
	}
	for _, c := range cmds {
		for _, w := range strings.Fields(c) {
			for _, bad := range []string{"down", "-v", "--force-recreate", "up", "rm", "restart"} {
				if w == bad {
					t.Fatalf("forbidden %q in %q", bad, c)
				}
			}
		}
	}
	return cmds
}

func apiSays(start bool) StackStateChecker {
	return func(ctx context.Context, id int) (bool, string, error) {
		if start {
			return true, "running/degraded", nil
		}
		return false, "stopped/stopped", nil
	}
}

func partlyDown() []types.Container {
	return []types.Container{
		stackC("w", "modstack1g0", "exited", "Exited (1) 5s", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "web"}),
		stackC("s", "modstack1g0", "running", "Up", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "side"}),
		stackC("i", "modstack1g0", "exited", "Exited (0) 9m", map[string]string{restartIntentLabel: "no", stackServiceLabel: "init"}),
	}
}

func TestBootRestoreStartsOnlyDownServiceNeverHealthySibling(t *testing.T) {
	cmds := bootRestoreCase(t, partlyDown(), nil, true, apiSays(true))
	if strings.Join(cmds, "|") != "start web" {
		t.Fatalf("commands %v, want exactly [start web] (no stop, no init, no sidecar)", cmds)
	}
}

func TestBootRestoreUsesAPIServiceList(t *testing.T) {
	cmds := bootRestoreCase(t, partlyDown(), []string{"web"}, false, apiSays(true))
	if strings.Join(cmds, "|") != "start web" {
		t.Fatalf("commands %v", cmds)
	}
}

func TestBootRestoreAllDownWithSecretsNamesServices(t *testing.T) {
	list := []types.Container{
		stackC("w", "modstack1g0", "exited", "Exited (0) 1h", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "web"}),
		stackC("s", "modstack1g0", "exited", "Exited (137) 1h", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "side"}),
	}
	cmds := bootRestoreCase(t, list, nil, true, apiSays(true))
	if strings.Join(cmds, "|") != "start web side" {
		t.Fatalf("commands %v, want start web side (review2 M1: never a bare start)", cmds)
	}
	cmds = bootRestoreCase(t, list, nil, false, apiSays(true)) // no secret files: per service
	if strings.Join(cmds, "|") != "start web side" {
		t.Fatalf("commands %v, want start web side", cmds)
	}
}

func TestBootRestoreOperatorStoppedNoStart(t *testing.T) {
	if cmds := bootRestoreCase(t, partlyDown(), nil, false, apiSays(false)); len(cmds) != 0 {
		t.Fatalf("API says stopped: no command allowed, got %v", cmds)
	}
}

func TestBootRestoreAPIUnreachableNoStart(t *testing.T) {
	down := func(ctx context.Context, id int) (bool, string, error) {
		return false, "", errors.New("dial tcp: connection refused")
	}
	if cmds := bootRestoreCase(t, partlyDown(), nil, false, down); len(cmds) != 0 {
		t.Fatalf("API unreachable: fail closed, got %v", cmds)
	}
	if cmds := bootRestoreCase(t, partlyDown(), nil, false, nil); len(cmds) != 0 {
		t.Fatalf("no stack-state channel: fail closed, got %v", cmds)
	}
}

func TestBootRestoreNothingDownIsNoop(t *testing.T) {
	list := []types.Container{
		stackC("i", "modstack1g0", "exited", "Exited (0) 9m", map[string]string{restartIntentLabel: "no", stackServiceLabel: "init"}),
		stackC("s", "modstack1g0", "running", "Up", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "main"}),
	}
	if cmds := bootRestoreCase(t, list, nil, true, apiSays(true)); len(cmds) != 0 {
		t.Fatalf("init exit 0 with intent no must stay down quietly, got %v", cmds)
	}
}

// Review2 M1: the start list is always explicit, never empty-means-all.
func TestBootRestoreServicesTable(t *testing.T) {
	cases := []struct {
		want, down []string
		out        string
	}{
		{nil, nil, ""},
		{nil, []string{"web", "db"}, "web db"},
		{[]string{"web"}, []string{"web", "db"}, "web"},
		{[]string{"*"}, nil, ""},
		{[]string{"*"}, []string{"db"}, "db"},
		{[]string{"web", "web", " ", "-d"}, nil, "web"},
	}
	for _, c := range cases {
		if got := strings.Join(bootRestoreServices(c.want, c.down), " "); got != c.out {
			t.Errorf("want=%v down=%v: got %q, want %q", c.want, c.down, got, c.out)
		}
	}
}

// Review2 M1: whole project down after a reboot, with a finished one-shot
// (intent no), a never-started container and a default-policy init that exited
// 0 after boot: only the real services are started, by name.
func TestBootRestoreWholeDownNeverBareStart(t *testing.T) {
	list := []types.Container{
		stackC("w", "modstack1g0", "exited", "Exited (0) 1h", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "web"}),
		stackC("d", "modstack1g0", "exited", "Exited (143) 1h", map[string]string{restartIntentLabel: "default", stackServiceLabel: "db"}),
		stackC("j", "modstack1g0", "exited", "Exited (0) 2d", map[string]string{restartIntentLabel: "no", stackServiceLabel: "seed"}),
		stackC("i", "modstack1g0", "exited", "Exited (0) 1m", map[string]string{restartIntentLabel: "default", stackServiceLabel: "init"}),
		stackC("c", "modstack1g0", "created", "Created", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "never"}),
	}
	cmds := bootRestoreCase(t, list, nil, true, apiSays(true))
	if strings.Join(cmds, "|") != "start web db" {
		t.Fatalf("commands %v, want exactly [start web db]", cmds)
	}
}

// Review2 M4: a default-policy init container that exited 0 is not down: the
// stack reports nothing down (stays running) and nothing is started.
func TestDefaultIntentInitExit0NotDown(t *testing.T) {
	list := []types.Container{
		stackC("i", "modstack1g0", "exited", "Exited (0) 1m", map[string]string{restartIntentLabel: "default", stackServiceLabel: "init"}),
		stackC("s", "modstack1g0", "running", "Up", map[string]string{restartIntentLabel: "default", stackServiceLabel: "main"}),
	}
	if got := stackDownCount(list, nil, func(string) bool { return false }); len(got["modstack1g0"]) != 0 {
		t.Fatalf("down=%v, want none", got)
	}
	if cmds := bootRestoreCase(t, list, nil, true, apiSays(true)); len(cmds) != 0 {
		t.Fatalf("got %v, want no command", cmds)
	}
}

// Review2 N1 (HIGH): an in-place restore holds the project; the loop's
// boot_restore that arrives meanwhile starts nothing until the restore is done,
// and a restore that failed after stopping the stack keeps it held (not "down")
// until the operator's next lifecycle op.
func TestVolumeOpHoldsStackLoop(t *testing.T) {
	t.Setenv("MOD_STACK_WORKER_START", "1")
	withIntentFile(t)
	const proj = "modstack9g0"
	var mu sync.Mutex
	var cmds []string
	inStop := make(chan struct{})
	release := make(chan struct{})
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, s, extraEnv []string, args ...string) (string, error) {
		mu.Lock()
		cmds = append(cmds, strings.Join(args, " "))
		mu.Unlock()
		if args[0] == "stop" {
			close(inStop)
			<-release
			return "", errors.New("simulated stop failure mid-restore")
		}
		if args[0] == "ps" {
			return "abc\n", nil
		}
		return "", nil
	}
	oldD := stackDockerFor
	stackDockerFor = func(*Executor) stackPolicyDocker {
		return &fakePolicyDocker{list: []types.Container{
			stackC("p", proj, "exited", "Exited (0) 1s", map[string]string{restartIntentLabel: "unless-stopped", stackServiceLabel: "postgres"}),
		}}
	}
	t.Cleanup(func() { stackDockerFor = oldD })
	e.OnStackState(apiSays(true))
	t.Cleanup(func() {
		e.OnStackState(nil)
		stackHoldStopped(proj, false)
	})

	restore := &transport.Task{ID: 1, Payload: transport.TaskPayload{StackOp: "restore", StackProject: proj, StackVolume: "v"}}
	boot := &transport.Task{ID: 2, Payload: transport.TaskPayload{StackOp: "boot_restore", StackProject: proj}}
	var wg sync.WaitGroup
	var restoreErr error
	wg.Add(1)
	go func() {
		defer wg.Done()
		_, restoreErr = e.runStackOpSerialized(context.Background(), restore, nil, time.Now(), Callbacks{})
	}()
	<-inStop
	if !stackProjectHeld(proj) {
		t.Fatal("project must be held while the restore runs")
	}
	var bootRes *Result
	wg.Add(1)
	go func() {
		defer wg.Done()
		bootRes, _ = e.runStackOpSerialized(context.Background(), boot, nil, time.Now(), Callbacks{})
	}()
	time.Sleep(150 * time.Millisecond)
	mu.Lock()
	during := strings.Join(cmds, "|")
	mu.Unlock()
	if during != "stop" {
		t.Fatalf("while the restore holds the lock only its stop may run, got %q", during)
	}
	close(release)
	wg.Wait()
	if restoreErr == nil {
		t.Fatal("restore must report its failure")
	}
	for _, c := range cmds {
		if strings.HasPrefix(c, "start") {
			t.Fatalf("no start allowed after a failed in-place restore, got %v", cmds)
		}
	}
	if bootRes == nil || !strings.Contains(bootRes.LogTail, "skipped") {
		t.Fatalf("boot_restore must be skipped, got %+v", bootRes)
	}
	if !stackProjectHeld(proj) {
		t.Fatal("a failed in-place restore keeps the project held")
	}
	// The operator's own start ends the hold.
	start := &transport.Task{ID: 3, Payload: transport.TaskPayload{StackOp: "start", StackProject: proj}}
	_, _ = e.runStackOpSerialized(context.Background(), start, nil, time.Now(), Callbacks{})
	if stackProjectHeld(proj) {
		t.Fatal("an operator start must end the hold")
	}
}

// Paid default (flag unset): no hold, no serialisation change.
func TestVolumeOpNoHoldWhenLoopOff(t *testing.T) {
	t.Setenv("MOD_STACK_WORKER_START", "")
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, s, extraEnv []string, args ...string) (string, error) {
		if stackProjectHeld("modstack8g0") {
			t.Error("loop off: nothing may be held")
		}
		return "", errors.New("stop fails")
	}
	task := &transport.Task{Payload: transport.TaskPayload{StackOp: "restore", StackProject: "modstack8g0"}}
	_, _ = e.runStackOpSerialized(context.Background(), task, nil, time.Now(), Callbacks{})
	if stackProjectHeld("modstack8g0") {
		t.Fatal("loop off: a failed restore must not hold")
	}
}
