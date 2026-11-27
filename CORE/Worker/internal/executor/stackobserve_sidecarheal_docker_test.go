package executor

// SELFHEAL-ONESHOT-1126 — real-docker leg. A throwaway compose project
// `selfheal-oneshot`: keeper (stays up, so the project is Healthy), oneshot
// (exits 0, restart "no" — a finished job), flaky (exits 1, restart "no" so
// Docker itself does not retry it — the wedged-sidecar shape).
// healExitedStackSidecars must leave the exited-0 job alone (no start, no
// attempt entry, no still-down warning) and keep retrying the exited-1 one
// to the cap, warning exactly once. Skipped unless SELFHEAL_DOCKER=1 and a
// daemon is reachable; the lane run owns project cleanup.

import (
	"context"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/client"
)

const realHealProject = "selfheal-oneshot"

type realWarnSink struct {
	mu   sync.Mutex
	hits int
}

func (w *realWarnSink) Write(p []byte) (int, error) {
	line := string(p)
	w.mu.Lock()
	defer w.mu.Unlock()
	if strings.Contains(line, "still down after max restart attempts") {
		w.hits++
	}
	return len(p), nil
}

func composeArgv(dir string) []string {
	return []string{"docker", "compose", "-p", realHealProject,
		"-f", filepath.Join(dir, "compose.yml")}
}

func composeUp(dir string) (string, error) {
	argv := composeArgv(dir)
	out, err := exec.Command(argv[0], argv[1], argv[2], argv[3], argv[4], argv[5], "up", "-d").CombinedOutput()
	return string(out), err
}

func composeDown(dir string) (string, error) {
	argv := composeArgv(dir)
	out, err := exec.Command(argv[0], argv[1], argv[2], argv[3], argv[4], argv[5],
		"down", "-v", "--remove-orphans").CombinedOutput()
	return string(out), err
}

func listHealContainers(cli *client.Client) ([]types.Container, error) {
	return cli.ContainerList(context.Background(), container.ListOptions{
		All:     true,
		Filters: filters.NewArgs(filters.Arg("label", composeProjectLabel+"="+realHealProject)),
	})
}

func findService(containers []types.Container, service string) string {
	for _, c := range containers {
		if strings.TrimSpace(c.Labels[composeServiceLabel]) == service {
			return c.ID
		}
	}
	return ""
}

func TestHealExitedStackSidecarsRealDockerOneShotSkippedSidecarRetried(t *testing.T) {
	if os.Getenv("SELFHEAL_DOCKER") != "1" {
		t.Skip("SELFHEAL_DOCKER=1 required (real daemon; gpu5 lane run)")
	}
	if out, err := exec.Command("docker", "version", "--format", "{{.Server.Version}}").CombinedOutput(); err != nil {
		t.Skipf("no docker daemon reachable: %v %s", err, string(out))
	}

	tmpdir := os.Getenv("TMPDIR")
	if tmpdir == "" {
		tmpdir = "/tmp"
	}
	dir := filepath.Join(tmpdir, realHealProject)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	const yml = `services:
  keeper:
    image: alpine:3.20
    command: ["sh", "-c", "sleep 600"]
    restart: unless-stopped
  oneshot:
    image: alpine:3.20
    command: ["sh", "-c", "exit 0"]
    restart: "no"
  flaky:
    image: alpine:3.20
    command: ["sh", "-c", "exit 1"]
    restart: "no"
`
	if err := os.WriteFile(filepath.Join(dir, "compose.yml"), []byte(yml), 0o600); err != nil {
		t.Fatalf("write compose.yml: %v", err)
	}
	t.Cleanup(func() {
		// The lane run owns cleanup: project down (containers + network) and
		// the fixture directory removed. Nothing else on this host is touched.
		composeDown(dir)
		_ = os.RemoveAll(dir)
	})

	if out, err := composeUp(dir); err != nil {
		t.Fatalf("compose up: %v\n%s", err, out)
	}

	cli, err := client.NewClientWithOpts(client.FromEnv)
	if err != nil {
		t.Fatalf("docker client: %v", err)
	}
	defer cli.Close()

	var oneshotID, flakyID, oneshotFinished string
	for i := 0; i < 60; i++ {
		list, lerr := listHealContainers(cli)
		if lerr != nil {
			t.Fatalf("container list: %v", lerr)
		}
		var exited0, exited1 int
		for _, c := range list {
			if c.State == "exited" && strings.Contains(c.Status, "Exited (0)") {
				exited0++
				oneshotID = c.ID
			}
			if c.State == "exited" && strings.Contains(c.Status, "Exited (1)") {
				exited1++
				flakyID = c.ID
			}
		}
		if exited0 == 1 && exited1 == 1 {
			break
		}
		time.Sleep(500 * time.Millisecond)
		if i == 59 {
			t.Fatalf("oneshot/flaky never reached Exited(0)/Exited(1)")
		}
	}
	{
		info, ierr := cli.ContainerInspect(context.Background(), oneshotID)
		if ierr != nil {
			t.Fatalf("inspect oneshot: %v", ierr)
		}
		oneshotFinished = info.State.FinishedAt
	}

	sidecarStartAttempts.Clear()
	sidecarStillDownWarned.Clear()
	sink := &realWarnSink{}
	log := slog.New(slog.NewTextHandler(sink, nil))
	e := &Executor{log: log}

	for i := 0; i < 20; i++ {
		list, lerr := listHealContainers(cli)
		if lerr != nil {
			t.Fatalf("relist: %v", lerr)
		}
		e.healExitedStackSidecars(context.Background(), list, map[string]*StackObservation{
			realHealProject: {Project: realHealProject, Running: 1, Total: 3},
		})
		time.Sleep(300 * time.Millisecond)
	}

	// 1) The one-shot was never started: FinishedAt unchanged, still Exited(0),
	// and it never consumed an attempt-counter entry.
	info, ierr := cli.ContainerInspect(context.Background(), oneshotID)
	if ierr != nil {
		t.Fatalf("inspect oneshot after heal: %v", ierr)
	}
	if info.State.FinishedAt != oneshotFinished {
		t.Fatalf("one-shot was restarted: FinishedAt moved %s -> %s", oneshotFinished, info.State.FinishedAt)
	}
	if info.State.ExitCode != 0 {
		t.Fatalf("one-shot ended in state %s (exit %d)", info.State.Status, info.State.ExitCode)
	}
	if _, ok := sidecarStartAttempts.Load(oneshotID); ok {
		t.Fatal("one-shot consumed an attempt-counter entry")
	}

	// 2) The exited-1 sidecar WAS retried to the cap, and warned once.
	if _, ok := sidecarStartAttempts.Load(flakyID); !ok {
		t.Fatal("flaky sidecar has no attempt-counter entry — it was never retried")
	}
	if sink.hits != 1 {
		t.Fatalf("expected exactly one still-down warning for the capped sidecar, got %d", sink.hits)
	}

	// 3) The keeper is untouched and still running.
	final, ferr := listHealContainers(cli)
	if ferr != nil {
		t.Fatalf("final list: %v", ferr)
	}
	if findService(final, "keeper") == "" || !containsRunning(final, "keeper") {
		t.Fatalf("keeper service is not running — the fixture broke: %s", describe(final))
	}
}

func containsRunning(containers []types.Container, service string) bool {
	for _, c := range containers {
		if strings.TrimSpace(c.Labels[composeServiceLabel]) == service && c.State == "running" {
			return true
		}
	}
	return false
}

func describe(containers []types.Container) string {
	var parts []string
	for _, c := range containers {
		parts = append(parts, c.Labels[composeServiceLabel]+":"+c.State)
	}
	return strings.Join(parts, " ")
}
