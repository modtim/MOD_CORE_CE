package executor

import (
	"context"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
)

// These exercise wasOOMKilled against a REAL kernel OOM kill rather than a
// mock, because the whole point of the change is that Docker's
// State.OOMKilled is the only way to distinguish "this host ran out of memory"
// (may succeed elsewhere) from "this code crashed" (fails everywhere). Both
// surface as exit 137, so a mock asserting our own assumption would prove
// nothing.
//
// Skipped when no Docker socket is available.

const oomTestImage = "python:3.11-slim"

func dockerOrSkip(t *testing.T) *client.Client {
	t.Helper()
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Skipf("no docker client: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := cli.Ping(ctx); err != nil {
		t.Skipf("docker unreachable: %v", err)
	}
	return cli
}

// runToCompletion starts a container with an optional hard memory cap and
// waits for it to exit, returning its ID.
func runToCompletion(t *testing.T, cli *client.Client, cmd []string, memBytes int64) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()

	hostCfg := &container.HostConfig{}
	if memBytes > 0 {
		// Memory == MemorySwap disables swap, so the cgroup has nowhere to
		// spill and the kernel must OOM-kill rather than thrash.
		hostCfg.Resources = container.Resources{Memory: memBytes, MemorySwap: memBytes}
	}

	resp, err := cli.ContainerCreate(ctx,
		&container.Config{Image: oomTestImage, Cmd: cmd},
		hostCfg, nil, nil, "")
	if err != nil {
		t.Skipf("cannot create container (image %s present?): %v", oomTestImage, err)
	}
	t.Cleanup(func() {
		rmCtx, rmCancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer rmCancel()
		_ = cli.ContainerRemove(rmCtx, resp.ID, container.RemoveOptions{Force: true})
	})

	if err := cli.ContainerStart(ctx, resp.ID, container.StartOptions{}); err != nil {
		t.Fatalf("start: %v", err)
	}
	statusCh, errCh := cli.ContainerWait(ctx, resp.ID, container.WaitConditionNotRunning)
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("wait: %v", err)
		}
	case <-statusCh:
	}
	return resp.ID
}

func TestWasOOMKilled_DetectsRealOOM(t *testing.T) {
	cli := dockerOrSkip(t)
	e := &Executor{docker: cli, log: slog.New(slog.NewTextHandler(os.Stderr, nil))}

	// Allocate well past a 32MB cap until the kernel intervenes.
	id := runToCompletion(t, cli, []string{"python", "-c",
		"b=[]\nwhile True:\n    b.append(bytearray(10*1024*1024))"}, 32*1024*1024)

	insCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	c, err := cli.ContainerInspect(insCtx, id)
	if err != nil {
		t.Fatalf("inspect: %v", err)
	}
	if !c.State.OOMKilled {
		t.Skipf("kernel did not report OOMKilled (exit %d) — cgroup memory "+
			"limits may not be enforced here", c.State.ExitCode)
	}

	if got := e.wasOOMKilled(context.Background(), id); !got {
		t.Fatalf("wasOOMKilled = false for a container Docker reports as "+
			"OOMKilled=true (exit %d) — the manager would treat a "+
			"memory-starved host as a code crash and never retry elsewhere",
			c.State.ExitCode)
	}
}

func TestWasOOMKilled_FalseOnCleanExit(t *testing.T) {
	cli := dockerOrSkip(t)
	e := &Executor{docker: cli, log: slog.New(slog.NewTextHandler(os.Stderr, nil))}

	id := runToCompletion(t, cli, []string{"python", "-c", "print('ok')"}, 0)

	if e.wasOOMKilled(context.Background(), id) {
		t.Fatal("wasOOMKilled = true for a cleanly-exited container — a false " +
			"OOM claim sends the manager rescheduling work that was never " +
			"memory-starved")
	}
}

func TestWasOOMKilled_FalseOnOrdinaryNonZeroExit(t *testing.T) {
	// The distinction that matters: a plain crash must NOT look like an OOM,
	// or every ordinary failure would get retried on a different host.
	cli := dockerOrSkip(t)
	e := &Executor{docker: cli, log: slog.New(slog.NewTextHandler(os.Stderr, nil))}

	id := runToCompletion(t, cli, []string{"python", "-c",
		"import sys; sys.exit(1)"}, 0)

	if e.wasOOMKilled(context.Background(), id) {
		t.Fatal("wasOOMKilled = true for an ordinary non-zero exit")
	}
}

func TestWasOOMKilled_FalseOnUnknownContainer(t *testing.T) {
	// Inspect failures must resolve to false, not a guess.
	cli := dockerOrSkip(t)
	e := &Executor{docker: cli, log: slog.New(slog.NewTextHandler(os.Stderr, nil))}

	if e.wasOOMKilled(context.Background(), "does-not-exist-0000") {
		t.Fatal("wasOOMKilled = true for a container that does not exist")
	}
}
