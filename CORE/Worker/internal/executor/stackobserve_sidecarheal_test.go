package executor

// 1522 — a stack's egress sidecar (or any other service container) can end up
// `exited` or `created` even while the rest of the stack keeps serving: the
// restart policy only covers a container that STARTED and later exited, not
// one whose start itself failed (the WSL bind-mount shim race that put
// modstack22g0-tsegress-site-switchyard-1 into Exited(127) for ~20h). These
// tests exercise healExitedStackSidecars with a fake `docker` (via the same
// dockerCLI seam stacknetheal_test.go uses) so no daemon is required.

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/docker/docker/api/types"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

var testSidecarHealLog = slog.New(slog.NewTextHandler(io.Discard, nil))

func fakeExitedContainer(id, project, service, state string) types.Container {
	return types.Container{
		ID:    id,
		State: state,
		Labels: map[string]string{
			composeProjectLabel: project,
			composeServiceLabel: service,
		},
	}
}

func resetSidecarHeal() {
	sidecarStartAttempts.Clear()
	sidecarStillDownWarned.Clear()
}

// fakeHealCLI records `start` calls and answers `inspect` with a canned
// "exit|policy" pair, so a test can dress a container up as a finished one-shot
// or a wedged sidecar without a daemon.
func fakeHealCLI(started *[]string, inspectOut string) func(context.Context, ...string) (string, error) {
	return func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			*started = append(*started, args[1])
		}
		if len(args) == 4 && args[0] == "inspect" {
			return inspectOut, nil
		}
		return "", nil
	}
}

func fakeOneShotContainer(id, project, service, state, oneoff string) types.Container {
	c := fakeExitedContainer(id, project, service, state)
	if oneoff != "" {
		c.Labels[composeOneoffLabel] = oneoff
	}
	return c
}

// healWarnCounter collects the WARN lines a test wants to count: it is a
// Write sink behind slog.NewTextHandler (the same shape backend_select_test.go
// uses for captureLogger), so a test can assert the "still down" line is
// logged once per container+state change instead of once per heartbeat.
type healWarnCounter struct {
	mu    sync.Mutex
	hits  int32
	last  string
}

func (w *healWarnCounter) Write(p []byte) (int, error) {
	line := string(p)
	w.mu.Lock()
	defer w.mu.Unlock()
	if strings.Contains(line, "level=WARN") && strings.Contains(line, "still down after max restart attempts") {
		w.hits++
		w.last = line
	}
	return len(p), nil
}

func newHealWarnLogger() (*slog.Logger, *healWarnCounter) {
	w := &healWarnCounter{}
	return slog.New(slog.NewTextHandler(w, nil)), w
}

func TestHealExitedStackSidecars_StartsExitedContainerInRunningStack(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var started []string
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			started = append(started, args[1])
		}
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-egress", "modstack22g0", "tsegress-site-switchyard", "exited"),
	}
	byProject := map[string]*StackObservation{
		"modstack22g0": {Project: "modstack22g0", Running: 2, Total: 3},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	if len(started) != 1 || started[0] != "c-egress" {
		t.Fatalf("expected docker start on the exited sidecar, got %v", started)
	}
}

func TestHealExitedStackSidecars_CreatedStateAlsoStarted(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var started []string
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			started = append(started, args[1])
		}
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-created", "modstack30g0", "holder", "created"),
	}
	byProject := map[string]*StackObservation{
		"modstack30g0": {Project: "modstack30g0", Running: 1, Total: 2},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	if len(started) != 1 || started[0] != "c-created" {
		t.Fatalf("expected docker start on the created-state container, got %v", started)
	}
}

func TestHealExitedStackSidecars_FencedOrStoppedStackIsNeverTouched(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	called := false
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		called = true
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-fenced", "modstack41g0", "vllm", "exited"),
	}
	// Running == 0: either this host fenced it (FenceStacks ran `compose
	// stop`) or the API sent an explicit "stop" StackOp. Either way the
	// worker must not bring it back on its own.
	byProject := map[string]*StackObservation{
		"modstack41g0": {Project: "modstack41g0", Running: 0, Total: 1},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	if called {
		t.Fatal("docker start was called on a stack with Running == 0 (fenced/stopped-on-purpose); must never touch it")
	}
}

func TestHealExitedStackSidecars_UnknownProjectIsNeverTouched(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	called := false
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		called = true
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-unknown", "modstack99g0", "svc", "exited"),
	}
	// Project absent from byProject (e.g. never observed as Healthy) must be
	// treated the same as Running == 0: leave it alone.
	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, map[string]*StackObservation{})

	if called {
		t.Fatal("docker start was called for a project with no observation at all")
	}
}

func TestHealExitedStackSidecars_CapRespected_NoMoreAttemptsAfterMax(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var startCalls int32
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			atomic.AddInt32(&startCalls, 1)
		}
		// Simulate the start never actually taking — the container keeps
		// reporting exited on every subsequent reconcile tick.
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-stuck", "modstack50g0", "tsegress-cloud-x", "exited"),
	}
	byProject := map[string]*StackObservation{
		"modstack50g0": {Project: "modstack50g0", Running: 1, Total: 2},
	}

	e := &Executor{log: testSidecarHealLog}
	for i := 0; i < sidecarRestartMaxAttemptsDefault+5; i++ {
		e.healExitedStackSidecars(context.Background(), containers, byProject)
	}

	if int(startCalls) != sidecarRestartMaxAttemptsDefault {
		t.Fatalf("expected exactly %d docker start attempts (cap), got %d",
			sidecarRestartMaxAttemptsDefault, startCalls)
	}
}

func TestHealExitedStackSidecars_MaxAttemptsTunableOverridesDefault(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var startCalls int32
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			atomic.AddInt32(&startCalls, 1)
		}
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-tunable", "modstack51g0", "tsegress-site-x", "exited"),
	}
	byProject := map[string]*StackObservation{
		"modstack51g0": {Project: "modstack51g0", Running: 1, Total: 2},
	}

	store := tunables.New()
	store.Apply(map[string]interface{}{
		sidecarRestartMaxAttemptsTunableKey: 2,
	})
	e := &Executor{log: testSidecarHealLog, tunables: store}
	for i := 0; i < 10; i++ {
		e.healExitedStackSidecars(context.Background(), containers, byProject)
	}

	if startCalls != 2 {
		t.Fatalf("server-pushed attempt cap of 2 should be honored, got %d start calls", startCalls)
	}
}

func TestHealExitedStackSidecars_VanishedContainerIDIsPruned(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		return "", nil
	}

	byProject := map[string]*StackObservation{
		"modstack70g0": {Project: "modstack70g0", Running: 1, Total: 2},
	}

	e := &Executor{log: testSidecarHealLog}

	// First tick: the sidecar is exited, so a start is attempted and an entry
	// for its ID is recorded.
	e.healExitedStackSidecars(context.Background(),
		[]types.Container{fakeExitedContainer("c-vanish", "modstack70g0", "tsegress", "exited")},
		byProject)
	if _, ok := sidecarStartAttempts.Load("c-vanish"); !ok {
		t.Fatal("expected an attempt-counter entry for c-vanish after the first heal pass")
	}

	// Second tick: observeStacks' container list no longer contains
	// "c-vanish" at all — removed, or `docker compose up` recreated it under
	// a fresh ID. Nothing in this call references the old ID.
	e.healExitedStackSidecars(context.Background(),
		[]types.Container{fakeExitedContainer("c-fresh", "modstack70g0", "tsegress", "exited")},
		byProject)

	if _, ok := sidecarStartAttempts.Load("c-vanish"); ok {
		t.Fatal("expected the attempt-counter entry for the vanished container ID c-vanish to be pruned")
	}
	if _, ok := sidecarStartAttempts.Load("c-fresh"); !ok {
		t.Fatal("expected c-fresh, present in the current container list, to still have its own entry")
	}
}

func TestHealExitedStackSidecars_RunningContainerIgnored(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	called := false
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		called = true
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-running", "modstack60g0", "vllm", "running"),
	}
	byProject := map[string]*StackObservation{
		"modstack60g0": {Project: "modstack60g0", Running: 2, Total: 2},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	if called {
		t.Fatal("a running container must never be passed to docker start")
	}
}

// --- SELFHEAL-ONESHOT-1126: a one-shot job that exited 0 is not a down sidecar.

func TestHealExitedStackSidecars_ExitedZeroOneShotIsNeverRestarted(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var started []string
	var inspects int32
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			started = append(started, args[1])
		}
		if len(args) == 4 && args[0] == "inspect" {
			atomic.AddInt32(&inspects, 1)
			return "0|no", nil // exit 0, restart policy no: a finished job
		}
		return "", nil
	}

	containers := []types.Container{
		fakeOneShotContainer("c-migrate", "modstack25g0", "migrate", "exited", ""),
	}
	byProject := map[string]*StackObservation{
		"modstack25g0": {Project: "modstack25g0", Running: 2, Total: 3},
	}

	e := &Executor{log: testSidecarHealLog}
	for i := 0; i < 3; i++ {
		e.healExitedStackSidecars(context.Background(), containers, byProject)
	}

	if len(started) != 0 {
		t.Fatalf("a one-shot that exited 0 must never be `docker start`ed, got %v", started)
	}
	if _, ok := sidecarStartAttempts.Load("c-migrate"); ok {
		t.Fatal("a one-shot must not consume an attempt-counter entry")
	}
	if inspects == 0 {
		t.Fatal("expected the one-shot check to inspect the exited container")
	}
}

func TestHealExitedStackSidecars_OneoffLabelExitsZeroIsNeverRestarted(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var started []string
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			started = append(started, args[1])
		}
		if len(args) == 4 && args[0] == "inspect" {
			// exit 0 but a restart policy that asks for it to stay up; the
			// compose one-off label still marks it a job, not a sidecar.
			return "0|unless-stopped", nil
		}
		return "", nil
	}

	containers := []types.Container{
		fakeOneShotContainer("c-oneoff", "modstack26g0", "init", "exited", "True"),
	}
	byProject := map[string]*StackObservation{
		"modstack26g0": {Project: "modstack26g0", Running: 1, Total: 2},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	if len(started) != 0 {
		t.Fatalf("com.docker.compose.oneoff=True with exit 0 must not be restarted, got %v", started)
	}
}

func TestHealExitedStackSidecars_ExitedOneSidecarStillRestarted(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var started []string
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			started = append(started, args[1])
		}
		if len(args) == 4 && args[0] == "inspect" {
			return "127|unless-stopped", nil // the 1522 wedged-sidecar shape
		}
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-egress", "modstack22g0", "tsegress-site-switchyard", "exited"),
	}
	byProject := map[string]*StackObservation{
		"modstack22g0": {Project: "modstack22g0", Running: 2, Total: 3},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	if len(started) != 1 || started[0] != "c-egress" {
		t.Fatalf("a sidecar that exited non-zero must still be healed, got %v", started)
	}
}

func TestHealExitedStackSidecars_CreatedSidecarHealedWithoutInspect(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var started []string
	var inspects int32
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			started = append(started, args[1])
		}
		if len(args) == 4 && args[0] == "inspect" {
			atomic.AddInt32(&inspects, 1)
			return "0|no", nil
		}
		return "", nil
	}

	containers := []types.Container{
		fakeOneShotContainer("c-created", "modstack30g0", "holder", "created", "True"),
	}
	byProject := map[string]*StackObservation{
		"modstack30g0": {Project: "modstack30g0", Running: 1, Total: 2},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	// "created" means it never started: it is not a FINISHED job, so the
	// one-shot skip (which only applies to exited) must not suppress the heal.
	if len(started) != 1 || started[0] != "c-created" {
		t.Fatalf("a created-state container must still be started, got %v", started)
	}
	if inspects != 0 {
		t.Fatalf("the one-shot inspect must not run for created-state containers, got %d", inspects)
	}
}

func TestHealExitedStackSidecars_OneShotNeverReachesStillDownWarning(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var log, counter = newHealWarnLogger()
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 4 && args[0] == "inspect" {
			return "0|no", nil
		}
		return "", nil
	}

	containers := []types.Container{
		fakeOneShotContainer("c-migrate2", "modstack27g0", "migrate", "exited", ""),
	}
	byProject := map[string]*StackObservation{
		"modstack27g0": {Project: "modstack27g0", Running: 1, Total: 2},
	}

	e := &Executor{log: log, tunables: nil}
	for i := 0; i < sidecarRestartMaxAttemptsDefault+5; i++ {
		e.healExitedStackSidecars(context.Background(), containers, byProject)
	}

	if counter.hits != 0 {
		t.Fatalf("a finished one-shot must never log the still-down warning, got %d hits", counter.hits)
	}
}

func TestHealExitedStackSidecars_StillDownWarningLoggedOncePerState(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var log, counter = newHealWarnLogger()
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 4 && args[0] == "inspect" {
			return "1|unless-stopped", nil
		}
		return "", nil
	}

	byProject := map[string]*StackObservation{
		"modstack50g0": {Project: "modstack50g0", Running: 1, Total: 2},
	}
	e := &Executor{log: log}

	// Same container, same state, many heartbeats: one warning line only.
	for i := 0; i < sidecarRestartMaxAttemptsDefault+6; i++ {
		e.healExitedStackSidecars(context.Background(),
			[]types.Container{fakeExitedContainer("c-stuck2", "modstack50g0", "tsegress", "exited")},
			byProject)
	}
	if counter.hits != 1 {
		t.Fatalf("expected the still-down warning once per container+state, got %d", counter.hits)
	}

	// A state change (exited -> created) is new information: warn again.
	e.healExitedStackSidecars(context.Background(),
		[]types.Container{fakeExitedContainer("c-stuck2", "modstack50g0", "tsegress", "created")},
		byProject)
	if counter.hits != 2 {
		t.Fatalf("expected a second warning after a state change, got %d", counter.hits)
	}
}

func TestHealExitedStackSidecars_FailedInspectFallsBackToHeal(t *testing.T) {
	resetSidecarHeal()
	orig := dockerCLI
	defer func() { dockerCLI = orig }()

	var started []string
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		if len(args) == 2 && args[0] == "start" {
			started = append(started, args[1])
		}
		if len(args) == 4 && args[0] == "inspect" {
			return "", fmt.Errorf("docker: no such container")
		}
		return "", nil
	}

	containers := []types.Container{
		fakeExitedContainer("c-inspectfail", "modstack52g0", "tsegress", "exited"),
	}
	byProject := map[string]*StackObservation{
		"modstack52g0": {Project: "modstack52g0", Running: 1, Total: 2},
	}

	e := &Executor{log: testSidecarHealLog}
	e.healExitedStackSidecars(context.Background(), containers, byProject)

	if len(started) != 1 || started[0] != "c-inspectfail" {
		t.Fatalf("an unreadable inspect must fall back to the old heal behaviour, got %v", started)
	}
}
